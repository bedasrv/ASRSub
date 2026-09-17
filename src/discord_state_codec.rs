#![allow(dead_code, clippy::chunks_exact_to_as_chunks)]
//! The single canonical JSON and domain-hash implementation used by core
//! report/state identities.

use sha2::{Digest, Sha256};

use super::discord_state_schema::NotificationStateError;
use super::discord_types::{
    AggregateDisposition, EpisodeKind, EpisodeRunReport, FailureClass, ItemFailure,
    MissingLanguage, TargetStatus, WarningClass,
};

pub(crate) fn encode_string(value: &str) -> Vec<u8> {
    let mut out = Vec::with_capacity(value.len() + 2);
    out.push(b'"');
    for c in value.chars() {
        match c {
            '"' => out.extend_from_slice(br#"\""#),
            '\\' => out.extend_from_slice(br#"\\"#),
            c if (c as u32) <= 0x1f => {
                out.extend_from_slice(format!(r"\u{:04x}", c as u32).as_bytes())
            }
            '\u{2028}' => out.extend_from_slice(br#"\u2028"#),
            '\u{2029}' => out.extend_from_slice(br#"\u2029"#),
            c => {
                let mut buf = [0; 4];
                out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
            }
        }
    }
    out.push(b'"');
    out
}

pub(crate) fn domain_hash(domain: &'static [u8], bytes: &[u8]) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update(domain);
    h.update([0]);
    h.update(bytes);
    h.finalize().into()
}

pub(crate) fn hex(bytes: &[u8]) -> String {
    const TABLE: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(TABLE[(b >> 4) as usize] as char);
        out.push(TABLE[(b & 15) as usize] as char);
    }
    out
}

fn quoted(value: &str, out: &mut Vec<u8>) {
    out.extend_from_slice(&encode_string(value));
}
fn bool_value(value: bool, out: &mut Vec<u8>) {
    out.extend_from_slice(if value { b"true" } else { b"false" });
}
fn opt_u32(value: Option<u32>, out: &mut Vec<u8>) {
    match value {
        Some(v) => out.extend_from_slice(v.to_string().as_bytes()),
        None => out.extend_from_slice(b"null"),
    }
}

pub(crate) fn encode_report(report: &EpisodeRunReport) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(b"{\"schema\":\"report-v1\",\"kind\":");
    quoted(
        if report.kind() == EpisodeKind::Series {
            "series"
        } else {
            "movie"
        },
        &mut out,
    );
    out.extend_from_slice(b",\"episode_id\":");
    out.extend_from_slice(report.episode_id().to_string().as_bytes());
    out.extend_from_slice(b",\"title\":");
    quoted(report.title().as_str(), &mut out);
    out.extend_from_slice(b",\"season\":");
    opt_u32(report.season(), &mut out);
    out.extend_from_slice(b",\"episode\":");
    opt_u32(report.episode(), &mut out);
    out.extend_from_slice(b",\"targets\":[");
    for (i, target) in report.targets().as_slice().iter().enumerate() {
        if i != 0 {
            out.push(b',');
        }
        out.extend_from_slice(b"{\"language\":");
        quoted(target.language().as_str(), &mut out);
        out.extend_from_slice(b",\"status\":");
        quoted(target_status(target.status()), &mut out);
        out.extend_from_slice(b",\"artifact_sha256\":");
        match target.artifact_sha256() {
            Some(d) => quoted(&hex(d), &mut out),
            None => out.extend_from_slice(b"null"),
        }
        out.push(b'}');
    }
    out.extend_from_slice(b"],\"item_failure\":");
    match report.item_failure() {
        Some(v) => quoted(item_failure(v), &mut out),
        None => out.extend_from_slice(b"null"),
    }
    out.extend_from_slice(b",\"aggregate\":");
    quoted(aggregate(report.aggregate()), &mut out);
    out.push(b'}');
    out
}

fn target_status(v: &TargetStatus) -> &'static str {
    match v {
        TargetStatus::Completed { warning: None } => "ok",
        TargetStatus::Completed {
            warning: Some(WarningClass::Upload),
        } => "warn-upload",
        TargetStatus::Completed {
            warning: Some(WarningClass::Unknown),
        } => "warn-unknown",
        TargetStatus::Failed {
            class: FailureClass::Source,
        } => "fail-source",
        TargetStatus::Failed {
            class: FailureClass::Transcription,
        } => "fail-transcription",
        TargetStatus::Failed {
            class: FailureClass::Translation,
        } => "fail-translation",
        TargetStatus::Failed {
            class: FailureClass::Storage,
        } => "fail-storage",
        TargetStatus::Failed {
            class: FailureClass::Unknown,
        } => "fail-unknown",
        TargetStatus::MissingLanguage {
            case: MissingLanguage::ReportedButUnusable,
        } => "fail-language",
    }
}
fn item_failure(v: ItemFailure) -> &'static str {
    match v {
        ItemFailure::Source => "source",
        ItemFailure::Storage => "storage",
        ItemFailure::Unknown => "unknown",
    }
}
fn aggregate(v: AggregateDisposition) -> &'static str {
    match v {
        AggregateDisposition::Complete => "complete",
        AggregateDisposition::CompleteWithWarning => "complete-with-warning",
        AggregateDisposition::Partial => "partial",
        AggregateDisposition::Failed => "failed",
    }
}

pub(crate) fn report_hash(report: &EpisodeRunReport) -> [u8; 32] {
    domain_hash(b"asrsub-report-v1", &encode_report(report))
}

