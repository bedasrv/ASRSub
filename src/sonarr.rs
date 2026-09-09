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

    pub async fn series_titles(&self) -> HashMap<i64, String> {
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
        list.into_iter()
            .filter_map(|s| {
                Some((
                    s.get("id")?.as_i64()?,
                    s.get("title")?.as_str()?.to_string(),
                ))
            })
            .collect()
    }
}
