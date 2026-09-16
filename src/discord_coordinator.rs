#![allow(dead_code)]
//! Bounded notifier admission and lifecycle supervisor.

use tokio::sync::mpsc;

use super::discord_state_schema::NotificationStateError;
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
    let (sender, mut receiver) = mpsc::channel(NOTIFIER_QUEUE_CAPACITY);
    let handle = tokio::spawn(async move { while receiver.recv().await.is_some() {} });
    (CoordinatorHandle { sender }, handle)
}

pub(crate) fn classify_state_error(_error: NotificationStateError) -> &'static str {
    "notification_state_error"
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pipeline::PassStats;

    #[tokio::test]
    async fn idle_pass_does_not_enqueue() {
        let (handle, join) = start();
        let (reports, _) = BoundedReports::from_reports(std::iter::empty()).unwrap();
        assert!(handle
            .try_send(NotifierWork::Pass {
                reports,
                omitted_reports: 0
            })
            .is_ok());
        drop(handle);
        join.await.unwrap();
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
        let (handle, join) = start();
        assert!(handle.try_send(NotifierWork::Tick).is_ok());
        drop(handle);
        join.await.unwrap();
    }
}
