#![allow(dead_code)]
//! Bounded notifier admission and lifecycle supervisor.

use std::sync::Arc;
use tokio::sync::mpsc;

use super::discord_renderer;
use super::discord_state_lane::StateLaneHandle;
use super::discord_state_schema::NotificationStateError;
use super::discord_state_schema::{BootId, ClockSample};
use super::discord_transport::{DeliveryResult, DeliveryTransport};
use super::discord_types::BoundedReports;

pub(crate) const NOTIFIER_QUEUE_CAPACITY: usize = 8;

#[derive(Debug)]
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

pub(crate) fn start_with_lane(
    lane: Option<StateLaneHandle>,
) -> (CoordinatorHandle, tokio::task::JoinHandle<()>) {
    let (sender, mut receiver) = mpsc::channel(NOTIFIER_QUEUE_CAPACITY);
    let handle = tokio::spawn(async move {
        while let Some(work) = receiver.recv().await {
            let now = sample_clock();
            if let Some(lane) = &lane {
                let _ = lane.retry_blocked(now.clone());
                if let NotifierWork::Pass { reports, .. } = work {
                    let _ = lane.enqueue(reports, now);
                }
            }
        }
    });
    (CoordinatorHandle { sender }, handle)
}

pub(crate) fn start_with_dependencies(
    lane: StateLaneHandle,
    transport: Arc<dyn DeliveryTransport>,
) -> (CoordinatorHandle, tokio::task::JoinHandle<()>) {
    let (sender, mut receiver) = mpsc::channel(NOTIFIER_QUEUE_CAPACITY);
    let handle = tokio::spawn(async move {
        while let Some(work) = receiver.recv().await {
            let now = sample_clock();
            let _ = lane.retry_blocked(now.clone());
            match work {
                NotifierWork::Pass { reports, .. } => {
                    let _ = lane.enqueue(reports, now.clone());
                    let _ = deliver_once(&lane, transport.as_ref(), now).await;
                }
                NotifierWork::Tick => {
                    let _ = deliver_once(&lane, transport.as_ref(), now).await;
                }
            }
        }
    });
    (CoordinatorHandle { sender }, handle)
}

fn sample_clock() -> ClockSample {
    let boot = std::fs::read_to_string("/proc/sys/kernel/random/boot_id")
        .ok()
        .and_then(|v| BootId::parse(v.trim()).ok())
        .unwrap_or_else(|| {
            BootId::parse("00000000-0000-0000-0000-000000000000").expect("fixed boot id")
        });
    let epoch = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_nanos() as u64)
        .unwrap_or(0);
    static START: std::sync::OnceLock<std::time::Instant> = std::sync::OnceLock::new();
    let monotonic = START
        .get_or_init(std::time::Instant::now)
        .elapsed()
        .as_nanos() as u64;
    ClockSample::new(boot, epoch, monotonic, true)
}

async fn deliver_once(
    lane: &StateLaneHandle,
    transport: &dyn DeliveryTransport,
    now: ClockSample,
) -> Result<(), NotificationStateError> {
    if let Some(reservation) = lane.resume_reservation(now.clone())? {
        let reservation_id = reservation.reservation_id();
        let payload = reservation.payload();
        return finish_transport(lane, transport, reservation_id, payload, now).await;
    }
    let Some(view) = lane.inspect_due(now.clone())?.into_view() else {
        return Ok(());
    };
    let payload = discord_renderer::render(&view).map_err(|_| NotificationStateError::Capacity)?;
    let wire_payload = payload.clone();
    let reserved = lane.reserve_rendered(now.clone(), view, payload)?;
    finish_transport(
        lane,
        transport,
        reserved.reservation_id(),
        wire_payload,
        now,
    )
    .await
}

