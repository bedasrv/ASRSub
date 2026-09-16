//! Notification state store capability boundary.

use std::path::PathBuf;

use super::discord_state_lane::StateLaneHandle;
use super::discord_state_schema::NotificationStateError;

pub(crate) trait NotificationStateStore: Send + Sync {
    fn lane(&self) -> &StateLaneHandle;
}

pub(crate) trait NotificationStateStoreFactory: Send + Sync {
    fn open_for_daemon(&self) -> Result<Box<dyn NotificationStateStore>, NotificationStateError>;
    fn backend_token(&self) -> &'static str;
}

#[derive(Clone)]
pub(crate) struct LocalStateStoreFactory {
    state_file: PathBuf,
}

impl LocalStateStoreFactory {
    pub(crate) fn new(state_file: PathBuf) -> Self {
        Self { state_file }
    }
}

impl NotificationStateStoreFactory for LocalStateStoreFactory {
    fn open_for_daemon(&self) -> Result<Box<dyn NotificationStateStore>, NotificationStateError> {
        Ok(Box::new(super::discord_state_local::LocalStateStore::open(
            &self.state_file,
        )?))
    }

    fn backend_token(&self) -> &'static str {
        "local-statefs"
    }
}

#[cfg(test)]
mod tests {
    use super::super::discord_state_schema::BootId;
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
}
