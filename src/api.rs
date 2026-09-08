//! Control + telemetry HTTP API (axum).
//!
//! v1 routes (`/status /config /pause /resume /run-once /wake /health`) keep
//! the dashboard/pctl contract; `/api2/*` exposes the richer telemetry
//! surface. GETs are open (browser dashboard holds no token); POSTs require
//! `X-API-Key == CONTROL_API_KEY`.

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

fn check_token(cfg: &Config, headers: &HeaderMap) -> bool {
    // Deny-by-default when no key is configured (same posture as the v1
    // ControlHandler): an unconfigured daemon never accepts control POSTs.
    let key = cfg.control_key();
    if key.is_empty() {
        return false;
    }
    let token = headers
        .get("x-api-key")
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
        .route("/", get(h_index))
        .route("/status", get(h_status))
        .route("/health", get(h_health))
        .route("/config", get(h_config))
        .route("/pause", post(h_pause))
        .route("/resume", post(h_resume))
        .route("/run-once", post(h_run_once))
        .route("/wake", post(h_wake))
        .route("/api2/status", get(h_api2_status))
        .route("/api2/health", get(h_health))
        .route("/api2/config", get(h_config))
        .route("/api2/provenance", get(h_provenance))
        .route("/api2/wanted", get(h_wanted))
        .route("/api2/library", get(h_library))
        .route("/api2/exclusions", get(h_exclusions))
        .route("/api2/episode/:id/retry", post(h_retry))
        .route("/api2/episode/:id/skip", post(h_skip))
        .route("/api2/episode/:id/delete", post(h_delete))
        .route("/api2/episode/:id/exclude", post(h_exclude))
        .route("/api2/episode/:id/unexclude", post(h_unexclude))
        .route("/api2/pause", post(h_pause))
        .route("/api2/resume", post(h_resume))
        .route("/api2/run-once", post(h_run_once))
        .route("/api2/wake", post(h_wake))
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
    }))
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
        "providers": {"llm": s.pipeline.pool.len(), "whisper": s.pipeline.pool.whisper().is_some()},
    }))
}

async fn h_health() -> Json<Value> {
    Json(json!({"ok": true}))
}

async fn h_index() -> impl axum::response::IntoResponse {
    // Serve the operator dashboard when the HTML ships alongside the binary
    // (repo layout: assets/; Docker image: /app/assets/). Legacy fallbacks
    // cover older checkouts with dashboard.html at the root.
    for cand in [
        "assets/dashboard.html",
        "/app/assets/dashboard.html",
        "dashboard.html",
        "/app/dashboard.html",
    ] {
        if let Ok(html) = tokio::fs::read_to_string(cand).await {
            return axum::response::Response::builder()
                .header("content-type", "text/html; charset=utf-8")
                .body(axum::body::Body::from(html))
                .unwrap();
        }
    }
    axum::response::Response::builder()
        .header("content-type", "application/json")
        .body(axum::body::Body::from(
            r#"{"ok":true,"ui":"dashboard.html not bundled"}"#,
        ))
        .unwrap()
}

async fn h_config(State(s): State<Arc<AppState>>) -> Json<Value> {
    Json(serde_json::to_value(s.cfg.masked()).unwrap_or(json!({})))
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

async fn wanted_payload(s: &Arc<AppState>) -> Value {
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

async fn h_exclusions(State(s): State<Arc<AppState>>) -> Json<Value> {
    let ids: Vec<i64> = crate::state::parse_exclusions(&s.cfg.exclusions_file)
        .into_iter()
        .collect();
    Json(json!({"exclusions": ids}))
}

async fn authed(
    State(s): State<Arc<AppState>>,
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
    authed(State(s.clone()), headers).await?;
    s.paused.store(true, Ordering::Relaxed);
    Ok(Json(json!({"ok": true, "paused": true})))
}

async fn h_resume(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    authed(State(s.clone()), headers).await?;
    s.paused.store(false, Ordering::Relaxed);
    s.wake.notify_one();
    Ok(Json(json!({"ok": true, "paused": false})))
}

async fn h_run_once(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    authed(State(s.clone()), headers).await?;
    s.run_once.store(true, Ordering::Relaxed);
    s.wake.notify_one();
    Ok(Json(json!({"ok": true})))
}

async fn h_wake(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    authed(State(s.clone()), headers).await?;
    s.wake.notify_one();
    Ok(Json(json!({"ok": true})))
}

/// Action record writer. `kind` is `series` (default) or `movie` so the
/// daemon routes the record at consume time; without it a dashboard
/// Retry/Delete on a movie would target a nonexistent series id.
fn enqueue_action(
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
/// → series. Prefix match is case-insensitive (`M:5` works). Mirrors the
/// `m:`/`e:` routing in control_api_v2.
fn parse_episode_id(raw: &str) -> Result<(i64, &'static str), String> {
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

async fn h_retry(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(raw): Path<String>,
    body: Option<Json<Value>>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    authed(State(s.clone()), headers).await?;
    let (id, path_kind) =
        parse_episode_id(&raw).map_err(|e| (StatusCode::BAD_REQUEST, Json(json!({"error": e}))))?;
    let (kind, lang) = body_kind_lang(&body, path_kind);
    enqueue_action(&s.cfg, "retry", id, &kind, lang)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": e}))))?;
    s.wake.notify_one();
    Ok(Json(json!({"ok": true})))
}

async fn h_skip(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(raw): Path<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    authed(State(s.clone()), headers).await?;
    let (id, path_kind) =
        parse_episode_id(&raw).map_err(|e| (StatusCode::BAD_REQUEST, Json(json!({"error": e}))))?;
    enqueue_action(&s.cfg, "skip", id, path_kind, None)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": e}))))?;
    Ok(Json(json!({"ok": true})))
}

async fn h_delete(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(raw): Path<String>,
    body: Option<Json<Value>>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    authed(State(s.clone()), headers).await?;
    let (id, path_kind) =
        parse_episode_id(&raw).map_err(|e| (StatusCode::BAD_REQUEST, Json(json!({"error": e}))))?;
    let (kind, lang) = body_kind_lang(&body, path_kind);
    enqueue_action(&s.cfg, "delete", id, &kind, lang)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": e}))))?;
    s.wake.notify_one();
    Ok(Json(json!({"ok": true})))
}

async fn h_exclude(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(raw): Path<String>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    authed(State(s.clone()), headers).await?;
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
    authed(State(s.clone()), headers).await?;
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
}
