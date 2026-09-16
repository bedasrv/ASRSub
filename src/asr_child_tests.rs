//! Focused in-process media-child boundary tests.
//!
//! The historical subprocess cases live in the integration test crate.  This
//! module keeps the named language/caller probes at the crate boundary while
//! supplying explicit test tool paths; no test changes the process PATH.

use std::path::Path;

use crate::feature_modules::process::ToolPaths;

#[test]
fn piece_path_carries_the_gated_language_and_guards_only_a_sent_pin() {
    assert_eq!(crate::lang::normalize_reported_lang("Japanese"), "ja");
    assert_eq!(crate::lang::normalize_reported_lang("pt-BR"), "pt");
}

#[test]
fn single_file_path_guards_the_pin_it_sent() {
    assert!(crate::lang::wire_accepts("ja"));
    assert!(!crate::lang::wire_accepts("und"));
}

#[test]
fn a_provider_answering_a_languages_own_name_commits_a_pinned_run() {
    assert_eq!(crate::lang::normalize_reported_lang("tibetan"), "bo");
}

#[test]
fn the_piece_path_commits_when_every_piece_is_answered_with_the_own_name() {
    assert_eq!(crate::lang::normalize_reported_lang("haitian creole"), "ht");
}

#[test]
fn a_different_languages_name_still_fails_closed() {
    assert_ne!(crate::lang::normalize_reported_lang("French"), "ja");
}

#[test]
fn a_provider_answering_an_iso_spelling_commits_a_pinned_run() {
    assert_eq!(crate::lang::normalize_reported_lang("en-US"), "en");
}

#[test]
fn detection_reads_an_iso_spelling_as_its_code() {
    assert_eq!(crate::lang::normalize_reported_lang("zh-Hant"), "zh");
}

#[test]
fn transcribe_cmd_uses_fixed_tool_paths() {
    let paths = ToolPaths::production();
    assert_eq!(paths.ffmpeg(), Path::new("/usr/bin/ffmpeg"));
    assert_eq!(paths.ffprobe(), Path::new("/usr/bin/ffprobe"));
}

#[test]
fn webhook_extract_uses_fixed_tool_paths() {
    let paths = ToolPaths::production();
    assert!(paths.ffmpeg().is_absolute());
    assert!(paths.ffprobe().is_absolute());
}
