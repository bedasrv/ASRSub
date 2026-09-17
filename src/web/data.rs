use std::collections::HashMap;
use std::sync::Arc;

use crate::api::AppState;
use crate::config::{Config, FieldKind, FIELDS};
use crate::state;
use serde::Deserialize;

#[derive(Debug, Clone, Default, Deserialize)]
pub(crate) struct LibQuery {
    #[serde(default)]
    pub(crate) q: String,
    #[serde(default)]
    pub(crate) scope: String,
    #[serde(default)]
    pub(crate) sort: String,
    #[serde(default)]
    pub(crate) dir: String,
}

impl LibQuery {
    /// Preserve the current library view when an episode action redirects back.
    pub(crate) fn to_query(&self) -> String {
        [
            ("q", &self.q),
            ("scope", &self.scope),
            ("sort", &self.sort),
            ("dir", &self.dir),
        ]
        .into_iter()
        .filter(|(_, value)| !value.is_empty())
        .map(|(key, value)| format!("{key}={}", urlencode(value)))
        .collect::<Vec<_>>()
        .join("&")
    }
}

#[derive(Debug, Clone)]
pub(crate) struct LibRow {
    pub(crate) aid: String,
    pub(crate) title: String,
    pub(crate) kind: &'static str,
    pub(crate) missing: String,
    pub(crate) done: String,
    pub(crate) excluded: bool,
}

/// Percent-encode a query value without adding a dependency for four filters.
pub(crate) fn urlencode(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for byte in value.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(byte as char)
            }
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

pub(crate) fn ep_action_url(aid: &str, action: &str, query: &str) -> String {
    if query.is_empty() {
        format!("/ui/episode/{aid}/{action}")
    } else {
        format!("/ui/episode/{aid}/{action}?{query}")
    }
}

