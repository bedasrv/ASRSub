#![allow(dead_code)]
//! Checked cadence calculations used by the state engine.

pub(crate) const ATTEMPT_WINDOW_NS: u64 = 900 * 1_000_000_000;
pub(crate) const MAX_BACKOFF_SECONDS: u64 = 86_400;

pub(crate) fn next_backoff(previous: u64) -> Option<u64> {
    if previous == 0 {
        Some(900)
    } else {
        Some(previous.checked_mul(2)?.min(MAX_BACKOFF_SECONDS))
    }
}

pub(crate) fn next_deadline(
    attempt_start: u64,
    response_epoch: u64,
    backoff: u64,
    retry_after: Option<u64>,
) -> Option<u64> {
    let floor = attempt_start.checked_add(ATTEMPT_WINDOW_NS)?;
    let retry = response_epoch.checked_add(backoff.checked_mul(1_000_000_000)?)?;
    let header = retry_after
        .unwrap_or(0)
        .checked_mul(1_000_000_000)?
        .checked_add(response_epoch)?;
    Some(floor.max(retry).max(header))
}

pub(crate) fn first_retry_deadline(
    attempt_start: u64,
    response_epoch: u64,
    previous: u64,
    retry_after: Option<u64>,
) -> Option<(u64, u64)> {
    let backoff = next_backoff(previous)?;
    Some((
        backoff,
        next_deadline(attempt_start, response_epoch, backoff, retry_after)?,
    ))
}
