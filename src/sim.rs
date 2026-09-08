//! Offline full-program simulation (test-only, never compiled into the binary).
//!
//! Drives a real [`crate::pipeline::Pipeline`] against hermetic fakes — no
//! network beyond localhost, no GPUs, no model weights, no real media tools:
//!
//! * HTTP stubs (axum, one port): Sonarr (`/episode/:id`, `/series`),
//!   Bazarr (`/episodes/wanted`, `/movies`, `/episodes/subtitles`,
//!   `/system/tasks`), LLM (`/chat/completions`, echoes N latin lines for N
//!   input lines), Whisper (`/audio/transcriptions`, fixed `verbose_json`).
//! * Fake `ffprobe`/`ffmpeg` shell scripts prepended to a test-only `PATH`:
//!   canned audio-stream JSON, canned 300 s duration, canned audio bytes.
//!   The pipeline shells out exactly like production; it cannot tell.
//! * Real temp files for everything else: media, sidecars, state, registry,
//!   actions, glossary. All assertions read on-disk artifacts.
//!
//! Phases (one `#[tokio::test]`, sequential — it owns the process `PATH`):
//! * A — ASR path: no ja sidecar → stub-Whisper transcribe → stub-LLM
//!   translate (id+en share one transcription) → upload → registry/state.
//! * B — ladder path: adequate ja sidecar → zero new Whisper hits.
//! * C — actions round-trip: retry regenerates, skip filters the pass.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::body::Bytes;
use axum::extract::{Path as AxPath, Query, State};
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::Json;
use tokio::sync::Mutex;

/// Shared stub observations.
#[derive(Default)]
struct Stubs {
    uploads: Mutex<Vec<String>>,
    whisper_hits: Mutex<u32>,
    llm_hits: Mutex<u32>,
    refill_hits: Mutex<u32>,
}

#[derive(Clone)]
struct StubCx {
    stubs: Arc<Stubs>,
    media: PathBuf,
}

/// Sonarr `/episode/:id`: one monitored episode WITH a media file.
async fn sonarr_episode(AxPath(id): AxPath<i64>, State(cx): State<StubCx>) -> impl IntoResponse {
    Json(serde_json::json!({
        "id": id,
        "seriesId": 11,
        "seasonNumber": 1,
        "episodeNumber": 2,
        "title": "Ep Two",
        "monitored": true,
        "hasFile": true,
        "episodeFile": {"path": cx.media.to_string_lossy()},
    }))
}

async fn sonarr_series() -> impl IntoResponse {
    Json(vec![serde_json::json!({"id": 11, "title": "TestShow"})])
}

/// Bazarr `/episodes/wanted`: episode 7 missing id+en.
async fn bazarr_wanted() -> impl IntoResponse {
    Json(serde_json::json!({
        "total": 1,
        "data": [{
            "sonarrEpisodeId": 7,
            "seriesId": 11,
            "seriesTitle": "TestShow",
            "missing_subtitles": [{"code2": "id"}, {"code2": "en"}],
        }],
    }))
}

async fn bazarr_movies() -> impl IntoResponse {
    Json(serde_json::json!({"total": 0, "data": []}))
}

/// Bazarr manual upload: record `language`, answer 204 like production.
async fn bazarr_upload(
    State(cx): State<StubCx>,
    Query(q): Query<HashMap<String, String>>,
    _body: Bytes,
) -> impl IntoResponse {
    let lang = q.get("language").cloned().unwrap_or_default();
    cx.stubs.uploads.lock().await.push(lang);
    StatusCode::NO_CONTENT
}

async fn bazarr_refill(State(cx): State<StubCx>) -> impl IntoResponse {
    *cx.stubs.refill_hits.lock().await += 1;
    StatusCode::NO_CONTENT
}

