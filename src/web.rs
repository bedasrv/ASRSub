//! Server-rendered operator dashboard (htmx).
//!
//! Replaces the retired single-file `assets/dashboard.html`. Rendering and the
//! settings schema live in Rust, so the UI cannot drift from what the daemon
//! actually reads (`crate::config::FIELDS` is the single source of truth). htmx
//! and the stylesheet are embedded in the binary — no runtime asset files. The
//! JSON `/api2/*` API is unchanged and stays the interface for `pctl`.
//!
//! Auth model: read views are open (as the JSON GETs are); every mutation checks
//! the control key from the `X-API-Key` header. The shell keeps the key in
//! `sessionStorage` and attaches it to htmx requests via `hx-headers`, so the
//! key never persists server-side and there is no cookie/CSRF surface.

use std::collections::HashMap;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::extract::{Form, Path, Query, Request, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::middleware::Next;
use axum::response::{Html, IntoResponse, Response};
use axum::routing::{get, post};
use maud::{html, Markup, PreEscaped, DOCTYPE};

use crate::api::AppState;
use crate::config::{Config, FieldKind, ENV_ONLY_KEYS, FIELDS, FIELD_GROUPS};
use crate::state;

const HTMX_JS: &str = include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/assets/htmx.min.js"));
const APP_CSS: &str = include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/assets/app.css"));

/// Marks a response body as a dashboard fragment htmx is allowed to swap.
///
/// The shell only swaps error responses that carry it ([`SHELL_JS`]), so a bare
/// framework or reverse-proxy error page can never replace a dashboard region.
pub(crate) const FRAGMENT_HEADER: &str = "x-asrsub-fragment";

/// Render `html` as a dashboard fragment with `status`, marked swappable.
fn fragment(status: StatusCode, html: String) -> Response {
    (status, [(FRAGMENT_HEADER, "1")], Html(html)).into_response()
}

/// A minimal fragment whose root id is the region htmx asked to swap.
fn error_body(id: &str, msg: &str) -> String {
    html! { div id=(id) { div class="banner err" { (msg) } } }.into_string()
}

/// Only ids the dashboard actually renders may be echoed back into HTML.
fn sanitize_target(raw: &str) -> String {
    raw.trim()
        .trim_start_matches('#')
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '-' || *c == '_')
        .take(64)
        .collect()
}

/// Replace any non-fragment error response on this router with a swappable one.
///
/// The handlers already return fragments, but axum's own rejections (unknown
/// `/ui` route -> 404, wrong method -> 405, a form POST without the urlencoded
/// content type -> 415) are plain text, which htmx would either drop silently or
/// (worse, with a naive "swap all 4xx" rule) paste into a dashboard region.
async fn fragment_errors(req: Request, next: Next) -> Response {
    let target = req
        .headers()
        .get("hx-target")
        .and_then(|v| v.to_str().ok())
        .map(sanitize_target)
        .filter(|t| !t.is_empty())
        .unwrap_or_else(|| "overview-body".to_string());
    let res = next.run(req).await;
    if (res.status().is_client_error() || res.status().is_server_error())
        && !res.headers().contains_key(FRAGMENT_HEADER)
    {
        let msg = format!("Request failed (HTTP {}).", res.status().as_u16());
        return fragment(res.status(), error_body(&target, &msg));
    }
    res
}

/// Shell script: control-key handling, htmx 401 handling, nav highlighting.
/// Kept as a raw string (maud cannot template JS).
///
/// The key is injected into every htmx request via `htmx:configRequest` (the
/// documented hook) rather than an inherited `hx-headers` attribute, so it also
/// applies to requests issued after a swap and cannot be lost to attribute
/// inheritance timing.
const SHELL_JS: &str = r#"
(function () {
  function key() { return sessionStorage.getItem('asrsub-key') || ''; }
  function paint() {
    var k = key();
    var el = document.getElementById('ctl-key');
    if (el && el.value !== k) el.value = k;
    var st = document.getElementById('key-state');
    if (st) {
      st.textContent = k ? 'key set' : 'no key';
      st.classList.toggle('ok', !!k);
    }
  }
  window.asrsubSetKey = function (v) {
    sessionStorage.setItem('asrsub-key', (v || '').trim());
    paint();
  };
  document.addEventListener('htmx:configRequest', function (e) {
    var k = key();
    if (k) e.detail.headers['X-API-Key'] = k;
  });
  document.addEventListener('htmx:beforeSwap', function (e) {
    // Error responses the server marks as fragments (401 missing/wrong key, 400
    // bad id or refused save, 404 unknown action, 500 a state write failed) all
    // carry a banner, so swap them instead of htmx's default "drop errors" —
    // otherwise a failed action looks like nothing happened at all. The marker
    // keeps unrelated bodies (proxy/SSO 403, 502) out of the dashboard. 5xx
    // still reports as an error.
    var st = e.detail.xhr ? e.detail.xhr.status : 0;
    var frag = e.detail.xhr ? e.detail.xhr.getResponseHeader('X-Asrsub-Fragment') : null;
    if (st >= 400 && frag) {
      e.detail.shouldSwap = true;
      e.detail.isError = st >= 500;
    }
  });
  document.addEventListener('click', function (e) {
    var b = e.target.closest('nav.tabs button');
    if (!b) return;
    document.querySelectorAll('nav.tabs button').forEach(function (x) {
      x.classList.toggle('active', x === b);
    });
  });
  paint();
})();
"#;

pub fn routes() -> axum::Router<Arc<AppState>> {
    axum::Router::new()
        .route("/ui/overview", get(h_overview))
        .route("/ui/library", get(h_library))
        .route("/ui/activity", get(h_activity))
        .route("/ui/settings", get(h_settings))
        .route("/ui/provenance", get(h_provenance))
        .route("/ui/config", post(h_config_save))
        .route("/ui/control/{action}", post(h_control))
        .route("/ui/episode/{id}/{action}", post(h_episode_action))
        .route("/assets/htmx.min.js", get(h_htmx))
        .route("/assets/app.css", get(h_css))
        // Framework rejections on these routes (404/405/415) become fragments
        // too, so every /ui/* failure the shell may see is swappable.
        // `route_layer`, not `layer`: the fallback stays a plain 404 instead of
        // answering every unmatched path in the app with an HTML fragment.
        .route_layer(axum::middleware::from_fn(fragment_errors))
}

