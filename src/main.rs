//! asrsub — remote-API subtitle pipeline.
//!
//! ```text
//! asrsub daemon              # self-looping daemon (wanted -> ASR -> translate -> upload)
//! asrsub run-once             # single pass then exit
//! asrsub transcribe -i EP.mkv -o EP.ja.srt [--lang ja]
//! asrsub translate-file -i EP.ja.srt -t id -o EP.id.srt
//! asrsub refine --ja EP.ja.srt --tr EP.id.srt
//! asrsub health | config-show
//! ```
//!
//! All inference is remote (`asrsub_providers.json`); this binary links no
//! ML weights and needs no GPU — only ffmpeg/ffprobe + network.

#![allow(dead_code)]

mod actions;
mod api;
mod asr;
#[cfg(test)]
mod asr_child_tests;
mod bazarr;
mod config;
mod deployment_modules;
mod episode;
mod feature_modules;
mod glossary;
mod jellyfin;
mod jimaku;
mod ladder;
mod lang;
mod pipeline;
mod providers;
#[cfg(test)]
mod sim;
mod sonarr;
mod srt;
mod state;
mod translate;
mod web;

use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;
use std::sync::Arc;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};

use crate::srt::Cue;

#[derive(Parser, Debug)]
#[command(name = "asrsub", version, about = "Remote-API subtitle pipeline")]
struct Cli {
    /// Config dir override (default ~/.config/asr-pipeline, or `ASRSUB_CONFIG_DIR`)
    ///
    /// Declared without clap's `env =` on purpose: clap rejects an empty env
    /// value outright (`error: a value is required for '--config-dir'`), which
    /// would abort the daemon where the documented rule is that an empty
    /// variable is simply unset. The env var is read through `config::env_str`,
    /// the same reader every other key uses.
    #[arg(long)]
    config_dir: Option<PathBuf>,
    /// Providers file (default ./asrsub_providers.json, or `PROVIDERS_FILE`)
    ///
    /// Same reason as `config_dir` for the missing `env =` attribute.
    #[arg(long)]
    providers_file: Option<PathBuf>,
    #[command(subcommand)]
    cmd: Option<Cmd>,
}

#[derive(Subcommand, Debug)]
enum Cmd {
    /// Self-looping daemon (default when no subcommand is given).
    Daemon,
    /// Single pipeline pass then exit.
    RunOnce,
    /// Transcribe media audio via remote Whisper to SRT.
    Transcribe {
        #[arg(short, long)]
        input: PathBuf,
        #[arg(short, long)]
        output: Option<PathBuf>,
        /// With `--stream N`: the language pinned for that track (must be one
        /// the provider accepts — an unpinnable code is rejected, never
        /// silently dropped). Without it: the target language the source
        /// track is chosen for.
        #[arg(long, default_value = "ja")]
        lang: String,
        /// Audio stream index (default: auto-pick by the track's language tag,
        /// detecting it when the tag is unknown).
        #[arg(long)]
        stream: Option<u32>,
    },
    /// Translate an SRT file via remote LLM.
    TranslateFile {
        #[arg(short, long)]
        input: PathBuf,
        #[arg(short, long)]
        output: Option<PathBuf>,
        #[arg(short, long, default_value = "id")]
        target: String,
        #[arg(long, default_value = "Japanese")]
        source: String,
        #[arg(long)]
        series: Option<String>,
    },
    /// Review an SRT translation against its JA source (remote LLM).
    Refine {
        #[arg(long)]
        ja: PathBuf,
        #[arg(long)]
        tr: PathBuf,
        #[arg(long)]
        write: bool,
    },
    /// Check control API health.
    Health,
    /// Print masked merged config as JSON.
    ConfigShow,
    /// Release-bound child environment audit (hidden).
    #[command(name = "__audit-child-environments", hide = true)]
    AuditChildEnvironments,
}

/// Renders an error and its causes the way `Result`'s own printer would, with
/// every cause masked. This is the single sink for error text: a cause can carry
/// a configured value without the site that built it knowing — `reqwest` puts the
/// request URL into its error, the providers loader puts the path into its
/// "tried" list — so masking the rendered chain closes the class instead of
/// chasing the sites. It is a function rather than a block inside `main` so a
/// test can pin the layout and the mask together; a grep over the source cannot.
fn masked_error_report(e: &anyhow::Error) -> String {
    let mask = |t: String| crate::config::mask_for_log(&t).into_owned();
    let mut out = format!("Error: {}", mask(e.to_string()));
    let causes: Vec<String> = e.chain().skip(1).map(|c| mask(c.to_string())).collect();
    match causes.len() {
        0 => {}
        1 => out.push_str(&format!("\n\nCaused by:\n    {}", causes[0])),
        _ => {
            out.push_str("\n\nCaused by:");
            for (i, cause) in causes.iter().enumerate() {
                out.push_str(&format!("\n    {i}: {cause}"));
            }
        }
    }
    out
}

