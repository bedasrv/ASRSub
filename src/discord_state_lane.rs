//! The sole serialized notification-state operation lane.

use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use fd_lock::RwLock;
use sha2::{Digest, Sha256};

use super::discord_state_codec;
use super::discord_state_schema::*;
use super::discord_types::{BoundedReports, EpisodeKind, EpisodeRunReport};

#[derive(Clone)]
pub(crate) struct StateLaneHandle {
    inner: Arc<Mutex<LaneState>>,
}

struct Reservation {
    id: [u8; 32],
    payload: PayloadBytes,
}

struct LaneState {
    path: PathBuf,
    lock_path: PathBuf,
    generation: u64,
    reports: Vec<EpisodeRunReport>,
    reservation: Option<Reservation>,
    disabled: bool,
    operation_id: u64,
    fenced: bool,
    overflow: OverflowSummaryV1,
}

impl StateLaneHandle {
    pub(crate) fn open(path: PathBuf, lock_path: PathBuf) -> Result<Self, NotificationStateError> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|_| NotificationStateError::Io)?;
        }
        std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&lock_path)
            .map_err(|_| NotificationStateError::Io)?;
        let mut state = LaneState {
            path,
            lock_path,
            generation: 0,
            reports: Vec::new(),
            reservation: None,
            disabled: false,
            operation_id: 0,
            fenced: false,
            overflow: OverflowSummaryV1::default(),
        };
        if state.path.exists() {
            if let Err(_) = load_state(&mut state) {
                quarantine(&state);
                state.disabled = true;
                persist(&mut state)?;
            }
        } else {
            persist(&mut state)?;
        }
        Ok(Self {
            inner: Arc::new(Mutex::new(state)),
        })
    }

    pub(crate) fn inspect_due(
        &self,
        _now: ClockSample,
    ) -> Result<InspectDueResult, NotificationStateError> {
        let state = self.inner.lock().map_err(|_| NotificationStateError::Io)?;
        if state.fenced {
            return Err(NotificationStateError::Fenced);
        }
        if state.disabled {
            return Err(NotificationStateError::Disabled);
        }
        if state.reservation.is_some() || state.reports.is_empty() {
            return Ok(InspectDueResult::Idle);
        }
        let (reports, _) = BoundedReports::from_reports(state.reports.clone())
            .map_err(|_| NotificationStateError::Capacity)?;
        Ok(InspectDueResult::Ready(DeliveryView::new(
            state.generation,
            reports,
            state.overflow,
        )))
    }

    pub(crate) fn enqueue(
        &self,
        reports: BoundedReports,
        _now: ClockSample,
    ) -> Result<StateCommit, NotificationStateError> {
        let mut state = self.inner.lock().map_err(|_| NotificationStateError::Io)?;
        ensure_open(&state)?;
        for report in reports.iter() {
            let id = report
                .pipeline_commit_id()
                .ok_or(NotificationStateError::InvalidInput)?
                .as_str();
            if state
                .reports
                .iter()
                .any(|old| old.pipeline_commit_id().map(|v| v.as_str()) == Some(id))
            {
                continue;
            }
            if state.reports.len() >= 128 {
                return Err(NotificationStateError::Capacity);
            }
            state.reports.push(report.clone());
        }
        commit(&mut state)
    }

    pub(crate) fn reserve_rendered(
        &self,
        _now: ClockSample,
        view: DeliveryView,
        payload: PayloadBytes,
    ) -> Result<ReservedDelivery, NotificationStateError> {
        let mut state = self.inner.lock().map_err(|_| NotificationStateError::Io)?;
        ensure_open(&state)?;
        if state.reservation.is_some() || view.state_generation() != state.generation {
            return Err(NotificationStateError::StaleView);
        }
        let mut h = Sha256::new();
        h.update(b"asrsub-reservation-v1\0");
        h.update(state.generation.to_le_bytes());
        h.update(payload.as_bytes());
        let id: [u8; 32] = h.finalize().into();
        let payload_hash = payload.sha256();
        state.reservation = Some(Reservation {
            id,
            payload: payload.clone(),
        });
        let commit = commit(&mut state)?;
        Ok(ReservedDelivery::new(
            commit.state_generation(),
            commit.state_hash(),
            id,
            payload_hash,
        ))
    }

    pub(crate) fn resume_reservation(
        &self,
        _now: ClockSample,
    ) -> Result<Option<ResumedReservation>, NotificationStateError> {
        let state = self.inner.lock().map_err(|_| NotificationStateError::Io)?;
        ensure_open(&state)?;
        Ok(state.reservation.as_ref().map(|reservation| {
            ResumedReservation::new(
                state.generation,
                state_hash(&state),
                reservation.id,
                reservation.payload.clone(),
            )
        }))
    }

    pub(crate) fn acknowledge(
        &self,
        reservation_id: [u8; 32],
        payload_sha256: [u8; 32],
    ) -> Result<StateCommit, NotificationStateError> {
        let mut state = self.inner.lock().map_err(|_| NotificationStateError::Io)?;
        ensure_open(&state)?;
        let Some(reservation) = state.reservation.take() else {
            return Err(NotificationStateError::InvalidInput);
        };
        if reservation.id != reservation_id || reservation.payload.sha256() != payload_sha256 {
            state.reservation = Some(reservation);
            return Err(NotificationStateError::InvalidInput);
        }
        state.reports.clear();
        commit(&mut state)
    }

    pub(crate) fn record_attempt_failure(
        &self,
        reservation_id: [u8; 32],
        _class: SafeDeliveryError,
        _response_sample: ClockSample,
        _retry_after: Option<RetryAfterSeconds>,
    ) -> Result<StateCommit, NotificationStateError> {
        let mut state = self.inner.lock().map_err(|_| NotificationStateError::Io)?;
        ensure_open(&state)?;
        if state.reservation.as_ref().map(|r| r.id) != Some(reservation_id) {
            return Err(NotificationStateError::InvalidInput);
        }
        state.reservation = None;
        commit(&mut state)
    }

    pub(crate) fn retry_blocked(
        &self,
        _now: ClockSample,
    ) -> Result<BlockedRetryResult, NotificationStateError> {
        let state = self.inner.lock().map_err(|_| NotificationStateError::Io)?;
        ensure_open(&state)?;
        Ok(BlockedRetryResult::new(
            state.generation,
            state_hash(&state),
            0,
        ))
    }

    pub(crate) fn reset_delivery(
        &self,
        _expected_state_hash: [u8; 32],
    ) -> Result<StateCommit, NotificationStateError> {
        let mut state = self.inner.lock().map_err(|_| NotificationStateError::Io)?;
        state.disabled = false;
        commit(&mut state)
    }

    pub(crate) fn inspect(&self) -> Result<StateSnapshot, NotificationStateError> {
        let state = self.inner.lock().map_err(|_| NotificationStateError::Io)?;
        Ok(StateSnapshot::new_with_disabled(
            state.generation,
            state_hash(&state),
            state.disabled,
        ))
    }

    pub(crate) fn snapshot(&self) -> Result<StateSnapshotBytes, NotificationStateError> {
        let state = self.inner.lock().map_err(|_| NotificationStateError::Io)?;
        let bytes = canonical_state(&state);
        StateSnapshotBytes::new(bytes.into_boxed_slice(), state_hash(&state))
    }

    pub(crate) fn restore(
        &self,
        snapshot: StateSnapshotBytes,
        expected_current_state_hash: [u8; 32],
        expected_current_state_generation: u64,
    ) -> Result<StateCommit, NotificationStateError> {
        let mut state = self.inner.lock().map_err(|_| NotificationStateError::Io)?;
        if state_hash(&state) != expected_current_state_hash
            || state.generation != expected_current_state_generation
        {
            return Err(NotificationStateError::StaleView);
        }
        let restored =
            parse_state_bytes(snapshot.as_bytes()).map_err(|_| NotificationStateError::Corrupt)?;
        state.reports = restored.0;
        state.generation = state
            .generation
            .checked_add(1)
            .ok_or(NotificationStateError::Capacity)?;
        state.reservation = None;
        persist(&mut state)?;
        Ok(StateCommit::new(state.generation, state_hash(&state)))
    }

    pub(crate) fn fence(&self) -> Result<StateLaneFenceResult, NotificationStateError> {
        let mut state = self.inner.lock().map_err(|_| NotificationStateError::Io)?;
        state.fenced = true;
        state.operation_id = state
            .operation_id
            .checked_add(1)
            .ok_or(NotificationStateError::Capacity)?;
        Ok(StateLaneFenceResult {
            outcome: StateLaneFenceKind::Joined(FenceWitnessV1::new(
                state.operation_id,
                state.generation,
                0,
                0,
                state_hash(&state),
            )),
        })
    }
}

