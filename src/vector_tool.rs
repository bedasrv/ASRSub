#![allow(dead_code)]
use std::path::Path;

use super::discord_state_codec::{domain_hash, encode_string, hex};

pub(crate) fn run_core() {
    let args: Vec<String> = std::env::args().collect();
    if args
        .get(1..)
        .map(|tail| tail != ["core", "--output", "tests/fixtures/canonical_vectors.json"])
        .unwrap_or(true)
    {
        eprintln!("usage: asrsub-vectors core --output tests/fixtures/canonical_vectors.json");
        std::process::exit(2);
    }
    let strings = [
        "quote:\"",
        "backslash:\\",
        "control:\n",
        "non-ascii:こんにちは",
    ];
    let entries: Vec<String> = strings.iter().map(|value| {
        let bytes = encode_string(value);
        format!("{{\"name\":{},\"domain\":\"asrsub-vector-v1\",\"bytes_b64\":\"{}\",\"hash\":\"{}\"}}", String::from_utf8(encode_string(value)).unwrap(), base64(bytes.as_slice()), hex(&domain_hash(b"asrsub-vector-v1", &bytes)))
    }).collect();
    let output = format!(
        "{{\"schema\":\"canonical-vector-set-v1\",\"entries\":[{}]}}\n",
        entries.join(",")
    );
    let path = Path::new("tests/fixtures/canonical_vectors.json");
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, output).expect("write vectors");
    std::fs::rename(tmp, path).expect("install vectors");
}

fn base64(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    let mut i = 0;
    while i < bytes.len() {
        let a = bytes[i] as u32;
        let b = bytes.get(i + 1).copied().unwrap_or(0) as u32;
        let c = bytes.get(i + 2).copied().unwrap_or(0) as u32;
        let n = (a << 16) | (b << 8) | c;
        out.push(TABLE[((n >> 18) & 63) as usize] as char);
        out.push(TABLE[((n >> 12) & 63) as usize] as char);
        out.push(if i + 1 < bytes.len() {
            TABLE[((n >> 6) & 63) as usize] as char
        } else {
            '='
        });
        out.push(if i + 2 < bytes.len() {
            TABLE[(n & 63) as usize] as char
        } else {
            '='
        });
        i += 3;
    }
    out
}
