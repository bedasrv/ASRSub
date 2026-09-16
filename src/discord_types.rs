use sha2::{Digest, Sha256};

use super::discord_state_codec;
use super::discord_text::{SafeDisplayText, TextError};

pub(crate) const MAX_TARGETS_PER_REPORT: usize = 32;
pub(crate) const MAX_PASS_REPORTS: usize = 128;
pub(crate) const MAX_PASS_REPORT_BYTES: usize = 2 * 1024 * 1024;
pub(crate) const MAX_REPORT_BYTES: usize = 16 * 1024;

#[derive(Clone, Copy, Eq, Ord, PartialEq, PartialOrd, Debug)]
pub(crate) enum EpisodeKind {
    Series,
    Movie,
}

#[derive(Clone, Copy, Eq, Ord, PartialEq, PartialOrd, Debug)]
pub(crate) enum ItemFailure {
    Source,
    Storage,
    Unknown,
}

#[derive(Clone, Copy, Eq, Ord, PartialEq, PartialOrd, Debug)]
pub(crate) enum MissingLanguage {
    ReportedButUnusable,
}

#[derive(Clone, Copy, Eq, Ord, PartialEq, PartialOrd, Debug)]
pub(crate) enum NoTargetCase {
    Paused,
    NoConfiguredTargets,
    NoMissingTargets,
    AllTargetsAlreadyPresent,
}

#[derive(Clone, Copy, Eq, Ord, PartialEq, PartialOrd, Debug)]
pub(crate) enum WarningClass {
    Upload,
    Unknown,
}

#[derive(Clone, Copy, Eq, Ord, PartialEq, PartialOrd, Debug)]
pub(crate) enum FailureClass {
    Source,
    Transcription,
    Translation,
    Storage,
    Unknown,
}

#[derive(Clone, Copy, Eq, Ord, PartialEq, PartialOrd, Debug)]
pub(crate) enum TargetStatus {
    Completed { warning: Option<WarningClass> },
    Failed { class: FailureClass },
    MissingLanguage { case: MissingLanguage },
}

#[derive(Clone, Copy, Eq, Ord, PartialEq, PartialOrd, Debug)]
pub(crate) enum AggregateDisposition {
    Complete,
    CompleteWithWarning,
    Partial,
    Failed,
}

#[derive(Clone, Eq, PartialEq, Ord, PartialOrd)]
pub(crate) struct TargetLanguage(String);

impl TargetLanguage {
    pub(crate) fn parse(raw: &str) -> Result<Self, ReportConstructionError> {
        let value = raw.to_ascii_lowercase();
        if value.is_empty()
            || value.len() > 32
            || !value
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
        {
            return Err(ReportConstructionError::InvalidLanguage);
        }
        Ok(Self(value))
    }

    pub(crate) fn as_str(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for TargetLanguage {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_tuple("TargetLanguage").field(&self.0).finish()
    }
}

#[derive(Clone, Eq, PartialEq, Debug)]
pub(crate) struct TargetRunResult {
    language: TargetLanguage,
    status: TargetStatus,
    artifact_sha256: Option<[u8; 32]>,
}

impl TargetRunResult {
    pub(crate) fn try_new(
        language: TargetLanguage,
        status: TargetStatus,
        artifact_sha256: Option<[u8; 32]>,
    ) -> Result<Self, ReportConstructionError> {
        let needs_digest = matches!(status, TargetStatus::Completed { .. });
        if needs_digest != artifact_sha256.is_some() {
            return Err(ReportConstructionError::InvalidArtifactDigest);
        }
        Ok(Self {
            language,
            status,
            artifact_sha256,
        })
    }

    pub(crate) fn language(&self) -> &TargetLanguage {
        &self.language
    }
    pub(crate) fn status(&self) -> &TargetStatus {
        &self.status
    }
    pub(crate) fn artifact_sha256(&self) -> Option<&[u8; 32]> {
        self.artifact_sha256.as_ref()
    }
    pub(crate) fn severity(&self) -> u8 {
        match self.status {
            TargetStatus::Failed { .. } | TargetStatus::MissingLanguage { .. } => 0,
            TargetStatus::Completed { warning: Some(_) } => 1,
            TargetStatus::Completed { warning: None } => 2,
        }
    }
}

#[derive(Clone, Eq, PartialEq, Debug)]
pub(crate) struct BoundedTargets {
    items: Box<[TargetRunResult]>,
}

impl BoundedTargets {
    pub(crate) fn try_from<I: IntoIterator<Item = TargetRunResult>>(
        items: I,
    ) -> Result<Self, ReportConstructionError> {
        let mut values: Vec<_> = items.into_iter().collect();
        if values.is_empty() || values.len() > MAX_TARGETS_PER_REPORT {
            return Err(ReportConstructionError::TooManyTargets);
        }
        values.sort_by(|a, b| {
            (a.severity(), a.language.as_str()).cmp(&(b.severity(), b.language.as_str()))
        });
        if values
            .windows(2)
            .any(|pair| pair[0].language == pair[1].language)
        {
            return Err(ReportConstructionError::InvalidLanguage);
        }
        Ok(Self {
            items: values.into_boxed_slice(),
        })
    }

