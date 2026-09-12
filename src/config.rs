//! Runtime configuration: `pipeline.env` + `config.overrides.json` + process env.
//!
//! Precedence (highest wins): process env > overrides file > pipeline.env > defaults.
//! Secrets are never logged; [`Config::masked`] redacts them for `/config` output.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use serde_json::Value;

use crate::lang::normalize_lang;

/// Jellyfin base URL. Empty by default: no site-specific private address is
/// compiled in, so a deployment that omits `JELLYFIN_URL` simply leaves the
/// integration disabled instead of silently targeting someone else's network.
/// Set it explicitly whenever `JELLYFIN_API_KEY` is configured.
pub const DEFAULT_JELLYFIN_URL: &str = "";
pub const DEFAULT_JELLYFIN_MEDIA_ROOT: &str = "/media";
/// Default host/NAS media prefix where this daemon mounts the media tree.
/// Override with `NAS_MEDIA_PREFIX`. Intentionally distinct from
/// `JELLYFIN_MEDIA_ROOT`: this is the path *this* process reads, while
/// `jellyfin_media_root` is the *Jellyfin container's* view used only for the
/// refresh-lookup string mapping. See `Config::map_path`.
pub const DEFAULT_NAS_MEDIA_PREFIX: &str = "/mnt/nas/share/media";

