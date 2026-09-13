//! Remote Whisper ASR: ffmpeg audio extraction + OpenRouter transcription.
//!
//! No local model is ever loaded. Audio is extracted to a small 16 kHz mono
//! MP3 (fast upload, tiny temp disk), then POSTed as multipart to the
//! `whisper_stt` endpoint from `asrsub_providers.json`. Files over ~24 MB are
//! split into time-chunks and transcribed concurrently with timestamp offsets.
//! Segments are clamped to `<= MAX_CUE_MS` (default 8 s) and de-overlapped.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

use crate::providers::ProviderPool;
use crate::srt::{ensure_contiguous, split_long_cues, Cue};

pub const MAX_CUE_MS: u32 = 8000;
const CHUNK_BYTES: u64 = 24 * 1024 * 1024;

/// Audio track identity used for source choice: ffprobe index, language
/// tag, and title (commentary/audio-description tracks are titled).
#[derive(Debug, Clone)]
pub struct AudioStream {
    pub index: u32,
    pub language: Option<String>,
    pub title: Option<String>,
}

/// Chosen source track. `asr_lang` is that track's *real* normalized tag,
/// or `None` when the tag is missing/unusable — never a fabricated code.
#[derive(Debug, Clone)]
pub struct AudioChoice {
    pub stream_index: u32,
    /// Pinned source language; `None` means the tag is unknown and the
    /// request must omit `language` (detection).
    pub asr_lang: Option<String>,
    /// Known-tag answer. For a detection run (`asr_lang == None`) this is
    /// provisional (`true`); the caller recomputes it from the detected code.
    pub needs_translate: bool,
}

impl AudioChoice {
    /// True when the tag was missing/unusable: the ASR request must omit
    /// `language` on its first request and take the code from the response.
    pub fn detects_language(&self) -> bool {
        self.asr_lang.is_none()
    }

    /// Stable cache key before the effective language is known: the pinned
    /// tag, or the chosen stream for a detection run (so two targets that
    /// share one untagged track still pay for one transcription).
    pub fn cache_key(&self) -> String {
        match &self.asr_lang {
            Some(l) => l.clone(),
            None => format!("detect@{}", self.stream_index),
        }
    }
}

/// Commentary/audio-description marker used to skip descriptive tracks when
/// a normal alternative exists: their titles/tags often read `commentary`,
/// `comment`, `description`, `descriptive`, `SDH`.
fn is_commentary(s: &AudioStream) -> bool {
    let hay = format!(
        "{} {}",
        s.language.as_deref().unwrap_or(""),
        s.title.as_deref().unwrap_or("")
    )
    .to_lowercase();
    ["commentary", "comment", "description", "descriptive", "sdh"]
        .iter()
        .any(|k| hay.contains(k))
}

/// ffprobe audio streams for a container.
/// One ffprobe spawn for everything the episode pass needs from the
/// container: audio streams (source-track choice) + duration (timeline
/// span checks, ladder adequacy). Previously two spawns plus one more per
/// ladder adequacy check; ffprobe answers both sections in a single run.
pub struct MediaProbe {
    pub streams: Vec<AudioStream>,
    pub duration_s: Option<f64>,
    /// Container bit rate (b/s) for audio-size estimation; often absent.
    pub bit_rate: Option<u64>,
}

