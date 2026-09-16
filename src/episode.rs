//! Per-episode processing: source choice → translate → commit.
//!
//! For each missing language: ladder text first ([`crate::ladder`]), else
//! remote ASR (transcriptions shared across the episode's languages via a
//! per-`asr_lang` cache, so `id`+`en` pay for one Japanese transcription),
//! then remote translation keeping source timing, CPS merge, atomic install,
//! Bazarr upload, and registry/state commits.

use std::path::Path;

use anyhow::{Context, Result};
use sha2::{Digest, Sha256};

use crate::asr;
use crate::feature_modules::discord_text::SafeDisplayText;
use crate::feature_modules::discord_types::{
    EpisodeKind, EpisodeRunResult, FailureClass, NoTargetCase, TargetLanguage, TargetRunResult,
    TargetStatus, WarningClass,
};
use crate::feature_modules::pipeline_commit::{
    commit_target_ledgers, CommitLedgerError, CommitWitness, LedgerCommitRequest, LedgerIdentity,
    LedgerPaths,
};
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
    /// Effective source language (ASR tag/detected, or ladder source) and
    /// the chosen audio stream for ASR rows — the provenance that makes an
    /// `fr`-sourced row distinguishable from a `ja`-sourced one.
    source_lang: &'a str,
    source_stream: Option<u32>,
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
    /// Chosen audio stream index (ASR rows only; ladder rows are None).
    src_stream: Option<u32>,
}

#[derive(Debug)]
struct TargetFailure {
    class: FailureClass,
    error: anyhow::Error,
}

impl TargetFailure {
    fn new(class: FailureClass, error: anyhow::Error) -> Self {
        Self { class, error }
    }

    fn translation(error: anyhow::Error) -> Self {
        Self::new(FailureClass::Translation, error)
    }

    fn storage_error(error: anyhow::Error) -> Self {
        Self::new(FailureClass::Storage, error)
    }

    fn storage(error: CommitLedgerError) -> Self {
        Self::storage_error(anyhow::anyhow!("target ledger commit failed: {error:?}"))
    }
}

fn publish_target_ledgers(
    paths: LedgerPaths,
    identity: LedgerIdentity,
    registry_row: serde_json::Value,
    state_row: serde_json::Value,
) -> std::result::Result<CommitWitness, TargetFailure> {
    commit_target_ledgers(LedgerCommitRequest::TargetLedgerCommit {
        paths,
        identity,
        registry_row,
        state_row,
    })
    .map_err(TargetFailure::storage)
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

fn target_result(
    lang: &str,
    status: TargetStatus,
    artifact_sha256: Option<[u8; 32]>,
) -> Result<TargetRunResult> {
    let language = TargetLanguage::parse(lang)
        .map_err(|error| anyhow::anyhow!("invalid target language {lang:?}: {error:?}"))?;
    TargetRunResult::try_new(language, status, artifact_sha256)
        .map_err(|error| anyhow::anyhow!("invalid target result for {lang:?}: {error:?}"))
}

fn validated_report_title(raw: &str) -> Result<SafeDisplayText> {
    SafeDisplayText::sanitize(raw)
        .map_err(|error| anyhow::anyhow!("invalid report title: {error:?}"))
}

fn digest_bytes(bytes: &[u8]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hasher.finalize().into()
}

#[derive(Debug)]
struct InstalledSidecar {
    target: std::path::PathBuf,
    previous: Option<Vec<u8>>,
    artifact_sha256: [u8; 32],
}

fn install_sidecar(target: &Path, bytes: &[u8]) -> Result<InstalledSidecar> {
    let previous = match std::fs::read(target) {
        Ok(bytes) => Some(bytes),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => {
            return Err(error).with_context(|| format!("read existing sidecar {target:?}"))
        }
    };
    if let Some(parent) = target.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }
    let tmp = Path::new(&format!("{}.direct.tmp", target.display())).to_path_buf();
    std::fs::write(&tmp, bytes)?;
    if let Err(error) = std::fs::rename(&tmp, target) {
        let _ = std::fs::remove_file(&tmp);
        return Err(error.into());
    }
    Ok(InstalledSidecar {
        target: target.to_path_buf(),
        previous,
        artifact_sha256: digest_bytes(bytes),
    })
}

