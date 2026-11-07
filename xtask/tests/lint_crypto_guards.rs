// SPDX-License-Identifier: AGPL-3.0-only
//! CL-L4-10 (CR001, no hand-written cryptography, RU-010) and CL-L4-11 (CG001, no
//! constant guards in test code). Reject fixtures carry `// expect: CODE` on the exact
//! line; accept fixtures must be clean. Constants in the reject fixtures are the
//! published ones (FIPS 180-4, FIPS 202, RFC 2104/5869/7748/8439), cited in each file.
use std::{collections::BTreeSet, fs, path::Path};
use xtask::{
    lint::{Diagnostic, Source, scan_sources},
    policy::{Allowlist, Date},
};

fn load(kind: &str, outcome: &str, prefix: &str) -> Vec<Source> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("fixtures")
        .join(kind)
        .join(outcome);
    let mut sources: Vec<Source> = fs::read_dir(&dir)
        .unwrap()
        .map(|entry| {
            let path = entry.unwrap().path();
            Source {
                path: format!("{prefix}{}", path.file_name().unwrap().to_str().unwrap()),
                text: fs::read_to_string(path).unwrap(),
            }
        })
        .collect();
    sources.sort_by(|a, b| a.path.cmp(&b.path));
    assert!(!sources.is_empty(), "{kind}/{outcome}");
    sources
}
fn expected(sources: &[Source]) -> Vec<Diagnostic> {
    let mut result = Vec::new();
    for source in sources {
        let before = result.len();
        for (index, line) in source.text.lines().enumerate() {
            if let Some((_, codes)) = line.split_once("// expect: ") {
                for code in codes.split_whitespace() {
                    result.push(Diagnostic {
                        path: source.path.clone(),
                        line: index + 1,
                        code: code.into(),
                    });
                }
            }
        }
        assert!(
            result.len() > before,
            "fixture lacks oracle: {}",
            source.path
        );
    }
    result.sort();
    result
}
fn scan(path: &str, text: &str) -> Vec<(usize, String)> {
    scan_sources(&[Source {
        path: path.into(),
        text: text.into(),
    }])
    .unwrap()
    .into_iter()
    .map(|d| (d.line, d.code))
    .collect()
}
fn only(code: &str, findings: Vec<(usize, String)>) -> Vec<usize> {
    findings
        .into_iter()
        .filter(|(_, c)| c == code)
        .map(|(line, _)| line)
        .collect()
}

// ------------------------------------------------------------------ CR001

#[test]
fn crypto_reject_fixtures_have_exact_lines() {
    let sources = load("crypto", "reject", "crates/example/");
    assert_eq!(scan_sources(&sources).unwrap(), expected(&sources));
    assert_eq!(
        sources.len(),
        8,
        "sha256, chacha, poly1305, x25519, hmac/hkdf, others, internals, macros"
    );
}

#[test]
fn crypto_accept_fixtures_are_clean() {
    assert_eq!(
        scan_sources(&load("crypto", "accept", "crates/example/")).unwrap(),
        vec![]
    );
}

#[test]
fn crypto_rule_applies_to_every_path_not_only_tests() {
    let text = "pub const H0: u32 = 0x6a09e667;\n";
    for path in [
        "crates/example/src/lib.rs",
        "crates/example/tests/a.rs",
        "test-support/x/src/lib.rs",
        "xtask/src/x.rs",
    ] {
        assert_eq!(only("CR001", scan(path, text)), vec![1], "{path}");
    }
}

#[test]
fn a_constant_is_recognised_in_any_base_case_or_suffix() {
    for literal in [
        "1779033703",
        "0x6A09E667",
        "0x6a09_e667",
        "0x6a09e667_u32",
        "0b1101010000010011110011001100111",
        "0o15202363147",
    ] {
        let text = format!("pub const X: u64 = {literal};\n");
        assert_eq!(
            only("CR001", scan("crates/example/src/lib.rs", &text)),
            vec![1],
            "{literal}"
        );
    }
    // One off, and different sizes of the same digits, are other numbers.
    for literal in ["1779033702", "0x6a09e66", "0x16a09e667"] {
        let text = format!("pub const X: u64 = {literal};\n");
        assert_eq!(
            only("CR001", scan("crates/example/src/lib.rs", &text)),
            Vec::<usize>::new(),
            "{literal}"
        );
    }
}