    pub(crate) fn as_slice(&self) -> &[TargetRunResult] {
        &self.items
    }
}

#[derive(Clone, Eq, PartialEq, Debug)]
pub(crate) struct PipelineCommitId(String);

impl PipelineCommitId {
    pub(crate) fn from_canonical_report_bytes(bytes: &[u8]) -> Self {
        let mut h = Sha256::new();
        h.update(b"asrsub-pipeline-v1\0");
        h.update(bytes);
        let digest = h.finalize();
        Self(format!("asrsub-pipeline-v1-{}", hex_lower(&digest)))
    }

    pub(crate) fn as_str(&self) -> &str {
        &self.0
    }
    pub(crate) fn as_bytes(&self) -> &[u8] {
        self.0.as_bytes()
    }
}

fn hex_lower(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for &b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 15) as usize] as char);
    }
    out
}

#[derive(Clone, Eq, PartialEq, Debug)]
pub(crate) struct EpisodeRunReport {
    kind: EpisodeKind,
    episode_id: i64,
    title: SafeDisplayText,
    season: Option<u32>,
    episode: Option<u32>,
    targets: BoundedTargets,
    item_failure: Option<ItemFailure>,
    aggregate: AggregateDisposition,
    pipeline_commit_id: Option<PipelineCommitId>,
}

impl EpisodeRunReport {
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn try_new(
        kind: EpisodeKind,
        episode_id: i64,
        title: SafeDisplayText,
        season: Option<u32>,
        episode: Option<u32>,
        targets: BoundedTargets,
        item_failure: Option<ItemFailure>,
        aggregate: AggregateDisposition,
    ) -> Result<Self, ReportConstructionError> {
        let completed = targets
            .as_slice()
            .iter()
            .filter(|t| matches!(t.status, TargetStatus::Completed { .. }))
            .count();
        let failed = targets.as_slice().len() - completed;
        let warning = targets
            .as_slice()
            .iter()
            .any(|t| matches!(t.status, TargetStatus::Completed { warning: Some(_) }));
        let expected = if completed == targets.as_slice().len() && item_failure.is_none() {
            if warning {
                AggregateDisposition::CompleteWithWarning
            } else {
                AggregateDisposition::Complete
            }
        } else if completed > 0 {
            AggregateDisposition::Partial
        } else {
            AggregateDisposition::Failed
        };
        if expected != aggregate || (failed == 0 && item_failure.is_some() && completed == 0) {
            return Err(ReportConstructionError::InvalidStatusCombination);
        }
        let mut report = Self {
            kind,
            episode_id,
            title,
            season,
            episode,
            targets,
            item_failure,
            aggregate,
            pipeline_commit_id: None,
        };
        let bytes = discord_state_codec::encode_report(&report);
        if bytes.len() > MAX_REPORT_BYTES {
            return Err(ReportConstructionError::CounterOverflow);
        }
        report.pipeline_commit_id = Some(PipelineCommitId::from_canonical_report_bytes(&bytes));
        Ok(report)
    }

    pub(crate) fn kind(&self) -> EpisodeKind {
        self.kind
    }
    pub(crate) fn episode_id(&self) -> i64 {
        self.episode_id
    }
    pub(crate) fn title(&self) -> &SafeDisplayText {
        &self.title
    }
    pub(crate) fn season(&self) -> Option<u32> {
        self.season
    }
    pub(crate) fn episode(&self) -> Option<u32> {
        self.episode
    }
    pub(crate) fn targets(&self) -> &BoundedTargets {
        &self.targets
    }
    pub(crate) fn item_failure(&self) -> Option<ItemFailure> {
        self.item_failure
    }
    pub(crate) fn aggregate(&self) -> AggregateDisposition {
        self.aggregate
    }
    pub(crate) fn pipeline_commit_id(&self) -> Option<&PipelineCommitId> {
        self.pipeline_commit_id.as_ref()
    }