pub(crate) fn decode_report(bytes: &[u8]) -> Result<EpisodeRunReport, NotificationStateError> {
    let value: serde_json::Value =
        serde_json::from_slice(bytes).map_err(|_| NotificationStateError::Corrupt)?;
    let object = value.as_object().ok_or(NotificationStateError::Corrupt)?;
    let get = |key: &str| object.get(key).ok_or(NotificationStateError::Corrupt);
    if object.len() != 9 || get("schema")?.as_str() != Some("report-v1") {
        return Err(NotificationStateError::Corrupt);
    }
    let kind = match get("kind")?.as_str() {
        Some("series") => EpisodeKind::Series,
        Some("movie") => EpisodeKind::Movie,
        _ => return Err(NotificationStateError::Corrupt),
    };
    let id = get("episode_id")?
        .as_i64()
        .ok_or(NotificationStateError::Corrupt)?;
    let title_raw = get("title")?
        .as_str()
        .ok_or(NotificationStateError::Corrupt)?;
    let title = super::discord_text::SafeDisplayText::sanitize(title_raw)
        .map_err(|_| NotificationStateError::Corrupt)?;
    if title.as_str() != title_raw {
        return Err(NotificationStateError::Corrupt);
    }
    let season = get("season")?
        .as_u64()
        .map(|v| u32::try_from(v).map_err(|_| NotificationStateError::Corrupt))
        .transpose()?;
    let episode = get("episode")?
        .as_u64()
        .map(|v| u32::try_from(v).map_err(|_| NotificationStateError::Corrupt))
        .transpose()?;
    let mut targets = Vec::new();
    for target in get("targets")?
        .as_array()
        .ok_or(NotificationStateError::Corrupt)?
    {
        let o = target.as_object().ok_or(NotificationStateError::Corrupt)?;
        let lang = super::discord_types::TargetLanguage::parse(
            o.get("language")
                .and_then(|v| v.as_str())
                .ok_or(NotificationStateError::Corrupt)?,
        )
        .map_err(|_| NotificationStateError::Corrupt)?;
        let status_raw = o
            .get("status")
            .and_then(|v| v.as_str())
            .ok_or(NotificationStateError::Corrupt)?;
        let status = match status_raw {
            "ok" => TargetStatus::Completed { warning: None },
            "warn-upload" => TargetStatus::Completed {
                warning: Some(WarningClass::Upload),
            },
            "warn-unknown" => TargetStatus::Completed {
                warning: Some(WarningClass::Unknown),
            },
            "fail-source" => TargetStatus::Failed {
                class: FailureClass::Source,
            },
            "fail-transcription" => TargetStatus::Failed {
                class: FailureClass::Transcription,
            },
            "fail-translation" => TargetStatus::Failed {
                class: FailureClass::Translation,
            },
            "fail-storage" => TargetStatus::Failed {
                class: FailureClass::Storage,
            },
            "fail-unknown" => TargetStatus::Failed {
                class: FailureClass::Unknown,
            },
            "fail-language" => TargetStatus::MissingLanguage {
                case: MissingLanguage::ReportedButUnusable,
            },
            _ => return Err(NotificationStateError::Corrupt),
        };
        let digest = match o
            .get("artifact_sha256")
            .ok_or(NotificationStateError::Corrupt)?
        {
            serde_json::Value::Null => None,
            serde_json::Value::String(s) => {
                if s.len() != 64
                    || !s
                        .bytes()
                        .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
                {
                    return Err(NotificationStateError::Corrupt);
                }
                let mut d = [0; 32];
                for (i, p) in s.as_bytes().chunks_exact(2).enumerate() {
                    d[i] = u8::from_str_radix(std::str::from_utf8(p).unwrap(), 16)
                        .map_err(|_| NotificationStateError::Corrupt)?;
                }
                Some(d)
            }
            _ => return Err(NotificationStateError::Corrupt),
        };
        targets.push(
            super::discord_types::TargetRunResult::try_new(lang, status, digest)
                .map_err(|_| NotificationStateError::Corrupt)?,
        );
    }
    let item_failure = match get("item_failure")?.as_str() {
        None => None,
        Some("source") => Some(ItemFailure::Source),
        Some("storage") => Some(ItemFailure::Storage),
        Some("unknown") => Some(ItemFailure::Unknown),
        _ => return Err(NotificationStateError::Corrupt),
    };
    let aggregate = match get("aggregate")?.as_str() {
        Some("complete") => AggregateDisposition::Complete,
        Some("complete-with-warning") => AggregateDisposition::CompleteWithWarning,
        Some("partial") => AggregateDisposition::Partial,
        Some("failed") => AggregateDisposition::Failed,
        _ => return Err(NotificationStateError::Corrupt),
    };
    let report = EpisodeRunReport::try_new(
        kind,
        id,
        title,
        season,
        episode,
        super::discord_types::BoundedTargets::try_from(targets)
            .map_err(|_| NotificationStateError::Corrupt)?,
        item_failure,
        aggregate,
    )
    .map_err(|_| NotificationStateError::Corrupt)?;
    if encode_report(&report) != bytes {
        return Err(NotificationStateError::Contradiction);
    }
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn canonical_escapes_controls_without_named_shortcuts() {
        assert_eq!(
            encode_string("a\n\t\"\\\u{2028}"),
            b"\"a\\u000a\\u0009\\\"\\\\\\u2028\""
        );
    }
}
