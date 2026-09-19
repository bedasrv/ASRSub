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
//! * E — movie end-to-end: Radarr movie discovered, transcribed, uploaded,
//!   committed with `kind: movie` (a series row sharing the numeric id
//!   must not suppress it).
//! * F — stale done self-heals: deleted sidecar resurfaces as missing.
//! * G — upload retries (2×500 then 204 → 3 attempts, done) and
//!   Bazarr-down grace (all-500 → still done, sidecar authoritative).
//! * H — an untagged track with no detectable language fails the language
//!   (no sidecar, no upload, no `done` row).
//! * I — the successful detection path: the same untagged track, but the
//!   provider reports `language`, so the probe (which must carry no
//!   `language` field) establishes the source and the run completes with the
//!   detected code in the registry.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
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
    upload_attempts: AtomicU32,
    upload_failures: AtomicU32,
    whisper_hits: Mutex<u32>,
    whisper_broken_hits: Mutex<u32>,
    llm_hits: Mutex<u32>,
    refill_hits: Mutex<u32>,
    movie_on: AtomicBool,
    wanted_id_missing: AtomicBool,
    /// Detected `language` the Whisper stub reports (`None` omits it, like a
    /// provider that cannot detect). Phase I sets it to "ja" so the
    /// successful-detection path is covered end to end.
    detected_language: Mutex<Option<String>>,
    /// Raw multipart bodies the Whisper stub received, in order: proves the
    /// unforced probe carried no `language` form field.
    whisper_bodies: Mutex<Vec<String>>,
}

#[derive(Clone)]
struct StubCx {
    stubs: Arc<Stubs>,
    media: PathBuf,
    movie: PathBuf,
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
async fn bazarr_wanted(State(cx): State<StubCx>) -> impl IntoResponse {
    let mut missing = vec![];
    if cx.stubs.wanted_id_missing.load(Ordering::Relaxed) {
        missing.push(serde_json::json!({"code2": "id"}));
    }
    missing.push(serde_json::json!({"code2": "en"}));
    Json(serde_json::json!({
        "total": 1,
        "data": [{
            "sonarrEpisodeId": 7,
            "seriesId": 11,
            "seriesTitle": "TestShow",
            "missing_subtitles": missing,
        }],
    }))
}

async fn bazarr_movies(State(cx): State<StubCx>) -> impl IntoResponse {
    let data = if cx.stubs.movie_on.load(Ordering::Relaxed) {
        vec![serde_json::json!({
            "radarrId": 100,
            "monitored": true,
            "path": cx.movie.to_string_lossy(),
            "title": "TestMovie",
        })]
    } else {
        vec![]
    };
    Json(serde_json::json!({"total": data.len(), "data": data}))
}

/// Bazarr manual upload: record `language`, answer 204 like production.
/// `upload_failures` armed → that many 500s first (retry drill).
async fn bazarr_upload(
    State(cx): State<StubCx>,
    Query(q): Query<HashMap<String, String>>,
    _body: Bytes,
) -> impl IntoResponse {
    cx.stubs.upload_attempts.fetch_add(1, Ordering::SeqCst);
    if cx
        .stubs
        .upload_failures
        .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| {
            if n > 0 {
                Some(n - 1)
            } else {
                None
            }
        })
        .is_ok()
    {
        return StatusCode::INTERNAL_SERVER_ERROR.into_response();
    }
    let lang = q.get("language").cloned().unwrap_or_default();
    cx.stubs.uploads.lock().await.push(lang);
    StatusCode::NO_CONTENT.into_response()
}

async fn bazarr_refill(State(cx): State<StubCx>) -> impl IntoResponse {
    *cx.stubs.refill_hits.lock().await += 1;
    StatusCode::NO_CONTENT
}

