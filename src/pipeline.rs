//! Staged subtitle pipeline: wanted -> ASR -> translate -> upload.
//!
//! Throughput architecture (the point of the Rust rewrite):
//!
//! ```text
//! discover once per pass (Bazarr wanted + movies: 2 calls, skip-filtered)
//!   │
//!   ▼
//! episode workers × EPISODE_CONCURRENCY (default min(ncpu,8))
//!   │  ├─ ladder fast-path: adequate ja/en sidecar or Jimaku direct → skip ASR
//!   │  ├─ ASR: ffmpeg extract → remote Whisper (per-endpoint semaphores)
//!   │  ├─ translate: chunk fan-out over fastest-first provider pool
//!   │  └─ upload: Bazarr 204 → verify sidecar → direct-write fallback
//!   ▼
//! commit (registry upsert + state append under flock, atomic SRT install)
//! ```
//!
//! The pass lives here (`run_pass`, `discover`); the stages live in
//! focused modules: [`crate::actions`] (skip/retry/delete),
//! [`crate::ladder`] (cheap text sources), [`crate::episode`]
//! (per-episode translate + commit).
//!
//! No stage ever holds model weights; RSS stays flat (~tens of MB) no matter
//! how many episodes are queued. Every remote call has a deadline so one hung
//! free-tier endpoint cannot stall the sweep.

use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

use tokio::sync::Semaphore;

#[path = "pipeline_reports.rs"]
mod pipeline_reports;
#[path = "pipeline_targets.rs"]
mod pipeline_targets;
pub(crate) use pipeline_reports::{reduce_target_outcomes, PassOutcome};
use pipeline_targets::movie_original_lang;
#[allow(unused_imports)]
pub(crate) use pipeline_targets::TargetKey;
pub(crate) use pipeline_targets::{verified_targets, VerifiedTarget};

use crate::bazarr::Bazarr;
use crate::config::Config;
use crate::feature_modules::discord_types::{
    AggregateDisposition, BoundedReports, EpisodeRunResult, TargetStatus,
};
use crate::feature_modules::process::ToolPaths;
use crate::glossary::Glossary;
use crate::jellyfin::Jellyfin;
use crate::jimaku::Jimaku;
use crate::lang::normalize_lang;
use crate::providers::ProviderPool;
use crate::sonarr::Sonarr;
use crate::state::{self, StateEntry};

#[derive(Debug, Clone)]
pub struct Candidate {
    pub episode_id: i64,
    pub series_id: Option<i64>,
    pub series_title: String,
    pub path: Option<String>,
    pub missing: Vec<String>,
    pub is_movie: bool,
    /// Media original language, best effort (source-track choice). Movies
    /// carry it straight from the Radarr payload; series resolve it from the
    /// pass's Sonarr listing in `process_one`. `None` never fails a pass.
    pub original_lang: Option<String>,
}

#[derive(Debug, Clone, Default)]
pub struct PassStats {
    pub scanned: usize,
    pub processed: usize,
    pub done: usize,
    pub failed: usize,
    pub skipped: usize,
}

pub struct Pipeline {
    pub cfg: Config,
    pub pool: ProviderPool,
    pub sonarr: Sonarr,
    pub bazarr: Bazarr,
    pub jellyfin: Jellyfin,
    pub jimaku: Jimaku,
    pub glossary: Glossary,
    pub paused: Arc<AtomicBool>,
    pub processed_total: AtomicU64,
    /// Bounds concurrent Bazarr uploads across episodes (Bazarr queues each
    /// upload as an async job; unbounded bursts wedged it in the past).
    pub(crate) upload_sem: Arc<Semaphore>,
    /// Last Jimaku-direct attempt per stem (misses become re-eligible after
    /// `JIMAKU_RETRY_COOLDOWN`; successes land a sidecar so the ladder never
    /// asks again). In-memory: a daemon restart retries everything, which is
    /// the desired backstop, not a bug.
    pub(crate) jimaku_tried:
        std::sync::Mutex<std::collections::HashMap<String, std::time::Instant>>,
    pub(crate) tools: ToolPaths,
}

impl Pipeline {
    pub fn new(cfg: Config, pool: ProviderPool, http: reqwest::Client) -> Self {
        Self::new_with_tools(cfg, pool, http, ToolPaths::production())
    }

