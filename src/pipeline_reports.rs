use crate::feature_modules::discord_text::SafeDisplayText;
use crate::feature_modules::discord_types::{
    AggregateDisposition, BoundedReports, BoundedTargets, EpisodeKind, EpisodeRunReport,
    FailureClass, ItemFailure, ReportConstructionError, TargetLanguage, TargetRunResult,
    TargetStatus,
};

use super::{Candidate, PassStats};

pub(crate) struct PassOutcome {
    stats: PassStats,
    reports: BoundedReports,
    omitted_reports: u64,
}

impl PassOutcome {
    pub(crate) fn new(stats: PassStats, reports: BoundedReports, omitted_reports: u64) -> Self {
        Self {
            stats,
            reports,
            omitted_reports,
        }
    }
    pub(crate) fn stats(&self) -> &PassStats {
        &self.stats
    }
    pub(crate) fn reports(&self) -> &BoundedReports {
        &self.reports
    }
    pub(crate) fn omitted_reports(&self) -> u64 {
        self.omitted_reports
    }
    pub(crate) fn into_parts(self) -> (PassStats, BoundedReports, u64) {
        (self.stats, self.reports, self.omitted_reports)
    }
}

pub(crate) fn reduce_target_outcomes(
    kind: EpisodeKind,
    episode_id: i64,
    title: SafeDisplayText,
    season: Option<u32>,
    episode: Option<u32>,
    targets: Vec<TargetRunResult>,
    item_failure: Option<ItemFailure>,
) -> Result<EpisodeRunReport, ReportConstructionError> {
    let targets = BoundedTargets::try_from(targets)?;
    let completed = targets
        .as_slice()
        .iter()
        .filter(|target| matches!(target.status(), TargetStatus::Completed { .. }))
        .count();
    let warning = targets.as_slice().iter().any(|target| {
        matches!(
            target.status(),
            TargetStatus::Completed { warning: Some(_) }
        )
    });
    let aggregate = if completed == targets.as_slice().len() && item_failure.is_none() {
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
    EpisodeRunReport::try_new(
        kind,
        episode_id,
        title,
        season,
        episode,
        targets,
        item_failure,
        aggregate,
    )
}

pub(super) fn bound_pass_reports(
    reports: Vec<EpisodeRunReport>,
) -> Result<(BoundedReports, u64), ReportConstructionError> {
    BoundedReports::from_reports(reports)
}

pub(super) fn failure_report_for_candidate(candidate: &Candidate) -> Option<EpisodeRunReport> {
    let kind = if candidate.is_movie {
        EpisodeKind::Movie
    } else {
        EpisodeKind::Series
    };
    let title = SafeDisplayText::sanitize(&candidate.series_title)
        .or_else(|_| SafeDisplayText::sanitize("?"))
        .ok()?;
    let targets = candidate
        .missing
        .iter()
        .map(|language| {
            TargetRunResult::try_new(
                TargetLanguage::parse(language).ok()?,
                TargetStatus::Failed {
                    class: FailureClass::Unknown,
                },
                None,
            )
            .ok()
        })
        .collect::<Option<Vec<_>>>()?;
    reduce_target_outcomes(
        kind,
        candidate.episode_id,
        title,
        None,
        None,
        targets,
        Some(ItemFailure::Unknown),
    )
    .ok()
}
