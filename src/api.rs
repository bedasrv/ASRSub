//! Control + telemetry HTTP API (axum).
//!
//! Unversioned routes (`/status /config /pause /resume /run-once /wake
//! /health`) preserve the operator control contract; `/api2/*` exposes the
//! richer telemetry surface. GETs are open (browser dashboard holds no
//! token); POSTs require `X-API-Key == CONTROL_API_KEY`.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::Json;
use axum::routing::{get, post};
use serde_json::{json, Value};
use tokio::sync::Mutex;

use crate::config::Config;
use crate::pipeline::Pipeline;
use crate::state::StateEntry;

#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct LastPass {
    pub at: Option<String>,
    pub scanned: usize,
    pub done: usize,
    pub failed: usize,
}

pub struct AppState {
    pub cfg: Config,
    pub pipeline: Arc<Pipeline>,
    pub paused: Arc<AtomicBool>,
    pub wake: Arc<tokio::sync::Notify>,
    pub run_once: Arc<AtomicBool>,
    pub started_at: u64,
    pub last_pass: Mutex<LastPass>,
    pub current: Mutex<Option<String>>,
    /// Cached `/api2/wanted` payload: dashboard polls hit this instead of
    /// Bazarr on every render (10 s TTL).
    wanted_cache: Mutex<Option<(u64, Value)>>,
}

impl AppState {
    pub fn new(cfg: Config, pipeline: Arc<Pipeline>) -> Arc<Self> {
        Arc::new(Self {
            cfg,
            paused: pipeline.paused.clone(),
            pipeline,
            wake: Arc::new(tokio::sync::Notify::new()),
            run_once: Arc::new(AtomicBool::new(false)),
            started_at: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_secs())
                .unwrap_or(0),
            last_pass: Mutex::new(LastPass::default()),
            current: Mutex::new(None),
            wanted_cache: Mutex::new(None),
        })
    }
}

pub(crate) fn check_token(cfg: &Config, headers: &HeaderMap) -> bool {
    // Deny-by-default when no key is configured: an unconfigured daemon
    // never accepts control POSTs.
    let key = cfg.control_key();
    if key.is_empty() {
        return false;
    }
    // Accept either sender header: `X-API-Key` (dashboard/pctl) or
    // `X-Control-Key` (media-server notification plugins). Either must equal
    // CONTROL_API_KEY.
    let token = headers
        .get("x-api-key")
        .or_else(|| headers.get("x-control-key"))
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    secure_eq(token, &key)
}

/// Constant-time string equality for the control token (lengths leak, the
/// key itself does not).
fn secure_eq(a: &str, b: &str) -> bool {
    let (x, y) = (a.as_bytes(), b.as_bytes());
    if x.len() != y.len() {
        return false;
    }
    x.iter()
        .zip(y.iter())
        .fold(0u8, |acc, (p, q)| acc | (p ^ q))
        == 0
}

pub fn router(state: Arc<AppState>) -> axum::Router {
    axum::Router::new()
        .route("/", get(crate::web::h_index))
        .route("/status", get(h_status))
        .route("/health", get(h_health))
        .route("/ready", get(h_ready))
        .route("/config", get(h_config))
        .route("/pause", post(h_pause))
        .route("/resume", post(h_resume))
        .route("/run-once", post(h_run_once))
        .route("/wake", post(h_wake))
        .route("/api2/status", get(h_api2_status))
        .route("/api2/health", get(h_health))
        .route("/api2/ready", get(h_ready))
        .route("/api2/config", get(h_config).post(h_config_put))
        .route("/api2/provenance", get(h_provenance))
        .route("/api2/wanted", get(h_wanted))
        .route("/api2/library", get(h_library))
        .route("/api2/activity", get(h_activity))
        .route("/api2/exclusions", get(h_exclusions))
        .route("/api2/episode/{id}/retry", post(h_retry))
        .route("/api2/episode/{id}/skip", post(h_skip))
        .route("/api2/episode/{id}/delete", post(h_delete))
        .route("/api2/episode/{id}/exclude", post(h_exclude))
        .route("/api2/episode/{id}/unexclude", post(h_unexclude))
        .route("/api2/pause", post(h_pause))
        .route("/api2/resume", post(h_resume))
        .route("/api2/run-once", post(h_run_once))
        .route("/api2/wake", post(h_wake))
        .merge(crate::web::routes())
        .with_state(state)
}

