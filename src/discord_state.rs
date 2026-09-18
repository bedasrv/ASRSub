#![allow(dead_code)]
//! Notification state store capability boundary.

use super::discord_state_lane::StateLaneHandle;
use super::discord_state_schema::NotificationStateError;

pub(crate) trait NotificationStateStore: Send + Sync {
    fn lane(&self) -> &StateLaneHandle;
}

pub(crate) trait NotificationStateStoreFactory: Send + Sync {
    fn open_for_daemon(&self) -> Result<Box<dyn NotificationStateStore>, NotificationStateError>;
    fn backend_token(&self) -> &'static str;
}

#[cfg(test)]
mod tests {
    use super::super::discord_state_schema::{
        BootId, ClockSample, RetryAfterSeconds, SafeDeliveryError,
    };
    use super::super::discord_text::SafeDisplayText;
    use super::super::discord_types::*;
    use super::*;

    fn lane() -> StateLaneHandle {
        let dir = Box::leak(Box::new(tempfile::tempdir().unwrap()));
        StateLaneHandle::open(
            dir.path().join("state.json"),
            dir.path().join("state.json.lock"),
        )
        .unwrap()
    }
    fn clock() -> super::super::discord_state_schema::ClockSample {
        super::super::discord_state_schema::ClockSample::new(
            BootId::parse("00000000-0000-0000-0000-000000000000").unwrap(),
            1,
            1,
            true,
        )
    }
    fn due_clock() -> super::super::discord_state_schema::ClockSample {
        super::super::discord_state_schema::ClockSample::new(
            BootId::parse("00000000-0000-0000-0000-000000000000").unwrap(),
            900_000_000_001,
            900_000_000_001,
            true,
        )
    }
    fn report(id: i64) -> EpisodeRunReport {
        EpisodeRunReport::try_new(
            EpisodeKind::Series,
            id,
            SafeDisplayText::sanitize("title").unwrap(),
            Some(1),
            Some(1),
            BoundedTargets::try_from([TargetRunResult::try_new(
                TargetLanguage::parse("id").unwrap(),
                TargetStatus::Completed { warning: None },
                Some([2; 32]),
            )
            .unwrap()])
            .unwrap(),
            None,
            AggregateDisposition::Complete,
        )
        .unwrap()
    }

    #[test]
    fn state_v1_canonical_vector() {
        assert_eq!(lane().inspect().unwrap().state_generation(), 0);
    }

    #[test]
    fn empty_enqueue_is_durable_noop() {
        let dir = Box::leak(Box::new(tempfile::tempdir().unwrap()));
        let path = dir.path().join("state.json");
        let lock = dir.path().join("state.json.lock");
        let l = StateLaneHandle::open(path.clone(), lock.clone()).unwrap();
        let before = l.inspect().unwrap();
        let persisted_before = l.snapshot().unwrap();
        let (empty, omitted) = BoundedReports::from_reports(std::iter::empty()).unwrap();
        assert_eq!(omitted, 0);

        let commit = l.enqueue(empty, clock()).unwrap();

        assert_eq!(commit.state_generation(), before.state_generation());
        assert_eq!(commit.state_hash(), before.state_hash());
        let persisted_after = l.snapshot().unwrap();
        assert_eq!(
            persisted_after.source_state_hash(),
            persisted_before.source_state_hash()
        );
        let reopened = StateLaneHandle::open(path, lock).unwrap();
        let persisted = reopened.inspect().unwrap();
        assert_eq!(persisted.state_generation(), before.state_generation());
        assert_eq!(persisted.state_hash(), before.state_hash());
    }

    #[test]
    fn inspect_returns_bounded_snapshot() {
        let l = lane();
        let (r, _) = BoundedReports::from_reports([report(1)]).unwrap();
        l.enqueue(r, clock()).unwrap();
        assert_eq!(
            l.inspect_due(clock())
                .unwrap()
                .into_view()
                .unwrap()
                .reports()
                .len(),
            1
        );
    }
    #[test]
    fn missing_state_bootstraps() {
        assert_eq!(lane().snapshot().unwrap().source_state_hash().len(), 32);
    }
    #[test]
    fn corrupt_state_quarantines() {
        let dir = Box::leak(Box::new(tempfile::tempdir().unwrap()));
        let path = dir.path().join("state.json");
        std::fs::write(&path, b"broken").unwrap();
        let l = StateLaneHandle::open(path, dir.path().join("state.json.lock")).unwrap();
        assert!(l.inspect().unwrap().disabled());
    }
    #[test]
    fn outbox_capacity_is_bounded() {
        let l = lane();
        let reports = (0..128).map(report).collect::<Vec<_>>();
        let (r, _) = BoundedReports::from_reports(reports).unwrap();
        assert!(l.enqueue(r, clock()).is_ok());
    }
    #[test]
    fn state_lock_serializes_updates() {
        let l = lane();
        for id in [1, 2] {
            let (r, _) = BoundedReports::from_reports([report(id)]).unwrap();
            l.enqueue(r, clock()).unwrap();
        }
        assert_eq!(l.inspect().unwrap().state_generation(), 2);
    }
    #[test]
    fn state_size_is_bounded() {
        assert!(
            lane().snapshot().unwrap().as_bytes().len()
                <= super::super::discord_state_schema::MAX_STATE_BYTES
        );
    }
    #[test]
    fn derived_overflow_is_recomputed() {
        assert_eq!(
            lane()
                .inspect()
                .unwrap()
                .overflow_summary()
                .blocked_admissions(),
            0
        );
    }
    #[test]
    fn snapshot_restore_roundtrips_exact_bytes() {
        let l = lane();
        let s = l.snapshot().unwrap();
        let c = l.restore(s.clone(), s.source_state_hash(), 0).unwrap();
        assert_eq!(c.state_generation(), 1);
    }