/// LLM stub: parse the `{lines}` array out of the user message and return
/// exactly that many latin lines (passes wrong-script + echo guards).
async fn llm_chat(
    State(cx): State<StubCx>,
    Json(body): Json<serde_json::Value>,
) -> impl IntoResponse {
    *cx.stubs.llm_hits.lock().await += 1;
    let n = body
        .get("messages")
        .and_then(|m| m.as_array())
        .and_then(|m| {
            m.iter()
                .find(|x| x.get("role").and_then(|r| r.as_str()) == Some("user"))
        })
        .and_then(|u| u.get("content"))
        .and_then(|c| c.as_str())
        .and_then(|s| serde_json::from_str::<serde_json::Value>(s).ok())
        .and_then(|v| v.get("lines").and_then(|l| l.as_array()).map(|a| a.len()))
        .unwrap_or(0);
    let out: Vec<String> = (0..n).map(|i| format!("Hasil {i}")).collect();
    Json(
        serde_json::json!({"choices": [{"message": {"content": serde_json::to_string(&out).unwrap()}}]}),
    )
}

/// Whisper stub: one fixed Japanese segment per call, plus the configured
/// detected `language` (absent unless a phase asks for detection). Records
/// every received multipart body so a phase can assert what was NOT sent.
async fn whisper_stt(State(cx): State<StubCx>, body: Bytes) -> impl IntoResponse {
    *cx.stubs.whisper_hits.lock().await += 1;
    cx.stubs
        .whisper_bodies
        .lock()
        .await
        .push(String::from_utf8_lossy(&body).to_string());
    let mut v = serde_json::json!({
        "segments": [{"start": 0.0, "end": 2.5, "text": "こんにちは世界これはテストです"}],
    });
    if let Some(lang) = cx.stubs.detected_language.lock().await.clone() {
        v["language"] = serde_json::json!(lang);
    }
    Json(v)
}

/// Broken Whisper stub: always 500 (failover drill target).
async fn whisper_broken(State(cx): State<StubCx>, _body: Bytes) -> impl IntoResponse {
    *cx.stubs.whisper_broken_hits.lock().await += 1;
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        Json(serde_json::json!({"error": "drill failure"})),
    )
}