/// LLM stub: parse the `{lines}` array out of the user message and return
/// exactly that many latin lines (passes wrong-script + echo guards).
async fn llm_chat(State(cx): State<StubCx>, Json(body): Json<serde_json::Value>) -> impl IntoResponse {
    *cx.stubs.llm_hits.lock().await += 1;
    let n = body
        .get("messages")
        .and_then(|m| m.as_array())
        .and_then(|m| m.iter().find(|x| x.get("role").and_then(|r| r.as_str()) == Some("user")))
        .and_then(|u| u.get("content"))
        .and_then(|c| c.as_str())
        .and_then(|s| serde_json::from_str::<serde_json::Value>(s).ok())
        .and_then(|v| v.get("lines").and_then(|l| l.as_array()).map(|a| a.len()))
        .unwrap_or(0);
    let out: Vec<String> = (0..n).map(|i| format!("Hasil {i}")).collect();
    Json(serde_json::json!({"choices": [{"message": {"content": serde_json::to_string(&out).unwrap()}}]}))
}

/// Whisper stub: one fixed Japanese segment per call.
async fn whisper_stt(State(cx): State<StubCx>, _body: Bytes) -> impl IntoResponse {
    *cx.stubs.whisper_hits.lock().await += 1;
    Json(serde_json::json!({
        "segments": [{"start": 0.0, "end": 2.5, "text": "こんにちは世界これはテストです"}],
    }))
}

fn router(cx: StubCx) -> axum::Router {
    axum::Router::new()
        .route("/episode/:id", get(sonarr_episode))
        .route("/series", get(sonarr_series))
        .route("/episodes/wanted", get(bazarr_wanted))
        .route("/movies", get(bazarr_movies))
        .route("/episodes/subtitles", post(bazarr_upload))
        .route("/movies/subtitles", post(bazarr_upload))
        .route("/system/tasks", post(bazarr_refill))
        .route("/chat/completions", post(llm_chat))
        .route("/audio/transcriptions", post(whisper_stt))
        .with_state(cx)
}

/// Restores process env on drop (the sim owns `PATH` while it runs).
struct EnvGuard {
    saved: Vec<(&'static str, Option<String>)>,
}

impl EnvGuard {
    fn set(key: &'static str, val: &str) -> Self {
        Self {
            saved: vec![(key, std::env::var(key).ok())],
        }
        .and_set(key, val)
    }

    fn and_set(mut self, key: &'static str, val: &str) -> Self {
        if !self.saved.iter().any(|(k, _)| *k == key) {
            self.saved.push((key, std::env::var(key).ok()));
        }
        unsafe { std::env::set_var(key, val) };
        self
    }

    fn scrub_proxy(mut self) -> Self {
        for k in [
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ] {
            if !self.saved.iter().any(|(x, _)| *x == k) {
                self.saved.push((k, std::env::var(k).ok()));
            }
            unsafe { std::env::remove_var(k) };
        }
        self
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        for (k, v) in self.saved.drain(..) {
            match v {
                Some(val) => unsafe { std::env::set_var(k, val) },
                None => unsafe { std::env::remove_var(k) },
            }
        }
    }
}

fn write_exe(path: &Path, body: &str) {
    std::fs::write(path, body).unwrap();
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).unwrap();
}

/// 40 adequate Japanese cues spanning ~266 s of the fake 300 s duration.
fn ladder_ja_fixture() -> String {
    let mut s = String::new();
    for i in 0..40 {
        let start = 1000 + i * 6800;
        let end = start + 5000;
        s.push_str(&format!(
            "{}\n{} --> {}\n{}\n\n",
            i + 1,
            crate::srt::fmt_ts(start),
            crate::srt::fmt_ts(end),
            "あ".repeat(45),
        ));
    }
    s
}

fn state_rows(path: &Path) -> Vec<crate::state::StateEntry> {
    crate::state::load_jsonl(path)
}

fn registry_rows(path: &Path) -> Vec<crate::state::RegistryRow> {
    crate::state::load_jsonl(path)
}