fn now_s() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn fmt_uptime(s: u64) -> String {
    let (d, h, m) = (s / 86400, (s % 86400) / 3600, (s % 3600) / 60);
    if d > 0 {
        format!("{d}d {h}h")
    } else if h > 0 {
        format!("{h}h {m}m")
    } else {
        format!("{m}m")
    }
}

fn ok_pill(ok: bool, yes: &str, no: &str) -> Markup {
    let cls = if ok { "pill ok" } else { "pill bad" };
    html! { span class=(cls) { @if ok { (yes) } @else { (no) } } }
}

fn truthy(v: &str) -> bool {
    matches!(v.trim().to_lowercase().as_str(), "1" | "true" | "yes")
}

async fn h_htmx() -> Response {
    (
        [
            (
                header::CONTENT_TYPE,
                "application/javascript; charset=utf-8",
            ),
            (header::CACHE_CONTROL, "public, max-age=86400"),
        ],
        HTMX_JS,
    )
        .into_response()
}

async fn h_css() -> Response {
    (
        [
            (header::CONTENT_TYPE, "text/css; charset=utf-8"),
            (header::CACHE_CONTROL, "public, max-age=86400"),
        ],
        APP_CSS,
    )
        .into_response()
}

/// Banner rendered at the top of a fragment after a mutation attempt.
fn flash_banner(flash: Option<(&str, bool)>) -> Markup {
    html! {
        @if let Some((text, err)) = flash {
            div class=(if err { "banner err" } else { "banner ok" }) { (text) }
        }
    }
}

/// Full page shell. `active` highlights a nav tab; `body` is the initial view.
pub async fn h_index(State(s): State<Arc<AppState>>) -> Html<String> {
    let body = overview_frag(&s, None).await;
    Html(layout(&s.cfg, "status", body).into_string())
}

fn layout(cfg: &Config, active: &str, body: Markup) -> Markup {
    html! {
        (DOCTYPE)
        html lang="en" {
            head {
                meta charset="utf-8";
                meta name="viewport" content="width=device-width, initial-scale=1";
                title { "ASRSub" }
                link rel="stylesheet" href="/assets/app.css";
                script src="/assets/htmx.min.js" {}
            }
            body {
                header class="top" {
                    h1 { "ASRSub" }
                    span class="muted" { "control API " code { (cfg.webhook_port) } }
                    span class="spacer" {}
                    span id="key-state" { "no key" }
                    input id="ctl-key" type="password" placeholder="control key" autocomplete="off"
                        onchange="asrsubSetKey(this.value)";
                    button class="btn" onclick="asrsubSetKey(document.getElementById('ctl-key').value)" { "Unlock" }
                }
                nav class="tabs" {
                    button class=(if active == "library" { "active" } else { "muted" })
                        hx-get="/ui/library" hx-target="#main" hx-swap="innerHTML" { "Library" }
                    button class=(if active == "activity" { "active" } else { "muted" })
                        hx-get="/ui/activity" hx-target="#main" hx-swap="innerHTML" { "Activity" }
                    button class=(if active == "status" { "active" } else { "muted" })
                        hx-get="/ui/overview" hx-target="#main" hx-swap="innerHTML" { "Status" }
                    button class=(if active == "provenance" { "active" } else { "muted" })
                        hx-get="/ui/provenance" hx-target="#main" hx-swap="innerHTML" { "Provenance" }
                    button class=(if active == "settings" { "active" } else { "muted" })
                        hx-get="/ui/settings" hx-target="#main" hx-swap="innerHTML" { "Settings" }
                }
                main id="main" { (body) }
                div id="toasts" {}
                script { (PreEscaped(SHELL_JS)) }
            }
        }
    }
}

// ---------------------------------------------------------------- overview --

async fn h_overview(State(s): State<Arc<AppState>>) -> Html<String> {
    Html(overview_frag(&s, None).await.into_string())
}

async fn overview_frag(s: &Arc<AppState>, flash: Option<(&str, bool)>) -> Markup {
    let last = s.last_pass.lock().await.clone();
    let current = s.current.lock().await.clone();
    let paused = s.paused.load(Ordering::Relaxed);
    let uptime = fmt_uptime(now_s().saturating_sub(s.started_at));
    let (ready, rb) = crate::api::readiness_for(s).await;
    let checks = &rb["checks"];
    let integ = &rb["integrations"];
    let last_at = last.at.clone().unwrap_or_else(|| "never".to_string());
    html! {
        div id="overview-body" hx-get="/ui/overview" hx-trigger="every 5s" hx-swap="outerHTML" {
            (flash_banner(flash))
            div class="card" {
                h2 { "Readiness" }
                div class="grid" {
                    div class="stat" { div class="k" { "Ready" }
                        div class="v" { (ok_pill(ready, "ready", "not ready")) } }
                    div class="stat" { div class="k" { "Media root" }
                        div class="v" { (ok_pill(checks["media_root"]["ok"].as_bool().unwrap_or(false), "mounted", "missing")) }
                        div class="muted mono" { (s.cfg.nas_media_prefix) } }
                    div class="stat" { div class="k" { "Providers" }
                        div class="v" { (ok_pill(checks["providers"]["ok"].as_bool().unwrap_or(false), "ok", "incomplete")) }
                        div class="muted" { "llm " (checks["providers"]["llm"].as_u64().unwrap_or(0)) " · whisper " (checks["providers"]["whisper"].as_u64().unwrap_or(0)) } }
                    div class="stat" { div class="k" { "State dir" }
                        div class="v" { (ok_pill(checks["state_dir"]["ok"].as_bool().unwrap_or(false), "writable", "unwritable")) } }
                }
                div class="row-actions mt-m" {
                    @if paused {
                        button class="btn primary" hx-post="/ui/control/resume" hx-target="#overview-body" hx-swap="outerHTML" { "Resume" }
                    } @else {
                        button class="btn" hx-post="/ui/control/pause" hx-target="#overview-body" hx-swap="outerHTML" { "Pause" }
                    }
                    button class="btn" hx-post="/ui/control/run-once" hx-target="#overview-body" hx-swap="outerHTML" { "Run once" }
                    button class="btn" hx-post="/ui/control/wake" hx-target="#overview-body" hx-swap="outerHTML" { "Wake" }
                }
            }
            div class="card" {
                h2 { "Pipeline" }
                div class="grid" {
                    div class="stat" { div class="k" { "Paused" } div class="v" { (paused) } }
                    div class="stat" { div class="k" { "Uptime" } div class="v" { (uptime) } }
                    div class="stat" { div class="k" { "Current" } div class="v" { @if let Some(c) = &current { (c) } @else { "idle" } } }
                }
                div class="muted mt-s" { "Last pass " (last_at) " · scanned " (last.scanned) " · done " (last.done) " · failed " (last.failed) }
            }
            div class="card" {
                h2 { "Integrations" }
                div class="row-actions" {
                    (ok_pill(integ["sonarr"].as_bool().unwrap_or(false), "Sonarr configured", "Sonarr off"))
                    (ok_pill(integ["bazarr"].as_bool().unwrap_or(false), "Bazarr configured", "Bazarr off"))
                    (ok_pill(integ["jellyfin"].as_bool().unwrap_or(false), "Jellyfin refresh on", "Jellyfin off"))
                }
                @if integ["jellyfin_misconfigured"].as_bool().unwrap_or(false) {
                    div class="banner err" { "JELLYFIN_API_KEY is set but JELLYFIN_URL is empty — refresh is disabled. Set JELLYFIN_URL." }
                }
            }
        }
    }
}

