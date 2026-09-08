//! Integration tests at the binary boundary (`CARGO_BIN_EXE_asrsub`).
//! No network access: `--help`, config-only commands, and the providers
//! file shape. Unit coverage for the pipeline internals lives in `src/*.rs`.

use std::process::Command;

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
    let out = Command::new(bin())
        .arg("run-once")
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
    let text =
        std::fs::read_to_string("asrsub_providers.json.example").expect("providers example");
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
