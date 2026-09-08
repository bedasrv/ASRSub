//! Durable JSONL ledgers: state, subtitle registry, actions, exclusions.
//!
//! All writers are append-or-atomic-replace and hold an `flock` on a `.lock`
//! sidecar so the daemon, the control API, and one-shot CLIs never interleave
//! partial rows. Reads tolerate garbage lines (crash-safe tails).

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use fd_lock::RwLock;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct StateEntry {
    #[serde(rename = "sonarrEpisodeId", default)]
    pub episode_id: Option<i64>,
    #[serde(default)]
    pub language: Option<String>,
    #[serde(default)]
    pub status: Option<String>,
    #[serde(default)]
    pub kind: Option<String>,
    #[serde(default)]
    pub ts: Option<String>,
    #[serde(flatten)]
    pub extra: HashMap<String, serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RegistryRow {
    #[serde(default)]
    pub stem: Option<String>,
    #[serde(default)]
    pub lang: Option<String>,
    #[serde(default)]
    pub episode_id: Option<i64>,
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub source_kind: Option<String>,
    #[serde(default)]
    pub source_path: Option<String>,
    #[serde(default)]
    pub source_hash: Option<String>,
    #[serde(default)]
    pub target_path: Option<String>,
    #[serde(default)]
    pub target_hash: Option<String>,
    #[serde(default)]
    pub audio_id: Option<String>,
    #[serde(default)]
    pub ts: Option<String>,
    #[serde(flatten)]
    pub extra: HashMap<String, serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ActionRecord {
    #[serde(default)]
    pub ts: Option<String>,
    #[serde(alias = "action", default)]
    pub r#type: Option<String>,
    #[serde(default)]
    pub episode_id: Option<i64>,
    #[serde(default)]
    pub language: Option<String>,
    /// "series" (default) or "movie".
    #[serde(default)]
    pub kind: Option<String>,
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub note: Option<String>,
}

fn lock_for(path: &Path) -> PathBuf {
    let mut s = path.as_os_str().to_owned();
    s.push(".lock");
    PathBuf::from(s)
}

/// Read JSONL dicts, skipping blank/garbage lines.
pub fn load_jsonl<T>(path: &Path) -> Vec<T>
where
    T: for<'de> Deserialize<'de>,
{
    let Ok(text) = std::fs::read_to_string(path) else {
        return Vec::new();
    };
    text.lines()
        .filter_map(|l| {
            let l = l.trim();
            if l.is_empty() {
                return None;
            }
            serde_json::from_str::<T>(l).ok()
        })
        .collect()
}

/// Append one JSON row (with fresh `ts` when the struct carries one via
/// `extra` or a top-level field the caller set).
pub fn append_jsonl<T>(path: &Path, value: &T) -> Result<()>
where
    T: Serialize,
{
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }
    let lock_path = lock_for(path);
    // Ensure the lock file exists without truncating the ledger.
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&lock_path)
        .with_context(|| format!("open lock {lock_path:?}"))?;
    let lock_file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&lock_path)?;
    let mut guard = RwLock::new(lock_file);
    let _w = guard.write()?;
    let mut line = serde_json::to_string(value)?;
    line.push('\n');
    use std::io::Write;
    let mut fh = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    fh.write_all(line.as_bytes())?;
    fh.sync_data()?;
    Ok(())
}

/// Atomic full rewrite (tmp + rename) under the write lock.
pub fn rewrite_jsonl<T>(path: &Path, rows: &[T]) -> Result<()>
where
    T: Serialize,
{
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }
    let lock_path = lock_for(path);
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&lock_path)?;
    let lock_file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&lock_path)?;
    let mut guard = RwLock::new(lock_file);
    let _w = guard.write()?;
    let tmp = path.with_extension("jsonl.tmp");
    let mut buf = String::new();
    for r in rows {
        buf.push_str(&serde_json::to_string(r)?);
        buf.push('\n');
    }
    std::fs::write(&tmp, buf)?;
    std::fs::rename(&tmp, path)?;
    Ok(())
}