// ----------------------------------------------------------------- library --

#[derive(serde::Deserialize, Default)]
struct LibQuery {
    #[serde(default)]
    q: String,
    #[serde(default)]
    scope: String,
    #[serde(default)]
    sort: String,
    #[serde(default)]
    dir: String,
}

impl LibQuery {
    /// Query string used to round-trip the current filters through action POSTs
    /// (`?q=…&scope=…`), so retrying an item does not reset the operator's view.
    fn to_query(&self) -> String {
        let mut parts = Vec::new();
        for (k, v) in [
            ("q", &self.q),
            ("scope", &self.scope),
            ("sort", &self.sort),
            ("dir", &self.dir),
        ] {
            if !v.is_empty() {
                parts.push(format!("{k}={}", urlencode(v)));
            }
        }
        parts.join("&")
    }
}

struct LibRow {
    aid: String,
    title: String,
    kind: &'static str,
    missing: String,
    done: String,
    excluded: bool,
}

/// Percent-encode the small set of characters that break a query value. Sufficient
/// for the filter fields (search text, fixed enum tokens); not a general encoder.
fn urlencode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// Action URL for a library row, carrying the current filter query so the
/// re-rendered fragment keeps the operator's search/scope/sort.
fn ep_action_url(aid: &str, action: &str, qs: &str) -> String {
    if qs.is_empty() {
        format!("/ui/episode/{aid}/{action}")
    } else {
        format!("/ui/episode/{aid}/{action}?{qs}")
    }
}

async fn h_library(State(s): State<Arc<AppState>>, Query(q): Query<LibQuery>) -> Html<String> {
    Html(library_frag(&s, &q, None).await.into_string())
}

