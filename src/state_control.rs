#![allow(dead_code)]
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
    Ok(())
}
