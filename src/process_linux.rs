#![allow(dead_code)]
//! Linux syscall/identity primitive façade.

pub(crate) fn has_pidfd() -> bool {
    cfg!(target_os = "linux")
}
pub(crate) fn has_openat2() -> bool {
    cfg!(target_os = "linux")
}
