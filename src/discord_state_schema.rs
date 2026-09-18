#![allow(dead_code)]
use super::discord_types::BoundedReports;
use sha2::Digest;

pub(crate) const MAX_PAYLOAD_BYTES: usize = 65_536;
pub(crate) const MAX_STATE_BYTES: usize = 4 * 1024 * 1024;

#[derive(Clone, Eq, PartialEq, Debug)]
pub(crate) struct PayloadBytes(Box<[u8]>);

impl PayloadBytes {
    pub(crate) fn try_from_bytes(bytes: Box<[u8]>) -> Result<Self, NotificationStateError> {
        if bytes.is_empty() || bytes.len() > MAX_PAYLOAD_BYTES {
            return Err(NotificationStateError::Capacity);
        }
        Ok(Self(bytes))
    }
    pub(crate) fn as_bytes(&self) -> &[u8] {
        &self.0
    }
    pub(crate) fn sha256(&self) -> [u8; 32] {
        use sha2::{Digest, Sha256};
        let mut h = Sha256::new();
        h.update(b"asrsub-payload-v1\0");
        h.update(&self.0);
        h.finalize().into()
    }
}

#[derive(Clone, Eq, PartialEq, Debug)]
pub(crate) struct BootId(String);

impl BootId {
    pub(crate) fn parse(raw: &str) -> Result<Self, NotificationStateError> {
        let bytes = raw.as_bytes();
        let valid = bytes.len() == 36
            && bytes.iter().enumerate().all(|(i, b)| {
                if matches!(i, 8 | 13 | 18 | 23) {
                    *b == b'-'
                } else {
                    b.is_ascii_hexdigit() && !b.is_ascii_uppercase()
                }
            });
        if !valid {
            return Err(NotificationStateError::InvalidInput);
        }
        Ok(Self(raw.to_string()))
    }
    pub(crate) fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Clone, Eq, PartialEq, Debug)]
pub(crate) struct ClockSample {
    boot_id: BootId,
    epoch_ns: u64,
    monotonic_ns: u64,
    synchronized: bool,
}

impl ClockSample {
    pub(crate) fn new(
        boot_id: BootId,
        epoch_ns: u64,
        monotonic_ns: u64,
        synchronized: bool,
    ) -> Self {
        Self {
            boot_id,
            epoch_ns,
            monotonic_ns,
            synchronized,
        }
    }
    pub(crate) fn boot_id(&self) -> &BootId {
        &self.boot_id
    }
    pub(crate) fn epoch_ns(&self) -> u64 {
        self.epoch_ns
    }
    pub(crate) fn monotonic_ns(&self) -> u64 {
        self.monotonic_ns
    }
    pub(crate) fn synchronized(&self) -> bool {
        self.synchronized
    }
}

#[derive(Clone, Copy, Eq, PartialEq, Debug)]
pub(crate) struct RetryAfterSeconds(u32);

impl RetryAfterSeconds {
    pub(crate) fn parse(raw: &str) -> Result<Self, NotificationStateError> {
        let value = raw
            .parse::<u64>()
            .map_err(|_| NotificationStateError::InvalidInput)?;
        if value > 86_400 {
            return Err(NotificationStateError::InvalidInput);
        }
        Ok(Self(value as u32))
    }
    pub(crate) fn as_u64(&self) -> u64 {
        self.0 as u64
    }
}

#[derive(Clone, Copy, Eq, PartialEq, Debug)]
pub(crate) enum NotificationStateError {
    Missing,
    Corrupt,
    Contradiction,
    Capacity,
    Clock,
    Io,
    Disabled,
    InvalidInput,
    StaleView,
    Fenced,
}

#[derive(Clone, Copy, Eq, PartialEq, Debug)]
pub(crate) enum SafeDeliveryError {
    Transport,
    Timeout,
    ConnectionReset,
    RetryableResponse,
    PermanentResponse,
    Clock,
    Write,
    Corrupt,
    Disabled,
}

#[derive(Clone, Copy, Eq, PartialEq, Debug, Default)]
pub(crate) struct OverflowSummaryV1 {
    attention_reports: u64,
    warning_reports: u64,
    completed_reports: u64,
    target_outcomes: u64,
    pre_admission_drops: u64,
    blocked_admissions: u64,
}

impl OverflowSummaryV1 {
    pub(crate) fn new(
        attention_reports: u64,
        warning_reports: u64,
        completed_reports: u64,
        target_outcomes: u64,
        pre_admission_drops: u64,
        blocked_admissions: u64,
    ) -> Self {
        Self {
            attention_reports,
            warning_reports,
            completed_reports,
            target_outcomes,
            pre_admission_drops,
            blocked_admissions,
        }
    }
    pub(crate) fn attention_reports(&self) -> u64 {
        self.attention_reports
    }
    pub(crate) fn warning_reports(&self) -> u64 {
        self.warning_reports
    }
    pub(crate) fn completed_reports(&self) -> u64 {
        self.completed_reports
    }
    pub(crate) fn target_outcomes(&self) -> u64 {
        self.target_outcomes
    }
    pub(crate) fn pre_admission_drops(&self) -> u64 {
        self.pre_admission_drops
    }
    pub(crate) fn blocked_admissions(&self) -> u64 {
        self.blocked_admissions
    }
}

