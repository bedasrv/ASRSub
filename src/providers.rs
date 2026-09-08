//! Remote provider pool loaded from `asrsub_providers.json`.
//!
//! The JSON file is the single source of truth for remote inference:
//! `llm_translation_models` (OpenAI-compatible `/chat/completions`) and
//! `whisper_stt` (OpenAI-compatible `/audio/transcriptions` multipart).
//! Local Whisper/LLM inference is intentionally absent: this binary never
//! loads model weights, keeping RSS flat when sweeping large libraries.
//!
//! Throughput design:
//! * LLM endpoints are sorted fastest-first (`probe_latency_s`) and raced
//!   through a shared [`ProviderPool`] with per-endpoint semaphores so no
//!   single free-tier key is hammered.
//! * Whisper tries `whisper_stt` first, then `whisper_stt_fallbacks` in
//!   order, with the same per-endpoint semaphores.
//! * Consecutive failures trip a short circuit-breaker (60 s) instead of
//!   burning the pass on a dead endpoint.

use std::path::Path;
use std::sync::atomic::{AtomicU32, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use tokio::sync::Semaphore;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct RequestShape {
    #[serde(default)]
    pub temperature: Option<f64>,
    #[serde(default)]
    pub thinking: Option<serde_json::Value>,
    #[serde(default)]
    pub headers: Vec<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct LlmProvider {
    pub provider: String,
    pub endpoint: String,
    pub base_url: String,
    pub model: String,
    #[serde(default)]
    pub key_env: String,
    #[serde(default)]
    pub api_key: String,
    #[serde(default = "default_latency")]
    pub probe_latency_s: f64,
    #[serde(default)]
    pub thinking_param_accepted: bool,
    #[serde(default)]
    pub request_shape: Option<RequestShape>,
}

fn default_latency() -> f64 {
    30.0
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct WhisperProvider {
    pub provider: String,
    pub endpoint: String,
    pub model: String,
    #[serde(default)]
    pub key_env: String,
    #[serde(default)]
    pub api_key: String,
    #[serde(default)]
    pub rate_usd_per_audio_sec: Option<f64>,
    #[serde(default)]
    pub via_upstream: Option<String>,
    #[serde(default)]
    pub request_shape: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ProvidersFile {
    #[serde(default)]
    pub llm_translation_models: Vec<LlmProvider>,
    #[serde(default)]
    pub whisper_stt: Option<WhisperProvider>,
    /// Optional Whisper failover endpoints, same shape as `whisper_stt`,
    /// attempted in order after the primary (primary first, then these).
    /// Absent in older files: fully backward compatible (fails over to
    /// nothing, exactly like before).
    #[serde(default)]
    pub whisper_stt_fallbacks: Vec<WhisperProvider>,
}

impl ProvidersFile {
    /// Load the providers file. An explicitly configured path that exists is
    /// parsed strictly — read/parse failures are returned with context, never
    /// swallowed. When the configured path is missing, the well-known
    /// fallbacks (CWD, executable dir) are tried before failing.
    pub fn load(path: &Path) -> Result<Self> {
        if path.exists() {
            return Self::load_exact(path);
        }
        let mut tried = vec![path.to_path_buf()];
        if path.is_relative() {
            let mut fallbacks = vec![Path::new("asrsub_providers.json").to_path_buf()];
            if let Ok(exe) = std::env::current_exe() {
                if let Some(dir) = exe.parent() {
                    fallbacks.push(dir.join("asrsub_providers.json"));
                }
            }
            for cand in fallbacks {
                if cand == *path {
                    continue;
                }
                tried.push(cand.clone());
                if cand.exists() {
                    return Self::load_exact(&cand);
                }
            }
        }
        anyhow::bail!(
            "providers file not found (tried {:?}); set PROVIDERS_FILE or --providers-file",
            tried
        )
    }

    fn load_exact(path: &Path) -> Result<Self> {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("read providers file {path:?}"))?;
        let mut v: Self = serde_json::from_str(&text)
            .with_context(|| format!("parse providers file {path:?}"))?;
        v.llm_translation_models
            .sort_by(|a, b| a.probe_latency_s.partial_cmp(&b.probe_latency_s).unwrap());
        Ok(v)
    }

    #[cfg(test)]
    pub fn load_str(text: &str) -> Result<Self> {
        let mut v: Self = serde_json::from_str(text)?;
        v.llm_translation_models
            .sort_by(|a, b| a.probe_latency_s.partial_cmp(&b.probe_latency_s).unwrap());
        Ok(v)
    }
}

impl LlmProvider {
    /// API key: embedded value wins, otherwise `$key_env` at call time so
    /// rotated keys apply without restart.
    pub fn api_key(&self) -> String {
        if !self.api_key.is_empty() {
            return self.api_key.clone();
        }
        if !self.key_env.is_empty() {
            return std::env::var(&self.key_env).unwrap_or_default();
        }
        String::new()
    }
}

impl WhisperProvider {
    pub fn api_key(&self) -> String {
        if !self.api_key.is_empty() {
            return self.api_key.clone();
        }
        if !self.key_env.is_empty() {
            return std::env::var(&self.key_env).unwrap_or_default();
        }
        String::new()
    }
}

struct EndpointHealth {
    semaphore: Arc<Semaphore>,
    failures: AtomicU32,
    banned_until: AtomicU64, // epoch millis
}

impl EndpointHealth {
    fn new(permits: usize) -> Self {
        Self {
            semaphore: Arc::new(Semaphore::new(permits)),
            failures: AtomicU32::new(0),
            banned_until: AtomicU64::new(0),
        }
    }

    fn banned(&self, now_ms: u64) -> bool {
        self.banned_until.load(Ordering::Relaxed) > now_ms
    }

    fn record_success(&self) {
        self.failures.store(0, Ordering::Relaxed);
    }

    fn record_failure(&self, now_ms: u64) {
        let n = self.failures.fetch_add(1, Ordering::Relaxed) + 1;
        if n >= 3 {
            self.banned_until.store(now_ms + 60_000, Ordering::Relaxed);
            self.failures.store(0, Ordering::Relaxed);
        }
    }
}

/// Shared, cloneable pool over the ordered LLM endpoints.
#[derive(Clone)]
pub struct ProviderPool {
    inner: Arc<PoolInner>,
}

struct PoolInner {
    providers: Vec<LlmProvider>,
    health: Vec<EndpointHealth>,
    whisper: Vec<WhisperProvider>,
    whisper_health: Vec<EndpointHealth>,
    http: reqwest::Client,
}

impl ProviderPool {
    pub fn new(file: ProvidersFile, http: reqwest::Client) -> Self {
        let per_endpoint = std::env::var("LLM_PER_ENDPOINT_CONCURRENCY")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(4);
        let health = file
            .llm_translation_models
            .iter()
            .map(|_| EndpointHealth::new(per_endpoint))
            .collect();
        let whisper_permits: usize = std::env::var("WHISPER_CONCURRENCY")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(4);
        // Primary first, then fallbacks (may be empty: fully backward
        // compatible with files carrying only `whisper_stt`).
        let mut whisper = Vec::with_capacity(1 + file.whisper_stt_fallbacks.len());
        whisper.extend(file.whisper_stt);
        whisper.extend(file.whisper_stt_fallbacks);
        let whisper_health = whisper
            .iter()
            .map(|_| EndpointHealth::new(whisper_permits))
            .collect();
        Self {
            inner: Arc::new(PoolInner {
                providers: file.llm_translation_models,
                health,
                whisper,
                whisper_health,
                http,
            }),
        }
    }

    pub fn len(&self) -> usize {
        self.inner.providers.len()
    }

    /// Number of configured Whisper endpoints (primary + fallbacks).
    pub fn whisper_len(&self) -> usize {
        self.inner.whisper.len()
    }

    pub fn http(&self) -> &reqwest::Client {
        &self.inner.http
    }

    fn now_ms() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0)
    }

    /// Fastest-first endpoint order, skipping circuit-broken entries.
    /// When all are broken we return the full list (a broken endpoint may
    /// have recovered) rather than failing the chunk outright.
    pub fn ordered(&self) -> Vec<(usize, LlmProvider)> {
        let now = Self::now_ms();
        let mut v: Vec<(usize, LlmProvider)> = self
            .inner
            .providers
            .iter()
            .enumerate()
            .filter(|(i, _)| !self.inner.health[*i].banned(now))
            .map(|(i, p)| (i, p.clone()))
            .collect();
        if v.is_empty() {
            v = self
                .inner
                .providers
                .iter()
                .enumerate()
                .map(|(i, p)| (i, p.clone()))
                .collect();
        }
        v
    }

    pub async fn acquire(&self, idx: usize) -> tokio::sync::OwnedSemaphorePermit {
        self.inner.health[idx]
            .semaphore
            .clone()
            .acquire_owned()
            .await
            .expect("semaphore closed")
    }

    pub fn record_success(&self, idx: usize) {
        self.inner.health[idx].record_success();
    }

    pub fn record_failure(&self, idx: usize) {
        self.inner.health[idx].record_failure(Self::now_ms());
    }

    pub async fn acquire_whisper(&self, idx: usize) -> tokio::sync::OwnedSemaphorePermit {
        self.inner.whisper_health[idx]
            .semaphore
            .clone()
            .acquire_owned()
            .await
            .expect("semaphore closed")
    }

    /// Whisper endpoints in attempt order (primary first), skipping
    /// circuit-broken entries. Falls back to the full list when all are
    /// broken, mirroring [`Self::ordered`].
    pub fn ordered_whisper(&self) -> Vec<(usize, WhisperProvider)> {
        let now = Self::now_ms();
        let mut v: Vec<(usize, WhisperProvider)> = self
            .inner
            .whisper
            .iter()
            .enumerate()
            .filter(|(i, _)| !self.inner.whisper_health[*i].banned(now))
            .map(|(i, p)| (i, p.clone()))
            .collect();
        if v.is_empty() {
            v = self
                .inner
                .whisper
                .iter()
                .enumerate()
                .map(|(i, p)| (i, p.clone()))
                .collect();
        }
        v
    }

    /// True when Whisper is configured but every endpoint is circuit-broken.
    /// Callers fail fast on this instead of burning timeouts.
    pub fn whisper_banned(&self) -> bool {
        !self.inner.whisper.is_empty()
            && self
                .inner
                .whisper_health
                .iter()
                .all(|h| h.banned(Self::now_ms()))
    }

    pub fn record_whisper_success(&self, idx: usize) {
        self.inner.whisper_health[idx].record_success();
    }

    pub fn record_whisper_failure(&self, idx: usize) {
        self.inner.whisper_health[idx].record_failure(Self::now_ms());
    }

    /// Deadline for one LLM attempt: bounded so a hung free-tier endpoint
    /// cannot stall the whole library sweep.
    pub fn llm_timeout() -> Duration {
        let s: u64 = std::env::var("LLM_TIMEOUT_S")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(300);
        Duration::from_secs(s.clamp(30, 900))
    }

    pub fn whisper_timeout() -> Duration {
        let s: u64 = std::env::var("WHISPER_TIMEOUT_S")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(600);
        Duration::from_secs(s.clamp(60, 1800))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"{
      "llm_translation_models": [
        {"provider":"b","endpoint":"https://b/chat/completions","base_url":"https://b","model":"mb","key_env":"K","api_key":"","probe_latency_s":5.0,"thinking_param_accepted":true},
        {"provider":"a","endpoint":"https://a/chat/completions","base_url":"https://a","model":"ma","key_env":"K","api_key":"k","probe_latency_s":1.0,"thinking_param_accepted":true}
      ],
      "whisper_stt": {"provider":"openrouter","endpoint":"https://x/audio/transcriptions","model":"m","key_env":"K","api_key":"k"}
    }"#;

    #[test]
    fn sorts_fastest_first_and_reads_keys() {
        let f = ProvidersFile::load_str(SAMPLE).unwrap();
        assert_eq!(f.llm_translation_models[0].model, "ma");
        assert_eq!(f.llm_translation_models[0].api_key(), "k");
        assert!(f.whisper_stt.is_some());
    }

    #[test]
    fn legacy_file_without_fallbacks_parses_empty() {
        // Backward compat: files predating `whisper_stt_fallbacks` parse
        // with an empty failover list (primary-only, like before).
        let f = ProvidersFile::load_str(SAMPLE).unwrap();
        assert!(f.whisper_stt_fallbacks.is_empty());
    }

    #[test]
    fn whisper_fallbacks_keep_file_order_primary_first() {
        let text = r#"{
          "llm_translation_models": [],
          "whisper_stt": {"provider":"p0","endpoint":"https://w0/t","model":"m0","key_env":"","api_key":"k0"},
          "whisper_stt_fallbacks": [
            {"provider":"p1","endpoint":"https://w1/t","model":"m1","key_env":"","api_key":"k1"},
            {"provider":"p2","endpoint":"https://w2/t","model":"m2","key_env":"","api_key":"k2"}
          ]
        }"#;
        let f = ProvidersFile::load_str(text).unwrap();
        assert_eq!(f.whisper_stt_fallbacks.len(), 2);
        let http = reqwest::Client::builder().build().unwrap();
        let pool = ProviderPool::new(f, http);
        assert_eq!(pool.whisper_len(), 3);
        let order: Vec<String> =
            pool.ordered_whisper().into_iter().map(|(_, w)| w.model).collect();
        assert_eq!(order, vec!["m0", "m1", "m2"]);
        assert_eq!(order[0], "m0");
    }

    #[test]
    fn explicit_missing_path_fails_loudly() {
        // An explicit providers path must never silently fall back: the
        // error names the missing file.
        let err = ProvidersFile::load(Path::new("/nonexistent-dir-xyz/asrsub_providers.json"))
            .expect_err("missing explicit path must error");
        assert!(err.to_string().contains("not found"), "unexpected: {err:#}");
    }

    #[test]
    fn corrupt_explicit_file_reports_parse_error() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("asrsub_providers.json");
        std::fs::write(&p, "{not json").unwrap();
        let err = ProvidersFile::load(&p).expect_err("corrupt file must error");
        assert!(err.to_string().contains("parse"), "unexpected: {err:#}");
    }
}
