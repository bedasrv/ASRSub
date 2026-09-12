//! Runtime configuration: `pipeline.env` + `config.overrides.json` + process env.
//!
//! Precedence (highest wins): process env > overrides file > pipeline.env > defaults.
//! Secrets are never logged; [`Config::masked`] redacts them for `/config` output.

use std::borrow::Cow;
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
                value = %mask_for_log(v),
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
                value = %mask_for_log(v),
                "unparseable number in config; using the default ({default})"
            );
            default
        }),
        None => default,
    }
}

/// How many percent-escape layers [`carries_credential_colon`] peels back before
/// giving up. Eight is far past any value a person or a config generator writes,
/// and the doc names the bound rather than claiming "any depth": a fourth-layer
/// escape used to be published while the doc promised it was caught.
const PERCENT_ESCAPE_LAYERS: usize = 8;

/// Schemes whose `:` separates a scheme from an opaque payload rather than a
/// user name from a password. Without this list the scheme-less branch below
/// reads `mailto:admin@example.com` as a login and publishes `***:***@example.com`.
const NON_HIERARCHICAL_SCHEMES: &[&str] = &[
    "mailto", "data", "urn", "tel", "sms", "geo", "magnet", "bitcoin",
];

/// A colon that can separate a user name from a password, written any of the
/// ways a value can carry one: a literal `:`, a percent-encoded `%3A` up to
/// [`PERCENT_ESCAPE_LAYERS`] layers deep, an HTML entity (`&#58;`, `&#x3a;`,
/// `&colon;`, semicolon optional and any case) or the full-width `\u{FF1A}` /
/// small `\u{FE55}`. Matching only the literal byte published
/// `https:///user%3Apw@host` verbatim while the literal spelling of the same
/// value was masked, so the empty-authority class was only half closed.
fn carries_credential_colon(s: &str) -> bool {
    fn colon_like(s: &str) -> bool {
        if s.contains(':') || s.contains('\u{FF1A}') || s.contains('\u{FE55}') {
            return true;
        }
        let lower = s.to_ascii_lowercase();
        // Semicolon-less forms count too: `&colon` and `&#58` are what a sloppy
        // HTML encoder emits, and the trailing `;` is not what makes it a colon.
        entity_colon(&lower)
    }

    if colon_like(s) {
        return true;
    }
    // Escape layers: `%3A` is a colon and `%253A` is one layer further down; the
    // same credential can also arrive with the ampersand of an entity encoded
    // (`&amp;#58`), so each round tries both spellings and stops when neither
    // decodes.
    let mut current = s.to_string();
    for _ in 0..PERCENT_ESCAPE_LAYERS {
        match percent_decode_once(&current).or_else(|| entity_decode_once(&current)) {
            Some(decoded) => {
                if colon_like(&decoded) {
                    return true;
                }
                current = decoded;
            }
            None => break,
        }
    }
    false
}

/// Whether an HTML entity spelling of `:` appears, with or without the semicolon a
/// sloppy encoder drops.
///
/// The numeric forms count on the bare prefix, with no guard on the character
/// that follows, because the ambiguity cannot be resolved from the text: `&#580`
/// is U+0244 and `&#x3afb` is a CJK ideograph, but `&#58pw` is a colon whose
/// encoder dropped the semicolon. Guarding on the next character traded a
/// credential-free over-mask for a fail-open — it published
/// `r?u=svc&#x3aabc@inner` and `x&#581234@inner`, whose passwords begin with a
/// hex or a decimal digit, while the docs still claimed fail-closed masking.
/// Ambiguity resolves by masking, so the credential-free `&#580` is the price.
fn entity_colon(lower: &str) -> bool {
    lower.contains("&#58") || lower.contains("&#x3a") || lower.contains("&colon")
}

/// Whether the `#` at byte `i` of `s` introduces an HTML entity rather than a
/// fragment: `&#58`, `&#x3a`, or one layer deeper (`&amp;#58`, `&#38;#58`).
///
/// The authority split reads the first `/`, `?` or `#` as the end of the
/// authority, so a `#` that belongs to an entity moved the split past the
/// password: `a@b&#58Zk1P@host.lan` was split as `a@b` plus
/// `&#58Zk1P@host.lan`, the entity's `&`-run was declared the host, and the whole
/// credential was published by the unauthenticated `/config`. A `#` directly
/// after an `&`, or after an ampersand written as an entity, is an entity
/// introducer.
fn hash_starts_entity(s: &str, i: usize) -> bool {
    let before = &s[..i];
    if before.ends_with('&') {
        return true;
    }
    let trimmed = before.trim_end_matches(';').to_ascii_lowercase();
    trimmed.ends_with("&amp") || trimmed.ends_with("&#38") || trimmed.ends_with("&#x26")
}

/// Byte index of the first path/query/fragment separator in `s`, ignoring a `#`
/// that belongs to an entity (see [`hash_starts_entity`]).
fn first_separator(s: &str) -> Option<usize> {
    s.char_indices().find_map(|(i, c)| match c {
        '/' | '?' => Some(i),
        '#' if !hash_starts_entity(s, i) => Some(i),
        _ => None,
    })
}

/// One layer of entity-decoding for the ampersand (`&amp;` / `&#38;` -> `&`), or
/// `None` when the value carries no such escape. `&amp;#58pw@host` is the same
/// credential as `&#58pw@host`, one encoding layer further down.
fn entity_decode_once(s: &str) -> Option<String> {
    if !s.contains('&') {
        return None;
    }
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    let mut decoded = false;
    while let Some(i) = rest.find('&') {
        let tail = &rest[i..];
        let lower = tail.to_ascii_lowercase();
        // The semicolon is optional in the spelling a sloppy encoder emits, and
        // HTML5 accepts the legacy `&amp` without one: `&amp#58` is then a colon
        // one layer down (`a&amp#58s3cr3t@host:6767/x@y` was published whole by
        // the unauthenticated `/config`). A legacy spelling is only read as one
        // when the next character could not continue a name, which is what the
        // HTML5 parser does, so a `&amps` in prose is not rewritten.
        let terminated = ["&amp;", "&#38;", "&#x26;"];
        let legacy = ["&amp", "&#38", "&#x26"];
        let consumed = terminated
            .into_iter()
            .find(|p| lower.starts_with(p))
            .map(str::len)
            .or_else(|| {
                legacy
                    .into_iter()
                    .find(|p| {
                        lower.starts_with(p)
                            && !tail[p.len()..]
                                .chars()
                                .next()
                                .map(|c| c.is_alphanumeric() || c == '=')
                                .unwrap_or(false)
                    })
                    .map(str::len)
            });
        match consumed {
            Some(len) => {
                out.push_str(&rest[..i]);
                out.push('&');
                rest = &tail[len..];
                decoded = true;
            }
            None => {
                let step = tail.chars().next().map(char::len_utf8).unwrap_or(1);
                out.push_str(&rest[..i + step]);
                rest = &rest[i + step..];
            }
        }
    }
    out.push_str(rest);
    decoded.then_some(out)
}

