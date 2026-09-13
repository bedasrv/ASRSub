//! Integration tests at the binary boundary (`CARGO_BIN_EXE_asrsub`).
//! No remote access: `--help`, config-only commands, the providers file
//! shape, and two end-to-end `transcribe` runs against a localhost Whisper
//! stub (with fake `ffmpeg`/`ffprobe` on the child's `PATH`). Unit coverage
//! for the pipeline internals lives in `src/*.rs`.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};

fn bin() -> std::path::PathBuf {
    std::path::PathBuf::from(env!("CARGO_BIN_EXE_asrsub"))
}

#[test]
fn cli_help_lists_subcommands() {
    let out = Command::new(bin())
        .arg("--help")
        .output()
        .expect("run asrsub --help");
    assert!(out.status.success());
    let text = String::from_utf8_lossy(&out.stdout);
    for cmd in [
        "daemon",
        "run-once",
        "transcribe",
        "translate-file",
        "refine",
        "health",
    ] {
        assert!(text.contains(cmd), "missing subcommand {cmd} in:\n{text}");
    }
}

#[test]
fn cli_health_is_config_only() {
    // Must not require the providers file or any services.
    let out = Command::new(bin())
        .arg("health")
        .env(
            "PROVIDERS_FILE",
            "/nonexistent-dir-xyz/asrsub_providers.json",
        )
        .output()
        .expect("run asrsub health");
    assert!(out.status.success());
    let v: serde_json::Value = serde_json::from_slice(&out.stdout).expect("health prints JSON");
    assert_eq!(v.get("config_ok"), Some(&serde_json::Value::Bool(true)));
}

#[test]
fn cli_run_once_degrades_without_services() {
    // No Sonarr/Bazarr configured: empty pass, exit 0, valid stats JSON.
    // Point at the committed keyless example so the test never depends on
    // the untracked live providers file (absent on fresh CI clones).
    let out = Command::new(bin())
        .arg("run-once")
        .env("PROVIDERS_FILE", "asrsub_providers.json.example")
        .env("SONARR_URL", "")
        .env("BAZARR_URL", "")
        .output()
        .expect("run asrsub run-once");
    assert!(out.status.success());
    let v: serde_json::Value = serde_json::from_slice(&out.stdout).expect("run-once prints JSON");
    assert_eq!(v.get("scanned"), Some(&serde_json::json!(0)));
}

#[test]
fn providers_file_shape() {
    // Shape contract pins the committed EXAMPLE (the live file is
    // untracked since 2026-09-08 and may not exist on fresh clones).
    let text = std::fs::read_to_string("asrsub_providers.json.example").expect("providers example");
    let v: serde_json::Value = serde_json::from_str(&text).expect("valid json");
    assert!(v
        .get("llm_translation_models")
        .and_then(|x| x.as_array())
        .map(|a| !a.is_empty())
        .unwrap_or(false));
    assert!(v
        .get("whisper_stt")
        .and_then(|x| x.get("endpoint"))
        .is_some());
}

// ---------------------------------------------------------------------------
// End-to-end ASR plumbing: the real binary, a localhost Whisper stub, and fake
// media tools on the child's PATH.
// ---------------------------------------------------------------------------

/// A local Whisper stub: answers one `verbose_json` body per request, in
/// arrival order (`script[i]` for the i-th request, the last entry repeating),
/// and records the `language` form field each request actually carried.
struct WhisperStub {
    endpoint: String,
    seen: Arc<Mutex<Vec<StubRequest>>>,
}

#[derive(Debug, Clone)]
struct StubRequest {
    /// The `language` form field: `None` means the request was unforced.
    language: Option<String>,
    /// The uploaded part's name (`part.mp3` for a piece, `full.mp3` for the
    /// whole-file path).
    filename: Option<String>,
}