async fn h_status(State(s): State<Arc<AppState>>) -> Json<Value> {
    let last = s.last_pass.lock().await.clone();
    let current = s.current.lock().await.clone();
    Json(json!({
        "paused": s.paused.load(Ordering::Relaxed),
        "uptime_s": SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0).saturating_sub(s.started_at),
        "run_once_requested": s.run_once.load(Ordering::Relaxed),
        "last_pass": {"at": last.at, "scanned": last.scanned, "done": last.done, "failed": last.failed},
        "current": current,
        "started_at": s.started_at,
        // Media root present, as an additive boolean. A vanished NAS no
        // longer looks identical to "idle" (done=0 naps).
        "media_ok": media_present(&s.cfg.nas_media_prefix),
    }))
}

/// The host/NAS media root `map_path` points `/data/` at still exists.
fn media_present(prefix: &str) -> bool {
    std::path::Path::new(prefix).is_dir()
}

async fn h_api2_status(State(s): State<Arc<AppState>>) -> Json<Value> {
    let last = s.last_pass.lock().await.clone();
    Json(json!({
        "reachable": true,
        "paused": s.paused.load(Ordering::Relaxed),
        "uptime_s": SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0).saturating_sub(s.started_at),
        "last_pass": last,
        "target_langs": s.cfg.target_langs,
        "episode_concurrency": s.cfg.episode_concurrency,
        "providers": {"llm": s.pipeline.pool.len(), "whisper_endpoints": s.pipeline.pool.whisper_len()},
    }))
}

async fn h_health() -> Json<Value> {
    Json(json!({"ok": true}))
}

/// Dependency-aware readiness (`/ready`, also `/api2/ready`).
///
/// Unlike `/health` (process liveness), this gates on local prerequisites and
/// answers `503` until they hold. External services are diagnostics only so a
/// slow or down Sonarr/Bazarr/Jellyfin never flaps the probe.
async fn h_ready(State(s): State<Arc<AppState>>) -> (StatusCode, Json<Value>) {
    let (ready, body) = readiness(&s.cfg, s.pipeline.pool.len(), s.pipeline.pool.whisper_len());
    let code = if ready {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    (code, Json(body))
}

/// Pure readiness evaluation (see [`h_ready`]). `llm`/`whisper` are the
/// configured provider counts. Returns `(ready, payload)`.
pub(crate) fn readiness(cfg: &Config, llm: usize, whisper: usize) -> (bool, Value) {
    let media_ok = media_present(&cfg.nas_media_prefix);
    // ASR needs Whisper and translation needs at least one LLM endpoint.
    let providers_ok = llm > 0 && whisper > 0;
    let state_ok = state_dir_writable(&cfg.state_file);
    let ready = media_ok && providers_ok && state_ok;
    let state_dir = cfg
        .state_file
        .parent()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_default();
    let jellyfin_on = !cfg.jellyfin_url.is_empty() && !cfg.jellyfin_api_key.is_empty();
    (
        ready,
        json!({
            "ready": ready,
            "checks": {
                "media_root": {"ok": media_ok, "path": cfg.nas_media_prefix},
                "providers": {"ok": providers_ok, "llm": llm, "whisper": whisper},
                "state_dir": {"ok": state_ok, "path": state_dir},
            },
            "integrations": {
                "sonarr": !cfg.sonarr_url.is_empty(),
                "bazarr": !cfg.bazarr_url.is_empty(),
                "jellyfin": jellyfin_on,
                // A key with no URL silently disables refresh: surface it.
                "jellyfin_misconfigured": !cfg.jellyfin_api_key.is_empty()
                    && cfg.jellyfin_url.is_empty(),
            },
        }),
    )
}

/// Probe the state directory for writability without leaving an artifact.
fn state_dir_writable(state_file: &std::path::Path) -> bool {
    let dir = state_file
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .map(std::path::Path::to_path_buf)
        .unwrap_or_else(|| std::path::PathBuf::from("."));
    if std::fs::create_dir_all(&dir).is_err() {
        return false;
    }
    let probe = dir.join(".asrsub-ready-probe");
    match std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&probe)
    {
        Ok(_) => {
            let _ = std::fs::remove_file(&probe);
            true
        }
        Err(_) => false,
    }
}