/// Full-program simulation: stub services + fake media tools, real pipeline.
/// See the module docs for the phase plan.
#[tokio::test]
async fn simulate_library_pass() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let media_dir = root.join("media");
    let cfg_dir = root.join("cfg");
    let tmp_dir = root.join("tmp");
    let bin_dir = root.join("bin");
    for d in [&media_dir, &cfg_dir, &tmp_dir, &bin_dir] {
        std::fs::create_dir_all(d).unwrap();
    }
    let media = media_dir.join("ep.mkv");
    std::fs::write(&media, b"not real media").unwrap();
    let stem = media.with_extension("");
    let stem_s = stem.to_string_lossy().to_string();
    std::fs::write(cfg_dir.join("glossary.json"), "{}").unwrap();

    // Fake ffprobe: canned streams / duration regardless of input.
    write_exe(
        &bin_dir.join("ffprobe"),
        r#"#!/bin/sh
if printf '%s' "$*" | grep -q "stream=index"; then
  printf '{"streams":[{"index":1,"codec_name":"aac","codec_type":"audio","tags":{"language":"jpn"}}]}'
else
  printf '{"format":{"duration":"300.0"}}'
fi
"#,
    );
    // Fake ffmpeg: materialize the requested output (audio bytes / srt).
    write_exe(
        &bin_dir.join("ffmpeg"),
        r#"#!/bin/sh
out=""
for a in "$@"; do out="$a"; done
case "$out" in
  *.mp3) printf 'FAKEAUDIO' > "$out" ;;
  *.srt) printf '1\n00:00:01,000 --> 00:00:02,000\nfake\n\n' > "$out" ;;
  *) : > "$out" ;;
