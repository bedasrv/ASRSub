#![allow(dead_code)]
//! Optional Discord configuration and runtime-secret grammar.

use std::path::Path;

const MAX_SECRET_BYTES: usize = 512;
const WEBHOOK_PREFIX: &str = "https://discord.com/api/webhooks/";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum SecretReadError {
    Missing,
    Io,
    Invalid,
}

/// Opaque validated input for the transport constructor.  It intentionally
/// has no formatting or serialization implementation.
pub(crate) struct RuntimeSecretBytes(Box<[u8]>);

impl RuntimeSecretBytes {
    pub(crate) fn as_bytes(&self) -> &[u8] {
        &self.0
    }
}

pub(crate) fn is_reserved_key(raw: &str) -> bool {
    raw.trim_matches(|c: char| c.is_ascii_whitespace())
        .eq_ignore_ascii_case("DISCORD_WEBHOOK_URL")
}

pub(crate) fn filter_reserved(map: &mut std::collections::HashMap<String, String>) {
    map.retain(|key, _| !is_reserved_key(key));
}

pub(crate) fn validate_webhook_bytes(bytes: &[u8]) -> Result<(), SecretReadError> {
    if bytes.is_empty() || bytes.len() > MAX_SECRET_BYTES || !bytes.is_ascii() {
        return Err(SecretReadError::Invalid);
    }
    let value = std::str::from_utf8(bytes).map_err(|_| SecretReadError::Invalid)?;
    if value != value.trim()
        || value
            .bytes()
            .any(|b| b.is_ascii_whitespace() || b == b'\\' || b == 0)
    {
        return Err(SecretReadError::Invalid);
    }
    let rest = value
        .strip_prefix(WEBHOOK_PREFIX)
        .ok_or(SecretReadError::Invalid)?;
    if rest.contains('?') || rest.contains('#') || rest.contains('%') || rest.contains('@') {
        return Err(SecretReadError::Invalid);
    }
    let mut pieces = rest.split('/');
    let snowflake = pieces.next().ok_or(SecretReadError::Invalid)?;
    let token = pieces.next().ok_or(SecretReadError::Invalid)?;
    if pieces.next().is_some()
        || !(17..=20).contains(&snowflake.len())
        || !snowflake.bytes().all(|b| b.is_ascii_digit())
        || token.is_empty()
        || token.len() > 256
        || token == "."
        || token == ".."
        || !token
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
    {
        return Err(SecretReadError::Invalid);
    }
    Ok(())
}

pub(crate) fn read_runtime_secret(path: &Path) -> Result<RuntimeSecretBytes, SecretReadError> {
    let metadata = std::fs::symlink_metadata(path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            SecretReadError::Missing
        } else {
            SecretReadError::Io
        }
    })?;
    if !metadata.file_type().is_file() || metadata.len() > (MAX_SECRET_BYTES as u64 + 1) {
        return Err(SecretReadError::Invalid);
    }
    let bytes = std::fs::read(path).map_err(|_| SecretReadError::Io)?;
    validate_webhook_bytes(&bytes)?;
    Ok(RuntimeSecretBytes(bytes.into_boxed_slice()))
}

pub(crate) fn read_optional_runtime_secret(path: &Path) -> Option<RuntimeSecretBytes> {
    read_runtime_secret(path).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reserved_key_predicate_is_ascii_case_insensitive() {
        assert!(is_reserved_key("  discord_webhook_url\t"));
        assert!(!is_reserved_key("DISCORD_WEBHOOK_URL_EXTRA"));
    }

    #[test]
    fn webhook_grammar_accepts_only_fixed_shape() {
        let valid = b"https://discord.com/api/webhooks/12345678901234567/a_b-C.1";
        assert!(validate_webhook_bytes(valid).is_ok());
        for invalid in [
            b"https://discord.com/api/webhooks/1/token".as_slice(),
            b"https://discord.com/api/webhooks/12345678901234567/token/extra",
            b"https://discord.com/api/webhooks/12345678901234567/token?wait=true",
        ] {
            assert_eq!(
                validate_webhook_bytes(invalid),
                Err(SecretReadError::Invalid)
            );
        }
    }
}
