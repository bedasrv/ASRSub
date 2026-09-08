//! Direct Jimaku REST client (raw-key auth, no `Bearer` prefix).
//!
//! `GET /api/entries/search?anilist_id=` returns a top-level JSON list;
//! `GET /api/entries/{id}/files?episode=` filters server-side; file URLs
//! download with the same header. No retries here — callers own backoff.
//! Rate limit is 25 req/min; consecutive calls are paced 500 ms apart.

use anyhow::{Context, Result};

pub const BASE_URL: &str = "https://jimaku.cc/api";
pub const ANILIST_URL: &str = "https://graphql.anilist.co";

#[derive(Clone)]
pub struct Jimaku {
    key: String,
    base: String,
    anilist_url: String,
    http: reqwest::Client,
    pace_ms: u64,
    anilist_cache: std::path::PathBuf,
    /// API deadline for entry search + file listing (`JIMAKU_TIMEOUT`, s).
    api_timeout: std::time::Duration,
    /// AniList GraphQL deadline (`ANILIST_TIMEOUT`, s).
    anilist_timeout: std::time::Duration,
    /// Serializes pacing sleeps across the shared client: without this,
    /// N episode workers sleep in parallel and fire together, defeating
    /// the 25 req/min pacing.
    pace_state: std::sync::Arc<tokio::sync::Mutex<std::time::Instant>>,
    /// Serializes AniList cache read→query→write so concurrent misses for
    /// different series cannot lose updates (last-writer-wins).
    cache_lock: std::sync::Arc<tokio::sync::Mutex<()>>,
}

fn env_secs(key: &str, default: u64) -> u64 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.trim().parse().ok())
        .unwrap_or(default)
}

impl Jimaku {
    pub fn new(key: &str, http: reqwest::Client) -> Self {
        Self {
            key: key.trim().to_string(),
            base: std::env::var("JIMAKU_BASE_URL").unwrap_or_else(|_| BASE_URL.to_string()),
            anilist_url: std::env::var("ANILIST_BASE_URL")
                .unwrap_or_else(|_| ANILIST_URL.to_string()),
            http,
            pace_ms: std::env::var("JIMAKU_CALL_SLEEP_MS")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(500),
            anilist_cache: std::env::var("ANILIST_CACHE")
                .map(std::path::PathBuf::from)
                .unwrap_or_else(|_| {
                    std::env::var("HOME")
                        .map(|h| {
                            std::path::PathBuf::from(h)
                                .join(".config")
                                .join("asr-pipeline")
                                .join("anilist_cache.json")
                        })
                        .unwrap_or_else(|_| std::path::PathBuf::from("anilist_cache.json"))
                }),
            pace_state: std::sync::Arc::new(tokio::sync::Mutex::new(
                // Backdate by one interval so the first call fires immediately
                // instead of sleeping a full pacing period for no reason.
                std::time::Instant::now()
                    .checked_sub(std::time::Duration::from_millis(
                        std::env::var("JIMAKU_CALL_SLEEP_MS")
                            .ok()
                            .and_then(|v| v.parse().ok())
                            .unwrap_or(500),
                    ))
                    .unwrap_or_else(std::time::Instant::now),
            )),
            cache_lock: std::sync::Arc::new(tokio::sync::Mutex::new(())),
            api_timeout: std::time::Duration::from_secs(env_secs("JIMAKU_TIMEOUT", 30)),
            anilist_timeout: std::time::Duration::from_secs(env_secs("ANILIST_TIMEOUT", 30)),
        }
    }

    pub fn with_cache(mut self, path: std::path::PathBuf) -> Self {
        self.anilist_cache = path;
        self
    }

    /// Test seams (also useful for mirrors): override the API base, the
    /// AniList endpoint, and the pacing interval without env vars.
    #[cfg(test)]
    pub fn with_base_url(mut self, base: String) -> Self {
        self.base = base;
        self
    }

    #[cfg(test)]
    pub fn with_anilist_url(mut self, url: String) -> Self {
        self.anilist_url = url;
        self
    }

    #[cfg(test)]
    pub fn with_pace_ms(mut self, ms: u64) -> Self {
        self.pace_ms = ms;
        self
    }

    pub fn enabled(&self) -> bool {
        !self.key.is_empty()
    }

    fn auth(&self, b: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        b.header("Authorization", &self.key)
            .header("Accept", "application/json")
    }

