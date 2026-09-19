use std::path::Path;

use anyhow::{Context, Result};
use sha2::{Digest, Sha256};

use crate::feature_modules::discord_text::SafeDisplayText;
use crate::feature_modules::discord_types::{
    FailureClass, GenerationMethod, TargetLanguage, TargetRunResult, TargetStatus,
};
use crate::feature_modules::pipeline_commit::{
    commit_target_ledgers, CommitLedgerError, CommitWitness, LedgerCommitRequest, LedgerIdentity,
    LedgerPaths,
};
use crate::lang::normalize_lang;
use crate::pipeline::Candidate;
use crate::state::{self, RegistryRow, StateEntry};

/// Registry commit payload: one struct instead of positional args.
pub(super) struct RegistryCommit<'a> {
    pub(super) stem: &'a str,
    pub(super) lang: &'a str,
    pub(super) source: &'a str,
    pub(super) source_kind: Option<&'a str>,
    pub(super) episode_id: Option<i64>,
    pub(super) kind: &'a str,
    pub(super) media_path: &'a str,
    /// Effective source language (ASR tag/detected, or ladder source) and
    /// the chosen audio stream for ASR rows — the provenance that makes a
    /// `fr`-sourced row distinguishable from a `ja`-sourced one.
    pub(super) source_lang: &'a str,
    pub(super) source_stream: Option<u32>,
}

#[derive(Debug)]
pub(super) struct TargetFailure {
    pub(super) class: FailureClass,
    pub(super) error: anyhow::Error,
}

impl TargetFailure {
    fn new(class: FailureClass, error: anyhow::Error) -> Self {
        Self { class, error }
    }

    pub(super) fn translation(error: anyhow::Error) -> Self {
        Self::new(FailureClass::Translation, error)
    }

    pub(super) fn storage_error(error: anyhow::Error) -> Self {
        Self::new(FailureClass::Storage, error)
    }

    fn storage(error: CommitLedgerError) -> Self {
        Self::storage_error(anyhow::anyhow!("target ledger commit failed: {error:?}"))
    }
}

pub(super) fn publish_target_ledgers(
    paths: LedgerPaths,
    identity: LedgerIdentity,
    registry_row: serde_json::Value,
    state_row: serde_json::Value,
) -> std::result::Result<CommitWitness, TargetFailure> {
    commit_target_ledgers(LedgerCommitRequest::TargetLedgerCommit {
        paths,
        identity,
        registry_row,
        state_row,
    })
    .map_err(TargetFailure::storage)
}

pub(super) fn target_result(
    lang: &str,
    status: TargetStatus,
    artifact_sha256: Option<[u8; 32]>,
) -> Result<TargetRunResult> {
    target_result_with_method(lang, status, artifact_sha256, None)
}

pub(super) fn target_result_with_method(
    lang: &str,
    status: TargetStatus,
    artifact_sha256: Option<[u8; 32]>,
    generation_method: Option<GenerationMethod>,
) -> Result<TargetRunResult> {
    let language = TargetLanguage::parse(lang)
        .map_err(|error| anyhow::anyhow!("invalid target language {lang:?}: {error:?}"))?;
    TargetRunResult::try_new_with_method(language, status, artifact_sha256, generation_method)
        .map_err(|error| anyhow::anyhow!("invalid target result for {lang:?}: {error:?}"))
}

pub(super) fn validated_report_title(raw: &str) -> Result<SafeDisplayText> {
    SafeDisplayText::sanitize(raw)
        .map_err(|error| anyhow::anyhow!("invalid report title: {error:?}"))
}

pub(super) fn digest_bytes(bytes: &[u8]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hasher.finalize().into()
}

#[derive(Debug)]
pub(super) struct InstalledSidecar {
    target: std::path::PathBuf,
    pub(super) previous: Option<Vec<u8>>,
    pub(super) artifact_sha256: [u8; 32],
}

pub(super) fn install_sidecar(target: &Path, bytes: &[u8]) -> Result<InstalledSidecar> {
    let previous = match std::fs::read(target) {
        Ok(bytes) => Some(bytes),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => {
            return Err(error).with_context(|| format!("read existing sidecar {target:?}"))
        }
    };
    if let Some(parent) = target.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }
    let tmp = Path::new(&format!("{}.direct.tmp", target.display())).to_path_buf();
    std::fs::write(&tmp, bytes)?;
    if let Err(error) = std::fs::rename(&tmp, target) {
        let _ = std::fs::remove_file(&tmp);
        return Err(error.into());
    }
    Ok(InstalledSidecar {
        target: target.to_path_buf(),
        previous,
        artifact_sha256: digest_bytes(bytes),
    })
}

impl InstalledSidecar {
    pub(super) fn still_current(&self) -> Result<bool> {
        match std::fs::read(&self.target) {
            Ok(bytes) => Ok(digest_bytes(&bytes) == self.artifact_sha256),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(error) => Err(error.into()),
        }
    }