#[test]
fn hmac_and_hkdf_wrappers_pass_only_when_they_call_an_admitted_crate() {
    let delegating = "pub fn hmac_sha256(k: &[u8], m: &[u8]) -> Vec<u8> {\n    let mut mac = Hmac::<Sha256>::new_from_slice(k).unwrap();\n    mac.update(m);\n    mac.finalize().into_bytes().to_vec()\n}\n";
    assert_eq!(
        only("CR001", scan("crates/example/src/lib.rs", delegating)),
        Vec::<usize>::new()
    );
    let by_hand = "pub fn hmac_sha256(k: &[u8], m: &[u8]) -> Vec<u8> {\n    let mut v = k.to_vec();\n    v.extend_from_slice(m);\n    v\n}\n";
    assert_eq!(
        only("CR001", scan("crates/example/src/lib.rs", by_hand)),
        vec![1]
    );
    let method =
        "impl Mac {\n    pub fn hkdf_expand(&self) -> Vec<u8> {\n        Vec::new()\n    }\n}\n";
    assert_eq!(
        only("CR001", scan("crates/example/src/lib.rs", method)),
        vec![2]
    );
}

#[test]
fn the_two_hmac_pad_bytes_in_one_function_are_flagged_at_the_function() {
    let text = "pub fn tag(key: &[u8]) -> (Vec<u8>, Vec<u8>) {\n    let i = key.iter().map(|b| b ^ 0x36).collect();\n    let o = key.iter().map(|b| b ^ 0x5c).collect();\n    (i, o)\n}\npub fn only_one(key: &[u8]) -> Vec<u8> {\n    key.iter().map(|b| b ^ 0x36).collect()\n}\n";
    assert_eq!(
        only("CR001", scan("crates/example/src/lib.rs", text)),
        vec![1]
    );
}

#[test]
fn the_curve_a24_constant_and_names_in_strings_or_comments_are_judged_by_kind() {
    assert_eq!(
        only(
            "CR001",
            scan("crates/example/src/lib.rs", "pub const A: u64 = 121666;\n")
        ),
        vec![1]
    );
    let quiet = "// 0x6a09e667\npub const S: &str = \"0x6a09e667 sha256\";\n";
    assert_eq!(
        only("CR001", scan("crates/example/src/lib.rs", quiet)),
        Vec::<usize>::new()
    );
}

#[test]
fn crypto_findings_can_be_allowlisted_only_with_owner_reason_and_expiry() {
    let sources = load("crypto", "reject", "crates/example/");
    let all = scan_sources(&sources).unwrap();
    let source = sources
        .iter()
        .find(|s| s.path.ends_with("sha256_tables.rs"))
        .unwrap();
    let line = 4;
    let text = source.text.lines().nth(line - 1).unwrap();
    let entry = |expiry: &str, owner: &str| {
        serde_json::json!({"version":1,"entries":[{
            "path": source.path, "line": line, "rule": "CR001", "source_line": text,
            "owner": owner, "reason": "documentation fixture, not shipped", "expiry": expiry,
            "replacement_plan": "none: scanning test input"}]})
        .to_string()
    };
    let today = Date::parse("2026-10-28").unwrap();
    let allow = Allowlist::parse(&entry("2099-01-01", "L4"), today).unwrap();
    let remaining = allow.apply(all.clone(), &sources).unwrap();
    assert_eq!(remaining.len(), all.len() - 1);
    assert!(
        !remaining
            .iter()
            .any(|d| d.path == source.path && d.line == line)
    );
    assert!(
        Allowlist::parse(&entry("2020-01-01", "L4"), today).is_err(),
        "expired"
    );
    assert!(
        Allowlist::parse(&entry("2099-01-01", " "), today).is_err(),
        "no owner"
    );
}