fn cfg_dir() -> PathBuf {
    env_str("ASRSUB_CONFIG_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| dirs_home().join(".config").join("asr-pipeline"))
}

/// The process environment's value for `key`, treating an empty or
/// whitespace-only variable as unset — the same rule every config layer applies
/// (see [`RawConfig::load`]).
///
/// A few knobs are read straight from the environment by their consumers rather
/// than through the config map (`PROVIDERS_FILE`, `JIMAKU_BASE_URL`,
/// `ANILIST_BASE_URL`, `ANILIST_CACHE`, `ASRSUB_CONFIG_DIR`). `std::env::var`
/// returns `Ok("")` for `KEY=`, which for those readers means an empty base URL,
/// an empty path, or a startup abort instead of the documented default — so an
/// empty variable has to be filtered here too, or "empty means unset" is only
/// true for some of the settings.
pub(crate) fn env_str(key: &str) -> Option<String> {
    std::env::var(key)
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
}

fn dirs_home() -> PathBuf {
    env_str("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/home/user"))
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
///
/// Deliberately absent: unimplemented retiming/alignment, ladder
/// upgrade/hunt budgets, regeneration/library switches, worker-count aliases,
/// local-inference keys, and outbound-webhook keys. Env-provided values for
/// these are ignored outright; file-provided ones still echo in `/config`
/// but nothing consumes them. Listing them here would promise knobs that do
/// nothing.
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
    "NAS_MEDIA_PREFIX",
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
        // into the config map or `/config` output. An empty variable is not a
        // value: every loader (and `env_pinned`) treats "" as unset, so letting
        // it overwrite the file layer both discarded a configured value and let
        // the settings form report a save the daemon could never apply.
        for (k, v) in std::env::vars() {
            if v.trim().is_empty() {
                continue;
            }
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
    /// Host/NAS path this process reads media from; `/data/…` container
    /// paths from Sonarr/Radarr map here. See [`Config::map_path`].
    pub nas_media_prefix: String,
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

/// Parse an integer from the merged config, warning instead of silently
/// falling back when a value was *set* but cannot be parsed.
///
/// The settings form accepts anything `f64`-parseable for a numeric field, so
/// `MAX_EPS_PER_RUN=4.5` or `WEBHOOK_PORT=70000` used to be saved, reported as
/// written, and then quietly replaced by the default at load.
fn parse_int<T>(raw: &HashMap<String, String>, key: &str, default: T) -> T
where
    T: std::str::FromStr + std::fmt::Display + Copy,
{
    match raw.get(key).map(|v| v.trim()).filter(|v| !v.is_empty()) {
        Some(v) => v.parse().unwrap_or_else(|_| {
            tracing::warn!(
                key,
                value = v,
                "unparseable integer in config; using the default ({default})"
            );
            default
        }),
        None => default,
    }
}

/// [`parse_int`] for float-valued keys.
fn parse_float(raw: &HashMap<String, String>, key: &str, default: f64) -> f64 {
    match raw.get(key).map(|v| v.trim()).filter(|v| !v.is_empty()) {
        Some(v) => v.parse().unwrap_or_else(|_| {
            tracing::warn!(
                key,
                value = v,
                "unparseable number in config; using the default ({default})"
            );
            default
        }),
        None => default,
    }
}

/// Schemes whose `:` separates a scheme from an opaque payload rather than a
/// user name from a password. Without this list the scheme-less branch below
/// reads `mailto:admin@example.com` as a login and publishes `***:***@example.com`.
const NON_HIERARCHICAL_SCHEMES: &[&str] = &[
    "mailto", "data", "urn", "tel", "sms", "geo", "magnet", "bitcoin",
];

/// True when `s` starts with something shaped like the `host[:port]` a
/// credential URL carries after its `@`.
///
/// Used by the scheme-qualified branch to recognise a `@` that belongs to the
/// path or the query (a base URL like `https://bazarr.lan:6767/api?x=a@b`) and
/// not to a userinfo, so a value with no credentials is left alone. It is never
/// allowed to *suppress* masking of a `user:pass@` userinfo: masking must fail
/// closed, because `/config` is answered without authentication.
fn starts_with_authority(s: &str) -> bool {
    let host_port = s.split(['/', '?', '#']).next().unwrap_or("");
    if host_port.is_empty() {
        return false;
    }
    // IPv6 literals keep their colons inside the brackets.
    let (host, port) = match host_port.rsplit_once(':') {
        Some((h, p)) => (h, p),
        None => (host_port, ""),
    };
    !host.is_empty()
        && host
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_' | '[' | ']' | ':'))
        && (port.is_empty() || port.chars().all(|c| c.is_ascii_digit()))
}

/// `scheme://user:pass@host/path` → `scheme://***:***@host/path`, and the same
/// for a scheme-less `user:pass@host`.
///
/// `/config` is answered without authentication, so a credential embedded in an
/// otherwise harmless key (a service URL whose name carries no KEY/TOKEN hint)
/// must not be published either. Two properties matter:
///
/// * **Fail closed.** Anything that carries a colon before the last `@` and
///   looks like an address is masked even when the host is malformed — a
///   trailing space, a non-ASCII or percent-encoded host, an alphabetic port, an
///   empty host, a UNC path, an IPv6 zone id. Host-shapedness may only *add*
///   masking (recognising a `@` that belongs to a path instead of a userinfo),
///   never remove it; a shape the classifier does not recognise is published
///   verbatim otherwise, and that is the wrong default for a credential.
/// * **The userinfo is anchored on the last `@`**, not on the first path
///   separator, so a password containing `/`, `?` or `#` cannot survive unmasked.
///
/// A scheme-less value is masked only when a colon marks real credentials, which
/// leaves plain `user@host` (an email address) untouched, and a non-hierarchical
/// scheme (`mailto:admin@example.com`, `urn:isbn:…@x`) that carries no port is
/// left alone because it has no password to hide. `None` when the value carries
/// no credential-shaped userinfo.
fn redact_userinfo(value: &str) -> Option<String> {
    // A trailing space or newline is a copy/paste artefact, not part of a host.
    let value = value.trim();
    let (prefix, rest) = match value.split_once("://") {
        Some((scheme, rest)) => (format!("{scheme}://"), rest),
        None => (String::new(), value),
    };
    let at = rest.rfind('@')?;
    let userinfo = &rest[..at];
    let after = &rest[at + 1..];
    let host = after.split(['/', '?', '#']).next().unwrap_or("");
    if userinfo.is_empty() {
        return None;
    }
    if prefix.is_empty() {
        // Scheme-less: a colon is what distinguishes `user:pass@host` from a
        // bare address, and the listed schemes have no password at all.
        if !userinfo.contains(':') {
            return None;
        }
        let word = userinfo
            .split(':')
            .next()
            .unwrap_or("")
            .to_ascii_lowercase();
        if NON_HIERARCHICAL_SCHEMES.contains(&word.as_str()) && !host.contains(':') {
            return None;
        }
    } else if userinfo.contains(['/', '?', '#']) {
        // The `@` sits past the authority. Unless the text in front of it is
        // itself host-shaped, this is a password containing a separator and must
        // still be masked.
        let first = userinfo.split(['/', '?', '#']).next().unwrap_or("");
        if starts_with_authority(first) {
            return None;
        }
    }
    let masked = if userinfo.contains(':') {
        "***:***"
    } else {
        "***"
    };
    Some(format!("{prefix}{masked}@{after}"))
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
        // An empty value means "unset" everywhere else in this loader (`get`,
        // `parse_*`), so a Bool must fall back to its default too. Treating ""
        // as false made the settings form — which renders the default for an
        // empty value — disagree with the daemon, and made a default-true
        // switch impossible to turn on from the dashboard.
        let bool_of = |k: &str, d: bool| {
            raw.get(k)
                .map(|v| v.trim().to_string())
                .filter(|v| !v.is_empty())
                .map(|v| matches!(v.to_lowercase().as_str(), "1" | "true" | "yes"))
                .unwrap_or(d)
        };
        let parse_usize = |k: &str, d: usize| parse_int(&raw, k, d);
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
        // `sdh_placeholders` is accepted as a lower-case alias for the
        // spelling older deployments used in pipeline.env.
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
            nas_media_prefix: get("NAS_MEDIA_PREFIX", DEFAULT_NAS_MEDIA_PREFIX),
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
            ai_marker_cue_ms: parse_int(&raw, "AI_MARKER_CUE_MS", 1500),
            sdh_placeholders,
            cps_merge_max: parse_float(&raw, "CPS_MERGE_MAX", 20.0),
            cps_merge_max_chars: parse_usize("CPS_MERGE_MAX_CHARS", 84),
            cps_merge_max_dur_ms: parse_int(&raw, "CPS_MERGE_MAX_DUR_MS", 7000),
            cps_merge_max_gap_ms: parse_int(&raw, "CPS_MERGE_MAX_GAP_MS", 1000),
            webhook_port: parse_int(&raw, "WEBHOOK_PORT", 8085),
            control_api_key_file: get_path("CONTROL_API_KEY_FILE", "/run/secrets/control_api_key"),
            ladder_min_cues: parse_usize("LADDER_MIN_CUES", 40),
            ladder_min_chars: parse_usize("LADDER_MIN_CHARS", 1500),
            ladder_min_cjk: parse_float(&raw, "LADDER_MIN_CJK", 0.6),
            ladder_span_tol: parse_float(&raw, "LADDER_SPAN_TOLERANCE", 0.15),
            max_cue_ms: parse_int(&raw, "MAX_CUE_MS", crate::asr::MAX_CUE_MS),
            anilist_cache: get_path(
                "ANILIST_CACHE",
                &dir.join("anilist_cache.json").to_string_lossy(),
            ),
            raw,
        })
    }

    /// Map a Sonarr/Radarr `/data/...` container path onto this host's
    /// media prefix (`NAS_MEDIA_PREFIX`, default `/mnt/nas/share/media`).
    /// The host prefix is intentionally NOT `jellyfin_media_root`: this is
    /// where the daemon reads the file, while the Jellyfin root is the
    /// *Jellyfin container's* view used only for the refresh lookup (see
    /// [`map_host_to_jellyfin`]). Mixing them breaks refresh silently on any
    /// mount change, so both are configurable and kept distinct.
    pub fn map_path(&self, container_path: &str) -> String {
        map_container_path(&self.nas_media_prefix, container_path)
    }

    /// Secrets-masked view for `/config` telemetry.
    ///
    /// Key-name hints catch every credential key in this tree; URL userinfo is
    /// stripped as well, so a password smuggled inside a URL value
    /// (`https://user:pass@host`) cannot reach an unauthenticated caller.
    pub fn masked(&self) -> HashMap<String, String> {
        const HINTS: [&str; 7] = ["KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "AUTH", "CRED"];
        self.raw
            .iter()
            .map(|(k, v)| {
                let secret = HINTS.iter().any(|h| k.to_uppercase().contains(h));
                let shown = if secret {
                    "***".to_string()
                } else {
                    match redact_userinfo(v) {
                        Some(redacted) => {
                            tracing::warn!(key = k.as_str(), "config value carries URL credentials; masking userinfo for /config");
                            redacted
                        }
                        None => v.clone(),
                    }
                };
                (k.clone(), shown)
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
        // The *process environment* only. `pipeline.env` and
        // `config.overrides.json` live in a mounted directory that anybody with
        // host access can edit, and a key read from there would keep
        // authenticating a deployment that deliberately removed it from the
        // environment — the documented contract is that the control key never
        // comes from `pipeline.env`. An empty variable therefore denies control
        // access (`check_token` refuses an empty key) instead of falling back to
        // a stale file value.
        std::env::var("CONTROL_API_KEY").unwrap_or_default()
    }
}

/// Input widget for a settings field.
///
/// An integer field carries the inclusive maximum its *consumer's* parser
/// accepts (`u16::MAX` for `WEBHOOK_PORT`, `u32::MAX` for the `*_MS` knobs), so
/// the form can reject a value `Config::load` would otherwise silently replace
/// with the default after storing it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FieldKind {
    Text,
    Secret,
    Bool,
    Int(u64),
    Float,
    Csv,
}

/// One settings field. The dashboard renders the settings form from [`FIELDS`],
/// so the UI can never expose a knob the daemon does not read (every key is in
/// [`ENV_ALLOWLIST`], enforced by a test).
#[derive(Debug, Clone, Copy)]
pub struct Field {
    pub group: &'static str,
    pub key: &'static str,
    pub label: &'static str,
    pub kind: FieldKind,
    pub help: &'static str,
    /// Display hint shown when the key is not explicitly set. The settings form
    /// always submits the value it rendered as a hidden baseline, so an
    /// inaccurate hint can never cause a spurious write.
    pub default: &'static str,
}

/// Ordered settings groups `(id, title)`.
pub const FIELD_GROUPS: &[(&str, &str)] = &[
    ("media", "Media services"),
    ("ai", "Providers & languages"),
    ("workflow", "Processing"),
    ("state", "Paths & state"),
    ("advanced", "Advanced"),
];

/// Single source of truth for the settings UI.
pub const FIELDS: &[Field] = &[
    Field {
        group: "media",
        key: "SONARR_URL",
        label: "Sonarr URL",
        kind: FieldKind::Text,
        help: "Base URL of Sonarr, including /api/v3.",
        default: "",
    },
    Field {
        group: "media",
        key: "SONARR_API_KEY",
        label: "Sonarr API key",
        kind: FieldKind::Secret,
        help: "Sonarr API key.",
        default: "",
    },
    Field {
        group: "media",
        key: "BAZARR_URL",
        label: "Bazarr URL (primary)",
        kind: FieldKind::Text,
        help: "Primary Bazarr (Japanese profile).",
        default: "",
    },
    Field {
        group: "media",
        key: "BAZARR_API_KEY",
        label: "Bazarr API key (primary)",
        kind: FieldKind::Secret,
        help: "API key for the primary Bazarr.",
        default: "",
    },
    Field {
        group: "media",
        key: "BAZARR_URL_2",
        label: "Bazarr URL (secondary)",
        kind: FieldKind::Text,
        help: "Secondary Bazarr for the id/en profile (optional).",
        default: "",
    },
    Field {
        group: "media",
        key: "BAZARR_API_KEY_2",
        label: "Bazarr API key (secondary)",
        kind: FieldKind::Secret,
        help: "API key for the secondary Bazarr.",
        default: "",
    },
    Field {
        group: "media",
        key: "JELLYFIN_URL",
        label: "Jellyfin URL",
        kind: FieldKind::Text,
        help: "Base URL of Jellyfin. No compiled-in default: leave empty to disable refresh.",
        default: "",
    },
    Field {
        group: "media",
        key: "JELLYFIN_API_KEY",
        label: "Jellyfin API key",
        kind: FieldKind::Secret,
        help: "API key for Jellyfin refresh (needs JELLYFIN_URL set).",
        default: "",
    },
    Field {
        group: "media",
        key: "JELLYFIN_MEDIA_ROOT",
        label: "Jellyfin media root",
        kind: FieldKind::Text,
        help: "Path prefix the Jellyfin server reports for the same media.",
        default: "/media",
    },
    Field {
        group: "media",
        key: "NAS_MEDIA_PREFIX",
        label: "Host media prefix",
        kind: FieldKind::Text,
        help: "Host/NAS path this daemon reads media at; /data/... maps here.",
        default: "/mnt/nas/share/media",
    },
    Field {
        group: "ai",
        key: "TARGET_LANGS",
        label: "Target languages",
        kind: FieldKind::Csv,
        help: "Languages to generate subtitles for, e.g. id,en.",
        default: "id,en",
    },
    Field {
        group: "ai",
        key: "PROVIDERS_FILE",
        label: "Providers file",
        kind: FieldKind::Text,
        help: "Path to asrsub_providers.json (LLM + Whisper endpoints).",
        default: "asrsub_providers.json",
    },
    Field {
        group: "ai",
        key: "JIMAKU_API_KEY",
        label: "Jimaku API key",
        kind: FieldKind::Secret,
        help: "Direct Jimaku source rung (empty disables it).",
        default: "",
    },
    Field {
        group: "ai",
        key: "JIMAKU_DIRECT_ENABLED",
        label: "Jimaku direct enabled",
        kind: FieldKind::Bool,
        help: "Try Jimaku for existing Japanese subtitles before ASR.",
        default: "true",
    },
    Field {
        group: "workflow",
        key: "MAX_EPS_PER_RUN",
        label: "Max episodes per run",
        kind: FieldKind::Int(usize::MAX as u64),
        help: "Cap on items processed per pass.",
        default: "8",
    },
    Field {
        group: "workflow",
        key: "EPISODE_CONCURRENCY",
        label: "Episode concurrency",
        kind: FieldKind::Int(usize::MAX as u64),
        help: "Parallel episodes per pass. Empty = detected CPU count (clamped 2-8), \
               which is machine-dependent and therefore not shown as a value here.",
        // No static default on purpose: the loader derives this from the host's
        // CPU count, so displaying "4" would name a number the daemon may not be
        // using. Empty renders as "not set"; typing a number pins it.
        default: "",
    },
    Field {
        group: "workflow",
        key: "ASR_CONCURRENCY",
        label: "ASR concurrency",
        kind: FieldKind::Int(usize::MAX as u64),
        help: "Transcription fan-out per episode.",
        default: "4",
    },
    Field {
        group: "workflow",
        key: "TRANSLATE_CONCURRENCY",
        label: "Translate concurrency",
        kind: FieldKind::Int(usize::MAX as u64),
        help: "Translation fan-out per episode.",
        default: "16",
    },
    Field {
        group: "workflow",
        key: "UPLOAD_CONCURRENCY",
        label: "Upload concurrency",
        kind: FieldKind::Int(usize::MAX as u64),
        help: "Parallel Bazarr uploads across episodes.",
        default: "8",
    },
    Field {
        group: "workflow",
        key: "TRANSLATE_CHUNK",
        label: "Translate chunk size",
        kind: FieldKind::Int(usize::MAX as u64),
        help: "Lines per LLM translation request.",
        default: "10",
    },
    Field {
        group: "workflow",
        key: "MAX_CUE_MS",
        label: "Max cue duration (ms)",
        kind: FieldKind::Int(u32::MAX as u64),
        help: "Maximum subtitle cue duration.",
        default: "8000",
    },
    Field {
        group: "workflow",
        key: "AI_MARKER_CUE",
        label: "AI marker cue",
        kind: FieldKind::Bool,
        help: "Write [AI-generated by ASRSub] as the first cue.",
        default: "true",
    },
    Field {
        group: "workflow",
        key: "AI_MARKER_CUE_MS",
        label: "AI marker duration (ms)",
        kind: FieldKind::Int(u32::MAX as u64),
        help: "Duration of the AI marker cue.",
        default: "1500",
    },
    Field {
        group: "state",
        key: "STATE_FILE",
        label: "State file",
        kind: FieldKind::Text,
        help: "Pipeline state ledger (state.jsonl).",
        default: "~/.config/asr-pipeline/state.jsonl",
    },
    Field {
        group: "state",
        key: "ACTIONS_FILE",
        label: "Actions file",
        kind: FieldKind::Text,
        help: "Dashboard/pctl action queue (actions.jsonl).",
        default: "~/.config/asr-pipeline/actions.jsonl",
    },
    Field {
        group: "state",
        key: "EXCLUSIONS_FILE",
        label: "Exclusions file",
        kind: FieldKind::Text,
        help: "Excluded episode ids (exclusions.jsonl).",
        default: "~/.config/asr-pipeline/exclusions.jsonl",
    },
    Field {
        group: "state",
        key: "REGISTRY_FILE",
        label: "Registry file",
        kind: FieldKind::Text,
        help: "Subtitle provenance registry (subtitle_registry.jsonl).",
        default: "~/.config/asr-pipeline/subtitle_registry.jsonl",
    },
    Field {
        group: "state",
        key: "REFINE_STATE_FILE",
        label: "Refine state file",
        kind: FieldKind::Text,
        help: "Refine review ledger (refine_state.jsonl).",
        default: "~/.config/asr-pipeline/refine_state.jsonl",
    },
    Field {
        group: "state",
        key: "GLOSSARY_FILE",
        label: "Glossary file",
        kind: FieldKind::Text,
        help: "Series glossary JSON.",
        default: "~/.config/asr-pipeline/glossary.json",
    },
    Field {
        group: "state",
        key: "ANILIST_CACHE",
        label: "AniList cache",
        kind: FieldKind::Text,
        help: "AniList lookup cache JSON.",
        default: "~/.config/asr-pipeline/anilist_cache.json",
    },
    Field {
        group: "state",
        key: "TMP_DIR",
        label: "Temp dir",
        kind: FieldKind::Text,
        help: "Scratch directory for audio/subtitle work.",
        default: "~/.config/asr-pipeline/tmp",
    },
    Field {
        group: "state",
        key: "WEBHOOK_PORT",
        label: "Control/webhook port",
        kind: FieldKind::Int(u16::MAX as u64),
        help: "Port for the dashboard, control API, and /webhook.",
        default: "8085",
    },
    Field {
        group: "advanced",
        key: "SDH_PLACEHOLDERS",
        label: "Lyric placeholders",
        kind: FieldKind::Csv,
        help: "Placeholder tokens for foreign/lyric lines, e.g. （歌詞）.",
        default: "（歌詞）",
    },
    Field {
        group: "advanced",
        key: "CPS_MERGE_MAX",
        label: "Max reading speed",
        kind: FieldKind::Float,
        help: "Chars/sec cap before merging cues.",
        default: "20.0",
    },
    Field {
        group: "advanced",
        key: "CPS_MERGE_MAX_CHARS",
        label: "Max merged chars",
        kind: FieldKind::Int(usize::MAX as u64),
        help: "Max characters in a merged cue.",
        default: "84",
    },
    Field {
        group: "advanced",
        key: "CPS_MERGE_MAX_DUR_MS",
        label: "Max merged duration (ms)",
        kind: FieldKind::Int(u32::MAX as u64),
        help: "Max duration of a merged cue.",
        default: "7000",
    },
    Field {
        group: "advanced",
        key: "CPS_MERGE_MAX_GAP_MS",
        label: "Max merge gap (ms)",
        kind: FieldKind::Int(u32::MAX as u64),
        help: "Max gap bridged when merging cues.",
        default: "1000",
    },
    Field {
        group: "advanced",
        key: "LADDER_MIN_CUES",
        label: "Ladder min cues",
        kind: FieldKind::Int(usize::MAX as u64),
        help: "Minimum source cues to accept a sidecar.",
        default: "40",
    },
    Field {
        group: "advanced",
        key: "LADDER_MIN_CHARS",
        label: "Ladder min chars",
        kind: FieldKind::Int(usize::MAX as u64),
        help: "Minimum source characters to accept a sidecar.",
        default: "1500",
    },
    Field {
        group: "advanced",
        key: "LADDER_MIN_CJK",
        label: "Ladder min CJK ratio",
        kind: FieldKind::Float,
        help: "Minimum CJK ratio for Japanese sources.",
        default: "0.6",
    },
    Field {
        group: "advanced",
        key: "LADDER_SPAN_TOLERANCE",
        label: "Ladder span tolerance",
        kind: FieldKind::Float,
        help: "Max span/duration deviation for a sidecar.",
        default: "0.15",
    },
];

/// Keys owned by the pipeline but deliberately **not** in [`FIELDS`]. They are
/// consumed by `std::env::var` at the point of use (`providers.rs`,
/// `jimaku.rs`), so neither this UI's `config.overrides.json` layer nor
/// `pipeline.env` reaches them — `pipeline.env` is parsed into the config map,
/// never exported into the process environment. Set them in the *container*
/// environment: the compose `environment:` block or the optional `env_file`
/// (`PROVIDER_KEYS_FILE`).
pub const ENV_ONLY_KEYS: &[&str] = &[
    "LLM_PER_ENDPOINT_CONCURRENCY",
    "LLM_TIMEOUT_S",
    "WHISPER_CONCURRENCY",
    "WHISPER_TIMEOUT_S",
    "JIMAKU_BASE_URL",
    "JIMAKU_CALL_SLEEP_MS",
    "JIMAKU_TIMEOUT",
    "ANILIST_TIMEOUT",
    "ANILIST_BASE_URL",
];

/// True when `key` is an editable settings field (used to reject unknown keys
/// on `POST /api2/config`).
pub fn is_editable_key(key: &str) -> bool {
    FIELDS.iter().any(|f| f.key == key)
}

/// What `key` expects, when `value` cannot be used for it: `None` means the
/// value is usable (or the key is not a typed field).
///
/// Both write paths — the settings form (`/ui/config`) and the JSON API
/// (`/api2/config`) — validate through this, so neither can persist a value the
/// loader would only warn about and silently discard on the next start. The
/// bound is the *consumer's*, not a guess from the default's shape: a u16 field
/// refuses 70000 even though it parses as a `u64`.
pub fn value_requirement(key: &str, value: &str) -> Option<String> {
    let f = FIELDS.iter().find(|f| f.key == key)?;
    // Validate the *trimmed* value: every consumer trims (`parse_env_file`,
    // `parse_int`, `get`, and the form's `collect_changes`), so a padded `" 5"`
    // loads as 5 and must not be refused here — the two write paths would
    // otherwise disagree about the same logical input.
    let value = value.trim();
    if value.is_empty() {
        // Empty means unset, not invalid.
        return None;
    }
    match f.kind {
        FieldKind::Int(max) => value
            .parse::<u64>()
            .ok()
            .filter(|n| *n <= max)
            .is_none()
            .then(|| format!("a whole number between 0 and {max}")),
        FieldKind::Float => value
            .parse::<f64>()
            .is_err()
            .then(|| "a number".to_string()),
        _ => None,
    }
}

/// True when the process environment defines `key`.
///
/// Process env outranks every config layer ([`RawConfig`] applies it last), and
/// the shipped compose pins `NAS_MEDIA_PREFIX` and `WEBHOOK_PORT` that way
/// because the bind mount and the reverse proxy must agree with them. A
/// dashboard save for such a key would persist a value the daemon can never
/// apply, so the UI renders those fields read-only and the config API rejects
/// them instead of pretending to save.
///
/// A variable that is set but *empty* does not count: every loader in this
/// module treats an empty value as "unset", so pinning on mere presence made a
/// knob the environment does not actually override impossible to edit.
pub fn env_pinned(key: &str) -> bool {
    std::env::var(key)
        .map(|v| !v.trim().is_empty())
        .unwrap_or(false)
}

/// Merge `pairs` into `config.overrides.json` under the configured dir.
///
/// The overrides layer beats `pipeline.env` but not process env (see module
/// docs). Atomic replace under the same sidecar lock the ledgers use, and the
/// file is created `0600`: secret-typed settings reach it, so it must never be
/// world-readable. An empty value is written as empty but `Config::load`
/// treats empty as "unset", so to clear a value remove the key from the file —
/// clearing is intentionally not expressible here.
pub fn write_overrides(pairs: &[(String, String)]) -> anyhow::Result<PathBuf> {
    use std::io::Write;
    use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};

    let path = cfg_dir().join("config.overrides.json");
    crate::state::ensure_parent(&path)?;
    let lock_file = crate::state::open_lock(&path)?;
    let mut guard = fd_lock::RwLock::new(lock_file);
    let _w = guard.write()?;
    let mut map: serde_json::Map<String, Value> = std::fs::read_to_string(&path)
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default();
    for (k, v) in pairs {
        map.insert(k.clone(), Value::String(v.clone()));
    }
    let tmp = path.with_extension("json.tmp");
    {
        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&tmp)?;
        f.write_all(&serde_json::to_vec_pretty(&Value::Object(map))?)?;
        f.flush()?;
    }
    std::fs::rename(&tmp, &path)?;
    // The rename carries the temp file's mode; this also repairs a file an
    // older build (or an operator's umask) left group/world-readable.
    let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    Ok(path)
}

