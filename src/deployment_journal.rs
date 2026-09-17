#![allow(dead_code)]
use super::deployment_journal_schema::{JournalPhase, JournalV1};
pub(crate) fn next(c: Option<JournalPhase>, n: JournalPhase) -> bool {
    matches!(
        (c, n),
        (None, JournalPhase::Prepared)
            | (Some(JournalPhase::Prepared), JournalPhase::Quiescing)
            | (Some(JournalPhase::Quiescing), JournalPhase::Quiesced)
            | (Some(JournalPhase::Started), JournalPhase::Committed)
            | (Some(JournalPhase::Started), JournalPhase::RollbackRequired)
            | (
                Some(JournalPhase::RollbackRequired),
                JournalPhase::RolledBack
            )
            | (Some(JournalPhase::Prepared), JournalPhase::Aborted)
    )
}
pub(crate) fn prepared(n: String) -> JournalV1 {
    JournalV1 {
        transaction_nonce: n,
        phase: JournalPhase::Prepared,
        accept_work: false,
        lease_present: true,
        updated_epoch_ns: 0,
    }
}
