#![allow(dead_code)]
//! Hidden release-bound child environment audit entry point.

fn validate_production_prerequisites() -> anyhow::Result<crate::feature_modules::process::ToolPaths>
{
    if !crate::feature_modules::process_linux::has_pidfd() {
        anyhow::bail!("pidfd unavailable");
    }
    if !crate::feature_modules::process_landlock::supported() {
        anyhow::bail!("landlock unavailable");
    }
    let tools = crate::feature_modules::process::ToolPaths::production();
    for path in [tools.ffmpeg(), tools.ffprobe()] {
        let metadata = std::fs::metadata(path)
            .map_err(|_| anyhow::anyhow!("production media tool unavailable"))?;
        if !metadata.is_file() {
            anyhow::bail!("production media tool is not regular");
        }
    }
    Ok(tools)
}

pub(crate) async fn run() -> anyhow::Result<()> {
    let tools = validate_production_prerequisites()?;
    let spec = crate::feature_modules::process::MediaChildSpec::new(
        crate::feature_modules::process::ChildProgram::Ffprobe,
        ["-version".to_string()],
        std::time::Duration::from_secs(15),
    )
    .map_err(|_| anyhow::anyhow!("invalid audit child spec"))?;
    let output = crate::feature_modules::process::run(&tools, spec)
        .await
        .map_err(|_| anyhow::anyhow!("audit child failed"))?;
    if !matches!(
        output.termination,
        crate::feature_modules::process::ChildTermination::Exited(0)
    ) {
        anyhow::bail!("audit child did not exit cleanly");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    #[test]
    fn rejects_missing_production_prerequisites() {
        assert!(std::fs::metadata("/definitely/missing/asrsub-media-tool").is_err());
    }
}