pub async fn probe_media(path: &str) -> Result<MediaProbe> {
    let out = tokio::process::Command::new("ffprobe")
        .args([
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_name,codec_type:stream_tags=language,title:format=duration,bit_rate",
            "-of",
            "json",
            path,
        ])
        .output()
        .await
        .context("spawn ffprobe")?;
    if !out.status.success() {
        anyhow::bail!("ffprobe failed: {}", String::from_utf8_lossy(&out.stderr));
    }
    let v: serde_json::Value = serde_json::from_slice(&out.stdout)?;
    let mut streams = Vec::new();
    for s in v
        .get("streams")
        .and_then(|x| x.as_array())
        .cloned()
        .unwrap_or_default()
    {
        if s.get("codec_type").and_then(|x| x.as_str()) != Some("audio") {
            continue;
        }
        streams.push(AudioStream {
            index: s.get("index").and_then(|x| x.as_u64()).unwrap_or(0) as u32,
            language: s
                .get("tags")
                .and_then(|t| t.get("language"))
                .and_then(|l| l.as_str())
                .map(str::to_string),
            title: s
                .get("tags")
                .and_then(|t| t.get("title"))
                .and_then(|t| t.as_str())
                .map(str::to_string),
        });
    }
    let duration_s = v
        .get("format")
        .and_then(|f| f.get("duration"))
        .and_then(|d| d.as_str())
        .and_then(|d| d.parse().ok());
    let bit_rate = v
        .get("format")
        .and_then(|f| f.get("bit_rate"))
        .and_then(|b| {
            b.as_str()
                .and_then(|s| s.parse().ok())
                .or_else(|| b.as_u64())
        });
    Ok(MediaProbe {
        streams,
        duration_s,
        bit_rate,
    })
}
/// Source-track choice, called once per target language. Order:
///
/// 1. a track whose normalized language equals the **target** (no
///    translation needed — generalizes the old `en` special case to every
///    target, e.g. an `id` target with an `ind` track);
/// 2. else a track matching the media's **original language**, when known
///    and different from the target;
/// 3. else a `ja`-tagged track;
/// 4. else an `en`-tagged track;
/// 5. else the first non-commentary track; else `streams[0]`.
///
/// The tag only chooses the track: the language sent to Whisper is that
/// tag's real code, or nothing (detection) when the tag is missing. The old
/// implementation forced `ja` for any other tag, so a `fre` track was
/// transcribed as Japanese and produced a fabricated translation (the
/// defect this fixes). Commentary/description tracks are skipped whenever a
/// non-commentary alternative exists.
pub fn choose_source(
    streams: &[AudioStream],
    target_lang: &str,
    original_lang: Option<&str>,
) -> Option<AudioChoice> {
    if streams.is_empty() {
        return None;
    }
    let target = crate::lang::normalize_lang(target_lang);
    let original = original_lang.map(crate::lang::normalize_lang);
    let norm = |s: &AudioStream| s.language.as_deref().map(crate::lang::normalize_lang);
    let find = |want: &str| {
        streams
            .iter()
            .find(|s| !is_commentary(s) && norm(s).as_deref() == Some(want))
    };
    let picked = find(&target)
        .or_else(|| {
            original
                .as_deref()
                .filter(|o| !o.is_empty() && *o != target)
                .and_then(find)
        })
        .or_else(|| find("ja"))
        .or_else(|| find("en"))
        .or_else(|| streams.iter().find(|s| !is_commentary(s)))
        .or_else(|| streams.first())?;
    let asr_lang = norm(picked);
    // Known tag: translate unless the source already is the target.
    // Unknown tag: decided from the detected language after transcription.
    let needs_translate = asr_lang.as_deref().map(|l| l != target).unwrap_or(true);
    Some(AudioChoice {
        stream_index: picked.index,
        asr_lang,
        needs_translate,
    })
}

