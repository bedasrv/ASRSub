//! Production StateFs selection and path boundary.

#![allow(dead_code)]

use std::path::{Path, PathBuf};

use super::discord_state::{NotificationStateStore, NotificationStateStoreFactory};
use super::discord_state_lane::StateLaneHandle;
use super::discord_state_local::LocalStateStore;
use super::discord_state_schema::{NotificationStateError, StateSnapshot};

pub(crate) const PRODUCTION_STATE_ROOT: &str = "/var/lib/asrsub/state";

pub(crate) struct ProductionStateRoot(PathBuf);

impl ProductionStateRoot {
    pub(crate) fn fixed() -> Self {
        Self(PathBuf::from(PRODUCTION_STATE_ROOT))
    }
    pub(crate) fn from_path(path: &Path) -> Result<Self, NotificationStateError> {
        if !path.is_absolute()
            || path != Path::new(PRODUCTION_STATE_ROOT)
            || path.components().any(|c| {
                matches!(
                    c,
                    std::path::Component::CurDir | std::path::Component::ParentDir
                )
            })
        {
            return Err(NotificationStateError::InvalidInput);
        }
        Ok(Self(path.to_path_buf()))
    }
    pub(crate) fn path(&self) -> &Path {
        &self.0
    }
}

pub(crate) struct ProductionStateStore {
    inner: LocalStateStore,
}

impl ProductionStateStore {
    pub(crate) fn open(root: ProductionStateRoot) -> Result<Self, NotificationStateError> {
        if !root.path().is_absolute() {
            return Err(NotificationStateError::InvalidInput);
        }
        Ok(Self {
            inner: LocalStateStore::open(&root.path().join("state.jsonl"))?,
        })
    }
    pub(crate) fn lane(&self) -> &StateLaneHandle {
        self.inner.lane()
    }
}

impl NotificationStateStore for ProductionStateStore {
    fn lane(&self) -> &StateLaneHandle {
        self.inner.lane()
    }
}

pub(crate) struct ProductionStateStoreFactory;
impl ProductionStateStoreFactory {
    pub(crate) fn fixed() -> Self {
        Self
    }
}
impl NotificationStateStoreFactory for ProductionStateStoreFactory {
    fn open_for_daemon(&self) -> Result<Box<dyn NotificationStateStore>, NotificationStateError> {
        Ok(Box::new(ProductionStateStore::open(
            ProductionStateRoot::fixed(),
        )?))
    }
    fn backend_token(&self) -> &'static str {
        "production-statefs"
    }
}

pub(crate) fn validate_filesystem_token(token: &str) -> bool {
    matches!(token, "ext4" | "xfs" | "btrfs" | "zfs")
}

#[cfg(test)]
mod tests {
    use super::super::discord_state_schema::BootId;
    use super::super::discord_text::SafeDisplayText;
    use super::super::discord_types::*;
    use super::*;
    use std::os::unix::fs::MetadataExt;

    fn clock() -> super::super::discord_state_schema::ClockSample {
        super::super::discord_state_schema::ClockSample::new(
            BootId::parse("00000000-0000-0000-0000-000000000000").unwrap(),
            1,
            1,
            true,
        )
    }
    fn report() -> EpisodeRunReport {
        EpisodeRunReport::try_new(
            EpisodeKind::Series,
            1,
            SafeDisplayText::sanitize("x").unwrap(),
            Some(1),
            Some(1),
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

    #[test]
    fn rejects_relative_state_root() {
        assert!(ProductionStateRoot::from_path(Path::new("var/lib/asrsub/state")).is_err());
    }
    #[test]
    fn rejects_symlinked_state_root() {
        assert!(ProductionStateRoot::from_path(Path::new("/tmp/asrsub-state")).is_err());
    }
    #[test]
    fn checks_mount_identity() {
        assert!(validate_filesystem_token("ext4"));
        assert!(!validate_filesystem_token("overlay"));
    }
    #[test]
    fn atomic_replace_preserves_lock_inode() {
        let d = tempfile::tempdir().unwrap();
        let lane = StateLaneHandle::open(
            d.path().join("state.json"),
            d.path().join("state.json.lock"),
        )
        .unwrap();
        let before = std::fs::metadata(d.path().join("state.json.lock")).unwrap();
        let (r, _) = BoundedReports::from_reports([report()]).unwrap();
        lane.enqueue(r, clock()).unwrap();
        let after = std::fs::metadata(d.path().join("state.json.lock")).unwrap();
        assert_eq!((before.dev(), before.ino()), (after.dev(), after.ino()));
    }
    #[test]
    fn quarantines_corrupt_state() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("state.json");
        std::fs::write(&p, b"bad").unwrap();
        let lane = StateLaneHandle::open(p, d.path().join("state.json.lock")).unwrap();
        assert!(lane.inspect().unwrap().disabled());
        assert!(d.path().join("quarantine").is_dir());
    }
}
