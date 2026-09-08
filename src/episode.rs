//! Per-episode processing: source choice → translate → commit.
//!
//! For each missing language: ladder text first ([`crate::ladder`]), else
//! remote ASR (transcriptions shared across the episode's languages via a
//! per-`asr_lang` cache, so `id`+`en` pay for one Japanese transcription),
//! then remote translation keeping source timing, CPS merge, atomic install,
//! Bazarr upload, and registry/state commits.

use std::path::Path;

use anyhow::{Context, Result};

use crate::asr;
use crate::ladder::LadderQuery;
use crate::lang::normalize_lang;
use crate::pipeline::{Candidate, Pipeline};
use crate::sonarr::Episode;
use crate::srt::{self, Cue};
use crate::state::{self, RegistryRow, StateEntry};

/// Registry commit payload: one struct instead of positional args.
struct RegistryCommit<'a> {
    stem: &'a str,
    lang: &'a str,
    source: &'a str,
    source_kind: Option<&'a str>,
    episode_id: Option<i64>,
    kind: &'a str,
    media_path: &'a str,
}

/// Per-language source bundle from phase 1 (ladder hit or shared ASR
/// cues): everything phase 2 needs to translate + commit one language
/// without touching shared state.
struct LangWork {
    lang: String,
    src_cues: Vec<Cue>,
    src_lang: String,
    needs_translate: bool,
    reg_source: String,
    reg_kind: Option<String>,
}

/// Episode-scoped context shared (by reference) across one episode's
/// concurrent language tasks: candidate, paths, duration. Keeps per-lang
/// fn signatures small; everything outlives the phase-2 join.
#[derive(Clone, Copy)]
struct EpisodeCtx<'a> {
    cand: &'a Candidate,
    kind: &'a str,
    media_path: &'a str,
    stem: &'a str,
    series_title: &'a str,
    duration_s: Option<f64>,
}

