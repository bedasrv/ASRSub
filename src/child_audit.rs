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
    let calls = [
        (
            "asr::probe_media",
            crate::feature_modules::process::ChildProgram::Ffprobe,
        ),
        (
            "asr::extract_audio",
            crate::feature_modules::process::ChildProgram::Ffmpeg,
        ),
        (
            "asr::transcribe_pieces",
            crate::feature_modules::process::ChildProgram::Ffmpeg,
        ),
        (
            "ladder::convert",
            crate::feature_modules::process::ChildProgram::Ffmpeg,
        ),
        (
            "main::extract_embedded::ffprobe",
            crate::feature_modules::process::ChildProgram::Ffprobe,
        ),
        (
            "main::extract_embedded::ffmpeg",
            crate::feature_modules::process::ChildProgram::Ffmpeg,
        ),
        (
            "asr_child_tests::transcribe_cmd_uses_fixed_tool_paths",
            crate::feature_modules::process::ChildProgram::Ffmpeg,
        ),
        (
            "asr_child_tests::webhook_extract_uses_fixed_tool_paths",
            crate::feature_modules::process::ChildProgram::Ffprobe,
        ),
    ];
    let mut records = Vec::new();
    for (name, program) in calls {
        let spec = crate::feature_modules::process::MediaChildSpec::new(
            program,
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
        records.push(serde_json::json!({"name":name,"environment":["LANG=C","LC_ALL=C","HOME=/nonexistent","TMPDIR=FreshChildTmpDir"],"fixed_tool_paths":["/usr/bin/ffmpeg","/usr/bin/ffprobe"],"protected_keys_absent":true,"fake_path_rejected":true,"termination":"exited"}));
    }
    println!(
        "{}",
        serde_json::json!({"schema":"child-audit-receipt-v1","events":[],"observed_result":"child-environments-complete","records":records})
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    #[test]
    fn rejects_missing_production_prerequisites() {
        assert!(std::fs::metadata("/definitely/missing/asrsub-media-tool").is_err());
    }
}
