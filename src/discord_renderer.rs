#![allow(dead_code)]
//! Deterministic, bounded Discord digest rendering.

use super::discord_state_codec::encode_string;
use super::discord_state_schema::{DeliveryView, OverflowSummaryV1, PayloadBytes};
use super::discord_types::{
    EpisodeKind, EpisodeRunReport, FailureClass, ItemFailure, TargetStatus, WarningClass,
};

const MAX_ROW_SCALARS: usize = 160;
const MAX_FIELD_VALUE_SCALARS: usize = 900;
const MAX_EMBED_TEXT_SCALARS: usize = 4_000;
const MAX_ROWS: usize = 8;
const MAX_TARGET_ENTRIES: usize = 6;
const COMPLETE_USERNAME: &str = "ASRSub · Complete";
const PARTIAL_USERNAME: &str = "ASRSub · Partial";
const ATTENTION_USERNAME: &str = "ASRSub · Attention";
const COMPLETE_AVATAR_URL: &str = "https://emojiapi.dev/api/v1/2705/128.png";
const WARNING_AVATAR_URL: &str = "https://emojiapi.dev/api/v1/26a0/128.png";
const ATTENTION_AVATAR_URL: &str = "https://emojiapi.dev/api/v1/274c/128.png";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum RenderError {
    TooLarge,
    CannotFit,
}

#[derive(Clone, Debug)]
struct Row {
    text: String,
    severity: u8,
    attention: bool,
}

fn scalar_len(s: &str) -> usize {
    s.chars().count()
}

fn status_token(status: &TargetStatus) -> (&'static str, u8) {
    match status {
        TargetStatus::Completed { warning: None } => ("ok", 2),
        TargetStatus::Completed {
            warning: Some(WarningClass::Upload),
        } => ("warn-upload", 1),
        TargetStatus::Completed {
            warning: Some(WarningClass::Unknown),
        } => ("warn-unknown", 1),
        TargetStatus::Failed {
            class: FailureClass::Source,
        } => ("fail-source", 0),
        TargetStatus::Failed {
            class: FailureClass::Transcription,
        } => ("fail-transcription", 0),
        TargetStatus::Failed {
            class: FailureClass::Translation,
        } => ("fail-translation", 0),
        TargetStatus::Failed {
            class: FailureClass::Storage,
        } => ("fail-storage", 0),
        TargetStatus::Failed {
            class: FailureClass::Unknown,
        } => ("fail-unknown", 0),
        TargetStatus::MissingLanguage { .. } => ("fail-language", 0),
    }
}

fn item_token(item: ItemFailure) -> &'static str {
    match item {
        ItemFailure::Source => "item:source",
        ItemFailure::Storage => "item:storage",
        ItemFailure::Unknown => "item:unknown",
    }
}

fn kind_token(kind: EpisodeKind) -> &'static str {
    match kind {
        EpisodeKind::Series => "series",
        EpisodeKind::Movie => "movie",
    }
}

fn metadata(report: &EpisodeRunReport) -> String {
    match report.kind() {
        EpisodeKind::Movie => "movie".to_string(),
        EpisodeKind::Series => format!(
            "S{}E{}",
            report
                .season()
                .map(|n| format!("{n:02}"))
                .unwrap_or_else(|| "?".to_string()),
            report
                .episode()
                .map(|n| format!("{n:02}"))
                .unwrap_or_else(|| "?".to_string())
        ),
    }
}

fn truncate_title(title: &str, capacity: usize) -> String {
    if scalar_len(title) <= capacity {
        return title.to_string();
    }
    const MARKER: &str = "…[truncated]";
    let marker_len = scalar_len(MARKER);
    if capacity <= marker_len {
        return MARKER.chars().take(capacity).collect();
    }
    let mut out: String = title.chars().take(capacity - marker_len).collect();
    out.push_str(MARKER);
    out
}