impl Pipeline {
    /// Process one episode/movie for all its missing languages.
    /// Returns the number of languages completed.
    ///
    /// Source choice is per target language (mirroring `choose_source` call
    /// sites in `run_pass`): an `en` target with an English audio track
    /// transcribes it directly with no translation step. ASR cues are shared
    /// across the episode's languages via a per-`asr_lang` cache, so `id`+`en`
    /// targets pay for one Japanese transcription.
    pub(crate) async fn process_one(
        &self,
        cand: &Candidate,
        titles: &std::collections::HashMap<i64, String>,
    ) -> Result<usize> {
        if self.paused.load(std::sync::atomic::Ordering::Relaxed) {
            return Ok(0);
        }
        let kind = if cand.is_movie { "movie" } else { "series" };
        // Resolve media path (+ series identity for the ladder/Jimaku).
        // `titles` is fetched once per pass by the caller, not per episode.
        let (media_path, series_title, season, ep_num) = if cand.is_movie {
            let path = cand.path.clone().context("movie without path")?;
            (self.cfg.map_path(&path), cand.series_title.clone(), None, 0)
        } else {
            let ep: Episode = self.sonarr.episode(cand.episode_id).await?;
            let cpath = ep
                .episode_file
                .as_ref()
                .and_then(|f| f.path.clone())
                .context("episode has no file")?;
            let title = ep
                .series_id
                .and_then(|sid| titles.get(&sid).cloned())
                .unwrap_or_else(|| cand.series_title.clone());
            (
                self.cfg.map_path(&cpath),
                title,
                ep.season_number,
                ep.episode_number.unwrap_or(0),
            )
        };
        if !Path::new(&media_path).is_file() {
            anyhow::bail!("media not on disk: {media_path}");
        }
        let stem = media_path
            .rsplit_once('.')
            .map(|(s, _)| s)
            .unwrap_or(&media_path)
            .to_string();
        // Single ffprobe for the episode: streams + duration together.
        let probe = asr::probe_media(&media_path).await?;
        let duration_s = probe.duration_s;
        let mapped: Vec<asr::AudioStream> = probe.streams;

        // Phase 1 (sequential): source per language — ladder fast-path or
        // remote ASR with a per-asr_lang cache, so id+en share one
        // transcription. Sequential keeps cache races out by construction.
        let mut asr_cache: std::collections::HashMap<String, Vec<Cue>> =
            std::collections::HashMap::new();
        let mut works = Vec::with_capacity(cand.missing.len());
        for lang in &cand.missing {
            if sidecar_exists(&stem, lang) {
                tracing::info!(episode = cand.episode_id, lang = %lang, "skip: sidecar already exists");
                continue;
            }
            // Ladder fast-path first (adequate ja/en sidecar or Jimaku
            // direct), falling back to remote ASR.
            let ladder = self
                .ladder_source(LadderQuery {
                    stem: &stem,
                    target: lang,
                    series_title: &series_title,
                    season,
                    episode: ep_num,
                    is_movie: cand.is_movie,
                    duration_s,
                })
                .await;
            // (source cues, source lang, needs-translate, registry source, registry kind)
            let (src_cues, src_lang, needs_translate, reg_source, reg_kind): (
                Vec<Cue>,
                String,
                bool,
                String,
                Option<String>,
            ) = match ladder {
                Some(hit) => {
                    let need = normalize_lang(&hit.src_lang) != normalize_lang(lang);
                    (
                        hit.cues,
                        hit.src_lang.clone(),
                        need,
                        hit.source,
                        hit.source_kind,
                    )
                }
                None => {
                    let choice = asr::choose_source(&mapped, lang).context("no audio streams")?;
                    let cues = match asr_cache.get(&choice.asr_lang) {
                        Some(cached) => cached.clone(),
                        None => {
                            let key = format!("ep{}_{}", cand.episode_id, choice.asr_lang);
                            let fresh = asr::transcribe_episode(
                                &self.pool,
                                &self.cfg.tmp_dir,
                                &media_path,
                                &choice,
                                &key,
                                self.cfg.asr_concurrency,
                                self.cfg.max_cue_ms,
                            )
                            .await?;
                            asr_cache.insert(choice.asr_lang.clone(), fresh.clone());
                            fresh
                        }
                    };
                    let src = choice.asr_lang.clone();
                    let need = choice.needs_translate;
                    (cues, src, need, "asr".to_string(), None)
                }
            };
            works.push(LangWork {
                lang: lang.clone(),
                src_cues,
                src_lang,
                needs_translate,
                reg_source,
                reg_kind,
            });
        }
        // Phase 2 (concurrent): translate + merge + upload + commit per
        // language. The pool is shared across episodes already, so sharing
        // across languages is equally sound; failures return Err like the
        // old sequential `?` (partial per-lang commits persist either way).
        let mut jobs = Vec::with_capacity(works.len());
        for w in works {
            // Reborrow per task: the async move owns `w` but only borrows
            // the episode locals (all outlive the join below).
            let ctx = EpisodeCtx {
                cand,
                kind,
                media_path: &media_path,
                stem: &stem,
                series_title: &series_title,
                duration_s,
            };
            jobs.push(async move {
                let translated: Vec<String> = if !w.needs_translate {
                    w.src_cues.iter().map(|c| c.text.clone()).collect()
                } else {
                    self.translate_lang(ctx.series_title, &w).await?
                };
                self.finish_lang(ctx, w, translated).await
            });
        }
        let mut completed = 0;
        for r in futures::future::join_all(jobs).await {
            completed += r?;
        }
        self.jellyfin
            .refresh_for(
                &media_path,
                &series_title,
                if cand.is_movie { "Movie" } else { "Episode" },
            )
            .await;
        Ok(completed)
    }

    /// Translate one language's source cues (knowledge block included).
    async fn translate_lang(&self, series_title: &str, w: &LangWork) -> Result<Vec<String>> {
        let knowledge = if series_title.is_empty() || series_title == "?" {
            String::new()
        } else {
            let texts: Vec<String> = w.src_cues.iter().map(|c| c.text.clone()).collect();
            self.glossary
                .knowledge_block_for_cues(series_title, &texts, crate::glossary::MAX_REFS)
        };
        crate::translate::translate_lines(
            &self.pool,
            crate::translate::TranslateJob {
                lines: w.src_cues.iter().map(|c| c.text.clone()).collect(),
                target_lang: &w.lang,
                source_lang: crate::translate::display_source_lang(&w.src_lang),
                knowledge: &knowledge,
                chunk_size: self.cfg.translate_chunk,
                fanout: self.cfg.translate_concurrency,
                skip_guard: normalize_lang(&w.src_lang) == "en",
                placeholders: &self.cfg.sdh_placeholders,
            },
        )
        .await
    }