async fn finish_transport(
    lane: &StateLaneHandle,
    transport: &dyn DeliveryTransport,
    reservation_id: [u8; 32],
    payload: super::discord_state_schema::PayloadBytes,
    now: ClockSample,
) -> Result<(), NotificationStateError> {
    match transport.send(&payload).await {
        DeliveryResult::Accepted => {
            lane.acknowledge(reservation_id, payload.sha256())?;
        }
        DeliveryResult::Failed(class) => {
            lane.record_attempt_failure(reservation_id, class, now, None)?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::feature_modules::discord_state_schema::{BootId, ClockSample, SafeDeliveryError};
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
    fn far_future_clock() -> ClockSample {
        ClockSample::new(
            BootId::parse("00000000-0000-0000-0000-000000000000").unwrap(),
            u64::MAX,
            u64::MAX,
            true,
        )
    }
    fn report() -> EpisodeRunReport {
        report_for_id(1)
    }
    fn report_for_id(episode_id: i64) -> EpisodeRunReport {
        EpisodeRunReport::try_new(
            EpisodeKind::Movie,
            episode_id,
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

    fn acknowledge_due(state: &StateLaneHandle) {
        let view = state.inspect_due(clock()).unwrap().into_view().unwrap();
        let payload = super::super::discord_state_schema::PayloadBytes::try_from_bytes(Box::from(
            &b"payload"[..],
        ))
        .unwrap();
        let reserved = state.reserve_rendered(clock(), view, payload).unwrap();
        state
            .acknowledge(reserved.reservation_id(), reserved.payload_sha256())
            .unwrap();
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

    #[tokio::test]
    async fn idle_tick_delivers_persisted_due_state() {
        let state = lane();
        let (reports, _) = BoundedReports::from_reports([report()]).unwrap();
        state.enqueue(reports, clock()).unwrap();
        let transport = Arc::new(super::super::discord_transport::FakeTransport::scripted(
            vec![DeliveryResult::Accepted],
        ));
        let (handle, join) = start_with_dependencies(state.clone(), transport.clone());
        assert!(handle.try_send(NotifierWork::Tick).is_ok());
        drop(handle);
        join.await.unwrap();
        assert!(state.inspect_due(clock()).unwrap().into_view().is_none());
        assert_eq!(transport.payload_count(), 1);
    }

    #[tokio::test]
    async fn retries_blocked_before_admitting_later_pass() {
        let state = lane();
        let (bulk, _) = BoundedReports::from_reports((0..128).map(report_for_id)).unwrap();
        state.enqueue(bulk, clock()).unwrap();
        let (blocked, _) = BoundedReports::from_reports([report_for_id(1000)]).unwrap();
        state.enqueue(blocked, clock()).unwrap();
        acknowledge_due(&state);

        let transport = Arc::new(super::super::discord_transport::FakeTransport::scripted(
            vec![DeliveryResult::Accepted],
        ));
        let (handle, join) = start_with_dependencies(state.clone(), transport.clone());
        let (later, _) = BoundedReports::from_reports((1..=128).map(report_for_id)).unwrap();
        handle
            .try_send(NotifierWork::Pass {
                reports: later,
                omitted_reports: 0,
            })
            .unwrap();
        drop(handle);
        join.await.unwrap();

        assert_eq!(transport.payload_count(), 1);
        let now = far_future_clock();
        assert_eq!(state.retry_blocked(now.clone()).unwrap().promoted(), 1);
        let view = state.inspect_due(now).unwrap().into_view().unwrap();
        assert_eq!(view.reports().iter().next().unwrap().episode_id(), 128);
    }

    #[tokio::test]
    async fn first_meaningful_delivery_acknowledges_204() {
        let state = lane();
        let transport = Arc::new(super::super::discord_transport::FakeTransport::scripted(
            vec![DeliveryResult::Accepted],
        ));
        let (handle, join) = start_with_dependencies(state.clone(), transport.clone());
        let (reports, _) = BoundedReports::from_reports([report()]).unwrap();
        handle
            .try_send(NotifierWork::Pass {
                reports,
                omitted_reports: 0,
            })
            .unwrap();
        drop(handle);
        join.await.unwrap();
        assert!(state.inspect_due(clock()).unwrap().into_view().is_none());
        assert_eq!(transport.payload_count(), 1);
    }

    #[tokio::test]
    async fn retryable_failure_retains_due_floor() {
        let state = lane();
        let transport = Arc::new(super::super::discord_transport::FakeTransport::scripted(
            vec![DeliveryResult::Failed(SafeDeliveryError::RetryableResponse)],
        ));
        let (handle, join) = start_with_dependencies(state.clone(), transport);
        let (reports, _) = BoundedReports::from_reports([report()]).unwrap();
        handle
            .try_send(NotifierWork::Pass {
                reports,
                omitted_reports: 0,
            })
            .unwrap();
        drop(handle);
        join.await.unwrap();
        assert!(state.inspect_due(clock()).unwrap().into_view().is_none());
    }

    #[tokio::test]
    async fn permanent_failure_disables_delivery() {
        let state = lane();
        let transport = Arc::new(super::super::discord_transport::FakeTransport::scripted(
            vec![DeliveryResult::Failed(SafeDeliveryError::PermanentResponse)],
        ));
        let (handle, join) = start_with_dependencies(state.clone(), transport);
        let (reports, _) = BoundedReports::from_reports([report()]).unwrap();
        handle
            .try_send(NotifierWork::Pass {
                reports,
                omitted_reports: 0,
            })
            .unwrap();
        drop(handle);
        join.await.unwrap();
        assert!(matches!(
            state.inspect_due(clock()),
            Err(NotificationStateError::Disabled)
        ));
    }

    #[tokio::test]
    async fn duplicate_crash_recovery_resumes_persisted_payload() {
        let state = lane();
        let (reports, _) = BoundedReports::from_reports([report()]).unwrap();
        state.enqueue(reports, clock()).unwrap();
        let view = state.inspect_due(clock()).unwrap().into_view().unwrap();
        let payload = super::super::discord_state_schema::PayloadBytes::try_from_bytes(Box::from(
            &b"payload"[..],
        ))
        .unwrap();
        state.reserve_rendered(clock(), view, payload).unwrap();
        let transport = Arc::new(super::super::discord_transport::FakeTransport::scripted(
            vec![DeliveryResult::Accepted],
        ));
        let (handle, join) = start_with_dependencies(state.clone(), transport.clone());
        let (empty, _) = BoundedReports::from_reports(std::iter::empty()).unwrap();
        handle
            .try_send(NotifierWork::Pass {
                reports: empty,
                omitted_reports: 0,
            })
            .unwrap();
        drop(handle);
        join.await.unwrap();
        assert!(state.inspect_due(clock()).unwrap().into_view().is_none());
        assert_eq!(transport.payload_count(), 1);
    }
}