/// `/data/…` container path → host/NAS path (pure; see [`Config::map_path`]).
fn map_container_path(prefix: &str, container_path: &str) -> String {
    if let Some(rest) = container_path.strip_prefix("/data/") {
        format!("{}/{rest}", prefix.trim_end_matches('/'))
    } else {
        container_path.to_string()
    }
}

/// Host/NAS path → Jellyfin-reported path. Strip the host prefix, prepend
/// `JELLYFIN_MEDIA_ROOT`; a path outside the host prefix passes through
/// unchanged. Shared by the refresh lookup and the mapping tests.
pub(crate) fn map_host_to_jellyfin(
    host_prefix: &str,
    jellyfin_root: &str,
    host_path: &str,
) -> String {
    let host = host_prefix.trim_end_matches('/');
    match host_path.strip_prefix(host) {
        Some(rest) => format!("{}{rest}", jellyfin_root.trim_end_matches('/')),
        None => host_path.to_string(),
    }
}

/// Serialises tests that mutate process-global state: `ASRSUB_CONFIG_DIR` and
/// any settings key, since `env_pinned` reads the process environment. Shared
/// with the web tests, which pin keys the same way.
#[cfg(test)]
pub(crate) static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

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
        // The /data/ → host-prefix mapping every movie/series path flows
        // through is pinned here, including a non-default host prefix.
        assert_eq!(
            map_container_path(DEFAULT_NAS_MEDIA_PREFIX, "/data/Shows/Ep.mkv"),
            "/mnt/nas/share/media/Shows/Ep.mkv"
        );
        assert_eq!(
            map_container_path("/srv/media", "/data/Movies/M.mkv"),
            "/srv/media/Movies/M.mkv"
        );
        // A trailing slash on the configured prefix must not double up.
        assert_eq!(
            map_container_path("/srv/media/", "/data/Movies/M.mkv"),
            "/srv/media/Movies/M.mkv"
        );
        assert_eq!(
            map_container_path(DEFAULT_NAS_MEDIA_PREFIX, "/mnt/x/Ep.mkv"),
            "/mnt/x/Ep.mkv"
        );
    }

    #[test]
    fn host_to_jellyfin_maps_non_default_roots() {
        // Non-default host prefix AND non-default Jellyfin root.
        assert_eq!(
            map_host_to_jellyfin("/srv/media", "/jellyfin/media", "/srv/media/Shows/Ep.mkv"),
            "/jellyfin/media/Shows/Ep.mkv"
        );
        // A path outside the host prefix passes through untouched.
        assert_eq!(
            map_host_to_jellyfin("/srv/media", "/media", "/other/Ep.mkv"),
            "/other/Ep.mkv"
        );
    }

    #[test]
    fn sonarr_to_jellyfin_path_round_trip() {
        // The full flow: Sonarr/Radarr container path → host path → the path
        // Jellyfin reports, for default and fully custom layouts.
        for (host, jelly, container, expected) in [
            (
                DEFAULT_NAS_MEDIA_PREFIX,
                DEFAULT_JELLYFIN_MEDIA_ROOT,
                "/data/Shows/Ep.mkv",
                "/media/Shows/Ep.mkv",
            ),
            (
                "/srv/media",
                "/jellyfin/m",
                "/data/Movies/M.mkv",
                "/jellyfin/m/Movies/M.mkv",
            ),
        ] {
            let mapped = map_container_path(host, container);
            assert_eq!(map_host_to_jellyfin(host, jelly, &mapped), expected);
        }
    }

    #[test]
    fn jellyfin_url_defaults_empty_not_site_specific() {
        // Regression: no compiled-in private address. An unconfigured
        // deployment leaves Jellyfin disabled rather than targeting a
        // particular LAN host.
        assert_eq!(DEFAULT_JELLYFIN_URL, "");
    }

    #[test]
    fn settings_schema_exposes_only_allowlisted_keys() {
        // The dashboard field list must never drift from the daemon: every
        // exposed key is one the pipeline actually reads.
        assert!(!FIELDS.is_empty());
        for f in FIELDS {
            assert!(
                ENV_ALLOWLIST.contains(&f.key),
                "settings field {} is not a pipeline-owned key",
                f.key
            );
            assert!(
                FIELD_GROUPS.iter().any(|(id, _)| *id == f.group),
                "field {} has unknown group {}",
                f.key,
                f.group
            );
        }
        // No duplicate keys.
        let mut keys: Vec<&str> = FIELDS.iter().map(|f| f.key).collect();
        keys.sort_unstable();
        let before = keys.len();
        keys.dedup();
        assert_eq!(before, keys.len(), "duplicate settings keys");
        // Every group has at least one field (no empty sections in the UI).
        for (id, _) in FIELD_GROUPS {
            assert!(
                FIELDS.iter().any(|f| f.group == *id),
                "group {id} has no fields"
            );
        }
        // Env-only knobs must never leak into the schema: they are read via
        // `std::env::var`, so the overrides layer this UI writes cannot reach
        // them. Exposing them would be a phantom knob.
        for k in ENV_ONLY_KEYS {
            assert!(
                !FIELDS.iter().any(|f| f.key == *k),
                "env-only key {k} must not be an editable field"
            );
            assert!(!is_editable_key(k), "env-only key {k} must not be editable");
        }
    }

    #[test]
    fn editable_key_rejects_unknown_and_accepts_known() {
        assert!(is_editable_key("TARGET_LANGS"));
        assert!(is_editable_key("NAS_MEDIA_PREFIX"));
        // Python-era phantom knobs and strays are not editable.
        assert!(!is_editable_key("TRANSLATE_MODEL"));
        assert!(!is_editable_key("HERMES_WEBHOOK_URL"));
        assert!(!is_editable_key("PATH"));
    }

    #[test]
    fn write_overrides_merges_and_round_trips() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let dir = tempfile::tempdir().unwrap();
        std::env::set_var("ASRSUB_CONFIG_DIR", dir.path());
        let r = write_overrides(&[
            ("TARGET_LANGS".to_string(), "id,en,es".to_string()),
            ("NAS_MEDIA_PREFIX".to_string(), "/srv/media".to_string()),
        ]);
        // Second write must merge, not clobber the first key.
        let r2 = write_overrides(&[("MAX_EPS_PER_RUN".to_string(), "4".to_string())]);
        std::env::remove_var("ASRSUB_CONFIG_DIR");
        r.unwrap();
        r2.unwrap();

        let text = std::fs::read_to_string(dir.path().join("config.overrides.json")).unwrap();
        let v: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(v["TARGET_LANGS"], "id,en,es");
        assert_eq!(v["NAS_MEDIA_PREFIX"], "/srv/media");
        assert_eq!(v["MAX_EPS_PER_RUN"], "4");

        // And Config::load picks the values up from that layer.
        std::env::set_var("ASRSUB_CONFIG_DIR", dir.path());
        let cfg = Config::load().unwrap();
        std::env::remove_var("ASRSUB_CONFIG_DIR");
        assert_eq!(cfg.target_langs, vec!["id", "en", "es"]);
        assert_eq!(cfg.nas_media_prefix, "/srv/media");
        assert_eq!(cfg.max_eps_per_run, 4);
    }

    #[test]
    fn override_file_is_never_world_readable() {
        use std::os::unix::fs::PermissionsExt;
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let dir = tempfile::tempdir().unwrap();
        std::env::set_var("ASRSUB_CONFIG_DIR", dir.path());
        let written = write_overrides(&[(
            "SONARR_API_KEY".to_string(),
            "applied-value-not-a-real-key".to_string(),
        )]);
        std::env::remove_var("ASRSUB_CONFIG_DIR");
        let path = written.unwrap();
        let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(
            mode, 0o600,
            "secret-typed settings land in this file; it must not be readable by others"
        );
    }

    #[test]
    fn empty_bool_value_keeps_the_documented_default() {
        // The settings form renders a field's default for an empty value, so
        // the loader must read "" as unset too — otherwise a default-true
        // switch could never be turned on from the dashboard.
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("pipeline.env"), "AI_MARKER_CUE=\n").unwrap();
        std::env::set_var("ASRSUB_CONFIG_DIR", dir.path());
        let cfg = Config::load().unwrap();
        std::env::remove_var("ASRSUB_CONFIG_DIR");
        assert!(
            cfg.ai_marker_cue,
            "empty value must not flip a true default"
        );
    }

    #[test]
    fn unparseable_numbers_fall_back_to_defaults() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("pipeline.env"),
            "MAX_EPS_PER_RUN=4.5\nWEBHOOK_PORT=not-a-port\n",
        )
        .unwrap();
        std::env::set_var("ASRSUB_CONFIG_DIR", dir.path());
        let cfg = Config::load().unwrap();
        std::env::remove_var("ASRSUB_CONFIG_DIR");
        // A warning is logged for each; the pass still runs on sane values.
        assert_eq!(cfg.max_eps_per_run, 8);
        assert_eq!(cfg.webhook_port, 8085);
    }

    #[test]
    fn env_pinned_ignores_empty_values() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        // A variable that is set but empty does not override anything (every
        // loader treats "" as unset, and `RawConfig::load` skips it), so it must
        // not lock the field either.
        std::env::set_var("ASRSUB_TEST_PIN_NONEMPTY", "8085");
        assert!(env_pinned("ASRSUB_TEST_PIN_NONEMPTY"));
        std::env::remove_var("ASRSUB_TEST_PIN_NONEMPTY");
        assert!(!env_pinned("ASRSUB_TEST_PIN_NONEMPTY"));

        std::env::set_var("ASRSUB_TEST_PIN_EMPTY", "");
        assert!(!env_pinned("ASRSUB_TEST_PIN_EMPTY"));
        std::env::set_var("ASRSUB_TEST_PIN_EMPTY", "   ");
        assert!(!env_pinned("ASRSUB_TEST_PIN_EMPTY"));
        std::env::remove_var("ASRSUB_TEST_PIN_EMPTY");
    }

    #[test]
    fn empty_env_value_does_not_shadow_the_file_layer() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("pipeline.env"), "WEBHOOK_PORT=1234\n").unwrap();
        let prev_dir = std::env::var_os("ASRSUB_CONFIG_DIR");
        std::env::set_var("ASRSUB_CONFIG_DIR", dir.path());
        // A deployment that leaves a variable empty is not overriding anything.
        // Letting the empty value replace the file layer made the dashboard
        // report a save ("Wrote 1 override(s)") that the daemon could never
        // apply, because the empty env var won at load.
        std::env::set_var("WEBHOOK_PORT", "");
        assert_eq!(
            RawConfig::load().0.get("WEBHOOK_PORT").map(String::as_str),
            Some("1234")
        );
        // A real value still wins over the file, as documented.
        std::env::set_var("WEBHOOK_PORT", "1235");
        assert_eq!(
            RawConfig::load().0.get("WEBHOOK_PORT").map(String::as_str),
            Some("1235")
        );
        std::env::remove_var("WEBHOOK_PORT");
        match prev_dir {
            Some(v) => std::env::set_var("ASRSUB_CONFIG_DIR", v),
            None => std::env::remove_var("ASRSUB_CONFIG_DIR"),
        }
    }

    #[test]
    fn value_requirement_matches_the_consumers_type() {
        // The shared rule behind both write paths (/ui/config and /api2/config).
        assert_eq!(
            value_requirement("WEBHOOK_PORT", "70000").as_deref(),
            Some("a whole number between 0 and 65535")
        );
        assert_eq!(value_requirement("WEBHOOK_PORT", "65535"), None);
        assert_eq!(
            value_requirement("MAX_CUE_MS", "4294967296").as_deref(),
            Some("a whole number between 0 and 4294967295")
        );
        assert_eq!(
            value_requirement("LADDER_MIN_CJK", "abc").as_deref(),
            Some("a number")
        );
        assert_eq!(value_requirement("LADDER_MIN_CJK", "0.55"), None);
        // Empty means unset, text fields take anything, unknown keys are not
        // this function's business (the API rejects them separately).
        assert_eq!(value_requirement("MAX_EPS_PER_RUN", ""), None);
        assert_eq!(value_requirement("TARGET_LANGS", "id,en,es"), None);
        assert_eq!(value_requirement("NOT_A_KEY", "1"), None);
    }

    #[test]
    fn url_credentials_are_stripped_from_config_output() {
        assert_eq!(
            redact_userinfo("https://user:pw@bazarr.lan:6767/api").as_deref(),
            Some("https://***:***@bazarr.lan:6767/api")
        );
        assert_eq!(
            redact_userinfo("https://user@bazarr.lan/api").as_deref(),
            Some("https://***@bazarr.lan/api")
        );
        // A scheme-less credential is still a credential.
        assert_eq!(
            redact_userinfo("user:pw@sonarr.lan:8989").as_deref(),
            Some("***:***@sonarr.lan:8989")
        );
        // ... but a bare user@host (or an email address) is not.
        assert_eq!(redact_userinfo("user@sonarr.lan"), None);
        assert_eq!(redact_userinfo("noreply@example.com"), None);
        // A password containing '@' must not survive half-masked: the host is
        // whatever follows the last '@' in the authority.
        assert_eq!(
            redact_userinfo("https://user:p@ss@bazarr.lan:6767").as_deref(),
            Some("https://***:***@bazarr.lan:6767")
        );
        assert_eq!(redact_userinfo("http://bazarr.lan:6767/api"), None);
        // Values that merely contain a colon and an '@' are not logins: masking
        // them loses information (`mailto:` was rewritten to `***:***@…`).
        assert_eq!(redact_userinfo("mailto:admin@example.com"), None);
        assert_eq!(redact_userinfo("urn:isbn:1234@x"), None);
        // A query string's '@' sits past the authority, so nothing is masked.
        assert_eq!(redact_userinfo("https://bazarr.lan/api?x=a@b"), None);
        // IPv6 literals keep their colons.
        assert_eq!(
            redact_userinfo("https://user:pw@[::1]:8080/api").as_deref(),
            Some("https://***:***@[::1]:8080/api")
        );
        // A password containing a path/query separator used to hide the whole
        // credential, because the authority was cut at the first '/' or '?'.
        assert_eq!(
            redact_userinfo("https://user:pa/ss@bazarr.lan:6767").as_deref(),
            Some("https://***:***@bazarr.lan:6767")
        );
        assert_eq!(
            redact_userinfo("user:pa?ss@sonarr.lan:8989").as_deref(),
            Some("***:***@sonarr.lan:8989")
        );
        // Known, pre-existing over-masking: these carry no password but have a
        // colon before the last '@', and nothing distinguishes them from a
        // `user:pass@host` value. Masking is the safe direction.
        assert_eq!(
            redact_userinfo("fe80::1@host.lan").as_deref(),
            Some("***:***@host.lan")
        );

        // A key whose *name* carries no secret hint still loses its userinfo:
        // `/config` is unauthenticated.
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("pipeline.env"),
            "BAZARR_URL=https://user:pw@bazarr.lan:6767\n",
        )
        .unwrap();
        std::env::set_var("ASRSUB_CONFIG_DIR", dir.path());
        let cfg = Config::load().unwrap();
        let masked = cfg.masked();
        std::env::remove_var("ASRSUB_CONFIG_DIR");
        assert_eq!(
            masked.get("BAZARR_URL").map(String::as_str),
            Some("https://***:***@bazarr.lan:6767")
        );
    }

    /// The redaction boundary must fail closed: a credential is masked whatever
    /// the host looks like. The regression this pins was a host-shape *filter*
    /// that returned `None` — publishing `user:pass@…` verbatim — for a host with
    /// a trailing space, a non-ASCII or percent-encoded name, an alphabetic port,
    /// an empty host, a UNC path or an IPv6 zone id. A trailing space is a plain
    /// copy/paste artefact, so this is the common case, not an exotic one.
    #[test]
    fn url_credentials_are_masked_even_when_the_host_is_malformed() {
        for (value, want) in [
            (
                "https://user:s3cr3t@sonarr.lan ",
                "https://***:***@sonarr.lan",
            ),
            (
                "https://user:s3cr3t@sonarr.lan\t",
                "https://***:***@sonarr.lan",
            ),
            (
                "https://user:pw@bazarr.lan:6767\n",
                "https://***:***@bazarr.lan:6767",
            ),
            ("user:s3cr3t@sonarr.lan ", "***:***@sonarr.lan"),
            (
                "https://user:pw@sonarr.lan:http",
                "https://***:***@sonarr.lan:http",
            ),
            ("https://user:pw@:8080/api", "https://***:***@:8080/api"),
            ("https://user:pw@/api", "https://***:***@/api"),
            ("https://user:pw@café.host/x", "https://***:***@café.host/x"),
            (
                "https://user:pw@host%20name/x",
                "https://***:***@host%20name/x",
            ),
            (
                "https://user:pw@[fe80::1%eth0]:8080/api",
                "https://***:***@[fe80::1%eth0]:8080/api",
            ),
            ("\\\\user:pw@nas\\share", "***:***@nas\\share"),
            // A scheme-less value whose user name happens to be a scheme word is
            // still a credential when a port follows the host.
            ("tel:pw@host.lan:22", "***:***@host.lan:22"),
        ] {
            assert_eq!(
                redact_userinfo(value).as_deref(),
                Some(want),
                "credential published verbatim: {value:?}"
            );
        }
        // Documented trade-off: with no port after the host, a scheme-less value
        // whose first word is a non-hierarchical scheme is read as that URI and
        // left alone. `mailto:`/`urn:` are the reason the list exists; the cost
        // is a credential whose *user name* is `data`, `tel`, … and which has no
        // port. Masking it would publish `***:***@…` for every email address.
        assert_eq!(redact_userinfo("data:pw@host.lan"), None);
        // A `@` past a host-shaped authority is a path/query character, not a
        // userinfo — that is the only thing host shape is allowed to decide.
        assert_eq!(redact_userinfo("https://bazarr.lan:6767/api?x=a@b"), None);
    }

    /// `value_requirement` feeds both write paths, and every consumer trims, so
    /// a padded number must be accepted rather than refused by one path and
    /// loaded by the other.
    #[test]
    fn value_requirement_ignores_surrounding_whitespace() {
        assert_eq!(value_requirement("MAX_EPS_PER_RUN", "   5   "), None);
        assert_eq!(
            value_requirement("WEBHOOK_PORT", " 70000 ").as_deref(),
            Some("a whole number between 0 and 65535")
        );
        assert_eq!(value_requirement("LADDER_MIN_CJK", " 0.55\n"), None);
    }

    /// An empty or whitespace-only variable is unset for the consumers that read
    /// the environment directly (they cannot see `Config`'s merge).
    #[test]
    fn env_str_treats_an_empty_variable_as_unset() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        std::env::set_var("ASRSUB_TEST_ENV_STR", "");
        assert_eq!(env_str("ASRSUB_TEST_ENV_STR"), None);
        std::env::set_var("ASRSUB_TEST_ENV_STR", "   ");
        assert_eq!(env_str("ASRSUB_TEST_ENV_STR"), None);
        std::env::set_var("ASRSUB_TEST_ENV_STR", " value \n");
        assert_eq!(env_str("ASRSUB_TEST_ENV_STR").as_deref(), Some("value"));
        std::env::remove_var("ASRSUB_TEST_ENV_STR");
        assert_eq!(env_str("ASRSUB_TEST_ENV_STR"), None);
    }

    /// `pipeline.env` and `config.overrides.json` live in a mounted directory;
    /// the control key is documented as never coming from them. Blanking the
    /// environment variable must therefore deny control access instead of
    /// reviving a file-layer key.
    #[test]
    fn control_key_never_comes_from_a_config_file() {
        if std::path::Path::new("/run/secrets/control_api_key").exists() {
            // The shipped default secret would win before the environment is
            // consulted, so this test cannot be deterministic on such a host.
            return;
        }
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("pipeline.env"),
            "CONTROL_API_KEY=file-layer-key\n",
        )
        .unwrap();
        let prev_dir = std::env::var_os("ASRSUB_CONFIG_DIR");
        let prev_file = std::env::var_os("CONTROL_API_KEY_FILE");
        let prev_key = std::env::var_os("CONTROL_API_KEY");
        std::env::set_var("ASRSUB_CONFIG_DIR", dir.path());
        std::env::remove_var("CONTROL_API_KEY_FILE");
        std::env::set_var("CONTROL_API_KEY", "");
        let cfg = Config::load().unwrap();
        assert_eq!(
            cfg.control_key(),
            "",
            "a key in pipeline.env must not authenticate control requests"
        );
        std::env::set_var("CONTROL_API_KEY", "env-layer-key");
        assert_eq!(cfg.control_key(), "env-layer-key");
        match prev_key {
            Some(v) => std::env::set_var("CONTROL_API_KEY", v),
            None => std::env::remove_var("CONTROL_API_KEY"),
        }
        if let Some(v) = prev_file {
            std::env::set_var("CONTROL_API_KEY_FILE", v);
        }
        match prev_dir {
            Some(v) => std::env::set_var("ASRSUB_CONFIG_DIR", v),
            None => std::env::remove_var("ASRSUB_CONFIG_DIR"),
        }
    }
}