/// Extract one audio track to a compact MP3 for upload.
pub async fn extract_audio(media: &str, stream_index: u32, dest: &Path) -> Result<()> {
    if let Some(p) = dest.parent() {
        if !p.as_os_str().is_empty() {
            tokio::fs::create_dir_all(p).await?;
        }
    }
    let out = tokio::process::Command::new("ffmpeg")
        .args([
            "-v",
            "error",
            "-y",
            "-i",
            media,
            "-map",
            &format!("0:{stream_index}"),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            &dest.to_string_lossy(),
        ])
        .output()
        .await
        .context("spawn ffmpeg")?;
    if !out.status.success() {
        anyhow::bail!(
            "ffmpeg extract failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
    }
    Ok(())
}

/// Text fields for one Whisper request. `language` is omitted entirely when
/// the tag is unknown (`None`): detection, never a fabricated code.
fn whisper_text_fields(model: &str, lang: Option<&str>) -> Vec<(&'static str, String)> {
    let mut fields = vec![
        ("model", model.to_string()),
        ("response_format", "verbose_json".to_string()),
    ];
    if let Some(l) = lang {
        fields.push(("language", l.to_string()));
    }
    fields
}

/// Shared multipart body for both transcription paths: model +
/// `verbose_json` + (optional) language + file. One constructor so the two
/// callers cannot drift (field names, response format).
fn whisper_form(
    model: &str,
    lang: Option<&str>,
    bytes: Vec<u8>,
    filename: String,
) -> Result<reqwest::multipart::Form> {
    let part = reqwest::multipart::Part::bytes(bytes)
        .file_name(filename)
        .mime_str("audio/mpeg")?;
    let mut form = reqwest::multipart::Form::new();
    for (k, v) in whisper_text_fields(model, lang) {
        form = form.text(k, v);
    }
    Ok(form.part("file", part))
}

/// Detected language from a `verbose_json` response's top-level `language`
/// field (previously discarded — only `segments` was read). No usable code
/// is an error, never a guess: the language fails instead of committing a
/// subtitle whose source we cannot establish.
fn detected_lang(v: &serde_json::Value) -> Result<String> {
    let raw = v
        .get("language")
        .and_then(|l| l.as_str())
        .unwrap_or("")
        .trim();
    let code = crate::lang::normalize_lang(raw);
    if code.is_empty() {
        anyhow::bail!(
            "whisper returned no detected language (no usable `language` in verbose_json)"
        );
    }
    Ok(code)
}

/// Effective source language for one transcription: a pinned tag is used
/// as-is; an unforced (detection) response must carry a usable code.
fn effective_lang(pinned: Option<&str>, v: &serde_json::Value) -> Result<String> {
    match pinned {
        Some(l) => Ok(l.to_string()),
        None => detected_lang(v),
    }
}

/// Fail closed when a chunk reports a language other than the code we
/// pinned. A forced run cannot self-verify (the provider echoes the field),
/// so a contradiction means the transcript's source is unknown and the
/// language must error rather than be committed.
fn check_pinned_lang(pinned: &str, v: &serde_json::Value) -> Result<()> {
    if let Some(got) = v.get("language").and_then(|l| l.as_str()) {
        let got = crate::lang::normalize_lang(got);
        if !got.is_empty() && got != pinned {
            anyhow::bail!("whisper language mismatch: pinned {pinned}, reported {got}");
        }
    }
    Ok(())
}

/// One transcription request against every configured Whisper endpoint in
/// order (primary, then `whisper_stt_fallbacks`): per-endpoint semaphore,
/// per-endpoint breaker records. Transport errors and non-2xx fall through
/// to the next endpoint; the last error surfaces when all fail.
async fn whisper_request(
    pool: &ProviderPool,
    bytes: Vec<u8>,
    filename: String,
    lang: Option<&str>,
) -> Result<serde_json::Value> {
    let endpoints = pool.ordered_whisper();
    if endpoints.is_empty() {
        anyhow::bail!("no whisper_stt provider configured");
    }
    let mut last_err = String::from("no whisper endpoints attempted");
    for (idx, wp) in endpoints {
        if wp.api_key().is_empty() {
            tracing::debug!(
                endpoint = %crate::config::mask_for_log(&wp.endpoint),
                "whisper: skipping keyless endpoint"
            );
            continue;
        }
        let _permit = pool.acquire_whisper(idx).await;
        let form = whisper_form(&wp.model, lang, bytes.clone(), filename.clone())?;
        // The reqwest-level timeout is the single deadline (no outer
        // tokio::time::timeout wrapper): one mechanism, both paths.
        let resp = pool
            .http()
            .post(&wp.endpoint)
            .header("Authorization", format!("Bearer {}", wp.api_key()))
            .multipart(form)
            .timeout(ProviderPool::whisper_timeout())
            .send()
            .await;
        let resp = match resp {
            Err(e) => {
                pool.record_whisper_failure(idx);
                last_err = format!(
                    "{}: transport: {}",
                    crate::config::mask_for_log(&wp.endpoint),
                    crate::config::mask_for_log(&format!("{e:#}"))
                );
                continue;
            }
            Ok(r) => r,
        };
        if !resp.status().is_success() {
            let code = resp.status().as_u16();
            let body = resp.text().await.unwrap_or_default();
            pool.record_whisper_failure(idx);
            last_err = format!(
                "{}: HTTP {code}: {}",
                crate::config::mask_for_log(&wp.endpoint),
                crate::srt::snippet(&body)
            );
            continue;
        }
        pool.record_whisper_success(idx);
        return resp.json().await.context("decode whisper response");
    }
    anyhow::bail!("all whisper endpoints failed; last: {last_err}")
}

/// Whole-file transcription (one request). A known tag pins the language;
/// an unknown tag sends no `language` and takes the effective code from the
/// response. Returns the cues plus that effective language.
async fn transcribe_file(
    pool: &ProviderPool,
    file: &Path,
    lang: Option<&str>,
) -> Result<Transcript> {
    if pool.whisper_banned() {
        // Fail fast while every breaker is open instead of burning timeouts.
        anyhow::bail!("whisper circuit open (recent failures); deferring");
    }
    let bytes = tokio::fs::read(file).await?;
    let name = file
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("audio.mp3")
        .to_string();
    let v = whisper_request(pool, bytes, name, lang).await?;
    Ok(Transcript {
        cues: cues_from_verbose(&v, 0),
        lang: effective_lang(lang, &v)?,
    })
}

fn cues_from_verbose(v: &serde_json::Value, offset_ms: u32) -> Vec<Cue> {
    let mut cues = Vec::new();
    // verbose_json: {segments:[{start,end,text}], text?...}
    if let Some(segs) = v.get("segments").and_then(|s| s.as_array()) {
        for s in segs {
            let start = s.get("start").and_then(|x| x.as_f64()).unwrap_or(0.0);
            let end = s.get("end").and_then(|x| x.as_f64()).unwrap_or(0.0);
            let text = s.get("text").and_then(|x| x.as_str()).unwrap_or("").trim();
            if text.is_empty() {
                continue;
            }
            cues.push(Cue::new(
                offset_ms + (start * 1000.0) as u32,
                offset_ms + (end * 1000.0) as u32,
                text.to_string(),
            ));
        }
    } else if let Some(text) = v.get("text").and_then(|x| x.as_str()) {
        // Plain json fallback: single cue; duration unknown -> caller splits.
        let text = text.trim();
        if !text.is_empty() {
            cues.push(Cue::new(offset_ms, offset_ms + 8000, text.to_string()));
        }
    }
    cues
}

/// One episode's transcription: cues plus the effective source language —
/// the pinned tag, or the code detected on the unforced first request.
/// The language is what decides `needs_translate` and the provenance row.
#[derive(Debug, Clone)]
pub struct Transcript {
    pub cues: Vec<Cue>,
    pub lang: String,
}

/// Full ASR for one episode: extract -> (chunked) remote transcribe ->
/// contiguous `<=max_cue_ms` cues. Temp audio is removed on every path,
/// including transcription failures (a `Drop` guard owns the cleanup, not
/// the tail). `fanout` bounds concurrent piece transcriptions (the shared
/// whisper semaphore + breaker apply on top).
///
/// Routing avoids a wasted full transcode: when the probe already shows
/// the audio exceeds `CHUNK_BYTES`, pieces split straight from the media
/// (the full.mp3 extract would be transcoded and deleted unused). The
/// estimate only ever skips work that is provably redundant — unknown or
/// borderline sizes take the old extract-then-measure path, and the chunk
/// layout uses the probed duration (no third ffprobe).
pub struct TranscribeJob<'a> {
    pub tmp_dir: &'a Path,
    pub media_path: &'a str,
    pub choice: &'a AudioChoice,
    pub episode_key: &'a str,
    /// Probed container duration (piece layout; 1 h fallback like before).
    pub duration_s: Option<f64>,
    /// Estimated audio bytes from the probe (chunk routing; None measures).
    pub audio_bytes: Option<u64>,
    pub fanout: usize,
    pub max_cue_ms: u32,
}