impl WhisperStub {
    fn start(script: Vec<Option<&str>>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind whisper stub");
        let addr = listener.local_addr().expect("stub addr");
        let seen: Arc<Mutex<Vec<StubRequest>>> = Arc::new(Mutex::new(Vec::new()));
        let taken = seen.clone();
        let script: Vec<Option<String>> =
            script.into_iter().map(|s| s.map(str::to_string)).collect();
        std::thread::spawn(move || {
            for (n, stream) in listener.incoming().enumerate() {
                let Ok(mut sock) = stream else { break };
                let Some(body) = read_request_body(&mut sock) else {
                    continue;
                };
                taken.lock().expect("stub lock").push(StubRequest {
                    language: multipart_field(&body, "language"),
                    filename: multipart_filename(&body),
                });
                let reported = script.get(n).or_else(|| script.last()).cloned().flatten();
                let mut payload = serde_json::json!({
                    "segments": [{"start": 0.0, "end": 2.5, "text": "stub"}],
                });
                if let Some(code) = reported {
                    payload["language"] = serde_json::json!(code);
                }
                let json = serde_json::to_string(&payload).expect("stub json");
                let _ = sock.write_all(
                    format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                        json.len(),
                        json
                    )
                    .as_bytes(),
                );
            }
        });
        Self {
            endpoint: format!("http://{addr}/audio/transcriptions"),
            seen,
        }
    }

    /// The `language` field of every request, in arrival order.
    fn languages(&self) -> Vec<Option<String>> {
        self.seen
            .lock()
            .expect("stub lock")
            .iter()
            .map(|r| r.language.clone())
            .collect()
    }

    fn requests(&self) -> Vec<StubRequest> {
        self.seen.lock().expect("stub lock").clone()
    }
}

fn read_request_body(sock: &mut TcpStream) -> Option<Vec<u8>> {
    let mut buf = Vec::new();
    let mut chunk = [0u8; 8192];
    let head_end = loop {
        if let Some(p) = find_bytes(&buf, b"\r\n\r\n") {
            break p;
        }
        let n = sock.read(&mut chunk).ok()?;
        if n == 0 {
            return None;
        }
        buf.extend_from_slice(&chunk[..n]);
    };
    let head = String::from_utf8_lossy(&buf[..head_end]).to_ascii_lowercase();
    let len: usize = head
        .lines()
        .find_map(|l| l.strip_prefix("content-length:"))
        .and_then(|v| v.trim().parse().ok())
        .unwrap_or(0);
    while buf.len() < head_end + 4 + len {
        let n = sock.read(&mut chunk).ok()?;
        if n == 0 {
            break;
        }
        buf.extend_from_slice(&chunk[..n]);
    }
    Some(buf[head_end + 4..].to_vec())
}

fn find_bytes(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}

/// Value of a multipart text field (`None` when the request omitted it).
fn multipart_field(body: &[u8], name: &str) -> Option<String> {
    let text = String::from_utf8_lossy(body);
    let marker = format!("name=\"{name}\"");
    let rest = text.split_once(&marker)?.1;
    let value = rest.split_once("\r\n\r\n")?.1;
    Some(value.split_once("\r\n")?.0.to_string())
}

fn multipart_filename(body: &[u8]) -> Option<String> {
    let text = String::from_utf8_lossy(body);
    let rest = text.split_once("filename=\"")?.1;
    Some(rest.split_once('"')?.0.to_string())
}

fn write_exe(path: PathBuf, body: &str) {
    std::fs::write(&path, body).expect("write fake tool");
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).expect("chmod");
}

/// Fake `ffprobe`/`ffmpeg` in a private bin dir, prepended to the CHILD's
/// `PATH` only (no process-global mutation, so the two tests run in parallel).
/// `streams` is the raw `streams` array; `duration` x `bit_rate` is the probe
/// estimate that routes the run (`> 24 MB` → pieces, else one whole file).
fn write_fake_tools(bin_dir: &Path, streams: &str, duration: &str, bit_rate: &str) {
    write_exe(
        bin_dir.join("ffprobe"),
        &format!(
            "#!/bin/sh\nprintf '%s' '{{\"streams\":{streams},\"format\":{{\"duration\":\"{duration}\",\"bit_rate\":\"{bit_rate}\"}}}}'\n"
        ),
    );
    write_exe(
        bin_dir.join("ffmpeg"),
        "#!/bin/sh\nout=\"\"\nfor a in \"$@\"; do out=\"$a\"; done\nprintf 'FAKEAUDIO' > \"$out\"\n",
    );
}

