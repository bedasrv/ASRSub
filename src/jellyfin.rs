//! Jellyfin library refresh: fire-and-forget, never fails an episode.
//!
//! Mirrors `jellyfin_refresh` in orchestrator.py: path-based item lookup
//! (robust against Sonarr-vs-Jellyfin title drift), `POST
//! /Items/{id}/Refresh` with full metadata refresh, then a throttled full
//! `POST /Library/Refresh` (>= 300 s apart) so new external sidecars get
//! indexed even when the realtime monitor misses them (NFS). No-op without
//! `JELLYFIN_API_KEY`. All work runs on a spawned task with ~5 s timeouts.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

#[derive(Clone)]
pub struct Jellyfin {
    base: String,
    key: String,
    media_root: String,
    http: reqwest::Client,
    last_scan: Arc<AtomicU64>,
}

impl Jellyfin {
    pub fn with_root(base: &str, key: &str, media_root: &str, http: reqwest::Client) -> Self {
        Self {
            base: base.trim_end_matches('/').to_string(),
            key: key.trim().to_string(),
            media_root: media_root.trim_end_matches('/').to_string(),
            http,
            last_scan: Arc::new(AtomicU64::new(0)),
        }
    }

    pub fn enabled(&self) -> bool {
        !self.key.is_empty() && !self.base.is_empty()
    }

    /// Fire-and-forget refresh for one media file. `item_type` is `"Episode"`
    /// (series) or `"Movie"` (radarr track).
    pub async fn refresh_for(&self, media_path: &str, title: &str, item_type: &str) {
        if !self.enabled() || media_path.is_empty() {
            return;
        }
        let this = self.clone();
        let (media_path, title, item_type) = (
            media_path.to_string(),
            title.to_string(),
            item_type.to_string(),
        );
        tokio::spawn(async move {
            this.refresh_blocking(&media_path, &title, &item_type).await;
        });
    }

    async fn refresh_blocking(&self, media_path: &str, title: &str, item_type: &str) {
        // NAS-local path → Jellyfin-container path: strip the NAS prefix this
        // host mounts the media at, prepend Jellyfin's media root. This is
        // the inverse of Config::map_path (/data/ → NAS-local); the two
        // prefixes must stay distinct (see NAS_MEDIA_PREFIX).
        let jelly_path = match media_path.strip_prefix(crate::config::NAS_MEDIA_PREFIX) {
            Some(rest) => format!("{}{}", self.media_root, rest),
            None => media_path.to_string(),
        };
        let filename = media_path.rsplit('/').next().unwrap_or(media_path);
        let item = if item_type == "Movie" {
            self.find_movie(filename, &jelly_path).await
        } else {
            self.find_episode(media_path, &jelly_path, filename).await
        };
        let item = match item {
            Some(it) => Some(it),
            None => {
                self.find_by_title(filename, &jelly_path, title, item_type)
                    .await
            }
        };
        let Some(id) = item
            .as_ref()
            .and_then(|v| v.get("Id"))
            .and_then(|v| v.as_str())
            .map(str::to_string)
        else {
            tracing::warn!(file = filename, "jellyfin: item not found");
            return;
        };
        let r = self
            .http
            .post(format!("{}/Items/{id}/Refresh", self.base))
            .header("X-Emby-Token", &self.key)
            .json(&serde_json::json!({
                "MetadataRefreshMode": "FullRefresh",
                "ImageRefreshMode": "None",
                "ReplaceAllMetadata": false,
                "ReplaceAllImages": false,
            }))
            .timeout(std::time::Duration::from_secs(5))
            .send()
            .await;
        match r {
            Ok(resp) => tracing::info!(
                file = filename,
                code = resp.status().as_u16(),
                "jellyfin: refresh"
            ),
            Err(e) => tracing::warn!(file = %media_path, error = %e, "jellyfin: refresh failed"),
        }
        self.maybe_scan().await;
    }

