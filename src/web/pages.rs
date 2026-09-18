use std::collections::HashMap;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use maud::{html, Markup, DOCTYPE};

use crate::api::AppState;
use crate::config::{Config, FieldKind, ENV_ONLY_KEYS, FIELDS, FIELD_GROUPS};
use crate::state;

use super::data;

fn now_s() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0)
}

fn fmt_uptime(seconds: u64) -> String {
    let (days, hours, minutes) = (
        seconds / 86_400,
        (seconds % 86_400) / 3_600,
        (seconds % 3_600) / 60,
    );
    if days > 0 {
        format!("{days}d {hours}h")
    } else if hours > 0 {
        format!("{hours}h {minutes}m")
    } else {
        format!("{minutes}m")
    }
}

fn ok_pill(ok: bool, yes: &str, no: &str) -> Markup {
    let class = if ok { "pill ok" } else { "pill bad" };
    html! { span class=(class) { @if ok { (yes) } @else { (no) } } }
}

fn flash_banner(flash: Option<(&str, bool)>) -> Markup {
    html! {
        @if let Some((text, error)) = flash {
            div class=(if error { "banner err" } else { "banner ok" }) role="status" { (text) }
        }
    }
}

fn nav_link(active: &str, page: &str, href: &str, label: &str) -> Markup {
    html! {
        a class=(if active == page { "active" } else { "" }) href=(href) { (label) }
    }
}

/// One complete document is returned for every UI read and mutation error.
pub(crate) fn layout(
    cfg: &Config,
    active: &str,
    body: Markup,
    refresh: Option<(&str, u64)>,
) -> String {
    html! {
        (DOCTYPE)
        html lang="en" {
            head {
                meta charset="utf-8";
                meta name="viewport" content="width=device-width, initial-scale=1";
                @if let Some((path, seconds)) = refresh {
                    meta http-equiv="refresh" content=(format!("{seconds}; url={path}"));
                }
                title { "ASRSub · " (active) }
                link rel="stylesheet" href="/assets/app.css";
                script src="/assets/app.js" defer {}
            }
            body {
                header class="top" {
                    h1 { "ASRSub" }
                    span class="muted" { "HTTP " code { (cfg.webhook_port) } }
                    span class="spacer" {}
                    span class="muted" { "Access via Pomerium / Pocket ID" }
                }
                nav class="tabs" aria-label="Operator pages" {
                    (nav_link(active, "status", "/ui/status", "Status"))
                    (nav_link(active, "library", "/ui/library", "Library"))
                    (nav_link(active, "activity", "/ui/activity", "Activity"))
                    (nav_link(active, "provenance", "/ui/provenance", "Provenance"))
                    (nav_link(active, "settings", "/ui/settings", "Settings"))
                }
                main id="main" data-page=(active) { (body) }
            }
        }
    }
    .into_string()
}

