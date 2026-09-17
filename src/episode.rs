//! Per-episode processing: source choice → translate → commit.
//!
//! For each missing language: ladder text first ([`crate::ladder`]), else
//! remote ASR (transcriptions shared across the episode's languages via a
//! per-`asr_lang` cache, so `id`+`en` pay for one Japanese transcription),
//! then remote translation keeping source timing, CPS merge, atomic install,
//! Bazarr upload, and registry/state commits.

use std::path::Path;

use anyhow::{Context, Result};

#[path = "episode_commit.rs"]
mod episode_commit;
#[path = "episode_target.rs"]
mod episode_target;

use crate::asr;
use crate::feature_modules::discord_types::{
    EpisodeKind, EpisodeRunResult, FailureClass, NoTargetCase, TargetStatus,
};
use crate::ladder::LadderQuery;
use crate::lang::normalize_lang;
use crate::pipeline::{Candidate, Pipeline};
use crate::sonarr::Episode;
use crate::srt::Cue;
use crate::state::{self, StateEntry};

use episode_commit::{
    existing_target_digest, target_is_verified, target_result, validated_report_title,
    TargetFailure,
};
use episode_target::{EpisodeCtx, LangWork};

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
    use super::episode_commit::{
        digest_bytes, install_sidecar, publish_target_ledgers, target_ledger_rows,
        validated_report_title, RegistryCommit,
    };
    use super::*;
    use crate::state::RegistryRow;

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