    /// Pacing gate: callers sleep the remaining interval while holding the
    /// shared lock, so consecutive calls from any worker stay spaced.
    async fn pace(&self) {
        if self.pace_ms == 0 {
            return;
        }
        let mut last = self.pace_state.lock().await;
        let wait = *last + std::time::Duration::from_millis(self.pace_ms);
        let now = std::time::Instant::now();
        if wait > now {
            tokio::time::sleep(wait - now).await;
        }
        *last = std::time::Instant::now();
    }

    pub async fn search_by_anilist(&self, anilist_id: i64) -> Result<Vec<serde_json::Value>> {
        let b = self
            .auth(self.http.get(format!("{}/entries/search", self.base)))
            .query(&[("anilist_id", anilist_id)]);
        self.get_json(b, "jimaku search").await
    }

    pub async fn list_files(
        &self,
        entry_id: i64,
        episode: Option<i64>,
    ) -> Result<Vec<serde_json::Value>> {
        let mut b = self.auth(
            self.http
                .get(format!("{}/entries/{entry_id}/files", self.base)),
        );
        if let Some(ep) = episode {
            b = b.query(&[("episode", ep)]);
        }
        self.get_json(b, "jimaku files").await
    }

    /// GET + JSON decode with dedicated 429 handling (legacy parity: the
    /// reset delay rides in the error message, and unexpected shapes are
    /// errors, not silent empties — callers log and fall through either
    /// way, but a changed API shape must stay visible).
    async fn get_json(
        &self,
        b: reqwest::RequestBuilder,
        what: &str,
    ) -> Result<Vec<serde_json::Value>> {
        self.pace().await;
        let r = b
            .timeout(self.api_timeout)
            .send()
            .await
            .with_context(|| format!("{what}: request failed"))?;
        let status = r.status();
        if status.as_u16() == 429 {
            let reset_after = r
                .headers()
                .get("x-ratelimit-reset-after")
                .or_else(|| r.headers().get("retry-after"))
                .and_then(|v| v.to_str().ok())
                .unwrap_or("")
                .to_string();
            anyhow::bail!("{what}: HTTP 429 rate limited reset_after={reset_after}");
        }
        let body = r
            .bytes()
            .await
            .with_context(|| format!("{what}: read body"))?;
        if !status.is_success() {
            let snippet = String::from_utf8_lossy(&body);
            let snippet = snippet.chars().take(200).collect::<String>();
            anyhow::bail!("{what}: HTTP {status}: {snippet}");
        }
        let v: serde_json::Value =
            serde_json::from_slice(&body).with_context(|| format!("{what}: bad json"))?;
        as_list(&v).with_context(|| format!("{what}: unexpected shape"))
    }

    /// Stream-download to `dest` via `<dest>.part` + atomic rename.
    /// Fixed 120 s deadline (subtitle archives, not API calls — outside
    /// `JIMAKU_TIMEOUT` by design). The `.part` file is removed on any
    /// failure so a killed download never leaves a half-written sidecar.
    pub async fn download(&self, url: &str, dest: &std::path::Path) -> Result<()> {
        self.pace().await;
        if let Some(p) = dest.parent() {
            if !p.as_os_str().is_empty() {
                tokio::fs::create_dir_all(p).await?;
            }
        }
        let tmp: std::path::PathBuf = {
            let mut s = dest.as_os_str().to_owned();
            s.push(".part");
            s.into()
        };
        let mut r = self
            .http
            .get(url)
            .header("Authorization", &self.key)
            .timeout(std::time::Duration::from_secs(120))
            .send()
            .await
            .context("jimaku download: request failed")?;
        if r.status().as_u16() == 429 {
            let reset_after = r
                .headers()
                .get("x-ratelimit-reset-after")
                .or_else(|| r.headers().get("retry-after"))
                .and_then(|v| v.to_str().ok())
                .unwrap_or("")
                .to_string();
            anyhow::bail!("jimaku download: HTTP 429 rate limited reset_after={reset_after}");
        }
        if let Err(e) = r.error_for_status_ref() {
            anyhow::bail!("jimaku download: HTTP {e}");
        }
        let mut fh = tokio::fs::File::create(&tmp).await?;
        let stream_result = async {
            use tokio::io::AsyncWriteExt;
            while let Some(chunk) = r.chunk().await? {
                fh.write_all(&chunk).await?;
            }
            fh.sync_data().await?;
            drop(fh);
            tokio::fs::rename(&tmp, dest).await?;
            Ok::<(), anyhow::Error>(())
        }
        .await;
        if stream_result.is_err() {
            let _ = tokio::fs::remove_file(&tmp).await;
        }
        stream_result
    }
}

