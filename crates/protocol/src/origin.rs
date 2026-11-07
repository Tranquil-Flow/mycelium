// SPDX-License-Identifier: AGPL-3.0-only
//! `mycelium_internet.bootstrap.canonical_https_origin` (bootstrap.py:70-122 @ f4e366e9): the one canonical
//! `https://host[:port]` text. Moved here from `api::join_link` (round 14) so the seed server (`seed::server`,
//! `SeedHTTPServer.__init__` validates `public_seed_url` with it) and the API share one implementation (CL-L5-05).
//! Pure: no clock, filesystem, or RNG (wasm32-safe). Executed-oracle corpus: `seed/tests/oracle_origin.rs`
//! (FX-L5-origin-r14) and `api/tests/join_link.rs`.
use serde_json::Value;

/// A bounded `ValueError` of the oracle's origin/port validators.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Invalid;

/// `str.isspace()`: Unicode White_Space plus the C0 separators U+001C..U+001F.
pub fn py_isspace(c: char) -> bool {
    matches!(
        c,
        '\t' | '\n'
            | '\u{b}'
            | '\u{c}'
            | '\r'
            | '\u{1c}'..='\u{1f}'
            | ' '
            | '\u{85}'
            | '\u{a0}'
            | '\u{1680}'
            | '\u{2000}'..='\u{200a}'
            | '\u{2028}'
            | '\u{2029}'
            | '\u{202f}'
            | '\u{205f}'
            | '\u{3000}'
    )
}
/// `str.strip()` with no argument.
pub fn py_strip(text: &str) -> &str {
    text.trim_matches(py_isspace)
}

/// `ipaddress.IPv4Address(text)`: four ASCII decimal octets, no leading zeros, each <= 255.
fn is_ipv4(host: &str) -> bool {
    let octets: Vec<&str> = host.split('.').collect();
    octets.len() == 4
        && octets.iter().all(|o| {
            !o.is_empty()
                && o.len() <= 3
                && o.bytes().all(|b| b.is_ascii_digit())
                && (o.len() == 1 || !o.starts_with('0'))
                && o.parse::<u16>().is_ok_and(|n| n <= 255)
        })
}
/// `_HOST_LABEL_RE`: 1-63 ASCII letters/digits/hyphens, alphanumeric at both ends.
fn host_label(label: &str) -> bool {
    let bytes = label.as_bytes();
    (1..=63).contains(&bytes.len())
        && bytes
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || *b == b'-')
        && bytes[0].is_ascii_alphanumeric()
        && bytes[bytes.len() - 1].is_ascii_alphanumeric()
}

/// `canonical_https_origin`: the one canonical `https://host[:port]` text, else a `ValueError`.
pub fn canonical_https_origin(value: &Value) -> Result<String, Invalid> {
    let text = value.as_str().ok_or(Invalid)?;
    if text.is_empty()
        || text != py_strip(text)
        || text.chars().any(|c| u32::from(c) < 0x20)
        || !text.starts_with("https://")
    {
        return Err(Invalid);
    }
    let netloc = &text["https://".len()..];
    if netloc.is_empty() || netloc != py_strip(netloc) || netloc.contains(['/', '?', '#', '@']) {
        return Err(Invalid);
    }
    let (host, port_text) = match netloc.split_once(':') {
        Some((host, port)) => (host, Some(port)),
        None => (netloc, None),
    };
    if host.is_empty() {
        return Err(Invalid);
    }
    let port = match port_text {
        None => None,
        Some(digits) => {
            // A non-ASCII digit can never reproduce the ASCII canonical text below.
            if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
                return Err(Invalid);
            }
            let significant = digits.trim_start_matches('0');
            if significant.is_empty() || significant.len() > 5 {
                return Err(Invalid);
            }
            let port: u32 = significant.parse().map_err(|_| Invalid)?;
            if port == 0 || port > 65_535 {
                return Err(Invalid);
            }
            Some(port)
        }
    };
    if host.contains(':') || !host.is_ascii() {
        return Err(Invalid);
    }
    let canonical_host = if is_ipv4(host) {
        host.to_owned()
    } else {
        if host.ends_with('.') || !host.trim_end_matches('.').split('.').all(host_label) {
            return Err(Invalid);
        }
        host.to_ascii_lowercase()
    };
    let canonical = match port {
        Some(port) => format!("https://{canonical_host}:{port}"),
        None => format!("https://{canonical_host}"),
    };
    if text == canonical {
        Ok(canonical)
    } else {
        Err(Invalid)
    }
}