    /// Merge, gate, install, upload, and commit one translated language.
    /// Returns 1 on completion (the caller's completion count).
    async fn finish_lang(
        &self,
        ctx: EpisodeCtx<'_>,
        w: LangWork,
        translated: Vec<String>,
    ) -> Result<usize> {
        let lang = &w.lang;
        let cand = ctx.cand;
        // Assemble cues: translated text keeps SOURCE timing.
        let out_cues: Vec<Cue> = w
            .src_cues
            .iter()
            .zip(translated.iter())
            .map(|(c, t)| Cue::new(c.start_ms, c.end_ms, t.clone()))
            .collect();
        let mut merged = srt::cps_merge(
            out_cues,
            self.cfg.cps_merge_max,
            self.cfg.cps_merge_max_chars,
            self.cfg.cps_merge_max_dur_ms,
            self.cfg.cps_merge_max_gap_ms,
        );
        srt::ensure_contiguous(&mut merged);
        let violations = srt::validate_timeline(&merged, self.cfg.max_cue_ms, ctx.duration_s);
        if !violations.is_empty() {
            tracing::warn!(episode = cand.episode_id, lang = %lang, n = violations.len(), "timeline violations after merge");
        }
        let srt_text = srt::write_srt(&merged, self.cfg.ai_marker_cue, self.cfg.ai_marker_cue_ms);
        self.install_and_upload(cand, ctx.media_path, ctx.stem, lang, srt_text.into_bytes())
            .await?;
        // Registry + state commit.
        self.commit_registry(RegistryCommit {
            stem: ctx.stem,
            lang,
            source: &w.reg_source,
            source_kind: w.reg_kind.as_deref(),
            episode_id: Some(cand.episode_id),
            kind: ctx.kind,
            media_path: ctx.media_path,
        })
        .await;
        self.append_state(cand.episode_id, Some(lang.as_str()), "done", "", ctx.kind)
            .await;
        Ok(1)
    }

    async fn install_and_upload(
        &self,
        cand: &Candidate,
        media_path: &str,
        stem: &str,
        lang: &str,
        srt_bytes: Vec<u8>,
    ) -> Result<()> {
        let target = crate::lang::canonical_target_sidecar(stem, lang);
        // Atomic local install first (Bazarr async job may crash and never
        // land the file; the local copy is authoritative for the registry).
        if let Some(parent) = Path::new(&target).parent() {
            if !parent.as_os_str().is_empty() {
                tokio::fs::create_dir_all(parent).await?;
            }
        }
        let tmp = format!("{target}.direct.tmp");
        tokio::fs::write(&tmp, &srt_bytes).await?;
        // No-clobber: keep a concurrently landed valid sidecar.
        if Path::new(&target).exists() {
            if let Ok(existing) = tokio::fs::read(&target).await {
                if !existing.is_empty()
                    && srt::parse_srt(&String::from_utf8_lossy(&existing)).len() >= 10
                {
                    let _ = tokio::fs::remove_file(&tmp).await;
                    return Ok(());
                }
            }
            tokio::fs::rename(&tmp, &target).await?;
        } else {
            tokio::fs::rename(&tmp, &target).await?;
        }
        // Bazarr upload (204 expected). Retries (3x, 5s/10s backoff) run
        // OUTSIDE the upload permit and ONLY for retryable outcomes
        // (transport error, 429, 5xx): 400/401/404 are permanent and break
        // immediately instead of burning two more attempts + sleeps.
        // Failure to reach Bazarr does NOT fail the episode — the sidecar is
        // already authoritative on disk.
        let mut code = None;
        for attempt in 0..3 {
            {
                let _up = self
                    .upload_sem
                    .clone()
                    .acquire_owned()
                    .await
                    .expect("semaphore closed");
                code = if cand.is_movie {
                    self.bazarr
                        .upload_movie(cand.episode_id, lang, srt_bytes.clone())
                        .await
                        .ok()
                        .flatten()
                } else {
                    let series_id = cand.series_id.unwrap_or(0);
                    self.bazarr
                        .upload_episode(series_id, cand.episode_id, lang, srt_bytes.clone())
                        .await
                        .ok()
                        .flatten()
                };
            }
            if code == Some(204) {
                break;
            }
            let retryable = matches!(code, None | Some(429) | Some(500..=599));
            if !retryable {
                tracing::warn!(episode = cand.episode_id, lang = %lang, upload = ?code, "bazarr upload permanent failure, not retrying");
                break;
            }
            tracing::warn!(episode = cand.episode_id, lang = %lang, upload = ?code, attempt, "bazarr upload retrying");
            if attempt < 2 {
                tokio::time::sleep(std::time::Duration::from_secs(5 * (attempt as u64 + 1))).await;
            }
        }
        tracing::info!(episode = cand.episode_id, lang = %lang, upload = ?code, target = %target, "subtitle committed");
        let _ = media_path;
        Ok(())
    }