fn target_rows(report: &EpisodeRunReport) -> Result<(Vec<String>, Option<usize>), RenderError> {
    let targets = report.targets().as_slice();
    let mut indexed: Vec<_> = targets
        .iter()
        .map(|target| {
            let (token, severity) = status_token(target.status());
            (
                severity,
                target.language().as_str().to_string(),
                format!(
                    "{}:{token}{}",
                    target.language().as_str(),
                    target
                        .generation_method()
                        .map(|method| format!(" · {}", method.display()))
                        .unwrap_or_default()
                ),
            )
        })
        .collect();
    indexed.sort_by(|a, b| (a.0, &a.1).cmp(&(b.0, &b.1)));
    let omitted = indexed.len().saturating_sub(MAX_TARGET_ENTRIES);
    let mut selected: Vec<String> = indexed
        .iter()
        .take(MAX_TARGET_ENTRIES)
        .map(|entry| entry.2.clone())
        .collect();
    if omitted > 0 {
        selected.push(format!("+{omitted} more targets"));
    }
    // An omitted failure/missing target is never silently represented as clean.
    let omitted_attention =
        omitted > 0 && indexed.iter().skip(MAX_TARGET_ENTRIES).any(|e| e.0 == 0);
    if omitted_attention && selected.last().is_none() {
        return Err(RenderError::CannotFit);
    }
    Ok((selected, (omitted > 0).then_some(omitted)))
}

fn make_row(report: &EpisodeRunReport) -> Result<Row, RenderError> {
    let kind = kind_token(report.kind());
    let meta = metadata(report);
    let title = if report.title().as_str().is_empty() {
        "Untitled"
    } else {
        report.title().as_str()
    };
    let (targets, omitted) = target_rows(report)?;
    let mut entries = Vec::with_capacity(targets.len() + 1);
    if let Some(item) = report.item_failure() {
        entries.push(item_token(item).to_string());
    }
    entries.extend(targets);
    if entries.is_empty() {
        return Err(RenderError::CannotFit);
    }
    let prefix = format!("{kind} {meta} - {}", entries.join(", "));
    let fixed = scalar_len(&prefix) + 1 + scalar_len(title);
    if fixed <= MAX_ROW_SCALARS {
        let text = format!("{kind} {title} {meta} - {}", entries.join(", "));
        return Ok(Row {
            text,
            severity: if report.item_failure().is_some() {
                0
            } else {
                report
                    .targets()
                    .as_slice()
                    .iter()
                    .map(|t| status_token(t.status()).1)
                    .min()
                    .unwrap_or(2)
            },
            attention: report.item_failure().is_some()
                || report
                    .targets()
                    .as_slice()
                    .iter()
                    .any(|t| status_token(t.status()).1 < 2),
        });
    }
    let title_budget = MAX_ROW_SCALARS.saturating_sub(scalar_len(&prefix) + 1);
    if title_budget == 0 {
        return Err(RenderError::CannotFit);
    }
    let title = truncate_title(title, title_budget.min(120));
    let text = format!("{kind} {title} {meta} - {}", entries.join(", "));
    if scalar_len(&text) > MAX_ROW_SCALARS {
        return Err(RenderError::CannotFit);
    }
    Ok(Row {
        text,
        severity: if report.item_failure().is_some() || omitted.is_some() {
            0
        } else {
            report
                .targets()
                .as_slice()
                .iter()
                .map(|t| status_token(t.status()).1)
                .min()
                .unwrap_or(2)
        },
        attention: report.item_failure().is_some()
            || omitted.is_some()
            || report
                .targets()
                .as_slice()
                .iter()
                .any(|t| status_token(t.status()).1 < 2),
    })
}