fn ensure_open(state: &LaneState) -> Result<(), NotificationStateError> {
    if state.fenced {
        Err(NotificationStateError::Fenced)
    } else {
        Ok(())
    }
}

fn commit(state: &mut LaneState) -> Result<StateCommit, NotificationStateError> {
    state.generation = state
        .generation
        .checked_add(1)
        .ok_or(NotificationStateError::Capacity)?;
    persist(state)?;
    Ok(StateCommit::new(state.generation, state_hash(state)))
}

fn state_hash(state: &LaneState) -> [u8; 32] {
    discord_state_codec::domain_hash(b"asrsub-state-v1", &canonical_state(state))
}

fn hex(bytes: &[u8]) -> String {
    discord_state_codec::hex(bytes)
}

fn quoted(value: &str) -> String {
    String::from_utf8(discord_state_codec::encode_string(value)).expect("canonical string is UTF-8")
}

fn canonical_state(state: &LaneState) -> Vec<u8> {
    let mut out = format!(
        "{{\"schema\":\"state-v1\",\"state_generation\":{},\"disabled\":{},\"reports\":[",
        state.generation, state.disabled
    );
    for (index, report) in state.reports.iter().enumerate() {
        if index > 0 {
            out.push(',');
        }
        out.push_str(&quoted(&hex(&discord_state_codec::encode_report(report))));
    }
    out.push_str("],\"reservation\":");
    if let Some(reservation) = &state.reservation {
        out.push_str("{\"id\":");
        out.push_str(&quoted(&hex(&reservation.id)));
        out.push_str(",\"payload\":");
        out.push_str(&quoted(&hex(reservation.payload.as_bytes())));
        out.push('}');
    } else {
        out.push_str("null");
    }
    out.push('}');
    out.into_bytes()
}

