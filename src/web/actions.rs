use std::collections::HashMap;
use std::sync::atomic::Ordering;
use std::sync::Arc;

use axum::extract::rejection::{FormRejection, QueryRejection};
use axum::extract::{Form, Path, Query, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{Html, IntoResponse, Response};
use serde_json::Value;

use crate::api::AppState;
use crate::config::Config;
use crate::state;

use super::data::{self, LibQuery};
use super::pages;

pub(crate) const UNAUTH_MSG: &str =
    "Wrong or missing control key. Enter it in the header and click Unlock, then retry.";
const LIB_QUERY_MSG: &str = "Library filters could not be decoded.";

fn redirect(location: String) -> Response {
    (StatusCode::SEE_OTHER, [(header::LOCATION, location)]).into_response()
}

fn html_error(status: StatusCode, page: String) -> Response {
    (status, Html(page)).into_response()
}

fn library_location(query: &LibQuery) -> String {
    let encoded = query.to_query();
    if encoded.is_empty() {
        "/ui/library".to_string()
    } else {
        format!("/ui/library?{encoded}")
    }
}

pub(crate) async fn h_config_save(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    form: Result<Form<HashMap<String, String>>, FormRejection>,
) -> Response {
    if !crate::api::check_token(&s.cfg, &headers) {
        return html_error(
            StatusCode::UNAUTHORIZED,
            pages::settings_page(&s.cfg, Some((false, UNAUTH_MSG.to_string()))),
        );
    }
    let Form(form) = match form {
        Ok(form) => form,
        Err(rejection) => {
            let _ = rejection;
            return html_error(
                StatusCode::BAD_REQUEST,
                pages::settings_page(
                    &s.cfg,
                    Some((false, "Settings form could not be decoded.".to_string())),
                ),
            );
        }
    };

    let shadowed = data::shadowed_attempts(&s.cfg, &form);
    let pairs = data::collect_changes(&form);
    let view = || Config::load().unwrap_or_else(|_| s.cfg.clone());

    if !shadowed.is_empty() {
        return html_error(
            StatusCode::BAD_REQUEST,
            pages::settings_page(
                &view(),
                Some((
                    false,
                    format!(
                        "{} is set by the deployment environment and cannot be changed here. Nothing saved.",
                        shadowed.join(", ")
                    ),
                )),
            ),
        );
    }
    if pairs.is_empty() {
        return Html(pages::settings_page(
            &view(),
            Some((true, "No changes to save.".to_string())),
        ))
        .into_response();
    }
    if let Some((bad, want)) = data::invalid_number(&pairs) {
        return html_error(
            StatusCode::BAD_REQUEST,
            pages::settings_page(
                &view(),
                Some((false, format!("{bad} must be {want}. Nothing saved."))),
            ),
        );
    }
    if let Err(error) = crate::config::write_overrides(&pairs) {
        return html_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            pages::settings_page(&view(), Some((false, format!("Save failed: {error}")))),
        );
    }
    redirect("/ui/settings".to_string())
}

pub(crate) async fn h_control(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(action): Path<String>,
) -> Response {
    if !crate::api::check_token(&s.cfg, &headers) {
        return html_error(
            StatusCode::UNAUTHORIZED,
            pages::status_page(&s, Some((UNAUTH_MSG, true))).await,
        );
    }
    match action.as_str() {
        "pause" => s.paused.store(true, Ordering::Relaxed),
        "resume" => {
            s.paused.store(false, Ordering::Relaxed);
            s.wake.notify_one();
        }
        "run-once" => {
            s.run_once.store(true, Ordering::Relaxed);
            s.wake.notify_one();
        }
        "wake" => s.wake.notify_one(),
        _ => {
            return html_error(
                StatusCode::NOT_FOUND,
                pages::status_page(&s, Some(("Unknown control action.", true))).await,
            );
        }
    }
    redirect("/ui/status".to_string())
}

pub(crate) async fn h_episode_action(
    State(s): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((id, action)): Path<(String, String)>,
    query: Result<Query<LibQuery>, QueryRejection>,
) -> Response {
    if !crate::api::check_token(&s.cfg, &headers) {
        let query = match query {
            Ok(Query(query)) => query,
            Err(_) => LibQuery::default(),
        };
        return html_error(
            StatusCode::UNAUTHORIZED,
            pages::library_page(&s, &query, Some((UNAUTH_MSG, true))).await,
        );
    }
    let Query(query) = match query {
        Ok(query) => query,
        Err(rejection) => {
            let _ = rejection;
            return html_error(
                StatusCode::BAD_REQUEST,
                pages::library_page(&s, &LibQuery::default(), Some((LIB_QUERY_MSG, true))).await,
            );
        }
    };
    let (episode_id, kind) = match crate::api::parse_episode_id(&id) {
        Ok(value) => value,
        Err(error) => {
            return html_error(
                StatusCode::BAD_REQUEST,
                pages::library_page(&s, &query, Some((&error, true))).await,
            );
        }
    };

    match action.as_str() {
        "exclude" => {
            let record = serde_json::json!({
                "episode_id": episode_id,
                "reason": "ui",
                "ts": state::utc_now_iso(),
            });
            if let Err(error) = state::append_jsonl(&s.cfg.exclusions_file, &record) {
                return html_error(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    pages::library_page(
                        &s,
                        &query,
                        Some((&format!("Exclude failed: {error}"), true)),
                    )
                    .await,
                );
            }
        }
        "unexclude" => {
            #[derive(serde::Deserialize, serde::Serialize)]
            struct Exclusion {
                #[serde(default)]
                episode_id: Option<i64>,
                #[serde(flatten)]
                rest: HashMap<String, Value>,
            }
            let rows: Vec<Exclusion> = state::load_jsonl(&s.cfg.exclusions_file);
            let kept: Vec<Exclusion> = rows
                .into_iter()
                .filter(|row| row.episode_id != Some(episode_id))
                .collect();
            if let Err(error) = state::rewrite_jsonl(&s.cfg.exclusions_file, &kept) {
                return html_error(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    pages::library_page(
                        &s,
                        &query,
                        Some((&format!("Unexclude failed: {error}"), true)),
                    )
                    .await,
                );
            }
        }
        typ @ ("retry" | "skip" | "delete") => {
            if let Err(error) = crate::api::enqueue_action(&s.cfg, typ, episode_id, kind, None) {
                return html_error(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    pages::library_page(
                        &s,
                        &query,
                        Some((&format!("Queue failed: {error}"), true)),
                    )
                    .await,
                );
            }
            if typ != "skip" {
                s.wake.notify_one();
            }
        }
        _ => {
            return html_error(
                StatusCode::NOT_FOUND,
                pages::library_page(&s, &query, Some(("Unknown episode action.", true))).await,
            );
        }
    }
    redirect(library_location(&query))
}
