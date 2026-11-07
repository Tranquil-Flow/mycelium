// SPDX-License-Identifier: AGPL-3.0-only
//! CR001: no hand-written cryptography (RU-010). Token-level, so constants hidden in
//! macro invocations are seen and comments/doc comments/ordinary strings are not.
//!
//! A finding is one of:
//! * an integer literal equal to a published primitive constant (SHA-1/2 initial words and
//!   round words, SHA-512 words, ChaCha "expand ..-byte k" words, the Poly1305 clamp and
//!   2^130 - 5, the Curve25519 a24 / Montgomery A / 51-bit limb mask, Keccak round words),
//!   in any base or suffix; or the ChaCha sigma text; or the first eight AES S-box bytes in a row;
//! * a `fn` named like a primitive internal (`quarter_round`, `sha256_compress`, `fe_mul`, ...);
//! * a `fn hmac`, `hmac_sha256`, `hkdf_expand`, ... whose body names none of the admitted crates;
//! * a function body holding both HMAC pad bytes 0x36 and 0x5c.
//!
//! Exceptions go through the owned, expiring `xtask/allowlist.json` like every rule.
use super::Diagnostic;
use proc_macro2::{Delimiter, TokenStream, TokenTree};
use std::{collections::BTreeSet, sync::OnceLock};

/// Hex digits of the published constants, kept as text so this file holds none of them
/// as integer literals (the lint scans its own sources).
const CONSTANTS: &[&str] = &[
    // SHA-256 initial hash (FIPS 180-4 5.3.3) and the first K words (4.2.2)
    "6a09e667",
    "bb67ae85",
    "3c6ef372",
    "a54ff53a",
    "510e527f",
    "9b05688c",
    "1f83d9ab",
    "5be0cd19",
    "428a2f98",
    "71374491",
    "b5c0fbcf",
    "e9b5dba5",
    "3956c25b",
    "59f111f1",
    "923f82a4",
    "ab1c5ed5",
    // SHA-512 initial words and first K words
    "6a09e667f3bcc908",
    "bb67ae8584caa73b",
    "3c6ef372fe94f82b",
    "a54ff53a5f1d36f1",
    "428a2f98d728ae22",
    "7137449123ef65cd",
    // SHA-1 (5.3.1) initial words and round words
    "67452301",
    "efcdab89",
    "98badcfe",
    "10325476",
    "c3d2e1f0",
    "5a827999",
    "6ed9eba1",
    "8f1bbcdc",
    "ca62c1d6",
    // ChaCha20 constant words (RFC 8439 2.3)
    "61707865",
    "3320646e",
    "79622d32",
    "6b206574",
    // Poly1305 clamp and prime (RFC 8439 2.5): 128-bit, high and low halves, 2^130 - 5
    "0ffffffc0ffffffc0ffffffc0fffffff",
    "0ffffffc0fffffff",
    "0ffffffc0ffffffc",
    "3fffffffffffffffb",
    // Curve25519 51-bit limb mask
    "7ffffffffffff",
    // Keccak-f round words (FIPS 202 3.2.5)
    "8000000000008082",
    "800000000000808a",
    "8000000080008000",
    "8000000080008081",
    "8000000000008009",
];
/// RFC 7748 a24 for the two curves and the Montgomery A of Curve25519, in decimal (as text,
/// so this file holds none of them as integer literals).
const DECIMAL_CONSTANTS: &[&str] = &["121665", "121666", "486662"];
/// First eight AES S-box bytes (FIPS 197 figure 7).
const SBOX_PREFIX: &[&str] = &["63", "7c", "77", "7b", "f2", "6b", "6f", "c5"];
const INTERNAL_NAMES: &[&str] = &[
    "quarter_round",
    "chacha_block",
    "chacha20_block",
    "poly1305_block",
    "poly1305_blocks",
    "sha256_compress",
    "sha512_compress",
    "sha256_transform",
    "sha1_transform",
    "keccak_f",
    "keccak_round",
    "fe_mul",
    "fe_square",
    "fe_invert",
    "montgomery_ladder",
    "ladder_step",
    "ghash",
];
/// A function called exactly like the primitive: `hmac`, `hmac_sha256`, `hkdf_expand`, ...
/// (a test named `hmac_sha256_matches_vectors` is a sentence, not a primitive).
const WRAPPER_HEADS: &[&str] = &["hmac", "hkdf"];
const WRAPPER_WORDS: &[&str] = &[
    "sha1", "sha256", "sha384", "sha512", "extract", "expand", "sign", "verify", "tag", "derive",
    "compute", "mac", "digest",
];
fn primitive_wrapper_name(lower: &str) -> bool {
    let mut words = lower.split('_');
    words
        .next()
        .is_some_and(|head| WRAPPER_HEADS.contains(&head))
        && words.all(|w| WRAPPER_WORDS.contains(&w))
}
/// Identifiers that show a body delegates to an admitted crate (RU-010 list, lower case).
const ADMITTED: &[&str] = &[
    "chacha20poly1305",
    "xchacha20poly1305",
    "chacha20",
    "poly1305",
    "hkdf",
    "hmac",
    "x25519_dalek",
    "ed25519_dalek",
    "curve25519_dalek",
    "sha2",
    "sha256",
    "sha384",
    "sha512",
    "blake3",
];
const PAD_BYTES: [u128; 2] = [0x36, 0x5c];

