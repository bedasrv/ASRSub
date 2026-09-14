mod actions;
mod data;
mod pages;

use std::sync::Arc;

use axum::extract::{Query, State};
use axum::http::header;
use axum::response::{Html, IntoResponse, Response};
use axum::routing::{get, post};

use crate::api::AppState;

const APP_JS: &str = include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/assets/app.js"));
const APP_CSS: &str = include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/assets/app.css"));

pub(crate) fn routes() -> axum::Router<Arc<AppState>> {
    axum::Router::new()
        .route("/ui/status", get(h_status))
        // Keep the old overview URL as a normal full-page alias.
        .route("/ui/overview", get(h_status))
        .route("/ui/library", get(h_library))
        .route("/ui/activity", get(h_activity))
        .route("/ui/provenance", get(h_provenance))
        .route("/ui/settings", get(h_settings))
        .route("/ui/config", post(actions::h_config_save))
        .route("/ui/control/{action}", post(actions::h_control))
        .route("/ui/episode/{id}/{action}", post(actions::h_episode_action))
        .route("/assets/app.js", get(h_app_js))
        .route("/assets/app.css", get(h_app_css))
}

pub async fn h_index(State(s): State<Arc<AppState>>) -> Html<String> {
    Html(pages::status_page(&s, None).await)
}

async fn h_status(State(s): State<Arc<AppState>>) -> Html<String> {
    Html(pages::status_page(&s, None).await)
}

async fn h_library(
    State(s): State<Arc<AppState>>,
    Query(query): Query<data::LibQuery>,
) -> Html<String> {
    Html(pages::library_page(&s, &query, None).await)
}

async fn h_activity(State(s): State<Arc<AppState>>) -> Html<String> {
    Html(pages::activity_page(&s))
}

async fn h_provenance(State(s): State<Arc<AppState>>) -> Html<String> {
    Html(pages::provenance_page(&s))
}

async fn h_settings(State(s): State<Arc<AppState>>) -> Html<String> {
    Html(pages::settings_page(&s.cfg, None))
}

async fn h_app_js() -> Response {
    (
        [(
            header::CONTENT_TYPE,
            "application/javascript; charset=utf-8",
        )],
        APP_JS,
    )
        .into_response()
}

async fn h_app_css() -> Response {
    ([(header::CONTENT_TYPE, "text/css; charset=utf-8")], APP_CSS).into_response()
}

#[cfg(test)]
mod tests {
    #[test]
    fn page_urls_are_normal_browser_routes() {
        assert_eq!(
            super::data::ep_action_url("m:7", "retry", "q=x"),
            "/ui/episode/m:7/retry?q=x"
        );
    }
}
