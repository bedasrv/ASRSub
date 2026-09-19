#![allow(dead_code)]
use sha2::{Digest, Sha256};

use super::discord_state_codec;
use super::discord_text::{SafeDisplayText, TextError};

pub(crate) const MAX_TARGETS_PER_REPORT: usize = 32;
pub(crate) const MAX_PASS_REPORTS: usize = 128;
pub(crate) const MAX_PASS_REPORT_BYTES: usize = 2 * 1024 * 1024;
pub(crate) const MAX_REPORT_BYTES: usize = 16 * 1024;
pub(crate) const MAX_GENERATION_MODELS: usize = 3;
pub(crate) const MAX_GENERATION_MODEL_SCALARS: usize = 48;
pub(crate) const MAX_GENERATION_METHOD_SCALARS: usize = 112;

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

#[derive(Clone, Copy, Eq, PartialEq, Debug)]
pub(crate) enum GenerationSource {
    ExistingSubtitle,
    Sidecar,
    Jimaku,
    Whisper,
}

#[derive(Clone, Eq, PartialEq, Debug)]
pub(crate) struct GenerationMethod {
    source: GenerationSource,
    whisper_models: Box<[String]>,
    llm_models: Box<[String]>,
}

impl GenerationMethod {
    pub(crate) fn new(
        source: GenerationSource,
        whisper_models: &[String],
        llm_models: &[String],
    ) -> Self {
        Self {
            source,
            whisper_models: bounded_model_names(whisper_models),
            llm_models: bounded_model_names(llm_models),
        }
    }

    pub(crate) fn existing_subtitle() -> Self {
        Self::new(GenerationSource::ExistingSubtitle, &[], &[])
    }

    pub(crate) fn display(&self) -> String {
        let llm = display_models("LLM", &self.llm_models);
        let mut rendered = match self.source {
            GenerationSource::ExistingSubtitle => "Existing subtitle".to_string(),
            GenerationSource::Sidecar => append_translation("Sidecar".to_string(), llm.as_deref()),
            GenerationSource::Jimaku => append_translation("Jimaku".to_string(), llm.as_deref()),
            GenerationSource::Whisper => {
                let source = display_models("Whisper", &self.whisper_models)
                    .unwrap_or_else(|| "Whisper".to_string());
                append_translation(source, llm.as_deref())
            }
        };
        if rendered.chars().count() > MAX_GENERATION_METHOD_SCALARS {
            rendered = rendered
                .chars()
                .take(MAX_GENERATION_METHOD_SCALARS)
                .collect();
        }
        rendered
    }

    pub(crate) fn source(&self) -> GenerationSource {
        self.source
    }

    pub(crate) fn whisper_models(&self) -> &[String] {
        &self.whisper_models
    }

    pub(crate) fn llm_models(&self) -> &[String] {
        &self.llm_models
    }
}

fn append_translation(source: String, llm: Option<&str>) -> String {
    match llm {
        Some(llm) => format!("{source} → {llm}"),
        None => source,
    }
}

fn display_models(label: &str, models: &[String]) -> Option<String> {
    (!models.is_empty()).then(|| format!("{label}: {}", models.join(", ")))
}

fn bounded_model_names(raw: &[String]) -> Box<[String]> {
    let mut names: Vec<String> = raw
        .iter()
        .filter_map(|model| sanitize_model(model))
        .collect();
    names.sort();
    names.dedup();
    names.truncate(MAX_GENERATION_MODELS);
    names.into_boxed_slice()
}

fn sanitize_model(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    let lowered = trimmed.to_ascii_lowercase();
    if trimmed.is_empty()
        || lowered.contains("://")
        || lowered.starts_with("http")
        || lowered.contains("api_key")
        || lowered.contains("key_env")
    {
        return None;
    }
    let mut clean = String::new();
    for c in trimmed.chars() {
        if c.is_ascii_alphanumeric() || matches!(c, '/' | ':' | '.' | '-' | '_') {
            clean.push(c);
        }
    }
    if clean.is_empty() {
        return None;
    }
    Some(clean.chars().take(MAX_GENERATION_MODEL_SCALARS).collect())
}

#[derive(Clone, Eq, PartialEq, Debug)]
pub(crate) struct TargetRunResult {
    language: TargetLanguage,
    status: TargetStatus,
    artifact_sha256: Option<[u8; 32]>,
    // Notification-only provenance. It is intentionally omitted from the
    // canonical report codec and absent after state decode.
    generation_method: Option<GenerationMethod>,
}

impl TargetRunResult {
    pub(crate) fn try_new(
        language: TargetLanguage,
        status: TargetStatus,
        artifact_sha256: Option<[u8; 32]>,
    ) -> Result<Self, ReportConstructionError> {
        Self::try_new_with_method(language, status, artifact_sha256, None)
    }

    pub(crate) fn try_new_with_method(
        language: TargetLanguage,
        status: TargetStatus,
        artifact_sha256: Option<[u8; 32]>,
        generation_method: Option<GenerationMethod>,
    ) -> Result<Self, ReportConstructionError> {
        let needs_digest = matches!(status, TargetStatus::Completed { .. });
        if needs_digest != artifact_sha256.is_some() {
            return Err(ReportConstructionError::InvalidArtifactDigest);
        }
        Ok(Self {
            language,
            status,
            artifact_sha256,
            generation_method,
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
    pub(crate) fn generation_method(&self) -> Option<&GenerationMethod> {
        self.generation_method.as_ref()
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
        assert_eq!(r.pipeline_commit_id().unwrap().as_str().len(), 83);
    }

    #[test]
    fn notification_provenance_is_omitted_from_canonical_report_bytes() {
        let method = GenerationMethod::new(
            GenerationSource::Whisper,
            &["provider/whisper".to_string()],
            &["provider/llm".to_string()],
        );
        let decorated = EpisodeRunReport::try_new(
            EpisodeKind::Movie,
            8,
            SafeDisplayText::sanitize("movie").unwrap(),
            None,
            None,
            BoundedTargets::try_from([TargetRunResult::try_new_with_method(
                TargetLanguage::parse("id").unwrap(),
                TargetStatus::Completed { warning: None },
                Some([8; 32]),
                Some(method),
            )
            .unwrap()])
            .unwrap(),
            None,
            AggregateDisposition::Complete,
        )
        .unwrap();
        let plain = EpisodeRunReport::try_new(
            EpisodeKind::Movie,
            8,
            SafeDisplayText::sanitize("movie").unwrap(),
            None,
            None,
            BoundedTargets::try_from([TargetRunResult::try_new(
                TargetLanguage::parse("id").unwrap(),
                TargetStatus::Completed { warning: None },
                Some([8; 32]),
            )
            .unwrap()])
            .unwrap(),
            None,
            AggregateDisposition::Complete,
        )
        .unwrap();
        let bytes = discord_state_codec::encode_report(&decorated);
        assert!(!String::from_utf8_lossy(&bytes).contains("generation_method"));
        assert_eq!(
            decorated.pipeline_commit_id().unwrap(),
            plain.pipeline_commit_id().unwrap()
        );
        let decoded = discord_state_codec::decode_report(&bytes).unwrap();
        assert!(decoded.targets().as_slice()[0]
            .generation_method()
            .is_none());
    }
}
