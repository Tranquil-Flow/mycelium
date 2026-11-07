// SPDX-License-Identifier: AGPL-3.0-only
//! `link.svg` QR renderer against the EXECUTED oracle: `server.qr_svg` (vendored Nayuki
//! qrcodegen, ECC MEDIUM with boost, automatic mask, border 4) at f4e366e9 on Python 3.14.7.
//! The corpus (`fixtures/oracle_qr.json`, made by `L10-evidence/r7-probe-qr.py`) holds
//! sha256 of the exact SVG bytes for generated numeric / alphanumeric / byte-mode inputs across
//! every QR version, real `swarm_link` texts, edge strings and full SVGs.
use api::{qr, swarm::docs::swarm_link};
use serde_json::Value;
use sha2::{Digest, Sha256};

fn corpus() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_qr.json")).unwrap()
}
/// The probe's generator: an LCG over the named alphabet (chars, not bytes).
fn generate(alphabet: &str, chars: usize) -> String {
    let set: Vec<char> = alphabet.chars().collect();
    let mut x = chars as u64 + 1;
    (0..chars)
        .map(|_| {
            x = (x * 1_103_515_245 + 12_345) & 0x7FFF_FFFF;
            set[((x >> 16) as usize) % set.len()]
        })
        .collect()
}
fn sha(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}
fn check(label: &str, text: &str, case: &Value) {
    match (qr::svg(text), case["sha256"].as_str()) {
        (Ok(svg), Some(want)) => {
            assert_eq!(sha(&svg), want, "{label}");
            assert_eq!(svg.len() as u64, case["bytes"].as_u64().unwrap(), "{label}");
        }
        (Err(_), None) => assert!(case["error"].is_string(), "{label}"),
        (got, want) => panic!("{label}: got {:?}, oracle {want:?}", got.map(|s| s.len())),
    }
}

#[test]
fn generated_corpus_is_byte_identical_across_modes_versions_and_masks() {
    let corpus = corpus();
    let (mut ok, mut too_long) = (0, 0);
    for case in corpus["generated"].as_array().unwrap() {
        let g = &case["gen"];
        let alphabet = corpus["alphabets"][g["alphabet"].as_str().unwrap()]
            .as_str()
            .unwrap();
        let chars = g["chars"].as_u64().unwrap() as usize;
        check(&format!("{g}"), &generate(alphabet, chars), case);
        if case["error"].is_string() {
            too_long += 1;
        } else {
            ok += 1;
        }
    }
    // Pinned corpus size: guards against silently dropping cases.
    assert_eq!(
        (ok, too_long),
        (
            corpus["expect_ok"].as_u64().unwrap() as usize,
            corpus["expect_too_long"].as_u64().unwrap() as usize
        )
    );
}

#[test]
fn real_swarm_links_and_edge_strings_are_byte_identical() {
    let corpus = corpus();
    let links = corpus["links"].as_array().unwrap();
    assert_eq!(links.len(), 60);
    for case in links {
        let link = swarm_link(
            case["origin"].as_str().unwrap(),
            case["swarm_id"].as_str().unwrap(),
            case["owner_key"].as_str().unwrap(),
        )
        .unwrap();
        assert_eq!(link, case["text"].as_str().unwrap());
        check(&link, &link, case);
    }
    let fixed = corpus["fixed"].as_array().unwrap();
    assert_eq!(fixed.len(), 15);
    for case in fixed {
        let text = case["text"].as_str().unwrap();
        check(&format!("{text:?}"), text, case);
    }
}

#[test]
fn full_svg_bytes_match() {
    let corpus = corpus();
    let link = corpus["links"][0]["text"].as_str().unwrap();
    for (name, text) in [
        ("link0", link),
        ("hello", "HELLO WORLD"),
        ("digits", "0123456789"),
    ] {
        assert_eq!(
            String::from_utf8(qr::svg(text).unwrap()).unwrap(),
            corpus["full_svgs"][name].as_str().unwrap(),
            "{name}"
        );
    }
}

#[test]
fn over_capacity_text_is_an_error_not_a_truncated_code() {
    // qrcodegen raises DataTooLongError at 2332 UTF-8 bytes (version 40, ECC MEDIUM).
    assert!(qr::svg(&"a".repeat(2331)).is_ok());
    assert!(qr::svg(&"a".repeat(2332)).is_err());
}