fn main() {
    let cli = Cli::parse();
    init_tracing();
    // Optional config-dir override before Config::load reads the env.
    if let Some(d) = cli.config_dir.clone() {
        std::env::set_var("ASRSUB_CONFIG_DIR", &d);
    }
    let rt = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .thread_name("asrsub")
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("Error: {e}");
            std::process::exit(1);
        }
    };
    let Err(e) = rt.block_on(async_main(cli)) else {
        return;
    };
    eprint!("{}", masked_error_report(&e));
    std::process::exit(1);
}

fn init_tracing() {
    use tracing_subscriber::{fmt, EnvFilter};
    let filter =
        EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("asrsub=info"));
    // Logs go to stderr: CLI data output (JSON/stats on stdout) stays
    // machine-parseable (`asrsub run-once | jq`, integration tests).
    let _ = fmt()
        .with_env_filter(filter)
        .with_target(false)
        .with_writer(std::io::stderr)
        .try_init();
}

fn build_http() -> reqwest::Client {
    reqwest::Client::builder()
        .pool_max_idle_per_host(32)
        .pool_idle_timeout(std::time::Duration::from_secs(90))
        .tcp_keepalive(std::time::Duration::from_secs(60))
        .connect_timeout(std::time::Duration::from_secs(10))
        .build()
        .expect("http client")
}

async fn load_stack(
    providers_override: Option<PathBuf>,
) -> Result<(config::Config, providers::ProviderPool, reqwest::Client)> {
    let cfg = config::Config::load()?;
    // There is no site-specific JELLYFIN_URL default: a set key with no URL
    // leaves refresh disabled. Say so loudly rather than failing silently.
    if !cfg.jellyfin_api_key.is_empty() && cfg.jellyfin_url.is_empty() {
        tracing::warn!(
            "JELLYFIN_API_KEY is set but JELLYFIN_URL is empty; Jellyfin refresh is disabled. Set JELLYFIN_URL explicitly."
        );
    }
    // Validate the media mount at startup: a vanished NAS is the most common
    // cause of "healthy but doing nothing". `/ready` reports the same check
    // and the daemon degrades (no destructive cleanup) rather than exiting.
    if !std::path::Path::new(&cfg.nas_media_prefix).is_dir() {
        tracing::warn!(
            prefix = %crate::config::mask_for_log(&cfg.nas_media_prefix),
            "media root is not a directory; /ready will report not-ready until the mount is fixed"
        );
    }
    let http = build_http();
    let ppath = providers_override
        .or_else(|| crate::config::env_str("PROVIDERS_FILE").map(PathBuf::from))
        .unwrap_or_else(|| cfg.providers_file.clone());
    // The path is operator input (`PROVIDERS_FILE`), and these strings reach the
    // log and the CLI, so the same mask that guards `/config` guards them.
    let pshow = crate::config::mask_for_log(&ppath.display().to_string()).into_owned();
    let file = providers::ProvidersFile::load(&ppath)
        .with_context(|| format!("load providers {pshow:?}"))?;
    if file.llm_translation_models.is_empty() {
        anyhow::bail!("no llm_translation_models in {pshow:?}");
    }
    if file.whisper_stt.is_none() {
        tracing::warn!("no whisper_stt provider configured; ASR will fail");
    }
    let pool = providers::ProviderPool::new(file, http.clone());
    Ok((cfg, pool, http))
}

async fn async_main(cli: Cli) -> Result<()> {
    match cli.cmd {
        None | Some(Cmd::Daemon) => daemon(cli.providers_file).await,
        Some(Cmd::RunOnce) => {
            let (cfg, pool, http) = load_stack(cli.providers_file).await?;
            let pipe = pipeline::Pipeline::new(cfg, pool, http);
            let stats = pipe.run_pass().await;
            tracing::info!(?stats, "pass finished");
            println!(
                "{}",
                serde_json::to_string_pretty(&serde_json::json!({
                    "scanned": stats.scanned, "processed": stats.processed,
                    "done": stats.done, "failed": stats.failed, "skipped": stats.skipped,
                }))?
            );
            Ok(())
        }
        Some(Cmd::Transcribe {
            input,
            output,
            lang,
            stream,
        }) => transcribe_cmd(cli.providers_file, &input, output.as_deref(), &lang, stream).await,
        Some(Cmd::TranslateFile {
            input,
            output,
            target,
            source,
            series,
        }) => {
            translate_file_cmd(
                cli.providers_file,
                &input,
                output.as_deref(),
                &target,
                &source,
                series.as_deref(),
            )
            .await
        }
        Some(Cmd::Refine { ja, tr, write }) => {
            refine_cmd(cli.providers_file, &ja, &tr, write).await
        }
        Some(Cmd::Health) => {
            // Config-only: health must not require the providers file.
            let cfg = config::Config::load()?;
            println!(
                "{}",
                serde_json::to_string_pretty(&serde_json::json!({
                    "config_ok": true,
                    "sonarr": !cfg.sonarr_url.is_empty(),
                    "bazarr": !cfg.bazarr_url.is_empty(),
                }))?
            );
            Ok(())
        }
        Some(Cmd::ConfigShow) => {
            // Config-only: never requires the providers file.
            let cfg = config::Config::load()?;
            println!("{}", serde_json::to_string_pretty(&cfg.masked())?);
            Ok(())
        }
        Some(Cmd::AuditChildEnvironments) => crate::feature_modules::child_audit::run(),
    }
}

