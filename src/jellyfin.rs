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

    /// Current Jellyfin authorization scheme. Jellyfin 12 rejects the
    /// deprecated `X-Emby-Token` header with HTTP 401; the API key must be
    /// sent as `Authorization: MediaBrowser Token="<API_KEY>"`.
    fn auth_value(&self) -> String {
        format!(r#"MediaBrowser Token="{}""#, self.key)
    }

    /// Path-based item identity: exact match or same filename (robust
    /// against Sonarr-vs-Jellyfin title drift). Shared by all three lookups.
    fn path_matches(p: &str, jelly_path: &str, filename: &str) -> bool {
        p == jelly_path || p.ends_with(&format!("/{filename}"))
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
            .header("Authorization", self.auth_value())
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
            .header("Authorization", self.auth_value())
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
                Self::path_matches(p, jelly_path, filename)
            })
            .cloned()
    }

    /// Movie lookup by filename stem, gated on path match.
    async fn find_movie(&self, filename: &str, jelly_path: &str) -> Option<serde_json::Value> {
        let stem = crate::lang::stem_of(filename);
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
            Self::path_matches(p, jelly_path, filename)
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
        let stem = crate::lang::stem_of(filename);
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
                Self::path_matches(p, jelly_path, filename)
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
            .header("Authorization", self.auth_value())
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
            .header("Authorization", self.auth_value())
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    const PLACEHOLDER: &str = "TEST-PLACEHOLDER-KEY";

    fn client() -> Jellyfin {
        Jellyfin::with_root(
            "http://127.0.0.1:8096",
            PLACEHOLDER,
            "/media",
            reqwest::Client::new(),
        )
    }

    #[test]
    fn auth_value_uses_mediabrowser_scheme() {
        // Exact outgoing value (placeholder key only, never a real secret).
        assert_eq!(
            client().auth_value(),
            format!(r#"MediaBrowser Token="{PLACEHOLDER}""#)
        );
    }

    #[test]
    fn auth_value_trims_surrounding_whitespace() {
        let j = Jellyfin::with_root(
            "http://127.0.0.1:8096/",
            "  TEST-PLACEHOLDER-KEY  ",
            "/media/",
            reqwest::Client::new(),
        );
        assert_eq!(
            j.auth_value(),
            r#"MediaBrowser Token="TEST-PLACEHOLDER-KEY""#
        );
    }

    fn header_snapshot(headers: &axum::http::HeaderMap) -> (String, bool) {
        let auth = headers
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_string();
        let deprecated = headers.contains_key("x-emby-token");
        (auth, deprecated)
    }

    async fn serve(router: axum::Router) -> String {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move {
            let _ = axum::serve(listener, router).await;
        });
        base
    }

    #[tokio::test]
    async fn get_items_sends_mediabrowser_authorization() {
        let seen: Arc<Mutex<Vec<(String, bool)>>> = Default::default();
        let seen_clone = seen.clone();
        let router = axum::Router::new().route(
            "/Items",
            axum::routing::get(move |headers: axum::http::HeaderMap| async move {
                seen_clone.lock().unwrap().push(header_snapshot(&headers));
                axum::Json(serde_json::json!({"Items": []}))
            }),
        );
        let base = serve(router).await;
        let j = Jellyfin::with_root(&base, PLACEHOLDER, "/media", reqwest::Client::new());
        let items = j
            .get_items(&[("Recursive", "true")])
            .await
            .expect("stub responds");
        assert!(items.is_empty());
        let got = seen.lock().unwrap();
        assert_eq!(got.len(), 1, "expected one GET /Items call");
        assert_eq!(
            got[0].0,
            format!(r#"MediaBrowser Token="{PLACEHOLDER}""#),
            "Authorization header must use MediaBrowser scheme"
        );
        assert!(!got[0].1, "deprecated X-Emby-Token header must not be sent");
    }

    #[tokio::test]
    async fn refresh_flow_sends_mediabrowser_auth_on_all_calls() {
        // Records (endpoint, Authorization value, deprecated header present).
        let seen: Arc<Mutex<Vec<(String, String, bool)>>> = Default::default();
        let jelly_episode_path = "/media/Shows/TestShow/Season 1/Ep.mkv".to_string();

        let s_items = seen.clone();
        let items_router = move |headers: axum::http::HeaderMap| {
            let s_items = s_items.clone();
            async move {
                let (auth, deprecated) = header_snapshot(&headers);
                s_items
                    .lock()
                    .unwrap()
                    .push(("GET /Items".to_string(), auth, deprecated));
                axum::Json(serde_json::json!({"Items": [
                    {"Id": "SERIES1", "Path": "/media/Shows/TestShow"},
                ]}))
            }
        };

        let s_ep = seen.clone();
        let episodes_router = move |headers: axum::http::HeaderMap| {
            let s_ep = s_ep.clone();
            let jelly_episode_path = jelly_episode_path.clone();
            async move {
                let (auth, deprecated) = header_snapshot(&headers);
                s_ep.lock().unwrap().push((
                    "GET /Shows/SERIES1/Episodes".to_string(),
                    auth,
                    deprecated,
                ));
                axum::Json(serde_json::json!({"Items": [
                    {"Id": "EP1", "Path": jelly_episode_path},
                ]}))
            }
        };

        let s_refresh = seen.clone();
        let refresh_router = move |headers: axum::http::HeaderMap| {
            let s_refresh = s_refresh.clone();
            async move {
                let (auth, deprecated) = header_snapshot(&headers);
                s_refresh.lock().unwrap().push((
                    "POST /Items/EP1/Refresh".to_string(),
                    auth,
                    deprecated,
                ));
                axum::http::StatusCode::NO_CONTENT
            }
        };

        let s_scan = seen.clone();
        let scan_router = move |headers: axum::http::HeaderMap| {
            let s_scan = s_scan.clone();
            async move {
                let (auth, deprecated) = header_snapshot(&headers);
                s_scan.lock().unwrap().push((
                    "POST /Library/Refresh".to_string(),
                    auth,
                    deprecated,
                ));
                axum::http::StatusCode::NO_CONTENT
            }
        };

        let router = axum::Router::new()
            .route("/Items", axum::routing::get(items_router))
            .route(
                "/Shows/SERIES1/Episodes",
                axum::routing::get(episodes_router),
            )
            .route("/Items/EP1/Refresh", axum::routing::post(refresh_router))
            .route("/Library/Refresh", axum::routing::post(scan_router));
        let base = serve(router).await;
        let j = Jellyfin::with_root(&base, PLACEHOLDER, "/media", reqwest::Client::new());

        // NAS-local path maps to the stub episode path via media_root.
        let media_path = format!(
            "{}{}",
            crate::config::NAS_MEDIA_PREFIX,
            "/Shows/TestShow/Season 1/Ep.mkv"
        );
        j.refresh_blocking(&media_path, "TestShow", "Episode").await;

        let expected = format!(r#"MediaBrowser Token="{PLACEHOLDER}""#);
        let got = seen.lock().unwrap().clone();
        for endpoint in [
            "GET /Items",
            "GET /Shows/SERIES1/Episodes",
            "POST /Items/EP1/Refresh",
            "POST /Library/Refresh",
        ] {
            let hit = got
                .iter()
                .find(|(p, _, _)| p == endpoint)
                .unwrap_or_else(|| panic!("expected {endpoint} call, got {got:?}"));
            assert_eq!(
                hit.1, expected,
                "{endpoint} must send MediaBrowser Authorization"
            );
            assert!(!hit.2, "{endpoint} must not send deprecated X-Emby-Token");
        }
    }
}
