#![allow(dead_code)]
#[derive(Clone, Debug)]
pub(crate) struct JoinWitnessV1 {
    pub(crate) queue_drained: bool,
    pub(crate) state_lane_idle: bool,
    pub(crate) children_empty: bool,
}
impl JoinWitnessV1 {
    pub(crate) fn clean() -> Self {
        Self {
            queue_drained: true,
            state_lane_idle: true,
            children_empty: true,
        }
    }
}
