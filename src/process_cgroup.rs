#![allow(dead_code)]
//! cgroup-v2 capability operations.

pub(crate) fn delegated_leaf_token() -> &'static str {
    "/run/asrsub/children-cgroup"
}
pub(crate) fn required_controllers() -> [&'static str; 3] {
    ["cpu", "memory", "pids"]
}
