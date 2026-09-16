//! Linux no-follow/mount capability façade for ProductionStateStore.

#![allow(dead_code)]

pub(crate) fn supports_openat2() -> bool {
    cfg!(target_os = "linux")
}
pub(crate) fn supports_renameat2() -> bool {
    cfg!(target_os = "linux")
}
pub(crate) fn supports_statx_mount_id() -> bool {
    cfg!(target_os = "linux")
}
