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

mod actions;
mod api;
mod asr;
mod bazarr;
mod config;
mod episode;
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

use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;
use std::sync::Arc;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};

use crate::srt::Cue;

#[derive(Parser, Debug)]
#[command(name = "asrsub", version, about = "Remote-API subtitle pipeline")]
struct Cli {
    /// Config dir override (default ~/.config/asr-pipeline)
    #[arg(long, env = "ASRSUB_CONFIG_DIR")]
    config_dir: Option<PathBuf>,
    /// Providers file (default ./asrsub_providers.json or PROVIDERS_FILE)
    #[arg(long, env = "PROVIDERS_FILE")]
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
        #[arg(long, default_value = "ja")]
        lang: String,
        /// Audio stream index (default: auto-pick Japanese).
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
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    init_tracing();
    // Optional config-dir override before Config::load reads the env.
    if let Some(d) = cli.config_dir.clone() {
        std::env::set_var("ASRSUB_CONFIG_DIR", &d);
    }
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .thread_name("asrsub")
        .build()?;
    rt.block_on(async_main(cli))
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
    let http = build_http();
    let ppath = providers_override
        .or_else(|| std::env::var("PROVIDERS_FILE").ok().map(PathBuf::from))
        .unwrap_or_else(|| cfg.providers_file.clone());
    let file = providers::ProvidersFile::load(&ppath)
        .with_context(|| format!("load providers {ppath:?}"))?;
    if file.llm_translation_models.is_empty() {
        anyhow::bail!("no llm_translation_models in {ppath:?}");
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
    }
}

async fn transcribe_cmd(
    providers_file: Option<PathBuf>,
    input: &Path,
    output: Option<&Path>,
    lang: &str,
    stream: Option<u32>,
) -> Result<()> {
    let (cfg, pool, _) = load_stack(providers_file).await?;
    let streams = asr::probe_audio(&input.to_string_lossy()).await?;
    let mapped: Vec<asr::AudioStream> = streams;
    let choice = match stream {
        Some(i) => asr::AudioChoice {
            stream_index: i,
            asr_lang: lang.to_string(),
            needs_translate: true,
        },
        None => asr::choose_source(&mapped, lang).context("no audio streams")?,
    };
    let key = format!("cli-{}", std::process::id());
    let cues = asr::transcribe_episode(
        &pool,
        &cfg.tmp_dir,
        &input.to_string_lossy(),
        &choice,
        &key,
        cfg.asr_concurrency,
        cfg.max_cue_ms,
    )
    .await?;
    let out_path = output
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| input.with_extension(format!("{lang}.srt")));
    srt::write_srt_to(
        &out_path.to_string_lossy(),
        &cues,
        cfg.ai_marker_cue,
        cfg.ai_marker_cue_ms,
    )?;
    println!("wrote {} cues -> {}", cues.len(), out_path.display());
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
    // Like the pipeline: no foreign-script guard when the SOURCE is already
    // English/latin (skip_guard), or `en→id` via CLI becomes all placeholders.
    let skip_guard = !matches!(
        source.trim().to_lowercase().as_str(),
        "japanese" | "ja" | "jpn" | "jp"
    );
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
    let out_cues: Vec<Cue> = cues
        .iter()
        .zip(lines.iter())
        .map(|(c, t)| Cue::new(c.start_ms, c.end_ms, t.clone()))
        .collect();
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
    let (cfg, pool, http) = load_stack(providers_file).await?;
    // Single-instance guard (flock on state dir).
    let lock_path = cfg.state_file.with_extension("daemon.lock");
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
        anyhow::bail!("another asrsub daemon holds {lock_path:?}");
    };
    tracing::info!("asrsub daemon starting (remote-API, no local models)");

    let pipe = Arc::new(pipeline::Pipeline::new(cfg.clone(), pool, http));
    let app_state = api::AppState::new(cfg.clone(), pipe.clone());

    // Control API server.
    let app = api::router(app_state.clone());
    // Webhook route shares the port: tdarr POST /webhook wakes + extracts.
    let app = app.route(
        "/webhook",
        axum::routing::post({
            let st = app_state.clone();
            move |body: String| {
                let st = st.clone();
                async move {
                    handle_webhook(st, &body).await;
                    axum::http::StatusCode::OK
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

async fn sleep_or_wake(st: &Arc<api::AppState>, secs: u64) {
    tokio::select! {
        _ = st.wake.notified() => {}
        _ = tokio::time::sleep(std::time::Duration::from_secs(secs)) => {}
    }
}

/// Minimal tdarr webhook: wakes the daemon; when the payload carries a media
/// file path, embedded subtitle streams are extracted to sidecars first.
async fn handle_webhook(st: Arc<api::AppState>, body: &str) {
    st.wake.notify_one();
    let v: serde_json::Value = match serde_json::from_str(body) {
        Ok(v) => v,
        Err(_) => return,
    };
    let file = v
        .get("file")
        .or_else(|| v.get("filePath"))
        .or_else(|| v.get("path"))
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .to_string();
    if file.is_empty() || !Path::new(&file).is_file() {
        return;
    }
    tokio::spawn(async move {
        if let Err(e) = extract_embedded(&file).await {
            tracing::debug!(error = %e, "webhook embedded extract skipped");
        }
    });
}

/// Extract embedded ja/en/id subtitle streams to canonical
/// `{stem}.{lang}.hi.srt` sidecars (what the pipeline owns everywhere else).
async fn extract_embedded(media: &str) -> Result<()> {
    let out = tokio::process::Command::new("ffprobe")
        .args([
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,codec_name:stream_tags=language",
            "-of",
            "json",
            media,
        ])
        .output()
        .await?;
    let v: serde_json::Value = serde_json::from_slice(&out.stdout)?;
    let stem = media.rsplit_once('.').map(|(s, _)| s).unwrap_or(media);
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
        let st = tokio::process::Command::new("ffmpeg")
            .args([
                "-v",
                "error",
                "-y",
                "-i",
                media,
                "-map",
                &format!("0:{idx}"),
                &dest,
            ])
            .status()
            .await?;
        if st.success() {
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
}