#[derive(Clone, Debug)]
pub(crate) struct DeliveryView {
    state_generation: u64,
    overflow_summary: OverflowSummaryV1,
    reports: BoundedReports,
}

impl DeliveryView {
    pub(crate) fn new(
        state_generation: u64,
        reports: BoundedReports,
        overflow_summary: OverflowSummaryV1,
    ) -> Self {
        Self {
            state_generation,
            overflow_summary,
            reports,
        }
    }
    pub(crate) fn state_generation(&self) -> u64 {
        self.state_generation
    }
    pub(crate) fn reports(&self) -> &BoundedReports {
        &self.reports
    }
    pub(crate) fn overflow_summary(&self) -> &OverflowSummaryV1 {
        &self.overflow_summary
    }
}

#[derive(Clone, Debug)]
pub(crate) enum InspectDueResult {
    Idle,
    Ready(DeliveryView),
}
impl InspectDueResult {
    pub(crate) fn into_view(self) -> Option<DeliveryView> {
        match self {
            Self::Idle => None,
            Self::Ready(v) => Some(v),
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct StateCommit {
    state_generation: u64,
    state_hash: [u8; 32],
}
impl StateCommit {
    pub(crate) fn new(state_generation: u64, state_hash: [u8; 32]) -> Self {
        Self {
            state_generation,
            state_hash,
        }
    }
    pub(crate) fn state_generation(&self) -> u64 {
        self.state_generation
    }
    pub(crate) fn state_hash(&self) -> [u8; 32] {
        self.state_hash
    }
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct ReservedDelivery {
    state_generation: u64,
    state_hash: [u8; 32],
    reservation_id: [u8; 32],
    payload_sha256: [u8; 32],
}
impl ReservedDelivery {
    pub(crate) fn new(
        state_generation: u64,
        state_hash: [u8; 32],
        reservation_id: [u8; 32],
        payload_sha256: [u8; 32],
    ) -> Self {
        Self {
            state_generation,
            state_hash,
            reservation_id,
            payload_sha256,
        }
    }
    pub(crate) fn state_generation(&self) -> u64 {
        self.state_generation
    }
    pub(crate) fn state_hash(&self) -> [u8; 32] {
        self.state_hash
    }
    pub(crate) fn reservation_id(&self) -> [u8; 32] {
        self.reservation_id
    }
    pub(crate) fn payload_sha256(&self) -> [u8; 32] {
        self.payload_sha256
    }
}

#[derive(Clone, Debug)]
pub(crate) struct ResumedReservation {
    state_generation: u64,
    state_hash: [u8; 32],
    reservation_id: [u8; 32],
    payload: PayloadBytes,
}
impl ResumedReservation {
    pub(crate) fn new(
        state_generation: u64,
        state_hash: [u8; 32],
        reservation_id: [u8; 32],
        payload: PayloadBytes,
    ) -> Self {
        Self {
            state_generation,
            state_hash,
            reservation_id,
            payload,
        }
    }
    pub(crate) fn state_generation(&self) -> u64 {
        self.state_generation
    }
    pub(crate) fn state_hash(&self) -> [u8; 32] {
        self.state_hash
    }
    pub(crate) fn reservation_id(&self) -> [u8; 32] {
        self.reservation_id
    }
    pub(crate) fn payload(self) -> PayloadBytes {
        self.payload
    }
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct BlockedRetryResult {
    state_generation: u64,
    state_hash: [u8; 32],
    promoted: u32,
}
impl BlockedRetryResult {
    pub(crate) fn new(state_generation: u64, state_hash: [u8; 32], promoted: u32) -> Self {
        Self {
            state_generation,
            state_hash,
            promoted,
        }
    }
    pub(crate) fn state_generation(&self) -> u64 {
        self.state_generation
    }
    pub(crate) fn state_hash(&self) -> [u8; 32] {
        self.state_hash
    }
    pub(crate) fn promoted(&self) -> u32 {
        self.promoted
    }
}

#[derive(Clone, Debug)]
pub(crate) struct StateSnapshotBytes {
    canonical_bytes: Box<[u8]>,
    source_state_hash: [u8; 32],
}
impl StateSnapshotBytes {
    pub(crate) fn new(bytes: Box<[u8]>, hash: [u8; 32]) -> Result<Self, NotificationStateError> {
        if bytes.len() > MAX_STATE_BYTES {
            return Err(NotificationStateError::Capacity);
        }
        Ok(Self {
            canonical_bytes: bytes,
            source_state_hash: hash,
        })
    }
    pub(crate) fn as_bytes(&self) -> &[u8] {
        &self.canonical_bytes
    }
    pub(crate) fn source_state_hash(&self) -> [u8; 32] {
        self.source_state_hash
    }
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct StateSnapshot {
    state_generation: u64,
    state_hash: [u8; 32],
    pending_count: usize,
    outbox_count: usize,
    blocked_count: usize,
    overflow_summary: OverflowSummaryV1,
    disabled: bool,
}
impl StateSnapshot {
    pub(crate) fn new(state_generation: u64, state_hash: [u8; 32]) -> Self {
        Self {
            state_generation,
            state_hash,
            pending_count: 0,
            outbox_count: 0,
            blocked_count: 0,
            overflow_summary: Default::default(),
            disabled: false,
        }
    }
    pub(crate) fn new_with_disabled(
        state_generation: u64,
        state_hash: [u8; 32],
        disabled: bool,
    ) -> Self {
        let mut snapshot = Self::new(state_generation, state_hash);
        snapshot.disabled = disabled;
        snapshot
    }
    pub(crate) fn state_generation(&self) -> u64 {
        self.state_generation
    }
    pub(crate) fn state_hash(&self) -> [u8; 32] {
        self.state_hash
    }
    pub(crate) fn pending_count(&self) -> usize {
        self.pending_count
    }
    pub(crate) fn outbox_count(&self) -> usize {
        self.outbox_count
    }
    pub(crate) fn blocked_count(&self) -> usize {
        self.blocked_count
    }
    pub(crate) fn overflow_summary(&self) -> &OverflowSummaryV1 {
        &self.overflow_summary
    }
    pub(crate) fn disabled(&self) -> bool {
        self.disabled
    }
}

#[derive(Clone, Debug)]
pub(crate) struct FenceWitnessV1 {
    operation_id: u64,
    fence_generation: u64,
    queue_drained: u32,
    in_flight: u32,
    last_durable_state_hash: [u8; 32],
}
impl FenceWitnessV1 {
    pub(crate) fn new(
        operation_id: u64,
        fence_generation: u64,
        queue_drained: u32,
        in_flight: u32,
        last_durable_state_hash: [u8; 32],
    ) -> Self {
        Self {
            operation_id,
            fence_generation,
            queue_drained,
            in_flight,
            last_durable_state_hash,
        }
    }
    pub(crate) fn operation_id(&self) -> u64 {
        self.operation_id
    }
    pub(crate) fn fence_generation(&self) -> u64 {
        self.fence_generation
    }
    pub(crate) fn join_witness_hash(&self) -> [u8; 32] {
        let mut h = sha2::Sha256::new();
        h.update(b"asrsub-fence-witness-v1\0");
        h.update(self.operation_id.to_le_bytes());
        h.update(self.fence_generation.to_le_bytes());
        h.finalize().into()
    }
    pub(crate) fn queue_drained(&self) -> u32 {
        self.queue_drained
    }
    pub(crate) fn in_flight(&self) -> u32 {
        self.in_flight
    }
    pub(crate) fn last_durable_state_hash(&self) -> [u8; 32] {
        self.last_durable_state_hash
    }
}
#[derive(Clone, Debug)]
pub(crate) struct FenceRecoveryWitnessV1 {
    operation_id: u64,
    fence_generation: u64,
    queue_drained: u32,
    in_flight: u32,
    last_durable_state_hash: [u8; 32],
    join_proven: bool,
}
impl FenceRecoveryWitnessV1 {
    pub(crate) fn new(
        operation_id: u64,
        fence_generation: u64,
        queue_drained: u32,
        in_flight: u32,
        last_durable_state_hash: [u8; 32],
        join_proven: bool,
    ) -> Self {
        Self {
            operation_id,
            fence_generation,
            queue_drained,
            in_flight,
            last_durable_state_hash,
            join_proven,
        }
    }
    pub(crate) fn operation_id(&self) -> u64 {
        self.operation_id
    }
    pub(crate) fn fence_generation(&self) -> u64 {
        self.fence_generation
    }
    pub(crate) fn queue_drained(&self) -> u32 {
        self.queue_drained
    }
    pub(crate) fn in_flight(&self) -> u32 {
        self.in_flight
    }
    pub(crate) fn last_durable_state_hash(&self) -> [u8; 32] {
        self.last_durable_state_hash
    }
    pub(crate) fn is_recovery_required(&self) -> bool {
        !self.join_proven
    }
    pub(crate) fn join_witness_hash(&self) -> [u8; 32] {
        let mut h = sha2::Sha256::new();
        h.update(b"asrsub-fence-recovery-witness-v1\0");
        h.update(self.operation_id.to_le_bytes());
        h.finalize().into()
    }
}

#[derive(Clone, Debug)]
pub(crate) enum StateLaneFenceKind {
    Joined(FenceWitnessV1),
    Recovery(FenceRecoveryWitnessV1),
}
#[derive(Clone, Debug)]
pub(crate) struct StateLaneFenceResult {
    pub(crate) outcome: StateLaneFenceKind,
}
impl StateLaneFenceResult {
    pub(crate) fn joined_witness(&self) -> Option<&FenceWitnessV1> {
        if let StateLaneFenceKind::Joined(v) = &self.outcome {
            Some(v)
        } else {
            None
        }
    }
    pub(crate) fn recovery_witness(&self) -> Option<&FenceRecoveryWitnessV1> {
        if let StateLaneFenceKind::Recovery(v) = &self.outcome {
            Some(v)
        } else {
            None
        }
    }
}