async fn h_config(State(s): State<Arc<AppState>>) -> Json<Value> {
    Json(serde_json::to_value(s.cfg.masked()).unwrap_or(json!({})))
}

/// Write settings overrides (`POST /api2/config`). Body is a flat JSON object
/// of `{KEY: value}`; only keys exposed by the settings schema are accepted, so
/// a typo cannot pin an ignored key. Persists to `config.overrides.json`; the
/// daemon applies it on the next restart.
async fn h_config_put(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    authed_state(&s, headers).await?;
    let Some(obj) = body.as_object() else {
        return Err((
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "body must be a JSON object"})),
        ));
    };
    let mut pairs: Vec<(String, String)> = Vec::new();
    for (k, v) in obj {
        if !crate::config::is_editable_key(k) {
            return Err((
                StatusCode::BAD_REQUEST,
                Json(json!({"error": format!("unknown or non-editable key: {k}")})),
            ));
        }
        let sval = match v {
            Value::String(s) => s.clone(),
            Value::Bool(b) => b.to_string(),
            Value::Number(n) => n.to_string(),
            Value::Null => String::new(),
            other => other.to_string(),
        };
        pairs.push((k.clone(), sval));
    }
    crate::config::write_overrides(&pairs).map_err(|e| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": e.to_string()})),
        )
    })?;
    Ok(Json(
        json!({"ok": true, "written": pairs.len(), "restart_required": true}),
    ))
}

async fn h_provenance(State(s): State<Arc<AppState>>) -> Json<Value> {
    let rows: Vec<crate::state::RegistryRow> = crate::state::load_jsonl(&s.cfg.registry_file);
    let mut by_lang: std::collections::HashMap<String, usize> = Default::default();
    for r in &rows {
        if let Some(l) = r.lang.as_deref() {
            *by_lang.entry(crate::lang::normalize_lang(l)).or_default() += 1;
        }
    }
    Json(json!({"rows": rows.len(), "by_lang": by_lang}))
}

pub(crate) async fn wanted_payload(s: &Arc<AppState>) -> Value {
    const TTL_S: u64 = 10;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    // The check and the store each hold the lock briefly; the Bazarr fetch
    // runs unlocked. Two concurrent polls may both fetch (one extra hit,
    // harmless) but never serialize behind a slow fetch.
    {
        let guard = s.wanted_cache.lock().await;
        if let Some((at, v)) = guard.clone() {
            if now.saturating_sub(at) < TTL_S {
                return v;
            }
        }
    }
    let cands = s.pipeline.discover(&std::collections::HashSet::new()).await;
    let fresh = json!({
        "total": cands.len(),
        "data": cands.iter().map(|c| json!({
            "sonarrEpisodeId": c.episode_id,
            "seriesTitle": c.series_title,
            "missing_subtitles": c.missing,
            "movie": c.is_movie,
        })).collect::<Vec<_>>(),
    });
    *s.wanted_cache.lock().await = Some((now, fresh.clone()));
    fresh
}

async fn h_wanted(State(s): State<Arc<AppState>>) -> Json<Value> {
    Json(wanted_payload(&s).await)
}

