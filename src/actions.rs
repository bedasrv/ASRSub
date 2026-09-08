//! Dashboard action handling: `skip` / `retry` / `delete`.
//!
//! Consumed once per pass from `actions.jsonl` (atomic drain in
//! `state::consume_actions`). Mirrors `consume_actions` in orchestrator.py:
//! `skip` excludes ids from the pass; `retry` clears state rows AND removes
//! subtitle files (Bazarr only re-wants missing files); `delete` additionally
//! refreshes Jellyfin. `kind` (`series`/`movie`) routes each record and
//! `language` scopes it (null = whole episode).

use std::path::Path;

use crate::lang::{normalize_lang, replaceable_target_sidecar_paths};
use crate::pipeline::Pipeline;
use crate::state::{self, StateEntry};

impl Pipeline {
    /// Process pending actions once per pass; returns ids to skip this pass.
    ///
    /// Mirrors `consume_actions` in orchestrator.py:
    ///
    /// - `skip`: episode excluded from this pass's candidates.
    /// - `retry`: state rows cleared AND subtitle files removed (same deleter
    ///   as `delete`) — Bazarr only re-wants episodes with missing files, so
    ///   clearing state alone would never reprocess. `language` scopes both
    ///   the state clearing and the file deletion (null = whole episode).
    /// - `delete`: sidecars + tmp copies removed, done state cleared (so the
    ///   episode regenerates), Jellyfin refresh drops the removed subtitle.
    ///
    /// `kind` (`series` default / `movie`) routes each record; unknown
    /// episode ids (non-integer) are ignored.
    pub(crate) async fn consume_actions(&self) -> std::collections::HashSet<i64> {
        let mut skip_ids = std::collections::HashSet::new();
        let records = state::consume_actions(&self.cfg.actions_file);
        if records.is_empty() {
            return skip_ids;
        }
        // Group retry/delete by (id -> kinds + langs) like Python.
        let mut retry: std::collections::HashMap<
            i64,
            (
                std::collections::HashSet<String>,
                std::collections::HashSet<Option<String>>,
            ),
        > = Default::default();
        let mut delete: std::collections::HashMap<
            i64,
            (
                std::collections::HashSet<String>,
                std::collections::HashSet<Option<String>>,
            ),
        > = Default::default();
        for rec in &records {
            let Some(eid) = rec.episode_id else { continue };
            let raw_kind = rec.kind.as_deref().unwrap_or("series");
            let kind = normalize_lang(raw_kind);
            // Unknown kinds are logged and dropped (never silently match).
            if kind != "series" && kind != "movie" {
                tracing::warn!(
                    episode = eid,
                    kind = raw_kind,
                    "action: unknown kind, dropping record"
                );
                continue;
            }
            let action = rec.r#type.as_deref().unwrap_or("");
            match action {
                "skip" => {
                    skip_ids.insert(eid);
                }
                "retry" | "delete" => {
                    let map = if action == "retry" {
                        &mut retry
                    } else {
                        &mut delete
                    };
                    let entry = map.entry(eid).or_default();
                    entry.0.insert(kind);
                    let lang = rec
                        .language
                        .as_deref()
                        .map(normalize_lang)
                        .filter(|l| !l.is_empty());
                    entry.1.insert(lang);
                }
                _ => {}
            }
        }
        if !retry.is_empty() || !delete.is_empty() {
            let state_rows: Vec<StateEntry> = state::load_jsonl(&self.cfg.state_file);
            let mut kept = Vec::with_capacity(state_rows.len());
            for e in state_rows {
                let drop = match e.episode_id {
                    Some(eid) => {
                        // State-row kinds are normalized like action kinds so
                        // `Series`/`MOVIE` spellings still match.
                        let e_kind = normalize_lang(e.kind.as_deref().unwrap_or("series"));
                        let e_lang = e.language.as_deref().map(normalize_lang);
                        [(&retry, "retry"), (&delete, "delete")]
                            .iter()
                            .any(|(map, _)| match map.get(&eid) {
                                Some((kinds, langs)) => {
                                    kinds.contains(&e_kind)
                                        && (langs.contains(&None)
                                            || e_lang
                                                .as_ref()
                                                .map(|l| langs.contains(&Some(l.clone())))
                                                .unwrap_or(false))
                                }
                                None => false,
                            })
                    }
                    None => false,
                };
                if !drop {
                    kept.push(e);
                }
            }
            let _ = state::rewrite_jsonl(&self.cfg.state_file, &kept);
        }
        // Deletes first (files + registry + refresh), then retries (files +
        // registry + wanted refill so they re-enter `wanted` immediately).
        for eid in sorted_keys(&delete) {
            let (kinds, langs) = &delete[&eid];
            let lang_list = lang_scope(langs);
            if kinds.contains("series") {
                let n = self.delete_episode_subtitles(eid, lang_list.clone()).await;
                tracing::info!("action: delete episode {eid} (removed {n} SRTs, state cleared)");
            }
            if kinds.contains("movie") {
                let n = self.delete_movie_subtitles(eid, lang_list.clone()).await;
                tracing::info!("action: delete movie {eid} (removed {n} SRTs, state cleared)");
            }
            self.refresh_for_action(eid, kinds).await;
        }
        for eid in sorted_keys(&retry) {
            let (kinds, langs) = &retry[&eid];
            let lang_list = lang_scope(langs);
            if kinds.contains("series") {
                let n = self.delete_episode_subtitles(eid, lang_list.clone()).await;
                tracing::info!("action: retry episode {eid} (state cleared, {n} SRTs removed)");
            }
            if kinds.contains("movie") {
                let n = self.delete_movie_subtitles(eid, lang_list.clone()).await;
                tracing::info!("action: retry movie {eid} (state cleared, {n} SRTs removed)");
            }
        }
        for eid in sorted_set(&skip_ids) {
            tracing::info!("action: skip episode {eid} (excluded this pass)");
        }
        skip_ids
    }

