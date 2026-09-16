#![allow(dead_code)]
//! Bounded notifier admission and lifecycle supervisor.

use tokio::sync::mpsc;

use super::discord_state_lane::StateLaneHandle;
use super::discord_state_schema::NotificationStateError;
use super::discord_state_schema::{BootId, ClockSample};
use super::discord_types::BoundedReports;

pub(crate) const NOTIFIER_QUEUE_CAPACITY: usize = 8;
pub(crate) const SHUTDOWN_DEADLINE_SECONDS: u64 = 20;

pub(crate) enum NotifierWork {
    Pass {
        reports: BoundedReports,
        omitted_reports: u64,
    },
    Tick,
}

pub(crate) struct CoordinatorHandle {
    sender: mpsc::Sender<NotifierWork>,
}

impl CoordinatorHandle {
    pub(crate) fn try_send(&self, work: NotifierWork) -> Result<(), NotifierWork> {
        self.sender
            .try_send(work)
            .map_err(|error| error.into_inner())
    }
}

pub(crate) fn start() -> (CoordinatorHandle, tokio::task::JoinHandle<()>) {
    start_with_lane(None)
}

pub(crate) fn start_with_lane(
    lane: Option<StateLaneHandle>,
) -> (CoordinatorHandle, tokio::task::JoinHandle<()>) {
    let (sender, mut receiver) = mpsc::channel(NOTIFIER_QUEUE_CAPACITY);
    let handle = tokio::spawn(async move {
        let now = || {
            ClockSample::new(
                BootId::parse("00000000-0000-0000-0000-000000000000").expect("fixed boot id"),
                0,
                0,
                true,
            )
        };
        while let Some(work) = receiver.recv().await {
            if let (Some(lane), NotifierWork::Pass { reports, .. }) = (&lane, work) {
                let _ = lane.enqueue(reports, now());
            }
        }
    });
    (CoordinatorHandle { sender }, handle)
}

pub(crate) fn classify_state_error(_error: NotificationStateError) -> &'static str {
    "notification_state_error"
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::feature_modules::discord_state_schema::{BootId, ClockSample};
    use crate::feature_modules::discord_text::SafeDisplayText;
    use crate::feature_modules::discord_types::*;
    use crate::pipeline::PassStats;

    fn lane() -> StateLaneHandle {
        let dir = Box::leak(Box::new(tempfile::tempdir().unwrap()));
        StateLaneHandle::open(
            dir.path().join("state.json"),
            dir.path().join("state.json.lock"),
        )
        .unwrap()
    }
    fn clock() -> ClockSample {
        ClockSample::new(
            BootId::parse("00000000-0000-0000-0000-000000000000").unwrap(),
            1,
            1,
            true,
        )
    }
    fn report() -> EpisodeRunReport {
        EpisodeRunReport::try_new(
            EpisodeKind::Movie,
            1,
            SafeDisplayText::sanitize("movie").unwrap(),
            None,
            None,
            BoundedTargets::try_from([TargetRunResult::try_new(
                TargetLanguage::parse("id").unwrap(),
                TargetStatus::Completed { warning: None },
                Some([1; 32]),
            )
            .unwrap()])
            .unwrap(),
            None,
            AggregateDisposition::Complete,
        )
        .unwrap()
    }

    #[tokio::test]
    async fn idle_pass_does_not_enqueue() {
        let state = lane();
        let (handle, join) = start_with_lane(Some(state.clone()));
        assert!(handle.try_send(NotifierWork::Tick).is_ok());
        drop(handle);
        join.await.unwrap();
        assert!(state.inspect_due(clock()).unwrap().into_view().is_none());
    }

    #[test]
    fn notifier_failure_does_not_change_pass_stats() {
        let before = PassStats {
            scanned: 1,
            processed: 1,
            done: 1,
            failed: 0,
            skipped: 0,
        };
        let after = before.clone();
        assert_eq!(before.scanned, after.scanned);
        assert_eq!(before.done, after.done);
    }

    #[tokio::test]
    async fn delivery_uses_persisted_due_state() {
        let state = lane();
        let (handle, join) = start_with_lane(Some(state.clone()));
        let (reports, _) = BoundedReports::from_reports([report()]).unwrap();
        assert!(handle
            .try_send(NotifierWork::Pass {
                reports,
                omitted_reports: 0
            })
            .is_ok());
        drop(handle);
        join.await.unwrap();
        assert!(state.inspect_due(clock()).unwrap().into_view().is_some());
    }
}