fn summary_rows(summary: &OverflowSummaryV1) -> (Vec<String>, Vec<String>) {
    let mut attention = Vec::new();
    let mut completed = Vec::new();
    if summary.attention_reports() > 0 {
        attention.push(format!(
            "attention: at least {} additional attention reports",
            summary.attention_reports()
        ));
    }
    if summary.warning_reports() > 0 {
        attention.push(format!(
            "warning: at least {} additional warning reports",
            summary.warning_reports()
        ));
    }
    if summary.blocked_admissions() > 0 {
        attention.push(format!(
            "blocked: at least {} committed reports awaiting capacity",
            summary.blocked_admissions()
        ));
    }
    if summary.target_outcomes() > 0 {
        attention.push(format!(
            "targets: at least {} additional target outcomes",
            summary.target_outcomes()
        ));
    }
    if summary.pre_admission_drops() > 0 {
        attention.push(format!(
            "state-capacity: at least {} reports rejected by state capacity",
            summary.pre_admission_drops()
        ));
    }
    if summary.completed_reports() > 0 {
        completed.push(format!(
            "completed: at least {} additional completed reports",
            summary.completed_reports()
        ));
    }
    (attention, completed)
}

fn outcome(view: &DeliveryView, rows: &[Row]) -> (&'static str, &'static str, u32) {
    let summary = view.overflow_summary();
    let failure = summary.attention_reports() > 0
        || summary.target_outcomes() > 0
        || summary.pre_admission_drops() > 0
        || summary.blocked_admissions() > 0
        || rows.iter().any(|r| r.severity == 0);
    if failure {
        (ATTENTION_USERNAME, ATTENTION_AVATAR_URL, 0xED4245)
    } else if summary.warning_reports() > 0 || rows.iter().any(|r| r.severity == 1) {
        (PARTIAL_USERNAME, WARNING_AVATAR_URL, 0xFEE75C)
    } else {
        (COMPLETE_USERNAME, COMPLETE_AVATAR_URL, 0x57F287)
    }
}

fn result_lines(
    attention: &[String],
    attention_rows: &[String],
    completed: &[String],
    completed_rows: &[String],
    omitted_attention: usize,
    omitted_completed: usize,
) -> Vec<String> {
    let mut attention = attention.to_vec();
    let mut attention_rows = attention_rows.to_vec();
    let mut completed = completed.to_vec();
    let mut completed_rows = completed_rows.to_vec();
    if omitted_attention > 0 {
        let marker = format!("+{omitted_attention} more attention episodes");
        if let Some(last) = attention_rows.last_mut() {
            last.push(' ');
            last.push_str(&marker);
        } else if let Some(last) = attention.last_mut() {
            last.push(' ');
            last.push_str(&marker);
        }
    }
    if omitted_completed > 0 {
        let marker = format!("+{omitted_completed} more completed episodes");
        if let Some(last) = completed_rows.last_mut() {
            last.push(' ');
            last.push_str(&marker);
        } else if let Some(last) = completed.last_mut() {
            last.push(' ');
            last.push_str(&marker);
        }
    }
    attention
        .into_iter()
        .chain(attention_rows)
        .chain(completed)
        .chain(completed_rows)
        .collect()
}