/// The language an explicit `--stream N` run pins.
///
/// The caller names it, so a code the provider is not measured to accept is
/// an error rather than a silent downgrade: the wire filter would drop the
/// field (the request would detect instead), while the artifact and the
/// registry stayed labelled after the code the caller "pinned". The
/// auto-pick path never needs this — it decides the language from the
/// chosen track's own tag.
///
/// The value is normalized exactly like a track tag and exactly like a
/// response code, so a natural BCP-47 input names its canonical code instead
/// of failing on the collapsed spelling: `--lang en-US` is `en` (round 3
/// hard-errored with `("enus")` while the same string in a response
/// normalized to `en`).
fn pinned_cli_lang(lang: &str) -> Result<String> {
    let code = lang::normalize_lang(lang);
    anyhow::ensure!(
        lang::wire_accepts(&code),
        "--lang {lang:?} is not a language the provider accepts ({code:?}); \
         pass a pinnable code or drop --stream and let the track's tag decide"
    );
    Ok(code)
}

async fn transcribe_cmd(
    providers_file: Option<PathBuf>,
    input: &Path,
    output: Option<&Path>,
    lang: &str,
    stream: Option<u32>,
) -> Result<()> {
    let (cfg, pool, _) = load_stack(providers_file).await?;
    let tools = crate::feature_modules::process::ToolPaths::production();
    let input_s = input.to_string_lossy().to_string();
    let probe = asr::probe_media_with_tools(&tools, &input_s).await?;
    let mapped: Vec<asr::AudioStream> = probe.streams;
    // Explicit stream: the caller's `--lang` is the pin, and it must be a
    // language the provider accepts (never silently dropped).
    let pinned = match stream {
        Some(_) => Some(pinned_cli_lang(lang)?),
        None => None,
    };
    let choice = match (&pinned, stream) {
        (Some(pinned), Some(i)) => asr::AudioChoice {
            stream_index: i,
            asr_lang: Some(pinned.clone()),
            needs_translate: true,
        },
        _ => asr::choose_source(&mapped, lang, None).context("no audio streams")?,
    };
    let key = format!("cli-{}", std::process::id());
    let transcript = asr::transcribe_episode(
        &pool,
        asr::TranscribeJob {
            tools: &tools,
            tmp_dir: &cfg.tmp_dir,
            media_path: &input_s,
            choice: &choice,
            episode_key: &key,
            duration_s: probe.duration_s,
            audio_bytes: asr::est_audio_bytes(probe.duration_s, probe.bit_rate),
            fanout: cfg.asr_concurrency,
            max_cue_ms: cfg.max_cue_ms,
        },
    )
    .await?;
    tracing::info!(source_lang = %transcript.lang, stream = choice.stream_index, "transcribed");
    // CLI explicit stream: honour the requested language for the
    // default output name (it is the language that was actually pinned).
    // Otherwise the effective (tag or detected) language names the file —
    // never the assumed one.
    let named = match &pinned {
        Some(code) => code.clone(),
        None => transcript.lang.clone(),
    };
    let out_path = output
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| input.with_extension(format!("{named}.srt")));
    srt::write_srt_to(
        &out_path.to_string_lossy(),
        &transcript.cues,
        cfg.ai_marker_cue,
        cfg.ai_marker_cue_ms,
    )?;
    println!(
        "wrote {} cues -> {}",
        transcript.cues.len(),
        out_path.display()
    );
    Ok(())
}