async fn h_library(State(s): State<Arc<AppState>>) -> Json<Value> {
    // Wanted items annotated with pipeline state: done languages per
    // (kind, episode) from state.jsonl latest rows, plus the exclusion set.
    // Cached implicitly via the shared wanted payload (10 s TTL).
    let wanted = wanted_payload(&s).await;
    let entries: Vec<crate::state::StateEntry> = crate::state::load_jsonl(&s.cfg.state_file);
    let mut done: std::collections::HashMap<(String, i64), Vec<String>> = Default::default();
    for e in entries {
        if e.status.as_deref() != Some("done") {
            continue;
        }
        if let (Some(eid), Some(lang)) = (e.episode_id, e.language.as_deref()) {
            done.entry((e.kind.unwrap_or_else(|| "series".to_string()), eid))
                .or_default()
                .push(crate::lang::normalize_lang(lang));
        }
    }
    let excluded = crate::state::parse_exclusions(&s.cfg.exclusions_file);
    let items: Vec<Value> = wanted
        .get("data")
        .and_then(|d| d.as_array())
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .map(|mut it| {
            let eid = it
                .get("sonarrEpisodeId")
                .and_then(|v| v.as_i64())
                .unwrap_or(-1);
            let kind = if it.get("movie").and_then(|v| v.as_bool()).unwrap_or(false) {
                "movie"
            } else {
                "series"
            };
            it["kind"] = Value::String(kind.to_string());
            it["excluded"] = Value::Bool(excluded.contains(&eid));
            it["done_languages"] = done
                .get(&(kind.to_string(), eid))
                .map(|v| json!(v))
                .unwrap_or_else(|| json!([]));
            it
        })
        .collect();
    Json(json!({"total": items.len(), "data": items}))
}

/// Recent pipeline activity, newest first, in the dashboard's
/// `{items:[...]}` shape (`kind`, `ts`, `episode_id`, `language`,
/// `detail`, `ai`). State-derived only: no Bazarr history merge, so the
/// endpoint stays a cheap local read on every dashboard poll.
async fn h_activity(State(s): State<Arc<AppState>>) -> Json<Value> {
    Json(json!({"items": activity_items(40, &s.cfg.state_file)}))
}

pub(crate) fn activity_items(n: usize, state_file: &std::path::Path) -> Vec<Value> {
    let rows: Vec<StateEntry> = crate::state::load_jsonl(state_file);
    rows.into_iter()
        .rev()
        .take(n.max(1))
        .map(|e| {
            let kind = e.kind.as_deref().unwrap_or("series");
            // Dashboard pills: "pipeline" rows show the status detail,
            // "movie" rows get the MOVIE badge.
            let ui_kind = if kind == "movie" { "movie" } else { "pipeline" };
            let status = e.status.as_deref().unwrap_or("");
            // `detail` travels inside the flattened `extra` map (see
            // `append_state`); surface it after the status.
            let detail = match e.extra.get("detail").and_then(|v| v.as_str()) {
                Some(d) if !d.is_empty() => format!("{status}: {d}"),
                _ => status.to_string(),
            };
            json!({
                "kind": ui_kind,
                "ts": e.ts,
                "episode_id": e.episode_id,
                "language": e.language.as_deref().map(crate::lang::normalize_lang),
                "detail": detail,
                "ai": status == "done",
            })
        })
        .collect()
}

async fn h_exclusions(State(s): State<Arc<AppState>>) -> Json<Value> {
    let ids: Vec<i64> = crate::state::parse_exclusions(&s.cfg.exclusions_file)
        .into_iter()
        .collect();
    Json(json!({"exclusions": ids}))
}

/// Token check for control POSTs (deny-by-default when unconfigured).
async fn authed_state(
    s: &Arc<AppState>,
    headers: HeaderMap,
) -> Result<(), (StatusCode, Json<Value>)> {
    if check_token(&s.cfg, &headers) {
        Ok(())
    } else {
        Err((
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "unauthorized"})),
        ))
    }
}

async fn h_pause(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    authed_state(&s, headers).await?;
    s.paused.store(true, Ordering::Relaxed);
    Ok(Json(json!({"ok": true, "paused": true})))
}

async fn h_resume(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    authed_state(&s, headers).await?;
    s.paused.store(false, Ordering::Relaxed);
    s.wake.notify_one();
    Ok(Json(json!({"ok": true, "paused": false})))
}