/// Drain actions.jsonl: read everything, truncate, return records.
///
/// The read and the truncate happen while holding the same exclusive sidecar
/// lock that `append_jsonl`/`rewrite_jsonl` use, so rows appended concurrently
/// (e.g. a dashboard Retry arriving mid-pass) are either read in this drain
/// or survive as the new tail — never silently dropped. Malformed lines are
/// skipped; a missing file yields an empty vec.
pub fn consume_actions(path: &Path) -> Vec<ActionRecord> {
    let lock_path = lock_for(path);
    // Best-effort lock-file creation; a missing ledger is not an error.
    let _ = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&lock_path);
    let Ok(lock_file) = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&lock_path)
    else {
        return load_jsonl(path);
    };
    let mut guard = RwLock::new(lock_file);
    let Ok(_w) = guard.write() else {
        return load_jsonl(path);
    };
    let text = std::fs::read_to_string(path).unwrap_or_default();
    let mut rows = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        if let Ok(rec) = serde_json::from_str::<ActionRecord>(line) {
            rows.push(rec);
        }
    }
    // Truncate only what was read: anything appended after our read raced the
    // lock and is already serialized behind us, so it is the new tail.
    // (Appends go through the same lock, hence cannot interleave mid-read.)
    if path.exists() {
        let _ = std::fs::write(path, "");
    }
    rows
}

/// Delete registry rows for one sidecar: matches `(stem, lang)` rows and
/// legacy `(episode_id, lang)` rows. Language comparison uses the same
/// `normalize_lang` contract as the rest of the pipeline, so deleting `id`
/// also drops legacy `ind` rows (likewise `ja`/`jpn`, `en`/`eng`).
/// Returns true when rows were removed.
pub fn registry_delete(
    path: &Path,
    stem: Option<&str>,
    episode_id: Option<i64>,
    lang: &str,
) -> bool {
    let rows: Vec<RegistryRow> = load_jsonl(path);
    if rows.is_empty() {
        return false;
    }
    let norm = crate::lang::normalize_lang(lang);
    let before = rows.len();
    let kept: Vec<RegistryRow> = rows
        .into_iter()
        .filter(|r| {
            let rlang = crate::lang::normalize_lang(r.lang.as_deref().unwrap_or(""));
            if rlang != norm {
                return true;
            }
            if let Some(s) = stem {
                if r.stem.as_deref() == Some(s) {
                    return false;
                }
            }
            if let Some(eid) = episode_id {
                if r.episode_id == Some(eid) {
                    return false;
                }
            }
            true
        })
        .collect();
    if kept.len() == before {
        return false;
    }
    rewrite_jsonl(path, &kept).is_ok()
}

pub fn parse_exclusions(path: &Path) -> HashSet<i64> {
    #[derive(Deserialize)]
    struct Excl {
        #[serde(default)]
        episode_id: Option<i64>,
    }
    load_jsonl::<Excl>(path)
        .into_iter()
        .filter_map(|e| e.episode_id)
        .collect()
}

pub fn utc_now_iso() -> String {
    // No chrono dependency: derive UTC ISO8601 from system time via a tiny
    // days/civil-date conversion (valid for 1970..2106).
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let (y, mo, d, h, mi, s) = unix_to_ymd_hms(secs);
    format!("{y:04}-{mo:02}-{d:02}T{h:02}:{mi:02}:{s:02}Z")
}

fn unix_to_ymd_hms(secs: u64) -> (i32, u32, u32, u32, u32, u32) {
    let days = (secs / 86_400) as i64;
    let tod = (secs % 86_400) as u32;
    // Howard Hinnant's civil_from_days.
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let y = if m <= 2 { y + 1 } else { y } as i32;
    (y, m, d, tod / 3600, (tod % 3600) / 60, tod % 60)
}

