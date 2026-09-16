#![allow(dead_code, clippy::chunks_exact_to_as_chunks)]
use super::discord_fs::{ProductionStateRoot, ProductionStateStore};
use super::discord_state_schema::StateSnapshotBytes;

fn hash_bytes(value: &str) -> anyhow::Result<[u8; 32]> {
    if value.len() != 64 {
        anyhow::bail!("invalid state hash");
    }
    let mut output = [0; 32];
    for (index, pair) in value.as_bytes().chunks_exact(2).enumerate() {
        output[index] = u8::from_str_radix(std::str::from_utf8(pair)?, 16)?;
    }
    Ok(output)
}

fn transaction_path(nonce: &str, name: &str) -> std::path::PathBuf {
    std::path::Path::new("/var/lib/asrsub/deploy-state/transactions")
        .join(nonce)
        .join(name)
}
pub(crate) fn run(args: &[String]) -> anyhow::Result<()> {
    let mut it = args.iter().skip(1);
    let operation = it
        .next()
        .ok_or_else(|| anyhow::anyhow!("state operation required"))?;
    let mut nonce = None;
    let mut expected_hash = None;
    let mut expected_generation = None;
    let mut fds = std::collections::HashSet::new();
    while let Some(flag) = it.next() {
        let value = it
            .next()
            .ok_or_else(|| anyhow::anyhow!("state option value required"))?;
        match flag.as_str() {
            "--transaction-nonce" => nonce = Some(value.clone()),
            "--expected-current-state-hash" | "--expected-state-hash" => {
                expected_hash = Some(value.clone())
            }
            "--expected-current-state-generation" => {
                expected_generation = Some(value.parse::<u64>()?)
            }
            "--lock-fd" | "--lock-proof-fd" | "--control-key-fd" => {
                fds.insert(flag.clone());
                value.parse::<i32>()?;
            }
            _ => anyhow::bail!("unknown state option"),
        }
    }
    if let Some(value) = &nonce {
        if !super::deployment_commands::valid_nonce(value) {
            anyhow::bail!("invalid transaction nonce");
        }
    }
    if let Some(value) = &expected_hash {
        if value.len() != 64
            || !value
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
        {
            anyhow::bail!("invalid state hash");
        }
    }
    match operation.as_str() {
        "snapshot" | "post-snapshot" => {
            if nonce.is_none() || fds.len() != 3 {
                anyhow::bail!("authenticated snapshot descriptors required");
            }
        }
        "restore" => {
            if nonce.is_none()
                || expected_hash.is_none()
                || expected_generation.is_none()
                || fds.len() != 3
            {
                anyhow::bail!("authenticated restore arguments required");
            }
        }
        "reset" => {
            if expected_hash.is_none() || (!fds.is_empty() && fds.len() != 3) {
                anyhow::bail!("authenticated reset arguments required");
            }
        }
        _ => anyhow::bail!("unknown state operation"),
    }
    let store = ProductionStateStore::open(ProductionStateRoot::fixed())
        .map_err(|error| anyhow::anyhow!("state store: {error:?}"))?;
    let lane = store.lane();
    match operation.as_str() {
        "snapshot" | "post-snapshot" => {
            let nonce = nonce.as_deref().expect("validated nonce");
            let snapshot = lane
                .snapshot()
                .map_err(|error| anyhow::anyhow!("state snapshot: {error:?}"))?;
            let path = transaction_path(
                nonce,
                if operation == "snapshot" {
                    "state-snapshot.json"
                } else {
                    "state-snapshot-forward.json"
                },
            );
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent)?;
            }
            std::fs::write(path, snapshot.as_bytes())?;
        }
        "restore" => {
            let nonce = nonce.as_deref().expect("validated nonce");
            let path = transaction_path(nonce, "state-snapshot.json");
            let bytes = std::fs::read(path)?;
            let snapshot = StateSnapshotBytes::new(
                bytes.into_boxed_slice(),
                hash_bytes(expected_hash.as_deref().unwrap())?,
            )
            .map_err(|error| anyhow::anyhow!("state snapshot: {error:?}"))?;
            lane.restore(
                snapshot,
                hash_bytes(expected_hash.as_deref().unwrap())?,
                expected_generation.unwrap(),
            )
            .map_err(|error| anyhow::anyhow!("state restore: {error:?}"))?;
        }
        "reset" => {
            lane.reset_delivery(hash_bytes(expected_hash.as_deref().unwrap())?)
                .map_err(|error| anyhow::anyhow!("state reset: {error:?}"))?;
        }
        _ => unreachable!(),
    }
    Ok(())
}