impl InstalledSidecar {
    fn still_current(&self) -> Result<bool> {
        match std::fs::read(&self.target) {
            Ok(bytes) => Ok(digest_bytes(&bytes) == self.artifact_sha256),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(error) => Err(error.into()),
        }
    }

    /// Remove or restore only while the target still contains this install.
    /// A concurrent replacement is left untouched.
    fn rollback_if_unchanged(&self) -> Result<()> {
        let current = match std::fs::read(&self.target) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(error.into()),
        };
        if digest_bytes(&current) != self.artifact_sha256 {
            return Ok(());
        }
        if let Some(previous) = &self.previous {
            let tmp = Path::new(&format!("{}.rollback.tmp", self.target.display())).to_path_buf();
            std::fs::write(&tmp, previous)?;
            let still_current = match std::fs::read(&self.target) {
                Ok(bytes) => digest_bytes(&bytes) == self.artifact_sha256,
                Err(_) => false,
            };
            if !still_current {
                let _ = std::fs::remove_file(&tmp);
                return Ok(());
            }
            if let Err(error) = std::fs::rename(&tmp, &self.target) {
                let _ = std::fs::remove_file(&tmp);
                return Err(error.into());
            }
        } else {
            let still_current = match std::fs::read(&self.target) {
                Ok(bytes) => digest_bytes(&bytes) == self.artifact_sha256,
                Err(_) => false,
            };
            if still_current {
                match std::fs::remove_file(&self.target) {
                    Ok(()) => {}
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                    Err(error) => return Err(error.into()),
                }
            }
        }
        Ok(())
    }
}

fn existing_target_digest(target_path: &Path) -> Result<[u8; 32]> {
    let bytes = std::fs::read(target_path)
        .with_context(|| format!("read target sidecar {}", target_path.display()))?;
    Ok(digest_bytes(&bytes))
}

fn target_is_verified(
    cfg: &crate::config::Config,
    candidate: &Candidate,
    lang: &str,
) -> Option<crate::pipeline::VerifiedTarget> {
    let kind = if candidate.is_movie {
        "movie"
    } else {
        "series"
    };
    let key = (kind.to_string(), candidate.episode_id, normalize_lang(lang));
    crate::pipeline::verified_targets(
        &state::load_jsonl::<RegistryRow>(&cfg.registry_file),
        &state::load_jsonl::<StateEntry>(&cfg.state_file),
    )
    .remove(&key)
}

fn target_ledger_rows(
    commit: &RegistryCommit<'_>,
    artifact_sha256: [u8; 32],
) -> Result<(LedgerIdentity, serde_json::Value, serde_json::Value)> {
    let episode_id = commit
        .episode_id
        .context("target ledger missing episode id")?;
    let language = normalize_lang(commit.lang);
    let digest = crate::feature_modules::discord_state_codec::hex(&artifact_sha256);
    let target = crate::lang::canonical_target_sidecar(commit.stem, commit.lang);
    let timestamp = state::utc_now_iso();

    let mut registry_extra = std::collections::HashMap::new();
    registry_extra.insert(
        "media_path".to_string(),
        serde_json::Value::String(commit.media_path.to_string()),
    );
    registry_extra.insert(
        "source_lang".to_string(),
        serde_json::Value::String(normalize_lang(commit.source_lang)),
    );
    registry_extra.insert(
        "artifact_sha256".to_string(),
        serde_json::Value::String(digest.clone()),
    );
    if let Some(stream) = commit.source_stream {
        registry_extra.insert("source_stream".to_string(), serde_json::Value::from(stream));
    }
    if commit.kind == "movie" {
        registry_extra.insert(
            "kind".to_string(),
            serde_json::Value::String("movie".to_string()),
        );
    }
    let registry_row = RegistryRow {
        stem: Some(commit.stem.to_string()),
        lang: Some(language.clone()),
        episode_id: Some(episode_id),
        source: Some(commit.source.to_string()),
        source_kind: commit.source_kind.map(str::to_string),
        source_path: Some(target.clone()),
        target_path: Some(target),
        ts: Some(timestamp.clone()),
        extra: registry_extra,
    };

    let mut state_extra = std::collections::HashMap::new();
    state_extra.insert(
        "artifact_sha256".to_string(),
        serde_json::Value::String(digest),
    );
    state_extra.insert(
        "detail".to_string(),
        serde_json::Value::String(String::new()),
    );
    let state_row = StateEntry {
        episode_id: Some(episode_id),
        language: Some(language.clone()),
        status: Some("done".to_string()),
        kind: (commit.kind == "movie").then(|| "movie".to_string()),
        ts: Some(timestamp),
        extra: state_extra,
    };
    let identity = LedgerIdentity {
        kind: commit.kind.to_string(),
        episode_id,
        language,
        artifact_sha256,
    };
    Ok((
        identity,
        serde_json::to_value(registry_row)?,
        serde_json::to_value(state_row)?,
    ))
}

