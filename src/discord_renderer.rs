//! Deterministic, bounded Discord digest rendering.

use super::discord_state_codec::encode_string;
use super::discord_state_schema::{DeliveryView, OverflowSummaryV1, PayloadBytes};
use super::discord_types::{
    AggregateDisposition, EpisodeKind, EpisodeRunReport, FailureClass, ItemFailure, TargetStatus,
    WarningClass,
};

const MAX_ROW_SCALARS: usize = 160;
const MAX_FIELD_NAME_SCALARS: usize = 32;
const MAX_FIELD_VALUE_SCALARS: usize = 900;
const MAX_DESCRIPTION_SCALARS: usize = 800;
const MAX_EMBED_TEXT_SCALARS: usize = 4_000;
const MAX_ROWS: usize = 8;
const MAX_TARGET_ENTRIES: usize = 6;

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
                format!("{}:{token}", target.language().as_str()),
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

fn outcome(view: &DeliveryView, rows: &[Row]) -> (String, u32) {
    let summary = view.overflow_summary();
    let attention = summary.attention_reports() > 0
        || summary.target_outcomes() > 0
        || summary.pre_admission_drops() > 0
        || summary.blocked_admissions() > 0
        || rows.iter().any(|r| r.attention);
    if attention {
        ("❌ ASRSub · Attention required".to_string(), 0xED4245)
    } else if summary.warning_reports() > 0 || rows.iter().any(|r| r.severity == 1) {
        ("⚠ ASRSub · Partial".to_string(), 0xFEE75C)
    } else {
        ("✅ ASRSub · Complete".to_string(), 0x57F287)
    }
}

fn field_value(rows: &[String]) -> String {
    if rows.is_empty() {
        "none".to_string()
    } else {
        rows.join("\n")
    }
}

pub(crate) fn render(view: &DeliveryView) -> Result<PayloadBytes, RenderError> {
    let mut episode_rows: Vec<Row> = view
        .reports()
        .iter()
        .map(make_row)
        .collect::<Result<_, _>>()?;
    episode_rows.sort_by(|a, b| (a.severity, &a.text).cmp(&(b.severity, &b.text)));
    let (mut attention, mut completed) = summary_rows(view.overflow_summary());
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
    if omitted_attention > 0 {
        let marker = format!("+{omitted_attention} more attention episodes");
        if let Some(last) = attention_rows.last_mut() {
            last.push_str(" ");
            last.push_str(&marker);
        } else if let Some(last) = attention.last_mut() {
            last.push_str(" ");
            last.push_str(&marker);
        }
    }
    if omitted_completed > 0 {
        let marker = format!("+{omitted_completed} more completed episodes");
        if let Some(last) = completed_rows.last_mut() {
            last.push_str(" ");
            last.push_str(&marker);
        } else if let Some(last) = completed.last_mut() {
            last.push_str(" ");
            last.push_str(&marker);
        }
    }
    let (title, color) = outcome(view, &episode_rows);
    debug_assert!(scalar_len("Needs attention") <= MAX_FIELD_NAME_SCALARS);
    debug_assert!(scalar_len("Completed") <= MAX_FIELD_NAME_SCALARS);
    let needs = field_value(
        &attention
            .iter()
            .chain(attention_rows.iter())
            .cloned()
            .collect::<Vec<_>>(),
    );
    let done = field_value(
        &completed
            .iter()
            .chain(completed_rows.iter())
            .cloned()
            .collect::<Vec<_>>(),
    );
    if scalar_len(&needs) > MAX_FIELD_VALUE_SCALARS || scalar_len(&done) > MAX_FIELD_VALUE_SCALARS {
        return Err(RenderError::TooLarge);
    }
    let description = "ASRSub daemon pass digest";
    if scalar_len(&title) > MAX_DESCRIPTION_SCALARS
        || scalar_len(description) > MAX_DESCRIPTION_SCALARS
    {
        return Err(RenderError::TooLarge);
    }
    let total = scalar_len(&title)
        + scalar_len(description)
        + scalar_len("Needs attention")
        + scalar_len("Completed")
        + scalar_len(&needs)
        + scalar_len(&done)
        + 4;
    if total > MAX_EMBED_TEXT_SCALARS {
        return Err(RenderError::TooLarge);
    }
    let mut bytes = Vec::new();
    bytes.extend_from_slice(b"{\"embeds\":[{\"title\":");
    bytes.extend_from_slice(&encode_string(&title));
    bytes.extend_from_slice(b",\"description\":");
    bytes.extend_from_slice(&encode_string(description));
    bytes.extend_from_slice(b",\"color\":");
    bytes.extend_from_slice(color.to_string().as_bytes());
    bytes.extend_from_slice(b",\"fields\":[{\"name\":\"Needs attention\",\"value\":");
    bytes.extend_from_slice(&encode_string(&needs));
    bytes.extend_from_slice(b",\"inline\":false},{\"name\":\"Completed\",\"value\":");
    bytes.extend_from_slice(&encode_string(&done));
    bytes.extend_from_slice(b",\"inline\":false}]}],\"allowed_mentions\":{\"parse\":[]}}");
    PayloadBytes::try_from_bytes(bytes.into_boxed_slice()).map_err(|_| RenderError::TooLarge)
}

#[cfg(test)]
mod tests {
    use super::super::discord_state_schema::OverflowSummaryV1;
    use super::super::discord_text::SafeDisplayText;
    use super::super::discord_types::{BoundedTargets, TargetLanguage, TargetRunResult};
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
        assert_eq!(value["allowed_mentions"]["parse"], serde_json::json!([]));
        assert!(value.get("content").is_none());
        assert_eq!(
            value["embeds"][0]["description"],
            "ASRSub daemon pass digest"
        );
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
        assert!(String::from_utf8_lossy(payload.as_bytes()).contains("id:warn-unknown"));
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
    fn redacts_untrusted_fields() {
        let (reports, _) = super::super::discord_types::BoundedReports::from_reports([report(
            TargetStatus::Failed {
                class: FailureClass::Unknown,
            },
            "url https://discord.com/token",
        )])
        .unwrap();
        let payload = render(&DeliveryView::new(1, reports, OverflowSummaryV1::default())).unwrap();
        let text = String::from_utf8_lossy(payload.as_bytes());
        assert!(!text.contains("https://"));
    }
}
