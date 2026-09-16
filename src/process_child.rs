#![allow(dead_code)]
//! Exact child environment and argument policy.

use std::path::PathBuf;

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ChildEnvironmentPolicy {
    pub(crate) lang: &'static str,
    pub(crate) lc_all: &'static str,
    pub(crate) home: &'static str,
    pub(crate) tmpdir: PathBuf,
}

impl ChildEnvironmentPolicy {
    pub(crate) fn new(tmpdir: PathBuf) -> Self {
        Self {
            lang: "C",
            lc_all: "C",
            home: "/nonexistent",
            tmpdir,
        }
    }
    pub(crate) fn keys(&self) -> [&'static str; 4] {
        ["LANG", "LC_ALL", "HOME", "TMPDIR"]
    }
}
