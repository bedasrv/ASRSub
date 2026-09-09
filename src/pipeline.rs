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
        let (skip_ids, retries) = self.consume_actions().await;
        // Discover (Bazarr) and series titles (Sonarr) are independent:
        // fire together, latency is the max, not the sum.
        let (mut candidates, titles) =
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
            return stats;
        }
        let sem = Arc::new(Semaphore::new(self.cfg.episode_concurrency.max(1)));
        let mut jobs = Vec::with_capacity(caps.len());
        for cand in caps {
            let sem = sem.clone();
            let titles = &titles;
            jobs.push(async move {
                let _p = sem.acquire_owned().await.expect("semaphore closed");
                (cand.episode_id, self.process_one(&cand, titles).await)
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
                    }),
                    Err(e) => {
                        tracing::warn!(episode = r.episode_id, error = %e, "action: retry episode lookup failed, skipping")
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
        // Done state only suppresses a language while the sidecar the
        // registry points at is still on disk. A `done` row whose file is
        // gone (user deleted it, NAS hiccup) must resurface as missing —
        // otherwise the subtitle is lost forever with no signal (legacy
        // parity: wanted/library never expose stale done). No registry row
        // at all also resurfaces: registry and state are committed in the
        // same flow, so a missing row means no verified completion.
        let verified = verified_targets(&state::load_jsonl::<state::RegistryRow>(
            &self.cfg.registry_file,
        ));
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
                                || !verified.contains(&(
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
                    });
                }
            }
            Err(e) => tracing::warn!(error = %e, "bazarr wanted fetch failed"),
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
                let stem = crate::lang::stem_of(&media);
                let mut missing = Vec::new();
                for l in &self.cfg.target_langs {
                    if done.contains(&("movie".to_string(), rid, l.clone()))
                        && verified.contains(&("movie".to_string(), rid, l.clone()))
                    {
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

/// Registry rows whose recorded target sidecar is still on disk, keyed
/// `(kind, episode_id, lang)`. Pure over the row list (the `is_file` probe
/// is the only I/O) for testability. `kind` defaults to `series` — series
/// rows omit it, movies carry `"kind": "movie"` in `extra`.
fn verified_targets(
    rows: &[state::RegistryRow],
) -> std::collections::HashSet<(String, i64, String)> {
    rows.iter()
        .filter_map(|r| {
            let target = r.target_path.as_deref()?;
            if !Path::new(target).is_file() {
                return None;
            }
            Some((
                r.extra
                    .get("kind")
                    .and_then(|k| k.as_str())
                    .unwrap_or("series")
                    .to_string(),
                r.episode_id?,
                crate::lang::normalize_lang(r.lang.as_deref().unwrap_or("")),
            ))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn verified_targets_needs_row_and_file() {
        let dir = tempfile::tempdir().unwrap();
        let live = dir.path().join("ep.id.hi.srt");
        std::fs::write(&live, "x").unwrap();
        let row = |kind: Option<&str>, id: i64, lang: &str, target: &str| state::RegistryRow {
            stem: None,
            lang: Some(lang.to_string()),
            episode_id: Some(id),
            source: None,
            source_kind: None,
            source_path: None,
            target_path: Some(target.to_string()),
            ts: None,
            extra: kind
                .map(|k| {
                    [("kind".to_string(), serde_json::Value::String(k.to_string()))]
                        .into_iter()
                        .collect()
                })
                .unwrap_or_default(),
        };
        let rows = vec![
            // Verified: row + file on disk (alias normalized to canonical).
            row(None, 7, "ind", live.to_str().unwrap()),
            // Stale: row exists but the file is gone.
            row(
                None,
                7,
                "en",
                dir.path().join("gone.en.hi.srt").to_str().unwrap(),
            ),
            // Movie row without kind defaults to series, not movie.
            row(None, 100, "id", live.to_str().unwrap()),
            row(Some("movie"), 100, "id", live.to_str().unwrap()),
        ];
        let v = verified_targets(&rows);
        assert!(v.contains(&("series".to_string(), 7, "id".to_string())));
        assert!(!v.contains(&("series".to_string(), 7, "en".to_string())));
        assert!(v.contains(&("series".to_string(), 100, "id".to_string())));
        assert!(v.contains(&("movie".to_string(), 100, "id".to_string())));
    }
}