pub(crate) async fn status_page(s: &Arc<AppState>, flash: Option<(&str, bool)>) -> String {
    let last = s.last_pass.lock().await.clone();
    let current = s.current.lock().await.clone();
    let paused = s.paused.load(Ordering::Relaxed);
    let uptime = fmt_uptime(now_s().saturating_sub(s.started_at));
    let (ready, readiness) = crate::api::readiness_for(s).await;
    let checks = &readiness["checks"];
    let integrations = &readiness["integrations"];
    let last_at = last.at.as_deref().unwrap_or("never");

    let body = html! {
        div id="status-page" {
            div class="page-heading" {
                div { h2 { "Status" } p class="muted" { "Daemon readiness and pipeline state." } }
                a class="btn" href="/ui/status" { "Refresh" }
            }
            (flash_banner(flash))
            div class="card" {
                h2 { "Readiness" }
                div class="grid" {
                    div class="stat" {
                        div class="k" { "Ready" }
                        div class="v" { (ok_pill(ready, "ready", "not ready")) }
                    }
                    div class="stat" {
                        div class="k" { "Media root" }
                        div class="v" { (ok_pill(checks["media_root"]["ok"].as_bool().unwrap_or(false), "mounted", "missing")) }
                        div class="muted mono" { (crate::config::mask_for_log(&s.cfg.nas_media_prefix).as_ref()) }
                    }
                    div class="stat" {
                        div class="k" { "Providers" }
                        div class="v" { (ok_pill(checks["providers"]["ok"].as_bool().unwrap_or(false), "ok", "incomplete")) }
                        div class="muted" { "llm " (checks["providers"]["llm"].as_u64().unwrap_or(0)) " · whisper " (checks["providers"]["whisper"].as_u64().unwrap_or(0)) }
                    }
                    div class="stat" {
                        div class="k" { "State dir" }
                        div class="v" { (ok_pill(checks["state_dir"]["ok"].as_bool().unwrap_or(false), "writable", "unwritable")) }
                    }
                }
                div class="row-actions mt-m" {
                    @if paused {
                        form method="post" action="/ui/control/resume" {
                            button class="btn primary" type="submit" { "Resume" }
                        }
                    } @else {
                        form method="post" action="/ui/control/pause" {
                            button class="btn" type="submit" { "Pause" }
                        }
                    }
                    form method="post" action="/ui/control/run-once" {
                        button class="btn" type="submit" { "Run once" }
                    }
                    form method="post" action="/ui/control/wake" {
                        button class="btn" type="submit" { "Wake" }
                    }
                }
            }
            div class="card" {
                h2 { "Pipeline" }
                div class="grid" {
                    div class="stat" { div class="k" { "Paused" } div class="v" { (paused) } }
                    div class="stat" { div class="k" { "Uptime" } div class="v" { (uptime) } }
                    div class="stat" { div class="k" { "Current" } div class="v" { @if let Some(current) = &current { (current) } @else { "idle" } } }
                }
                div class="muted mt-s" { "Last pass " (last_at) " · scanned " (last.scanned) " · done " (last.done) " · failed " (last.failed) }
            }
            div class="card" {
                h2 { "Integrations" }
                div class="row-actions" {
                    (ok_pill(integrations["sonarr"].as_bool().unwrap_or(false), "Sonarr configured", "Sonarr off"))
                    (ok_pill(integrations["bazarr"].as_bool().unwrap_or(false), "Bazarr configured", "Bazarr off"))
                    (ok_pill(integrations["jellyfin"].as_bool().unwrap_or(false), "Jellyfin refresh on", "Jellyfin off"))
                }
                @if integrations["jellyfin_misconfigured"].as_bool().unwrap_or(false) {
                    div class="banner err" { "JELLYFIN_API_KEY is set but JELLYFIN_URL is empty — refresh is disabled. Set JELLYFIN_URL." }
                }
            }
        }
    };
    layout(&s.cfg, "status", body, Some(("/ui/status", 5)))
}

