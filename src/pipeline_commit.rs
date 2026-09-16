#![allow(
    dead_code,
    clippy::chunks_exact_to_as_chunks,
    clippy::suspicious_open_options
)]
//! Strict per-target pipeline ledger commit protocol.

use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};

use fd_lock::RwLock;
use sha2::{Digest, Sha256};

#[derive(Clone, Debug)]
pub(crate) struct LedgerPaths {
    pub(crate) registry: PathBuf,
    pub(crate) state: PathBuf,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct LedgerIdentity {
    pub(crate) kind: String,
    pub(crate) episode_id: i64,
    pub(crate) language: String,
    pub(crate) artifact_sha256: [u8; 32],
}
#[derive(Clone, Debug)]
pub(crate) enum LedgerCommitRequest {
    TargetLedgerCommit {
        paths: LedgerPaths,
        identity: LedgerIdentity,
        registry_row: serde_json::Value,
        state_row: serde_json::Value,
    },
    ErrorMarkerCommit {
        state_path: PathBuf,
        marker_identity: LedgerIdentity,
        state_row: serde_json::Value,
    },
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum CommitLedgerError {
    Io,
    Storage,
    Contradiction,
    InvalidInput,
}
#[derive(Clone, Debug)]
pub(crate) struct CommitWitness {
    pipeline_commit_id: String,
    report_hash: [u8; 32],
}
impl CommitWitness {
    pub(crate) fn pipeline_commit_id(&self) -> &str {
        &self.pipeline_commit_id
    }
    pub(crate) fn report_hash(&self) -> [u8; 32] {
        self.report_hash
    }
}

pub(crate) fn commit_target_ledgers(
    request: LedgerCommitRequest,
) -> Result<CommitWitness, CommitLedgerError> {
    match request {
        LedgerCommitRequest::TargetLedgerCommit {
            paths,
            identity,
            registry_row,
            state_row,
        } => commit_pair(paths, identity, registry_row, state_row),
        LedgerCommitRequest::ErrorMarkerCommit {
            state_path,
            marker_identity,
            state_row,
        } => commit_one(&state_path, &marker_identity, state_row),
    }
}

fn lock_path(path: &Path) -> PathBuf {
    let mut s = path.as_os_str().to_os_string();
    s.push(".lock");
    PathBuf::from(s)
}
fn open_lock(path: &Path) -> Result<std::fs::File, CommitLedgerError> {
    if let Some(p) = path.parent() {
        std::fs::create_dir_all(p).map_err(|_| CommitLedgerError::Io)?;
    }
    std::fs::OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .open(lock_path(path))
        .map_err(|_| CommitLedgerError::Io)
}
fn canonical_line(value: &serde_json::Value) -> Result<Vec<u8>, CommitLedgerError> {
    let mut s = serde_json::to_string(value).map_err(|_| CommitLedgerError::InvalidInput)?;
    s.push('\n');
    Ok(s.into_bytes())
}
fn digest(value: &serde_json::Value) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update(serde_json::to_vec(value).unwrap_or_default());
    h.finalize().into()
}
fn commit_pair(
    paths: LedgerPaths,
    identity: LedgerIdentity,
    registry_row: serde_json::Value,
    state_row: serde_json::Value,
) -> Result<CommitWitness, CommitLedgerError> {
    let (first, second) = if paths.registry <= paths.state {
        (&paths.registry, &paths.state)
    } else {
        (&paths.state, &paths.registry)
    };
    let l1 = open_lock(first)?;
    let l2 = open_lock(second)?;
    let mut g1 = RwLock::new(l1);
    let mut g2 = RwLock::new(l2);
    let _a = g1.write().map_err(|_| CommitLedgerError::Io)?;
    let _b = g2.write().map_err(|_| CommitLedgerError::Io)?;
    append_if_absent(&paths.registry, &identity, &registry_row)?;
    append_if_absent(&paths.state, &identity, &state_row)?;
    let report_hash = digest(&registry_row);
    let mut h = Sha256::new();
    h.update(b"asrsub-pipeline-v1\0");
    h.update(report_hash);
    Ok(CommitWitness {
        pipeline_commit_id: format!("asrsub-pipeline-v1-{}", hex(&h.finalize())),
        report_hash,
    })
}
fn commit_one(
    path: &Path,
    identity: &LedgerIdentity,
    row: serde_json::Value,
) -> Result<CommitWitness, CommitLedgerError> {
    let lock = open_lock(path)?;
    let mut guard = RwLock::new(lock);
    let _w = guard.write().map_err(|_| CommitLedgerError::Io)?;
    append_if_absent(path, identity, &row)?;
    let report_hash = digest(&row);
    let mut h = Sha256::new();
    h.update(b"asrsub-pipeline-v1\0");
    h.update(report_hash);
    Ok(CommitWitness {
        pipeline_commit_id: format!("asrsub-pipeline-v1-{}", hex(&h.finalize())),
        report_hash,
    })
}
fn append_if_absent(
    path: &Path,
    identity: &LedgerIdentity,
    row: &serde_json::Value,
) -> Result<(), CommitLedgerError> {
    if let Some(p) = path.parent() {
        std::fs::create_dir_all(p).map_err(|_| CommitLedgerError::Io)?;
    }
    let mut found = None;
    if let Ok(file) = std::fs::File::open(path) {
        for line in std::io::BufReader::new(file).lines() {
            let line = line.map_err(|_| CommitLedgerError::Io)?;
            let Ok(value) = serde_json::from_str::<serde_json::Value>(&line) else {
                return Err(CommitLedgerError::Storage);
            };
            if row_identity(&value)
                .map(|i| i == *identity)
                .unwrap_or(false)
            {
                found = Some(value);
            }
        }
    }
    if let Some(existing) = found {
        return if existing == *row {
            Ok(())
        } else {
            Err(CommitLedgerError::Contradiction)
        };
    }
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|_| CommitLedgerError::Io)?;
    f.write_all(&canonical_line(row)?)
        .map_err(|_| CommitLedgerError::Io)?;
    f.sync_data().map_err(|_| CommitLedgerError::Io)?;
    Ok(())
}
fn row_identity(value: &serde_json::Value) -> Option<LedgerIdentity> {
    let o = value.as_object()?;
    let kind = o
        .get("kind")
        .and_then(|v| v.as_str())
        .unwrap_or("series")
        .to_string();
    let id = o
        .get("episode_id")
        .or_else(|| o.get("sonarrEpisodeId"))?
        .as_i64()?;
    let language = o
        .get("language")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    let d = o
        .get("artifact_sha256")
        .and_then(|v| v.as_str())
        .and_then(parse_hex)?;
    Some(LedgerIdentity {
        kind,
        episode_id: id,
        language,
        artifact_sha256: d,
    })
}
fn parse_hex(s: &str) -> Option<[u8; 32]> {
    if s.len() != 64 {
        return None;
    }
    let mut d = [0; 32];
    for (i, p) in s.as_bytes().chunks_exact(2).enumerate() {
        d[i] = u8::from_str_radix(std::str::from_utf8(p).ok()?, 16).ok()?;
    }
    Some(d)
}
fn hex(bytes: &[u8]) -> String {
    const H: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(H[(b >> 4) as usize] as char);
        s.push(H[(b & 15) as usize] as char);
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;
    fn id() -> LedgerIdentity {
        LedgerIdentity {
            kind: "series".into(),
            episode_id: 7,
            language: "id".into(),
            artifact_sha256: [3; 32],
        }
    }
    fn row() -> serde_json::Value {
        serde_json::json!({"kind":"series","episode_id":7,"language":"id","artifact_sha256":"0303030303030303030303030303030303030303030303030303030303030303"})
    }
    #[test]
    fn paired_ledger_commit_is_idempotent() {
        let d = tempfile::tempdir().unwrap();
        let p = LedgerPaths {
            registry: d.path().join("r"),
            state: d.path().join("s"),
        };
        commit_target_ledgers(LedgerCommitRequest::TargetLedgerCommit {
            paths: p.clone(),
            identity: id(),
            registry_row: row(),
            state_row: row(),
        })
        .unwrap();
        commit_target_ledgers(LedgerCommitRequest::TargetLedgerCommit {
            paths: p.clone(),
            identity: id(),
            registry_row: row(),
            state_row: row(),
        })
        .unwrap();
        assert_eq!(
            std::fs::read_to_string(p.registry).unwrap().lines().count(),
            1
        );
    }
    #[test]
    fn paired_ledger_conflict_is_storage() {
        let d = tempfile::tempdir().unwrap();
        let p = LedgerPaths {
            registry: d.path().join("r"),
            state: d.path().join("s"),
        };
        commit_target_ledgers(LedgerCommitRequest::TargetLedgerCommit {
            paths: p.clone(),
            identity: id(),
            registry_row: row(),
            state_row: row(),
        })
        .unwrap();
        let mut bad = row();
        bad["source"] = serde_json::json!("other");
        assert_eq!(
            commit_target_ledgers(LedgerCommitRequest::TargetLedgerCommit {
                paths: p,
                identity: id(),
                registry_row: bad,
                state_row: row()
            })
            .unwrap_err(),
            CommitLedgerError::Contradiction
        );
    }
}