fn cleanup_failed_install(
    cfg: &crate::config::Config,
    candidate: &Candidate,
    lang: &str,
    installed: &InstalledSidecar,
) -> Result<()> {
    if let Some(verified) = target_is_verified(cfg, candidate, lang) {
        if verified.artifact_sha256 == installed.artifact_sha256 {
            return Ok(());
        }
    }
    installed.rollback_if_unchanged()
}

impl Pipeline {
    /// Process one episode/movie for all its missing languages.
    ///
    /// Each target produces one typed result. A target failure therefore does
    /// not cancel or hide sibling target completions from the same episode.
    pub(crate) async fn process_one(
        &self,
        cand: &Candidate,
        series_titles: &std::collections::HashMap<i64, crate::sonarr::SeriesInfo>,
    ) -> Result<EpisodeRunResult> {
        if self.paused.load(std::sync::atomic::Ordering::Relaxed) {
            return Ok(EpisodeRunResult::NoTarget(NoTargetCase::Paused));
        }
        if cand.missing.is_empty() {
            return Ok(EpisodeRunResult::NoTarget(NoTargetCase::NoMissingTargets));
        }
        let kind = if cand.is_movie { "movie" } else { "series" };
        // Resolve media path (+ series identity for the ladder/Jimaku).
        // `series_titles` is fetched once per pass by the caller, not per
        // episode, and carries the original language used for source choice.
        let (media_path, series_title, original_lang, season, ep_num) = if cand.is_movie {
            let path = cand.path.clone().context("movie without path")?;
            (
                self.cfg.map_path(&path),
                cand.series_title.clone(),
                cand.original_lang.clone(),
                None,
                0,
            )
        } else {
            let ep: Episode = self.sonarr.episode(cand.episode_id).await?;
            let cpath = ep
                .episode_file
                .as_ref()
                .and_then(|f| f.path.clone())
                .context("episode has no file")?;
            let info = ep.series_id.and_then(|sid| series_titles.get(&sid));
            let title = info
                .map(|i| i.title.clone())
                .filter(|t| !t.is_empty())
                .unwrap_or_else(|| cand.series_title.clone());
            let original = info.and_then(|i| i.original_language.clone());
            (
                self.cfg.map_path(&cpath),
                title,
                original,
                ep.season_number,
                ep.episode_number.unwrap_or(0),
            )
        };
        // Validate the report title before ffprobe, ladder, installation, or
        // ledger work so a bad display value cannot arrive after real outcomes.
        let report_title = validated_report_title(&series_title)?;
        if !Path::new(&media_path).is_file() {
            anyhow::bail!("media not on disk: {media_path}");
        }
        let stem = crate::lang::stem_of(&media_path).to_string();
        // Single ffprobe for the episode: streams + duration together.
        let probe = asr::probe_media_with_tools(&self.tools, &media_path).await?;
        let duration_s = probe.duration_s;
        let mapped: Vec<asr::AudioStream> = probe.streams;

        // Phase 1 (sequential): source per language — ladder fast-path or
        // remote ASR with a per-choice cache (the track's tag, or the chosen
        // stream when its tag is unknown), so id+en share one transcription.
        // Sequential keeps cache races out by construction.
        let mut asr_cache: std::collections::HashMap<String, asr::Transcript> =
            std::collections::HashMap::new();
        let mut target_outcomes = Vec::with_capacity(cand.missing.len());
        let mut works = Vec::with_capacity(cand.missing.len());
        for lang in &cand.missing {
            if let Some(verified) = target_is_verified(&self.cfg, cand, lang) {
                tracing::info!(episode = cand.episode_id, lang = %lang, "skip: sidecar already exists");
                match existing_target_digest(&verified.target_path) {
                    Ok(digest) if digest == verified.artifact_sha256 => {
                        target_outcomes.push(target_result(
                            lang,
                            TargetStatus::Completed { warning: None },
                            Some(digest),
                        )?)
                    }
                    Ok(_) => {
                        target_outcomes.push(target_result(
                            lang,
                            TargetStatus::Failed {
                                class: FailureClass::Storage,
                            },
                            None,
                        )?);
                    }
                    Err(error) => {
                        tracing::warn!(
                            episode = cand.episode_id,
                            lang = %lang,
                            error = %crate::config::mask_for_log(&error.to_string()),
                            "existing target could not be read"
                        );
                        target_outcomes.push(target_result(
                            lang,
                            TargetStatus::Failed {
                                class: FailureClass::Storage,
                            },
                            None,
                        )?);
                    }
                }
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
            // (source cues, source lang, needs-translate, registry source, registry kind, stream)
            let (src_cues, src_lang, needs_translate, reg_source, reg_kind, src_stream): (
                Vec<Cue>,
                String,
                bool,
                String,
                Option<String>,
                Option<u32>,
            ) = match ladder {
                Some(hit) => {
                    let need = normalize_lang(&hit.src_lang) != normalize_lang(lang);
                    (
                        hit.cues,
                        hit.src_lang.clone(),
                        need,
                        hit.source,
                        hit.source_kind,
                        None,
                    )
                }
                None => {
                    let Some(choice) = asr::choose_source(&mapped, lang, original_lang.as_deref())
                    else {
                        let error = anyhow::anyhow!("no audio streams");
                        tracing::warn!(
                            episode = cand.episode_id,
                            lang = %lang,
                            error = %error,
                            "source selection failed"
                        );
                        target_outcomes.push(target_result(
                            lang,
                            TargetStatus::Failed {
                                class: FailureClass::Source,
                            },
                            None,
                        )?);
                        continue;
                    };
                    // Cache key is stable before the language is known (the
                    // tag, or the chosen stream when it must be detected),
                    // so two targets sharing one track share one transcription.
                    let cache_key = choice.cache_key();
                    let transcript = match asr_cache.get(&cache_key) {
                        Some(cached) => cached.clone(),
                        None => {
                            let key = format!("ep{}_{}", cand.episode_id, cache_key);
                            let fresh = match asr::transcribe_episode(
                                &self.pool,
                                asr::TranscribeJob {
                                    tmp_dir: &self.cfg.tmp_dir,
                                    tools: &self.tools,
                                    media_path: &media_path,
                                    choice: &choice,
                                    episode_key: &key,
                                    duration_s: probe.duration_s,
                                    audio_bytes: asr::est_audio_bytes(
                                        probe.duration_s,
                                        probe.bit_rate,
                                    ),
                                    fanout: self.cfg.asr_concurrency,
                                    max_cue_ms: self.cfg.max_cue_ms,
                                },
                            )
                            .await
                            {
                                Ok(transcript) => transcript,
                                Err(error) => {
                                    tracing::warn!(
                                        episode = cand.episode_id,
                                        lang = %lang,
                                        error = %crate::config::mask_for_log(&error.to_string()),
                                        "transcription failed"
                                    );
                                    target_outcomes.push(target_result(
                                        lang,
                                        TargetStatus::Failed {
                                            class: FailureClass::Transcription,
                                        },
                                        None,
                                    )?);
                                    continue;
                                }
                            };
                            asr_cache.insert(cache_key, fresh.clone());
                            fresh
                        }
                    };
                    // The effective language is the code actually sent as a
                    // pin, or the detected code — never a fabricated one. It
                    // decides both the translation step and the provenance
                    // row: the comparison uses the effective source, so a tag
                    // the wire gate dropped (which took detection) is judged
                    // by what was really transcribed, not by the tag's
                    // provisional answer.
                    let src = transcript.lang.clone();
                    let need = if choice.wire_pin().is_some() {
                        choice.needs_translate
                    } else {
                        normalize_lang(&src) != normalize_lang(lang)
                    };
                    (
                        transcript.cues,
                        src,
                        need,
                        "asr".to_string(),
                        None,
                        Some(choice.stream_index),
                    )
                }
            };
            tracing::info!(
                episode = cand.episode_id,
                lang = %lang,
                source_lang = %src_lang,
                stream = ?src_stream,
                needs_translate,
                "source chosen"
            );
            works.push(LangWork {
                lang: lang.clone(),
                src_cues,
                src_lang,
                needs_translate,
                reg_source,
                reg_kind,
                src_stream,
            });
        }

        if works.is_empty()
            && target_outcomes.len() == cand.missing.len()
            && target_outcomes
                .iter()
                .all(|target| matches!(target.status(), TargetStatus::Completed { .. }))
        {
            return Ok(EpisodeRunResult::NoTarget(
                NoTargetCase::AllTargetsAlreadyPresent,
            ));
        }

        // Phase 2 (concurrent): translate + merge + upload + commit per
        // language. Every future is reduced after join_all, so one failure
        // cannot discard a sibling completion.
        let mut jobs = Vec::with_capacity(works.len());
        for w in works {
            let lang = w.lang.clone();
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
                let result = if !w.needs_translate {
                    let translated: Vec<String> =
                        w.src_cues.iter().map(|cue| cue.text.clone()).collect();
                    self.finish_lang(ctx, w, translated).await
                } else {
                    match self.translate_lang(ctx.series_title, &w).await {
                        Ok(translated) => self.finish_lang(ctx, w, translated).await,
                        Err(error) => Err(TargetFailure::translation(error)),
                    }
                };
                (lang, result)
            });
        }
        for (lang, result) in futures::future::join_all(jobs).await {
            match result {
                Ok(target) => target_outcomes.push(target),
                Err(failure) => {
                    tracing::warn!(
                        episode = cand.episode_id,
                        lang = %lang,
                        error = %crate::config::mask_for_log(&failure.error.to_string()),
                        "target failed"
                    );
                    target_outcomes.push(target_result(
                        &lang,
                        TargetStatus::Failed {
                            class: failure.class,
                        },
                        None,
                    )?);
                }
            }
        }
        self.jellyfin
            .refresh_for(
                &media_path,
                &series_title,
                if cand.is_movie { "Movie" } else { "Episode" },
            )
            .await;