async fn h_run_once(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    authed_state(&s, headers).await?;
    s.run_once.store(true, Ordering::Relaxed);
    s.wake.notify_one();
    Ok(Json(json!({"ok": true})))
}

async fn h_wake(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    authed_state(&s, headers).await?;
    s.wake.notify_one();
    Ok(Json(json!({"ok": true})))
}

/// Action record writer. `kind` is `series` (default) or `movie` so the
/// daemon routes the record at consume time; without it a dashboard
/// Retry/Delete on a movie would target a nonexistent series id.
pub(crate) fn enqueue_action(
    cfg: &Config,
    typ: &str,
    id: i64,
    kind: &str,
    lang: Option<String>,
) -> Result<(), String> {
    let rec = serde_json::json!({
        "ts": crate::state::utc_now_iso(),
        "type": typ,
        "episode_id": id,
        "kind": kind,
        "language": lang.map(|l| crate::lang::normalize_lang(&l)),
        "source": "api",
        "note": "",
    });
    crate::state::append_jsonl(&cfg.actions_file, &rec).map_err(|e| e.to_string())?;
    Ok(())
}

/// Split an episode path id into `(id, kind)`: `m:5` → movie, `e:5` or `5`
/// → series. Prefix match is case-insensitive (`M:5` works).
pub(crate) fn parse_episode_id(raw: &str) -> Result<(i64, &'static str), String> {
    let lower = raw.to_lowercase();
    if let Some(n) = lower.strip_prefix("m:") {
        return n
            .parse::<i64>()
            .map(|id| (id, "movie"))
            .map_err(|_| format!("bad episode id: {raw}"));
    }
    let digits = lower.strip_prefix("e:").unwrap_or(&lower);
    digits
        .parse::<i64>()
        .map(|id| (id, "series"))
        .map_err(|_| format!("bad episode id: {raw}"))
}

/// Body knobs shared by retry/delete: optional `language` scope and an
/// optional `kind` override (path prefix wins by default).
fn body_kind_lang(body: &Option<Json<Value>>, path_kind: &'static str) -> (String, Option<String>) {
    let kind = body
        .as_ref()
        .and_then(|b| b.get("kind"))
        .and_then(|v| v.as_str())
        .map(crate::lang::normalize_lang)
        .filter(|k| k == "movie" || k == "series")
        .unwrap_or_else(|| path_kind.to_string());
    let lang = body
        .as_ref()
        .and_then(|b| b.get("language"))
        .and_then(|v| v.as_str())
        .map(str::to_string);
    (kind, lang)
}

/// Shared retry/skip/delete handler: id routing, optional body kind/lang
/// override, action enqueue, wake. `skip` passes no body and no wake.
async fn id_action(
    s: &Arc<AppState>,
    headers: HeaderMap,
    raw: &str,
    body: &Option<Json<Value>>,
    typ: &str,
    wake: bool,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    authed_state(s, headers).await?;
    let (id, path_kind) =
        parse_episode_id(raw).map_err(|e| (StatusCode::BAD_REQUEST, Json(json!({"error": e}))))?;
    let (kind, lang) = body_kind_lang(body, path_kind);
    enqueue_action(&s.cfg, typ, id, &kind, lang)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": e}))))?;
    if wake {
        s.wake.notify_one();
    }
    Ok(Json(json!({"ok": true})))
}

async fn h_retry(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(raw): Path<String>,
    body: Option<Json<Value>>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    id_action(&s, headers, &raw, &body, "retry", true).await
}

async fn h_skip(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(raw): Path<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    id_action(&s, headers, &raw, &None, "skip", false).await
}

async fn h_delete(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(raw): Path<String>,
    body: Option<Json<Value>>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    id_action(&s, headers, &raw, &body, "delete", true).await
}