fn router(cx: StubCx) -> axum::Router {
    axum::Router::new()
        .route("/episode/{id}", get(sonarr_episode))
        .route("/series", get(sonarr_series))
        .route("/episodes/wanted", get(bazarr_wanted))
        .route("/movies", get(bazarr_movies))
        .route("/episodes/subtitles", post(bazarr_upload))
        .route("/movies/subtitles", post(bazarr_upload))
        .route("/system/tasks", post(bazarr_refill))
        .route("/chat/completions", post(llm_chat))
        .route("/audio/transcriptions", post(whisper_stt))
        .route("/audio-broken", post(whisper_broken))
        .with_state(cx)
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

    // Fake ffprobe: canned streams / duration regardless of input
    // (combined query returns both sections, like the real tool).
    write_exe(
        &bin_dir.join("ffprobe"),
        r#"#!/bin/sh
if printf '%s' "$*" | /usr/bin/grep -q "stream=index"; then
  printf '{"streams":[{"index":1,"codec_name":"aac","codec_type":"audio","tags":{"language":"jpn"}}],"format":{"duration":"300.0"}}'
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
    // One stub server for every remote dependency.
    let stubs = Arc::new(Stubs::default());
    stubs.wanted_id_missing.store(true, Ordering::Relaxed);
    let movie = media_dir.join("movie.mkv");
    let cx = StubCx {
        stubs: stubs.clone(),
        media: media.clone(),
        movie: movie.clone(),
    };
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move {
        let _ = axum::serve(listener, router(cx)).await;
    });

    let http = reqwest::Client::builder().build().unwrap();
    let file = |p: &str| cfg_dir.join(p);
    let _cfg = crate::config::Config {
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
        nas_media_prefix: "/mnt/nas/share/media".into(),
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
        ladder_min_cues: 40,
        ladder_min_chars: 1500,
        ladder_min_cjk: 0.6,
        ladder_span_tol: 0.15,
        anilist_cache: file("anilist.json"),
        max_cue_ms: 8000,
    };
    let pool = crate::providers::ProviderPool::new(
        crate::providers::ProvidersFile {
            llm_translation_models: vec![crate::providers::LlmProvider {
                endpoint: format!("{base}/chat/completions"),
                model: "stub".into(),
                key_env: String::new(),
                api_key: "x".into(),
                probe_latency_s: 0.01,
                thinking_param_accepted: false,
            }],
            whisper_stt: Some(crate::providers::WhisperProvider {
                endpoint: format!("{base}/audio/transcriptions"),
                model: "stub".into(),
                key_env: String::new(),
                api_key: "x".into(),
            }),
            whisper_stt_fallbacks: vec![],
        },
        http.clone(),
    );
    let tools = crate::feature_modules::process::ToolPaths::for_test(
        bin_dir.join("ffmpeg"),
        bin_dir.join("ffprobe"),
    );
    let pipe = crate::pipeline::Pipeline::new_with_tools(_cfg.clone(), pool, http.clone(), tools);

    // ---- Phase A: ASR path (no ja sidecar) ----
    let stats = pipe.run_pass_with_tools(&pipe.tools).await;
    assert_eq!(
        (stats.scanned, stats.processed, stats.done, stats.failed),
        (1, 1, 1, 0),
        "run_pass compatibility fields must retain their legacy meanings"
    );
    // One shared transcription for both languages.
    assert_eq!(*stubs.whisper_hits.lock().await, 1);
    // Both target sidecars land with the AI marker.
    for lang in ["id", "en"] {
        let p = format!("{stem_s}.{lang}.hi.srt");
        let text = std::fs::read_to_string(&p).unwrap();
        assert!(
            text.contains("[AI-generated by ASRSub]"),
            "{p} missing marker"
        );
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
    let rows = crate::state::load_jsonl::<crate::state::StateEntry>(&pipe.cfg.state_file);
    assert_eq!(
        rows.iter()
            .filter(|r| r.status.as_deref() == Some("done"))
            .count(),
        2
    );
    let reg = crate::state::load_jsonl::<crate::state::RegistryRow>(&pipe.cfg.registry_file);
    assert_eq!(reg.len(), 2);
    assert!(reg.iter().all(|r| r.source.as_deref() == Some("asr")));
    assert!(reg.iter().all(|r| r.source_kind.is_none()));
    // Provenance records the real source: the `jpn` track on stream 1.
    for r in &reg {
        assert_eq!(
            r.extra.get("source_lang").and_then(|v| v.as_str()),
            Some("ja"),
            "row missing source_lang: {r:?}"
        );
        assert_eq!(
            r.extra.get("source_stream").and_then(|v| v.as_u64()),
            Some(1),
            "row missing source_stream: {r:?}"
        );
    }

    // A translation failure is a typed target failure. It must not install a
    // blank sidecar, upload it, or admit a ledger row for the target.
    let failing_pool = crate::providers::ProviderPool::new(
        crate::providers::ProvidersFile {
            llm_translation_models: vec![],
            whisper_stt: Some(crate::providers::WhisperProvider {
                endpoint: format!("{base}/audio/transcriptions"),
                model: "stub".into(),
                key_env: String::new(),
                api_key: "x".into(),
            }),
            whisper_stt_fallbacks: vec![],
        },
        http.clone(),
    );
    let failing_pipe = crate::pipeline::Pipeline::new_with_tools(
        _cfg.clone(),
        failing_pool,
        http.clone(),
        pipe.tools.clone(),
    );
    let failing_candidate = crate::pipeline::Candidate {
        episode_id: 8,
        series_id: Some(11),
        series_title: "TestShow".into(),
        path: None,
        missing: vec!["fr".into()],
        is_movie: false,
        original_lang: None,
    };
    let series_titles = HashMap::from([(
        11,
        crate::sonarr::SeriesInfo {
            title: "TestShow".into(),
            original_language: None,
        },
    )]);
    let outcome = failing_pipe
        .process_one(&failing_candidate, &series_titles)
        .await
        .unwrap();
    let report = match outcome {
        crate::feature_modules::discord_types::EpisodeRunResult::Report(report) => report,
        other => panic!("unexpected translation-failure outcome: {other:?}"),
    };
    let target = report
        .targets()
        .as_slice()
        .iter()
        .find(|target| target.language().as_str() == "fr")
        .expect("translation failure target");
    assert!(matches!(
        target.status(),
        crate::feature_modules::discord_types::TargetStatus::Failed {
            class: crate::feature_modules::discord_types::FailureClass::Translation
        }
    ));
    assert!(!Path::new(&format!("{stem_s}.fr.hi.srt")).exists());
    let registry =
        crate::state::load_jsonl::<crate::state::RegistryRow>(&failing_pipe.cfg.registry_file);
    assert!(!registry
        .iter()
        .any(|row| row.episode_id == Some(8) && row.lang.as_deref() == Some("fr")));
    assert!(!stubs.uploads.lock().await.iter().any(|lang| lang == "fr"));

    // ---- Phase B: ladder path (adequate ja sidecar, zero new ASR) ----
    std::fs::remove_file(&pipe.cfg.state_file).ok();
    std::fs::remove_file(&pipe.cfg.registry_file).ok();
    for lang in ["id", "en"] {
        std::fs::remove_file(format!("{stem_s}.{lang}.hi.srt")).ok();
    }
    stubs.uploads.lock().await.clear();
    std::fs::write(format!("{stem_s}.ja.srt"), ladder_ja_fixture()).unwrap();
    let whisper_before_ladder = *stubs.whisper_hits.lock().await;
    let stats = pipe.run_pass_with_tools(&pipe.tools).await;
    assert_eq!((stats.scanned, stats.done, stats.failed), (1, 1, 0));
    assert_eq!(
        *stubs.whisper_hits.lock().await,
        whisper_before_ladder,
        "ladder must skip ASR"
    );
    assert!(*stubs.llm_hits.lock().await > 0);
    let reg = crate::state::load_jsonl::<crate::state::RegistryRow>(&pipe.cfg.registry_file);
    assert_eq!(reg.len(), 2);
    assert!(reg.iter().all(|r| r.source.as_deref() == Some("jpn")));
    assert!(reg
        .iter()
        .all(|r| r.source_kind.as_deref() == Some("external")));
    // The doc claim is "`source_stream` on ASR rows only, `source_lang` on
    // every row": the ladder path had no assertion for either key, so a
    // ladder row could start carrying `source_stream` (or lose
    // `source_lang`) unseen.
    for r in &reg {
        assert!(
            !r.extra.contains_key("source_stream"),
            "ladder row must not carry source_stream: {r:?}"
        );
        assert_eq!(
            r.extra.get("source_lang").and_then(|v| v.as_str()),
            Some("ja"),
            "ladder row missing source_lang: {r:?}"
        );
    }

    // ---- Phase C: actions round-trip ----
    // Retry(id) regenerates IN THE SAME PASS even though Bazarr does not
    // report id missing (the production win: no waiting for Bazarr's
    // rescan + a later pass — the retry resolves straight from Sonarr).
    stubs.wanted_id_missing.store(false, Ordering::Relaxed);
    std::fs::write(
        &pipe.cfg.actions_file,
        "{\"type\":\"retry\",\"episode_id\":7,\"kind\":\"series\",\"language\":\"id\"}\n",
    )
    .unwrap();
    let stats = pipe.run_pass_with_tools(&pipe.tools).await;
    assert_eq!((stats.scanned, stats.done), (1, 1));
    stubs.wanted_id_missing.store(true, Ordering::Relaxed);
    let rows = crate::state::load_jsonl::<crate::state::StateEntry>(&pipe.cfg.state_file);
    assert!(rows
        .iter()
        .any(|r| r.language.as_deref() == Some("id") && r.status.as_deref() == Some("done")));
    assert!(
        *stubs.refill_hits.lock().await >= 1,
        "retry must refill wanted"
    );
    // Skip filters the pass even with a missing language.
    std::fs::remove_file(format!("{stem_s}.en.hi.srt")).ok();
    std::fs::write(&pipe.cfg.state_file, "").unwrap();
    std::fs::write(
        &pipe.cfg.actions_file,
        "{\"type\":\"skip\",\"episode_id\":7}\n",
    )
    .unwrap();
    let stats = pipe.run_pass_with_tools(&pipe.tools).await;
    assert_eq!(stats.scanned, 0, "skip must filter the candidate");
    // Actions consumed: next pass sees the missing language again.
    let stats = pipe.run_pass_with_tools(&pipe.tools).await;
    assert_eq!((stats.scanned, stats.done), (1, 1));

    // ---- Phase D: whisper failover (primary 500s, fallback serves) ----
    std::fs::remove_file(&pipe.cfg.state_file).ok();
    std::fs::remove_file(&pipe.cfg.registry_file).ok();
    for lang in ["id", "en", "ja"] {
        std::fs::remove_file(format!("{stem_s}.{lang}.srt")).ok();
        std::fs::remove_file(format!("{stem_s}.{lang}.hi.srt")).ok();
    }
    stubs.uploads.lock().await.clear();
    let whisper_before = *stubs.whisper_hits.lock().await;
    let failover_pool = crate::providers::ProviderPool::new(
        crate::providers::ProvidersFile {
            llm_translation_models: vec![crate::providers::LlmProvider {
                endpoint: format!("{base}/chat/completions"),
                model: "stub".into(),
                key_env: String::new(),
                api_key: "x".into(),
                probe_latency_s: 0.01,
                thinking_param_accepted: false,
            }],
            whisper_stt: Some(crate::providers::WhisperProvider {
                endpoint: format!("{base}/audio-broken"),
                model: "stub".into(),
                key_env: String::new(),
                api_key: "x".into(),
            }),
            whisper_stt_fallbacks: vec![crate::providers::WhisperProvider {
                endpoint: format!("{base}/audio/transcriptions"),
                model: "stub".into(),
                key_env: String::new(),
                api_key: "x".into(),
            }],
        },
        http.clone(),
    );
    let pipe2 = crate::pipeline::Pipeline::new_with_tools(
        pipe.cfg.clone(),
        failover_pool,
        http,
        pipe.tools.clone(),
    );
    let stats = pipe2.run_pass_with_tools(&pipe2.tools).await;
    assert_eq!((stats.scanned, stats.done, stats.failed), (1, 1, 0));
    // Primary attempted (and failed) first, fallback served the shared
    // transcription — one serving hit, not two.
    assert_eq!(*stubs.whisper_broken_hits.lock().await, 1);
    assert_eq!(*stubs.whisper_hits.lock().await, whisper_before + 1);
    let mut ups = stubs.uploads.lock().await.clone();
    ups.sort();
    assert_eq!(ups, vec!["en".to_string(), "id".to_string()]);

    // ---- Phase E: movie end-to-end (kind-aware, no series collision) ----
    // Series ep7 keeps its Phase-D sidecars, so the only candidate is the
    // movie. A SERIES done row sharing the numeric id must not suppress it.
    std::fs::write(&movie, b"not real media").unwrap();
    let mstem_s = movie.with_extension("").to_string_lossy().to_string();
    crate::state::append_jsonl(
        &pipe.cfg.state_file,
        &serde_json::json!({"sonarrEpisodeId": 100, "language": "id", "status": "done"}),
    )
    .unwrap();
    stubs.movie_on.store(true, Ordering::Relaxed);
    stubs.uploads.lock().await.clear();
    let stats = pipe.run_pass_with_tools(&pipe.tools).await;
    assert_eq!((stats.scanned, stats.done, stats.failed), (1, 1, 0));
    for lang in ["id", "en"] {
        let p = format!("{mstem_s}.{lang}.hi.srt");
        let text = std::fs::read_to_string(&p).unwrap();
        assert!(
            text.contains("[AI-generated by ASRSub]"),
            "{p} missing marker"
        );
        assert!(text.contains("Hasil 0"), "{p} missing translation");
    }
    let mut ups = stubs.uploads.lock().await.clone();
    ups.sort();
    assert_eq!(ups, vec!["en".to_string(), "id".to_string()]);
    // Both movie languages committed as kind=movie; the decoy series row
    // for id 100 is still there, untouched and irrelevant.
    let rows = crate::state::load_jsonl::<crate::state::StateEntry>(&pipe.cfg.state_file);
    for lang in ["id", "en"] {
        assert!(
            rows.iter().any(|r| r.kind.as_deref() == Some("movie")
                && r.episode_id == Some(100)
                && r.language.as_deref() == Some(lang)
                && r.status.as_deref() == Some("done")),
            "missing movie done row for {lang}"
        );
    }
    let reg = crate::state::load_jsonl::<crate::state::RegistryRow>(&pipe.cfg.registry_file);
    assert_eq!(
        reg.iter()
            .filter(|r| r.extra.get("kind").and_then(|v| v.as_str()) == Some("movie"))
            .count(),
        2
    );

    // ---- Phase F: stale done self-heals (series + movie symmetry) ----
    // The done rows say complete, but the series id sidecar is gone: the
    // next pass must resurface it as missing and regenerate it, while the
    // intact movie sidecars stay untouched (no re-upload).
    std::fs::remove_file(format!("{stem_s}.id.hi.srt")).unwrap();
    stubs.uploads.lock().await.clear();
    let whisper_before = *stubs.whisper_hits.lock().await;
    let stats = pipe.run_pass_with_tools(&pipe.tools).await;
    assert_eq!((stats.scanned, stats.done, stats.failed), (1, 1, 0));
    let text = std::fs::read_to_string(format!("{stem_s}.id.hi.srt")).unwrap();
    assert!(text.contains("[AI-generated by ASRSub]"));
    assert_eq!(*stubs.whisper_hits.lock().await, whisper_before + 1);
    let ups = stubs.uploads.lock().await.clone();
    assert_eq!(ups, vec!["id".to_string()]);

    // ---- Phase G: upload retries, then Bazarr-down grace ----
    // Two 500s then 204: the shared 3-attempt policy (None/429/5xx retry,
    // 204 breaks, 400/401/404 fail fast) must deliver exactly 3 attempts
    // and still complete the episode.
    std::fs::remove_file(format!("{mstem_s}.id.hi.srt")).unwrap();
    stubs.uploads.lock().await.clear();
    stubs.upload_attempts.store(0, Ordering::SeqCst);
    stubs.upload_failures.store(2, Ordering::SeqCst);
    let stats = pipe.run_pass_with_tools(&pipe.tools).await;
    assert_eq!((stats.scanned, stats.done, stats.failed), (1, 1, 0));
    assert_eq!(stubs.upload_attempts.load(Ordering::SeqCst), 3);
    let text = std::fs::read_to_string(format!("{mstem_s}.id.hi.srt")).unwrap();
    assert!(text.contains("[AI-generated by ASRSub]"));
    // Bazarr permanently down: the episode still completes — the sidecar
    // on disk is authoritative, the upload is best-effort delivery.
    std::fs::remove_file(format!("{mstem_s}.en.hi.srt")).unwrap();
    stubs.upload_attempts.store(0, Ordering::SeqCst);
    stubs.upload_failures.store(99, Ordering::SeqCst);
    let stats = pipe.run_pass_with_tools(&pipe.tools).await;
    assert_eq!((stats.scanned, stats.done, stats.failed), (1, 1, 0));
    assert_eq!(stubs.upload_attempts.load(Ordering::SeqCst), 3);
    assert!(std::path::Path::new(&format!("{mstem_s}.en.hi.srt")).is_file());

    // ---- Phase H: an untagged track with no detectable language fails ----
    // No language tag -> detection mode; the stub Whisper reports no
    // `language`, so the source cannot be established. The language must
    // fail: no sidecar written, no upload, nothing committed done.
    stubs.movie_on.store(false, Ordering::Relaxed);
    for lang in ["id", "en"] {
        std::fs::remove_file(format!("{stem_s}.{lang}.hi.srt")).ok();
    }
    std::fs::write(&pipe.cfg.state_file, "").unwrap();
    std::fs::remove_file(&pipe.cfg.registry_file).ok();
    stubs.uploads.lock().await.clear();
    write_exe(
        &bin_dir.join("ffprobe"),
        r#"#!/bin/sh
if printf '%s' "$*" | /usr/bin/grep -q "stream=index"; then
  printf '{"streams":[{"index":1,"codec_name":"aac","codec_type":"audio"}],"format":{"duration":"300.0"}}'
else
  printf '{"format":{"duration":"300.0"}}'
fi
"#,
    );
    let stats = pipe.run_pass_with_tools(&pipe.tools).await;
    assert_eq!(
        (stats.scanned, stats.done, stats.failed),
        (1, 0, 1),
        "undetectable source language must fail the language"
    );
    for lang in ["id", "en"] {
        assert!(
            !std::path::Path::new(&format!("{stem_s}.{lang}.hi.srt")).exists(),
            "no sidecar may be written for an unestablished source language"
        );
    }
    assert!(
        crate::state::load_jsonl::<crate::state::RegistryRow>(&pipe.cfg.registry_file).is_empty()
    );
    assert!(stubs.uploads.lock().await.is_empty());
    let rows = crate::state::load_jsonl::<crate::state::StateEntry>(&pipe.cfg.state_file);
    assert!(rows.iter().any(|r| r.status.as_deref() == Some("error")));

    // ---- Phase I: successful detection (round-2 coverage) ----
    // The same untagged track, but the provider now reports a language. The
    // probe must still be unforced (no `language` field), and the detected
    // code must complete the run: sidecar written, uploaded, and recorded as
    // the episode's `source_lang`.
    *stubs.detected_language.lock().await = Some("ja".to_string());
    std::fs::write(&pipe.cfg.state_file, "").unwrap();
    std::fs::remove_file(&pipe.cfg.registry_file).ok();
    stubs.uploads.lock().await.clear();
    stubs.upload_failures.store(0, Ordering::SeqCst);
    let bodies_before = stubs.whisper_bodies.lock().await.len();
    let hits_before = *stubs.whisper_hits.lock().await;
    let stats = pipe.run_pass_with_tools(&pipe.tools).await;
    assert_eq!(
        (stats.scanned, stats.done, stats.failed),
        (1, 1, 0),
        "a detected source language must complete the episode"
    );
    assert_eq!(*stubs.whisper_hits.lock().await, hits_before + 1);
    let probes: Vec<String> = stubs.whisper_bodies.lock().await[bodies_before..].to_vec();
    assert_eq!(probes.len(), 1, "one transcription serves both targets");
    assert!(
        !probes[0].contains("name=\"language\""),
        "the detection probe must send no `language` field: {}",
        probes[0]
    );
    for lang in ["id", "en"] {
        let p = format!("{stem_s}.{lang}.hi.srt");
        let text = std::fs::read_to_string(&p).unwrap();
        assert!(
            text.contains("[AI-generated by ASRSub]"),
            "{p} missing marker"
        );
        assert!(text.contains("Hasil 0"), "{p} missing translation");
    }
    let mut ups = stubs.uploads.lock().await.clone();
    ups.sort();
    assert_eq!(ups, vec!["en".to_string(), "id".to_string()]);
    let reg = crate::state::load_jsonl::<crate::state::RegistryRow>(&pipe.cfg.registry_file);
    assert_eq!(reg.len(), 2);
    for r in &reg {
        assert_eq!(r.source.as_deref(), Some("asr"), "row is not ASR: {r:?}");
        assert_eq!(
            r.extra.get("source_lang").and_then(|v| v.as_str()),
            Some("ja"),
            "the detected code must be the recorded source: {r:?}"
        );
        assert_eq!(
            r.extra.get("source_stream").and_then(|v| v.as_u64()),
            Some(1),
            "ASR rows record the chosen stream: {r:?}"
        );
    }
    assert_eq!(
        crate::state::load_jsonl::<crate::state::StateEntry>(&pipe.cfg.state_file)
            .iter()
            .filter(|r| r.status.as_deref() == Some("done"))
            .count(),
        2
    );

    // ---- Phase J: sibling target reduction keeps one success + one failure ----
    // Re-run both missing targets. Make the id registry row contradict the
    // strict identity while leaving en unchanged; the pass must retain both
    // target outcomes and reduce them to a partial report.
    for lang in ["id", "en"] {
        std::fs::remove_file(format!("{stem_s}.{lang}.hi.srt")).ok();
    }
    std::fs::write(&pipe.cfg.state_file, "").unwrap();
    let mut registry =
        crate::state::load_jsonl::<crate::state::RegistryRow>(&pipe.cfg.registry_file);
    for row in &mut registry {
        if row.lang.as_deref() == Some("id") {
            row.source = Some("contradiction".to_string());
        }
    }
    crate::state::rewrite_jsonl(&pipe.cfg.registry_file, &registry).unwrap();
    assert_eq!(
        registry
            .iter()
            .find(|row| row.lang.as_deref() == Some("id"))
            .and_then(|row| row.source.as_deref()),
        Some("contradiction")
    );
    let outcome = pipe.run_pass_outcome().await;
    let (stats, reports, omitted) = outcome.into_parts();
    assert_eq!(
        (stats.scanned, stats.processed, stats.done, stats.failed),
        (1, 1, 1, 1)
    );
    assert_eq!(omitted, 0);
    assert_eq!(reports.len(), 1);
    let report = reports.iter().next().unwrap();
    assert_eq!(
        report.aggregate(),
        crate::feature_modules::discord_types::AggregateDisposition::Partial,
        "target outcomes: {:?}",
        report.targets()
    );
    assert!(report.targets().as_slice().iter().any(|target| {
        target.language().as_str() == "id"
            && matches!(
                target.status(),
                crate::feature_modules::discord_types::TargetStatus::Failed {
                    class: crate::feature_modules::discord_types::FailureClass::Storage
                }
            )
    }));
    assert!(report.targets().as_slice().iter().any(|target| {
        target.language().as_str() == "en"
            && matches!(
                target.status(),
                crate::feature_modules::discord_types::TargetStatus::Completed { .. }
            )
    }));

    // A later pass must not promote the installed-but-unadmitted id sidecar.
    let retry = pipe.run_pass_outcome().await;
    let (retry_stats, retry_reports, retry_omitted) = retry.into_parts();
    assert_eq!(
        (
            retry_stats.scanned,
            retry_stats.processed,
            retry_stats.done,
            retry_stats.failed
        ),
        (1, 1, 0, 1)
    );
    assert_eq!(retry_omitted, 0);
    assert_eq!(retry_reports.len(), 1);
    assert_eq!(
        retry_reports.iter().next().unwrap().aggregate(),
        crate::feature_modules::discord_types::AggregateDisposition::Failed
    );
    let rows = crate::state::load_jsonl::<crate::state::StateEntry>(&pipe.cfg.state_file);
    assert!(!rows.iter().any(|row| {
        row.language.as_deref() == Some("id") && row.status.as_deref() == Some("done")
    }));
}

#[cfg(test)]
mod tests {
    #[test]
    fn core_acceptance_manifest_lists_named_tests() {
        let manifest: serde_json::Value = serde_json::from_str(include_str!(
            "../tests/fixtures/core_acceptance/acceptance-manifest.json"
        ))
        .expect("core acceptance manifest JSON");
        let names = manifest["tests"].as_array().expect("tests array");
        let source = concat!(
            include_str!("main.rs"),
            include_str!("api.rs"),
            include_str!("pipeline.rs"),
            include_str!("episode.rs"),
            include_str!("sim.rs"),
            include_str!("discord_renderer.rs"),
            include_str!("discord_state.rs"),
            include_str!("discord_transport.rs"),
            include_str!("../tests/pipeline_rs.rs")
        );
        for name in names {
            let name = name.as_str().expect("named acceptance test");
            let symbol = name.rsplit("::").next().unwrap_or(name);
            assert!(
                source.contains(&format!("fn {symbol}")),
                "missing acceptance selector {name}"
            );
        }
    }
}
