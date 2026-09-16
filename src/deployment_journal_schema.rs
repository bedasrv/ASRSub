#![allow(dead_code)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum JournalPhase {
    Prepared,
    Quiescing,
    Quiesced,
    Installing,
    Installed,
    Starting,
    Started,
    Committed,
    RollbackRequired,
    RolledBack,
    Aborted,
    RecoveryRequired,
}
impl JournalPhase {
    pub(crate) fn token(self) -> &'static str {
        match self {
            Self::Prepared => "prepared",
            Self::Quiescing => "quiescing",
            Self::Quiesced => "quiesced",
            Self::Installing => "installing",
            Self::Installed => "installed",
            Self::Starting => "starting",
            Self::Started => "started",
            Self::Committed => "committed",
            Self::RollbackRequired => "rollback_required",
            Self::RolledBack => "rolled_back",
            Self::Aborted => "aborted",
            Self::RecoveryRequired => "recovery_required",
        }
    }
}
#[derive(Clone, Debug)]
pub(crate) struct JournalV1 {
    pub(crate) transaction_nonce: String,
    pub(crate) phase: JournalPhase,
    pub(crate) accept_work: bool,
    pub(crate) lease_present: bool,
    pub(crate) updated_epoch_ns: u64,
}