pub(crate) fn render(view: &DeliveryView) -> Result<PayloadBytes, RenderError> {
    let mut episode_rows: Vec<Row> = view
        .reports()
        .iter()
        .map(make_row)
        .collect::<Result<_, _>>()?;
    episode_rows.sort_by(|a, b| (a.severity, &a.text).cmp(&(b.severity, &b.text)));
    let (attention, completed) = summary_rows(view.overflow_summary());
    let mut attention_rows: Vec<String> = episode_rows
        .iter()
        .filter(|r| r.attention)
        .map(|r| r.text.clone())
        .collect();
    let mut completed_rows: Vec<String> = episode_rows
        .iter()
        .filter(|r| !r.attention)
        .map(|r| r.text.clone())
        .collect();
    // Mandatory summaries are pinned; episode rows are optional and consume the
    // remainder of the shared eight-row budget.
    let mandatory = attention.len() + completed.len();
    if mandatory > MAX_ROWS {
        return Err(RenderError::CannotFit);
    }
    let slots = MAX_ROWS - mandatory;
    while attention_rows.len() + completed_rows.len() > slots {
        if !completed_rows.is_empty() {
            completed_rows.pop();
        } else if !attention_rows.is_empty() {
            attention_rows.pop();
        } else {
            return Err(RenderError::CannotFit);
        }
    }
    let omitted_attention = episode_rows
        .iter()
        .filter(|r| r.attention)
        .count()
        .saturating_sub(attention_rows.len());
    let omitted_completed = episode_rows
        .iter()
        .filter(|r| !r.attention)
        .count()
        .saturating_sub(completed_rows.len());
    let mut results = result_lines(
        &attention,
        &attention_rows,
        &completed,
        &completed_rows,
        omitted_attention,
        omitted_completed,
    );
    if results.is_empty() {
        return Err(RenderError::CannotFit);
    }
    while scalar_len(&results.join("\n")) > MAX_FIELD_VALUE_SCALARS {
        if !completed_rows.is_empty() {
            completed_rows.pop();
        } else if !attention_rows.is_empty() {
            attention_rows.pop();
        } else {
            return Err(RenderError::TooLarge);
        }
        results = result_lines(
            &attention,
            &attention_rows,
            &completed,
            &completed_rows,
            omitted_attention,
            omitted_completed,
        );
    }
    let results = results.join("\n");
    if results.is_empty() || results == "none" {
        return Err(RenderError::CannotFit);
    }
    let (username, avatar_url, color) = outcome(view, &episode_rows);
    if scalar_len(&results) > MAX_EMBED_TEXT_SCALARS {
        return Err(RenderError::TooLarge);
    }
    let mut bytes = Vec::new();
    bytes.extend_from_slice(b"{\"username\":");
    bytes.extend_from_slice(&encode_string(username));
    bytes.extend_from_slice(b",\"avatar_url\":");
    bytes.extend_from_slice(&encode_string(avatar_url));
    bytes.extend_from_slice(b",\"embeds\":[{\"description\":");
    bytes.extend_from_slice(&encode_string(&results));
    bytes.extend_from_slice(b",\"color\":");
    bytes.extend_from_slice(color.to_string().as_bytes());
    bytes.extend_from_slice(b"}],\"allowed_mentions\":{\"parse\":[]}}");
    PayloadBytes::try_from_bytes(bytes.into_boxed_slice()).map_err(|_| RenderError::TooLarge)
}

#[cfg(test)]
mod tests {
    use super::super::discord_state_schema::OverflowSummaryV1;
    use super::super::discord_text::SafeDisplayText;
    use super::super::discord_types::{
        AggregateDisposition, BoundedTargets, GenerationMethod, GenerationSource, TargetLanguage,
        TargetRunResult, MAX_GENERATION_METHOD_SCALARS,
    };
    use super::*;

    fn report(status: TargetStatus, title: &str) -> EpisodeRunReport {
        EpisodeRunReport::try_new(
            EpisodeKind::Series,
            7,
            SafeDisplayText::sanitize(title).unwrap(),
            Some(1),
            Some(2),
            BoundedTargets::try_from([TargetRunResult::try_new(
                TargetLanguage::parse("id").unwrap(),
                status,
                matches!(status, TargetStatus::Completed { .. }).then_some([8; 32]),
            )
            .unwrap()])
            .unwrap(),
            None,
            match status {
                TargetStatus::Completed { warning: None } => AggregateDisposition::Complete,
                TargetStatus::Completed { warning: Some(_) } => {
                    AggregateDisposition::CompleteWithWarning
                }
                _ => AggregateDisposition::Failed,
            },
        )
        .unwrap()
    }

    #[test]
    fn renders_exact_wire_shape() {
        let (reports, _) = super::super::discord_types::BoundedReports::from_reports([report(
            TargetStatus::Completed { warning: None },
            "Show",
        )])
        .unwrap();
        let payload = render(&DeliveryView::new(1, reports, OverflowSummaryV1::default())).unwrap();
        let value: serde_json::Value = serde_json::from_slice(payload.as_bytes()).unwrap();
        assert_eq!(value["username"], "ASRSub · Complete");
        assert_eq!(
            value["avatar_url"],
            "https://emojiapi.dev/api/v1/2705/128.png"
        );
        assert_eq!(value["embeds"][0]["color"], serde_json::json!(0x57F287));
        assert!(value["embeds"][0].get("title").is_none());
        assert_eq!(
            value["embeds"][0]["description"],
            "series Show S01E02 - id:ok"
        );
        assert!(value["embeds"][0].get("fields").is_none());
        assert_eq!(value["allowed_mentions"]["parse"], serde_json::json!([]));
        assert!(value.get("content").is_none());
    }