/// One layer of percent-decoding (`%3A` -> `:`), or `None` when the value carries
/// no escape. Bytes are decoded one at a time, so a multi-byte sequence becomes
/// mojibake; that is fine here — the only question this answers is whether a
/// colon hides behind an escape.
fn percent_decode_once(s: &str) -> Option<String> {
    if !s.contains('%') {
        return None;
    }
    let bytes = s.as_bytes();
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    let mut decoded = false;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hi = (bytes[i + 1] as char).to_digit(16);
            let lo = (bytes[i + 2] as char).to_digit(16);
            if let (Some(hi), Some(lo)) = (hi, lo) {
                if let Some(c) = char::from_u32(hi * 16 + lo) {
                    out.push(c);
                    i += 3;
                    decoded = true;
                    continue;
                }
            }
        }
        out.push(bytes[i] as char);
        i += 1;
    }
    decoded.then_some(out)
}

/// Whether `prefix` — the text in front of the first `://` — is a URL scheme.
///
/// `split_once("://")` finds the first occurrence *anywhere*, so without this
/// check a scheme-less `user:pw@host/redir?url=http://x@y` was parsed as though
/// `user:pw@host/redir?url=http` were the scheme: the credential in front of the
/// first `@` was never examined and was published while only the embedded URL
/// was masked.
fn is_scheme(prefix: &str) -> bool {
    let mut chars = prefix.chars();
    match chars.next() {
        Some(c) if c.is_ascii_alphabetic() => {}
        _ => return false,
    }
    chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'))
}

/// Mask a configuration value for a log line.
///
/// `/config` masks credentials, but a log line is a second sink for the same
/// value: a warning that echoed a configured value verbatim printed
/// `prefix=user@nas.lan:6767/redir?url=http://svc:PWZ9K@inner` to stderr. Every
/// site that logs a configuration value goes through this so both sinks agree,
/// and a value that carries nothing is returned borrowed, so the common case
/// costs no allocation.
pub fn mask_for_log(value: &str) -> Cow<'_, str> {
    match redact_userinfo(value) {
        Some(masked) => Cow::Owned(masked),
        None => Cow::Borrowed(value),
    }
}

/// Path of the single-instance lock for `state_file`.
///
/// The state path is operator input and this lock is a real file on disk, so a
/// credential-shaped name would be written into a filename. The lock therefore
/// sits beside the state file under its *masked* name, and only the file name is
/// masked: masking the whole path let the run-widening eat the leading directory
/// and drop the lock into the working directory (`/x/user:pw@host/sub/st.jsonl`
/// became `***:***@host/sub/st.daemon.lock`, a relative path).
///
/// A credential-free name (every path an operator actually writes) comes back
/// unchanged, so the one-daemon-per-state guard still keys on the real path. When
/// masking does change the name, two different credentials would collapse onto
/// one file — which would let a second daemon pass the guard — so the masked name
/// also carries a short digest of the real path.
pub fn lock_path(state_file: &Path) -> PathBuf {
    let name = state_file
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let masked = mask_for_log(&name).into_owned();
    if masked == name {
        return state_file.with_extension("daemon.lock");
    }
    let digest = fnv1a64(state_file.display().to_string().as_bytes());
    // `with_file_name` keeps the state file's own directory: a relative path stays
    // relative (a bare `state.jsonl` keeps its lock in the working directory) and
    // an absolute one stays absolute.
    state_file.with_file_name(format!("{masked}.{digest:016x}.daemon.lock"))
}

