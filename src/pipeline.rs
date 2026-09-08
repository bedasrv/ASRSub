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

use crate::bazarr::Bazarr;
use crate::config::Config;
use crate::episode::sidecar_exists;
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
}

impl Pipeline {
    pub fn new(cfg: Config, pool: ProviderPool, http: reqwest::Client) -> Self {
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
        }
    }

    /// One full pass: consume actions, discover once, process bounded.
    ///
    /// Discovery runs exactly once per pass (Bazarr `wanted` + `movies`);
    /// episodes are processed concurrently under an `EPISODE_CONCURRENCY`
    /// semaphore. Skipped ids from `consume_actions` filter candidates
    /// before the `MAX_EPS_PER_RUN` cap is applied.
    pub async fn run_pass(&self) -> PassStats {
        let skip_ids = self.consume_actions().await;
        let candidates = self.discover(&skip_ids).await;
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
            return stats;
        }
        let sem = Arc::new(Semaphore::new(self.cfg.episode_concurrency.max(1)));
        let mut jobs = Vec::with_capacity(caps.len());
        for cand in caps {
            let sem = sem.clone();
            jobs.push(async move {
                let _p = sem.acquire_owned().await.expect("semaphore closed");
                (cand.episode_id, self.process_one(&cand).await)
            });
        }
        for (eid, r) in futures::future::join_all(jobs).await {
            match r {
                Ok(n) if n > 0 => {
                    stats.done += 1;
                    self.processed_total.fetch_add(1, Ordering::Relaxed);
                }
                Ok(_) => stats.skipped += 1,
                Err(e) => {
                    tracing::warn!(episode = eid, error = %e, "episode failed");
                    stats.failed += 1;
                    self.append_state(eid, None, "error", "", "series").await;
                }
            }
        }
        stats
    }

    /// Wanted (series) + movie sweep, minus exclusions / done state / skips.
    ///
    /// `skip_ids` (from consumed `skip` actions) filter candidates before the
    /// caller's `MAX_EPS_PER_RUN` cap. Done keys are `(kind, id, lang)` so a
    /// series episode and a movie sharing a numeric id never collide.
    pub async fn discover(&self, skip_ids: &std::collections::HashSet<i64>) -> Vec<Candidate> {
        let excluded = state::parse_exclusions(&self.cfg.exclusions_file);
        let done: std::collections::HashSet<(String, i64, String)> =
            state::load_jsonl::<StateEntry>(&self.cfg.state_file)
                .into_iter()
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
        // Series wanted.
        match self.bazarr.wanted().await {
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
                    });
                }
            }
            Err(e) => tracing::warn!(error = %e, "bazarr wanted fetch failed"),
        }
        // Movies (optional; never fails the pass).
        if let Ok(movies) = self.bazarr.movies().await {
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
                let stem = media.rsplit_once('.').map(|(s, _)| s).unwrap_or(&media);
                let mut missing = Vec::new();
                for l in &self.cfg.target_langs {
                    if done.contains(&("movie".to_string(), rid, l.clone())) {
                        continue;
                    }
                    if sidecar_exists(stem, l) {
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
                    });
                }
            }
        }
        out.sort_by_key(|c| c.episode_id);
        out
    }
}