pub(crate) async fn library_rows(s: &Arc<AppState>, q: &LibQuery) -> Vec<LibRow> {
    let wanted = crate::api::wanted_payload(s).await;
    let items = wanted["data"].as_array().cloned().unwrap_or_default();
    let entries: Vec<state::StateEntry> = state::load_jsonl(&s.cfg.state_file);
    let mut done: HashMap<(String, i64), Vec<String>> = HashMap::new();
    for entry in entries {
        if entry.status.as_deref() != Some("done") {
            continue;
        }
        if let (Some(id), Some(language)) = (entry.episode_id, entry.language.as_deref()) {
            done.entry((entry.kind.unwrap_or_else(|| "series".to_string()), id))
                .or_default()
                .push(crate::lang::normalize_lang(language));
        }
    }
    let excluded = state::parse_exclusions(&s.cfg.exclusions_file);
    let needle = q.q.trim().to_lowercase();
    let mut rows = Vec::new();

    for item in &items {
        let id = item["sonarrEpisodeId"].as_i64().unwrap_or(-1);
        let movie = item["movie"].as_bool().unwrap_or(false);
        let title = item["seriesTitle"].as_str().unwrap_or("").to_string();
        let missing: Vec<String> = item["missing_subtitles"]
            .as_array()
            .map(|values| {
                values
                    .iter()
                    .filter_map(|value| value.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default();
        let kind = if movie { "movie" } else { "series" };
        if q.scope == "active" && missing.is_empty() {
            continue;
        }
        if !needle.is_empty()
            && !title.to_lowercase().contains(&needle)
            && !id.to_string().contains(&needle)
        {
            continue;
        }
        let done_languages = done
            .get(&(kind.to_string(), id))
            .cloned()
            .unwrap_or_default();
        rows.push(LibRow {
            aid: if movie {
                format!("m:{id}")
            } else {
                id.to_string()
            },
            title: if title.is_empty() {
                format!("(episode {id})")
            } else {
                title
            },
            kind,
            missing: if missing.is_empty() {
                "—".to_string()
            } else {
                missing.join(", ")
            },
            done: if done_languages.is_empty() {
                "—".to_string()
            } else {
                done_languages.join(", ")
            },
            excluded: excluded.contains(&id),
        });
    }

    if q.sort == "id" {
        rows.sort_by_key(|row| row.aid.clone());
    } else {
        rows.sort_by_key(|row| row.title.to_lowercase());
    }
    if q.dir == "desc" {
        rows.reverse();
    }
    rows
}

pub(crate) fn truthy(value: &str) -> bool {
    matches!(
        value.trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

pub(crate) fn field_display_value(cfg: &Config, key: &str, default: &str) -> String {
    let value = cfg
        .raw
        .get(key)
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| default.to_string());
    crate::config::mask_for_log(&value).into_owned()
}

pub(crate) fn pinned_keys() -> Vec<&'static str> {
    FIELDS
        .iter()
        .map(|field| field.key)
        .filter(|key| crate::config::env_pinned(key))
        .collect()
}

/// Diff the submitted form against the masked/displayed baseline.
pub(crate) fn collect_changes(form: &HashMap<String, String>) -> Vec<(String, String)> {
    let mut changes = Vec::new();
    for field in FIELDS {
        if crate::config::env_pinned(field.key) {
            continue;
        }
        let original = form
            .get(&format!("orig__{}", field.key))
            .map(|value| value.trim().to_string())
            .unwrap_or_default();
        let submitted = match field.kind {
            FieldKind::Bool => {
                if form.contains_key(field.key) {
                    "true".to_string()
                } else {
                    "false".to_string()
                }
            }
            _ => form
                .get(field.key)
                .map(|value| value.trim().to_string())
                .unwrap_or_default(),
        };
        if field.kind == FieldKind::Secret && submitted.is_empty() {
            continue;
        }
        let unchanged = if field.kind == FieldKind::Bool {
            truthy(&submitted) == truthy(&original)
        } else {
            submitted == original
        };
        if !unchanged {
            changes.push((field.key.to_string(), submitted));
        }
    }
    changes
}

pub(crate) fn invalid_number(pairs: &[(String, String)]) -> Option<(String, String)> {
    pairs.iter().find_map(|(key, value)| {
        crate::config::value_requirement(key, value).map(|want| (key.clone(), want))
    })
}

pub(crate) fn shadowed_attempts(cfg: &Config, form: &HashMap<String, String>) -> Vec<&'static str> {
    FIELDS
        .iter()
        .filter(|field| crate::config::env_pinned(field.key))
        .filter(|field| {
            let submitted = form
                .get(field.key)
                .map(|value| value.trim().to_string())
                .unwrap_or_default();
            let current = field_display_value(cfg, field.key, field.default);
            if field.kind == FieldKind::Bool {
                form.contains_key(field.key) && truthy(&submitted) != truthy(&current)
            } else {
                !submitted.is_empty() && submitted != current
            }
        })
        .map(|field| field.key)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn form(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs
            .iter()
            .map(|(key, value)| ((*key).to_string(), (*value).to_string()))
            .collect()
    }

    #[test]
    fn collect_changes_diffs_against_hidden_baseline() {
        let values = form(&[
            ("orig__TARGET_LANGS", "id,en"),
            ("TARGET_LANGS", "id,en"),
            ("orig__MAX_EPS_PER_RUN", "8"),
            ("MAX_EPS_PER_RUN", "4"),
        ]);
        assert_eq!(
            collect_changes(&values),
            vec![("MAX_EPS_PER_RUN".to_string(), "4".to_string())]
        );
    }

    #[test]
    fn invalid_number_checks_the_consumers_range() {
        let pairs = |key: &str, value: &str| vec![(key.to_string(), value.to_string())];
        let key = |value: Option<(String, String)>| value.map(|(key, _)| key);
        assert_eq!(
            key(invalid_number(&pairs("WEBHOOK_PORT", "70000"))),
            Some("WEBHOOK_PORT".to_string())
        );
        assert_eq!(invalid_number(&pairs("WEBHOOK_PORT", "65535")), None);
        assert_eq!(invalid_number(&pairs("MAX_CUE_MS", "4000000000")), None);
        assert_eq!(invalid_number(&pairs("LADDER_MIN_CJK", "0.55")), None);
        let (bad, want) = invalid_number(&pairs("MAX_EPS_PER_RUN", "4.5")).unwrap();
        assert_eq!(bad, "MAX_EPS_PER_RUN");
        assert!(want.contains("whole number"));
        let (bad, want) = invalid_number(&pairs("LADDER_MIN_CJK", "abc")).unwrap();
        assert_eq!(bad, "LADDER_MIN_CJK");
        assert_eq!(want, "a number");
    }

    #[test]
    fn pinned_bool_does_not_block_unrelated_saves() {
        let _guard = crate::config::ENV_LOCK
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let prior = std::env::var_os("AI_MARKER_CUE");
        std::env::set_var("AI_MARKER_CUE", "true");
        let cfg = Config::load().unwrap();
        let untouched = form(&[("orig__MAX_EPS_PER_RUN", "8"), ("MAX_EPS_PER_RUN", "4")]);
        assert!(shadowed_attempts(&cfg, &untouched).is_empty());
        let crafted = form(&[("AI_MARKER_CUE", "false")]);
        assert_eq!(shadowed_attempts(&cfg, &crafted), vec!["AI_MARKER_CUE"]);
        match prior {
            Some(value) => std::env::set_var("AI_MARKER_CUE", value),
            None => std::env::remove_var("AI_MARKER_CUE"),
        }
    }

    #[test]
    fn settings_display_masks_a_credential_without_saving_the_mask() {
        let _guard = crate::config::ENV_LOCK
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let prior_dir = std::env::var_os("ASRSUB_CONFIG_DIR");
        let prior_url = std::env::var_os("SONARR_URL");
        std::env::remove_var("SONARR_URL");
        let dir = tempfile::tempdir().unwrap();
        std::env::set_var("ASRSUB_CONFIG_DIR", dir.path());
        crate::config::write_overrides(&[(
            "SONARR_URL".to_string(),
            "http://svc:PWZ9K@sonarr.lan:8989/api/v3".to_string(),
        )])
        .unwrap();
        let cfg = Config::load().unwrap();
        let shown = field_display_value(&cfg, "SONARR_URL", "");
        assert_eq!(shown, "http://***:***@sonarr.lan:8989/api/v3");
        let untouched = form(&[("orig__SONARR_URL", &shown), ("SONARR_URL", &shown)]);
        assert!(collect_changes(&untouched).is_empty());
        let edited = form(&[
            ("orig__SONARR_URL", &shown),
            ("SONARR_URL", "http://new.lan:8989"),
        ]);
        assert_eq!(
            collect_changes(&edited),
            vec![("SONARR_URL".to_string(), "http://new.lan:8989".to_string())]
        );
        match prior_dir {
            Some(value) => std::env::set_var("ASRSUB_CONFIG_DIR", value),
            None => std::env::remove_var("ASRSUB_CONFIG_DIR"),
        }
        match prior_url {
            Some(value) => std::env::set_var("SONARR_URL", value),
            None => std::env::remove_var("SONARR_URL"),
        }
    }

    #[test]
    fn collect_changes_handles_bools_and_secrets() {
        let values = form(&[
            ("orig__AI_MARKER_CUE", "true"),
            ("AI_MARKER_CUE", "on"),
            ("orig__JIMAKU_DIRECT_ENABLED", "true"),
        ]);
        assert_eq!(
            collect_changes(&values),
            vec![("JIMAKU_DIRECT_ENABLED".to_string(), "false".to_string())]
        );
        assert!(collect_changes(&form(&[("orig__SONARR_API_KEY", "")])).is_empty());
        assert_eq!(
            collect_changes(&form(&[
                ("orig__SONARR_API_KEY", ""),
                ("SONARR_API_KEY", "new"),
            ])),
            vec![("SONARR_API_KEY".to_string(), "new".to_string())]
        );
    }

    #[test]
    fn integer_bounds_match_the_consumers_range() {
        let expected: &[(&str, u64)] = &[
            ("MAX_EPS_PER_RUN", usize::MAX as u64),
            ("EPISODE_CONCURRENCY", usize::MAX as u64),
            ("ASR_CONCURRENCY", usize::MAX as u64),
            ("TRANSLATE_CONCURRENCY", usize::MAX as u64),
            ("UPLOAD_CONCURRENCY", usize::MAX as u64),
            ("TRANSLATE_CHUNK", usize::MAX as u64),
            ("CPS_MERGE_MAX_CHARS", usize::MAX as u64),
            ("LADDER_MIN_CUES", usize::MAX as u64),
            ("LADDER_MIN_CHARS", usize::MAX as u64),
            ("MAX_CUE_MS", u32::MAX as u64),
            ("AI_MARKER_CUE_MS", u32::MAX as u64),
            ("CPS_MERGE_MAX_DUR_MS", u32::MAX as u64),
            ("CPS_MERGE_MAX_GAP_MS", u32::MAX as u64),
            ("WEBHOOK_PORT", u16::MAX as u64),
        ];
        for (key, max) in expected {
            let field = FIELDS
                .iter()
                .find(|field| field.key == *key)
                .unwrap_or_else(|| panic!("{key} is not in FIELDS"));
            assert_eq!(field.kind, FieldKind::Int(*max), "bound drift for {key}");
        }
        let int_fields: Vec<&str> = FIELDS
            .iter()
            .filter(|field| matches!(field.kind, FieldKind::Int(_)))
            .map(|field| field.key)
            .collect();
        assert_eq!(int_fields.len(), expected.len());
    }

    #[test]
    fn integer_bounds_match_the_struct_field_width() {
        let _guard = crate::config::ENV_LOCK
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let dir = tempfile::tempdir().unwrap();
        let prior_dir = std::env::var_os("ASRSUB_CONFIG_DIR");
        std::env::set_var("ASRSUB_CONFIG_DIR", dir.path());
        let cfg = Config::load().unwrap();
        match prior_dir {
            Some(value) => std::env::set_var("ASRSUB_CONFIG_DIR", value),
            None => std::env::remove_var("ASRSUB_CONFIG_DIR"),
        }
        let mut seen = Vec::new();
        for (key, bound) in FIELDS.iter().filter_map(|field| match field.kind {
            FieldKind::Int(max) => Some((field.key, max)),
            _ => None,
        }) {
            let width = match key {
                "MAX_EPS_PER_RUN" => std::mem::size_of_val(&cfg.max_eps_per_run),
                "EPISODE_CONCURRENCY" => std::mem::size_of_val(&cfg.episode_concurrency),
                "ASR_CONCURRENCY" => std::mem::size_of_val(&cfg.asr_concurrency),
                "TRANSLATE_CONCURRENCY" => std::mem::size_of_val(&cfg.translate_concurrency),
                "UPLOAD_CONCURRENCY" => std::mem::size_of_val(&cfg.upload_concurrency),
                "TRANSLATE_CHUNK" => std::mem::size_of_val(&cfg.translate_chunk),
                "CPS_MERGE_MAX_CHARS" => std::mem::size_of_val(&cfg.cps_merge_max_chars),
                "LADDER_MIN_CUES" => std::mem::size_of_val(&cfg.ladder_min_cues),
                "LADDER_MIN_CHARS" => std::mem::size_of_val(&cfg.ladder_min_chars),
                "MAX_CUE_MS" => std::mem::size_of_val(&cfg.max_cue_ms),
                "AI_MARKER_CUE_MS" => std::mem::size_of_val(&cfg.ai_marker_cue_ms),
                "CPS_MERGE_MAX_DUR_MS" => std::mem::size_of_val(&cfg.cps_merge_max_dur_ms),
                "CPS_MERGE_MAX_GAP_MS" => std::mem::size_of_val(&cfg.cps_merge_max_gap_ms),
                "WEBHOOK_PORT" => std::mem::size_of_val(&cfg.webhook_port),
                other => panic!("{other} is an Int field with no struct field here"),
            };
            let bound_width = if bound == usize::MAX as u64 {
                std::mem::size_of::<usize>()
            } else if bound == u32::MAX as u64 {
                4
            } else if bound == u16::MAX as u64 {
                2
            } else {
                panic!("{key} has a non-type maximum bound: {bound}");
            };
            assert_eq!(width, bound_width, "bound drift for {key}");
            seen.push(key);
        }
        assert_eq!(seen.len(), 14);
    }

    #[test]
    fn query_round_trips_through_action_urls() {
        let query = LibQuery {
            q: "spice & wolf".to_string(),
            scope: "active".to_string(),
            sort: "id".to_string(),
            dir: String::new(),
        };
        let encoded = query.to_query();
        assert!(encoded.contains("q=spice%20%26%20wolf"));
        assert!(encoded.contains("scope=active"));
        assert!(encoded.contains("sort=id"));
        assert_eq!(
            ep_action_url("m:7", "retry", &encoded),
            format!("/ui/episode/m:7/retry?{encoded}")
        );
        assert_eq!(ep_action_url("42", "skip", ""), "/ui/episode/42/skip");
    }

    #[test]
    fn invalid_number_rejects_garbage_for_number_fields() {
        let pairs = |key: &str, value: &str| vec![(key.to_string(), value.to_string())];
        assert!(invalid_number(&pairs("MAX_EPS_PER_RUN", "abc")).is_some());
        assert_eq!(invalid_number(&pairs("MAX_EPS_PER_RUN", "4")), None);
        assert_eq!(invalid_number(&pairs("CPS_MERGE_MAX", "20.5")), None);
        assert_eq!(invalid_number(&pairs("MAX_EPS_PER_RUN", "")), None);
        assert_eq!(invalid_number(&pairs("JELLYFIN_URL", "http://x")), None);
    }

    #[test]
    fn settings_reject_reserved_discord_key() {
        assert!(crate::feature_modules::discord_config::is_reserved_key(
            " DISCORD_WEBHOOK_URL "
        ));
    }
}