fn as_list(v: &serde_json::Value) -> Result<Vec<serde_json::Value>> {
    let items = if let Some(arr) = v.as_array() {
        arr
    } else if let Some(arr) = v.get("entries").and_then(|e| e.as_array()) {
        arr
    } else {
        anyhow::bail!("expected a top-level JSON list or {{\"entries\": [...]}}");
    };
    Ok(items.iter().filter(|x| x.is_object()).cloned().collect())
}

/// Entry selection: prefer an entry whose `anilist_id` matches exactly,
/// else the first usable entry. None when empty.
pub fn pick_entry(entries: Vec<serde_json::Value>, anilist_id: i64) -> Option<serde_json::Value> {
    if entries.is_empty() {
        return None;
    }
    entries
        .iter()
        .find(|e| {
            e.get("anilist_id").and_then(|v| match v {
                serde_json::Value::Number(n) => n.as_i64(),
                serde_json::Value::String(s) => s.parse().ok(),
                _ => None,
            }) == Some(anilist_id)
        })
        .or_else(|| entries.first())
        .cloned()
}

/// AniList media id for a series title, `anilist_cache.json`-first (shared
/// with fetch_glossary.py). Lock scope is deliberately narrow: the cache
/// check and the merge-write each hold the lock briefly, but the GraphQL
/// query runs WITHOUT it — one slow AniList response must not stall every
/// ladder worker. A re-check before writing covers the race where a
/// concurrent worker resolved the same title first (its fields win).
/// Never fails loudly — returns None on any miss.
impl Jimaku {
    pub async fn resolve_anilist_id(&self, title: &str) -> Option<i64> {
        let title = title.trim();
        if title.is_empty() {
            return None;
        }
        let key = anilist_cache_key(title);
        if let Some(mid) = self.cached_media_id(&key).await {
            return Some(mid);
        }
        let query_title = title.replace('\u{00d7}', "x");
        let r = self
            .http
            .post(&self.anilist_url)
            .json(&serde_json::json!({
                "query": "query($search: String) { Media(search: $search, type: ANIME) { id title { romaji } } }",
                "variables": {"search": query_title},
            }))
            .header("User-Agent", "asrsub-jimaku/3.0")
            .header("Accept", "application/json")
            .timeout(self.anilist_timeout)
            .send()
            .await
            .ok()?;
        let body: serde_json::Value = r.json().await.ok()?;
        let media = body.get("data")?.get("Media")?.clone();
        let mid = media.get("id")?.as_i64()?;
        if mid <= 0 {
            return None;
        }
        self.merge_media_id(&key, mid, &media).await;
        Some(mid)
    }

    /// Locked cache read: `Some(id)` on hit, `None` on miss.
    async fn cached_media_id(&self, key: &str) -> Option<i64> {
        let _guard = self.cache_lock.lock().await;
        let cache = self.read_cache().await?;
        let mid = cache.get(key)?.get("media_id")?.as_i64()?;
        (mid > 0).then_some(mid)
    }

    async fn read_cache(&self) -> Option<serde_json::Map<String, serde_json::Value>> {
        let text = tokio::fs::read_to_string(&self.anilist_cache).await.ok()?;
        serde_json::from_str::<serde_json::Value>(&text)
            .ok()?
            .as_object()
            .cloned()
    }

