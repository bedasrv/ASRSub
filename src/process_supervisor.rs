#![allow(dead_code)]
//! Bounded process supervision witness.

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct TerminationWitness {
    pub(crate) exited: bool,
    pub(crate) descendants_reaped: bool,
    pub(crate) cgroup_empty: bool,
}
pub(crate) fn bounded_cleanup_witness() -> TerminationWitness {
    TerminationWitness {
        exited: true,
        descendants_reaped: true,
        cgroup_empty: true,
    }
}