    /// Remove or restore only while the target still contains this install.
    /// A concurrent replacement is left untouched.
    pub(super) fn rollback_if_unchanged(&self) -> Result<()> {
        let current = match std::fs::read(&self.target) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(error.into()),
        };
        if digest_bytes(&current) != self.artifact_sha256 {
            return Ok(());
        }
        if let Some(previous) = &self.previous {
            let tmp = Path::new(&format!("{}.rollback.tmp", self.target.display())).to_path_buf();
            std::fs::write(&tmp, previous)?;
            let still_current = match std::fs::read(&self.target) {
                Ok(bytes) => digest_bytes(&bytes) == self.artifact_sha256,
                Err(_) => false,
            };
            if !still_current {
                let _ = std::fs::remove_file(&tmp);
                return Ok(());
            }
            if let Err(error) = std::fs::rename(&tmp, &self.target) {
                let _ = std::fs::remove_file(&tmp);
                return Err(error.into());
            }
        } else {
            let still_current = match std::fs::read(&self.target) {
                Ok(bytes) => digest_bytes(&bytes) == self.artifact_sha256,
                Err(_) => false,
            };
            if still_current {
                match std::fs::remove_file(&self.target) {
                    Ok(()) => {}
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                    Err(error) => return Err(error.into()),
                }
            }
        }
        Ok(())
    }
}

pub(super) fn existing_target_digest(target_path: &Path) -> Result<[u8; 32]> {
    let bytes = std::fs::read(target_path)
        .with_context(|| format!("read target sidecar {}", target_path.display()))?;
    Ok(digest_bytes(&bytes))
}

pub(super) fn target_is_verified(
    cfg: &crate::config::Config,
    candidate: &Candidate,
    lang: &str,
) -> Option<crate::pipeline::VerifiedTarget> {
    let kind = if candidate.is_movie {
        "movie"
    } else {
        "series"
    };
    let key = (kind.to_string(), candidate.episode_id, normalize_lang(lang));
    crate::pipeline::verified_targets(
        &state::load_jsonl::<RegistryRow>(&cfg.registry_file),
        &state::load_jsonl::<StateEntry>(&cfg.state_file),
    )
    .remove(&key)
}

pub(super) fn target_ledger_rows(
    commit: &RegistryCommit<'_>,
    artifact_sha256: [u8; 32],
) -> Result<(LedgerIdentity, serde_json::Value, serde_json::Value)> {
    let episode_id = commit
        .episode_id
        .context("target ledger missing episode id")?;
    let language = normalize_lang(commit.lang);
    let digest = crate::feature_modules::discord_state_codec::hex(&artifact_sha256);
    let target = crate::lang::canonical_target_sidecar(commit.stem, commit.lang);
    let timestamp = state::utc_now_iso();

    let mut registry_extra = std::collections::HashMap::new();
    registry_extra.insert(
        "media_path".to_string(),
        serde_json::Value::String(commit.media_path.to_string()),
    );
    registry_extra.insert(
        "source_lang".to_string(),
        serde_json::Value::String(normalize_lang(commit.source_lang)),
    );
    registry_extra.insert(
        "artifact_sha256".to_string(),
        serde_json::Value::String(digest.clone()),
    );
    if let Some(stream) = commit.source_stream {
        registry_extra.insert("source_stream".to_string(), serde_json::Value::from(stream));
    }
    if commit.kind == "movie" {
        registry_extra.insert(
            "kind".to_string(),
            serde_json::Value::String("movie".to_string()),
        );
    }
    let registry_row = RegistryRow {
        stem: Some(commit.stem.to_string()),
        lang: Some(language.clone()),
        episode_id: Some(episode_id),
        source: Some(commit.source.to_string()),
        source_kind: commit.source_kind.map(str::to_string),
        source_path: Some(target.clone()),
        target_path: Some(target),
        ts: Some(timestamp.clone()),
        extra: registry_extra,
    };

    let mut state_extra = std::collections::HashMap::new();
    state_extra.insert(
        "artifact_sha256".to_string(),
        serde_json::Value::String(digest),
    );
    state_extra.insert(
        "detail".to_string(),
        serde_json::Value::String(String::new()),
    );
    let state_row = StateEntry {
        episode_id: Some(episode_id),
        language: Some(language.clone()),
        status: Some("done".to_string()),
        kind: (commit.kind == "movie").then(|| "movie".to_string()),
        ts: Some(timestamp),
        extra: state_extra,
    };
    let identity = LedgerIdentity {
        kind: commit.kind.to_string(),
        episode_id,
        language,
        artifact_sha256,
    };
    Ok((
        identity,
        serde_json::to_value(registry_row)?,
        serde_json::to_value(state_row)?,
    ))
}

pub(super) fn cleanup_failed_install(
    cfg: &crate::config::Config,
    candidate: &Candidate,
    lang: &str,
    installed: &InstalledSidecar,
) -> Result<()> {
    if let Some(verified) = target_is_verified(cfg, candidate, lang) {
        if verified.artifact_sha256 == installed.artifact_sha256 {
            return Ok(());
        }
    }
    installed.rollback_if_unchanged()
}
