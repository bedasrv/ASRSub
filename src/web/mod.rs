mod actions;
mod data;
mod pages;

use std::sync::Arc;

use axum::extract::rejection::QueryRejection;
use axum::extract::{Query, State};
use axum::http::{header, StatusCode};
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
    query: Result<Query<data::LibQuery>, QueryRejection>,
) -> Response {
    let Query(query) = match query {
        Ok(query) => query,
        Err(rejection) => {
            let _ = rejection;
            return (
                StatusCode::BAD_REQUEST,
                Html(
                    pages::library_page(
                        &s,
                        &data::LibQuery::default(),
                        Some(("Library filters could not be decoded.", true)),
                    )
                    .await,
                ),
            )
                .into_response();
        }
    };
    Html(pages::library_page(&s, &query, None).await).into_response()
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
    use std::sync::Arc;

    use axum::Router;

    async fn serve(app: Router) -> String {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move {
            let _ = axum::serve(listener, app).await;
        });
        base
    }

    fn test_app() -> (Router, tempfile::TempDir) {
        let dir = tempfile::tempdir().unwrap();
        let prior_dir = std::env::var_os("ASRSUB_CONFIG_DIR");
        std::env::set_var("ASRSUB_CONFIG_DIR", dir.path());
        let cfg = crate::config::Config::load().unwrap();
        match prior_dir {
            Some(value) => std::env::set_var("ASRSUB_CONFIG_DIR", value),
            None => std::env::remove_var("ASRSUB_CONFIG_DIR"),
        }
        let http = reqwest::Client::new();
        let pool = crate::providers::ProviderPool::new(
            crate::providers::ProvidersFile {
                llm_translation_models: vec![],
                whisper_stt: None,
                whisper_stt_fallbacks: vec![],
            },
            http.clone(),
        );
        let pipeline = Arc::new(crate::pipeline::Pipeline::new(cfg.clone(), pool, http));
        (
            crate::api::router(crate::api::AppState::new(cfg, pipeline)),
            dir,
        )
    }

    async fn response_text(response: reqwest::Response) -> String {
        let body = response.text().await.unwrap();
        assert!(
            body.starts_with("<!DOCTYPE html>"),
            "not a complete page: {body}"
        );
        body
    }

    #[test]
    fn page_urls_are_normal_browser_routes() {
        assert_eq!(
            super::data::ep_action_url("m:7", "retry", "q=x"),
            "/ui/episode/m:7/retry?q=x"
        );
    }

    #[tokio::test]
    async fn malformed_library_query_is_a_complete_bad_request() {
        let (app, _dir) = test_app();
        let base = serve(app).await;
        let response = reqwest::get(format!("{base}/ui/library?q=first&q=second"))
            .await
            .unwrap();
        assert_eq!(response.status(), reqwest::StatusCode::BAD_REQUEST);
        let body = response_text(response).await;
        assert!(body.contains("Library filters could not be decoded."));
        assert!(!body.contains("Failed to deserialize"));
    }

    #[tokio::test]
    async fn malformed_episode_query_still_returns_bad_request_without_local_user_authentication() {
        let (app, _dir) = test_app();
        let base = serve(app).await;
        let client = reqwest::Client::new();
        let response = client
            .post(format!("{base}/ui/episode/1/retry?q=first&q=second"))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), reqwest::StatusCode::BAD_REQUEST);
        let body = response_text(response).await;
        assert!(body.contains("Library filters could not be decoded."));
    }

    #[tokio::test]
    async fn valid_episode_query_is_processed_without_local_user_authentication() {
        let (app, _dir) = test_app();
        let base = serve(app).await;
        let response = reqwest::Client::new()
            .post(format!("{base}/ui/episode/1/retry?q=needle&scope=active"))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), reqwest::StatusCode::OK);
        let body = response_text(response).await;
        assert!(body.contains("name=\"q\" value=\"needle\""));
        assert!(body.contains("option value=\"active\" selected"));
    }

    #[tokio::test]
    async fn malformed_settings_form_is_a_safe_bad_request() {
        let (app, _dir) = test_app();
        let base = serve(app).await;
        let response = reqwest::Client::new()
            .post(format!("{base}/ui/config"))
            .header("Content-Type", "application/json")
            .body("{\"MAX_EPS_PER_RUN\":4}")
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), reqwest::StatusCode::BAD_REQUEST);
        let body = response_text(response).await;
        assert!(body.contains("Settings form could not be decoded."));
        assert!(!body.contains("Failed to deserialize"));
    }
}