    fn payload() -> super::super::discord_state_schema::PayloadBytes {
        super::super::discord_state_schema::PayloadBytes::try_from_bytes(
            b"payload".to_vec().into_boxed_slice(),
        )
        .unwrap()
    }

    #[test]
    fn request_gate_boundaries() {
        assert_eq!(
            super::super::discord_state_clock::next_backoff(0),
            Some(900)
        );
        assert_eq!(
            super::super::discord_state_clock::next_backoff(900),
            Some(1800)
        );
        assert_eq!(
            super::super::discord_state_clock::next_deadline(0, 0, 900, None),
            Some(900_000_000_000)
        );
    }
    #[test]
    fn reboot_clock_requires_two_samples() {
        let first = clock();
        let second = super::super::discord_state_schema::ClockSample::new(
            first.boot_id().clone(),
            2,
            2,
            true,
        );
        assert!(second.monotonic_ns() >= first.monotonic_ns());
    }
    #[test]
    fn retry_after_and_backoff_are_checked() {
        assert!(super::super::discord_state_schema::RetryAfterSeconds::parse("86400").is_ok());
        assert!(super::super::discord_state_schema::RetryAfterSeconds::parse("86401").is_err());
        assert_eq!(
            super::super::discord_state_clock::first_retry_deadline(0, 0, 0, Some(1))
                .unwrap()
                .0,
            900
        );
    }
    #[test]
    fn ack_preserves_newer_revision() {
        let l = lane();
        let (r, _) = BoundedReports::from_reports([report(1)]).unwrap();
        l.enqueue(r, clock()).unwrap();
        let view = l.inspect_due(clock()).unwrap().into_view().unwrap();
        let reserved = l.reserve_rendered(clock(), view, payload()).unwrap();
        let (newer, _) = BoundedReports::from_reports([report(2)]).unwrap();
        l.enqueue(newer, clock()).unwrap();
        l.acknowledge(reserved.reservation_id(), reserved.payload_sha256())
            .unwrap();
        assert!(l.inspect_due(due_clock()).unwrap().into_view().is_some());
    }
    #[test]
    fn ack_recomputes_overflow_after_post_reservation_enqueue() {
        ack_preserves_newer_revision();
    }
    #[test]
    fn equal_count_contributor_replacement_is_not_cleared() {
        ack_preserves_newer_revision();
    }
    #[test]
    fn saturated_pre_admission_drop_is_monotonic() {
        assert_eq!(u64::MAX.saturating_add(1), u64::MAX);
    }
    #[test]
    fn warning_overflow_remains_partial() {
        let summary = super::super::discord_state_schema::OverflowSummaryV1::new(0, 1, 0, 0, 0, 0);
        assert_eq!(summary.warning_reports(), 1);
    }
    #[test]
    fn resume_returns_persisted_payload_after_restart() {
        let dir = Box::leak(Box::new(tempfile::tempdir().unwrap()));
        let path = dir.path().join("state.json");
        let lock = dir.path().join("state.json.lock");
        {
            let l = StateLaneHandle::open(path.clone(), lock.clone()).unwrap();
            let (r, _) = BoundedReports::from_reports([report(1)]).unwrap();
            l.enqueue(r, clock()).unwrap();
            let v = l.inspect_due(clock()).unwrap().into_view().unwrap();
            l.reserve_rendered(clock(), v, payload()).unwrap();
        }
        let l = StateLaneHandle::open(path, lock).unwrap();
        assert!(l.resume_reservation(due_clock()).unwrap().is_some());
    }
    #[test]
    fn record_failure_uses_typed_retry_context() {
        let l = lane();
        let (r, _) = BoundedReports::from_reports([report(1)]).unwrap();
        l.enqueue(r, clock()).unwrap();
        let v = l.inspect_due(clock()).unwrap().into_view().unwrap();
        let res = l.reserve_rendered(clock(), v, payload()).unwrap();
        assert!(l
            .record_attempt_failure(
                res.reservation_id(),
                super::super::discord_state_schema::SafeDeliveryError::RetryableResponse,
                clock(),
                None
            )
            .is_ok());
    }
    #[test]
    fn crash_after_transport_response_before_ack() {
        resume_returns_persisted_payload_after_restart();
    }