    pub(crate) fn without_commit_id(mut self) -> Self {
        self.pipeline_commit_id = None;
        self
    }
}

#[derive(Clone, Eq, PartialEq, Debug)]
pub(crate) struct BoundedReports {
    items: Box<[EpisodeRunReport]>,
    canonical_bytes: u32,
}

impl BoundedReports {
    pub(crate) fn from_reports(
        reports: impl IntoIterator<Item = EpisodeRunReport>,
    ) -> Result<(Self, u64), ReportConstructionError> {
        let mut values: Vec<_> = reports.into_iter().collect();
        values.sort_by(report_order);
        let mut omitted = 0u64;
        let mut kept = Vec::with_capacity(values.len().min(MAX_PASS_REPORTS));
        let mut bytes = 0usize;
        for report in values {
            let size = discord_state_codec::encode_report(&report).len();
            if size > MAX_REPORT_BYTES
                || kept.len() == MAX_PASS_REPORTS
                || bytes
                    .checked_add(size)
                    .is_none_or(|n| n > MAX_PASS_REPORT_BYTES)
            {
                omitted = omitted
                    .checked_add(1)
                    .ok_or(ReportConstructionError::CounterOverflow)?;
                continue;
            }
            bytes += size;
            kept.push(report);
        }
        Ok((
            Self {
                items: kept.into_boxed_slice(),
                canonical_bytes: u32::try_from(bytes)
                    .map_err(|_| ReportConstructionError::CounterOverflow)?,
            },
            omitted,
        ))
    }

    pub(crate) fn len(&self) -> usize {
        self.items.len()
    }
    pub(crate) fn iter(&self) -> impl Iterator<Item = &EpisodeRunReport> {
        self.items.iter()
    }
    pub(crate) fn into_boxed_slice(self) -> Box<[EpisodeRunReport]> {
        self.items
    }
    pub(crate) fn canonical_bytes_len(&self) -> u32 {
        self.canonical_bytes
    }
}

fn report_order(a: &EpisodeRunReport, b: &EpisodeRunReport) -> std::cmp::Ordering {
    (
        if a.kind == EpisodeKind::Series { 0 } else { 1 },
        a.episode_id,
        a.season,
        a.episode,
        a.title.as_str(),
        discord_state_codec::encode_report(a),
    )
        .cmp(&(
            if b.kind == EpisodeKind::Series { 0 } else { 1 },
            b.episode_id,
            b.season,
            b.episode,
            b.title.as_str(),
            discord_state_codec::encode_report(b),
        ))
}

#[derive(Clone, Copy, Eq, PartialEq, Debug)]
pub(crate) enum ReportConstructionError {
    TooManyTargets,
    InvalidLanguage,
    InvalidArtifactDigest,
    InvalidStatusCombination,
    InvalidTitle,
    CounterOverflow,
}

impl From<TextError> for ReportConstructionError {
    fn from(_: TextError) -> Self {
        Self::InvalidTitle
    }
}

#[derive(Clone, Eq, PartialEq, Debug)]
pub(crate) enum EpisodeRunResult {
    Report(EpisodeRunReport),
    NoTarget(NoTargetCase),
}

#[cfg(test)]
mod tests {
    use super::super::discord_text::SafeDisplayText;
    use super::*;

    fn target(lang: &str, status: TargetStatus, digest: bool) -> TargetRunResult {
        TargetRunResult::try_new(
            TargetLanguage::parse(lang).unwrap(),
            status,
            digest.then_some([7; 32]),
        )
        .unwrap()
    }

    #[test]
    fn report_identity_is_stable() {
        let title = SafeDisplayText::sanitize("A title").unwrap();
        let t = target("id", TargetStatus::Completed { warning: None }, true);
        let r = EpisodeRunReport::try_new(
            EpisodeKind::Series,
            7,
            title,
            Some(1),
            Some(2),
            BoundedTargets::try_from([t]).unwrap(),
            None,
            AggregateDisposition::Complete,
        )
        .unwrap();
        assert!(r
            .pipeline_commit_id()
            .unwrap()
            .as_str()
            .starts_with("asrsub-pipeline-v1-"));
        assert_eq!(r.pipeline_commit_id().unwrap().as_str().len(), 82);
    }
}