/// Estimated audio bytes from probe facts; None when unknowable. Pure and
/// unit-tested: the estimate routes, never decides correctness.
pub fn est_audio_bytes(duration_s: Option<f64>, bit_rate: Option<u64>) -> Option<u64> {
    let dur = duration_s.filter(|d| *d > 0.0)?;
    let rate = bit_rate.filter(|b| *b > 0)?;
    Some((dur * rate as f64 / 8.0) as u64)
}

pub async fn transcribe_episode(pool: &ProviderPool, job: TranscribeJob<'_>) -> Result<Transcript> {
    let (tmp_dir, media_path, choice, episode_key) =
        (job.tmp_dir, job.media_path, job.choice, job.episode_key);
    let base = tmp_dir.join(format!("asr-{episode_key}"));
    tokio::fs::create_dir_all(&base).await?;
    // Scope guard: best-effort temp cleanup even when `?` early-returns.
    struct TempDir<'a>(&'a Path);
    impl Drop for TempDir<'_> {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(self.0);
        }
    }
    let _cleanup = TempDir(&base);
    let full = base.join("full.mp3");
    // Skip the full extract when the probe already proves chunking: the
    // extract would be transcoded and deleted without ever being read.
    let mut transcript = if !job.audio_bytes.is_some_and(|b| b > CHUNK_BYTES) {
        extract_audio(media_path, choice.stream_index, &full).await?;
        let size = tokio::fs::metadata(&full)
            .await
            .map(|m| m.len())
            .unwrap_or(0);
        if size <= CHUNK_BYTES {
            transcribe_file(pool, &full, choice.asr_lang.as_deref()).await?
        } else {
            transcribe_pieces(
                pool,
                &base,
                media_path,
                choice,
                job.duration_s.unwrap_or(3600.0),
                job.fanout,
            )
            .await?
        }
    } else {
        transcribe_pieces(
            pool,
            &base,
            media_path,
            choice,
            job.duration_s.unwrap_or(3600.0),
            job.fanout,
        )
        .await?
    };
    ensure_contiguous(&mut transcript.cues);
    transcript.cues = split_long_cues(transcript.cues, job.max_cue_ms);
    Ok(transcript)
}

