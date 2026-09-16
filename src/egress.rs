#![allow(dead_code)]
use std::net::IpAddr;
pub(crate) fn rejects_private_answers(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(v) => {
            v.is_private()
                || v.is_loopback()
                || v.is_link_local()
                || v.is_multicast()
                || v.is_unspecified()
                || v.is_documentation()
        }
        IpAddr::V6(v) => {
            v.is_loopback()
                || v.is_unspecified()
                || v.is_multicast()
                || (v.segments()[0] & 0xfe00 == 0xfc00)
        }
    }
}
pub(crate) fn snapshot_matches(expected: &[IpAddr], actual: &[IpAddr]) -> bool {
    expected == actual
}
pub(crate) fn uses_literal_snapshot_addresses(addresses: &[IpAddr]) -> bool {
    !addresses.is_empty() && addresses.iter().all(|a| !rejects_private_answers(*a))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_private_answers() {
        assert!(super::rejects_private_answers("127.0.0.1".parse().unwrap()));
        assert!(!super::rejects_private_answers("8.8.8.8".parse().unwrap()));
    }
    #[test]
    fn rejects_snapshot_change() {
        let a = vec!["8.8.8.8".parse().unwrap()];
        assert!(!super::snapshot_matches(&a, &["1.1.1.1".parse().unwrap()]));
    }
    #[test]
    fn uses_literal_snapshot_addresses() {
        assert!(super::uses_literal_snapshot_addresses(&["8.8.8.8"
            .parse()
            .unwrap()]));
    }
}
