#![allow(dead_code)]
use std::path::Path;
pub(crate) fn atomic_write(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let t = path.with_extension("tmp");
    std::fs::write(&t, bytes)?;
    std::fs::rename(t, path)
}
