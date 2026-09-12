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

use axum::extract::{Form, Path, Query, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{Html, IntoResponse, Response};
use axum::routing::{get, post};
use maud::{html, Markup, PreEscaped, DOCTYPE};

use crate::api::AppState;
use crate::config::{Config, FieldKind, ENV_ONLY_KEYS, FIELDS, FIELD_GROUPS};
use crate::state;

const HTMX_JS: &str = include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/assets/htmx.min.js"));
const APP_CSS: &str = include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/assets/app.css"));

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
    // 401 is expected (missing/wrong key): swap the rendered fragment, which
    // carries a banner, instead of htmx's default "do not swap errors".
    if (e.detail.xhr && e.detail.xhr.status === 401) {
      e.detail.shouldSwap = true;
      e.detail.isError = false;
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
    let (ready, rb) =
        crate::api::readiness(&s.cfg, s.pipeline.pool.len(), s.pipeline.pool.whisper_len());
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

fn settings_frag(cfg: &Config, msg: Option<(bool, String)>) -> Markup {
    html! {
        div id="settings-body" {
            div class="card" {
                h2 { "Settings" }
                div class="banner" {
                    "Edits are written to " code { "config.overrides.json" } " (beats pipeline.env). "
                    "Restart the daemon to apply. Secret fields left blank stay unchanged."
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
                                    div class="field" {
                                        label for=(f.key) { (f.label) }
                                        @match f.kind {
                                            FieldKind::Secret => {
                                                input type="password" id=(f.key) name=(f.key) value="" placeholder="unchanged" autocomplete="new-password";
                                                input type="hidden" name=(format!("orig__{}", f.key)) value="";
                                            }
                                            FieldKind::Bool => {
                                                input type="checkbox" id=(f.key) name=(f.key) checked[truthy(&cur)];
                                                input type="hidden" name=(format!("orig__{}", f.key)) value=(cur);
                                            }
                                            _ => {
                                                input type="text" id=(f.key) name=(f.key) value=(cur);
                                                input type="hidden" name=(format!("orig__{}", f.key)) value=(cur);
                                            }
                                        }
                                        div class="help" { (f.help) }
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

/// Reject non-numeric values for `Number` fields before they reach disk.
fn invalid_number(pairs: &[(String, String)]) -> Option<String> {
    for (k, v) in pairs {
        if v.is_empty() {
            continue;
        }
        if let Some(f) = FIELDS.iter().find(|f| f.key == k) {
            if f.kind == FieldKind::Number && v.parse::<f64>().is_err() {
                return Some(k.clone());
            }
        }
    }
    None
}

async fn h_config_save(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Form(form): Form<HashMap<String, String>>,
) -> Response {
    if !crate::api::check_token(&s.cfg, &headers) {
        let html = settings_frag(&s.cfg, Some((false, UNAUTH_MSG.to_string()))).into_string();
        return (StatusCode::UNAUTHORIZED, Html(html)).into_response();
    }
    let pairs = collect_changes(&form);
    let msg = if pairs.is_empty() {
        (true, "No changes to save.".to_string())
    } else if let Some(bad) = invalid_number(&pairs) {
        (false, format!("{bad} must be a number. Nothing saved."))
    } else {
        match crate::config::write_overrides(&pairs) {
            Ok(_) => (
                true,
                format!(
                    "Wrote {} override(s) to config.overrides.json. Restart the daemon to apply.",
                    pairs.len()
                ),
            ),
            Err(e) => (false, format!("Save failed: {e}")),
        }
    };
    Html(settings_frag(&s.cfg, Some(msg)).into_string()).into_response()
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
        return (StatusCode::UNAUTHORIZED, Html(html)).into_response();
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
        _ => return (StatusCode::NOT_FOUND, "unknown control action").into_response(),
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
        return (StatusCode::UNAUTHORIZED, Html(html)).into_response();
    }
    let (eid, kind) = match crate::api::parse_episode_id(&id) {
        Ok(v) => v,
        Err(e) => return (StatusCode::BAD_REQUEST, e).into_response(),
    };
    let msg: String = match action.as_str() {
        "exclude" => {
            let rec =
                serde_json::json!({"episode_id": eid, "reason": "ui", "ts": state::utc_now_iso()});
            if let Err(e) = state::append_jsonl(&s.cfg.exclusions_file, &rec) {
                return (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response();
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
                return (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response();
            }
            format!("Unexcluded {id}.")
        }
        typ @ ("retry" | "skip" | "delete") => {
            if let Err(e) = crate::api::enqueue_action(&s.cfg, typ, eid, kind, None) {
                return (StatusCode::INTERNAL_SERVER_ERROR, e).into_response();
            }
            if typ != "skip" {
                s.wake.notify_one();
            }
            format!("Queued {typ} for {id}.")
        }
        _ => return (StatusCode::NOT_FOUND, "unknown episode action").into_response(),
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
        // Number field: garbage rejected, numeric accepted.
        assert_eq!(
            invalid_number(&pairs("MAX_EPS_PER_RUN", "abc")),
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