    pub(crate) fn new_with_tools(
        cfg: Config,
        pool: ProviderPool,
        http: reqwest::Client,
        tools: ToolPaths,
    ) -> Self {
        let sonarr = Sonarr::new(&cfg.sonarr_url, &cfg.sonarr_api_key, http.clone());
        let bazarr = Bazarr::new(
            &cfg.bazarr_url,
            &cfg.bazarr_api_key,
            cfg.bazarr_url_2.clone(),
            &cfg.bazarr_api_key_2,
            http.clone(),
        );
        let jellyfin = Jellyfin::with_root(
            &cfg.jellyfin_url,
            &cfg.jellyfin_api_key,
            &cfg.jellyfin_media_root,
            &cfg.nas_media_prefix,
            http.clone(),
        );
        let jimaku =
            Jimaku::new(&cfg.jimaku_api_key, http.clone()).with_cache(cfg.anilist_cache.clone());
        let glossary = Glossary::load(&cfg.glossary_file);
        let upload_sem = Arc::new(Semaphore::new(cfg.upload_concurrency.max(1)));
        Self {
            cfg,
            pool,
            sonarr,
            bazarr,
            jellyfin,
            jimaku,
            glossary,
            paused: Arc::new(AtomicBool::new(false)),
            processed_total: AtomicU64::new(0),
            upload_sem,
            jimaku_tried: std::sync::Mutex::new(std::collections::HashMap::new()),
            tools,
        }
    }

    /// One full pass: consume actions, discover once, process bounded.
    ///
    /// Discovery runs exactly once per pass (Bazarr `wanted` + `movies`);
    /// episodes are processed concurrently under an `EPISODE_CONCURRENCY`
    /// semaphore. Skipped ids from `consume_actions` filter candidates
    /// before the `MAX_EPS_PER_RUN` cap is applied.
    async fn run_pass_legacy(&self) -> (PassStats, BoundedReports, u64) {
        let (skip_ids, retries) = self.consume_actions().await;
        // Discover (Bazarr) and series titles (Sonarr) are independent:
        // fire together, latency is the max, not the sum.
        let (mut candidates, series_titles) =
            tokio::join!(self.discover(&skip_ids), self.sonarr.series_titles());
        // Inline retries: reprocess THIS pass instead of waiting for Bazarr
        // to rescan (minutes) and a later pass to notice. Resolution mirrors
        // discover's filters — skips and exclusions win over retries, and an
        // episode Bazarr already reports missing is processed once (the
        // inline copy wins; same work either way, never double-transcribed).
        if !retries.is_empty() {
            let inline = self.resolve_retries(&retries, &skip_ids).await;
            if !inline.is_empty() {
                let keys: std::collections::HashSet<(bool, i64)> =
                    inline.iter().map(|c| (c.is_movie, c.episode_id)).collect();
                candidates.retain(|c| !keys.contains(&(c.is_movie, c.episode_id)));
                candidates.splice(..0, inline);
            }
        }
        let mut stats = PassStats {
            scanned: candidates.len(),
            ..Default::default()
        };
        let caps: Vec<_> = candidates
            .into_iter()
            .take(self.cfg.max_eps_per_run.max(1))
            .collect();
        stats.processed = caps.len();
        if caps.is_empty() {
            let (reports, omitted) =
                pipeline_reports::bound_pass_reports(Vec::new()).expect("empty report collector");
            return (stats, reports, omitted);
        }
        let sem = Arc::new(Semaphore::new(self.cfg.episode_concurrency.max(1)));
        let mut jobs = Vec::with_capacity(caps.len());
        for cand in caps {
            let sem = sem.clone();
            let series_titles = &series_titles;
            jobs.push(async move {
                let _p = sem.acquire_owned().await.expect("semaphore closed");
                let result = self.process_one(&cand, series_titles).await;
                (cand, result)
            });
        }
        let mut reports = Vec::new();
        for (cand, result) in futures::future::join_all(jobs).await {
            let eid = cand.episode_id;
            match result {
                Ok(EpisodeRunResult::Report(report)) => {
                    let has_completed =
                        report.targets().as_slice().iter().any(|target| {
                            matches!(target.status(), TargetStatus::Completed { .. })
                        });
                    let aggregate = report.aggregate();
                    match aggregate {
                        AggregateDisposition::Complete
                        | AggregateDisposition::CompleteWithWarning => {
                            if has_completed {
                                stats.done += 1;
                                self.processed_total.fetch_add(1, Ordering::Relaxed);
                            } else {
                                stats.failed += 1;
                            }
                        }
                        AggregateDisposition::Partial => {
                            if has_completed {
                                stats.done += 1;
                                self.processed_total.fetch_add(1, Ordering::Relaxed);
                            }
                            stats.failed += 1;
                        }
                        AggregateDisposition::Failed => stats.failed += 1,
                    }
                    if aggregate == AggregateDisposition::Failed {
                        let kind = if cand.is_movie { "movie" } else { "series" };
                        self.append_state(eid, None, "error", "", kind).await;
                    }
                    reports.push(report);
                }
                Ok(EpisodeRunResult::NoTarget(_)) => stats.skipped += 1,
                Err(error) => {
                    tracing::warn!(
                        episode = eid,
                        error = %crate::config::mask_for_log(&error.to_string()),
                        "episode failed"
                    );
                    stats.failed += 1;
                    let kind = if cand.is_movie { "movie" } else { "series" };
                    self.append_state(eid, None, "error", "", kind).await;
                    if let Some(report) = pipeline_reports::failure_report_for_candidate(&cand) {
                        reports.push(report);
                    }
                }
            }
        }
        let (reports, omitted) =
            pipeline_reports::bound_pass_reports(reports).expect("bounded report collector");
        (stats, reports, omitted)
    }