/// Split-by-duration piece transcription shared by both routing paths.
/// A known tag pins every piece. An unknown tag transcribes the FIRST piece
/// unforced, takes the detected code from its response, and pins the rest to
/// that code — so a foreign-language file is never transcribed as Japanese.
async fn transcribe_pieces(
    pool: &ProviderPool,
    base: &Path,
    media_path: &str,
    choice: &AudioChoice,
    dur: f64,
    fanout: usize,
) -> Result<Transcript> {
    // Split by duration into ~10 min pieces, at most 24. ffmpeg splits
    // run under a small semaphore (disk/CPU bound); transcription of the
    // pieces goes through the shared whisper semaphore + breaker.
    let piece = 600.0;
    let n = ((dur / piece).ceil() as usize).clamp(2, 24);
    let split_sem = std::sync::Arc::new(tokio::sync::Semaphore::new(4));
    let mut jobs = Vec::with_capacity(n);
    for k in 0..n {
        let start = k as f64 * dur / n as f64;
        let len = dur / n as f64;
        let dest = base.join(format!("part{k:02}.mp3"));
        let media = media_path.to_string();
        let idx = choice.stream_index;
        let split_sem = split_sem.clone();
        jobs.push(async move {
            let _p = split_sem.acquire_owned().await.expect("semaphore closed");
            let out = tokio::process::Command::new("ffmpeg")
                .args([
                    "-v",
                    "error",
                    "-y",
                    "-ss",
                    &format!("{start:.1}"),
                    "-t",
                    &format!("{len:.1}"),
                    "-i",
                    &media,
                    "-map",
                    &format!("0:{idx}"),
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "64k",
                    &dest.to_string_lossy(),
                ])
                .output()
                .await?;
            if !out.status.success() {
                anyhow::bail!("ffmpeg split failed");
            }
            Ok::<_, anyhow::Error>((dest, (start * 1000.0) as u32))
        });
    }
    let parts: Vec<(PathBuf, u32)> = futures::future::try_join_all(jobs).await?;
    let (lang, mut all, rest) = match choice.asr_lang.clone() {
        Some(pinned) => (pinned, Vec::new(), parts.as_slice()),
        None => {
            // Detection: the first piece must be alone (its response is the
            // only place the language appears).
            let ((dest, off), rest) = parts.split_first().context("no audio pieces")?;
            let v = post_chunk(pool, dest, None).await?;
            let detected = detected_lang(&v)?;
            let all = cues_from_verbose(&v, *off);
            (detected, all, rest)
        }
    };
    // Bounded concurrent transcription of the remaining pieces, pinned to
    // the language we established.
    let sem = std::sync::Arc::new(tokio::sync::Semaphore::new(fanout.max(1)));
    let mut tjobs = Vec::with_capacity(rest.len());
    for (dest, off) in rest {
        let pool = pool.clone();
        let sem = sem.clone();
        let lang = lang.clone();
        let verify = choice.detects_language();
        let off = *off;
        tjobs.push(async move {
            let _p = sem.acquire_owned().await.expect("semaphore closed");
            let v = post_chunk(&pool, dest, Some(&lang)).await?;
            if verify {
                check_pinned_lang(&lang, &v)?;
            }
            Ok::<_, anyhow::Error>(cues_from_verbose(&v, off))
        });
    }
    for v in futures::future::try_join_all(tjobs).await? {
        all.extend(v);
    }
    Ok(Transcript { cues: all, lang })
}