// ------------------------------------------------------------------ CG001

#[test]
fn guard_reject_fixtures_have_exact_lines_in_test_paths() {
    let sources = load("guards", "reject", "crates/example/tests/");
    assert_eq!(scan_sources(&sources).unwrap(), expected(&sources));
    let codes: BTreeSet<_> = scan_sources(&sources)
        .unwrap()
        .into_iter()
        .map(|d| d.code)
        .collect();
    assert_eq!(codes, BTreeSet::from(["CG001".to_string()]));
}

#[test]
fn guard_accept_fixtures_are_clean_in_test_paths() {
    assert_eq!(
        scan_sources(&load("guards", "accept", "crates/example/tests/")).unwrap(),
        vec![]
    );
}

#[test]
fn guards_are_only_policed_in_test_code() {
    let text = "pub fn f(x: u8) -> u8 {\n    if true {\n        return x;\n    }\n    0\n}\n";
    for path in [
        "crates/example/src/lib.rs",
        "crates/example/src/bin/tool.rs",
        "xtask/src/main.rs",
    ] {
        assert_eq!(
            only("CG001", scan(path, text)),
            Vec::<usize>::new(),
            "{path} is product code"
        );
    }
    for path in [
        "crates/example/tests/a.rs",
        "crates/example/tests/support/mod.rs",
        "test-support/sim/src/lib.rs",
        "test-support/budget_model/tests/x.rs",
        "crates/example/src/tests.rs",
    ] {
        assert_eq!(only("CG001", scan(path, text)), vec![2], "{path}");
    }
}

#[test]
fn guards_inside_cfg_test_modules_and_test_functions_of_product_files_are_flagged() {
    let text = "pub fn product() -> u8 {\n    if true { 1 } else { 2 }\n}\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn t() {\n        if false {\n            panic!();\n        }\n    }\n}\n#[test]\nfn loose() {\n    while true {\n        break;\n    }\n}\n";
    assert_eq!(
        only("CG001", scan("crates/example/src/lib.rs", text)),
        vec![8, 15]
    );
}

#[test]
fn a_guard_inside_a_nested_closure_or_macro_argument_is_seen() {
    let text = "#[test]\nfn t() {\n    let f = |x: bool| if x || true { 1 } else { 2 };\n    assert_eq!(f(false), 1);\n    let v: Vec<u8> = vec![if false { 1 } else { 2 }];\n    assert_eq!(v.len(), 1);\n}\n";
    assert_eq!(
        only("CG001", scan("crates/example/src/lib.rs", text)),
        vec![3, 5]
    );
}

#[test]
fn a_bool_const_is_a_guard_only_when_every_declaration_of_the_name_agrees() {
    let agree = "const F: bool = true;\n#[test]\nfn t() {\n    if F {\n    }\n}\n";
    assert_eq!(
        only("CG001", scan("crates/example/src/lib.rs", agree)),
        vec![4]
    );
    let conflict = "mod a { pub const F: bool = true; }\nmod b { pub const F: bool = false; }\n#[test]\nfn t(F: bool) {\n    if F {\n    }\n}\n";
    assert_eq!(
        only("CG001", scan("crates/example/src/lib.rs", conflict)),
        Vec::<usize>::new()
    );
}

#[test]
fn guard_findings_can_be_allowlisted_with_the_same_policy() {
    let sources = vec![Source {
        path: "crates/example/tests/a.rs".into(),
        text: "fn t() {\n    if true {\n    }\n}\n".into(),
    }];
    let all = scan_sources(&sources).unwrap();
    assert_eq!(all.len(), 1);
    let json = serde_json::json!({"version":1,"entries":[{
        "path": "crates/example/tests/a.rs", "line": 2, "rule": "CG001", "source_line": "    if true {",
        "owner": "L4", "reason": "demonstration", "expiry": "2099-01-01", "replacement_plan": "none"}]})
    .to_string();
    let allow = Allowlist::parse(&json, Date::parse("2026-10-28").unwrap()).unwrap();
    assert_eq!(allow.apply(all, &sources).unwrap(), vec![]);
}