        let report = crate::pipeline::reduce_target_outcomes(
            if cand.is_movie {
                EpisodeKind::Movie
            } else {
                EpisodeKind::Series
            },
            cand.episode_id,
            report_title,
            season.and_then(|value| u32::try_from(value).ok()),
            (!cand.is_movie && ep_num > 0)
                .then(|| u32::try_from(ep_num).ok())
                .flatten(),
            target_outcomes,
            None,
        )
        .map_err(|error| anyhow::anyhow!("invalid episode outcome: {error:?}"))?;
        Ok(EpisodeRunResult::Report(report))
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
    async fn finish_lang(
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
    fn paired_ledger_failure_propagates_without_success() {
        use crate::feature_modules::discord_types::FailureClass;
        use crate::feature_modules::pipeline_commit::{LedgerIdentity, LedgerPaths};

        let dir = tempfile::tempdir().unwrap();
        let state_path = dir.path().join("state");
        std::fs::create_dir(&state_path).unwrap();
        let row = serde_json::json!({
            "kind": "series",
            "episode_id": 7,
            "language": "id",
            "artifact_sha256": "0101010101010101010101010101010101010101010101010101010101010101"
        });
        let target = dir.path().join("ep.id.hi.srt");
        let installed = install_sidecar(&target, b"unadmitted").unwrap();
        let error = publish_target_ledgers(
            LedgerPaths {
                registry: dir.path().join("registry"),
                state: state_path,
            },
            LedgerIdentity {
                kind: "series".to_string(),
                episode_id: 7,
                language: "id".to_string(),
                artifact_sha256: [1; 32],
            },
            row.clone(),
            row,
        )
        .unwrap_err();

        assert_eq!(error.class, FailureClass::Storage);
        installed.rollback_if_unchanged().unwrap();
        assert!(!target.exists());
    }

    #[test]
    fn sidecar_exists_covers_alias_variants() {
        // Adoption ladder: an existing alias sidecar (for example, `jpn`
        // for a `ja` target) satisfies the target without reprocessing.
        let dir = tempfile::tempdir().unwrap();
        let stem = dir.path().join("ep").to_string_lossy().to_string();
        assert!(!sidecar_exists(&stem, "ja"));
        std::fs::write(format!("{stem}.jpn.srt"), "x").unwrap();
        assert!(sidecar_exists(&stem, "ja"));
        assert!(sidecar_exists(&stem, "jpn"));
    }

    #[test]
    fn invalid_report_title_is_rejected_before_pipeline_stages() {
        let error = validated_report_title(
            &"x".repeat(crate::feature_modules::discord_text::MAX_SAFE_TITLE_SCALARS + 1),
        );
        assert!(error.is_err());
    }

    #[test]
    fn target_ledger_rows_share_one_utc_timestamp() {
        let commit = RegistryCommit {
            stem: "/media/ep",
            lang: "id",
            source: "asr",
            source_kind: None,
            episode_id: Some(7),
            kind: "series",
            media_path: "/media/ep.mkv",
            source_lang: "ja",
            source_stream: Some(1),
        };
        let (_, registry, state) = target_ledger_rows(&commit, [9; 32]).unwrap();
        let registry: RegistryRow = serde_json::from_value(registry).unwrap();
        let state: StateEntry = serde_json::from_value(state).unwrap();
        assert!(registry.ts.is_some());
        assert!(registry
            .ts
            .as_deref()
            .is_some_and(|timestamp| timestamp.ends_with('Z')));
        assert_eq!(registry.ts, state.ts);
    }

    #[test]
    fn report_maps_failure_classes() {
        use crate::feature_modules::discord_text::SafeDisplayText;
        use crate::feature_modules::discord_types::*;
        let target = TargetRunResult::try_new(
            TargetLanguage::parse("id").unwrap(),
            TargetStatus::Failed {
                class: FailureClass::Storage,
            },
            None,
        )
        .unwrap();
        let report = EpisodeRunReport::try_new(
            EpisodeKind::Movie,
            100,
            SafeDisplayText::sanitize("movie").unwrap(),
            None,
            None,
            BoundedTargets::try_from([target]).unwrap(),
            None,
            AggregateDisposition::Failed,
        )
        .unwrap();
        assert_eq!(report.kind(), EpisodeKind::Movie);
        assert_eq!(report.aggregate(), AggregateDisposition::Failed);
    }

    #[test]
    fn unverified_parseable_orphan_is_replaced_by_generated_artifact() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("ep.id.hi.srt");
        let mut orphan = String::new();
        for index in 0..10 {
            orphan.push_str(&format!(
                "{}\n{} --> {}\nforeign {}\n\n",
                index + 1,
                crate::srt::fmt_ts(index * 2_000),
                crate::srt::fmt_ts(index * 2_000 + 1_000),
                index
            ));
        }
        assert_eq!(crate::srt::parse_srt(&orphan).len(), 10);
        std::fs::write(&target, orphan.as_bytes()).unwrap();
        let generated = b"generated by this run";
        let installed = install_sidecar(&target, generated).unwrap();
        assert_eq!(std::fs::read(&target).unwrap(), generated);
        assert_eq!(installed.previous.as_deref(), Some(orphan.as_bytes()));
        assert_eq!(installed.artifact_sha256, digest_bytes(generated));
        assert_ne!(installed.artifact_sha256, digest_bytes(orphan.as_bytes()));
    }