/// One piece request; `lang == None` omits `language` (detection probe).
async fn post_chunk(
    pool: &ProviderPool,
    file: &Path,
    lang: Option<&str>,
) -> Result<serde_json::Value> {
    if pool.whisper_banned() {
        anyhow::bail!("whisper circuit open (recent failures); deferring");
    }
    let bytes = tokio::fs::read(file).await?;
    whisper_request(pool, bytes, "part.mp3".to_string(), lang).await
}

#[cfg(test)]
mod tests {
    use super::*;

    fn track(index: u32, lang: Option<&str>, title: Option<&str>) -> AudioStream {
        AudioStream {
            index,
            language: lang.map(str::to_string),
            title: title.map(str::to_string),
        }
    }

    fn streams() -> Vec<AudioStream> {
        vec![track(0, Some("jpn"), None), track(1, Some("eng"), None)]
    }

    #[test]
    fn en_target_uses_en_track_without_translation() {
        let c = choose_source(&streams(), "en", None).unwrap();
        assert_eq!(c.stream_index, 1);
        assert_eq!(c.asr_lang.as_deref(), Some("en"));
        assert!(!c.needs_translate);
    }

    #[test]
    fn id_target_uses_ja_track_with_translation() {
        let c = choose_source(&streams(), "id", None).unwrap();
        assert_eq!(c.stream_index, 0);
        assert_eq!(c.asr_lang.as_deref(), Some("ja"));
        assert!(c.needs_translate);
    }

    #[test]
    fn empty_streams_choose_nothing() {
        assert!(choose_source(&[], "id", None).is_none());
    }

    #[test]
    fn id_target_prefers_english_over_french_when_no_original() {
        // No ja track, original unknown: the en-tagged track wins over the
        // first (French) track.
        let s = vec![track(1, Some("fre"), None), track(2, Some("eng"), None)];
        let c = choose_source(&s, "id", None).unwrap();
        assert_eq!(c.stream_index, 2);
        assert_eq!(c.asr_lang.as_deref(), Some("en"));
        assert!(c.needs_translate);
    }