    /// Series lookup via the media path's series dir, then match the episode
    /// whose `Path` equals the Jellyfin-side path or endswith the filename.
    async fn find_episode(
        &self,
        media_path: &str,
        jelly_path: &str,
        filename: &str,
    ) -> Option<serde_json::Value> {
        let parts: Vec<&str> = media_path.split('/').collect();
        let series_name = if parts.len() >= 3 {
            parts[parts.len() - 3]
        } else {
            ""
        };
        if series_name.is_empty() {
            return None;
        }
        let items = self
            .get_items(&[
                ("Recursive", "true"),
                ("IncludeItemTypes", "Series"),
                ("SearchTerm", series_name),
                ("Fields", "Path"),
            ])
            .await?;
        let series = items
            .iter()
            .find(|s| {
                s.get("Path")
                    .and_then(|p| p.as_str())
                    .map(|p| p.ends_with(&format!("/{series_name}")))
                    .unwrap_or(false)
            })
            .or_else(|| items.first())
            .cloned()?;
        let sid = series.get("Id")?.as_str()?;
        let season: Option<String> = if parts.len() >= 2 {
            let dir = parts[parts.len() - 2].to_lowercase();
            dir.strip_prefix("season")
                .and_then(|n| n.trim().parse::<u32>().ok())
                .map(|n| n.to_string())
        } else {
            None
        };
        let mut query = vec![("Fields", "Path".to_string())];
        if let Some(s) = season {
            query.push(("Season", s));
        }
        let r = self
            .http
            .get(format!("{}/Shows/{sid}/Episodes", self.base))
            .query(&query)
            .header("X-Emby-Token", &self.key)
            .timeout(std::time::Duration::from_secs(5))
            .send()
            .await
            .ok()?;
        let v: serde_json::Value = r.json().await.ok()?;
        v.get("Items")?
            .as_array()?
            .iter()
            .find(|ep| {
                let p = ep.get("Path").and_then(|x| x.as_str()).unwrap_or("");
                p == jelly_path || p.ends_with(&format!("/{filename}"))
            })
            .cloned()
    }

    /// Movie lookup by filename stem, gated on path match.
    async fn find_movie(&self, filename: &str, jelly_path: &str) -> Option<serde_json::Value> {
        let stem = filename
            .rsplit_once('.')
            .map(|(s, _)| s)
            .unwrap_or(filename);
        if stem.is_empty() {
            return None;
        }
        self.get_items(&[
            ("Recursive", "true"),
            ("IncludeItemTypes", "Movie"),
            ("Fields", "Path"),
            ("SearchTerm", stem),
        ])
        .await?
        .into_iter()
        .find(|it| {
            let p = it.get("Path").and_then(|x| x.as_str()).unwrap_or("");
            p == jelly_path || p.ends_with(&format!("/{filename}"))
        })
    }

    /// Legacy SearchTerm-by-title fallback loop.
    async fn find_by_title(
        &self,
        filename: &str,
        jelly_path: &str,
        title: &str,
        item_type: &str,
    ) -> Option<serde_json::Value> {
        let mut terms = vec![if title.is_empty() { filename } else { title }];
        if !title.is_empty() {
            if let Some(prefix) = title.split([':', ';', '!', '?', '(']).next().map(str::trim) {
                if !prefix.is_empty() && prefix != title {
                    terms.push(prefix);
                }
            }
        }
        let stem = filename
            .rsplit_once('.')
            .map(|(s, _)| s)
            .unwrap_or(filename);
        if !stem.is_empty() && stem != terms[0] {
            terms.push(stem);
        }
        for term in terms {
            let items = self
                .get_items(&[
                    ("Recursive", "true"),
                    ("IncludeItemTypes", item_type),
                    ("SearchTerm", term),
                    ("Fields", "Path,MediaStreams"),
                ])
                .await
                .unwrap_or_default();
            if let Some(hit) = items.into_iter().find(|it| {
                let p = it.get("Path").and_then(|x| x.as_str()).unwrap_or("");
                p == jelly_path || p.ends_with(&format!("/{filename}"))
            }) {
                return Some(hit);
            }
        }
        None
    }

    async fn get_items(&self, query: &[(&str, &str)]) -> Option<Vec<serde_json::Value>> {
        let r = self
            .http
            .get(format!("{}/Items", self.base))
            .query(query)
            .header("X-Emby-Token", &self.key)
            .timeout(std::time::Duration::from_secs(5))
            .send()
            .await
            .ok()?;
        let v: serde_json::Value = r.json().await.ok()?;
        Some(
            v.get("Items")
                .and_then(|i| i.as_array())
                .cloned()
                .unwrap_or_default(),
        )
    }

    /// Throttled full-library scan (at most one per 300 s).
    async fn maybe_scan(&self) {
        const INTERVAL: u64 = 300;
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        if now.saturating_sub(self.last_scan.load(Ordering::Relaxed)) < INTERVAL {
            tracing::debug!("jellyfin: library scan skipped (throttled)");
            return;
        }
        // Claim the slot before the POST so concurrent refreshes don't stack.
        self.last_scan.store(now, Ordering::Relaxed);
        let r = self
            .http
            .post(format!("{}/Library/Refresh", self.base))
            .header("X-Emby-Token", &self.key)
            .timeout(std::time::Duration::from_secs(10))
            .send()
            .await;
        match r {
            Ok(resp) => tracing::info!(
                code = resp.status().as_u16(),
                "jellyfin: library scan triggered"
            ),
            Err(e) => tracing::warn!(error = %e, "jellyfin: library scan failed"),
        }
    }
}
