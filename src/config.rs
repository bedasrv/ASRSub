//! Runtime configuration: `pipeline.env` + `config.overrides.json` + process env.
//!
//! Precedence (highest wins): process env > overrides file > pipeline.env > defaults.
//! Secrets are never logged; [`Config::masked`] redacts them for `/config` output.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use serde_json::Value;

use crate::lang::normalize_lang;

pub const DEFAULT_JELLYFIN_URL: &str = "http://10.10.20.160:8096";
pub const DEFAULT_JELLYFIN_MEDIA_ROOT: &str = "/media";
/// NAS-local media prefix on this host (mirrors the hardcoded
/// `/mnt/nas/share/media` in Python `map_path`). See `Config::map_path`.
pub const NAS_MEDIA_PREFIX: &str = "/mnt/nas/share/media";

fn cfg_dir() -> PathBuf {
    std::env::var("ASRSUB_CONFIG_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| dirs_home().join(".config").join("asr-pipeline"))
}

fn dirs_home() -> PathBuf {
    std::env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/home/user"))
}

fn parse_env_file(path: &Path, out: &mut HashMap<String, String>) {
    let Ok(text) = std::fs::read_to_string(path) else {
        return;
    };
    for raw in text.lines() {
        let mut line = raw.trim();
        if line.is_empty() || line.starts_with('#') || !line.contains('=') {
            continue;
        }
        // Tolerate `export KEY=...` shell-style lines.
        if let Some(rest) = line.strip_prefix("export ") {
            line = rest.trim_start();
        } else if let Some(rest) = line.strip_prefix("export\t") {
            line = rest.trim_start();
        }
        let (k, v) = line.split_once('=').unwrap();
        let key = k.trim().to_string();
        if key.is_empty() {
            continue;
        }
        out.insert(key, unquote(v.trim()));
    }
}

/// Parse a string list: JSON array (`["a","b"]`), CSV (`a,b`), or a single
/// value. Empty items are dropped; an empty result falls back to `default`.
fn parse_string_list(v: &str) -> Vec<String> {
    let v = v.trim();
    if v.starts_with('[') {
        if let Ok(arr) = serde_json::from_str::<Vec<String>>(v) {
            let out: Vec<String> = arr
                .into_iter()
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .collect();
            if !out.is_empty() {
                return out;
            }
        }
    }
    let out: Vec<String> = v
        .split(',')
        .map(|s| s.trim().trim_matches('"').trim_matches('\'').to_string())
        .filter(|s| !s.is_empty() && s != "[" && s != "]")
        .collect();
    if out.is_empty() {
        vec!["（歌詞）".to_string()]
    } else {
        out
    }
}

/// Strip one layer of matching surrounding quotes (`"..."` / `'...'`).
fn unquote(v: &str) -> String {
    if v.len() >= 2 {
        let b = v.as_bytes();
        if (b[0] == b'"' && b[v.len() - 1] == b'"') || (b[0] == b'\'' && b[v.len() - 1] == b'\'') {
            return v[1..v.len() - 1].to_string();
        }
    }
    v.to_string()
}

/// Process-env keys the pipeline owns. The environment wins over files, but
/// ONLY for these keys or keys already present from files — arbitrary env
/// (`PATH`, `HOSTNAME`, …) must never leak into the config map or `/config`.
/// Process-env keys the pipeline owns. The environment wins over files, but
/// ONLY for these keys or keys already present from files — arbitrary env
/// (`PATH`, `HOSTNAME`, …) must never leak into the config map or `/config`.
///
/// Deliberately absent: the Python-era surface this port did not implement
/// — `RETIME_*`, `ALIGN_*`, ladder upgrade/hunt budgets, `REGEN/
/// MOVIE_LIBRARY`, `MAX_TRANSLATE_WORKERS`, local-inference keys
/// (`TRANSLATE_BASE/MODEL/API_KEY`, `TRANSLATE_CONTEXT_LINES`), and
/// outbound-webhook keys (`WEBHOOK_URLS/SECRET/EVENTS`, `HERMES_*`).
/// Env-provided values for these are ignored outright; file-provided ones
/// still echo in `/config` but nothing consumes them. Listing them here
/// would promise knobs that do nothing.
const ENV_ALLOWLIST: &[&str] = &[
    "SONARR_URL",
    "SONARR_API_KEY",
    "BAZARR_URL",
    "BAZARR_API_KEY",
    "BAZARR_URL_2",
    "BAZARR_API_KEY_2",
    "JELLYFIN_URL",
    "JELLYFIN_API_KEY",
    "JELLYFIN_MEDIA_ROOT",
    "JIMAKU_API_KEY",
    "JIMAKU_DIRECT_ENABLED",
    "JIMAKU_BASE_URL",
    "JIMAKU_CALL_SLEEP_MS",
    "JIMAKU_TIMEOUT",
    "ANILIST_TIMEOUT",
    "ANILIST_CACHE",
    "TARGET_LANGS",
    "MAX_EPS_PER_RUN",
    "TRANSLATE_CHUNK",
    "EPISODE_CONCURRENCY",
    "ASR_CONCURRENCY",
    "TRANSLATE_CONCURRENCY",
    "UPLOAD_CONCURRENCY",
    "LLM_PER_ENDPOINT_CONCURRENCY",
    "LLM_TIMEOUT_S",
    "WHISPER_CONCURRENCY",
    "WHISPER_TIMEOUT_S",
    "TMP_DIR",
    "STATE_FILE",
    "ACTIONS_FILE",
    "EXCLUSIONS_FILE",
    "REGISTRY_FILE",
    "REFINE_STATE_FILE",
    "PROVIDERS_FILE",
    "GLOSSARY_FILE",
    "AI_MARKER_CUE",
    "AI_MARKER_CUE_MS",
    "SDH_PLACEHOLDERS",
    "CPS_MERGE_MAX",
    "CPS_MERGE_MAX_CHARS",
    "CPS_MERGE_MAX_DUR_MS",
    "CPS_MERGE_MAX_GAP_MS",
    "WEBHOOK_PORT",
    "CONTROL_API_KEY",
    "CONTROL_API_KEY_FILE",
    "LADDER_MIN_CUES",
    "LADDER_MIN_CHARS",
    "LADDER_MIN_CJK",
    "LADDER_SPAN_TOLERANCE",
    "MAX_CUE_MS",
    "ASRSUB_CONFIG_DIR",
    "RUST_LOG",
];

/// Flat string map after merging all layers. Kept so unknown/future keys
/// round-trip through `/api2/config` without code changes.
#[derive(Debug, Clone, Default)]
pub struct RawConfig(pub HashMap<String, String>);

impl RawConfig {
    pub fn load() -> Self {
        let dir = cfg_dir();
        let mut map = HashMap::new();
        // Legacy absolute paths first (lowest priority), then the configured
        // dir overlays them — never the reverse.
        parse_env_file(
            Path::new("/home/user/.config/asr-pipeline/pipeline.env"),
            &mut map,
        );
        parse_env_file(&dir.join("pipeline.env"), &mut map);
        // Small helper: JSON-object overrides merge into the map.
        fn merge_overrides(text: &str, map: &mut HashMap<String, String>) {
            if let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(text) {
                for (k, v) in obj {
                    let s = match v {
                        Value::String(s) => s,
                        other => other.to_string().trim_matches('"').to_string(),
                    };
                    map.insert(k, s);
                }
            }
        }
        // Legacy path first, configured dir overlays it.
        if let Ok(text) =
            std::fs::read_to_string("/home/user/.config/asr-pipeline/config.overrides.json")
        {
            merge_overrides(&text, &mut map);
        }
        if let Ok(text) = std::fs::read_to_string(dir.join("config.overrides.json")) {
            merge_overrides(&text, &mut map);
        }
        // Process env wins over files, restricted to pipeline-owned keys
        // (plus keys already present from files) so stray env never leaks
        // into the config map or `/config` output.
        for (k, v) in std::env::vars() {
            if map.contains_key(&k) || ENV_ALLOWLIST.contains(&k.as_str()) {
                map.insert(k, v);
            }
        }
        Self(map)
    }
}

#[derive(Debug, Clone)]
pub struct Config {
    pub raw: HashMap<String, String>,
    pub sonarr_url: String,
    pub sonarr_api_key: String,
    pub bazarr_url: String,
    pub bazarr_api_key: String,
    pub bazarr_url_2: Option<String>,
    pub bazarr_api_key_2: String,
    pub jellyfin_url: String,
    pub jellyfin_api_key: String,
    pub jellyfin_media_root: String,
    pub jimaku_api_key: String,
    pub jimaku_direct_enabled: bool,
    pub target_langs: Vec<String>,
    pub max_eps_per_run: usize,
    pub translate_chunk: usize,
    pub episode_concurrency: usize,
    pub asr_concurrency: usize,
    pub translate_concurrency: usize,
    pub upload_concurrency: usize,
    pub tmp_dir: PathBuf,
    pub state_file: PathBuf,
    pub actions_file: PathBuf,
    pub exclusions_file: PathBuf,
    pub registry_file: PathBuf,
    pub refine_state_file: PathBuf,
    pub providers_file: PathBuf,
    pub glossary_file: PathBuf,
    pub ai_marker_cue: bool,
    pub ai_marker_cue_ms: u32,
    pub sdh_placeholders: Vec<String>,
    pub cps_merge_max: f64,
    pub cps_merge_max_chars: usize,
    pub cps_merge_max_dur_ms: u32,
    pub cps_merge_max_gap_ms: u32,
    pub webhook_port: u16,
    pub control_api_key_file: PathBuf,
    pub ladder_min_cues: usize,
    pub ladder_min_chars: usize,
    pub ladder_min_cjk: f64,
    pub ladder_span_tol: f64,
    pub anilist_cache: PathBuf,
    pub max_cue_ms: u32,
}

impl Config {
    pub fn load() -> anyhow::Result<Self> {
        let raw = RawConfig::load().0;
        let get = |k: &str, d: &str| {
            raw.get(k)
                .cloned()
                .or_else(|| std::env::var(k).ok())
                .map(|v| v.trim().to_string())
                .filter(|v| !v.is_empty())
                .unwrap_or_else(|| d.to_string())
        };
        let get_path = |k: &str, d: &str| PathBuf::from(get(k, d));
        let dir = cfg_dir();
        let bool_of = |k: &str, d: bool| {
            raw.get(k)
                .map(|v| matches!(v.trim().to_lowercase().as_str(), "1" | "true" | "yes"))
                .unwrap_or(d)
        };
        let parse_usize =
            |k: &str, d: usize| raw.get(k).and_then(|v| v.trim().parse().ok()).unwrap_or(d);
        let target_langs = {
            let tl = get("TARGET_LANGS", "id,en");
            let tl = tl.trim();
            let langs: Vec<String> = if tl.starts_with('[') {
                tl.trim_matches(|c| c == '[' || c == ']')
                    .split(',')
                    .map(|s| normalize_lang(s.trim().trim_matches('"').trim_matches('\'')))
                    .filter(|s| !s.is_empty())
                    .collect()
            } else {
                tl.split(',')
                    .map(normalize_lang)
                    .filter(|s| !s.is_empty())
                    .collect()
            };
            let mut dedup = Vec::new();
            for l in langs {
                if !dedup.contains(&l) {
                    dedup.push(l);
                }
            }
            if dedup.is_empty() {
                vec!["id".to_string(), "en".to_string()]
            } else {
                dedup
            }
        };
        // JSON array preferred; bare CSV (`a,b`) and single values accepted
        // so env-style configuration never silently falls back to default.
        let sdh_placeholders = match raw.get("SDH_PLACEHOLDERS").or(raw.get("sdh_placeholders")) {
            Some(v) => parse_string_list(v),
            None => vec!["（歌詞）".to_string()],
        };
        let cpus = std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(4);
        Ok(Self {
            sonarr_url: get("SONARR_URL", ""),
            sonarr_api_key: get("SONARR_API_KEY", ""),
            bazarr_url: get("BAZARR_URL", ""),
            bazarr_api_key: get("BAZARR_API_KEY", ""),
            bazarr_url_2: raw.get("BAZARR_URL_2").filter(|s| !s.is_empty()).cloned(),
            bazarr_api_key_2: get("BAZARR_API_KEY_2", ""),
            jellyfin_url: get("JELLYFIN_URL", DEFAULT_JELLYFIN_URL),
            jellyfin_api_key: get("JELLYFIN_API_KEY", ""),
            jellyfin_media_root: get("JELLYFIN_MEDIA_ROOT", DEFAULT_JELLYFIN_MEDIA_ROOT),
            jimaku_api_key: get("JIMAKU_API_KEY", ""),
            jimaku_direct_enabled: bool_of("JIMAKU_DIRECT_ENABLED", true),
            target_langs,
            max_eps_per_run: parse_usize("MAX_EPS_PER_RUN", 8),
            translate_chunk: parse_usize("TRANSLATE_CHUNK", 10),
            episode_concurrency: parse_usize("EPISODE_CONCURRENCY", cpus.clamp(2, 8)),
            asr_concurrency: parse_usize("ASR_CONCURRENCY", 4),
            translate_concurrency: parse_usize("TRANSLATE_CONCURRENCY", 16),
            upload_concurrency: parse_usize("UPLOAD_CONCURRENCY", 8),
            tmp_dir: get_path("TMP_DIR", &dir.join("tmp").to_string_lossy()),
            state_file: get_path("STATE_FILE", &dir.join("state.jsonl").to_string_lossy()),
            actions_file: get_path("ACTIONS_FILE", &dir.join("actions.jsonl").to_string_lossy()),
            exclusions_file: get_path(
                "EXCLUSIONS_FILE",
                &dir.join("exclusions.jsonl").to_string_lossy(),
            ),
            registry_file: get_path(
                "REGISTRY_FILE",
                &dir.join("subtitle_registry.jsonl").to_string_lossy(),
            ),
            refine_state_file: get_path(
                "REFINE_STATE_FILE",
                &dir.join("refine_state.jsonl").to_string_lossy(),
            ),
            providers_file: get_path("PROVIDERS_FILE", "asrsub_providers.json"),
            glossary_file: get_path(
                "GLOSSARY_FILE",
                &dir.join("glossary.json").to_string_lossy(),
            ),
            ai_marker_cue: bool_of("AI_MARKER_CUE", true),
            ai_marker_cue_ms: raw
                .get("AI_MARKER_CUE_MS")
                .and_then(|v| v.trim().parse().ok())
                .unwrap_or(1500),
            sdh_placeholders,
            cps_merge_max: raw
                .get("CPS_MERGE_MAX")
                .and_then(|v| v.trim().parse().ok())
                .unwrap_or(20.0),
            cps_merge_max_chars: parse_usize("CPS_MERGE_MAX_CHARS", 84),
            cps_merge_max_dur_ms: raw
                .get("CPS_MERGE_MAX_DUR_MS")
                .and_then(|v| v.trim().parse().ok())
                .unwrap_or(7000),
            cps_merge_max_gap_ms: raw
                .get("CPS_MERGE_MAX_GAP_MS")
                .and_then(|v| v.trim().parse().ok())
                .unwrap_or(1000),
            webhook_port: raw
                .get("WEBHOOK_PORT")
                .and_then(|v| v.trim().parse().ok())
                .unwrap_or(8085),
            control_api_key_file: get_path("CONTROL_API_KEY_FILE", "/run/secrets/control_api_key"),
            ladder_min_cues: parse_usize("LADDER_MIN_CUES", 40),
            ladder_min_chars: parse_usize("LADDER_MIN_CHARS", 1500),
            ladder_min_cjk: raw
                .get("LADDER_MIN_CJK")
                .and_then(|v| v.trim().parse().ok())
                .unwrap_or(0.6),
            ladder_span_tol: raw
                .get("LADDER_SPAN_TOLERANCE")
                .and_then(|v| v.trim().parse().ok())
                .unwrap_or(0.15),
            max_cue_ms: raw
                .get("MAX_CUE_MS")
                .and_then(|v| v.trim().parse().ok())
                .unwrap_or(crate::asr::MAX_CUE_MS),
            anilist_cache: get_path(
                "ANILIST_CACHE",
                &dir.join("anilist_cache.json").to_string_lossy(),
            ),
            raw,
        })
    }

    /// Map a `/data/...` container path onto the NAS media root.
    /// Map a Sonarr/Radarr `/data/...` container path onto the NAS-local
    /// path. The NAS prefix is intentionally NOT `jellyfin_media_root`:
    /// Python `map_path` hardcodes `/mnt/nas/share/media` (where this host
    /// mounts the media), while `jellyfin_media_root` is the *Jellyfin
    /// container's* view used only for the refresh lookup in `jellyfin.rs`.
    /// Mixing them breaks refresh silently on any NAS move.
    pub fn map_path(&self, container_path: &str) -> String {
        if let Some(rest) = container_path.strip_prefix("/data/") {
            format!("{NAS_MEDIA_PREFIX}/{rest}")
        } else {
            container_path.to_string()
        }
    }

    /// Secrets-masked view for `/config` telemetry.
    pub fn masked(&self) -> HashMap<String, String> {
        const HINTS: [&str; 7] = ["KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "AUTH", "CRED"];
        self.raw
            .iter()
            .map(|(k, v)| {
                let secret = HINTS.iter().any(|h| k.to_uppercase().contains(h));
                (
                    k.clone(),
                    if secret { "***".to_string() } else { v.clone() },
                )
            })
            .collect()
    }

    pub fn control_key(&self) -> String {
        for cand in [
            std::env::var("CONTROL_API_KEY_FILE")
                .ok()
                .map(PathBuf::from),
            Some(self.control_api_key_file.clone()),
            Some(PathBuf::from("/run/secrets/control_api_key")),
        ]
        .into_iter()
        .flatten()
        {
            if let Ok(v) = std::fs::read_to_string(&cand) {
                let v = v.trim().to_string();
                if !v.is_empty() {
                    return v;
                }
            }
        }
        self.raw.get("CONTROL_API_KEY").cloned().unwrap_or_default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn env_parser_handles_export_and_quotes() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("pipeline.env");
        std::fs::write(&p, "# comment\nexport FOO=\"bar baz\"\nBARE='q'\nEMPTY=\n").unwrap();
        let mut map = HashMap::new();
        parse_env_file(&p, &mut map);
        assert_eq!(map.get("FOO").map(String::as_str), Some("bar baz"));
        assert_eq!(map.get("BARE").map(String::as_str), Some("q"));
        assert_eq!(map.get("EMPTY").map(String::as_str), Some(""));
    }

    #[test]
    fn stray_env_never_enters_config_map() {
        // Process env wins only for pipeline-owned keys: PATH-style strays
        // must not leak into the map (or `/config` output).
        std::env::set_var("ASRSUB_TEST_BOGUS_XYZ", "1");
        std::env::set_var("TARGET_LANGS", "id");
        let raw = RawConfig::load();
        std::env::remove_var("ASRSUB_TEST_BOGUS_XYZ");
        std::env::remove_var("TARGET_LANGS");
        assert!(!raw.0.contains_key("ASRSUB_TEST_BOGUS_XYZ"));
        assert_eq!(raw.0.get("TARGET_LANGS").map(String::as_str), Some("id"));
    }

    #[test]
    fn map_path_rewrites_container_prefix_only() {
        // Row 11 remainder: no sweep root chain exists (no sweep feature —
        // webhook paths are sender-provided), but the /data/ → NAS mapping
        // that every movie/series path flows through is pinned here.
        let cfg = Config::load().expect("config loads");
        assert_eq!(
            cfg.map_path("/data/Shows/Ep.mkv"),
            format!("{NAS_MEDIA_PREFIX}/Shows/Ep.mkv")
        );
        assert_eq!(cfg.map_path("/mnt/x/Ep.mkv"), "/mnt/x/Ep.mkv");
    }
}