fn hex(text: &str) -> u128 {
    u128::from_str_radix(text, 16).unwrap_or(0)
}
fn constants() -> &'static BTreeSet<u128> {
    static SET: OnceLock<BTreeSet<u128>> = OnceLock::new();
    SET.get_or_init(|| {
        CONSTANTS
            .iter()
            .map(|c| hex(c))
            .chain(
                DECIMAL_CONSTANTS
                    .iter()
                    .filter_map(|c| c.parse::<u128>().ok()),
            )
            .filter(|v| *v != 0)
            .collect()
    })
}
/// The ChaCha "expand 32-byte k" / "expand 16-byte k" text, assembled so this file does not hold it.
fn is_sigma(text: &str) -> bool {
    text.find("expand ").is_some_and(|at| {
        ["32", "16"]
            .iter()
            .any(|bits| text[at + 7..].starts_with(&format!("{bits}-byte k")))
    })
}
fn sbox() -> Vec<u128> {
    SBOX_PREFIX.iter().map(|b| hex(b)).collect()
}

/// Value of an integer literal token in any base, with `_` separators and a type suffix.
fn integer(text: &str) -> Option<u128> {
    if !text.starts_with(|c: char| c.is_ascii_digit()) {
        return None;
    }
    let clean: String = text.chars().filter(|c| *c != '_').collect();
    let (radix, digits) = match clean.get(..2) {
        Some("0x") => (16, &clean[2..]),
        Some("0o") => (8, &clean[2..]),
        Some("0b") => (2, &clean[2..]),
        _ => (10, clean.as_str()),
    };
    if radix == 10 && digits.contains(['.', 'e', 'E']) {
        return None;
    }
    let end = digits.find(['u', 'i']).unwrap_or(digits.len());
    u128::from_str_radix(&digits[..end], radix).ok()
}