async fn library_frag(s: &Arc<AppState>, q: &LibQuery, flash: Option<(&str, bool)>) -> Markup {
    let wanted = crate::api::wanted_payload(s).await;
    let items = wanted["data"].as_array().cloned().unwrap_or_default();
    let entries: Vec<state::StateEntry> = state::load_jsonl(&s.cfg.state_file);
    let mut done: HashMap<(String, i64), Vec<String>> = HashMap::new();
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
    let excluded = state::parse_exclusions(&s.cfg.exclusions_file);
    let needle = q.q.trim().to_lowercase();

    let mut rows: Vec<LibRow> = Vec::new();
    for it in &items {
        let eid = it["sonarrEpisodeId"].as_i64().unwrap_or(-1);
        let movie = it["movie"].as_bool().unwrap_or(false);
        let title = it["seriesTitle"].as_str().unwrap_or("").to_string();
        let is_excluded = excluded.contains(&eid);
        let missing: Vec<String> = it["missing_subtitles"]
            .as_array()
            .map(|a| {
                a.iter()
                    .filter_map(|v| v.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default();
        let kind = if movie { "movie" } else { "series" };
        let done_langs = done
            .get(&(kind.to_string(), eid))
            .cloned()
            .unwrap_or_default();
        // scope=active hides items with no missing subtitles (nothing to do).
        if q.scope == "active" && missing.is_empty() {
            continue;
        }
        if !needle.is_empty()
            && !title.to_lowercase().contains(&needle)
            && !eid.to_string().contains(&needle)
        {
            continue;
        }
        rows.push(LibRow {
            aid: if movie {
                format!("m:{eid}")
            } else {
                eid.to_string()
            },
            title: if title.is_empty() {
                format!("(episode {eid})")
            } else {
                title
            },
            kind,
            missing: if missing.is_empty() {
                "—".to_string()
            } else {
                missing.join(", ")
            },
            done: if done_langs.is_empty() {
                "—".to_string()
            } else {
                done_langs.join(", ")
            },
            excluded: is_excluded,
        });
    }
    // sort: title (default) | id | status
    let desc = q.dir == "desc";
    match q.sort.as_str() {
        "id" => rows.sort_by_key(|r| r.aid.clone()),
        _ => rows.sort_by_key(|r| r.title.to_lowercase()),
    }
    if desc {
        rows.reverse();
    }

    html! {
        div id="library-body" {
            @if let Some((text, err)) = flash {
                div class=(if err { "banner err" } else { "banner ok" }) { (text) }
            }
            form id="lib-filters" class="filters" hx-get="/ui/library"
                hx-target="#library-body" hx-swap="outerHTML" {
                input type="search" name="q" value=(q.q) placeholder="Search title or id" autocomplete="off";
                select name="scope" {
                    option value="" selected[(q.scope != "active")] { "All" }
                    option value="active" selected[(q.scope == "active")] { "Active only" }
                }
                select name="sort" {
                    option value="" selected[(q.sort != "id")] { "Sort: title" }
                    option value="id" selected[(q.sort == "id")] { "Sort: id" }
                }
                select name="dir" {
                    option value="" selected[(q.dir != "desc")] { "Asc" }
                    option value="desc" selected[(q.dir == "desc")] { "Desc" }
                }
                button class="btn" type="submit" { "Apply" }
            }
            div class="card" {
                div class="muted mb-s" { (rows.len()) " item(s)" }
                @if rows.is_empty() {
                    div class="empty" { "Nothing here." }
                } @else {
                    table {
                        thead { tr { th { "Title" } th { "Kind" } th { "Missing" } th { "Done" } th { "Actions" } } }
                        tbody {
                            @for r in &rows {
                                tr {
                                    td { (r.title) }
                                    td { span class="pill" { (r.kind) } }
                                    td class="muted" { (r.missing) }
                                    td class="muted" { (r.done) }
                                    td {
                                        div class="row-actions" {
                                            @let qs = q.to_query();
                                            button class="btn" hx-post=(ep_action_url(&r.aid, "retry", &qs))
                                                hx-target="#library-body" hx-swap="outerHTML" { "Retry" }
                                            button class="btn" hx-post=(ep_action_url(&r.aid, "skip", &qs))
                                                hx-target="#library-body" hx-swap="outerHTML" { "Skip" }
                                            @if r.excluded {
                                                button class="btn" hx-post=(ep_action_url(&r.aid, "unexclude", &qs))
                                                    hx-target="#library-body" hx-swap="outerHTML" { "Unexclude" }
                                            } @else {
                                                button class="btn" hx-post=(ep_action_url(&r.aid, "exclude", &qs))
                                                    hx-target="#library-body" hx-swap="outerHTML" { "Exclude" }
                                            }
                                            button class="btn danger" hx-confirm="Delete subtitles and reprocess?"
                                                hx-post=(ep_action_url(&r.aid, "delete", &qs))
                                                hx-target="#library-body" hx-swap="outerHTML" { "Delete" }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

// ---------------------------------------------------------------- activity --

async fn h_activity(State(s): State<Arc<AppState>>) -> Html<String> {
    let items = crate::api::activity_items(60, &s.cfg.state_file);
    Html(html! {
        div id="activity-body" hx-get="/ui/activity" hx-trigger="every 10s" hx-swap="outerHTML" {
            div class="card" {
                h2 { "Recent activity" }
                @if items.is_empty() {
                    div class="empty" { "No activity yet." }
                } @else {
                    table {
                        thead { tr { th { "When" } th { "Kind" } th { "Episode" } th { "Lang" } th { "Detail" } } }
                        tbody {
                            @for it in &items {
                                tr {
                                    td class="muted" { (it["ts"].as_str().unwrap_or("")) }
                                    td { span class="pill" { (it["kind"].as_str().unwrap_or("")) } }
                                    td { (it["episode_id"].as_i64().map(|i| i.to_string()).unwrap_or_default()) }
                                    td { (it["language"].as_str().unwrap_or("—")) }
                                    td { (it["detail"].as_str().unwrap_or("")) }
                                }
                            }
                        }
                    }
                }
            }
        }
    }.into_string())
}

// -------------------------------------------------------------- provenance --

async fn h_provenance(State(s): State<Arc<AppState>>) -> Html<String> {
    let rows: Vec<state::RegistryRow> = state::load_jsonl(&s.cfg.registry_file);
    let mut by_lang: HashMap<String, usize> = HashMap::new();
    for r in &rows {
        if let Some(l) = r.lang.as_deref() {
            *by_lang.entry(crate::lang::normalize_lang(l)).or_default() += 1;
        }
    }
    let mut langs: Vec<(String, usize)> = by_lang.into_iter().collect();
    langs.sort_by_key(|(_, n)| std::cmp::Reverse(*n));
    let recent: Vec<&state::RegistryRow> = rows.iter().rev().take(50).collect();
    Html(html! {
        div id="provenance-body" hx-get="/ui/provenance" hx-trigger="every 20s" hx-swap="outerHTML" {
            div class="card" {
                h2 { "Subtitle provenance" }
                div class="muted" { (rows.len()) " registry row(s)" }
                div class="row-actions mt-s" {
                    @for (l, n) in &langs { span class="pill" { (l) " " (n) } }
                }
            }
            div class="card" {
                h2 { "Recent" }
                @if recent.is_empty() {
                    div class="empty" { "No registry rows yet." }
                } @else {
                    table {
                        thead { tr { th { "When" } th { "Stem" } th { "Lang" } th { "Source" } th { "Episode" } } }
                        tbody {
                            @for r in &recent {
                                tr {
                                    td class="muted" { (r.ts.as_deref().unwrap_or("")) }
                                    td class="mono" { (r.stem.as_deref().unwrap_or("")) }
                                    td { (r.lang.as_deref().unwrap_or("")) }
                                    td { (r.source.as_deref().unwrap_or("")) }
                                    td { (r.episode_id.map(|i| i.to_string()).unwrap_or_default()) }
                                }
                            }
                        }
                    }
                }
            }
        }
    }.into_string())
}

// ---------------------------------------------------------------- settings --

fn field_display_value(cfg: &Config, key: &str, default: &str) -> String {
    cfg.raw
        .get(key)
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| default.to_string())
}

/// Keys whose effective value comes from the process environment. Process env
/// outranks every config layer, so a save here would persist a value the daemon
/// never applies: the shipped compose pins `NAS_MEDIA_PREFIX` (the bind-mount
/// target) and `WEBHOOK_PORT` (the healthcheck/reverse-proxy port). Those
/// fields render read-only instead of posing as editable knobs.
fn pinned_keys() -> Vec<&'static str> {
    FIELDS
        .iter()
        .map(|f| f.key)
        .filter(|k| crate::config::env_pinned(k))
        .collect()
}

fn settings_frag(cfg: &Config, msg: Option<(bool, String)>) -> Markup {
    let pinned = pinned_keys();
    html! {
        div id="settings-body" {
            div class="card" {
                h2 { "Settings" }
                div class="banner" {
                    "Edits are written to " code { "config.overrides.json" } ", which beats "
                    code { "pipeline.env" } " but not the container environment. "
                    "Restart the daemon to apply. Secret fields left blank stay unchanged."
                    @if !pinned.is_empty() {
                        br;
                        span class="muted" {
                            "Read-only here — the deployment environment sets them: "
                            code { (pinned.join(", ")) } " (change them in the compose .env)"
                        }
                    }
                    @if !ENV_ONLY_KEYS.is_empty() {
                        br;
                        span class="muted" {
                            "Environment-only keys (not editable here): "
                            code { (ENV_ONLY_KEYS.join(", ")) }
                        }
                    }
                }
                @if let Some((ok, text)) = &msg {
                    div class=(if *ok { "banner ok" } else { "banner err" }) { (text) }
                }
                form hx-post="/ui/config" hx-target="#settings-body" hx-swap="outerHTML" {
                    @for (gid, gtitle) in FIELD_GROUPS {
                        @if FIELDS.iter().any(|f| f.group == *gid) {
                            fieldset {
                                legend { (gtitle) }
                                @for f in FIELDS.iter().filter(|f| f.group == *gid) {
                                    @let cur = field_display_value(cfg, f.key, f.default);
                                    @let is_pinned = pinned.contains(&f.key);
                                    div class="field" {
                                        label for=(f.key) { (f.label) }
                                        @match f.kind {
                                            FieldKind::Secret => {
                                                @if is_pinned {
                                                    input type="text" id=(f.key) value="***" readonly disabled;
                                                } @else {
                                                    input type="password" id=(f.key) name=(f.key) value="" placeholder="unchanged" autocomplete="new-password";
                                                    input type="hidden" name=(format!("orig__{}", f.key)) value="";
                                                }
                                            }
                                            FieldKind::Bool => {
                                                @if is_pinned {
                                                    input type="checkbox" id=(f.key) checked[truthy(&cur)] disabled;
                                                } @else {
                                                    input type="checkbox" id=(f.key) name=(f.key) checked[truthy(&cur)];
                                                    input type="hidden" name=(format!("orig__{}", f.key)) value=(cur);
                                                }
                                            }
                                            _ => {
                                                @if is_pinned {
                                                    // Read-only and nameless: a disabled control is
                                                    // never submitted, and the hidden orig__ baseline
                                                    // is omitted for the same reason.
                                                    input type="text" id=(f.key) value=(cur) readonly disabled;
                                                } @else {
                                                    input type="text" id=(f.key) name=(f.key) value=(cur);
                                                    input type="hidden" name=(format!("orig__{}", f.key)) value=(cur);
                                                }
                                            }
                                        }
                                        div class="help" {
                                            (f.help)
                                            @if is_pinned {
                                                " Set by the deployment environment; edit the compose .env instead."
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    div class="row-actions mt-m" {
                        button class="btn primary" type="submit" { "Save" }
                    }
                }
            }
        }
    }
}

async fn h_settings(State(s): State<Arc<AppState>>) -> Html<String> {
    Html(settings_frag(&s.cfg, None).into_string())
}

/// Diff the submitted form against the hidden baseline each field rendered, so
/// saving writes only what the operator actually changed.
fn collect_changes(form: &HashMap<String, String>) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for f in FIELDS {
        // Process-env values outrank this layer, so never queue a write the
        // daemon cannot apply. The form already renders those fields disabled;
        // this also covers a hand-crafted POST.
        if crate::config::env_pinned(f.key) {
            continue;
        }
        let orig = form
            .get(&format!("orig__{}", f.key))
            .map(|v| v.trim().to_string())
            .unwrap_or_default();
        let submitted = match f.kind {
            FieldKind::Bool => {
                if form.contains_key(f.key) {
                    "true".to_string()
                } else {
                    "false".to_string()
                }
            }
            _ => form
                .get(f.key)
                .map(|v| v.trim().to_string())
                .unwrap_or_default(),
        };
        if f.kind == FieldKind::Secret && submitted.is_empty() {
            continue;
        }
        if f.kind == FieldKind::Bool {
            if truthy(&submitted) == truthy(&orig) {
                continue;
            }
        } else if submitted == orig {
            continue;
        }
        out.push((f.key.to_string(), submitted));
    }
    out
}

/// Reject values `Config::load` cannot use for a typed field, before they reach
/// disk. Returns the offending key and what the field expects, so the banner can
/// say it accurately (`LADDER_MIN_CJK=abc` is not "a whole number").
///
/// The rule lives in [`crate::config::value_requirement`] because the JSON API
/// writes the same file and must refuse the same values.
fn invalid_number(pairs: &[(String, String)]) -> Option<(String, String)> {
    pairs
        .iter()
        .find_map(|(k, v)| crate::config::value_requirement(k, v).map(|want| (k.clone(), want)))
}

/// Pinned keys this request actually tried to change (the form renders them
/// read-only, so this only fires for a hand-crafted POST).
fn shadowed_attempts(cfg: &Config, form: &HashMap<String, String>) -> Vec<&'static str> {
    FIELDS
        .iter()
        .filter(|f| crate::config::env_pinned(f.key))
        .filter(|f| {
            let submitted = form
                .get(f.key)
                .map(|v| v.trim().to_string())
                .unwrap_or_default();
            let current = field_display_value(cfg, f.key, f.default);
            if f.kind == FieldKind::Bool {
                // A disabled checkbox is never submitted, so absence means "not
                // attempted", never "set to false" — treating it as an attempt
                // refused every save while any pinned bool was in the
                // environment, even for untouched keys.
                form.contains_key(f.key) && truthy(&submitted) != truthy(&current)
            } else {
                !submitted.is_empty() && submitted != current
            }
        })
        .map(|f| f.key)
        .collect()
}

async fn h_config_save(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Form(form): Form<HashMap<String, String>>,
) -> Response {
    if !crate::api::check_token(&s.cfg, &headers) {
        let html = settings_frag(&s.cfg, Some((false, UNAUTH_MSG.to_string()))).into_string();
        return fragment(StatusCode::UNAUTHORIZED, html);
    }
    let shadowed = shadowed_attempts(&s.cfg, &form);
    let pairs = collect_changes(&form);
    // Anything that persists nothing is a refusal, not a success: answer 400 (as
    // the JSON config API does) while still returning the fragment the operator
    // needs to see.
    let mut refused = !shadowed.is_empty();
    let msg = if !shadowed.is_empty() {
        (
            false,
            format!(
                "{} is set by the deployment environment (compose) and cannot be changed here. \
                 Nothing saved.",
                shadowed.join(", ")
            ),
        )
    } else if pairs.is_empty() {
        (true, "No changes to save.".to_string())
    } else if let Some((bad, want)) = invalid_number(&pairs) {
        refused = true;
        (false, format!("{bad} must be {want}. Nothing saved."))
    } else {
        match crate::config::write_overrides(&pairs) {
            Ok(_) => (
                true,
                format!(
                    "Wrote {} override(s) to config.overrides.json. Restart the daemon to apply.",
                    pairs.len()
                ),
            ),
            Err(e) => {
                refused = true;
                (false, format!("Save failed: {e}"))
            }
        }
    };
    // Re-render from what is on disk now (Config::load) instead of the running
    // config, so the form and its hidden baselines match the persisted state.
    // Otherwise the operator sees pre-save values and a second identical save
    // reports "no changes" while the file differs from the running daemon.
    let view = crate::config::Config::load().unwrap_or_else(|_| s.cfg.clone());
    let body = settings_frag(&view, Some(msg)).into_string();
    if refused {
        fragment(StatusCode::BAD_REQUEST, body)
    } else {
        Html(body).into_response()
    }
}

// ----------------------------------------------------------- mutations ----

const UNAUTH_MSG: &str =
    "Wrong or missing control key. Enter it in the header and click Unlock, then retry.";

async fn h_control(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(action): Path<String>,
) -> Response {
    if !crate::api::check_token(&s.cfg, &headers) {
        let html = overview_frag(&s, Some((UNAUTH_MSG, true)))
            .await
            .into_string();
        return fragment(StatusCode::UNAUTHORIZED, html);
    }
    let msg: &str = match action.as_str() {
        "pause" => {
            s.paused.store(true, Ordering::Relaxed);
            "Paused."
        }
        "resume" => {
            s.paused.store(false, Ordering::Relaxed);
            s.wake.notify_one();
            "Resumed."
        }
        "run-once" => {
            s.run_once.store(true, Ordering::Relaxed);
            s.wake.notify_one();
            "Run-once requested."
        }
        "wake" => {
            s.wake.notify_one();
            "Woken."
        }
        _ => {
            let html = overview_frag(&s, Some(("Unknown control action.", true)))
                .await
                .into_string();
            return fragment(StatusCode::NOT_FOUND, html);
        }
    };
    let html = overview_frag(&s, Some((msg, false))).await.into_string();
    (StatusCode::OK, Html(html)).into_response()
}

async fn h_episode_action(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((id, action)): Path<(String, String)>,
    Query(q): Query<LibQuery>,
) -> Response {
    if !crate::api::check_token(&s.cfg, &headers) {
        let html = library_frag(&s, &q, Some((UNAUTH_MSG, true)))
            .await
            .into_string();
        return fragment(StatusCode::UNAUTHORIZED, html);
    }
    let (eid, kind) = match crate::api::parse_episode_id(&id) {
        Ok(v) => v,
        Err(e) => {
            // Every error branch returns a rendered fragment so htmx can swap a
            // visible banner (see the beforeSwap handler in SHELL_JS).
            let html = library_frag(&s, &q, Some((&e, true))).await.into_string();
            return fragment(StatusCode::BAD_REQUEST, html);
        }
    };
    let msg: String = match action.as_str() {
        "exclude" => {
            let rec =
                serde_json::json!({"episode_id": eid, "reason": "ui", "ts": state::utc_now_iso()});
            if let Err(e) = state::append_jsonl(&s.cfg.exclusions_file, &rec) {
                let msg = format!("Exclude failed: {e}");
                let html = library_frag(&s, &q, Some((&msg, true))).await.into_string();
                return fragment(StatusCode::INTERNAL_SERVER_ERROR, html);
            }
            format!("Excluded {id}.")
        }
        "unexclude" => {
            #[derive(serde::Deserialize, serde::Serialize)]
            struct Excl {
                #[serde(default)]
                episode_id: Option<i64>,
                #[serde(flatten)]
                rest: HashMap<String, serde_json::Value>,
            }
            let rows: Vec<Excl> = state::load_jsonl(&s.cfg.exclusions_file);
            let kept: Vec<Excl> = rows
                .into_iter()
                .filter(|e| e.episode_id != Some(eid))
                .collect();
            if let Err(e) = state::rewrite_jsonl(&s.cfg.exclusions_file, &kept) {
                let msg = format!("Unexclude failed: {e}");
                let html = library_frag(&s, &q, Some((&msg, true))).await.into_string();
                return fragment(StatusCode::INTERNAL_SERVER_ERROR, html);
            }
            format!("Unexcluded {id}.")
        }
        typ @ ("retry" | "skip" | "delete") => {
            if let Err(e) = crate::api::enqueue_action(&s.cfg, typ, eid, kind, None) {
                let msg = format!("Queue failed: {e}");
                let html = library_frag(&s, &q, Some((&msg, true))).await.into_string();
                return fragment(StatusCode::INTERNAL_SERVER_ERROR, html);
            }
            if typ != "skip" {
                s.wake.notify_one();
            }
            format!("Queued {typ} for {id}.")
        }
        _ => {
            let html = library_frag(&s, &q, Some(("Unknown episode action.", true)))
                .await
                .into_string();
            return fragment(StatusCode::NOT_FOUND, html);
        }
    };
    let html = library_frag(&s, &q, Some((&msg, false)))
        .await
        .into_string();
    (StatusCode::OK, Html(html)).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn form(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn collect_changes_diffs_against_hidden_baseline() {
        // Unchanged text: no write. Changed text: one write.
        let f = form(&[
            ("orig__TARGET_LANGS", "id,en"),
            ("TARGET_LANGS", "id,en"),
            ("orig__MAX_EPS_PER_RUN", "8"),
            ("MAX_EPS_PER_RUN", "4"),
        ]);
        let changes = collect_changes(&f);
        assert_eq!(
            changes,
            vec![("MAX_EPS_PER_RUN".to_string(), "4".to_string())]
        );
    }

    #[test]
    fn invalid_number_checks_the_consumers_range() {
        let pairs = |k: &str, v: &str| vec![(k.to_string(), v.to_string())];
        let key = |o: Option<(String, String)>| o.map(|(k, _)| k);
        // WEBHOOK_PORT is a u16 in Config: 70000 used to be accepted, written,
        // and then silently replaced by the default when the daemon reloaded.
        assert_eq!(
            key(invalid_number(&pairs("WEBHOOK_PORT", "70000"))),
            Some("WEBHOOK_PORT".to_string())
        );
        assert_eq!(invalid_number(&pairs("WEBHOOK_PORT", "65535")), None);
        // The *_MS knobs are u32.
        assert_eq!(
            key(invalid_number(&pairs("MAX_CUE_MS", "99999999999"))),
            Some("MAX_CUE_MS".to_string())
        );
        assert_eq!(invalid_number(&pairs("MAX_CUE_MS", "4000000000")), None);
        // Decimals go to the float fields only, and the message names the
        // requirement rather than calling everything a whole number.
        assert_eq!(invalid_number(&pairs("LADDER_MIN_CJK", "0.55")), None);
        let (bad, want) = invalid_number(&pairs("MAX_EPS_PER_RUN", "4.5")).unwrap();
        assert_eq!(bad, "MAX_EPS_PER_RUN");
        assert!(want.contains("whole number"), "unexpected wording: {want}");
        let (bad, want) = invalid_number(&pairs("LADDER_MIN_CJK", "abc")).unwrap();
        assert_eq!(bad, "LADDER_MIN_CJK");
        assert_eq!(want, "a number");
    }

    #[test]
    fn pinned_bool_does_not_block_unrelated_saves() {
        let _guard = crate::config::ENV_LOCK
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        let prior = std::env::var_os("AI_MARKER_CUE");
        std::env::set_var("AI_MARKER_CUE", "true");
        let cfg = Config::load().unwrap();
        // A pinned checkbox renders disabled, so the browser never submits it:
        // absence must mean "not attempted", not "set to false". Reading it as an
        // attempt refused *every* save while a pinned bool was in the environment.
        let untouched = form(&[("orig__MAX_EPS_PER_RUN", "8"), ("MAX_EPS_PER_RUN", "4")]);
        assert!(
            shadowed_attempts(&cfg, &untouched).is_empty(),
            "an untouched pinned bool must not block an unrelated save"
        );
        // A hand-crafted attempt to flip it is still refused.
        let crafted = form(&[("AI_MARKER_CUE", "false")]);
        assert_eq!(shadowed_attempts(&cfg, &crafted), vec!["AI_MARKER_CUE"]);
        match prior {
            Some(v) => std::env::set_var("AI_MARKER_CUE", v),
            None => std::env::remove_var("AI_MARKER_CUE"),
        }
    }

    #[test]
    fn integer_bounds_match_their_consumer_types() {
        // A wrong bound either refuses a value the daemon accepts or re-opens
        // "saved, then silently replaced by the default at load". Keep this
        // table in step with Config's struct fields.
        let expected: &[(&str, u64)] = &[
            ("MAX_EPS_PER_RUN", usize::MAX as u64),
            ("EPISODE_CONCURRENCY", usize::MAX as u64),
            ("ASR_CONCURRENCY", usize::MAX as u64),
            ("TRANSLATE_CONCURRENCY", usize::MAX as u64),
            ("UPLOAD_CONCURRENCY", usize::MAX as u64),
            ("TRANSLATE_CHUNK", usize::MAX as u64),
            ("CPS_MERGE_MAX_CHARS", usize::MAX as u64),
            ("LADDER_MIN_CUES", usize::MAX as u64),
            ("LADDER_MIN_CHARS", usize::MAX as u64),
            ("MAX_CUE_MS", u32::MAX as u64),
            ("AI_MARKER_CUE_MS", u32::MAX as u64),
            ("CPS_MERGE_MAX_DUR_MS", u32::MAX as u64),
            ("CPS_MERGE_MAX_GAP_MS", u32::MAX as u64),
            ("WEBHOOK_PORT", u16::MAX as u64),
        ];
        for (key, max) in expected {
            let f = FIELDS
                .iter()
                .find(|f| f.key == *key)
                .unwrap_or_else(|| panic!("{key} is not in FIELDS"));
            assert_eq!(f.kind, FieldKind::Int(*max), "bound drift for {key}");
        }
        // Every Int field must be covered, so a new one cannot slip in unguarded.
        let int_fields: Vec<&str> = FIELDS
            .iter()
            .filter(|f| matches!(f.kind, FieldKind::Int(_)))
            .map(|f| f.key)
            .collect();
        assert_eq!(
            int_fields.len(),
            expected.len(),
            "add new integer fields to this table: {int_fields:?}"
        );
    }

    #[test]
    /// The bound in the table above is only right if it is the maximum of the
    /// type the *struct field* actually has — the drift the review reproduced was
    /// `webhook_port: u16` → `u32`, which left the table and the UI agreeing with
    /// each other while disagreeing with the loader. Reading the field widths off
    /// a loaded `Config` ties the table to the struct itself.
    fn integer_bounds_match_the_struct_field_width() {
        let _guard = crate::config::ENV_LOCK
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        let dir = tempfile::tempdir().unwrap();
        let prev_dir = std::env::var_os("ASRSUB_CONFIG_DIR");
        std::env::set_var("ASRSUB_CONFIG_DIR", dir.path());
        let cfg = crate::config::Config::load().unwrap();
        match prev_dir {
            Some(v) => std::env::set_var("ASRSUB_CONFIG_DIR", v),
            None => std::env::remove_var("ASRSUB_CONFIG_DIR"),
        }
        let mut seen: Vec<&str> = Vec::new();
        for (key, bound) in FIELDS.iter().filter_map(|f| match f.kind {
            FieldKind::Int(max) => Some((f.key, max)),
            _ => None,
        }) {
            // Width of the struct field this key is loaded into. The match arm
            // list is the point: an Int field added to FIELDS without an arm
            // fails this test (the `panic!` below), not the build.
            let width = match key {
                "MAX_EPS_PER_RUN" => std::mem::size_of_val(&cfg.max_eps_per_run),
                "EPISODE_CONCURRENCY" => std::mem::size_of_val(&cfg.episode_concurrency),
                "ASR_CONCURRENCY" => std::mem::size_of_val(&cfg.asr_concurrency),
                "TRANSLATE_CONCURRENCY" => std::mem::size_of_val(&cfg.translate_concurrency),
                "UPLOAD_CONCURRENCY" => std::mem::size_of_val(&cfg.upload_concurrency),
                "TRANSLATE_CHUNK" => std::mem::size_of_val(&cfg.translate_chunk),
                "CPS_MERGE_MAX_CHARS" => std::mem::size_of_val(&cfg.cps_merge_max_chars),
                "LADDER_MIN_CUES" => std::mem::size_of_val(&cfg.ladder_min_cues),
                "LADDER_MIN_CHARS" => std::mem::size_of_val(&cfg.ladder_min_chars),
                "MAX_CUE_MS" => std::mem::size_of_val(&cfg.max_cue_ms),
                "AI_MARKER_CUE_MS" => std::mem::size_of_val(&cfg.ai_marker_cue_ms),
                "CPS_MERGE_MAX_DUR_MS" => std::mem::size_of_val(&cfg.cps_merge_max_dur_ms),
                "CPS_MERGE_MAX_GAP_MS" => std::mem::size_of_val(&cfg.cps_merge_max_gap_ms),
                "WEBHOOK_PORT" => std::mem::size_of_val(&cfg.webhook_port),
                other => panic!("{other} is an Int field with no struct field here"),
            };
            let bound_width = if bound == usize::MAX as u64 {
                std::mem::size_of::<usize>()
            } else if bound == u32::MAX as u64 {
                4
            } else if bound == u16::MAX as u64 {
                2
            } else {
                panic!("{key} has a bound that is not a type maximum: {bound}")
            };
            assert_eq!(
                width, bound_width,
                "{key}: FIELDS allows up to {bound} but the struct field is {width} bytes"
            );
            seen.push(key);
        }
        assert_eq!(seen.len(), 14, "integer fields covered: {seen:?}");
    }

    #[test]
    fn error_fragments_echo_only_known_target_ids() {
        assert_eq!(sanitize_target("#settings-body"), "settings-body");
        assert_eq!(sanitize_target(" overview-body "), "overview-body");
        // htmx sends the target id back in a request header; it lands in HTML.
        assert_eq!(sanitize_target("#bad\"id<script>"), "badidscript");
    }

    #[test]
    fn collect_changes_handles_bools_and_secrets() {
        // Bool flipping false→true (checkbox present) writes; true→true doesn't.
        let f = form(&[
            ("orig__AI_MARKER_CUE", "true"),
            ("AI_MARKER_CUE", "on"),
            ("orig__JIMAKU_DIRECT_ENABLED", "true"),
        ]);
        let changes = collect_changes(&f);
        assert!(
            !changes.iter().any(|(k, _)| k == "AI_MARKER_CUE"),
            "unchanged bool must not write: {changes:?}"
        );
        // Unchecked box (absent) with a true baseline writes "false".
        assert_eq!(
            changes,
            vec![("JIMAKU_DIRECT_ENABLED".to_string(), "false".to_string())]
        );

        // Blank secret is never written (keeps the existing value).
        let f = form(&[("orig__SONARR_API_KEY", ""), ("SONARR_API_KEY", "")]);
        assert!(collect_changes(&f).is_empty());
        // A provided secret writes.
        let f = form(&[("orig__SONARR_API_KEY", ""), ("SONARR_API_KEY", "new")]);
        assert_eq!(
            collect_changes(&f),
            vec![("SONARR_API_KEY".to_string(), "new".to_string())]
        );
    }

    #[test]
    fn invalid_number_rejects_garbage_for_number_fields() {
        let pairs = |k: &str, v: &str| vec![(k.to_string(), v.to_string())];
        let key = |o: Option<(String, String)>| o.map(|(k, _)| k);
        // Number field: garbage rejected, numeric accepted.
        assert_eq!(
            key(invalid_number(&pairs("MAX_EPS_PER_RUN", "abc"))),
            Some("MAX_EPS_PER_RUN".to_string())
        );
        assert_eq!(invalid_number(&pairs("MAX_EPS_PER_RUN", "4")), None);
        assert_eq!(invalid_number(&pairs("CPS_MERGE_MAX", "20.5")), None);
        // Empty means "clear/unset" and is not validated.
        assert_eq!(invalid_number(&pairs("MAX_EPS_PER_RUN", "")), None);
        // Text fields may contain anything.
        assert_eq!(invalid_number(&pairs("JELLYFIN_URL", "http://x")), None);
    }

    #[test]
    fn query_round_trips_through_action_urls() {
        let q = LibQuery {
            q: "spice & wolf".to_string(),
            scope: "active".to_string(),
            sort: "id".to_string(),
            dir: String::new(),
        };
        let qs = q.to_query();
        assert!(qs.contains("q=spice%20%26%20wolf"));
        assert!(qs.contains("scope=active"));
        assert!(qs.contains("sort=id"));
        assert!(!qs.contains("dir="));
        assert_eq!(
            ep_action_url("m:7", "retry", &qs),
            format!("/ui/episode/m:7/retry?{qs}")
        );
        // Empty filters: no dangling '?'.
        assert_eq!(ep_action_url("42", "skip", ""), "/ui/episode/42/skip");
    }
}
