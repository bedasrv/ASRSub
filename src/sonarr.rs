//! Sonarr client: episode lookup + series titles.
//!
//! One shared `reqwest::Client` (pooled connections, rustls) serves the whole
//! daemon; every call has an explicit timeout so a wedged *arr never stalls
//! the library sweep.

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct EpisodeFile {
    #[serde(default)]
    pub path: Option<String>,
}

/// Subset of the Sonarr episode payload the pipeline actually consumes.
/// Deliberately narrow: `id`/`monitored`/`hasFile` are not read anywhere
/// (candidate selection comes from Bazarr `wanted`, not Sonarr flags).
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Episode {
    #[serde(rename = "seriesId", default)]
    pub series_id: Option<i64>,
    #[serde(rename = "seasonNumber", default)]
    pub season_number: Option<i64>,
    #[serde(rename = "episodeNumber", default)]
    pub episode_number: Option<i64>,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(rename = "episodeFile", default)]
    pub episode_file: Option<EpisodeFile>,
}

#[derive(Clone)]
pub struct Sonarr {
    base: String,
    key: String,
    http: reqwest::Client,
}

/// Per-series identity the pass consumes: the title (ladder/Jimaku) plus
/// the original language, best effort (source-track choice). A missing
/// `originalLanguage` degrades to `None` — it never fails a pass.
#[derive(Debug, Clone, Default)]
pub struct SeriesInfo {
    pub title: String,
    pub original_language: Option<String>,
}

impl Sonarr {
    pub fn new(base: &str, key: &str, http: reqwest::Client) -> Self {
        Self {
            base: base.trim_end_matches('/').to_string(),
            key: key.to_string(),
            http,
        }
    }

    pub async fn episode(&self, id: i64) -> Result<Episode> {
        let r = self
            .http
            .get(format!("{}/episode/{id}", self.base))
            .header("X-Api-Key", &self.key)
            .timeout(std::time::Duration::from_secs(60))
            .send()
            .await
            .with_context(|| format!("sonarr GET /episode/{id}"))?;
        if r.status() == reqwest::StatusCode::NOT_FOUND {
            anyhow::bail!("sonarr 404 for episode {id}");
        }
        Ok(r.error_for_status()?.json().await?)
    }

    /// `/series` listing: `id -> SeriesInfo` (title + original language).
    /// Best effort like before — a failed call or a missing field degrades,
    /// it never fails the pass.
    pub async fn series_titles(&self) -> HashMap<i64, SeriesInfo> {
        let Ok(r) = self
            .http
            .get(format!("{}/series", self.base))
            .header("X-Api-Key", &self.key)
            .timeout(std::time::Duration::from_secs(30))
            .send()
            .await
        else {
            return HashMap::new();
        };
        let Ok(list) = r.json::<Vec<serde_json::Value>>().await else {
            return HashMap::new();
        };
        parse_series(list)
    }
}

/// Parse a `/series` listing into `id -> SeriesInfo`. Sonarr returns
/// `originalLanguage: {id, name}`; only the name is a language identity (the
/// numeric id is Sonarr-internal), and it normalizes to a code later. A
/// series without the field still lands with `None`.
fn parse_series(list: Vec<serde_json::Value>) -> HashMap<i64, SeriesInfo> {
    list.into_iter()
        .filter_map(|s| {
            let id = s.get("id")?.as_i64()?;
            let title = s
                .get("title")
                .and_then(|t| t.as_str())
                .unwrap_or("")
                .to_string();
            let original_language = s
                .get("originalLanguage")
                .and_then(|l| l.get("name"))
                .and_then(|n| n.as_str())
                .map(|n| n.trim().to_string())
                .filter(|n| !n.is_empty());
            Some((
                id,
                SeriesInfo {
                    title,
                    original_language,
                },
            ))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_series_carries_title_and_original_language() {
        let list = vec![
            serde_json::json!({
                "id": 11, "title": "TestShow",
                "originalLanguage": {"id": 3, "name": "Japanese"}
            }),
            serde_json::json!({"id": 12, "title": "NoLang"}),
            serde_json::json!({"id": 13, "title": "BlankLang", "originalLanguage": {"name": "  "}}),
            // No id: dropped (nothing can reference it).
            serde_json::json!({"title": "no id"}),
        ];
        let m = parse_series(list);
        assert_eq!(m.len(), 3);
        assert_eq!(m[&11].title, "TestShow");
        assert_eq!(m[&11].original_language.as_deref(), Some("Japanese"));
        assert_eq!(m[&12].original_language, None);
        assert_eq!(m[&13].original_language, None);
    }
}