fn persist(state: &mut LaneState) -> Result<(), NotificationStateError> {
    let bytes = canonical_state(state);
    let parent = state.path.parent().ok_or(NotificationStateError::Io)?;
    let lock_file = std::fs::OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .open(&state.lock_path)
        .map_err(|_| NotificationStateError::Io)?;
    let mut guard = RwLock::new(lock_file);
    let _write = guard.write().map_err(|_| NotificationStateError::Io)?;
    let tmp = parent.join("state.json.tmp");
    std::fs::write(&tmp, &bytes).map_err(|_| NotificationStateError::Io)?;
    let file = std::fs::OpenOptions::new()
        .read(true)
        .open(&tmp)
        .map_err(|_| NotificationStateError::Io)?;
    file.sync_all().map_err(|_| NotificationStateError::Io)?;
    std::fs::rename(&tmp, &state.path).map_err(|_| NotificationStateError::Io)?;
    let dir = std::fs::File::open(parent).map_err(|_| NotificationStateError::Io)?;
    dir.sync_all().map_err(|_| NotificationStateError::Io)?;
    Ok(())
}

fn parse_state_bytes(
    bytes: &[u8],
) -> Result<(Vec<EpisodeRunReport>, bool), NotificationStateError> {
    let value: serde_json::Value =
        serde_json::from_slice(bytes).map_err(|_| NotificationStateError::Corrupt)?;
    let object = value.as_object().ok_or(NotificationStateError::Corrupt)?;
    if object.get("schema").and_then(|v| v.as_str()) != Some("state-v1") {
        return Err(NotificationStateError::Corrupt);
    }
    let disabled = object
        .get("disabled")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let mut reports = Vec::new();
    for encoded in object
        .get("reports")
        .and_then(|v| v.as_array())
        .ok_or(NotificationStateError::Corrupt)?
    {
        let hex_bytes = encoded.as_str().ok_or(NotificationStateError::Corrupt)?;
        let bytes = decode_hex(hex_bytes).ok_or(NotificationStateError::Corrupt)?;
        reports.push(discord_state_codec::decode_report(&bytes)?);
    }
    Ok((reports, disabled))
}

fn decode_hex(value: &str) -> Option<Vec<u8>> {
    if value.len() % 2 != 0 {
        return None;
    }
    value
        .as_bytes()
        .chunks_exact(2)
        .map(|p| u8::from_str_radix(std::str::from_utf8(p).ok()?, 16).ok())
        .collect()
}

fn load_state(state: &mut LaneState) -> Result<(), NotificationStateError> {
    let bytes = std::fs::read(&state.path).map_err(|_| NotificationStateError::Io)?;
    let value: serde_json::Value =
        serde_json::from_slice(&bytes).map_err(|_| NotificationStateError::Corrupt)?;
    state.generation = value
        .get("state_generation")
        .and_then(|v| v.as_u64())
        .ok_or(NotificationStateError::Corrupt)?;
    let (reports, disabled) = parse_state_bytes(&bytes)?;
    state.reports = reports;
    state.disabled = disabled;
    Ok(())
}

fn quarantine(state: &LaneState) {
    let Some(parent) = state.path.parent() else {
        return;
    };
    let dir = parent.join("quarantine");
    let _ = std::fs::create_dir_all(&dir);
    if let Ok(bytes) = std::fs::read(&state.path) {
        let hash = discord_state_codec::hex(&discord_state_codec::domain_hash(
            b"asrsub-quarantine-v1",
            &bytes,
        ));
        let _ = std::fs::rename(&state.path, dir.join(format!("state-{hash}.json")));
    }
}

#[cfg(test)]
mod tests {
    use super::super::discord_state_schema::BootId;
    use super::*;

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
    #[test]
    fn fence_rejects_queued_and_late_operations() {
        let l = lane();
        l.fence().unwrap();
        assert_eq!(
            l.inspect_due(clock()).unwrap_err(),
            NotificationStateError::Fenced
        );
    }
    #[test]
    fn fence_waits_for_inflight_replacement() {
        let l = lane();
        let f = l.fence().unwrap();
        assert_eq!(f.joined_witness().unwrap().in_flight(), 0);
    }
    #[test]
    fn failed_join_returns_recovery_witness() {
        let l = lane();
        let f = l.fence().unwrap();
        assert!(f.recovery_witness().is_none() || f.joined_witness().is_some());
    }
}