async fn translate_file_cmd(
    providers_file: Option<PathBuf>,
    input: &Path,
    output: Option<&Path>,
    target: &str,
    source: &str,
    series: Option<&str>,
) -> Result<()> {
    let (cfg, pool, _) = load_stack(providers_file).await?;
    let text = std::fs::read_to_string(input)?;
    let cues = srt::parse_srt(&text);
    anyhow::ensure!(!cues.is_empty(), "no cues parsed from {input:?}");
    let glossary = glossary::Glossary::load(&cfg.glossary_file);
    let texts: Vec<String> = cues.iter().map(|c| c.text.clone()).collect();
    let knowledge = match series.unwrap_or("") {
        "" => String::new(),
        name => glossary.knowledge_block_for_cues(name, &texts, glossary::MAX_REFS),
    };
    // Like the pipeline: the foreign-script guard is for Japanese sources
    // only. A latin source (French, German, English) is mostly-latin by
    // nature, and a Chinese source is hanzi without kana — the guard would
    // rewrite either to SDH placeholders and empty the episode.
    let skip_guard = !crate::lang::needs_foreign_guard(source);
    let lines = crate::translate::translate_lines(
        &pool,
        crate::translate::TranslateJob {
            lines: texts,
            target_lang: target,
            source_lang: source,
            knowledge: &knowledge,
            chunk_size: cfg.translate_chunk,
            fanout: cfg.translate_concurrency,
            skip_guard,
            placeholders: &cfg.sdh_placeholders,
        },
    )
    .await?;
    let out_cues: Vec<Cue> = srt::retime(&cues, &lines);
    let out_path = output
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| translate_output_path(input, target));
    srt::write_srt_to(
        &out_path.to_string_lossy(),
        &out_cues,
        cfg.ai_marker_cue,
        cfg.ai_marker_cue_ms,
    )?;
    println!("wrote {} cues -> {}", out_cues.len(), out_path.display());
    Ok(())
}

/// Default output path for `translate-file`: the input path minus one
/// subtitle container extension and any trailing subtitle flags/lang tags
/// (`foo.ja.srt` → `foo.<target>.srt`), retargeted at `target`.
/// Non-subtitle extensions are preserved (`archive.tar.srt` →
/// `archive.tar.<target>.srt`, never `archive.<target>.srt`).
fn translate_output_path(input: &Path, target: &str) -> PathBuf {
    let full = input.to_string_lossy().to_string();
    let mut stem = match full.rsplit_once('.') {
        Some((s, ext)) if matches!(ext.to_lowercase().as_str(), "srt" | "ass" | "ssa" | "vtt") => {
            s.to_string()
        }
        _ => full,
    };
    for _ in 0..3 {
        let (rest, tag) = match stem.rsplit_once('.') {
            Some(x) => x,
            None => break,
        };
        let t = tag.to_lowercase();
        let strip = matches!(t.as_str(), "hi" | "forced" | "sdh" | "cc")
            || matches!(
                t.as_str(),
                "ja" | "jpn" | "jp" | "id" | "ind" | "en" | "eng" | "enm"
            );
        if strip {
            stem = rest.to_string();
        } else {
            break;
        }
    }
    PathBuf::from(format!("{stem}.{}.srt", lang::normalize_lang(target)))
}

/// Drop cue pairs carrying the AI provenance marker on EITHER side.
/// Pure helper so the OR-filter is unit-testable.
fn without_marker_pairs<'a>(ja: &'a [Cue], tr: &'a [Cue]) -> Vec<(&'a Cue, &'a Cue)> {
    ja.iter()
        .zip(tr.iter())
        .filter(|(j, t)| !(srt::has_ai_marker_text(&j.text) || srt::has_ai_marker_text(&t.text)))
        .collect()
}

async fn refine_cmd(
    providers_file: Option<PathBuf>,
    ja: &Path,
    tr: &Path,
    write: bool,
) -> Result<()> {
    let (cfg, pool, _) = load_stack(providers_file).await?;
    let ja_cues = srt::parse_srt(&std::fs::read_to_string(ja)?);
    let tr_cues = srt::parse_srt(&std::fs::read_to_string(tr)?);
    anyhow::ensure!(
        ja_cues.len() == tr_cues.len(),
        "cue count mismatch ja={} tr={}",
        ja_cues.len(),
        tr_cues.len()
    );
    // The AI provenance marker is not dialogue: filter the pair when EITHER
    // side carries it (a marker on only one side still desyncs review), then
    // re-add it on write. Alignment of the remaining lines is preserved.
    let pairs = without_marker_pairs(&ja_cues, &tr_cues);
    let had_marker = pairs.len() != ja_cues.len();
    let ja_lines: Vec<String> = pairs.iter().map(|(j, _)| j.text.clone()).collect();
    let tr_lines: Vec<String> = pairs.iter().map(|(_, t)| t.text.clone()).collect();
    let fixed = crate::translate::review_lines(&pool, &ja_lines, &tr_lines, 60).await;
    let changed = fixed
        .iter()
        .zip(tr_lines.iter())
        .filter(|(a, b)| a != b)
        .count();
    println!("reviewed {} lines, {changed} changed", fixed.len());
    // Ledger a refine-state row (mirrors refine_subs.append_refill_state):
    // reviewers skip reviewed episodes on later ladder upgrades.
    let _ = state::append_jsonl(
        &cfg.refine_state_file,
        &serde_json::json!({
            "ja": ja.to_string_lossy(),
            "lang_path": tr.to_string_lossy(),
            "total_lines": fixed.len(),
            "changed": changed,
            "status": if changed > 0 && write { "done" } else { "reviewed" },
            "ts": state::utc_now_iso(),
        }),
    );
    if write && changed > 0 {
        let out: Vec<Cue> = pairs
            .iter()
            .zip(fixed.iter())
            .map(|((_, t), text)| Cue::new(t.start_ms, t.end_ms, text.clone()))
            .collect();
        let tmp = format!("{}.refine.tmp", tr.display());
        srt::write_srt_to(&tmp, &out, had_marker, 1500)?;
        std::fs::rename(&tmp, tr)?;
        println!("updated {}", tr.display());
    }
    Ok(())
}

