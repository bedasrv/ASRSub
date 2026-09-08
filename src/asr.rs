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

#[derive(Debug, Clone)]
pub struct AudioStream {
    pub index: u32,
    pub codec: String,
    pub language: Option<String>,
}

#[derive(Debug, Clone)]
pub struct AudioChoice {
    pub stream_index: u32,
    pub asr_lang: String,
    pub needs_translate: bool,
}

/// ffprobe audio streams for a container.
pub async fn probe_audio(path: &str) -> Result<Vec<AudioStream>> {
    let out = tokio::process::Command::new("ffprobe")
        .args([
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_name,codec_type:stream_tags=language",
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
            codec: s
                .get("codec_name")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string(),
            language: s
                .get("tags")
                .and_then(|t| t.get("language"))
                .and_then(|l| l.as_str())
                .map(str::to_string),
        });
    }
    Ok(streams)
}

/// Source-track choice, mirroring `choose_source` in orchestrator.py.
///
/// Called once per target language: an `en` target with an English-tagged
/// track transcribes it directly (`needs_translate == false`); otherwise the
/// Japanese-tagged track (or first track) is transcribed as `asr_lang`.
///
/// Deliberate deviation: unknown/untagged tracks default to `"ja"`, not
/// Python's `"en"`. For an anime library undetermined ≈ Japanese, and
/// forcing Whisper `language=en` on Japanese audio mistranscribes the whole
/// episode; mistranscribing hypothetical English audio as Japanese is the
/// far rarer failure.
pub fn choose_source(streams: &[AudioStream], target_lang: &str) -> Option<AudioChoice> {
    if streams.is_empty() {
        return None;
    }
    let target = crate::lang::normalize_lang(target_lang);
    if target == "en" {
        if let Some(s) = streams.iter().find(|s| {
            s.language
                .as_deref()
                .map(crate::lang::normalize_lang)
                .as_deref()
                == Some("en")
        }) {
            return Some(AudioChoice {
                stream_index: s.index,
                asr_lang: "en".to_string(),
                needs_translate: false,
            });
        }
    }
    let s = streams
        .iter()
        .find(|s| {
            s.language
                .as_deref()
                .map(crate::lang::normalize_lang)
                .as_deref()
                == Some("ja")
        })
        .unwrap_or(&streams[0]);
    let tag = s.language.as_deref().unwrap_or("");
    let norm = crate::lang::normalize_lang(tag);
    let asr_lang = if norm == "ja" || norm == "en" {
        norm
    } else {
        "ja".to_string()
    };
    let needs_translate = asr_lang != target;
    Some(AudioChoice {
        stream_index: s.index,
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

/// Shared multipart body for both transcription paths: model +
/// `verbose_json` + language + file. One constructor so the two callers
/// cannot drift (field names, response format).
fn whisper_form(
    model: &str,
    lang: &str,
    bytes: Vec<u8>,
    filename: String,
) -> Result<reqwest::multipart::Form> {
    let part = reqwest::multipart::Part::bytes(bytes)
        .file_name(filename)
        .mime_str("audio/mpeg")?;
    Ok(reqwest::multipart::Form::new()
        .text("model", model.to_string())
        .text("response_format", "verbose_json")
        .text("language", lang.to_string())
        .part("file", part))
}

async fn transcribe_file(pool: &ProviderPool, file: &Path, lang: &str) -> Result<Vec<Cue>> {
    if pool.whisper_banned() {
        // Fail fast while the breaker is open instead of burning timeouts.
        anyhow::bail!("whisper circuit open (recent failures); deferring");
    }
    let whisper = pool
        .whisper()
        .context("no whisper_stt provider configured")?
        .clone();
    let _permit = pool.acquire_whisper().await;
    let bytes = tokio::fs::read(file).await?;
    let name = file
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("audio.mp3")
        .to_string();
    let form = whisper_form(&whisper.model, lang, bytes, name)?;
    // The reqwest-level timeout below is the single deadline (no outer
    // tokio::time::timeout wrapper): one mechanism, both paths.
    let resp = match pool
        .http()
        .post(&whisper.endpoint)
        .header("Authorization", format!("Bearer {}", whisper.api_key()))
        .multipart(form)
        .timeout(ProviderPool::whisper_timeout())
        .send()
        .await
    {
        Err(e) => {
            pool.record_whisper_failure();
            return Err(e.into());
        }
        Ok(r) => r,
    };
    if !resp.status().is_success() {
        let code = resp.status().as_u16();
        let body = resp.text().await.unwrap_or_default();
        pool.record_whisper_failure();
        anyhow::bail!(
            "whisper HTTP {code}: {}",
            body.chars().take(300).collect::<String>()
        );
    }
    pool.record_whisper_success();
    let v: serde_json::Value = resp.json().await?;
    Ok(cues_from_verbose(&v, 0))
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

/// Duration via ffprobe (seconds), None on failure.
pub async fn media_duration_s(path: &str) -> Option<f64> {
    let out = tokio::process::Command::new("ffprobe")
        .args([
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            path,
        ])
        .output()
        .await
        .ok()?;
    let v: serde_json::Value = serde_json::from_slice(&out.stdout).ok()?;
    v.get("format")?.get("duration")?.as_str()?.parse().ok()
}

/// Full ASR for one episode: extract -> (chunked) remote transcribe ->
/// contiguous `<=8s` cues. Temp audio is removed on every path, including
/// transcription failures (a `Drop` guard owns the cleanup, not the tail).
/// `fanout` bounds concurrent piece transcriptions (the shared whisper
/// semaphore + breaker apply on top).
pub async fn transcribe_episode(
    pool: &ProviderPool,
    tmp_dir: &Path,
    media_path: &str,
    choice: &AudioChoice,
    episode_key: &str,
    fanout: usize,
) -> Result<Vec<Cue>> {
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
    extract_audio(media_path, choice.stream_index, &full).await?;
    let size = tokio::fs::metadata(&full)
        .await
        .map(|m| m.len())
        .unwrap_or(0);
    let mut cues = if size <= CHUNK_BYTES {
        transcribe_file(pool, &full, &choice.asr_lang).await?
    } else {
        // Split by duration into ~10 min pieces, at most 24. ffmpeg splits
        // run under a small semaphore (disk/CPU bound); transcription of the
        // pieces goes through the shared whisper semaphore + breaker.
        let dur = media_duration_s(media_path).await.unwrap_or(3600.0);
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
        // Bounded concurrent transcription of the pieces.
        let sem = std::sync::Arc::new(tokio::sync::Semaphore::new(fanout.max(1)));
        let mut tjobs = Vec::with_capacity(parts.len());
        for (dest, off) in parts {
            let pool = pool.clone();
            let sem = sem.clone();
            let lang = choice.asr_lang.clone();
            tjobs.push(async move {
                let _p = sem.acquire_owned().await.expect("semaphore closed");
                let v = transcribe_chunk(&pool, &dest, &lang, off).await?;
                Ok::<_, anyhow::Error>(v)
            });
        }
        let mut all = Vec::new();
        for v in futures::future::try_join_all(tjobs).await? {
            all.extend(v);
        }
        all
    };
    ensure_contiguous(&mut cues);
    Ok(split_long_cues(cues, MAX_CUE_MS))
}

async fn transcribe_chunk(
    pool: &ProviderPool,
    file: &Path,
    lang: &str,
    offset_ms: u32,
) -> Result<Vec<Cue>> {
    if pool.whisper_banned() {
        anyhow::bail!("whisper circuit open (recent failures); deferring");
    }
    let whisper = pool
        .whisper()
        .context("no whisper_stt provider configured")?
        .clone();
    let _permit = pool.acquire_whisper().await;
    let bytes = tokio::fs::read(file).await?;
    let form = whisper_form(&whisper.model, lang, bytes, "part.mp3".to_string())?;
    let resp = pool
        .http()
        .post(&whisper.endpoint)
        .header("Authorization", format!("Bearer {}", whisper.api_key()))
        .multipart(form)
        .timeout(ProviderPool::whisper_timeout())
        .send()
        .await;
    let resp = match resp {
        Ok(r) => r,
        Err(e) => {
            pool.record_whisper_failure();
            return Err(e.into());
        }
    };
    if !resp.status().is_success() {
        let code = resp.status().as_u16();
        let body = resp.text().await.unwrap_or_default();
        pool.record_whisper_failure();
        anyhow::bail!(
            "whisper chunk HTTP {code}: {}",
            body.chars().take(200).collect::<String>()
        );
    }
    pool.record_whisper_success();
    let v: serde_json::Value = resp.json().await?;
    Ok(cues_from_verbose(&v, offset_ms))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn streams() -> Vec<AudioStream> {
        vec![
            AudioStream {
                index: 0,
                codec: "aac".into(),
                language: Some("jpn".into()),
            },
            AudioStream {
                index: 1,
                codec: "aac".into(),
                language: Some("eng".into()),
            },
        ]
    }

    #[test]
    fn en_target_uses_en_track_without_translation() {
        let c = choose_source(&streams(), "en").unwrap();
        assert_eq!(c.stream_index, 1);
        assert_eq!(c.asr_lang, "en");
        assert!(!c.needs_translate);
    }

    #[test]
    fn id_target_uses_ja_track_with_translation() {
        let c = choose_source(&streams(), "id").unwrap();
        assert_eq!(c.stream_index, 0);
        assert_eq!(c.asr_lang, "ja");
        assert!(c.needs_translate);
    }

    #[test]
    fn empty_streams_choose_nothing() {
        assert!(choose_source(&[], "id").is_none());
    }
}