/// FNV-1a over the bytes of a path, used only to keep two masked lock names
/// apart. Credential-free by construction: the digest is what the file name
/// shows instead of the credential it stands for.
fn fnv1a64(bytes: &[u8]) -> u64 {
    let mut hash = 0xcbf2_9ce4_8422_2325u64;
    for b in bytes {
        hash ^= u64::from(*b);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash
}

/// Whether `text` reads as a host with an optional port: no colon at all, one
/// colon whose tail is all digits, or a *real* bracketed IPv6 literal
/// (`[::1]`, `[fe80::1%eth0]:8080`) whose colons belong to the address. A colon
/// in this position is a port, not evidence of a credential, so a run must never
/// start inside it. Malformed shapes — an empty host (`:8080`), an empty port
/// (`host:`), a second colon (`host:1:2`) — are `false`, so the caller keeps the
/// fail-closed fallback and masks; the readable host is that price.
///
/// Two spellings are `false` even though they look like a host, because treating
/// them as one handed the tail scan a floor that hid a password:
///
/// * A colon written any other way inside the candidate (`%3A`, an entity, the
///   full-width `\u{FF1A}`) is credential evidence, not a port: `b%3AZk3P` and
///   `b&#58Zk1P` were read as hosts, and the credential behind them was published
///   by the unauthenticated `/config`.
/// * A bracket is not a licence to hide a `user:pass`: only text that parses as an
///   IPv6 address (an optional `%zone` aside) counts, so `user@[root:s3cr3t]/x@y`
///   keeps the fail-closed fallback instead of publishing `root:s3cr3t`.
fn is_host_port(text: &str) -> bool {
    let port_ok = |port: &str| {
        !port.is_empty() && !port.contains(':') && port.bytes().all(|b| b.is_ascii_digit())
    };
    // A colon in any other spelling means this is a credential, not a host.
    let hidden_colon = |part: &str| {
        part.contains('%')
            || part.contains('&')
            || part.contains('\u{FF1A}')
            || part.contains('\u{FE55}')
    };
    if let Some(rest) = text.strip_prefix('[') {
        // Bracketed IPv6 literal, optionally followed by a port.
        let Some((literal, tail)) = rest.split_once(']') else {
            return false;
        };
        let address = literal.split('%').next().unwrap_or("");
        if address.parse::<std::net::Ipv6Addr>().is_err() {
            return false;
        }
        return match tail.strip_prefix(':') {
            None => tail.is_empty(),
            Some(port) => port_ok(port),
        };
    }
    match text.split_once(':') {
        None => !text.is_empty() && !hidden_colon(text),
        Some((host, port)) => !host.is_empty() && !hidden_colon(host) && port_ok(port),
    }
}

/// Mask every credential-shaped run in `tail`, the part of a value that follows
/// the authority's userinfo.
///
/// For each `@`, candidates for the start of the credential are taken right to
/// left — after the innermost `scheme://`, after the last `/`, `?` or `#`, then
/// the start of the segment — and the rightmost candidate whose run carries a
/// colon is the one masked. Widening leftwards when the narrowest run has no
/// colon is what catches a password that contains a separator
/// (`er:p/ss@host`, `?to=svc:pa/ss@inner`), which a fixed one-separator rule
/// published. A run that never carries a colon is a plain address or path
/// (`?to=nominal@host`, `b.mkv`) and is left as it stands, so a redirected URL
/// stays legible where it can: `...?url=http://***:***@inner`.
///
/// `floor` is the width of the `host[:port]` that follows the userinfo: the first
/// index at which a run may start for the first `@`, re-based as `rest` advances,
/// so a run never begins inside the host. Without it a port colon was read as a
/// credential and the widening swallowed the readable host of an already-masked
/// authority (`https://user@host:6767/x@y` -> `https://***@***:***@y`). The
/// price is symmetric and named here rather than discovered later: a digit-only
/// password sitting in the host slot is published with the host it is mistaken
/// for (`https://u@svc:1234/x@y`), because the two spellings are identical and
/// keeping the readable host is why the floor exists. `0` when the value has no
/// authority to exclude. `None` when nothing in `tail` is masked.
fn mask_tail_runs(tail: &str, floor: usize) -> Option<String> {
    if !tail.contains('@') {
        return None;
    }
    let mut out = String::with_capacity(tail.len());
    let mut rest = tail;
    let mut masked = false;
    let mut floor = floor;
    while let Some(at) = rest.find('@') {
        let head = &rest[..at];
        let mut starts: Vec<usize> = vec![floor.min(head.len())];
        for (i, _) in head.match_indices("://") {
            starts.push(i + 3);
        }
        for (i, c) in head.char_indices() {
            if matches!(c, '/' | '?' | '#') {
                starts.push(i + 1);
            }
        }
        starts.sort_unstable_by(|a, b| b.cmp(a));
        starts.dedup();
        let start = starts
            .into_iter()
            .find(|&s| !head[s..].is_empty() && carries_credential_colon(&head[s..]));
        match start {
            Some(s) => {
                out.push_str(&head[..s]);
                out.push_str("***:***");
                masked = true;
            }
            None => out.push_str(head),
        }
        out.push('@');
        rest = &rest[at + 1..];
        floor = floor.saturating_sub(at + 1);
    }
    out.push_str(rest);
    masked.then_some(out)
}

/// `scheme://user:pass@host/path` → `scheme://***:***@host/path`, and the same
/// for a scheme-less `user:pass@host`.
///
/// `/config` is answered without authentication, so a credential embedded in an
/// otherwise harmless key (a service URL whose name carries no KEY/TOKEN hint)
/// must not be published either. Two properties matter:
///
/// * **Fail closed.** Anything that carries a colon before the `@` that ends the
///   userinfo is masked, whatever the host looks like: a trailing space, a
///   non-ASCII or percent-encoded host, an alphabetic port, an empty host, an
///   empty authority (`https:///root:1234@nas.lan`), a UNC path, an IPv6 zone id,
///   a password that begins with digits and contains a separator. Host shape
///   decides only whether a `@` sits in the path or the query, and even then only
///   when *neither* the text in front of the separator *nor* the text in front of
///   the `@` could be a `user:pass` pair — `https://user:1/2@host` is
///   indistinguishable from `https://host:port/path@x`, so it is masked, and so
///   is the harmless `https://bazarr.lan:6767/api?x=a@b`. Losing a readable base
///   URL costs information; publishing `root:1234` costs the credential. The
///   colon may be spelled literally, percent-encoded up to
///   [`PERCENT_ESCAPE_LAYERS`] layers deep, as an HTML entity or as the full-width
///   `\u{FF1A}`, one encoding layer deeper (`&amp;#58`), and with the legacy
///   semicolon-less `&amp` that HTML5 accepts (`&amp#58` — the same credential one
///   layer down, and decoding only the terminated spelling published
///   `a&amp#58s3cr3t@host:6767/x@y` whole). A numeric entity counts whatever
///   follows it: `&#580` is U+0244 and `&#x3afb` a CJK ideograph, but guarding on
///   the next character published `svc&#x3aabc@inner`, so the credential-free
///   `&#580` is the price of fail-closed masking. The slot right after the `@` is
///   part of the authority and is checked too, not merely excluded from the
///   widening: brackets that do not hold an IPv6 literal (`[root:s3cr3t]`,
///   `[svc:pw]`) and a colon there in any other spelling (`b&#58Zk1P`,
///   `b%3AZk3P`) are masked with the name. A *malformed port* keeps its colon
///   where a colon belongs, so `sonarr.lan:http` stays readable.
/// * **The userinfo is the one inside the authority** when the value has an
///   authority, and the last `@` otherwise, so neither a password containing
///   `/`, `?` or `#` nor an `@` inside a password survives unmasked.
/// * **Every credential-shaped run is masked**, not just the authority's: the
///   tail is scanned too (`mask_tail_runs`), so a value that embeds a second
///   credentialed URL in its own path or query
///   (`https://user:pw@gw.lan/redirect?url=http://a:b@c`) masks both pairs and
///   stays legible: `https://***:***@gw.lan/redirect?url=http://***:***@c`. A run
///   with no colon is an address or a path, not a credential
///   (`?to=nominal@host`), and is left as it stands. The run widens leftwards
///   only while it has no colon, so the readable prefix survives wherever it
///   honestly can and a password containing `/`, `?` or `#` no longer hides
///   behind the separator. The host[:port] after the userinfo is excluded from
///   that widening (`is_host_port`), so a port colon cannot justify masking the
///   readable host it belongs to.
///
/// A scheme-less value is masked only when a colon marks real credentials, which
/// leaves plain `user@host` (an email address) untouched, and a non-hierarchical
/// scheme word (`mailto:admin@example.com`, `urn:isbn:…@x`) with no port after
/// the host is left alone: it has no password to hide, and masking it would
/// rewrite every email address. Documented trade-off — a credential whose user
/// name is literally `mailto`/`data`/`tel`/… and whose host carries no port is
/// read as that URI and published. A colon-free name is not the end of the check:
/// the value's tail is scanned in the same breath, so
/// `nominal@host/redir?url=http://svc:pw@inner` masks `svc:pw` while the bare name
/// stays readable.
///
/// The authority ends at the first `/`, `?` or `#` **that is not part of an
/// entity**: a `#` in `&#58` is an entity introducer, so a name carrying an
/// escaped colon keeps the credential on the userinfo side of the split instead
/// of publishing it in a "tail" the scan never treats as a credential.
///
/// A *colon-free* authority-less userinfo is published
/// (`https://///pw@host`, `https:///path@x`) — the extra slashes make it a user
/// name with no password, and masking every path that contains an `@` would be
/// the wrong default. A *scheme-qualified* bare user name is masked down to `***`
/// even though it carries no password (`ssh://git@github.com/owner/repo`), because
/// a scheme says the value has an authority and the name in it is replaced; the
/// scheme-less `user@host` spelling is left alone, and that inconsistency is
/// deliberate — a `git@` in a clone URL is not a credential, but telling the two
/// apart without publishing one by mistake is not possible. `None` when the value
/// carries no credential-shaped userinfo.
fn redact_userinfo(value: &str) -> Option<String> {
    // A trailing space or newline is a copy/paste artefact, not part of a host.
    let value = value.trim();
    let (prefix, rest) = match value.split_once("://") {
        Some((scheme, rest)) if is_scheme(scheme) => (format!("{scheme}://"), rest),
        _ => (String::new(), value),
    };
    // Where the authority ends: the first path/query/fragment separator, with a
    // `#` that belongs to an entity ignored (`a@b&#58Zk1P@host.lan`).
    let sep = first_separator(rest);
    // A credential inside the authority is separated by the *last* `@` before
    // that separator (the WHATWG authority split, and the fail-closed choice:
    // `user@user@host` loses both names rather than one); anything later is a
    // path or query character. When the authority holds no `@` the last `@` in
    // the value is the candidate — and the branch below may still drop it as a
    // path/query character.
    let at = match sep {
        Some(s) if rest[..s].contains('@') => rest[..s].rfind('@')?,
        _ => rest.rfind('@')?,
    };
    let userinfo = &rest[..at];
    let after = &rest[at + 1..];
    let host = match first_separator(after) {
        Some(i) => &after[..i],
        None => after,
    };
    // The text immediately after the userinfo is the host slot, and it is part of
    // the authority: a host carries at most one colon, as a port. `is_host_port`
    // alone was not enough — it only kept the tail scan's widening out of that
    // text, and every candidate a scan takes is to its *right*, so whenever the
    // userinfo in front was the credential-shaped part the slot rode through:
    // `a&#58Zk1P@[root:s3cr3t]/redir?url=http://a:b@c` published `root:s3cr3t`
    // from the unauthenticated `/config`.
    //
    // Only two spellings make the slot a credential rather than a host with a
    // broken port: brackets that do not hold an IPv6 literal (`[root:s3cr3t]`,
    // `[svc:pw]`), and a colon that is not the literal port colon (a percent
    // escape, an entity, the full-width form). A malformed *port*
    // (`sonarr.lan:http`, `host:`) has its colon where a colon belongs, so it
    // stays readable — masking every host that fails to parse would cost real
    // base URLs for nothing.
    let escaped_colon = carries_credential_colon(&host.replace(':', ""));
    let host_hidden = !host.contains('@')
        && !is_host_port(host)
        && (escaped_colon || (host.starts_with('[') && host.contains(':')));
    // The mask replaces the slot *and* the credential run that continues past it,
    // up to the `@` that ends this authority: a password may straddle the
    // separator (`er:p/ss@host`), and splitting at the separator published `ss`.
    let scan_owned;
    let after: &str = if host_hidden {
        scan_owned = match after.find('@') {
            Some(i) => format!("***:***{}", &after[i..]),
            None => format!("***:***{}", &after[host.len()..]),
        };
        &scan_owned
    } else {
        after
    };
    // A readable host[:port] is still excluded from the widening, so a port colon
    // cannot justify masking the host it belongs to
    // (`https://user@host:6767/x@y` -> `https://***@***:***@y`); a hidden slot is
    // excluded by its own mask's width for the same reason.
    let floor = if host_hidden {
        "***:***".len()
    } else if is_host_port(host) {
        host.len()
    } else {
        0
    };
    // A hidden host slot is already a mask, so it must count as one even when
    // nothing in the tail needed masking: returning `None` there published the
    // whole value, and with it the credential the slot mask had just replaced.
    let finish = |tail: Option<String>, original: &str| -> Option<String> {
        match tail {
            Some(t) => Some(t),
            None if host_hidden => Some(original.to_string()),
            None => None,
        }
    };
    if userinfo.is_empty() {
        // No userinfo in the authority, so there is nothing to replace there —
        // but the value can still embed a credential later
        // (`http://@host.lan/a:b@c`). The tail scan decides; `None` when it finds
        // nothing either.
        return finish(mask_tail_runs(after, floor), after).map(|tail| format!("{prefix}@{tail}"));
    }
    if prefix.is_empty() {
        // Scheme-less: a colon is what distinguishes `user:pass@host` from a
        // bare address, and the listed scheme words have no password at all.
        if !carries_credential_colon(userinfo) {
            // The name carries no password, so nothing before the `@` is a
            // credential — but the value can still embed one after it, which the
            // split reads as a host when the first `@` came first
            // (`nominal@host/redir?url=http://svc:pw@inner`,
            // `us@er:p/ss@host.lan`). Returning `None` here published that whole
            // family verbatim; the tail scan decides instead, and `None` when it
            // finds nothing keeps `user@sonarr.lan` and `noreply@example.com`
            // untouched.
            return finish(mask_tail_runs(after, floor), after)
                .map(|tail| format!("{prefix}{userinfo}@{tail}"));
        }
        let word = userinfo
            .split(':')
            .next()
            .unwrap_or("")
            .to_ascii_lowercase();
        if NON_HIERARCHICAL_SCHEMES.contains(&word.as_str()) && !host.contains(':') {
            return None;
        }
    } else if let Some(s) = sep {
        // A `@` past the authority is a path or query character, not a userinfo —
        // unless the text in front of that separator could be a `user:pass` pair,
        // which is what `https://root:1234/secret@nas.lan` looks like, or the text
        // in front of the `@` itself could be one, which is what an authority-less
        // `https:///root:1234@nas.lan` looks like: there the separator sits at
        // index 0, so the empty candidate carried no colon and the value was
        // published verbatim. Ambiguity is resolved by masking.
        if s < at && !carries_credential_colon(&rest[..s]) && !carries_credential_colon(&rest[..at])
        {
            return None;
        }
    }
    let masked = if carries_credential_colon(userinfo) {
        "***:***"
    } else {
        "***"
    };
    let after = match finish(mask_tail_runs(after, floor), after) {
        Some(tail) => tail,
        None => after.to_string(),
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
        // comes from `pipeline.env`. Candidate *files* are tried first, so an
        // empty (or whitespace-only) variable contributes no key and denies
        // control access (`check_token` refuses an empty key) only when no key
        // file exists: a key file outranks this variable, and disabling the
        // control API takes removing the file as well.
        env_str("CONTROL_API_KEY").unwrap_or_default()
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

    /// Process environment is the highest-precedence config layer, so a test that
    /// asserts a **file** layer value only proves what it claims on a machine that
    /// does not export that key: with `TARGET_LANGS=id` left behind by a manual
    /// daemon run, `write_overrides_merges_and_round_trips` fails on correct code
    /// and `cargo test` goes red for a reason that has nothing to do with the
    /// commit under review. Scrub every pipeline key for the duration of such a
    /// test and put the environment back on drop. Any test that mutates a key
    /// itself must still hold `ENV_LOCK`; this guard only removes *ambient* values.
    struct ConfigEnvScrubbed(Vec<(String, Option<std::ffi::OsString>)>);

    impl ConfigEnvScrubbed {
        fn new() -> Self {
            let keys: Vec<String> = ENV_ALLOWLIST
                .iter()
                .chain(ENV_ONLY_KEYS.iter())
                .map(|k| (*k).to_string())
                .collect();
            let saved = keys
                .iter()
                .map(|k| (k.clone(), std::env::var_os(k)))
                .collect();
            for k in &keys {
                std::env::remove_var(k);
            }
            Self(saved)
        }
    }

    impl Drop for ConfigEnvScrubbed {
        fn drop(&mut self) {
            for (k, v) in &self.0 {
                match v {
                    Some(v) => std::env::set_var(k, v),
                    None => std::env::remove_var(k),
                }
            }
        }
    }

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
        //
        // This test sets `TARGET_LANGS`, an allowlisted key, so it must hold
        // `ENV_LOCK`: without it the value leaks into every test running in
        // parallel, and a test asserting the *file* layer sees the environment win
        // instead — the intermittent `["id"] != ["id","en","es"]` failure.
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
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
        let _scrub = ConfigEnvScrubbed::new();
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
        let _scrub = ConfigEnvScrubbed::new();
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
        let _scrub = ConfigEnvScrubbed::new();
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
        let _scrub = ConfigEnvScrubbed::new();
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
        // A password that begins with digits reads as `host:port` at the first
        // separator — the exact regression this test exists for. `user:1/2@host`
        // is spelled like `host:6767/path@x`, and `/config` is unauthenticated,
        // so the ambiguity is resolved by masking.
        assert_eq!(
            redact_userinfo("https://user:1/2@host.lan").as_deref(),
            Some("https://***:***@host.lan")
        );
        assert_eq!(
            redact_userinfo("https://root:1234/secret@nas.lan").as_deref(),
            Some("https://***:***@nas.lan")
        );
        assert_eq!(
            redact_userinfo("https://admin:1234?x@host.lan").as_deref(),
            Some("https://***:***@host.lan")
        );
        // A credential inside the authority is masked even when a second `@`
        // follows it in the path: the last `@` before the separator ends the
        // userinfo, and the tail is kept.
        assert_eq!(
            redact_userinfo("https://user:s3cr3t@host.lan/a@b").as_deref(),
            Some("https://***:***@host.lan/a@b")
        );
        // A `@` past the authority is a path/query character — and may be left
        // alone only when nothing in front of the separator could be a
        // `user:pass` pair. No colon, no credential.
        assert_eq!(redact_userinfo("https://bazarr.lan/api?x=a@b"), None);
        assert_eq!(redact_userinfo("https://bazarr.lan:6767/x"), None);
        // ... but a port makes `bazarr.lan:6767/api?x=a` indistinguishable from a
        // user name and password, so that one is masked (over-masking is the
        // safe direction; a real base URL carries no `@` in its query).
        assert_eq!(
            redact_userinfo("https://bazarr.lan:6767/api?x=a@b").as_deref(),
            Some("https://***:***@b")
        );
        // Colon-free tails are left alone: a path `@` under the `file:` scheme
        // (which allows an empty host), and an authority-less user name, where the
        // extra slashes make it a name with no password in it. Neither the text
        // before the separator nor the text before the `@` carries a colon.
        assert_eq!(redact_userinfo("file:///mnt/nas/a@b.mkv"), None);
        assert_eq!(redact_userinfo("https:///path@x"), None);
        // An *empty authority* is the case that made "nothing in front of the
        // separator carries a colon" true by construction, so `https:///user:pw@host`
        // was published verbatim while `file:///path@x` stayed intact — they are
        // told apart by the colon in front of the `@`, which wins. `/config` is
        // answered without authentication, so this is the fail-closed direction.
        for (value, want) in [
            ("https:///user:pw@host.lan", "https://***:***@host.lan"),
            ("file:///user:pw@host.lan", "file://***:***@host.lan"),
            ("smb:///user:pw@server/share", "smb://***:***@server/share"),
            ("http:////user:pw@host.lan", "http://***:***@host.lan"),
            ("https://?x=user:pw@host.lan", "https://***:***@host.lan"),
            ("https://#user:pw@host.lan", "https://***:***@host.lan"),
            // ... and the same shape with the credential in a path segment;
            // neither this commit nor its parent used to mask it.
            ("https://nas.lan/root:1234@host", "https://***:***@host"),
        ] {
            assert_eq!(
                redact_userinfo(value).as_deref(),
                Some(want),
                "credential published verbatim: {value:?}"
            );
        }
        // A scheme-qualified bare user name is masked by design (no colon before
        // the `@`, so it is `***`, not `***:***`), and the path `@` is kept.
        assert_eq!(
            redact_userinfo("https://user@bazarr.lan/api?x=a@b").as_deref(),
            Some("https://***@bazarr.lan/api?x=a@b")
        );
    }

    /// Every credential-shaped run is masked, not only the authority's: the tail
    /// is scanned too. A run starts after the innermost `scheme://` in front of the
    /// `@`, or after the last path/query separator when there is none, so the
    /// readable prefix survives and a redirected URL stays legible. This replaced a
    /// documented limitation — the tail used to be copied as it stood, which
    /// published an inner `svc:pw` from unauthenticated `/config`.
    #[test]
    fn a_second_credential_in_the_tail_is_masked_too() {
        assert_eq!(
            redact_userinfo("https://user:pw@gw.lan/redirect?url=http://a:b@c").as_deref(),
            Some("https://***:***@gw.lan/redirect?url=http://***:***@c")
        );
        assert_eq!(
            redact_userinfo("https://nominal@host.lan/redir?url=http://svc:pw@inner.lan")
                .as_deref(),
            Some("https://***@host.lan/redir?url=http://***:***@inner.lan")
        );
        assert_eq!(
            redact_userinfo("https://user:pw@h1/a:b@h2/c:d@h3").as_deref(),
            Some("https://***:***@h1/***:***@h2/***:***@h3")
        );
        // A tail password containing a separator is caught as well, because the run
        // starts after the `scheme://` and not after that `/`.
        assert_eq!(
            redact_userinfo("https://nominal@host.lan/redir?url=http://svc:pa/ss@inner.lan")
                .as_deref(),
            Some("https://***@host.lan/redir?url=http://***:***@inner.lan")
        );
        // An empty authority publishes nothing itself, but the tail can still
        // carry a credential — this used to return `None` and publish it.
        assert_eq!(
            redact_userinfo("http://@host.lan/a:b@c").as_deref(),
            Some("http://@host.lan/***:***@c")
        );
        assert_eq!(
            redact_userinfo("https://@host.lan/root:1234@nas.lan").as_deref(),
            Some("https://@host.lan/***:***@nas.lan")
        );
        // A base URL that merely embeds another URL, with no colon in front of a
        // `@`, is still untouched.
        assert_eq!(
            redact_userinfo("https://gw.lan/redirect?url=http://nominal.lan/x"),
            None
        );
        assert_eq!(
            redact_userinfo("https://user@host.lan/api?x=a@b").as_deref(),
            Some("https://***@host.lan/api?x=a@b")
        );
        assert_eq!(redact_userinfo("http://@host.lan"), None);
        // A *scheme-less* tail credential whose user name contains a separator is
        // masked too, because the run widens leftwards until it carries a colon.
        assert_eq!(
            redact_userinfo("https://nominal@host.lan/redir?to=a:b/c@d").as_deref(),
            Some("https://***@host.lan/redir?***:***@d")
        );
    }

    /// A user name that itself contains an `@` must not hide the password. The
    /// authority split takes the *last* `@` inside the authority, so everything
    /// after the first one is a tail credential and is scanned as one. Round eight
    /// published this whole family — 372 of its corpus's leak hits, closed at
    /// `ae6efa2`, reopened by `c45f091` and still open in `099ed42`.
    #[test]
    fn an_at_sign_inside_the_user_name_does_not_publish_the_password() {
        assert_eq!(
            redact_userinfo("https://us@er:p/ss@host.lan").as_deref(),
            Some("https://***@***:***@host.lan")
        );
        assert_eq!(
            redact_userinfo("https://user@corp.lan:s3cr3t@nas.lan").as_deref(),
            Some("https://***:***@nas.lan")
        );
        assert_eq!(
            redact_userinfo("https://user@corp.lan:s3cr3t/x@nas.lan").as_deref(),
            Some("https://***@***:***@nas.lan")
        );
        for value in ["https://us@er:p?ss@host.lan", "https://us@er:p#ss@host.lan"] {
            let out = redact_userinfo(value).unwrap_or_else(|| value.to_string());
            assert!(
                !out.contains("p?ss") && !out.contains("p#ss"),
                "password published: {out:?}"
            );
        }
    }

    /// `split_once("://")` finds the first `://` *anywhere*, so a value with no
    /// scheme whose own text embeds a URL used to be read as though the text in
    /// front of that `://` were the scheme: the credential in front of the first
    /// `@` was never examined and was published. `is_scheme` now decides, and the
    /// embedded URL is still masked as a tail run.
    #[test]
    fn a_scheme_less_value_carrying_a_url_still_masks_its_own_credential() {
        assert_eq!(
            redact_userinfo("admin:SECRET@host.lan/redir?url=http://a:b@c").as_deref(),
            Some("***:***@host.lan/redir?url=http://***:***@c")
        );
        assert_eq!(
            redact_userinfo("user:pw@host.lan?next=https://example.com").as_deref(),
            Some("***:***@host.lan?next=https://example.com")
        );
        assert_eq!(
            redact_userinfo("root:1234@nas.lan/path/https://y").as_deref(),
            Some("***:***@nas.lan/path/https://y")
        );
    }

    /// The split takes the first `@` when it sits before the first separator, so a
    /// scheme-less value whose user name contains an `@` — or whose tail carries a
    /// credential — used to leave the colon-free branch without ever scanning the
    /// tail and was published verbatim. Round nine's differential fuzz counted 784
    /// such values at `58b316b`. Five of its samples are pinned here.
    #[test]
    fn a_scheme_less_name_with_a_credentialed_tail_is_still_masked() {
        assert_eq!(
            redact_userinfo("nominal@host/redir?url=http://svc:pw@inner").as_deref(),
            Some("nominal@host/redir?url=http://***:***@inner")
        );
        assert_eq!(
            redact_userinfo("git@host.lan/redir?url=svc:pw@inner.lan").as_deref(),
            Some("git@host.lan/redir?***:***@inner.lan")
        );
        assert_eq!(
            redact_userinfo("us@er:p/ss@host.lan").as_deref(),
            Some("us@***:***@host.lan")
        );
        assert_eq!(
            redact_userinfo("user@nas.lan:6767/redir?url=http://svc:PWZ9K@inner").as_deref(),
            Some("user@nas.lan:6767/redir?url=http://***:***@inner")
        );
        assert_eq!(
            redact_userinfo("a@b?u=x://svc:PWZ9K@inner").as_deref(),
            Some("a@b?u=x://***:***@inner")
        );
        // The bare addresses the scheme-less rule exists to protect stay published.
        assert_eq!(redact_userinfo("user@sonarr.lan"), None);
        assert_eq!(redact_userinfo("noreply@example.com"), None);
    }

    /// A port colon is not credential evidence. Widening from the start of the tail
    /// read `host:6767` as a `user:pass` pair, so an already-masked authority lost
    /// its readable host and port (`https://user@host:6767/x@y` became
    /// `https://***@***:***@y`).
    #[test]
    fn the_host_port_is_not_credential_evidence() {
        assert_eq!(
            redact_userinfo("https://user@host:6767/x@y").as_deref(),
            Some("https://***@host:6767/x@y")
        );
        assert_eq!(
            redact_userinfo("https://user:pw@host.lan:8080/path@x").as_deref(),
            Some("https://***:***@host.lan:8080/path@x")
        );
        assert_eq!(
            redact_userinfo("https://user@nas.lan:6767/api?apikey=a@b").as_deref(),
            Some("https://***@nas.lan:6767/api?apikey=a@b")
        );
        assert_eq!(redact_userinfo("user@host:6767/x@y"), None);
        // A credential behind the port is still masked.
        assert_eq!(
            redact_userinfo("user@host:6767/svc:pw@inner").as_deref(),
            Some("user@host:6767/***:***@inner")
        );
        // The documented over-mask for a bare `host:port` still stands.
        assert_eq!(
            redact_userinfo("https://bazarr.lan:6767/api?x=a@b").as_deref(),
            Some("https://***:***@b")
        );
        // A bracketed IPv6 literal is a host too, so a credential-free value
        // keeps it instead of losing it to the tail scan.
        assert_eq!(redact_userinfo("user@[::1]:8080/x@y"), None);
        assert_eq!(redact_userinfo("user@[fe80::1%eth0]:80/pw@y"), None);
        // The named price of keeping a readable host: a digit-only password in
        // the host slot is published with the host it is mistaken for.
        assert_eq!(
            redact_userinfo("https://u@svc:1234/x@y").as_deref(),
            Some("https://***@svc:1234/x@y")
        );
    }

    /// `/config` is not the only sink for these values: a warning that echoed a
    /// configured value published it to stderr, so log sites mask too.

    #[test]
    fn an_entity_colon_inside_a_name_does_not_hide_the_password() {
        // The authority split read the `#` in `&#58` as a fragment boundary, so the
        // credential landed in a "tail" that the scan never treats as a credential
        // and the unauthenticated `/config` published it verbatim. A `#` that
        // belongs to an entity is an entity introducer, not a fragment.
        for v in [
            "a@b&#58Zk1P@host.lan",
            "a@b&#x3aZk1P@host.lan",
            "a@b&amp;#58Zk1P@host.lan",
            "ftp://a@b&#58Zk1P@host.lan",
            "https://us@er&#58;Zk1P@host.lan",
        ] {
            let out = redact_userinfo(v).expect(v);
            assert!(!out.contains("Zk1P"), "{v} -> {out}");
        }
        assert_eq!(
            redact_userinfo("a@b&#58Zk1P@host.lan").as_deref(),
            Some("***:***@host.lan")
        );
    }

    #[test]
    fn an_escaped_colon_after_a_name_does_not_hide_the_password() {
        // The same family with the colon written as `%3A`, a named entity or the
        // full-width character: the text in front of the `@` carries no literal
        // colon, so the split chose it as a name and the password was published.
        for v in [
            "a@b%3AZk3P/ss@host.lan",
            "a@b\u{FF1A}Zk3P/ss@host.lan",
            "a@b&colon#Zk1P@host.lan",
            "ftp://a@b%3AZk3P/ss@host.lan",
            "ftp://a@b&colonZk3P/ss@host.lan",
        ] {
            let out = redact_userinfo(v).expect(v);
            assert!(
                !out.contains("Zk3P") && !out.contains("Zk1P"),
                "{v} -> {out}"
            );
        }
    }

    #[test]
    fn only_a_real_ipv6_literal_is_a_bracketed_host() {
        // The host slot is excluded from the credential scan, so a bracket is not
        // a licence to hide a `user:pass`: anything that is not an IPv6 address
        // keeps the fail-closed fallback.
        for v in [
            "user@[root:s3cr3t]/x@y",
            "user@[svc:pw]:80/x@y",
            "https://u@[svc:pw]:8080/x@y",
        ] {
            let out = redact_userinfo(v).expect(v);
            assert!(
                !out.contains("s3cr3t") && !out.contains("svc:pw"),
                "{v} -> {out}"
            );
        }
        // A real literal still counts as a host, so the readable name survives.
        assert_eq!(redact_userinfo("user@[::1]:8080/x@y"), None);
        assert_eq!(redact_userinfo("user@[fe80::1%eth0]:80/pw@y"), None);
    }

    #[test]
    fn a_credential_without_a_host_after_it_is_not_a_pair() {
        // The slot after the `@` is part of the authority, so an escaped colon
        // there is a credential written where a host belongs (`b&#58Zk1P` reads as
        // `b:Zk1P`) and is masked with the name instead of being published.
        assert_eq!(redact_userinfo("a@b&#58Zk1P").as_deref(), Some("a@***:***"));
        // With no `@` at all there is nothing to attach a credential to: the scan
        // needs one.
        assert_eq!(redact_userinfo("r?u=svc:p"), None);
    }
    #[test]
    fn a_legacy_ampersand_hides_a_colon_one_layer_down() {
        // HTML5 accepts the legacy `&amp` without a semicolon, and `&amp#58` is
        // then `&#58`, a colon: decoding only the terminated spelling published
        // `a&amp#58s3cr3t@host:6767/x@y` whole from the unauthenticated `/config`.
        for v in [
            "a&amp#58s3cr3t@host:6767/x@y",
            "a&amp#581234@host:6767/x@y",
            "a&amp#58s3cr3t@[::1]:8080/x@y",
            "a&amp#58%3AZk1P@host:6767/x@y",
        ] {
            let out = redact_userinfo(v).unwrap_or_else(|| v.to_string());
            assert!(
                !out.contains("s3cr3t") && !out.contains("Zk1P"),
                "{v} -> {out}"
            );
            assert!(out.contains("***:***"), "{v} -> {out}");
        }
        // The legacy spelling is only read as an entity when the next character
        // could not continue a name, so a `&amps` in prose is not rewritten.
        assert_eq!(redact_userinfo("https://bazarr.lan/a?x=1&ampy@b"), None);
    }

    #[test]
    fn a_credential_in_the_host_slot_is_masked_with_the_name() {
        // `is_host_port` only kept the tail scan's widening out of the host slot,
        // and every candidate a scan takes is to its right, so whenever the name in
        // front was the credential-shaped part the slot rode through:
        // `a&#58Zk1P@[root:s3cr3t]/redir?url=http://a:b@c` published `root:s3cr3t`.
        for v in [
            "a&#58Zk1P@[root:s3cr3t]/redir?url=http://a:b@c",
            "a&#58Zk1P@[svc:pw]:80/x@y",
            "a&amp#58Zk1P@[root:s3cr3t]?u=svc:p@in",
            "x@b%3AZk3P/ss@host.lan",
        ] {
            let out = redact_userinfo(v).unwrap_or_else(|| v.to_string());
            assert!(
                !out.contains("s3cr3t") && !out.contains("svc:pw") && !out.contains("Zk3P"),
                "{v} -> {out}"
            );
        }
        // A malformed *port* has its colon where a colon belongs, so it stays
        // readable: the rule is for a credential in that slot, not for a port that
        // does not parse.
        assert_eq!(
            redact_userinfo("https://user:pw@sonarr.lan:http").as_deref(),
            Some("https://***:***@sonarr.lan:http")
        );
        // A password that straddles the separator is swallowed with the slot.
        assert_eq!(
            redact_userinfo("https://us@er:p/ss@host.lan").as_deref(),
            Some("https://***@***:***@host.lan")
        );
    }

    #[test]
    fn the_single_instance_lock_name_never_carries_a_credential() {
        // A credential-free path keeps its directory and its stem: this is every
        // path an operator actually writes, so the guard still keys on the real
        // path.
        assert_eq!(
            lock_path(Path::new("/var/lib/asrsub/state.jsonl")),
            PathBuf::from("/var/lib/asrsub/state.daemon.lock")
        );
        assert_eq!(
            lock_path(Path::new("state.jsonl")),
            PathBuf::from("state.daemon.lock")
        );
        // A credential-shaped name is masked *in place*: masking the whole path
        // let the widening eat the leading directory and drop the lock into the
        // working directory instead of beside the state file.
        let lock = lock_path(Path::new("/x/user:pw@host/sub/st.jsonl"));
        assert_eq!(lock, PathBuf::from("/x/user:pw@host/sub/st.daemon.lock"));
        let masked = lock_path(Path::new("user:pa?ss@sonarr"));
        let name = masked.file_name().unwrap().to_string_lossy().into_owned();
        assert!(!name.contains("pa?ss"), "{name}");
        assert!(
            name.starts_with("***:***@sonarr.") && name.ends_with(".daemon.lock"),
            "{name}"
        );
        // Two state paths that differ only inside the credential must not collapse
        // onto one lock name: that would let a second daemon pass the guard.
        assert_ne!(
            lock_path(Path::new("/x/a:pw1@h/st.jsonl")),
            lock_path(Path::new("/x/a:pw2@h/st.jsonl"))
        );
    }

    #[test]
    fn a_config_value_is_masked_before_it_reaches_a_log_line() {
        assert_eq!(mask_for_log("https://user:pw@host"), "https://***:***@host");
        assert_eq!(mask_for_log("user@nas.lan:6767"), "user@nas.lan:6767");
        assert_eq!(
            mask_for_log("user@nas.lan:6767/redir?url=http://svc:pw@inner"),
            "user@nas.lan:6767/redir?url=http://***:***@inner"
        );
        assert!(matches!(mask_for_log("plain"), Cow::Borrowed("plain")));
    }

    /// An entity spelling of the colon masks whatever follows it: the ambiguity
    /// between a longer entity (`&#580` is U+0244, `&#x3afb` is a CJK ideograph)
    /// and a colon whose encoder dropped the semicolon cannot be resolved from
    /// the text, and resolving it the wrong way publishes a password
    /// (`r?u=svc&#x3aabc@inner`, `x&#581234@inner`). Ambiguity resolves by
    /// masking, so a credential-free `&#580` is the price.
    #[test]
    fn an_entity_spelling_of_the_colon_masks_even_when_it_looks_longer() {
        for value in [
            "https://host/a&#580;b@c",
            "https://host/a&#5812;b@c",
            "https://host/a&#x3afb@c",
            "https://gw.lan/r?u=svc&#x3aabc@inner",
            "https://u@host/x&#581234@inner",
            "https:///user&#x3aabc@host.lan",
        ] {
            assert!(
                redact_userinfo(value).is_some(),
                "{value} must be masked, not published"
            );
        }
        for value in [
            "https:///user&#58pw@host.lan",
            "https:///user&colonpw@host.lan",
            "https:///user&#58;pw@host.lan",
            "https:///user&colon;pw@host.lan",
        ] {
            assert_eq!(
                redact_userinfo(value).as_deref(),
                Some("https://***:***@host.lan"),
                "{value}"
            );
        }
    }

    /// The run widens leftwards until it carries a colon, so a password that
    /// contains a separator — or the literal `://` — no longer hides behind it.
    #[test]
    fn a_tail_password_that_contains_a_separator_is_masked() {
        assert_eq!(
            redact_userinfo("https://nominal@host.lan/redir?to=svc:pa/ss@inner.lan").as_deref(),
            Some("https://***@host.lan/redir?***:***@inner.lan")
        );
        assert_eq!(
            redact_userinfo("https://nominal@h/x://a:b://c@d").as_deref(),
            Some("https://***@h/x://***:***@d")
        );
    }

    /// The escape depth is bounded and the bound is documented, and the semicolon
    /// is optional on the entity forms — a fourth-layer escape used to be published
    /// while the doc promised "any depth".
    #[test]
    fn deeper_escape_layers_and_semicolon_less_entities_are_evidence() {
        assert_eq!(
            redact_userinfo("https:///user%2525253Apw@host.lan").as_deref(),
            Some("https://***:***@host.lan")
        );
        assert_eq!(
            redact_userinfo("user%2525253Apw@host.lan").as_deref(),
            Some("***:***@host.lan")
        );
        for value in [
            "https:///user&colonpw@host.lan",
            "https:///user&#58pw@host.lan",
            "https:///user&#X3Apw@host.lan",
            "https:///user%26%23%35%38%3Bpw@host.lan",
        ] {
            assert_eq!(
                redact_userinfo(value).as_deref(),
                Some("https://***:***@host.lan"),
                "{value}"
            );
        }
    }

    /// The colon separating a user name from a password is not always a literal
    /// `:` byte. Matching only the byte published `https:///user%3Apw@host`
    /// verbatim — the same empty-authority class the previous round claimed to
    /// close — while the literal spelling of that value was masked. `/config` is
    /// answered without authentication, so every spelling counts.
    #[test]
    fn an_encoded_colon_is_credential_evidence() {
        for (value, want) in [
            ("https:///user%3Apw@host.lan", "https://***:***@host.lan"),
            ("file:///user%3Apw@host.lan", "file://***:***@host.lan"),
            (
                "smb:///user%3Apw@server/share",
                "smb://***:***@server/share",
            ),
            ("http:////user%3Apw@host.lan", "http://***:***@host.lan"),
            ("https://?x=user%3Apw@host.lan", "https://***:***@host.lan"),
            ("https://#user%3Apw@host.lan", "https://***:***@host.lan"),
            ("https://////user%3Apw@host.lan", "https://***:***@host.lan"),
            ("https:///user%253Apw@host.lan", "https://***:***@host.lan"),
            (
                "https:///user%25253Apw@host.lan",
                "https://***:***@host.lan",
            ),
            ("https:///user&#58;pw@host.lan", "https://***:***@host.lan"),
            ("https:///user&#x3a;pw@host.lan", "https://***:***@host.lan"),
            (
                "https:///user&colon;pw@host.lan",
                "https://***:***@host.lan",
            ),
            (
                "https:///user\u{FF1A}pw@host.lan",
                "https://***:***@host.lan",
            ),
            ("user%3Apw@host.lan:8080", "***:***@host.lan:8080"),
            ("https:///user:pa%2Fss@host.lan", "https://***:***@host.lan"),
        ] {
            assert_eq!(
                redact_userinfo(value).as_deref(),
                Some(want),
                "credential published verbatim: {value:?}"
            );
        }
        // A `%` that hides no colon is not a credential.
        assert_eq!(redact_userinfo("https://bazarr.lan/a%20b@c"), None);
        assert_eq!(redact_userinfo("file:///mnt/nas/a%20b@c.mkv"), None);
    }

    /// A colon-free authority-less userinfo is published: extra slashes after a
    /// special scheme make it a user name with no password in it (WHATWG reads
    /// `https://///pw@host` as the user name `pw`), which is not the credential
    /// this function exists for — masking every path containing an `@` would be
    /// the wrong default. Pinned so the asymmetry is deliberate, not accidental.
    #[test]
    fn a_colon_free_authority_less_userinfo_is_a_documented_exception() {
        assert_eq!(redact_userinfo("https://///pw@host"), None);
        assert_eq!(redact_userinfo("https:///path@x"), None);
        assert_eq!(redact_userinfo("file:///mnt/nas/a@b.mkv"), None);
        // With a colon in the same position it is a credential, and it is masked.
        assert_eq!(
            redact_userinfo("https://///pw:pw@host").as_deref(),
            Some("https://***:***@host")
        );
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
    /// the control key is documented as never coming from them, so a key written
    /// into a config file must not authenticate whatever else the host provides.
    ///
    /// The variable is the *last* candidate, ahead of it only files — so on a host
    /// that ships `/run/secrets/control_api_key` the variable cannot be observed
    /// at all, and this test asserts the file-first precedence there instead of
    /// returning silently (a passing test whose body never ran is not evidence;
    /// `cargo test` hides output unless `--show-output` is passed). Which branch
    /// ran is visible in the message on failure and with `--show-output`.
    #[test]
    fn control_key_never_comes_from_a_config_file() {
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
        // Host-independent half: the config-file layer never supplies the key.
        assert_ne!(
            cfg.control_key(),
            "file-layer-key",
            "a key in pipeline.env must not authenticate control requests"
        );
        // Branch on the code's own predicate — a readable, non-empty file — not on
        // `Path::exists`. Compose secrets are typically 0400 root-owned while
        // `cargo test` runs as the invoking user, and a k8s `/run/secrets/<name>/`
        // mount is a directory: `exists()` alone would take the file branch, find
        // no key, and report a failure that is the harness's fault, not the code's.
        let shipped_file_key = std::fs::read_to_string("/run/secrets/control_api_key")
            .map(|v| v.trim().to_string())
            .unwrap_or_default();
        if !shipped_file_key.is_empty() {
            assert_eq!(
                cfg.control_key(),
                shipped_file_key,
                "a host that ships the secret file must authenticate from the file"
            );
            std::env::set_var("CONTROL_API_KEY", "env-layer-key");
            assert_ne!(
                cfg.control_key(),
                "env-layer-key",
                "a key file outranks the environment variable"
            );
        } else {
            // No key file anywhere: an empty variable contributes no key, and one
            // holding only spaces is not a key anybody typed — `check_token`
            // refuses an empty key, so control access is denied.
            assert_eq!(
                cfg.control_key(),
                "",
                "an empty variable with no key file must deny, not authenticate"
            );
            std::env::set_var("CONTROL_API_KEY", "   ");
            assert_eq!(
                cfg.control_key(),
                "",
                "a whitespace-only variable must deny, not authenticate"
            );
            std::env::set_var("CONTROL_API_KEY", "env-layer-key");
            assert_eq!(cfg.control_key(), "env-layer-key");
            // ... and a real value is trimmed before it is used.
            std::env::set_var("CONTROL_API_KEY", " env-layer-key \n");
            assert_eq!(cfg.control_key(), "env-layer-key");
        }
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