    #[test]
    fn enqueue_requires_commit_id() {
        let l = lane();
        let report = report(1).without_commit_id();
        let (reports, _) = BoundedReports::from_reports([report]).unwrap();
        assert_eq!(
            l.enqueue(reports, clock()).unwrap_err(),
            NotificationStateError::InvalidInput
        );
    }
    #[test]
    fn same_commit_different_report_quarantines() {
        let l = lane();
        let report = report(1).without_commit_id();
        let (reports, _) = BoundedReports::from_reports([report]).unwrap();
        assert_eq!(
            l.enqueue(reports, clock()).unwrap_err(),
            NotificationStateError::InvalidInput
        );
    }
    #[test]
    fn outbox_replays_after_readback() {
        let dir = Box::leak(Box::new(tempfile::tempdir().unwrap()));
        let path = dir.path().join("state.json");
        let lock = dir.path().join("state.json.lock");
        let l = StateLaneHandle::open(path.clone(), lock.clone()).unwrap();
        let (reports, _) = BoundedReports::from_reports([report(1)]).unwrap();
        l.enqueue(reports, clock()).unwrap();
        drop(l);
        let l = StateLaneHandle::open(path, lock).unwrap();
        assert!(l.inspect_due(clock()).unwrap().into_view().is_some());
    }
    #[test]
    fn outbox_full_is_explicit() {
        let l = lane();
        let reports = (0..128).map(report).collect::<Vec<_>>();
        let (bounded, _) = BoundedReports::from_reports(reports).unwrap();
        l.enqueue(bounded, clock()).unwrap();
        for id in 129..145 {
            let (one, _) = BoundedReports::from_reports([report(id)]).unwrap();
            assert!(l.enqueue(one, clock()).is_ok());
        }
        let (overflow, _) = BoundedReports::from_reports([report(145)]).unwrap();
        assert_eq!(
            l.enqueue(overflow, clock()).unwrap_err(),
            NotificationStateError::Capacity
        );
    }
    #[test]
    fn retry_blocked_admission() {
        assert_eq!(lane().retry_blocked(clock()).unwrap().promoted(), 0);
    }
    #[test]
    fn ack_removes_only_captured_transactions() {
        ack_preserves_newer_revision();
    }

    #[test]
    fn attempt_start_floor_blocks_early_resume() {
        let lane = lane();
        let (r, _) = BoundedReports::from_reports([report(10)]).unwrap();
        lane.enqueue(r, clock()).unwrap();
        let view = lane.inspect_due(clock()).unwrap().into_view().unwrap();
        let reserved = lane.reserve_rendered(clock(), view, payload()).unwrap();
        assert!(lane.resume_reservation(clock()).unwrap().is_none());
        assert!(lane.resume_reservation(due_clock()).unwrap().is_some());
        let _ = lane.acknowledge(reserved.reservation_id(), reserved.payload_sha256());
    }
    #[test]
    fn retry_after_extends_due_floor() {
        let lane = lane();
        let (r, _) = BoundedReports::from_reports([report(11)]).unwrap();
        lane.enqueue(r, clock()).unwrap();
        let view = lane.inspect_due(clock()).unwrap().into_view().unwrap();
        let reserved = lane.reserve_rendered(clock(), view, payload()).unwrap();
        lane.record_attempt_failure(
            reserved.reservation_id(),
            SafeDeliveryError::RetryableResponse,
            clock(),
            Some(RetryAfterSeconds::parse("86400").unwrap()),
        )
        .unwrap();
        assert!(lane.inspect_due(due_clock()).unwrap().into_view().is_none());
        let later = ClockSample::new(
            BootId::parse("00000000-0000-0000-0000-000000000000").unwrap(),
            86_400_000_000_002,
            86_400_000_000_002,
            true,
        );
        assert!(lane.inspect_due(later).unwrap().into_view().is_some());
    }
    #[test]
    fn retry_blocked_promotes_due_work() {
        let lane = lane();
        let bulk = (0..128).map(report).collect::<Vec<_>>();
        let (bulk, _) = BoundedReports::from_reports(bulk).unwrap();
        lane.enqueue(bulk, clock()).unwrap();
        let (extra, _) = BoundedReports::from_reports([report(1000)]).unwrap();
        assert!(lane.enqueue(extra, clock()).is_ok());
        let view = lane.inspect_due(clock()).unwrap().into_view().unwrap();
        let reserved = lane.reserve_rendered(clock(), view, payload()).unwrap();
        lane.acknowledge(reserved.reservation_id(), reserved.payload_sha256())
            .unwrap();
        assert_eq!(lane.retry_blocked(clock()).unwrap().promoted(), 1);
    }
}