    pub(crate) async fn run_pass_outcome(&self) -> PassOutcome {
        let (stats, reports, omitted_reports) = self.run_pass_legacy().await;
        PassOutcome::new(stats, reports, omitted_reports)
    }

    pub async fn run_pass(&self) -> PassStats {
        self.run_pass_outcome().await.into_parts().0
    }

    #[cfg(test)]
    pub(crate) async fn run_pass_with_tools(&self, _tools: &ToolPaths) -> PassStats {
        self.run_pass_outcome().await.into_parts().0
    }

    /// Resolve operator retries into same-pass candidates (see `run_pass`).
    /// Skips, exclusions, and unresolvable ids drop the spec (logged);
    /// resolution never fails the pass.
    async fn resolve_retries(
        &self,
        retries: &[crate::actions::RetrySpec],
        skip_ids: &std::collections::HashSet<i64>,
    ) -> Vec<Candidate> {
        if retries.is_empty() {
            return Vec::new();
        }
        let excluded = state::parse_exclusions(&self.cfg.exclusions_file);
        // One movies listing shared by all movie retries (retry passes only).
        let need_movies = retries.iter().any(|r| r.kind == "movie");
        let movies: Vec<serde_json::Value> = if need_movies {
            self.bazarr.movies().await.unwrap_or_default()
        } else {
            Vec::new()
        };
        let mut out = Vec::new();
        for r in retries {
            if skip_ids.contains(&r.episode_id) || excluded.contains(&r.episode_id) {
                continue;
            }
            if r.kind == "movie" {
                let found = movies
                    .iter()
                    .find(|m| m.get("radarrId").and_then(|v| v.as_i64()) == Some(r.episode_id));
                let Some(m) = found else {
                    tracing::warn!(
                        episode = r.episode_id,
                        "action: retry movie not in library, skipping"
                    );
                    continue;
                };
                let path = m.get("path").and_then(|p| p.as_str()).unwrap_or("");
                if path.is_empty() || !Path::new(&self.cfg.map_path(path)).is_file() {
                    tracing::warn!(
                        episode = r.episode_id,
                        "action: retry movie file missing, skipping"
                    );
                    continue;
                }
                out.push(Candidate {
                    episode_id: r.episode_id,
                    series_id: None,
                    series_title: m
                        .get("title")
                        .and_then(|t| t.as_str())
                        .unwrap_or("?")
                        .to_string(),
                    path: Some(path.to_string()),
                    missing: r.langs.clone(),
                    is_movie: true,
                    original_lang: movie_original_lang(m),
                });
            } else {
                match self.sonarr.episode(r.episode_id).await {
                    Ok(ep) => out.push(Candidate {
                        episode_id: r.episode_id,
                        series_id: ep.series_id,
                        // Title resolved from the pass titles map in process_one.
                        series_title: "?".to_string(),
                        path: None,
                        missing: r.langs.clone(),
                        is_movie: false,
                        // Resolved from the Sonarr series listing in process_one.
                        original_lang: None,
                    }),
                    Err(e) => {
                        tracing::warn!(
                            episode = r.episode_id,
                            error = %crate::config::mask_for_log(&e.to_string()),
                            "action: retry episode lookup failed, skipping"
                        )
                    }
                }
            }
        }
        out
    }