/// One `asrsub transcribe` run with a hermetic environment: only the fake-tool
/// PATH, the providers file and the config dir survive, so ambient
/// `PROVIDERS_FILE`/`TARGET_LANGS`/`ASRSUB_CONFIG_DIR` (or a developer's real
/// config) cannot change the outcome.
fn transcribe_run(
    root: &Path,
    name: &str,
    streams: &str,
    duration: &str,
    bit_rate: &str,
    script: Vec<Option<&str>>,
    args: &[&str],
) -> (std::process::Output, WhisperStub, PathBuf) {
    let bin_dir = root.join(format!("bin-{name}"));
    let cfg_dir = root.join(format!("cfg-{name}"));
    let media_dir = root.join(format!("media-{name}"));
    for d in [&bin_dir, &cfg_dir, &media_dir] {
        std::fs::create_dir_all(d).expect("mkdir");
    }
    std::fs::write(cfg_dir.join("glossary.json"), "{}").expect("glossary");
    write_fake_tools(&bin_dir, streams, duration, bit_rate);
    let media = media_dir.join("ep.mkv");
    std::fs::write(&media, b"not real media").expect("media");
    let stub = WhisperStub::start(script);
    let providers = cfg_dir.join("providers.json");
    std::fs::write(
        &providers,
        format!(
            "{{\"llm_translation_models\":[{{\"endpoint\":\"http://127.0.0.1:1/chat/completions\",\"model\":\"stub\",\"key_env\":\"\",\"api_key\":\"x\"}}],\"whisper_stt\":{{\"endpoint\":\"{}\",\"model\":\"stub\",\"key_env\":\"\",\"api_key\":\"x\"}}}}",
            stub.endpoint
        ),
    )
    .expect("providers");
    let out = media_dir.join("out.srt");
    let output = Command::new(bin())
        .arg("transcribe")
        .arg("-i")
        .arg(&media)
        .arg("-o")
        .arg(&out)
        .args(args)
        .env_clear()
        .env(
            "PATH",
            format!(
                "{}:{}",
                bin_dir.display(),
                std::env::var("PATH").unwrap_or_default()
            ),
        )
        .env("PROVIDERS_FILE", &providers)
        .env("ASRSUB_CONFIG_DIR", &cfg_dir)
        .output()
        .expect("run asrsub transcribe");
    (output, stub, out)
}

/// One tagged audio track named `lang` (or untagged when `lang` is empty).
fn one_track(lang: &str) -> String {
    if lang.is_empty() {
        "[{\"index\":1,\"codec_name\":\"aac\",\"codec_type\":\"audio\"}]".to_string()
    } else {
        format!(
            "[{{\"index\":1,\"codec_name\":\"aac\",\"codec_type\":\"audio\",\"tags\":{{\"language\":\"{lang}\"}}}}]"
        )
    }
}