    #[test]
    fn french_only_track_never_invents_japanese() {
        // The exact defect: `fre` (stream 1) + `eng` (stream 2) with no ja
        // track, targeting id, transcribed French audio as Japanese. A
        // French-only file must pin `fr`, never `ja`.
        let s = vec![track(1, Some("fre"), None)];
        let c = choose_source(&s, "id", None).unwrap();
        assert_eq!(c.stream_index, 1);
        assert_eq!(c.asr_lang.as_deref(), Some("fr"));
        assert_ne!(c.asr_lang.as_deref(), Some("ja"));
        assert!(c.needs_translate);
    }

    #[test]
    fn indonesian_track_needs_no_translation() {
        // Target-track match generalizes the old `en`-only special case.
        let s = vec![track(0, Some("ind"), None), track(1, Some("jpn"), None)];
        let c = choose_source(&s, "id", None).unwrap();
        assert_eq!(c.stream_index, 0);
        assert_eq!(c.asr_lang.as_deref(), Some("id"));
        assert!(!c.needs_translate);
    }

    #[test]
    fn untagged_track_switches_to_detection() {
        let s = vec![track(0, None, None)];
        let c = choose_source(&s, "id", None).unwrap();
        assert_eq!(c.stream_index, 0);
        assert!(c.asr_lang.is_none());
        assert!(c.detects_language());
        // Provisional; the caller recomputes it from the detected code.
        assert!(c.needs_translate);
    }

    #[test]
    fn original_language_beats_ja_and_en_fallbacks() {
        // Sonarr/Radarr says the media is French; the `jpn` and `eng` tracks
        // must not win over the real original.
        let s = vec![
            track(0, Some("jpn"), None),
            track(1, Some("fre"), None),
            track(2, Some("eng"), None),
        ];
        let c = choose_source(&s, "id", Some("French")).unwrap();
        assert_eq!(c.stream_index, 1);
        assert_eq!(c.asr_lang.as_deref(), Some("fr"));
        // An original equal to the target adds nothing: target wins first.
        let c = choose_source(&s, "fr", Some("fr")).unwrap();
        assert_eq!(c.stream_index, 1);
        assert!(!c.needs_translate);
    }

    #[test]
    fn commentary_tracks_are_skipped_when_alternatives_exist() {
        let s = vec![
            track(0, Some("jpn"), Some("Japanese Commentary")),
            track(1, Some("jpn"), Some("Japanese")),
        ];
        let c = choose_source(&s, "id", None).unwrap();
        assert_eq!(c.stream_index, 1);
        // The target track is also skipped when only a commentary variant
        // exists: a descriptive track is never the only choice.
        let s = vec![
            track(0, Some("eng"), Some("English Descriptive Audio")),
            track(1, Some("jpn"), None),
        ];
        let c = choose_source(&s, "en", None).unwrap();
        assert_eq!(c.stream_index, 1);
        assert_eq!(c.asr_lang.as_deref(), Some("ja"));
        // Nothing but commentary/description tracks: still pick one rather
        // than give up (streams[0]).
        let s = vec![track(0, Some("eng"), Some("SDH"))];
        let c = choose_source(&s, "id", None).unwrap();
        assert_eq!(c.stream_index, 0);
    }

    #[test]
    fn cache_key_is_stable_before_the_language_is_known() {
        let pinned = AudioChoice {
            stream_index: 1,
            asr_lang: Some("ja".into()),
            needs_translate: true,
        };
        assert_eq!(pinned.cache_key(), "ja");
        let detected = AudioChoice {
            stream_index: 1,
            asr_lang: None,
            needs_translate: true,
        };
        assert_eq!(detected.cache_key(), "detect@1");
    }

    #[test]
    fn size_estimate_routes_only_when_provable() {
        // 24 MB threshold: provably-big skips the full extract, everything
        // else measures (the estimate routes, never decides correctness).
        assert!(est_audio_bytes(Some(3600.0), Some(64000)).unwrap() > 24 * 1024 * 1024);
        assert!(est_audio_bytes(Some(300.0), Some(64000)).unwrap() < 24 * 1024 * 1024);
        assert_eq!(est_audio_bytes(None, Some(64000)), None);
        assert_eq!(est_audio_bytes(Some(300.0), None), None);
        assert_eq!(est_audio_bytes(Some(0.0), Some(64000)), None);
        assert_eq!(est_audio_bytes(Some(300.0), Some(0)), None);
    }