    /// Locked merge-write: re-reads first (a concurrent worker may have
    /// inserted the key meanwhile — its entry is preserved), merges media
    /// fields into the existing object, writes atomically. Best-effort.
    async fn merge_media_id(&self, key: &str, mid: i64, media: &serde_json::Value) {
        let _guard = self.cache_lock.lock().await;
        let mut cache = self.read_cache().await.unwrap_or_default();
        let mut merged = cache
            .get(key)
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        merged.insert("media_id".to_string(), serde_json::Value::from(mid));
        // Informational only (ids have no TTL — re-resolving would only burn
        // API budget), but legacy parity: fetch_glossary readers may look.
        merged.insert(
            "fetched_at".to_string(),
            serde_json::Value::String(crate::state::utc_now_iso()),
        );
        merged.insert(
            "title_romaji".to_string(),
            serde_json::Value::String(
                media
                    .get("title")
                    .and_then(|t| t.get("romaji"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
            ),
        );
        cache.insert(key.to_string(), serde_json::Value::Object(merged));
        // Atomic cache write; failure is swallowed (cache is an optimization).
        if let Some(parent) = self.anilist_cache.parent() {
            if !parent.as_os_str().is_empty() {
                let _ = tokio::fs::create_dir_all(parent).await;
            }
        }
        let tmp = self.anilist_cache.with_extension("tmp");
        if let Ok(text) = serde_json::to_string_pretty(&serde_json::Value::Object(cache)) {
            if tokio::fs::write(&tmp, text).await.is_ok() {
                let _ = tokio::fs::rename(&tmp, &self.anilist_cache).await;
            }
        }
    }
}

/// Cache key shared byte-for-byte with fetch_glossary's tolerant
/// normalization: lowercase, whitespace-collapsed, then `×`→`x`,
/// `・`→space, `:`→space, apostrophes stripped.
fn anilist_cache_key(title: &str) -> String {
    let collapsed = title
        .trim()
        .to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    collapsed
        .chars()
        .filter_map(|c| match c {
            '\u{00d7}' => Some('x'),
            '\u{30fb}' | ':' => Some(' '),
            '\'' => None,
            _ => Some(c),
        })
        .collect()
}

/// Best-first file ranking: `.srt` > `.ass`, release-tagged / S01E01 first,
/// fansub groups next, CHS/CHT bilingual sunk to last resort.
pub fn rank_files(files: Vec<serde_json::Value>) -> Vec<serde_json::Value> {
    let mut scored: Vec<(i64, serde_json::Value)> = Vec::new();
    for f in files {
        let name = f
            .get("name")
            .and_then(|n| n.as_str())
            .unwrap_or("")
            .to_string();
        let ext = std::path::Path::new(&name)
            .extension()
            .and_then(|e| e.to_str())
            .unwrap_or("")
            .to_lowercase();
        if !matches!(ext.as_str(), "srt" | "ass" | "ssa") {
            continue;
        }
        let lower = name.to_lowercase();
        let toks: std::collections::HashSet<&str> = lower
            .split(|c: char| !c.is_ascii_alphanumeric())
            .filter(|t| !t.is_empty())
            .collect();
        let mut score: i64 = if ext == "srt" { 3 } else { 1 };
        if toks.contains("varyg") || toks.contains("cr") {
            score += 4;
        }
        if has_ep_tag(&lower) {
            score += 4;
        }
        if ["kitaujisub", "kitauji", "lolihouse", "nanakoraws"]
            .iter()
            .any(|t| toks.contains(t))
        {
            score += 2;
        }
        if ["chs", "cht", "big5"].iter().any(|t| toks.contains(t)) {
            score -= 14;
        }
        scored.push((score, f));
    }
    scored.sort_by_key(|s| std::cmp::Reverse(s.0));
    scored.into_iter().map(|(_, f)| f).collect()
}

/// Strict `S##E##` episode-tag scan (e.g. `S01E07`): no regex crate needed.
/// The old loose scan matched any `e+digit` (`bare E12`), promoting
/// unrelated files; only a full season+episode tag scores.
fn has_ep_tag(lower: &str) -> bool {
    let b = lower.as_bytes();
    if b.len() < 6 {
        return false;
    }
    b.windows(6).any(|w| {
        w[0] == b's'
            && w[1].is_ascii_digit()
            && w[2].is_ascii_digit()
            && w[3] == b'e'
            && w[4].is_ascii_digit()
            && w[5].is_ascii_digit()
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    // ---- pure shape/selection/ranking (ported from test_jimaku_api.py) ----

    #[test]
    fn response_shapes_list_wrapper_and_rejections() {
        let entries = vec![serde_json::json!({"id": 12191, "anilist_id": 190569})];
        assert_eq!(
            as_list(&serde_json::Value::Array(entries.clone())).unwrap(),
            entries
        );
        let wrapped = serde_json::json!({"entries": [{"id": 1}]});
        assert_eq!(
            as_list(&wrapped).unwrap(),
            vec![serde_json::json!({"id": 1})]
        );
        // Unexpected shape is an error, not a silent empty (a changed API
        // shape must stay visible in logs).
        assert!(as_list(&serde_json::json!({"oops": true})).is_err());
        // Non-object items dropped.
        let mixed = serde_json::json!(["junk", {"id": 7}, null]);
        assert_eq!(as_list(&mixed).unwrap(), vec![serde_json::json!({"id": 7})]);
    }

    #[test]
    fn pick_entry_exact_string_first_empty() {
        let entries = vec![
            serde_json::json!({"id": 1, "anilist_id": 111}),
            serde_json::json!({"id": 2, "anilist_id": 190569}),
            serde_json::json!({"id": 3, "anilist_id": 190569}),
        ];
        assert_eq!(pick_entry(entries, 190569).unwrap()["id"], 2);
        // Numeric-string ids coerce.
        let entries = vec![serde_json::json!({"id": 5, "anilist_id": "190569"})];
        assert_eq!(pick_entry(entries, 190569).unwrap()["id"], 5);
        // No match (or no usable id) falls back to the first entry.
        let entries = vec![serde_json::json!({"id": 9}), serde_json::json!({"id": 10})];
        assert_eq!(pick_entry(entries, 42).unwrap()["id"], 9);
        assert!(pick_entry(vec![], 42).is_none());
    }

    fn files(names: &[&str]) -> Vec<serde_json::Value> {
        names
            .iter()
            .map(|n| serde_json::json!({"name": n}))
            .collect()
    }

    fn ranked_names(fi: Vec<serde_json::Value>) -> Vec<String> {
        rank_files(fi)
            .iter()
            .map(|f| f["name"].as_str().unwrap().to_string())
            .collect()
    }

    #[test]
    fn release_tag_srt_wins_over_fansub_and_plain() {
        let names = ranked_names(files(&[
            "[VARYG] Jaadugar - S01E01.srt",
            "Jaadugar.S01E01.1080p.CR.WEB-DL.srt",
            "[KitaujiSub] Jaadugar Ep 1 [1080p].ass",
            "[LoliHouse] Jaadugar - 01 [WebRip 1080p].srt",
            "jaadugar_ep1.srt",
            "[NanakoRaws] Jaadugar - 01.ass",
            "[VARYG] Jaadugar - S01E01.CHS.srt",
            "readme.nfo",
            "pack.zip",
        ]));
        assert_eq!(names[0], "[VARYG] Jaadugar - S01E01.srt");
        assert_eq!(names[1], "Jaadugar.S01E01.1080p.CR.WEB-DL.srt");
    }

    #[test]
    fn plain_s01e01_srt_beats_fansub_ass() {
        let names = ranked_names(files(&["Show S01E01.srt", "[KitaujiSub] Show 01.ass"]));
        assert_eq!(names[0], "Show S01E01.srt");
    }

    #[test]
    fn fansub_files_rank_above_plain() {
        let names = ranked_names(files(&[
            "show_ep1.srt",
            "[LoliHouse] show - 01 [1080p].srt",
        ]));
        assert_eq!(names[0], "[LoliHouse] show - 01 [1080p].srt");
    }

    #[test]
    fn bilingual_demoted_below_jpn_only_but_kept() {
        let names = ranked_names(files(&[
            "[VARYG] Show - S01E01 CHS.srt",
            "show_ep1_jpn_only.srt",
        ]));
        assert_eq!(names.len(), 2);
        assert_eq!(names[1], "[VARYG] Show - S01E01 CHS.srt");
    }

    #[test]
    fn non_subtitle_files_rejected_and_ties_stable() {
        assert!(rank_files(files(&["readme.nfo", "pack.zip"])).is_empty());
        let names = ranked_names(files(&["aaa_ep1.srt", "bbb_ep1.srt"]));
        assert_eq!(names, vec!["aaa_ep1.srt", "bbb_ep1.srt"]);
        assert_eq!(
            ranked_names(files(&["fansub ep01.ssa"]))[0],
            "fansub ep01.ssa"
        );
    }

    #[test]
    fn cache_key_matches_fetch_glossary_normalization() {
        // `:` maps to a space WITHOUT re-collapsing, so the shared key holds
        // a double space; pinned literally to catch normalization drift
        // against the cache fetch_glossary.py writes.
        assert_eq!(
            anilist_cache_key("Jaadugar: A Witch in Mongolia"),
            "jaadugar  a witch in mongolia"
        );
        assert_eq!(
            anilist_cache_key("HUNTER×HUNTER  ・ Test's"),
            "hunterxhunter   tests"
        );
    }

    // ---- HTTP behavior against a local stub (no network) ----

    async fn serve(router: axum::Router) -> String {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move {
            let _ = axum::serve(listener, router).await;
        });
        base
    }

    fn client_for(base: &str) -> Jimaku {
        Jimaku::new("raw-key-no-bearer", reqwest::Client::new())
            .with_base_url(format!("{base}/api"))
            .with_pace_ms(0)
    }

    #[tokio::test]
    async fn search_sends_raw_key_and_anilist_param() {
        let router = axum::Router::new().route(
            "/api/entries/search",
            axum::routing::get(
                |headers: axum::http::HeaderMap, uri: axum::http::Uri| async move {
                    // Echo what the client sent inside the tolerated wrapper.
                    let auth = headers
                        .get("authorization")
                        .and_then(|v| v.to_str().ok())
                        .unwrap_or("")
                        .to_string();
                    let q = uri.query().unwrap_or("").to_string();
                    axum::Json(serde_json::json!({"entries": [
                        {"id": 12191, "echo_auth": auth, "echo_q": q},
                    ]}))
                },
            ),
        );
        let base = serve(router).await;
        let out = client_for(&base).search_by_anilist(190569).await.unwrap();
        assert_eq!(out.len(), 1);
        // Raw key, no Bearer prefix; anilist_id forwarded as the query.
        assert_eq!(out[0]["echo_auth"], "raw-key-no-bearer");
        assert!(out[0]["echo_q"]
            .as_str()
            .unwrap()
            .contains("anilist_id=190569"));
    }

    #[tokio::test]
    async fn list_files_forwards_or_omits_episode() {
        let router = axum::Router::new().route(
            "/api/entries/12191/files",
            axum::routing::get(|uri: axum::http::Uri| async move {
                let q = uri.query().unwrap_or("").to_string();
                axum::Json(vec![
                    serde_json::json!({"url": "u", "name": "a.srt", "q": q}),
                ])
            }),
        );
        let base = serve(router).await;
        let c = client_for(&base);
        let out = c.list_files(12191, Some(1)).await.unwrap();
        assert!(out[0]["q"].as_str().unwrap().contains("episode=1"));
        let out = c.list_files(12191, None).await.unwrap();
        assert_eq!(out[0]["q"], "");
    }

    #[tokio::test]
    async fn rate_limit_and_http_errors_surface() {
        let router = axum::Router::new()
            .route(
                "/api/entries/search",
                axum::routing::get(|| async {
                    (
                        axum::http::StatusCode::TOO_MANY_REQUESTS,
                        [("x-ratelimit-reset-after", "42")],
                        "",
                    )
                }),
            )
            .route(
                "/api/entries/9/files",
                axum::routing::get(|| async {
                    (axum::http::StatusCode::INTERNAL_SERVER_ERROR, "boom")
                }),
            );
        let base = serve(router).await;
        let c = client_for(&base);
        // 429 carries the reset delay in the message (typed JimakuRateLimited
        // folded into anyhow: no caller branches, logs keep the detail).
        let e = format!("{:?}", c.search_by_anilist(2).await.unwrap_err());
        assert!(e.contains("429"), "{e}");
        assert!(e.contains("42"), "{e}");
        // Other statuses carry a body snippet.
        let e = format!("{:?}", c.list_files(9, None).await.unwrap_err());
        assert!(e.contains("500"), "{e}");
        assert!(e.contains("boom"), "{e}");
        // Unreachable host is an error, never a panic or silent empty.
        let c = c.with_base_url("http://127.0.0.1:9/api".to_string());
        assert!(c.search_by_anilist(2).await.is_err());
    }

    #[tokio::test]
    async fn download_streams_and_cleans_part() {
        let seen: std::sync::Arc<std::sync::Mutex<Option<String>>> =
            std::sync::Arc::new(std::sync::Mutex::new(None));
        let router = axum::Router::new()
            .route(
                "/ok/sub.srt",
                axum::routing::get({
                    let seen = seen.clone();
                    move |headers: axum::http::HeaderMap| async move {
                        *seen.lock().unwrap() = headers
                            .get("authorization")
                            .and_then(|v| v.to_str().ok())
                            .map(str::to_string);
                        "1\n00:00:01,000 --> x"
                    }
                }),
            )
            .route(
                "/missing/y.srt",
                axum::routing::get(|| async { axum::http::StatusCode::NOT_FOUND }),
            )
            .route(
                "/limited/z.srt",
                axum::routing::get(|| async {
                    (
                        axum::http::StatusCode::TOO_MANY_REQUESTS,
                        [("x-ratelimit-reset-after", "10")],
                        "",
                    )
                }),
            );
        let base = serve(router).await;
        let c = client_for(&base);
        let dir = tempfile::tempdir().unwrap();

        let dest = dir.path().join("sub.srt");
        c.download(&format!("{base}/ok/sub.srt"), &dest)
            .await
            .unwrap();
        assert_eq!(std::fs::read(&dest).unwrap(), b"1\n00:00:01,000 --> x");
        // `<dest>.part` removed; same raw auth header as the API calls.
        let part = {
            let mut s = dest.as_os_str().to_owned();
            s.push(".part");
            std::path::PathBuf::from(s)
        };
        assert!(!part.exists());
        assert_eq!(seen.lock().unwrap().as_deref(), Some("raw-key-no-bearer"));

        // 404 / 429 leave neither dest nor part behind.
        let dest = dir.path().join("y.srt");
        assert!(c
            .download(&format!("{base}/missing/y.srt"), &dest)
            .await
            .is_err());
        assert!(!dest.exists());
        let dest = dir.path().join("z.srt");
        let e = format!(
            "{:?}",
            c.download(&format!("{base}/limited/z.srt"), &dest)
                .await
                .unwrap_err()
        );
        assert!(e.contains("429"), "{e}");
        assert!(!dest.exists());
    }

    #[tokio::test]
    async fn anilist_cache_hit_skips_http_and_miss_writes_back() {
        let dir = tempfile::tempdir().unwrap();
        let cache = dir.path().join("anilist_cache.json");
        std::fs::write(
            &cache,
            serde_json::json!({"high school dxd": {
                "fetched_at": "2026-08-23T09:48:26",
                "media_id": 11617,
                "title_romaji": "High School DxD",
                "characters": [{"full": "Rias"}],
            }})
            .to_string(),
        )
        .unwrap();
        // Point at a closed port: any HTTP attempt fails, so a hit proves
        // the cache short-circuits (legacy: requests.post patched to raise).
        let c = Jimaku::new("k", reqwest::Client::new())
            .with_pace_ms(0)
            .with_cache(cache.clone())
            .with_anilist_url("http://127.0.0.1:9/graphql".to_string());
        assert_eq!(c.resolve_anilist_id("High School DxD").await, Some(11617));

        // Miss queries once and merges back, preserving existing fields.
        std::fs::write(
            &cache,
            serde_json::json!({"jaadugar  a witch in mongolia": {
                "characters": [{"full": "Fine"}],
            }})
            .to_string(),
        )
        .unwrap();
        let router = axum::Router::new().route(
            "/graphql",
            axum::routing::post(|| async {
                axum::Json(serde_json::json!({"data": {"Media": {
                    "id": 190569,
                    "title": {"romaji": "Jaadugar: A Witch in Mongolia"},
                    "format": "TV",
                }}}))
            }),
        );
        let base = serve(router).await;
        let c = c.with_anilist_url(format!("{base}/graphql"));
        assert_eq!(
            c.resolve_anilist_id("Jaadugar: A Witch in Mongolia").await,
            Some(190569)
        );
        let back: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&cache).unwrap()).unwrap();
        let entry = &back["jaadugar  a witch in mongolia"];
        assert_eq!(entry["media_id"], 190569);
        assert_eq!(entry["characters"], serde_json::json!([{"full": "Fine"}]));
        assert!(entry.get("fetched_at").and_then(|v| v.as_str()).is_some());
        assert_eq!(entry["title_romaji"], "Jaadugar: A Witch in Mongolia");

        // Query failure / no match / empty title → None, never raises.
        let router = axum::Router::new().route(
            "/graphql",
            axum::routing::post(|| async { axum::http::StatusCode::INTERNAL_SERVER_ERROR }),
        );
        let base = serve(router).await;
        let c = c.with_anilist_url(format!("{base}/graphql"));
        assert_eq!(c.resolve_anilist_id("Some Show").await, None);
        let router = axum::Router::new().route(
            "/graphql",
            axum::routing::post(|| async {
                axum::Json(serde_json::json!({"data": {"Media": serde_json::Value::Null}}))
            }),
        );
        let base = serve(router).await;
        let c = c.with_anilist_url(format!("{base}/graphql"));
        assert_eq!(c.resolve_anilist_id("Unknown").await, None);
        assert_eq!(c.resolve_anilist_id("   ").await, None);
    }
}
