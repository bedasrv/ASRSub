//! Bazarr client: wanted list, movies, manual subtitle upload.
//!
//! Upload routes are verified against live swagger: series use
//! `POST /api/episodes/subtitles?seriesid=&episodeid=&language=&forced=&hi=`
//! with multipart `file`; movies use `POST /api/movies/subtitles?radarrid=`.

use anyhow::Result;

#[derive(Debug, Clone)]
pub struct WantedItem {
    pub episode_id: i64,
    pub series_id: Option<i64>,
    pub series_title: Option<String>,
    pub missing: Vec<String>,
    pub raw: serde_json::Value,
}

#[derive(Clone)]
pub struct Bazarr {
    base: String,
    key: String,
    base2: Option<String>,
    key2: String,
    http: reqwest::Client,
}

impl Bazarr {
    pub fn new(
        base: &str,
        key: &str,
        base2: Option<String>,
        key2: &str,
        http: reqwest::Client,
    ) -> Self {
        Self {
            base: base.trim_end_matches('/').to_string(),
            key: key.to_string(),
            base2: base2
                .map(|s| s.trim_end_matches('/').to_string())
                .filter(|s| !s.is_empty()),
            key2: key2.to_string(),
            http,
        }
    }

    fn missing_of(v: &serde_json::Value) -> Vec<String> {
        v.get("missing_subtitles")
            .and_then(|m| m.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|m| m.get("code2")?.as_str().map(crate::lang::normalize_lang))
                    .collect()
            })
            .unwrap_or_default()
    }

    pub async fn wanted(&self) -> Result<Vec<WantedItem>> {
        if self.base.is_empty() {
            // Unconfigured: no spurious relative-URL requests (and no
            // warn-spam) on every pass.
            return Ok(Vec::new());
        }
        let fetch = |base: &str, key: &str| {
            let (http, base, key) = (self.http.clone(), base.to_string(), key.to_string());
            async move {
                let r = http
                    .get(format!("{base}/episodes/wanted"))
                    .query(&[("start", "0"), ("length", "500")])
                    .header("X-API-KEY", key)
                    .timeout(std::time::Duration::from_secs(60))
                    .send()
                    .await?;
                r.error_for_status()?.json::<serde_json::Value>().await
            }
        };
        // Primary + secondary profiles fetch concurrently (merge below is
        // pure). On primary failure the secondary result is discarded with
        // the same Err as before — one wasted LAN call on a path that was
        // already failing.
        let primary_fut = fetch(&self.base, &self.key);
        let secondary_fut = async {
            match self.base2.clone() {
                Some(b2) => fetch(&b2, &self.key2).await.ok(),
                None => None,
            }
        };
        let (primary, extra): (
            Result<serde_json::Value, reqwest::Error>,
            Option<serde_json::Value>,
        ) = tokio::join!(primary_fut, secondary_fut);
        let primary = primary?;
        // Merge secondary profile (id/en) missing langs by episode id.
        let mut by_id: std::collections::HashMap<i64, serde_json::Value> = Default::default();
        for it in primary
            .get("data")
            .and_then(|d| d.as_array())
            .cloned()
            .unwrap_or_default()
        {
            if let Some(eid) = it.get("sonarrEpisodeId").and_then(|v| v.as_i64()) {
                by_id.insert(eid, it);
            }
        }
        if let Some(extra) = extra {
            for it in extra
                .get("data")
                .and_then(|d| d.as_array())
                .cloned()
                .unwrap_or_default()
            {
                let Some(eid) = it.get("sonarrEpisodeId").and_then(|v| v.as_i64()) else {
                    continue;
                };
                match by_id.get_mut(&eid) {
                    None => {
                        by_id.insert(eid, it);
                    }
                    Some(prev) => {
                        let mut have: std::collections::HashSet<String> =
                            Self::missing_of(prev).into_iter().collect();
                        let mut merged = prev
                            .get("missing_subtitles")
                            .and_then(|m| m.as_array())
                            .cloned()
                            .unwrap_or_default();
                        for m in it
                            .get("missing_subtitles")
                            .and_then(|m| m.as_array())
                            .cloned()
                            .unwrap_or_default()
                        {
                            if let Some(c) = m.get("code2").and_then(|v| v.as_str()) {
                                let c = crate::lang::normalize_lang(c);
                                if have.insert(c.clone()) {
                                    let mut m = m.clone();
                                    m["code2"] = serde_json::Value::String(c);
                                    merged.push(m);
                                }
                            }
                        }
                        prev["missing_subtitles"] = serde_json::Value::Array(merged);
                    }
                }
            }
        }
        Ok(by_id
            .into_iter()
            .map(|(eid, v)| WantedItem {
                episode_id: eid,
                series_id: v.get("seriesId").and_then(|x| x.as_i64()),
                series_title: v
                    .get("seriesTitle")
                    .and_then(|x| x.as_str())
                    .map(str::to_string),
                missing: Self::missing_of(&v),
                raw: v,
            })
            .collect())
    }

    pub async fn movies(&self) -> Result<Vec<serde_json::Value>> {
        if self.base.is_empty() {
            return Ok(Vec::new());
        }
        let r = self
            .http
            .get(format!("{}/movies", self.base))
            .query(&[("start", "0"), ("length", "500")])
            .header("X-API-KEY", &self.key)
            .timeout(std::time::Duration::from_secs(60))
            .send()
            .await?
            .error_for_status()?;
        let v: serde_json::Value = r.json().await?;
        Ok(v.get("data")
            .and_then(|d| d.as_array())
            .cloned()
            .unwrap_or_default())
    }

    fn endpoint_for(&self, lang: &str) -> (&str, &str) {
        if crate::lang::normalize_lang(lang) == "ja" {
            return (&self.base, &self.key);
        }
        if let Some(b2) = self.base2.as_deref() {
            return (b2, &self.key2);
        }
        (&self.base, &self.key)
    }

    /// Shared subtitle POST: multipart `sub.srt`, `forced=false&hi=true`.
    /// `what` is `"episode"`/`"movie"` (log labels only).
    async fn post_subtitles(
        &self,
        url: &str,
        key: &str,
        query: &[(&str, String)],
        srt: Vec<u8>,
        what: &str,
    ) -> Result<Option<u16>> {
        let form = reqwest::multipart::Form::new().part(
            "file",
            reqwest::multipart::Part::bytes(srt)
                .file_name("sub.srt")
                .mime_str("application/x-subrip")?,
        );
        let r = self
            .http
            .post(url)
            .query(query)
            .header("X-API-KEY", key)
            .multipart(form)
            .timeout(std::time::Duration::from_secs(120))
            .send()
            .await;
        match r {
            Ok(resp) => {
                let code = resp.status().as_u16();
                if code != 204 {
                    tracing::warn!(code, "bazarr {what} upload non-204");
                }
                Ok(Some(code))
            }
            Err(e) => {
                tracing::warn!(error = %crate::config::mask_for_log(&e.to_string()), "bazarr {what} upload error");
                Ok(None)
            }
        }
    }

    /// Returns the HTTP status on success-path; retries 3x with backoff.
    /// A `204` means Bazarr accepted the bytes we just wrote to the
    /// canonical sidecar — no post-upload read-back: the sidecar on disk
    /// is authoritative (written before the upload), and staleness is
    /// caught at the next `discover` via registry-target verification,
    /// not here.
    /// Single upload attempt (no retry loop): the caller
    /// (`pipeline::install_and_upload`) retries with backoff OUTSIDE the
    /// upload semaphore, so a dead Bazarr never wedges the pool under a
    /// held permit across 5s+10s sleeps. Returns the HTTP status, or None
    /// on transport error.
    pub async fn upload_episode(
        &self,
        series_id: i64,
        episode_id: i64,
        lang: &str,
        srt: Vec<u8>,
    ) -> Result<Option<u16>> {
        let lang = crate::lang::normalize_lang(lang);
        let (base, key) = self.endpoint_for(&lang);
        if base.is_empty() {
            return Ok(None);
        }
        let url = format!("{base}/episodes/subtitles");
        self.post_subtitles(
            &url,
            key,
            &[
                ("seriesid", series_id.to_string()),
                ("episodeid", episode_id.to_string()),
                ("language", lang.clone()),
                ("forced", "false".to_string()),
                ("hi", "true".to_string()),
            ],
            srt,
            "episode",
        )
        .await
    }

    /// Single upload attempt (no retry loop — see `upload_episode`).
    pub async fn upload_movie(
        &self,
        radarr_id: i64,
        lang: &str,
        srt: Vec<u8>,
    ) -> Result<Option<u16>> {
        let lang = crate::lang::normalize_lang(lang);
        let (base, key) = self.endpoint_for(&lang);
        if base.is_empty() {
            return Ok(None);
        }
        let url = format!("{base}/movies/subtitles");
        self.post_subtitles(
            &url,
            key,
            &[
                ("radarrid", radarr_id.to_string()),
                ("language", lang.clone()),
                ("forced", "false".to_string()),
                ("hi", "true".to_string()),
            ],
            srt,
            "movie",
        )
        .await
    }

    /// Best-effort: nudge Bazarr instances to run their "Search for Missing
    /// Subtitles" task now, so episodes whose files were just deleted
    /// re-enter `wanted` immediately instead of waiting for the next
    /// scheduled scan. Never fails the caller (logs only).
    pub async fn wanted_refill(&self, kind: &str) {
        let job = match kind {
            "movie" => "wanted_search_missing_subtitles_movies",
            _ => "wanted_search_missing_subtitles_series",
        };
        let mut targets = vec![(self.base.clone(), self.key.clone())];
        if let Some(b2) = self.base2.clone() {
            targets.push((b2, self.key2.clone()));
        }
        // Both instances concurrently; logging per target is unchanged.
        let mut jobs = Vec::with_capacity(targets.len());
        for (base, key) in targets {
            let http = self.http.clone();
            jobs.push(async move {
                http.post(format!("{base}/system/tasks"))
                    .query(&[("taskid", job)])
                    .header("X-API-KEY", key)
                    .timeout(std::time::Duration::from_secs(30))
                    .send()
                    .await
            });
        }
        for r in futures::future::join_all(jobs).await {
            match r {
                Ok(resp) => tracing::info!(
                    task = job,
                    code = resp.status().as_u16(),
                    "bazarr wanted refill"
                ),
                Err(e) => tracing::warn!(
                    task = job,
                    error = %crate::config::mask_for_log(&e.to_string()),
                    "bazarr wanted refill failed"
                ),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_aliases_normalize_before_ordering() {
        // Legacy parity: Bazarr `code2` aliases (jpn/jp/ind/enm/eng) must
        // collapse to canonical codes here, so discover ordering and the
        // done-set comparison never see raw aliases.
        let v = serde_json::json!({"missing_subtitles": [
            {"code2": "jpn"}, {"code2": "enm"}, {"code2": "id"}, {"code2": "eng"},
        ]});
        assert_eq!(Bazarr::missing_of(&v), vec!["ja", "en", "id", "en"]);
        assert!(Bazarr::missing_of(&serde_json::json!({})).is_empty());
    }
}