async fn h_exclude(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(raw): Path<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    authed_state(&s, headers).await?;
    let (id, _) =
        parse_episode_id(&raw).map_err(|e| (StatusCode::BAD_REQUEST, Json(json!({"error": e}))))?;
    let rec =
        serde_json::json!({"episode_id": id, "reason": "api", "ts": crate::state::utc_now_iso()});
    crate::state::append_jsonl(&s.cfg.exclusions_file, &rec).map_err(|e| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": e.to_string()})),
        )
    })?;
    Ok(Json(json!({"ok": true})))
}

async fn h_unexclude(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(raw): Path<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    authed_state(&s, headers).await?;
    let (id, _) =
        parse_episode_id(&raw).map_err(|e| (StatusCode::BAD_REQUEST, Json(json!({"error": e}))))?;
    #[derive(serde::Deserialize, serde::Serialize)]
    struct Excl {
        #[serde(default)]
        episode_id: Option<i64>,
        #[serde(flatten)]
        rest: std::collections::HashMap<String, serde_json::Value>,
    }
    let rows: Vec<Excl> = crate::state::load_jsonl(&s.cfg.exclusions_file);
    let kept: Vec<Excl> = rows
        .into_iter()
        .filter(|e| e.episode_id != Some(id))
        .collect();
    crate::state::rewrite_jsonl(&s.cfg.exclusions_file, &kept).map_err(|e| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": e.to_string()})),
        )
    })?;
    Ok(Json(json!({"ok": true})))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn episode_id_prefix_routes_kind() {
        // m: → movie, e:/bare → series; garbage rejected. Without the kind,
        // dashboard movie actions would target nonexistent series ids.
        assert_eq!(parse_episode_id("m:5").unwrap(), (5, "movie"));
        assert_eq!(parse_episode_id("M:5").unwrap(), (5, "movie"));
        assert_eq!(parse_episode_id("e:5").unwrap(), (5, "series"));
        assert_eq!(parse_episode_id("5").unwrap(), (5, "series"));
        assert!(parse_episode_id("m:x").is_err());
        assert!(parse_episode_id("").is_err());
    }

    #[test]
    fn body_kind_overrides_path_default() {
        let body = |k: &str| Some(Json(json!({"kind": k, "language": "id"})));
        let (kind, lang) = body_kind_lang(&body("movie"), "series");
        assert_eq!(kind, "movie");
        assert_eq!(lang.as_deref(), Some("id"));
        // Unknown kinds fall back to the path default, never through.
        let (kind, _) = body_kind_lang(&body("ova"), "series");
        assert_eq!(kind, "series");
        let (kind, lang) = body_kind_lang(&None, "movie");
        assert_eq!(kind, "movie");
        assert_eq!(lang, None);
    }

    #[test]
    fn activity_maps_state_tail_newest_first() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("state.jsonl");
        for (id, status) in [(1, "done"), (2, "error"), (3, "done")] {
            crate::state::append_jsonl(
                &p,
                &serde_json::json!({
                    "sonarrEpisodeId": id,
                    "language": "id",
                    "status": status,
                    "detail": if status == "error" { "boom" } else { "" },
                    "kind": if id == 3 { "movie" } else { "series" },
                }),
            )
            .unwrap();
        }
        let items = activity_items(10, &p);
        assert_eq!(items.len(), 3);
        // Newest first; movie rows carry the MOVIE kind; error detail shown.
        assert_eq!(items[0].get("episode_id").and_then(|v| v.as_i64()), Some(3));
        assert_eq!(items[0].get("kind").and_then(|v| v.as_str()), Some("movie"));
        assert_eq!(
            items[1].get("detail").and_then(|v| v.as_str()),
            Some("error: boom")
        );
        assert_eq!(items[1].get("ai"), Some(&serde_json::Value::Bool(false)));
        assert_eq!(items[2].get("ai"), Some(&serde_json::Value::Bool(true)));
        // Cap honored.
        assert_eq!(activity_items(2, &p).len(), 2);
    }

    #[test]
    fn control_token_accepts_either_sender_header() {
        // Either sender header authorizes: dashboard/pctl use X-API-Key,
        // while media-server notification plugins use X-Control-Key.
        let dir = tempfile::tempdir().unwrap();
        let kf = dir.path().join("control_api_key");
        std::fs::write(&kf, "s3cret\n").unwrap();
        std::env::set_var("CONTROL_API_KEY_FILE", kf.to_str().unwrap());
        let cfg = crate::config::Config::load().expect("config loads");
        std::env::remove_var("CONTROL_API_KEY_FILE");

        let hdr = |name: axum::http::HeaderName, val: &str| {
            let mut h = HeaderMap::new();
            h.insert(name, val.parse().unwrap());
            h
        };
        assert!(check_token(
            &cfg,
            &hdr(axum::http::HeaderName::from_static("x-api-key"), "s3cret")
        ));
        assert!(check_token(
            &cfg,
            &hdr(
                axum::http::HeaderName::from_static("x-control-key"),
                "s3cret"
            )
        ));
        assert!(!check_token(
            &cfg,
            &hdr(axum::http::HeaderName::from_static("x-api-key"), "wrong")
        ));
        assert!(!check_token(&cfg, &HeaderMap::new()));
    }

    #[tokio::test]
    async fn status_exposes_media_readiness() {
        // Row 15 wiring: /status carries the media-root signal so a dead
        // NAS mount is distinguishable from an idle daemon.
        let cfg = crate::config::Config::load().expect("config loads");
        let http = reqwest::Client::new();
        let pool = crate::providers::ProviderPool::new(
            crate::providers::ProvidersFile {
                llm_translation_models: vec![],
                whisper_stt: None,
                whisper_stt_fallbacks: vec![],
            },
            http.clone(),
        );
        let pipe = std::sync::Arc::new(crate::pipeline::Pipeline::new(cfg.clone(), pool, http));
        let st = AppState::new(cfg, pipe);
        let body = h_status(State(st)).await.0;
        assert_eq!(body.get("paused"), Some(&serde_json::Value::Bool(false)));
        assert!(body.get("media_ok").and_then(|v| v.as_bool()).is_some());
    }

    #[test]
    fn state_dir_writable_probes_without_artifacts() {
        let dir = tempfile::tempdir().unwrap();
        let state = dir.path().join("nested").join("state.jsonl");
        assert!(state_dir_writable(&state));
        // The writability probe must clean up after itself.
        assert!(!dir
            .path()
            .join("nested")
            .join(".asrsub-ready-probe")
            .exists());
    }

    #[test]
    fn readiness_gates_on_local_prereqs_not_integrations() {
        // Local prerequisites: configured media root + state dir + providers.
        let dir = tempfile::tempdir().unwrap();
        let media = dir.path().join("media");
        std::fs::create_dir_all(&media).unwrap();
        let state = dir.path().join("state.jsonl");
        std::env::set_var("NAS_MEDIA_PREFIX", media.to_str().unwrap());
        std::env::set_var("STATE_FILE", state.to_str().unwrap());
        std::env::set_var("JELLYFIN_API_KEY", "placeholder");
        std::env::set_var("JELLYFIN_URL", "");
        let cfg = crate::config::Config::load().expect("config loads");
        std::env::remove_var("NAS_MEDIA_PREFIX");
        std::env::remove_var("STATE_FILE");
        std::env::remove_var("JELLYFIN_API_KEY");
        std::env::remove_var("JELLYFIN_URL");

        // Providers missing → not ready even though the local dirs are fine.
        let (ready, body) = readiness(&cfg, 0, 0);
        assert!(!ready);
        assert_eq!(body["checks"]["media_root"]["ok"], json!(true));
        assert_eq!(body["checks"]["providers"]["ok"], json!(false));
        assert_eq!(body["checks"]["state_dir"]["ok"], json!(true));

        // All local prerequisites met → ready; integrations are diagnostics.
        let (ready, body) = readiness(&cfg, 3, 1);
        assert!(ready);
        assert_eq!(body["ready"], json!(true));
        assert_eq!(body["integrations"]["jellyfin"], json!(false));
        // Key without URL is surfaced, not silently ignored.
        assert_eq!(body["integrations"]["jellyfin_misconfigured"], json!(true));
    }
}