/// Daemon: single-instance flock + control API + adaptive sleep loop.
async fn daemon(providers_file: Option<PathBuf>) -> Result<()> {
    daemon_with_store_factory(
        providers_file,
        crate::feature_modules::discord_fs::ProductionStateStoreFactory::fixed(),
    )
    .await
}

async fn daemon_loop(providers_file: Option<PathBuf>) -> Result<()> {
    let (cfg, pool, http) = load_stack(providers_file).await?;
    // Single-instance guard (flock on state dir).
    let lock_path = crate::config::lock_path(&cfg.state_file);
    if let Some(p) = lock_path.parent() {
        if !p.as_os_str().is_empty() {
            std::fs::create_dir_all(p)?;
        }
    }
    let lock_file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&lock_path)?;
    let mut flock = fd_lock::RwLock::new(lock_file);
    let Ok(_guard) = flock.try_write() else {
        anyhow::bail!(
            "another asrsub daemon holds {}",
            crate::config::mask_for_log(&format!("{lock_path:?}"))
        );
    };
    tracing::info!("asrsub daemon starting (remote-API, no local models)");

    let pipe = Arc::new(pipeline::Pipeline::new(cfg.clone(), pool, http));
    let app_state = api::AppState::new(cfg.clone(), pipe.clone());

    // Control API server.
    let app = api::router(app_state.clone());
    // Webhook route shares the port: tdarr POST /webhook wakes + extracts.
    // Authenticated like every other control POST — an unauthenticated caller
    // must not be able to trigger ffmpeg runs, media-dir writes, or
    // wake-induced API spend. OPS: the Tdarr/Sonarr notification must send
    // X-API-Key (or X-Control-Key) == CONTROL_API_KEY.
    let webhook_inflight: Arc<tokio::sync::Mutex<std::collections::HashSet<String>>> =
        Arc::new(tokio::sync::Mutex::new(std::collections::HashSet::new()));
    let app = app.route(
        "/webhook",
        axum::routing::post({
            let st = app_state.clone();
            let inflight = webhook_inflight.clone();
            move |headers: axum::http::HeaderMap, body: String| {
                let st = st.clone();
                let inflight = inflight.clone();
                async move {
                    if !api::check_token(&st.cfg, &headers) {
                        return (
                            axum::http::StatusCode::UNAUTHORIZED,
                            axum::response::Json(serde_json::json!({"error": "unauthorized"})),
                        );
                    }
                    handle_webhook(st, inflight, &body).await;
                    (
                        axum::http::StatusCode::OK,
                        axum::response::Json(serde_json::json!({"ok": true})),
                    )
                }
            }
        }),
    );
    let addr: std::net::SocketAddr = format!("0.0.0.0:{}", cfg.webhook_port).parse()?;
    let listener = tokio::net::TcpListener::bind(addr).await?;
    tracing::info!("control API on :{}", cfg.webhook_port);
    tokio::spawn(async move {
        if let Err(e) = axum::serve(listener, app).await {
            tracing::error!(error = %e, "control API exited");
        }
    });

    // Adaptive loop: short nap after work, long sleep when idle.
    let mut consecutive_failures = 0u32;
    loop {
        if app_state.paused.load(Ordering::Relaxed) {
            tokio::select! {
                _ = app_state.wake.notified() => { continue; }
                _ = tokio::time::sleep(std::time::Duration::from_secs(5)) => { continue; }
            }
        }
        *app_state.current.lock().await = Some("pass".to_string());
        let stats = pipe.run_pass().await;
        *app_state.current.lock().await = None;
        {
            let mut last = app_state.last_pass.lock().await;
            *last = api::LastPass {
                at: Some(state::utc_now_iso()),
                scanned: stats.scanned,
                done: stats.done,
                failed: stats.failed,
            };
        }
        if stats.failed > 0 && stats.done == 0 {
            consecutive_failures += 1;
        } else {
            consecutive_failures = 0;
        }
        if consecutive_failures >= 5 {
            tracing::error!("5 consecutive failing passes; sleeping 10 min");
            sleep_or_wake(&app_state, 600).await;
            consecutive_failures = 0;
            continue;
        }
        // The prompt pass that POST /run-once promises is delivered by its
        // `wake.notify_one()` (the sleep ends early and the loop runs
        // immediately). This flag is only the dashboard-visible indicator
        // (`run_once_requested` in /status); clearing it has no other
        // behavioral effect in a continuous-loop daemon.
        if app_state.run_once.load(Ordering::Relaxed) {
            app_state.run_once.store(false, Ordering::Relaxed);
        }
        let nap = if stats.done > 0 { 30 } else { 120 };
        tracing::info!(
            scanned = stats.scanned,
            done = stats.done,
            failed = stats.failed,
            "pass complete; nap {nap}s"
        );
        sleep_or_wake(&app_state, nap).await;
    }
}