/// Item 5(a): the multi-chunk path end to end. Eighteen hundred seconds at
/// 200 kbit/s estimates to ~45 MB, over the 24 MB threshold, so the run splits
/// into three pieces: what each request actually carried on the wire, and
/// whether the contradiction guard fired, are both asserted per request.
#[test]
fn piece_path_carries_the_gated_language_and_guards_only_a_sent_pin() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();

    // (1) The `eng` tag establishes the pin `en`; every one of the three
    // requests carries it and the provider echoes it, so the run completes.
    let (out, stub, srt) = transcribe_run(
        root,
        "echo",
        &one_track("eng"),
        "1800.0",
        "200000",
        vec![Some("en")],
        &["--lang", "id"],
    );
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(
        stub.languages(),
        vec![Some("en".to_string()); 3],
        "every piece must carry the pin the tag established"
    );
    assert!(stub
        .requests()
        .iter()
        .all(|r| r.filename.as_deref() == Some("part.mp3")));
    assert!(srt.is_file(), "no artifact written: {}", srt.display());
    assert_eq!(
        std::fs::read_to_string(&srt)
            .unwrap()
            .matches("stub")
            .count(),
        3,
        "one cue per piece"
    );

    // (2) A provider that contradicts a pin we DID send fails the language:
    // non-zero exit, no artifact, both codes named.
    let (out, stub, srt) = transcribe_run(
        root,
        "contradict",
        &one_track("eng"),
        "1800.0",
        "200000",
        vec![Some("ja")],
        &["--lang", "id"],
    );
    assert!(
        !out.status.success(),
        "a contradicted pin must fail the run"
    );
    let err = String::from_utf8_lossy(&out.stderr).to_string();
    assert!(err.contains("pinned en, reported ja"), "stderr: {err}");
    assert!(
        !srt.exists(),
        "no artifact may be written after a contradition"
    );
    let sent = stub.languages();
    assert!(!sent.is_empty() && sent.len() <= 3, "{sent:?}");
    assert!(
        sent.iter().all(|l| l.as_deref() == Some("en")),
        "no request may drop the pin: {sent:?}"
    );

    // (3) An untagged track detects instead. The probe reports `ceb` — a code
    // the endpoint rejects — so the two remaining pieces are unforced as well,
    // and their reports (`en`) cannot contradict a pin that was never sent.
    let (out, stub, srt) = transcribe_run(
        root,
        "detect",
        &one_track(""),
        "1800.0",
        "200000",
        vec![Some("ceb"), Some("en")],
        &["--lang", "id"],
    );
    assert!(
        out.status.success(),
        "an unforced report must not abort: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(
        stub.languages(),
        vec![None, None, None],
        "a code the endpoint rejects must never be sent"
    );
    assert!(srt.is_file());
}

/// Item 2: the whole-file path (one request) guards exactly like the piece
/// path — a pin that was sent cannot be contradicted, and a run that sent
/// nothing is never aborted by what the provider reports.
#[test]
fn single_file_path_guards_the_pin_it_sent() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();

    // (1) 40 s at 200 kbit/s estimates to ~1 MB: extract, then ONE request
    // carrying the `en` tag's pin. The provider answers `ja` → error, no
    // artifact (before this the run exited 0 and wrote an `en`-labelled SRT
    // containing `ja` text).
    let (out, stub, srt) = transcribe_run(
        root,
        "sf-contradict",
        &one_track("eng"),
        "40.0",
        "200000",
        vec![Some("ja")],
        &["--lang", "id"],
    );
    assert!(
        !out.status.success(),
        "a contradicted pin must fail the run"
    );
    let err = String::from_utf8_lossy(&out.stderr).to_string();
    assert!(err.contains("pinned en, reported ja"), "stderr: {err}");
    assert!(
        !srt.exists(),
        "no artifact may be written after a contradiction"
    );
    assert_eq!(stub.languages(), vec![Some("en".to_string())]);
    assert_eq!(stub.requests()[0].filename.as_deref(), Some("full.mp3"));

    // (2) The untagged track sends NO pin, so the guard must stay silent
    // whatever the provider reports (`ja` here is not a contradiction).
    let (out, stub, srt) = transcribe_run(
        root,
        "sf-detect",
        &one_track(""),
        "40.0",
        "200000",
        vec![Some("ja")],
        &["--lang", "id"],
    );
    assert!(
        out.status.success(),
        "an unforced report must not abort: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(stub.languages(), vec![None]);
    assert!(srt.is_file());

    // (3) The tag names a language the endpoint rejects (`ceb`): the CARRIED
    // code is real, the SENT value is nothing, and only what was sent may be
    // guarded — so the report stands and the run completes.
    let (out, stub, srt) = transcribe_run(
        root,
        "sf-carried",
        &one_track("ceb"),
        "40.0",
        "200000",
        vec![Some("en")],
        &["--lang", "id"],
    );
    assert!(
        out.status.success(),
        "a carried-but-unsent code must not be guarded: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(
        stub.languages(),
        vec![None],
        "a rejected code is never sent"
    );
    assert!(srt.is_file());
}