pub fn sha256_bytes(data: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    hex::encode(Sha256::digest(data))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn append_and_load_roundtrip() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("s.jsonl");
        append_jsonl(&p, &serde_json::json!({"a": 1})).unwrap();
        std::fs::write(&p, "{\"a\": 1}\nnot json\n\n{\"a\": 2}\n").unwrap();
        let v: Vec<serde_json::Value> = load_jsonl(&p);
        assert_eq!(v.len(), 2);
    }

    #[test]
    fn consume_drains_and_truncates_atomically() {
        // Tail-preserving drain: records present at read time are returned
        // exactly once; garbage lines are skipped; later appends survive.
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("actions.jsonl");
        std::fs::write(
            &p,
            "{\"type\": \"retry\", \"episode_id\": 7}\nGARBAGE\n\n{\"action\": \"skip\", \"episode_id\": 9, \"kind\": \"movie\"}\n",
        )
        .unwrap();
        let rows = consume_actions(&p);
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].r#type.as_deref(), Some("retry"));
        assert_eq!(rows[0].episode_id, Some(7));
        // `action` is accepted as a `type` alias; kind defaults downstream.
        assert_eq!(rows[1].r#type.as_deref(), Some("skip"));
        assert_eq!(rows[1].kind.as_deref(), Some("movie"));
        // Drained file is empty now.
        assert_eq!(std::fs::read_to_string(&p).unwrap(), "");
        // A post-drain append is returned by the next drain (never lost).
        append_jsonl(&p, &serde_json::json!({"type": "delete", "episode_id": 11})).unwrap();
        let rows2 = consume_actions(&p);
        assert_eq!(rows2.len(), 1);
        assert_eq!(rows2[0].episode_id, Some(11));
    }

    #[test]
    fn registry_delete_scopes_by_stem_lang_and_episode() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("reg.jsonl");
        let row = |stem: &str, lang: &str, ep: Option<i64>| serde_json::json!({"stem": stem, "lang": lang, "episode_id": ep});
        for r in [
            row("/m/a", "id", Some(1)),
            row("/m/a", "en", Some(1)),
            row("/m/b", "id", Some(2)),
            row("/m/a", "ind", Some(1)),
            row("/m/a", "jpn", Some(1)),
        ] {
            append_jsonl(&p, &r).unwrap();
        }
        // Alias normalization: deleting `id` drops legacy `ind` rows too.
        assert!(registry_delete(&p, Some("/m/a"), None, "id"));
        let kept: Vec<RegistryRow> = load_jsonl(&p);
        assert_eq!(kept.len(), 3);
        assert!(kept.iter().all(|r| r.lang.as_deref() != Some("ind")));
        // …and `ja` drops legacy `jpn` rows.
        assert!(registry_delete(&p, Some("/m/a"), None, "ja"));
        let kept: Vec<RegistryRow> = load_jsonl(&p);
        assert_eq!(kept.len(), 2);
        // Legacy episode-keyed rows match by episode id.
        assert!(registry_delete(&p, None, Some(2), "id"));
        let kept: Vec<RegistryRow> = load_jsonl(&p);
        assert_eq!(kept.len(), 1);
        assert_eq!(kept[0].lang.as_deref(), Some("en"));
        // No match → no rewrite, false.
        assert!(!registry_delete(&p, Some("/m/zzz"), Some(99), "id"));
    }

    #[test]
    fn state_entry_kind_roundtrip() {
        // Movies carry kind="movie" so numeric ids never collide with series.
        let e: StateEntry = serde_json::from_value(
            serde_json::json!({"sonarrEpisodeId": 5, "language": "id", "status": "done", "kind": "movie"}),
        )
        .unwrap();
        assert_eq!(e.kind.as_deref(), Some("movie"));
        let e2: StateEntry = serde_json::from_value(
            serde_json::json!({"sonarrEpisodeId": 5, "language": "id", "status": "done"}),
        )
        .unwrap();
        assert_eq!(e2.kind, None);
    }
}