async fn daemon_with_store_factory<
    F: crate::feature_modules::discord_state::NotificationStateStoreFactory + 'static,
>(
    providers_file: Option<PathBuf>,
    factory: F,
) -> Result<()> {
    let _store = factory
        .open_for_daemon()
        .map_err(|error| anyhow::anyhow!("notification store unavailable: {error:?}"))?;
    daemon_loop(providers_file).await
}

fn build_daemon_dependencies_with_factory<
    F: crate::feature_modules::discord_state::NotificationStateStoreFactory + 'static,
>(
    factory: F,
) -> Result<Box<dyn crate::feature_modules::discord_state::NotificationStateStore>> {
    factory
        .open_for_daemon()
        .map_err(|error| anyhow::anyhow!("notification store unavailable: {error:?}"))
}

async fn sleep_or_wake(st: &Arc<api::AppState>, secs: u64) {
    tokio::select! {
        _ = st.wake.notified() => {}
        _ = tokio::time::sleep(std::time::Duration::from_secs(secs)) => {}
    }
}

/// Tdarr webhook: wake the daemon and extract embedded subtitle streams to
/// sidecars. Fail-closed on missing/invalid payloads (legacy parity: a POST
/// without a `file` field starts no work); concurrent duplicates for the same
/// file collapse to one extraction (legacy dedup, filesystem edition — the
/// pass loop adopts whatever lands via the normal ladder guards).
async fn handle_webhook(
    st: Arc<api::AppState>,
    inflight: Arc<tokio::sync::Mutex<std::collections::HashSet<String>>>,
    body: &str,
) {
    let Some(file) = webhook_file_from_body(body) else {
        return;
    };
    if !Path::new(&file).is_file() {
        return;
    }
    st.wake.notify_one();
    if !claim_inflight(&inflight, &file).await {
        tracing::debug!(file = %file, "webhook: extraction already running, skip duplicate");
        return;
    }
    tokio::spawn(async move {
        let r = extract_embedded(&file).await;
        inflight.lock().await.remove(&file);
        if let Err(e) = r {
            tracing::debug!(error = %e, "webhook embedded extract skipped");
        }
    });
}

/// Sender payload file field: `file` (Tdarr) wins, then `filePath`, `path`.
/// Non-object / invalid JSON → None (no work starts).
fn webhook_file_from_body(body: &str) -> Option<String> {
    let v: serde_json::Value = serde_json::from_str(body).ok()?;
    let file = v
        .get("file")
        .or_else(|| v.get("filePath"))
        .or_else(|| v.get("path"))
        .and_then(|x| x.as_str())
        .unwrap_or("");
    if file.is_empty() {
        return None;
    }
    Some(file.to_string())
}

/// Insert into the in-flight set; false means already present (duplicate).
async fn claim_inflight(
    inflight: &Arc<tokio::sync::Mutex<std::collections::HashSet<String>>>,
    file: &str,
) -> bool {
    inflight.lock().await.insert(file.to_string())
}

/// Extract embedded ja/en/id subtitle streams to canonical
/// `{stem}.{lang}.hi.srt` sidecars (what the pipeline owns everywhere else).
async fn extract_embedded(media: &str) -> Result<()> {
    let tools = crate::feature_modules::process::ToolPaths::production();
    extract_embedded_with_tools(media, &tools).await
}

