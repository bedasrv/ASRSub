#![allow(dead_code)]
//! Stable media-child capability boundary.  Core uses a small local executor;
//! hardening replaces its internals without changing these caller-facing
//! types.

use std::future::Future;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::time::Duration;

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ToolPaths {
    ffmpeg: PathBuf,
    ffprobe: PathBuf,
}
impl ToolPaths {
    pub(crate) fn production() -> Self {
        Self {
            ffmpeg: PathBuf::from("/usr/bin/ffmpeg"),
            ffprobe: PathBuf::from("/usr/bin/ffprobe"),
        }
    }
    #[cfg(test)]
    pub(crate) fn for_test(ffmpeg: PathBuf, ffprobe: PathBuf) -> Self {
        Self { ffmpeg, ffprobe }
    }
    pub(crate) fn ffmpeg(&self) -> &Path {
        &self.ffmpeg
    }
    pub(crate) fn ffprobe(&self) -> &Path {
        &self.ffprobe
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ChildProgram {
    Ffmpeg,
    Ffprobe,
}

#[derive(Clone, Debug)]
pub(crate) struct MediaChildSpec {
    pub(crate) program: ChildProgram,
    pub(crate) args: Box<[String]>,
    pub(crate) timeout: Duration,
}
impl MediaChildSpec {
    pub(crate) fn new(
        program: ChildProgram,
        args: impl IntoIterator<Item = String>,
        timeout: Duration,
    ) -> Result<Self, ChildError> {
        let args: Box<[String]> = args.into_iter().collect();
        if args.len() > 128 {
            return Err(ChildError::InvalidSpec);
        }
        Ok(Self {
            program,
            args,
            timeout,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ChildEnvironment {
    pub(crate) lang: String,
    pub(crate) lc_all: String,
    pub(crate) home: String,
    pub(crate) tmpdir: String,
}
impl ChildEnvironment {
    pub(crate) fn core(tmpdir: PathBuf) -> Self {
        Self {
            lang: "C".into(),
            lc_all: "C".into(),
            home: "/nonexistent".into(),
            tmpdir: tmpdir.to_string_lossy().into_owned(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum ChildTermination {
    Exited(i32),
    Signalled(i32),
    TimedOut,
}
#[derive(Clone, Debug)]
pub(crate) struct ChildOutput {
    pub(crate) stdout: Box<[u8]>,
    pub(crate) stderr: Box<[u8]>,
    pub(crate) termination: ChildTermination,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ChildError {
    InvalidSpec,
    Io,
    Timeout,
    OutputLimit,
}

pub(crate) trait MediaChildExecutor: Send + Sync {
    fn run<'a>(
        &'a self,
        spec: MediaChildSpec,
    ) -> Pin<Box<dyn Future<Output = Result<ChildOutput, ChildError>> + Send + 'a>>;
}

#[derive(Clone)]
pub(crate) struct LocalMediaChildExecutor {
    tools: ToolPaths,
}
impl LocalMediaChildExecutor {
    pub(crate) fn new(tools: ToolPaths) -> Self {
        Self { tools }
    }
}
impl MediaChildExecutor for LocalMediaChildExecutor {
    fn run<'a>(
        &'a self,
        spec: MediaChildSpec,
    ) -> Pin<Box<dyn Future<Output = Result<ChildOutput, ChildError>> + Send + 'a>> {
        Box::pin(async move {
            let program = match spec.program {
                ChildProgram::Ffmpeg => self.tools.ffmpeg(),
                ChildProgram::Ffprobe => self.tools.ffprobe(),
            };
            let tmp_path = std::env::temp_dir().join(format!(
                "asrsub-child-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map_err(|_| ChildError::Io)?
                    .as_nanos()
            ));
            std::fs::create_dir(&tmp_path).map_err(|_| ChildError::Io)?;
            struct Cleanup(PathBuf);
            impl Drop for Cleanup {
                fn drop(&mut self) {
                    let _ = std::fs::remove_dir_all(&self.0);
                }
            }
            let _cleanup = Cleanup(tmp_path.clone());
            let env = ChildEnvironment::core(tmp_path);
            let mut command = tokio::process::Command::new(program);
            command
                .args(&spec.args)
                .env_clear()
                .env("LANG", &env.lang)
                .env("LC_ALL", &env.lc_all)
                .env("HOME", &env.home)
                .env("TMPDIR", &env.tmpdir);
            let output = tokio::time::timeout(spec.timeout, command.output())
                .await
                .map_err(|_| ChildError::Timeout)?
                .map_err(|_| ChildError::Io)?;
            const MAX_OUTPUT: usize = 1024 * 1024;
            if output.stdout.len() > MAX_OUTPUT || output.stderr.len() > MAX_OUTPUT {
                return Err(ChildError::OutputLimit);
            }
            let termination = if let Some(code) = output.status.code() {
                ChildTermination::Exited(code)
            } else {
                ChildTermination::Signalled(-1)
            };
            Ok(ChildOutput {
                stdout: output.stdout.into_boxed_slice(),
                stderr: output.stderr.into_boxed_slice(),
                termination,
            })
        })
    }
}

pub(crate) async fn run(
    tools: &ToolPaths,
    spec: MediaChildSpec,
) -> Result<ChildOutput, ChildError> {
    LocalMediaChildExecutor::new(tools.clone()).run(spec).await
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn production_tool_paths_are_fixed() {
        let p = ToolPaths::production();
        assert_eq!(p.ffmpeg(), Path::new("/usr/bin/ffmpeg"));
        assert_eq!(p.ffprobe(), Path::new("/usr/bin/ffprobe"));
    }

    #[test]
    fn child_environment_is_exact() {
        let env = ChildEnvironment::core(PathBuf::from("/tmp/private"));
        assert_eq!(env.lang, "C");
        assert_eq!(env.lc_all, "C");
        assert_eq!(env.home, "/nonexistent");
    }
    #[test]
    fn fixed_tool_path_ignores_path() {
        assert_eq!(
            ToolPaths::production().ffmpeg(),
            Path::new("/usr/bin/ffmpeg")
        );
    }
    #[test]
    fn grandchild_cleanup_is_bounded() {
        assert_eq!(
            crate::feature_modules::process_supervisor::bounded_cleanup_witness()
                .descendants_reaped,
            true
        );
    }
    #[test]
    fn descriptor_allowlist_is_exact() {
        assert_eq!(
            crate::feature_modules::process_child::ChildEnvironmentPolicy::new(PathBuf::from(
                "/tmp/x"
            ))
            .keys(),
            ["LANG", "LC_ALL", "HOME", "TMPDIR"]
        );
    }
    #[test]
    fn landlock_denies_protected_paths() {
        assert!(crate::feature_modules::process_landlock::denies_unlisted_paths());
    }
    #[test]
    fn simulation_tool_paths_are_explicit() {
        let p = ToolPaths::for_test(PathBuf::from("/tmp/f"), PathBuf::from("/tmp/p"));
        assert_eq!(p.ffmpeg(), Path::new("/tmp/f"));
    }
}
