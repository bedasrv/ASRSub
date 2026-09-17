use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

use crate::lang::normalize_lang;
use crate::state;

/// Radarr's `originalLanguage` for a movie (Bazarr passes the Radarr payload
/// through when it is present). Accepts the usual `{id, name}` object or a
/// bare string; absent/blank degrades to `None` — never a pass failure. Same
/// parser the Sonarr series listing uses (`crate::lang::original_language`).
pub(super) fn movie_original_lang(m: &serde_json::Value) -> Option<String> {
    crate::lang::original_language(m.get("originalLanguage")?)
}

/// A target admitted by a matching, digest-verified registry/state pair.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct VerifiedTarget {
    pub(crate) target_path: PathBuf,
    pub(crate) artifact_sha256: [u8; 32],
}

pub(crate) type TargetKey = (String, i64, String);

fn parse_digest(value: Option<&serde_json::Value>) -> Option<[u8; 32]> {
    let text = value?.as_str()?;
    if text.len() != 64 {
        return None;
    }
    let mut digest = [0u8; 32];
    let (pairs, remainder) = text.as_bytes().as_chunks::<2>();
    if !remainder.is_empty() {
        return None;
    }
    for (index, pair) in pairs.iter().enumerate() {
        digest[index] = u8::from_str_radix(std::str::from_utf8(pair).ok()?, 16).ok()?;
    }
    Some(digest)
}

fn digest_file(path: &Path) -> Option<[u8; 32]> {
    let bytes = std::fs::read(path).ok()?;
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    Some(hasher.finalize().into())
}

fn registry_kind(row: &state::RegistryRow) -> String {
    row.extra
        .get("kind")
        .and_then(|value| value.as_str())
        .unwrap_or("series")
        .to_string()
}

fn state_kind(row: &state::StateEntry) -> String {
    row.kind.as_deref().unwrap_or("series").to_string()
}

/// Return only targets whose registry and done-state rows form one exact
/// identity and whose recorded artifact digest matches the bytes on disk.
pub(crate) fn verified_targets(
    registry_rows: &[state::RegistryRow],
    state_rows: &[state::StateEntry],
) -> std::collections::HashMap<TargetKey, VerifiedTarget> {
    let done: Vec<(TargetKey, [u8; 32])> = state_rows
        .iter()
        .filter(|row| row.status.as_deref() == Some("done"))
        .filter_map(|row| {
            Some((
                (
                    state_kind(row),
                    row.episode_id?,
                    normalize_lang(row.language.as_deref().unwrap_or("")),
                ),
                parse_digest(row.extra.get("artifact_sha256")),
            ))
        })
        .filter_map(|(key, digest)| Some((key, digest?)))
        .collect();

    registry_rows
        .iter()
        .filter_map(|row| {
            let target_path = PathBuf::from(row.target_path.as_deref()?);
            if !target_path.is_file() {
                return None;
            }
            let key = (
                registry_kind(row),
                row.episode_id?,
                normalize_lang(row.lang.as_deref().unwrap_or("")),
            );
            let artifact_sha256 = parse_digest(row.extra.get("artifact_sha256"))?;
            if digest_file(&target_path) != Some(artifact_sha256) {
                return None;
            }
            if !done.iter().any(|(state_key, state_digest)| {
                state_key == &key && state_digest == &artifact_sha256
            }) {
                return None;
            }
            Some((
                key,
                VerifiedTarget {
                    target_path,
                    artifact_sha256,
                },
            ))
        })
        .collect()
}
