use std::path::Path;
use std::time::Duration;

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy)]
pub(crate) struct LlmTimeouts {
    pub(crate) connect: Duration,
    pub(crate) read: Duration,
    pub(crate) request: Duration,
    pub(crate) translation: Duration,
}

impl LlmTimeouts {
    pub(super) fn from_env() -> Self {
        let request = env_duration(
            "LLM_TIMEOUT_S",
            Duration::from_secs(300),
            Duration::from_secs(30),
            Duration::from_secs(900),
        );
        let connect = env_duration(
            "LLM_CONNECT_TIMEOUT_S",
            Duration::from_secs(10),
            Duration::from_secs(1),
            request,
        );
        let read = env_duration(
            "LLM_READ_TIMEOUT_S",
            Duration::from_secs(120),
            Duration::from_secs(1),
            request,
        );
        let translation = env_duration(
            "TRANSLATION_TIMEOUT_S",
            Duration::from_secs(900),
            Duration::from_secs(30),
            Duration::from_secs(3600),
        );
        Self {
            connect,
            read,
            request,
            translation,
        }
    }
}

fn env_duration(key: &str, default: Duration, min: Duration, max: Duration) -> Duration {
    let seconds = crate::config::env_str(key)
        .and_then(|v| v.parse::<u64>().ok())
        .map(Duration::from_secs)
        .unwrap_or(default);
    seconds.clamp(min, max)
}

/// Honored provider-file keys per entry: `endpoint`, `model`, `key_env`,
/// `api_key`, `probe_latency_s`, `thinking_param_accepted` (+ the
/// `whisper_stt` / `whisper_stt_fallbacks` / `llm_translation_models`
/// structure). Anything else in the file (legacy `request_shape`, cost
/// metadata, name aliases) parses but is intentionally ignored: unknown
/// keys never fail a load.

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct LlmProvider {
    pub endpoint: String,
    pub model: String,
    #[serde(default)]
    pub key_env: String,
    #[serde(default)]
    pub api_key: String,
    #[serde(default = "default_latency")]
    pub probe_latency_s: f64,
    #[serde(default)]
    pub thinking_param_accepted: bool,
}

fn default_latency() -> f64 {
    30.0
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct WhisperProvider {
    pub endpoint: String,
    pub model: String,
    #[serde(default)]
    pub key_env: String,
    #[serde(default)]
    pub api_key: String,
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
        // The path is operator input and this message reaches stderr and the log,
        // so it is masked like every other sink for a configured value.
        let tried: Vec<String> = tried
            .iter()
            .map(|p| crate::config::mask_for_log(&p.display().to_string()).into_owned())
            .collect();
        anyhow::bail!(
            "providers file not found (tried {tried:?}); set PROVIDERS_FILE or --providers-file"
        )
    }

    fn load_exact(path: &Path) -> Result<Self> {
        let shown = crate::config::mask_for_log(&path.display().to_string()).into_owned();
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("read providers file {shown:?}"))?;
        let mut v: Self = serde_json::from_str(&text)
            .with_context(|| format!("parse providers file {shown:?}"))?;
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

/// API key: embedded value wins, otherwise `$key_env` from the process
/// environment (fixed at container start, so rotation needs a recreate).
/// Shared by LLM and Whisper entries.
fn resolve_key(api_key: &str, key_env: &str) -> String {
    if !api_key.is_empty() {
        return api_key.to_string();
    }
    if !key_env.is_empty() {
        // Trimmed and empty-filtered like every other environment read: a key
        // pasted with a trailing newline must still work, and `KEY=` means
        // "no key", which leaves the endpoint unkeyed instead of sending
        // whitespace as a bearer token.
        return crate::config::env_str(key_env).unwrap_or_default();
    }
    String::new()
}

impl LlmProvider {
    /// API key: embedded value wins, otherwise `$key_env` from the process
    /// environment. The environment is fixed when the container starts, so a
    /// rotated key needs a container recreate, not a live reload.
    pub fn api_key(&self) -> String {
        resolve_key(&self.api_key, &self.key_env)
    }
}

impl WhisperProvider {
    pub fn api_key(&self) -> String {
        resolve_key(&self.api_key, &self.key_env)
    }
}
