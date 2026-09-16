#![allow(dead_code)]
//! Pure transition-kernel façade.  The lane owns serialization and invokes
//! these bounded helpers; no filesystem capability lives here.

use super::discord_state_clock;

pub(crate) fn first_retry_deadline(
    attempt_start: u64,
    response_epoch: u64,
    previous: u64,
    retry_after: Option<u64>,
) -> Option<(u64, u64)> {
    let backoff = discord_state_clock::next_backoff(previous)?;
    Some((
        backoff,
        discord_state_clock::next_deadline(attempt_start, response_epoch, backoff, retry_after)?,
    ))
}
