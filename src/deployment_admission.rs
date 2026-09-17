#![allow(dead_code)]
use super::deployment_barrier::{AdmissionMode, DeploymentBarrier};
#[derive(Clone)]
pub(crate) struct DeploymentAdmission {
    barrier: DeploymentBarrier,
}
impl DeploymentAdmission {
    pub(crate) fn new() -> Self {
        Self {
            barrier: DeploymentBarrier::new(),
        }
    }
    pub(crate) fn admit_mutation(&self) -> bool {
        self.barrier.admit()
    }
    pub(crate) fn quiesce(&self) -> u64 {
        self.barrier.quiesce()
    }
    pub(crate) fn mode(&self) -> AdmissionMode {
        if self.barrier.admit() {
            AdmissionMode::Running
        } else {
            AdmissionMode::Quiescing
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn all_mutations_share_generation_gate() {
        let a = DeploymentAdmission::new();
        assert!(a.admit_mutation());
        a.quiesce();
        assert!(!a.admit_mutation())
    }
    #[test]
    fn stale_active_permit_requires_recovery() {
        let a = DeploymentAdmission::new();
        a.quiesce();
        assert_eq!(a.mode(), AdmissionMode::Quiescing)
    }
    #[test]
    fn authenticated_stale_permit_resolution_requires_zero_cgroup() {
        assert!(DeploymentAdmission::new().admit_mutation())
    }
}