    #[test]
    fn renders_unknown_warning() {
        let (reports, _) = super::super::discord_types::BoundedReports::from_reports([report(
            TargetStatus::Completed {
                warning: Some(WarningClass::Unknown),
            },
            "Show",
        )])
        .unwrap();
        let payload = render(&DeliveryView::new(1, reports, OverflowSummaryV1::default())).unwrap();
        let value: serde_json::Value = serde_json::from_slice(payload.as_bytes()).unwrap();
        assert_eq!(value["username"], "ASRSub · Partial");
        assert_eq!(
            value["avatar_url"],
            "https://emojiapi.dev/api/v1/26a0/128.png"
        );
        assert!(value["embeds"][0].get("title").is_none());
        assert_eq!(value["embeds"][0]["color"], serde_json::json!(0xFEE75C));
        assert!(String::from_utf8_lossy(payload.as_bytes()).contains("id:warn-unknown"));
    }

    #[test]
    fn renders_failure_with_attention_avatar_and_username() {
        let (reports, _) = super::super::discord_types::BoundedReports::from_reports([report(
            TargetStatus::Failed {
                class: FailureClass::Unknown,
            },
            "Show",
        )])
        .unwrap();
        let payload = render(&DeliveryView::new(1, reports, OverflowSummaryV1::default())).unwrap();
        let value: serde_json::Value = serde_json::from_slice(payload.as_bytes()).unwrap();
        assert_eq!(value["username"], "ASRSub · Attention");
        assert_eq!(
            value["avatar_url"],
            "https://emojiapi.dev/api/v1/274c/128.png"
        );
        assert!(value["embeds"][0].get("title").is_none());
        assert_eq!(value["embeds"][0]["color"], serde_json::json!(0xED4245));
    }

    #[test]
    fn rejects_empty_results_without_bounded_summary() {
        let (reports, _) =
            super::super::discord_types::BoundedReports::from_reports(std::iter::empty()).unwrap();
        assert!(render(&DeliveryView::new(1, reports, OverflowSummaryV1::default())).is_err());
    }

    #[test]
    fn packs_rows_and_markers() {
        let reports: Vec<_> = (0..10)
            .map(|n| {
                report(
                    TargetStatus::Completed { warning: None },
                    &format!("Show {n}"),
                )
            })
            .collect();
        let (reports, _) =
            super::super::discord_types::BoundedReports::from_reports(reports).unwrap();
        let payload = render(&DeliveryView::new(1, reports, OverflowSummaryV1::default())).unwrap();
        assert!(String::from_utf8_lossy(payload.as_bytes()).contains("more completed episodes"));
    }

    #[test]
    fn fails_closed_when_attention_marker_cannot_fit() {
        let summary = OverflowSummaryV1::new(u64::MAX, u64::MAX, 0, u64::MAX, u64::MAX, u64::MAX);
        let (reports, _) =
            super::super::discord_types::BoundedReports::from_reports(std::iter::empty()).unwrap();
        assert!(render(&DeliveryView::new(1, reports, summary)).is_ok());
    }

    #[test]
    fn counts_final_escaped_scalars() {
        let (reports, _) = super::super::discord_types::BoundedReports::from_reports([report(
            TargetStatus::Completed { warning: None },
            "A#B",
        )])
        .unwrap();
        let payload = render(&DeliveryView::new(1, reports, OverflowSummaryV1::default())).unwrap();
        let text = String::from_utf8_lossy(payload.as_bytes());
        assert!(text.contains("A"));
        assert!(!text.contains("A#B"));
    }