pub(crate) async fn library_page(
    s: &Arc<AppState>,
    query: &data::LibQuery,
    flash: Option<(&str, bool)>,
) -> String {
    let rows = data::library_rows(s, query).await;
    let query_string = query.to_query();
    let body = html! {
        div id="library-page" {
            div class="page-heading" {
                div { h2 { "Library" } p class="muted" { "Wanted episodes and subtitle actions." } }
                a class="btn" href=(if query_string.is_empty() { "/ui/library".to_string() } else { format!("/ui/library?{query_string}") }) { "Refresh" }
            }
            (flash_banner(flash))
            form id="lib-filters" class="filters" method="get" action="/ui/library" {
                input type="search" name="q" value=(query.q) placeholder="Search title or id" autocomplete="off";
                select name="scope" {
                    option value="" selected[(query.scope != "active")] { "All" }
                    option value="active" selected[(query.scope == "active")] { "Active only" }
                }
                select name="sort" {
                    option value="" selected[(query.sort != "id")] { "Sort: title" }
                    option value="id" selected[(query.sort == "id")] { "Sort: id" }
                }
                select name="dir" {
                    option value="" selected[(query.dir != "desc")] { "Asc" }
                    option value="desc" selected[(query.dir == "desc")] { "Desc" }
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
                            @for row in &rows {
                                tr {
                                    td { (row.title) }
                                    td { span class="pill" { (row.kind) } }
                                    td class="muted" { (row.missing) }
                                    td class="muted" { (row.done) }
                                    td {
                                        div class="row-actions" {
                                            form method="post" action=(data::ep_action_url(&row.aid, "retry", &query_string)) {
                                                button class="btn" type="submit" { "Retry" }
                                            }
                                            form method="post" action=(data::ep_action_url(&row.aid, "skip", &query_string)) {
                                                button class="btn" type="submit" { "Skip" }
                                            }
                                            @if row.excluded {
                                                form method="post" action=(data::ep_action_url(&row.aid, "unexclude", &query_string)) {
                                                    button class="btn" type="submit" { "Unexclude" }
                                                }
                                            } @else {
                                                form method="post" action=(data::ep_action_url(&row.aid, "exclude", &query_string)) {
                                                    button class="btn" type="submit" { "Exclude" }
                                                }
                                            }
                                            form method="post" action=(data::ep_action_url(&row.aid, "delete", &query_string)) {
                                                button class="btn danger" type="submit" { "Delete" }
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
    };
    layout(&s.cfg, "library", body, None)
}

pub(crate) fn activity_page(s: &Arc<AppState>) -> String {
    let items = crate::api::activity_items(60, &s.cfg.state_file);
    let body = html! {
        div id="activity-page" {
            div class="page-heading" {
                div { h2 { "Activity" } p class="muted" { "Recent pipeline ledger entries." } }
                a class="btn" href="/ui/activity" { "Refresh" }
            }
            div class="card" {
                @if items.is_empty() {
                    div class="empty" { "No activity yet." }
                } @else {
                    table {
                        thead { tr { th { "When" } th { "Kind" } th { "Episode" } th { "Lang" } th { "Detail" } } }
                        tbody {
                            @for item in &items {
                                tr {
                                    td class="muted" { (item["ts"].as_str().unwrap_or("")) }
                                    td { span class="pill" { (item["kind"].as_str().unwrap_or("")) } }
                                    td { (item["episode_id"].as_i64().map(|id| id.to_string()).unwrap_or_default()) }
                                    td { (item["language"].as_str().unwrap_or("—")) }
                                    td { (item["detail"].as_str().unwrap_or("")) }
                                }
                            }
                        }
                    }
                }
            }
        }
    };
    layout(&s.cfg, "activity", body, Some(("/ui/activity", 10)))
}

pub(crate) fn provenance_page(s: &Arc<AppState>) -> String {
    let rows: Vec<state::RegistryRow> = state::load_jsonl(&s.cfg.registry_file);
    let mut by_language: HashMap<String, usize> = HashMap::new();
    for row in &rows {
        if let Some(language) = row.lang.as_deref() {
            *by_language
                .entry(crate::lang::normalize_lang(language))
                .or_default() += 1;
        }
    }
    let mut languages: Vec<(String, usize)> = by_language.into_iter().collect();
    languages.sort_by_key(|(_, count)| std::cmp::Reverse(*count));
    let recent: Vec<&state::RegistryRow> = rows.iter().rev().take(50).collect();
    let body = html! {
        div id="provenance-page" {
            div class="page-heading" {
                div { h2 { "Provenance" } p class="muted" { "Subtitle registry and source languages." } }
                a class="btn" href="/ui/provenance" { "Refresh" }
            }
            div class="card" {
                h2 { "Subtitle provenance" }
                div class="muted" { (rows.len()) " registry row(s)" }
                div class="row-actions mt-s" {
                    @for (language, count) in &languages { span class="pill" { (language) " " (count) } }
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
                            @for row in &recent {
                                tr {
                                    td class="muted" { (row.ts.as_deref().unwrap_or("")) }
                                    td class="mono" { (row.stem.as_deref().unwrap_or("")) }
                                    td { (row.lang.as_deref().unwrap_or("")) }
                                    td { (row.source.as_deref().unwrap_or("")) }
                                    td { (row.episode_id.map(|id| id.to_string()).unwrap_or_default()) }
                                }
                            }
                        }
                    }
                }
            }
        }
    };
    layout(&s.cfg, "provenance", body, Some(("/ui/provenance", 20)))
}

pub(crate) fn settings_page(cfg: &Config, message: Option<(bool, String)>) -> String {
    let pinned = data::pinned_keys();
    let body = html! {
        div id="settings-page" {
            div class="page-heading" {
                div { h2 { "Settings" } p class="muted" { "Persisted overrides apply after a daemon restart." } }
                a class="btn" href="/ui/settings" { "Refresh" }
            }
            div class="card" {
                div class="banner" {
                    "Edits are written to " code { "config.overrides.json" } ", which beats "
                    code { "pipeline.env" } " but not the container environment. Restart the daemon to apply. "
                    "Secret fields left blank stay unchanged. Credential-shaped values are masked; type a new value to change one."
                    @if !pinned.is_empty() {
                        br;
                        span class="muted" {
                            "Read-only here — the deployment environment sets: "
                            code { (pinned.join(", ")) } ". Change them in the compose .env."
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
                @if let Some((ok, text)) = &message {
                    div class=(if *ok { "banner ok" } else { "banner err" }) role="alert" { (text) }
                }
                form method="post" action="/ui/config" {
                    @for (group_id, group_title) in FIELD_GROUPS {
                        @if FIELDS.iter().any(|field| field.group == *group_id) {
                            fieldset {
                                legend { (group_title) }
                                @for field in FIELDS.iter().filter(|field| field.group == *group_id) {
                                    @let current = data::field_display_value(cfg, field.key, field.default);
                                    @let is_pinned = pinned.contains(&field.key);
                                    div class="field" {
                                        label for=(field.key) { (field.label) }
                                        @match field.kind {
                                            FieldKind::Secret => {
                                                @if is_pinned {
                                                    input type="text" id=(field.key) value="***" readonly disabled;
                                                } @else {
                                                    input type="password" id=(field.key) name=(field.key) value="" placeholder="unchanged" autocomplete="new-password";
                                                    input type="hidden" name=(format!("orig__{}", field.key)) value="";
                                                }
                                            }
                                            FieldKind::Bool => {
                                                @if is_pinned {
                                                    input type="checkbox" id=(field.key) checked[data::truthy(&current)] disabled;
                                                } @else {
                                                    input type="checkbox" id=(field.key) name=(field.key) checked[data::truthy(&current)];
                                                    input type="hidden" name=(format!("orig__{}", field.key)) value=(current);
                                                }
                                            }
                                            FieldKind::Int(_) => {
                                                @if is_pinned {
                                                    input type="number" id=(field.key) value=(current) readonly disabled;
                                                } @else {
                                                    input type="number" id=(field.key) name=(field.key) value=(current);
                                                    input type="hidden" name=(format!("orig__{}", field.key)) value=(current);
                                                }
                                            }
                                            _ => {
                                                @if is_pinned {
                                                    input type="text" id=(field.key) value=(current) readonly disabled;
                                                } @else {
                                                    input type="text" id=(field.key) name=(field.key) value=(current);
                                                    input type="hidden" name=(format!("orig__{}", field.key)) value=(current);
                                                }
                                            }
                                        }
                                        div class="help" {
                                            (field.help)
                                            @if is_pinned { " Set by the deployment environment; edit the compose .env instead." }
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
    };
    layout(cfg, "settings", body, None)
}
