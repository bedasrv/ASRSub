use std::path::Path;

use anyhow::Result;

use crate::feature_modules::discord_types::{TargetRunResult, TargetStatus, WarningClass};
use crate::feature_modules::pipeline_commit::LedgerPaths;
use crate::pipeline::{Candidate, Pipeline};
use crate::srt::{self, Cue};

use super::episode_commit::{
    cleanup_failed_install, install_sidecar, publish_target_ledgers, target_ledger_rows,
    target_result, InstalledSidecar, RegistryCommit, TargetFailure,
};

/// Per-language source bundle from phase 1 (ladder hit or shared ASR
/// cues): everything phase 2 needs to translate + commit one language
/// without touching shared state.
pub(super) struct LangWork {
    pub(super) lang: String,
    pub(super) src_cues: Vec<Cue>,
    pub(super) src_lang: String,
    pub(super) needs_translate: bool,
    pub(super) reg_source: String,
    pub(super) reg_kind: Option<String>,
    /// Chosen audio stream index (ASR rows only; ladder rows are None).
    pub(super) src_stream: Option<u32>,
}

/// Episode-scoped context shared (by reference) across one episode's
/// concurrent language tasks: candidate, paths, duration. Keeps per-lang
/// fn signatures small; everything outlives the phase-2 join.
#[derive(Clone, Copy)]
pub(super) struct EpisodeCtx<'a> {
    pub(super) cand: &'a Candidate,
    pub(super) kind: &'a str,
    pub(super) media_path: &'a str,
    pub(super) stem: &'a str,
    pub(super) series_title: &'a str,
    pub(super) duration_s: Option<f64>,
}

impl Pipeline {
    /// Translate one language's source cues (knowledge block included).
    pub(super) async fn translate_lang(
        &self,
        series_title: &str,
        w: &LangWork,
    ) -> Result<Vec<String>> {
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
                // The foreign-script guard exists for Japanese sources only
                // (ASR echoing OP/ED lyrics in English/Chinese). Any other
                // source — French, German, English, even Chinese — would be
                // rewritten to SDH placeholders, emptying the episode.
                skip_guard: !crate::lang::needs_foreign_guard(&w.src_lang),
                placeholders: &self.cfg.sdh_placeholders,
            },
        )
        .await
    }

    /// Merge, gate, install, upload, and commit one translated language.
    /// The target is admitted only after the strict paired-ledger commit
    /// returns its witness.
    pub(super) async fn finish_lang(
        &self,
        ctx: EpisodeCtx<'_>,
        w: LangWork,
        translated: Vec<String>,
    ) -> std::result::Result<TargetRunResult, TargetFailure> {
        let lang = &w.lang;
        let cand = ctx.cand;
        // Assemble cues: translated text keeps SOURCE timing.
        let out_cues: Vec<Cue> = srt::retime(&w.src_cues, &translated);
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
        let srt_bytes =
            srt::write_srt(&merged, self.cfg.ai_marker_cue, self.cfg.ai_marker_cue_ms).into_bytes();
        let (warning, installed) = self
            .install_and_upload(cand, ctx.media_path, ctx.stem, lang, srt_bytes.clone())
            .await
            .map_err(TargetFailure::storage_error)?;
        let artifact_sha256 = installed.artifact_sha256;
        if !installed
            .still_current()
            .map_err(TargetFailure::storage_error)?
        {
            if let Err(cleanup) = cleanup_failed_install(&self.cfg, cand, lang, &installed) {
                tracing::warn!(
                    episode = cand.episode_id,
                    lang = %lang,
                    error = %crate::config::mask_for_log(&cleanup.to_string()),
                    "failed to clean up replaced sidecar"
                );
            }
            return Err(TargetFailure::storage_error(anyhow::anyhow!(
                "target sidecar changed before ledger commit"
            )));
        }
        let registry_commit = RegistryCommit {
            stem: ctx.stem,
            lang,
            source: &w.reg_source,
            source_kind: w.reg_kind.as_deref(),
            episode_id: Some(cand.episode_id),
            kind: ctx.kind,
            media_path: ctx.media_path,
            source_lang: &w.src_lang,
            source_stream: w.src_stream,
        };
        let (identity, registry_row, state_row) =
            match target_ledger_rows(&registry_commit, artifact_sha256) {
                Ok(rows) => rows,
                Err(error) => {
                    if let Err(cleanup) = cleanup_failed_install(&self.cfg, cand, lang, &installed)
                    {
                        tracing::warn!(
                            episode = cand.episode_id,
                            lang = %lang,
                            error = %crate::config::mask_for_log(&cleanup.to_string()),
                            "failed to clean up unadmitted sidecar"
                        );
                    }
                    return Err(TargetFailure::storage_error(error));
                }
            };
        let _witness = match publish_target_ledgers(
            LedgerPaths {
                registry: self.cfg.registry_file.clone(),
                state: self.cfg.state_file.clone(),
            },
            identity,
            registry_row,
            state_row,
        ) {
            Ok(witness) => witness,
            Err(error) => {
                if let Err(cleanup) = cleanup_failed_install(&self.cfg, cand, lang, &installed) {
                    tracing::warn!(
                        episode = cand.episode_id,
                        lang = %lang,
                        error = %crate::config::mask_for_log(&cleanup.to_string()),
                        "failed to clean up unadmitted sidecar"
                    );
                }
                return Err(error);
            }
        };
        target_result(
            lang,
            TargetStatus::Completed { warning },
            Some(artifact_sha256),
        )
        .map_err(TargetFailure::storage_error)
    }

    async fn install_and_upload(
        &self,
        cand: &Candidate,
        media_path: &str,
        stem: &str,
        lang: &str,
        srt_bytes: Vec<u8>,
    ) -> Result<(Option<WarningClass>, InstalledSidecar)> {
        let target = crate::lang::canonical_target_sidecar(stem, lang);
        // Install our generated bytes unconditionally for an unverified target.
        // A parseable orphan is not a concurrent success; process_one skips
        // only targets admitted by a verified paired ledger.
        let installed = install_sidecar(Path::new(&target), &srt_bytes)?;
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
        Ok((
            (code != Some(204)).then_some(WarningClass::Upload),
            installed,
        ))
    }
}
