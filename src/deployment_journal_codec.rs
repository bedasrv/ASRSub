#![allow(dead_code)]
use super::deployment_journal_schema::JournalV1;
pub(crate) fn encode(j: &JournalV1) -> Vec<u8> {
    format!("{{\"schema\":\"journal-v1\",\"transaction_nonce\":\"{}\",\"phase\":\"{}\",\"accept_work\":{},\"lease_present\":{},\"updated_epoch_ns\":{}}}",j.transaction_nonce,j.phase.token(),j.accept_work,j.lease_present,j.updated_epoch_ns).into_bytes()
}
pub(crate) fn hash(j: &JournalV1) -> [u8; 32] {
    super::discord_state_codec::domain_hash(b"asrsub-journal-v1", &encode(j))
}
