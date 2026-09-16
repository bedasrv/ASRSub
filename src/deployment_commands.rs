#![allow(dead_code)]
pub(crate) fn valid_nonce(v: &str) -> bool {
    v.len() == 64
        && v.bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

pub(crate) fn egress_snapshot_is_literal(addresses: &[std::net::IpAddr]) -> bool {
    super::egress::uses_literal_snapshot_addresses(addresses)
}