    /// Jellyfin refresh after a delete action (series and/or movie item).
    async fn refresh_for_action(&self, eid: i64, kinds: &std::collections::HashSet<String>) {
        if kinds.contains("series") {
            if let Ok(ep) = self.sonarr.episode(eid).await {
                if let Some(p) = ep.episode_file.and_then(|f| f.path) {
                    let media = self.cfg.map_path(&p);
                    let title = ep.title.unwrap_or_default();
                    self.jellyfin.refresh_for(&media, &title, "Episode").await;
                }
            }
        }
        if kinds.contains("movie") {
            if let Ok(movies) = self.bazarr.movies().await {
                if let Some(m) = movies
                    .iter()
                    .find(|m| m.get("radarrId").and_then(|v| v.as_i64()) == Some(eid))
                {
                    if let Some(p) = m.get("path").and_then(|v| v.as_str()) {
                        let media = self.cfg.map_path(p);
                        let title = m
                            .get("title")
                            .and_then(|v| v.as_str())
                            .unwrap_or("")
                            .to_string();
                        self.jellyfin.refresh_for(&media, &title, "Movie").await;
                    }
                }
            }
        }
    }

    fn target_langs_or(&self, langs: Option<Vec<String>>) -> Vec<String> {
        match langs {
            Some(v) if !v.is_empty() => v,
            _ => self.cfg.target_langs.clone(),
        }
    }

    /// Remove every `{stem}.{lang}[.hi].srt` variant + tmp copies for one
    /// series episode; drops matching registry rows; nudges Bazarr wanted.
    /// Returns the number of files removed. Never raises.
    async fn delete_episode_subtitles(&self, episode_id: i64, langs: Option<Vec<String>>) -> usize {
        let langs = self.target_langs_or(langs);
        let mut removed = 0;
        if let Ok(ep) = self.sonarr.episode(episode_id).await {
            if let Some(p) = ep.episode_file.and_then(|f| f.path) {
                let media = self.cfg.map_path(&p);
                if Path::new(&media).is_file() {
                    let stem = media.rsplit_once('.').map(|(s, _)| s).unwrap_or(&media);
                    for l in &langs {
                        for cand in replaceable_target_sidecar_paths(stem, l) {
                            if tokio::fs::remove_file(&cand).await.is_ok() {
                                removed += 1;
                            }
                        }
                        state::registry_delete(
                            &self.cfg.registry_file,
                            Some(stem),
                            Some(episode_id),
                            l,
                        );
                    }
                }
            }
        } else {
            tracing::warn!("action: delete: episode {episode_id} not in Sonarr; tmp cleanup only");
        }
        for l in &langs {
            let tmp = self.cfg.tmp_dir.join(format!("{episode_id}_{l}.srt"));
            if tokio::fs::remove_file(&tmp).await.is_ok() {
                removed += 1;
            }
        }
        self.bazarr.wanted_refill("series").await;
        removed
    }

    /// Movie counterpart of [`Pipeline::delete_episode_subtitles`] (path
    /// resolved via Bazarr/Radarr; movies are not in Sonarr).
    async fn delete_movie_subtitles(&self, movie_id: i64, langs: Option<Vec<String>>) -> usize {
        let langs = self.target_langs_or(langs);
        let mut removed = 0;
        if let Ok(movies) = self.bazarr.movies().await {
            if let Some(m) = movies
                .iter()
                .find(|m| m.get("radarrId").and_then(|v| v.as_i64()) == Some(movie_id))
            {
                if let Some(p) = m.get("path").and_then(|v| v.as_str()) {
                    let media = self.cfg.map_path(p);
                    if Path::new(&media).is_file() {
                        let stem = media.rsplit_once('.').map(|(s, _)| s).unwrap_or(&media);
                        for l in &langs {
                            for cand in replaceable_target_sidecar_paths(stem, l) {
                                if tokio::fs::remove_file(&cand).await.is_ok() {
                                    removed += 1;
                                }
                            }
                            state::registry_delete(
                                &self.cfg.registry_file,
                                Some(stem),
                                Some(movie_id),
                                l,
                            );
                        }
                    }
                }
            }
        }
        for l in &langs {
            let tmp = self.cfg.tmp_dir.join(format!("{movie_id}_{l}.srt"));
            if tokio::fs::remove_file(&tmp).await.is_ok() {
                removed += 1;
            }
        }
        self.bazarr.wanted_refill("movie").await;
        removed
    }
}

/// Sorted ids of an action group map (deterministic log/apply order).
fn sorted_keys<V>(map: &std::collections::HashMap<i64, V>) -> Vec<i64> {
    let mut ids: Vec<i64> = map.keys().copied().collect();
    ids.sort_unstable();
    ids
}

fn sorted_set(set: &std::collections::HashSet<i64>) -> Vec<i64> {
    let mut ids: Vec<i64> = set.iter().copied().collect();
    ids.sort_unstable();
    ids
}

/// Language scope for a retry/delete group: `None` (null language present)
/// means the whole episode, else the sorted explicit list.
fn lang_scope(langs: &std::collections::HashSet<Option<String>>) -> Option<Vec<String>> {
    if langs.contains(&None) {
        return None;
    }
    let mut v: Vec<String> = langs.iter().filter_map(|l| l.clone()).collect();
    v.sort();
    v.dedup();
    Some(v)
}