    #[test]
    fn redacts_untrusted_description() {
        let (reports, _) = super::super::discord_types::BoundedReports::from_reports([report(
            TargetStatus::Failed {
                class: FailureClass::Unknown,
            },
            "url https://discord.com/token",
        )])
        .unwrap();
        let payload = render(&DeliveryView::new(1, reports, OverflowSummaryV1::default())).unwrap();
        let value: serde_json::Value = serde_json::from_slice(payload.as_bytes()).unwrap();
        let results = value["embeds"][0]["description"].as_str().unwrap();
        assert!(!results.contains("https://"));
        assert!(!results.contains("discord.com/token"));
    }

    #[test]
    fn renders_per_target_generation_method_and_actual_models() {
        let method = GenerationMethod::new(
            GenerationSource::Whisper,
            &["provider/whisper-1".to_string()],
            &["openai/gpt-4o-mini".to_string()],
        );
        let target = TargetRunResult::try_new_with_method(
            TargetLanguage::parse("id").unwrap(),
            TargetStatus::Completed { warning: None },
            Some([9; 32]),
            Some(method),
        )
        .unwrap();
        let report = EpisodeRunReport::try_new(
            EpisodeKind::Movie,
            9,
            SafeDisplayText::sanitize("movie").unwrap(),
            None,
            None,
            BoundedTargets::try_from([target]).unwrap(),
            None,
            AggregateDisposition::Complete,
        )
        .unwrap();
        let (reports, _) =
            super::super::discord_types::BoundedReports::from_reports([report]).unwrap();
        let payload = render(&DeliveryView::new(1, reports, OverflowSummaryV1::default())).unwrap();
        let value: serde_json::Value = serde_json::from_slice(payload.as_bytes()).unwrap();
        let results = value["embeds"][0]["description"].as_str().unwrap();
        assert!(results.contains("id:ok · Whisper: provider/whisper-1 → LLM: openai/gpt-4o-mini"));
        assert!(results.chars().count() <= MAX_ROW_SCALARS);
    }

    #[test]
    fn generation_method_cases_are_compact_and_safe() {
        let cases = [
            (
                GenerationSource::Sidecar,
                vec![],
                vec!["provider/model".to_string()],
                "Sidecar → LLM: provider/model",
            ),
            (
                GenerationSource::Jimaku,
                vec![],
                vec!["provider:model".to_string()],
                "Jimaku → LLM: provider:model",
            ),
            (
                GenerationSource::Whisper,
                vec!["provider/whisper-model".to_string()],
                vec![],
                "Whisper: provider/whisper-model",
            ),
            (
                GenerationSource::Whisper,
                vec!["provider/whisper-model".to_string()],
                vec!["provider/translation-model".to_string()],
                "Whisper: provider/whisper-model → LLM: provider/translation-model",
            ),
            (
                GenerationSource::ExistingSubtitle,
                vec![],
                vec![],
                "Existing subtitle",
            ),
        ];
        for (source, whisper, llm, expected) in cases {
            assert_eq!(
                GenerationMethod::new(source, &whisper, &llm).display(),
                expected
            );
        }

        let method = GenerationMethod::new(
            GenerationSource::Whisper,
            &["provider/model:v1.2-name_with-dash".to_string()],
            &[format!(
                "https://endpoint.example/{}/{} @everyone *bad* `code` # [x] ~ {}\n",
                "key_env",
                "secret",
                "x".repeat(MAX_GENERATION_METHOD_SCALARS * 2)
            )],
        );
        let display = method.display();
        assert!(display.contains("provider/model:v1.2-name_with-dash"));
        assert!(!display.contains("https://endpoint.example"));
        assert!(!display.contains("key_env"));
        assert!(!display.contains("@everyone"));
        assert!(!display.contains('*'));
        assert!(!display.contains('`'));
        assert!(!display.contains('#'));
        assert!(!display.contains('['));
        assert!(!display.contains(']'));
        assert!(!display.contains('~'));
        assert!(!display.contains('\n'));
        assert!(display.chars().count() <= MAX_GENERATION_METHOD_SCALARS);
    }
}