    /// Wanted (series) + movie sweep, minus exclusions / done state / skips.
    ///
    /// `skip_ids` (from consumed `skip` actions) filter candidates before the
    /// caller's `MAX_EPS_PER_RUN` cap. Done keys are `(kind, id, lang)` so a
    /// series episode and a movie sharing a numeric id never collide.
    pub async fn discover(&self, skip_ids: &std::collections::HashSet<i64>) -> Vec<Candidate> {
        let excluded = state::parse_exclusions(&self.cfg.exclusions_file);
        let state_rows = state::load_jsonl::<StateEntry>(&self.cfg.state_file);
        let done: std::collections::HashSet<(String, i64, String)> = state_rows
            .iter()
            .filter(|e| e.status.as_deref() == Some("done"))
            .filter_map(|e| {
                Some((
                    e.kind.as_deref().unwrap_or("series").to_string(),
                    e.episode_id?,
                    normalize_lang(e.language.as_deref()?),
                ))
            })
            .collect();
        let mut out = Vec::new();
        // Done state only suppresses a language while a matching registry row
        // and the recorded artifact digest still match the sidecar on disk.
        // A stale or digest-mismatched pair resurfaces as missing, so a
        // foreign replacement cannot be admitted by a leftover done row.
        let registry_rows = state::load_jsonl::<state::RegistryRow>(&self.cfg.registry_file);
        let verified = verified_targets(&registry_rows, &state_rows);
        // Wanted + movies are independent Bazarr calls: fire together,
        // latency is the max, not the sum.
        let (wanted, movies) = tokio::join!(self.bazarr.wanted(), self.bazarr.movies());
        // Series wanted.
        match wanted {
            Ok(items) => {
                for it in items {
                    if excluded.contains(&it.episode_id) || skip_ids.contains(&it.episode_id) {
                        continue;
                    }
                    let missing: Vec<String> = it
                        .missing
                        .iter()
                        .filter(|l| self.cfg.target_langs.contains(l))
                        .filter(|l| {
                            !done.contains(&("series".to_string(), it.episode_id, (*l).clone()))
                                || !verified.contains_key(&(
                                    "series".to_string(),
                                    it.episode_id,
                                    (*l).clone(),
                                ))
                        })
                        .cloned()
                        .collect();
                    if missing.is_empty() {
                        continue;
                    }
                    out.push(Candidate {
                        episode_id: it.episode_id,
                        series_id: it.series_id,
                        series_title: it.series_title.clone().unwrap_or_else(|| "?".to_string()),
                        path: it
                            .raw
                            .get("path")
                            .and_then(|p| p.as_str())
                            .map(str::to_string),
                        missing,
                        is_movie: false,
                        // Resolved from the Sonarr series listing in process_one.
                        original_lang: None,
                    });
                }
            }
            Err(e) => tracing::warn!(
                error = %crate::config::mask_for_log(&e.to_string()),
                "bazarr wanted fetch failed"
            ),
        }
        // Movies (optional; never fails the pass).
        if let Ok(movies) = movies {
            for m in movies {
                let Some(rid) = m.get("radarrId").and_then(|v| v.as_i64()) else {
                    continue;
                };
                if excluded.contains(&rid) || skip_ids.contains(&rid) {
                    continue;
                }
                if m.get("monitored").and_then(|v| v.as_bool()) == Some(false) {
                    continue;
                }
                let path = m.get("path").and_then(|p| p.as_str()).unwrap_or("");
                if path.is_empty() {
                    continue;
                }
                let media = self.cfg.map_path(path);
                if !Path::new(&media).is_file() {
                    continue;
                }
                let mut missing = Vec::new();
                for l in &self.cfg.target_langs {
                    if done.contains(&("movie".to_string(), rid, l.clone()))
                        && verified.contains_key(&("movie".to_string(), rid, l.clone()))
                    {
                        continue;
                    }
                    missing.push(l.clone());
                }
                if !missing.is_empty() {
                    out.push(Candidate {
                        episode_id: rid,
                        series_id: None,
                        series_title: m
                            .get("title")
                            .and_then(|t| t.as_str())
                            .unwrap_or("?")
                            .to_string(),
                        path: Some(path.to_string()),
                        missing,
                        is_movie: true,
                        original_lang: movie_original_lang(&m),
                    });
                }
            }
        }
        out.sort_by_key(|c| c.episode_id);
        out
    }
}

#[cfg(test)]
#[path = "pipeline_tests.rs"]
mod tests;
