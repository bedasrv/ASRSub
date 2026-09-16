//! Pure bounded outbox helpers.

use super::discord_types::EpisodeRunReport;

pub(crate) fn coalesce(reports: &mut Vec<EpisodeRunReport>, report: EpisodeRunReport) {
    if let Some(existing) = reports
        .iter()
        .position(|item| item.pipeline_commit_id() == report.pipeline_commit_id())
    {
        reports[existing] = report;
    } else {
        reports.push(report);
    }
}
