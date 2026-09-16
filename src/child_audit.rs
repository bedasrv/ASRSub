#![allow(dead_code)]
//! Hidden release-bound child environment audit entry point.

pub(crate) fn run() -> anyhow::Result<()> {
    if !crate::feature_modules::process_linux::has_pidfd() {
        anyhow::bail!("pidfd unavailable");
    }
    Ok(())
}