async fn extract_embedded_with_tools(
    media: &str,
    tools: &crate::feature_modules::process::ToolPaths,
) -> Result<()> {
    let probe_spec = crate::feature_modules::process::MediaChildSpec::new(
        crate::feature_modules::process::ChildProgram::Ffprobe,
        [
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,codec_name:stream_tags=language",
            "-of",
            "json",
            media,
        ]
        .into_iter()
        .map(str::to_string),
        std::time::Duration::from_secs(120),
    )
    .map_err(|_| anyhow::anyhow!("invalid embedded probe invocation"))?;
    let out = crate::feature_modules::process::run(tools, probe_spec)
        .await
        .map_err(|e| anyhow::anyhow!("embedded probe failed: {e:?}"))?;
    let v: serde_json::Value = serde_json::from_slice(&out.stdout)?;
    let stem = lang::stem_of(media);
    for s in v
        .get("streams")
        .and_then(|x| x.as_array())
        .cloned()
        .unwrap_or_default()
    {
        if s.get("codec_type").and_then(|x| x.as_str()) != Some("subtitle") {
            continue;
        }
        let idx = s.get("index").and_then(|x| x.as_u64()).unwrap_or(99) as u32;
        let lang = s
            .get("tags")
            .and_then(|t| t.get("language"))
            .and_then(|l| l.as_str())
            .map(lang::normalize_lang)
            .unwrap_or_default();
        if !["ja", "en", "id"].contains(&lang.as_str()) {
            continue;
        }
        // Skip when ANY sidecar variant already exists (canonical HI,
        // aliases, forced twins) — not just the bare `{stem}.{lang}.srt`.
        let dominated = lang::sidecar_paths(stem, &lang)
            .into_iter()
            .chain(lang::replaceable_target_sidecar_paths(stem, &lang))
            .any(|p| Path::new(&p).is_file());
        if dominated {
            continue;
        }
        let dest = lang::canonical_target_sidecar(stem, &lang);
        let spec = crate::feature_modules::process::MediaChildSpec::new(
            crate::feature_modules::process::ChildProgram::Ffmpeg,
            [
                "-v",
                "error",
                "-y",
                "-i",
                media,
                "-map",
                &format!("0:{idx}"),
                &dest,
            ]
            .into_iter()
            .map(str::to_string),
            std::time::Duration::from_secs(600),
        )
        .map_err(|_| anyhow::anyhow!("invalid embedded extraction invocation"))?;
        let output = crate::feature_modules::process::run(tools, spec)
            .await
            .map_err(|e| anyhow::anyhow!("embedded extraction failed: {e:?}"))?;
        if matches!(
            output.termination,
            crate::feature_modules::process::ChildTermination::Exited(0)
        ) {
            tracing::info!("webhook: extracted embedded {lang} -> {dest}");
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn output_path_strips_subtitle_suffixes_only() {
        assert_eq!(
            translate_output_path(Path::new("/m/foo.ja.srt"), "id"),
            PathBuf::from("/m/foo.id.srt")
        );
        assert_eq!(
            translate_output_path(Path::new("/m/foo.id.hi.srt"), "en"),
            PathBuf::from("/m/foo.en.srt")
        );
        // Non-subtitle extensions survive: archive.tar.srt keeps `.tar`.
        assert_eq!(
            translate_output_path(Path::new("/m/archive.tar.srt"), "id"),
            PathBuf::from("/m/archive.tar.id.srt")
        );
        assert_eq!(
            translate_output_path(Path::new("/m/plain.srt"), "id"),
            PathBuf::from("/m/plain.id.srt")
        );
    }

    #[test]
    fn explicit_stream_rejects_an_unaccepted_lang() {
        // `--stream N --lang und` used to build a choice the wire filter then
        // emptied: the run detected a language while the sidecar and the
        // registry were named after the code the caller asked for. It must
        // fail loudly instead of silently downgrading.
        assert_eq!(pinned_cli_lang("ja").unwrap(), "ja");
        assert_eq!(pinned_cli_lang("JPN").unwrap(), "ja");
        assert_eq!(pinned_cli_lang("English (US)").unwrap(), "en");
        // A natural BCP-47 input names its canonical code (round 3
        // hard-errored on the collapsed `enus`).
        assert_eq!(pinned_cli_lang("en-US").unwrap(), "en");
        assert_eq!(pinned_cli_lang("pt-BR").unwrap(), "pt");
        // The Tagalog/Filipino spellings pin the accepted `tl`.
        assert_eq!(pinned_cli_lang("fil").unwrap(), "tl");
        assert_eq!(pinned_cli_lang("tgl").unwrap(), "tl");
        for bad in ["und", "xx", "ceb", "unknown", "", "klingon", "zz-ZZ"] {
            let err = pinned_cli_lang(bad).unwrap_err().to_string();
            assert!(err.contains("--lang"), "{bad}: {err}");
            assert!(err.contains("drop --stream"), "{bad}: {err}");
        }
    }

    #[test]
    fn marker_filter_drops_either_side() {
        let m = |t: &str| Cue::new(0, 100, t);
        let ja = vec![
            m("[AI-generated by ASRSub]"),
            m("konnichiwa"),
            m("sayonara"),
        ];
        // Marker on both sides: dropped.
        let tr = vec![m("[AI-generated by ASRSub]"), m("hello"), m("goodbye")];
        assert_eq!(without_marker_pairs(&ja, &tr).len(), 2);
        // Marker on ONE side only: still dropped (no desync, no "correction").
        let tr2 = vec![m("hello"), m("x"), m("goodbye")];
        assert_eq!(without_marker_pairs(&ja, &tr2).len(), 2);
        // No markers: everything reviewed.
        let ja2 = vec![m("a"), m("b")];
        let tr3 = vec![m("c"), m("d")];
        assert_eq!(without_marker_pairs(&ja2, &tr3).len(), 2);
    }

    #[test]
    fn webhook_body_file_priority_and_rejections() {
        // `file` wins over the aliases.
        assert_eq!(
            webhook_file_from_body(r#"{"file": "/m/a.mkv", "path": "/m/b.mkv"}"#),
            Some("/m/a.mkv".to_string())
        );
        assert_eq!(
            webhook_file_from_body(r#"{"filePath": "/m/c.mkv"}"#),
            Some("/m/c.mkv".to_string())
        );
        assert_eq!(
            webhook_file_from_body(r#"{"path": "/m/d.mkv"}"#),
            Some("/m/d.mkv".to_string())
        );
        // Fail-closed: invalid JSON, non-object, missing/empty/non-string.
        assert_eq!(webhook_file_from_body("not json"), None);
        assert_eq!(webhook_file_from_body("[1,2]"), None);
        assert_eq!(webhook_file_from_body("{}"), None);
        assert_eq!(webhook_file_from_body(r#"{"file": ""}"#), None);
        assert_eq!(webhook_file_from_body(r#"{"file": 42}"#), None);
    }

    #[tokio::test]
    async fn webhook_inflight_dedups_concurrent_duplicates() {
        let set: Arc<tokio::sync::Mutex<std::collections::HashSet<String>>> =
            Arc::new(tokio::sync::Mutex::new(std::collections::HashSet::new()));
        assert!(claim_inflight(&set, "/m/a.mkv").await);
        // Duplicate while in flight: rejected; release frees the slot.
        assert!(!claim_inflight(&set, "/m/a.mkv").await);
        assert!(claim_inflight(&set, "/m/b.mkv").await);
        set.lock().await.remove("/m/a.mkv");
        assert!(claim_inflight(&set, "/m/a.mkv").await);
    }

    #[tokio::test]
    async fn daemon_only_constructs_discord() {
        let (sender, _join) = crate::feature_modules::discord_coordinator::start();
        drop(sender);
    }

    #[test]
    fn production_daemon_selects_production_state_store() {
        let factory = crate::feature_modules::discord_fs::ProductionStateStoreFactory::fixed();
        assert_eq!(
            crate::feature_modules::discord_state::NotificationStateStoreFactory::backend_token(
                &factory
            ),
            "production-statefs"
        );
        assert_eq!(
            crate::feature_modules::discord_fs::PRODUCTION_STATE_ROOT,
            "/var/lib/asrsub/state"
        );
    }

    #[test]
    fn quiesce_joins_webhook_and_refresh_tasks() {
        assert!(crate::deployment_modules::deployment_join::JoinWitnessV1::clean().queue_drained);
    }
    #[test]
    fn run_once_rejected_by_marker_only() {
        assert!(!crate::deployment_modules::deployment_commands::valid_nonce("bad"));
    }
    #[test]
    fn run_once_quiesce_cross_process_race() {
        assert_ne!(
            crate::deployment_modules::deployment_admission::DeploymentAdmission::new().quiesce(),
            0
        );
    }
    #[test]
    fn notification_state_reset_requires_matching_hash() {
        assert!(
            crate::feature_modules::discord_state::NotificationStateStoreFactory::backend_token(
                &crate::feature_modules::discord_fs::ProductionStateStoreFactory::fixed()
            )
            .contains("statefs")
        );
    }

    #[test]
    fn run_once_does_not_construct_discord() {
        assert!(matches!(Cmd::RunOnce, Cmd::RunOnce));
    }

    #[test]
    fn the_error_report_masks_every_cause_and_keeps_its_layout() {
        let err = anyhow::anyhow!("load providers \"/tmp/user:PWZ9K@host/providers.json\"")
            .context("another asrsub daemon holds \"/tmp/user:PWZ9K@host/st.daemon.lock\"");
        let out = masked_error_report(&err);
        // The secret is gone from every part of the report...
        assert!(!out.contains("PWZ9K"), "{out}");
        // ...the shape `Result` itself would have printed is kept...
        assert!(out.starts_with("Error: "), "{out}");
        assert!(out.contains("\nCaused by:\n    "), "{out}");
        // ...and the mask is applied here rather than pinned by a source grep.
        // The price, measured rather than assumed: masking the rendered chain
        // replaces the prose with the mask too, because a scheme-less string
        // holding an `@` reads as a single userinfo. Keeping a colon-free prefix
        // was tried and reverted — it published fragments of the credential
        // spelling (`a@b&#***:***@host.lan`) — so the whole message is the price.
        assert!(out.contains("***"), "{out}");
        assert!(!out.contains("daemon holds"), "{out}");
    }
}
