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
        let primary: serde_json::Value = fetch(&self.base, &self.key).await?;
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
        if let (Some(b2),) = (self.base2.clone(),) {
            if let Ok(extra) = fetch(&b2, &self.key2).await {
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

    /// Returns the HTTP status on success-path; retries 3x with backoff.
    /// A `204` means Bazarr queued the job; the caller then verifies the
    /// on-disk sidecar (Bazarr's async job can crash and never land it).
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
        let form = reqwest::multipart::Form::new().part(
            "file",
            reqwest::multipart::Part::bytes(srt)
                .file_name("sub.srt")
                .mime_str("application/x-subrip")?,
        );
        let r = self
            .http
            .post(&url)
            .query(&[
                ("seriesid", series_id.to_string()),
                ("episodeid", episode_id.to_string()),
                ("language", lang.clone()),
                ("forced", "false".to_string()),
                ("hi", "true".to_string()),
            ])
            .header("X-API-KEY", key)
            .multipart(form)
            .timeout(std::time::Duration::from_secs(120))
            .send()
            .await;
        match r {
            Ok(resp) => {
                let code = resp.status().as_u16();
                if code != 204 {
                    tracing::warn!(code, "bazarr episode upload non-204");
                }
                Ok(Some(code))
            }
            Err(e) => {
                tracing::warn!(error = %e, "bazarr episode upload error");
                Ok(None)
            }
        }
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
        let form = reqwest::multipart::Form::new().part(
            "file",
            reqwest::multipart::Part::bytes(srt)
                .file_name("sub.srt")
                .mime_str("application/x-subrip")?,
        );
        let r = self
            .http
            .post(&url)
            .query(&[
                ("radarrid", radarr_id.to_string()),
                ("language", lang.clone()),
                ("forced", "false".to_string()),
                ("hi", "true".to_string()),
            ])
            .header("X-API-KEY", key)
            .multipart(form)
            .timeout(std::time::Duration::from_secs(120))
            .send()
            .await;
        match r {
            Ok(resp) => {
                let code = resp.status().as_u16();
                if code != 204 {
                    tracing::warn!(code, "bazarr movie upload non-204");
                }
                Ok(Some(code))
            }
            Err(e) => {
                tracing::warn!(error = %e, "bazarr movie upload error");
                Ok(None)
            }
        }
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
        for (base, key) in targets {
            let r = self
                .http
                .post(format!("{base}/system/tasks"))
                .query(&[("taskid", job)])
                .header("X-API-KEY", key)
                .timeout(std::time::Duration::from_secs(30))
                .send()
                .await;
            match r {
                Ok(resp) => tracing::info!(
                    task = job,
                    code = resp.status().as_u16(),
                    "bazarr wanted refill"
                ),
                Err(e) => tracing::warn!(task = job, error = %e, "bazarr wanted refill failed"),
            }
        }
    }
}
