#![allow(dead_code)]
//! Bounded stdin-to-fixed-target comparison only.

use std::path::Path;

pub(crate) fn compare(target: &Path, stdin: &[u8]) -> Result<bool, &'static str> {
    if stdin.len() > 512 {
        return Err("input-too-large");
    }
    let target_bytes = std::fs::read(target).map_err(|_| "target-read")?;
    if target_bytes.len() > 512 {
        return Err("target-too-large");
    }
    Ok(target_bytes == stdin)
}