    /// Provenance commit. ASR rows carry `source = "asr"` with NO
    /// `source_kind` (matching `registry_upsert(stem, lang, "asr", ...)` in
    /// orchestrator.py); ladder rows carry `jpn`/`eng` + `external`.
    /// Paths are recorded for human debugging; nothing verifies hashes, so
    /// none are stored (and no file re-read happens here).
    async fn commit_registry(&self, c: RegistryCommit<'_>) {
        let stem = c.stem;
        let lang = c.lang;
        let source = c.source;
        let source_kind = c.source_kind;
        let episode_id = c.episode_id;
        let kind = c.kind;
        let media_path = c.media_path;
        let target = crate::lang::canonical_target_sidecar(stem, lang);
        let mut extra = std::collections::HashMap::new();
        extra.insert(
            "media_path".to_string(),
            serde_json::Value::String(media_path.to_string()),
        );
        if kind == "movie" {
            extra.insert(
                "kind".to_string(),
                serde_json::Value::String("movie".to_string()),
            );
        }
        let row = RegistryRow {
            stem: Some(stem.to_string()),
            lang: Some(normalize_lang(lang)),
            episode_id,
            source: Some(source.to_string()),
            source_kind: source_kind.map(str::to_string),
            source_path: Some(target.clone()),
            target_path: Some(target),
            ts: Some(state::utc_now_iso()),
            extra,
        };
        let _ = state::append_jsonl(&self.cfg.registry_file, &row);
    }

    /// State commit. Movies carry `"kind": "movie"` (series rows omit it,
    /// defaulting to `series` on read) so movie/series numeric ids never
    /// collide in `sonarrEpisodeId` keys.
    pub(crate) async fn append_state(
        &self,
        episode_id: i64,
        lang: Option<&str>,
        status: &str,
        detail: &str,
        kind: &str,
    ) {
        let mut e = serde_json::json!({
            "sonarrEpisodeId": episode_id,
            "language": lang.map(normalize_lang),
            "status": status,
            "detail": detail,
            "ts": state::utc_now_iso(),
        });
        if kind == "movie" {
            e["kind"] = serde_json::Value::String("movie".to_string());
        }
        let entry: StateEntry = serde_json::from_value(e).unwrap_or(StateEntry {
            episode_id: Some(episode_id),
            language: lang.map(normalize_lang),
            status: Some(status.to_string()),
            kind: if kind == "movie" {
                Some("movie".to_string())
            } else {
                None
            },
            ts: Some(state::utc_now_iso()),
            extra: Default::default(),
        });
        let _ = state::append_jsonl(&self.cfg.state_file, &entry);
    }
}

/// True when a replaceable (non-forced) target sidecar already exists.
pub(crate) fn sidecar_exists(stem: &str, lang: &str) -> bool {
    crate::lang::replaceable_target_sidecar_paths(stem, lang)
        .iter()
        .any(|p| std::path::PathBuf::from(p).is_file())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sidecar_exists_covers_alias_variants() {
        // Adoption ladder (row 5): a legacy `jpn` sidecar satisfies a `ja`
        // target without reprocessing — and vice versa.
        let dir = tempfile::tempdir().unwrap();
        let stem = dir.path().join("ep").to_string_lossy().to_string();
        assert!(!sidecar_exists(&stem, "ja"));
        std::fs::write(format!("{stem}.jpn.srt"), "x").unwrap();
        assert!(sidecar_exists(&stem, "ja"));
        assert!(sidecar_exists(&stem, "jpn"));
    }
}
