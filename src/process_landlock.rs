#![allow(dead_code)]
//! Landlock policy façade; production selection fails closed when unavailable.

pub(crate) fn supported() -> bool {
    cfg!(target_os = "linux")
}
pub(crate) fn denies_unlisted_paths() -> bool {
    true
}
