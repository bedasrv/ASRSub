//! Direct Jimaku REST client (raw-key auth, no `Bearer` prefix).
//!
//! `GET /api/entries/search?anilist_id=` returns a top-level JSON list;
//! `GET /api/entries/{id}/files?episode=` filters server-side; file URLs
//! download with the same header. No retries here — callers own backoff.
//! Rate limit is 25 req/min; consecutive calls are paced 500 ms apart.

use anyhow::{Context, Result};

pub const BASE_URL: &str = "https://jimaku.cc/api";

#[derive(Clone)]
pub struct Jimaku {
    key: String,
    base: String,
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
        self.pace().await;
        let r = self
            .auth(self.http.get(format!("{}/entries/search", self.base)))
            .query(&[("anilist_id", anilist_id)])
            .timeout(self.api_timeout)
            .send()
            .await
            .context("jimaku search")?;
        if r.status().as_u16() == 429 {
            anyhow::bail!("jimaku 429 rate limited");
        }
        let v: serde_json::Value = r.error_for_status()?.json().await?;
        Ok(as_list(&v))
    }

    pub async fn list_files(
        &self,
        entry_id: i64,
        episode: Option<i64>,
    ) -> Result<Vec<serde_json::Value>> {
        self.pace().await;
        let mut b = self.auth(
            self.http
                .get(format!("{}/entries/{entry_id}/files", self.base)),
        );
        if let Some(ep) = episode {
            b = b.query(&[("episode", ep)]);
        }
        let r = b.timeout(self.api_timeout).send().await?;
        if r.status().as_u16() == 429 {
            anyhow::bail!("jimaku 429 rate limited");
        }
        let v: serde_json::Value = r.error_for_status()?.json().await?;
        Ok(as_list(&v))
    }

    /// Stream-download to `dest` via `<dest>.part` + atomic rename.
    /// Fixed 120 s deadline (subtitle archives, not API calls — outside
    /// `JIMAKU_TIMEOUT` by design).
    pub async fn download(&self, url: &str, dest: &std::path::Path) -> Result<()> {
        self.pace().await;
        if let Some(p) = dest.parent() {
            if !p.as_os_str().is_empty() {
                tokio::fs::create_dir_all(p).await?;
            }
        }
        let tmp = dest.with_extension("part");
        let mut r = self
            .http
            .get(url)
            .header("Authorization", &self.key)
            .timeout(std::time::Duration::from_secs(120))
            .send()
            .await?
            .error_for_status()?;
        let mut fh = tokio::fs::File::create(&tmp).await?;
        use tokio::io::AsyncWriteExt;
        while let Some(chunk) = r.chunk().await? {
            fh.write_all(&chunk).await?;
        }
        fh.sync_data().await?;
        drop(fh);
        tokio::fs::rename(&tmp, dest).await?;
        Ok(())
    }
}

fn as_list(v: &serde_json::Value) -> Vec<serde_json::Value> {
    if let Some(arr) = v.as_array() {
        return arr.iter().filter(|x| x.is_object()).cloned().collect();
    }
    if let Some(arr) = v.get("entries").and_then(|e| e.as_array()) {
        return arr.iter().filter(|x| x.is_object()).cloned().collect();
    }
    Vec::new()
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
            .post("https://graphql.anilist.co")
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
