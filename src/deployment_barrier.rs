#![allow(dead_code)]
use std::sync::{Arc, Mutex};
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum AdmissionMode {
    Running,
    Quiescing,
    RecoveryRequired,
}
#[derive(Clone)]
pub(crate) struct DeploymentBarrier {
    inner: Arc<Mutex<(AdmissionMode, u64)>>,
}
impl DeploymentBarrier {
    pub(crate) fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new((AdmissionMode::Running, 0))),
        }
    }
    pub(crate) fn admit(&self) -> bool {
        self.inner.lock().unwrap().0 == AdmissionMode::Running
    }
    pub(crate) fn quiesce(&self) -> u64 {
        let mut x = self.inner.lock().unwrap();
        x.0 = AdmissionMode::Quiescing;
        x.1 += 1;
        x.1
    }
}