    #[test]
    fn failed_commit_cleanup_restores_unchanged_previous_sidecar() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("ep.id.hi.srt");
        let previous = b"previous admitted candidate";
        std::fs::write(&target, previous).unwrap();
        let installed = install_sidecar(&target, b"generated").unwrap();
        installed.rollback_if_unchanged().unwrap();
        assert_eq!(std::fs::read(&target).unwrap(), previous);
    }

    #[test]
    fn failed_commit_cleanup_removes_unadmitted_install() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("ep.id.hi.srt");
        let installed = install_sidecar(&target, b"generated").unwrap();
        installed.rollback_if_unchanged().unwrap();
        assert!(!target.exists());
    }

    #[test]
    fn failed_commit_cleanup_does_not_clobber_concurrent_replacement() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("ep.id.hi.srt");
        let installed = install_sidecar(&target, b"generated").unwrap();
        std::fs::write(&target, b"concurrent replacement").unwrap();
        assert!(!installed.still_current().unwrap());
        installed.rollback_if_unchanged().unwrap();
        assert_eq!(std::fs::read(&target).unwrap(), b"concurrent replacement");
    }

    #[test]
    fn ledger_append_is_idempotent_after_partial_commit() {
        use crate::feature_modules::pipeline_commit::*;
        let dir = tempfile::tempdir().unwrap();
        let paths = LedgerPaths {
            registry: dir.path().join("registry"),
            state: dir.path().join("state"),
        };
        let identity = LedgerIdentity {
            kind: "series".into(),
            episode_id: 7,
            language: "id".into(),
            artifact_sha256: [4; 32],
        };
        let row = serde_json::json!({"kind":"series","episode_id":7,"language":"id","artifact_sha256":"0404040404040404040404040404040404040404040404040404040404040404"});
        commit_target_ledgers(LedgerCommitRequest::TargetLedgerCommit {
            paths: paths.clone(),
            identity: identity.clone(),
            registry_row: row.clone(),
            state_row: row.clone(),
        })
        .unwrap();
        commit_target_ledgers(LedgerCommitRequest::TargetLedgerCommit {
            paths: paths.clone(),
            identity,
            registry_row: row.clone(),
            state_row: row,
        })
        .unwrap();
        assert_eq!(
            std::fs::read_to_string(paths.registry)
                .unwrap()
                .lines()
                .count(),
            1
        );
    }

    #[test]
    fn conflicting_ledger_identity_is_storage_failure() {
        use crate::feature_modules::pipeline_commit::*;
        let dir = tempfile::tempdir().unwrap();
        let paths = LedgerPaths {
            registry: dir.path().join("registry"),
            state: dir.path().join("state"),
        };
        let identity = LedgerIdentity {
            kind: "series".into(),
            episode_id: 7,
            language: "id".into(),
            artifact_sha256: [5; 32],
        };
        let row = serde_json::json!({"kind":"series","episode_id":7,"language":"id","artifact_sha256":"0505050505050505050505050505050505050505050505050505050505050505"});
        commit_target_ledgers(LedgerCommitRequest::TargetLedgerCommit {
            paths: paths.clone(),
            identity: identity.clone(),
            registry_row: row.clone(),
            state_row: row.clone(),
        })
        .unwrap();
        let mut conflict = row.clone();
        conflict["source"] = serde_json::json!("different");
        assert_eq!(
            commit_target_ledgers(LedgerCommitRequest::TargetLedgerCommit {
                paths,
                identity,
                registry_row: conflict,
                state_row: row.clone(),
            })
            .unwrap_err(),
            CommitLedgerError::Contradiction
        );
    }
}