struct Scan {
    lines: BTreeSet<usize>,
}
impl Scan {
    fn flag(&mut self, line: usize) {
        self.lines.insert(line);
    }
    fn stream(&mut self, tokens: TokenStream) {
        let items: Vec<TokenTree> = tokens.into_iter().collect();
        let mut window: Vec<u128> = Vec::new();
        let sequence = sbox();
        let mut index = 0;
        while index < items.len() {
            match &items[index] {
                TokenTree::Literal(literal) => {
                    let text = literal.to_string();
                    if let Some(value) = integer(&text) {
                        if constants().contains(&value) {
                            self.flag(literal.span().start().line);
                        }
                        window.push(value);
                        if window.len() >= sequence.len()
                            && window[window.len() - sequence.len()..] == sequence[..]
                        {
                            self.flag(literal.span().start().line);
                        }
                    } else {
                        window.clear();
                        if is_sigma(&text) {
                            self.flag(literal.span().start().line);
                        }
                    }
                }
                TokenTree::Punct(punct) if punct.as_char() == ',' => {}
                TokenTree::Punct(punct) if punct.as_char() == '#' => {
                    window.clear();
                    // `#[doc = ".."]` is documentation, not code.
                    // Inner docs (`//!`) are `#` `!` `[doc = ..]`.
                    let skip = usize::from(
                        matches!(items.get(index + 1), Some(TokenTree::Punct(p)) if p.as_char() == '!'),
                    );
                    if let Some(TokenTree::Group(group)) = items.get(index + 1 + skip)
                        && group.delimiter() == Delimiter::Bracket
                        && group
                            .stream()
                            .into_iter()
                            .next()
                            .is_some_and(|first| matches!(first, TokenTree::Ident(i) if i == "doc"))
                    {
                        index += 2 + skip;
                        continue;
                    }
                }
                TokenTree::Ident(keyword) if keyword == "fn" => {
                    window.clear();
                    if let Some(TokenTree::Ident(name)) = items.get(index + 1) {
                        self.function(name, &items[index + 2..]);
                    }
                }
                TokenTree::Group(group) => {
                    window.clear();
                    self.stream(group.stream());
                }
                _ => window.clear(),
            }
            index += 1;
        }
    }
    fn function(&mut self, name: &proc_macro2::Ident, rest: &[TokenTree]) {
        let line = name.span().start().line;
        let lower = name.to_string().to_ascii_lowercase();
        if INTERNAL_NAMES.contains(&lower.as_str()) {
            self.flag(line);
        }
        let body = rest.iter().find_map(|token| match token {
            TokenTree::Group(group) if group.delimiter() == Delimiter::Brace => {
                Some(group.stream())
            }
            _ => None,
        });
        // A bodiless declaration ends at `;` before any brace group.
        let bodiless = rest
            .iter()
            .position(|t| matches!(t, TokenTree::Punct(p) if p.as_char() == ';'))
            .zip(rest.iter().position(
                |t| matches!(t, TokenTree::Group(g) if g.delimiter() == Delimiter::Brace),
            ))
            .is_some_and(|(semi, brace)| semi < brace);
        let Some(body) = body.filter(|_| !bodiless) else {
            return;
        };
        let (mut idents, mut values) = (BTreeSet::new(), BTreeSet::new());
        collect(body, &mut idents, &mut values);
        if primitive_wrapper_name(&lower) && !idents.iter().any(|i| ADMITTED.contains(&i.as_str()))
        {
            self.flag(line);
        }
        if PAD_BYTES.iter().all(|pad| values.contains(pad)) {
            self.flag(line);
        }
    }
}
fn collect(tokens: TokenStream, idents: &mut BTreeSet<String>, values: &mut BTreeSet<u128>) {
    for token in tokens {
        match token {
            TokenTree::Ident(ident) => {
                idents.insert(ident.to_string().to_ascii_lowercase());
            }
            TokenTree::Literal(literal) => {
                if let Some(value) = integer(&literal.to_string()) {
                    values.insert(value);
                }
            }
            TokenTree::Group(group) => collect(group.stream(), idents, values),
            TokenTree::Punct(_) => {}
        }
    }
}

pub fn scan(path: &str, text: &str, findings: &mut Vec<Diagnostic>) {
    // A source that does not tokenize was already refused by the syntax pass.
    let Ok(tokens) = text.parse::<TokenStream>() else {
        return;
    };
    let mut scan = Scan {
        lines: BTreeSet::new(),
    };
    scan.stream(tokens);
    findings.extend(scan.lines.into_iter().map(|line| Diagnostic {
        path: path.to_string(),
        line,
        code: "CR001".to_string(),
    }));
}