    #[test]
    fn probe_omits_language_and_pinned_requests_carry_it() {
        // The probe chunk (unknown tag) must carry NO `language` field;
        // the follow-up chunks carry the detected code.
        let probe = whisper_text_fields("m", None);
        assert!(!probe.iter().any(|(k, _)| *k == "language"), "{probe:?}");
        assert!(probe.iter().any(|(k, _)| *k == "response_format"));
        let pinned = whisper_text_fields("m", Some("fr"));
        assert!(pinned.iter().any(|(k, v)| *k == "language" && v == "fr"));
    }

    #[test]
    fn detection_failure_errors_and_contradictions_fail_closed() {
        // A response without a usable `language` cannot establish the source.
        assert!(detected_lang(&serde_json::json!({"segments": []})).is_err());
        assert!(detected_lang(&serde_json::json!({"language": ""})).is_err());
        assert!(effective_lang(None, &serde_json::json!({})).is_err());
        // A pinned tag is used as-is (normalized), detection never overrides.
        assert_eq!(
            effective_lang(Some("fr"), &serde_json::json!({"language": "ja"})).unwrap(),
            "fr"
        );
        assert_eq!(
            detected_lang(&serde_json::json!({"language": "fr"})).unwrap(),
            "fr"
        );
        assert_eq!(
            detected_lang(&serde_json::json!({"language": "French"})).unwrap(),
            "fr"
        );
        // A chunk that contradicts the code we pinned fails the language.
        assert!(check_pinned_lang("fr", &serde_json::json!({"language": "fr"})).is_ok());
        assert!(check_pinned_lang("fr", &serde_json::json!({})).is_ok());
        assert!(check_pinned_lang("fr", &serde_json::json!({"language": "ja"})).is_err());
    }

    /// End-to-end request plumbing against a local stub: the probe is
    /// unforced, the pinned request carries the code, and a response with no
    /// language cannot be resolved.
    #[tokio::test]
    async fn probe_request_omits_language_and_pinned_request_sends_it() {
        use std::sync::{Arc, Mutex};
        let seen: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
        let app = axum::Router::new().route(
            "/stt",
            axum::routing::post({
                let seen = seen.clone();
                move |body: axum::body::Bytes| {
                    let seen = seen.clone();
                    async move {
                        seen.lock()
                            .unwrap()
                            .push(String::from_utf8_lossy(&body).to_string());
                        axum::Json(serde_json::json!({"language": "fr", "segments": []}))
                    }
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move {
            let _ = axum::serve(listener, app).await;
        });
        let pool = ProviderPool::new(
            crate::providers::ProvidersFile {
                llm_translation_models: vec![],
                whisper_stt: Some(crate::providers::WhisperProvider {
                    endpoint: format!("{base}/stt"),
                    model: "stub".into(),
                    key_env: String::new(),
                    api_key: "x".into(),
                }),
                whisper_stt_fallbacks: vec![],
            },
            reqwest::Client::new(),
        );
        let header = "Content-Disposition: form-data; name=\"language\"";
        let probe = whisper_request(&pool, b"probe".to_vec(), "part.mp3".into(), None)
            .await
            .unwrap();
        assert_eq!(detected_lang(&probe).unwrap(), "fr");
        whisper_request(&pool, b"rest".to_vec(), "part.mp3".into(), Some("fr"))
            .await
            .unwrap();
        let bodies = seen.lock().unwrap().clone();
        assert_eq!(bodies.len(), 2);
        assert!(
            !bodies[0].contains(header),
            "probe must be unforced: {}",
            bodies[0]
        );
        assert!(
            bodies[1].contains(header),
            "pinned chunk must carry the code: {}",
            bodies[1]
        );
    }
}