esac
exit 0
"#,
    );
    let orig_path = std::env::var("PATH").unwrap_or_default();
    let _env = EnvGuard::set(
        "PATH",
        &format!("{}:{orig_path}", bin_dir.to_string_lossy()),
    )
    .scrub_proxy();

    // One stub server for every remote dependency.
    let stubs = Arc::new(Stubs::default());
    let cx = StubCx {
        stubs: stubs.clone(),
        media: media.clone(),
    };
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move {
        let _ = axum::serve(listener, router(cx)).await;
    });

    let http = reqwest::Client::builder().build().unwrap();
    let file = |p: &str| cfg_dir.join(p);
    let cfg = crate::config::Config {
        raw: HashMap::new(),
        sonarr_url: base.clone(),
        sonarr_api_key: "t".into(),
        bazarr_url: base.clone(),
        bazarr_api_key: "t".into(),
        bazarr_url_2: None,
        bazarr_api_key_2: String::new(),
        jellyfin_url: base.clone(),
        jellyfin_api_key: String::new(),
        jellyfin_media_root: "/media".into(),
        jimaku_api_key: String::new(),
        jimaku_direct_enabled: false,
        target_langs: vec!["id".into(), "en".into()],
        max_eps_per_run: 8,
        translate_chunk: 10,
        episode_concurrency: 2,
        asr_concurrency: 2,
        translate_concurrency: 4,
        upload_concurrency: 2,
        tmp_dir: tmp_dir.clone(),
        state_file: file("state.jsonl"),
        actions_file: file("actions.jsonl"),
        exclusions_file: file("exclusions.jsonl"),
        registry_file: file("registry.jsonl"),
        refine_state_file: file("refine.jsonl"),
        providers_file: file("providers.json"),
        glossary_file: file("glossary.json"),
        ai_marker_cue: true,
        ai_marker_cue_ms: 1500,
        sdh_placeholders: vec!["（歌詞）".into()],
        cps_merge_max: 20.0,
        cps_merge_max_chars: 84,
        cps_merge_max_dur_ms: 7000,
        cps_merge_max_gap_ms: 1000,
        webhook_port: 18085,
        control_api_key_file: file("control.key"),
        ladder_min_cues: 40,
        ladder_min_chars: 1500,
        ladder_min_cjk: 0.6,
        ladder_span_tol: 0.15,
        anilist_cache: file("anilist.json"),
    };
    let pool = crate::providers::ProviderPool::new(
        crate::providers::ProvidersFile {
            llm_translation_models: vec![crate::providers::LlmProvider {
                provider: "stub".into(),
                endpoint: format!("{base}/chat/completions"),
                base_url: base.clone(),
                model: "stub".into(),
                key_env: String::new(),
                api_key: "x".into(),
                probe_latency_s: 0.01,
                thinking_param_accepted: false,
                request_shape: None,
            }],
            whisper_stt: Some(crate::providers::WhisperProvider {
                provider: "stub".into(),
                endpoint: format!("{base}/audio/transcriptions"),
                model: "stub".into(),
                key_env: String::new(),
                api_key: "x".into(),
                rate_usd_per_audio_sec: None,
                via_upstream: None,
                request_shape: None,
            }),
        },
        http.clone(),
    );
    let pipe = crate::pipeline::Pipeline::new(cfg, pool, http);

    // ---- Phase A: ASR path (no ja sidecar) ----
    let stats = pipe.run_pass().await;
    assert_eq!((stats.scanned, stats.done, stats.failed), (1, 1, 0));
    // One shared transcription for both languages.
    assert_eq!(*stubs.whisper_hits.lock().await, 1);
    // Both target sidecars land with the AI marker.
    for lang in ["id", "en"] {
        let p = format!("{stem_s}.{lang}.hi.srt");
        let text = std::fs::read_to_string(&p).unwrap();
        assert!(text.contains("[AI-generated by ASRSub]"), "{p} missing marker");
        assert!(text.contains("Hasil 0"), "{p} missing translation");
    }
    // No temp audio leaks.
    let leftovers: Vec<_> = std::fs::read_dir(&tmp_dir)
        .unwrap()
        .filter_map(|e| e.ok())
        .collect();
    assert!(leftovers.is_empty(), "temp leak: {leftovers:?}");
    // Uploads + ledger rows.
    let mut ups = stubs.uploads.lock().await.clone();
    ups.sort();
    assert_eq!(ups, vec!["en".to_string(), "id".to_string()]);
    let rows = state_rows(&pipe.cfg.state_file);
    assert_eq!(rows.iter().filter(|r| r.status.as_deref() == Some("done")).count(), 2);
    let reg = registry_rows(&pipe.cfg.registry_file);
    assert_eq!(reg.len(), 2);
    assert!(reg.iter().all(|r| r.source.as_deref() == Some("asr")));
    assert!(reg.iter().all(|r| r.source_kind.is_none()));

    // ---- Phase B: ladder path (adequate ja sidecar, zero new ASR) ----
    std::fs::remove_file(&pipe.cfg.state_file).ok();
    std::fs::remove_file(&pipe.cfg.registry_file).ok();
    for lang in ["id", "en"] {
        std::fs::remove_file(format!("{stem_s}.{lang}.hi.srt")).ok();
    }
    stubs.uploads.lock().await.clear();
    std::fs::write(format!("{stem_s}.ja.srt"), ladder_ja_fixture()).unwrap();
    let stats = pipe.run_pass().await;
    assert_eq!((stats.scanned, stats.done, stats.failed), (1, 1, 0));
    assert_eq!(*stubs.whisper_hits.lock().await, 1, "ladder must skip ASR");
    assert!(*stubs.llm_hits.lock().await > 0);
    let reg = registry_rows(&pipe.cfg.registry_file);
    assert_eq!(reg.len(), 2);
    assert!(reg.iter().all(|r| r.source.as_deref() == Some("jpn")));
    assert!(reg
        .iter()
        .all(|r| r.source_kind.as_deref() == Some("external")));

    // ---- Phase C: actions round-trip ----
    // Retry(id) regenerates just that language.
    std::fs::write(
        &pipe.cfg.actions_file,
        "{\"type\":\"retry\",\"episode_id\":7,\"kind\":\"series\",\"language\":\"id\"}\n",
    )
    .unwrap();
    let stats = pipe.run_pass().await;
    assert_eq!((stats.scanned, stats.done), (1, 1));
    let rows = state_rows(&pipe.cfg.state_file);
    assert!(rows.iter().any(|r| r.language.as_deref() == Some("id")
        && r.status.as_deref() == Some("done")));
    assert!(*stubs.refill_hits.lock().await >= 1, "retry must refill wanted");
    // Skip filters the pass even with a missing language.
    std::fs::remove_file(format!("{stem_s}.en.hi.srt")).ok();
    std::fs::write(&pipe.cfg.state_file, "").unwrap();
    std::fs::write(&pipe.cfg.actions_file, "{\"type\":\"skip\",\"episode_id\":7}\n").unwrap();
    let stats = pipe.run_pass().await;
    assert_eq!(stats.scanned, 0, "skip must filter the candidate");
    // Actions consumed: next pass sees the missing language again.
    let stats = pipe.run_pass().await;
    assert_eq!((stats.scanned, stats.done), (1, 1));
}
