// SPDX-License-Identifier: AGPL-3.0-only
//! RU-051: no instant exemption without producer/negative proof; carriers name origins.
//! Unfinished reviews remain explicit pending candidates, not not-a-bound verdicts.
#[path = "common/oracle_blobs.rs"]
#[allow(
    dead_code,
    reason = "shared oracle helper used by several test binaries"
)]
mod oracle_blobs;
#[path = "common/r24_member_lease_admission.rs"]
mod r24_member_lease_admission;
#[path = "common/r26_seed_inputs_admission.rs"]
mod r26_seed_inputs_admission;
#[path = "common/r28_seed_http_admission.rs"]
mod r28_seed_http_admission;
#[path = "common/r31_seed_admission.rs"]
mod r31_seed_admission;
use budget_model::inventory::{Candidate, adjudication as adj, candidates};
use std::collections::{BTreeMap, BTreeSet};
const WITNESSES: &str = include_str!("../data/python-ru051-b.tsv");
const HEADER: &str = "key\tfamily\tverdict\torigins\tproducers\ttarget\trationale";

fn census() -> BTreeMap<String, Candidate> {
    candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect()
}
fn check(text: &str, tables: &adj::Tables<'_>) -> Result<(), String> {
    let parsed = adj::parse(tables)?;
    let entries: BTreeMap<_, _> = parsed
        .entries
        .iter()
        .filter(|e| e.partition == 'B')
        .map(|e| (e.key.as_str(), e))
        .collect();
    let census = census();
    let mut lines = text.lines();
    if lines.next() != Some(HEADER) {
        return Err("header".into());
    }
    let mut seen = BTreeSet::new();
    for line in lines {
        let f: Vec<_> = line.split('\t').collect();
        if f.len() != 7 || !seen.insert(f[0]) {
            return Err("shape-or-duplicate".into());
        }
        let c = census.get(f[0]).ok_or("missing-candidate")?;
        if parsed.partitions[&c.key].0 != 'B' {
            return Err("wrong-partition".into());
        }
        if f[2] == "pending" {
            if let Some(e) = entries.get(f[0]) {
                // Keep the historical witness bytes frozen. Only the separate,
                // exact-key AND record-hash-bound r24/r26 cohorts may migrate a row.
                // A matching reviewer prefix alone never grants an exemption.
                if !(r24_member_lease_admission::admitted(&parsed).contains(f[0])
                    && r24_member_lease_admission::exact(e)
                    || r26_seed_inputs_admission::admitted(&parsed).contains(f[0])
                        && r26_seed_inputs_admission::exact(e)
                    || r28_seed_http_admission::admitted(&parsed).contains(f[0])
                        && r28_seed_http_admission::exact(e)
                    || r31_seed_admission::admitted(&parsed).contains(f[0])
                        && r31_seed_admission::exact(e))
                {
                    return Err("pending-has-verdict".into());
                }
                adj::row_of(&parsed, e, &census, &adj::registry)?;
            }
            continue;
        }
        let e = entries.get(f[0]).ok_or("missing-verdict")?;
        if e.verdict != f[2] || e.target != f[5] {
            return Err("verdict-or-target".into());
        }
        for token in e
            .rationale
            .split(|c: char| !c.is_ascii_alphanumeric() && c != '_')
        {
            if token.starts_with("Py")
                && token.as_bytes().get(2).is_some_and(u8::is_ascii_uppercase)
                && adj::registry(token).is_none()
            {
                return Err(format!("unknown-prose-registry-id:{token}"));
            }
        }
        if f[1] == "J-B1" {
            if !matches!(f[2], "derived" | "source-unbounded" | "bounded") {
                return Err("instant-exemption".into());
            }
            if f[4] == "-" {
                return Err("missing-producer".into());
            }
            for site in f[4].split(" | ") {
                if !e.citations.contains(&format!("{site} @ f4e366e9 sha256=")) {
                    return Err("uncited-producer".into());
                }
            }
            if f[2] == "source-unbounded" {
                let proof = parsed.proofs.get(f[0]).ok_or("missing-negative-proof")?;
                for site in f[4].split(" | ") {
                    if !proof.iter().any(|p| {
                        p.kind == "hop"
                            && p.verdict == "origin"
                            && format!("{}:{}", p.path, p.line) == site
                    }) {
                        return Err("producer-not-origin".into());
                    }
                }
            }
        } else if f[1] == "J-B2" {
            if f[2] != "not-a-bound" || f[3] == "-" {
                return Err("missing-carrier-origin".into());
            }
            for key in f[3].split(" | ") {
                if key == f[0] {
                    return Err("self-origin".into());
                }
                let origin = census.get(key).ok_or("missing-origin-candidate")?;
                if !e.rationale.contains(key) {
                    return Err("unnamed-origin".into());
                }
                if !e.citations.contains(&format!(
                    "{}:{} @ f4e366e9 sha256=",
                    origin.path, origin.line
                )) {
                    return Err("uncited-origin".into());
                }
            }
        } else {
            return Err("family".into());
        }
    }
    Ok(())
}

#[test]
fn ru051_original_107_instants_and_40_carriers_are_all_accounted_for() {
    check(WITNESSES, &adj::TABLES).unwrap();
    for (family, count, digest) in [
        (
            "J-B1",
            107,
            "65763ab4b8fc442b3b8f52f8be0937a34c21711f361e96896a31306f947e5e5a",
        ),
        (
            "J-B2",
            40,
            "f1697ec352aac97adb15b05148912034407156cb33afb35370aadbc963a14a2a",
        ),
    ] {
        let keys: BTreeSet<_> = WITNESSES
            .lines()
            .skip(1)
            .filter_map(|l| {
                let f: Vec<_> = l.split('\t').collect();
                (f[1] == family).then_some(f[0])
            })
            .collect();
        assert_eq!(keys.len(), count);
        assert_eq!(
            oracle_blobs::sha256_hex(keys.into_iter().collect::<Vec<_>>().join("\n").as_bytes()),
            digest
        );
    }
}

#[test]
fn ru051_refuses_forged_origin_producer_target_and_pending_claims() {
    let first = WITNESSES
        .lines()
        .skip(1)
        .find(|l| l.split('\t').nth(2) == Some("derived"))
        .unwrap();
    let f: Vec<_> = first.split('\t').collect();
    let forge = |column: usize, value: &str| {
        let mut fields = f.clone();
        fields[column] = value;
        WITNESSES.replacen(first, &fields.join("\t"), 1)
    };
    assert_eq!(
        check(&forge(4, "-"), &adj::TABLES).unwrap_err(),
        "missing-producer"
    );
    assert_eq!(
        check(&forge(4, "mycelium_router/contracts.py:1"), &adj::TABLES).unwrap_err(),
        "uncited-producer"
    );
    assert_eq!(
        check(
            &forge(5, "PyMyceliumDeadlinesPeerConnectDeadlineSecondsL332"),
            &adj::TABLES
        )
        .unwrap_err(),
        "verdict-or-target"
    );
    assert_eq!(
        check(&forge(2, "pending"), &adj::TABLES).unwrap_err(),
        "pending-has-verdict"
    );
    let carrier = WITNESSES
        .lines()
        .skip(1)
        .find(|l| l.split('\t').nth(2) == Some("not-a-bound"))
        .unwrap();
    let mut fields: Vec<_> = carrier.split('\t').collect();
    fields[3] = "-";
    let no_origin = WITNESSES.replacen(carrier, &fields.join("\t"), 1);
    assert_eq!(
        check(&no_origin, &adj::TABLES).unwrap_err(),
        "missing-carrier-origin"
    );
    fields[3] = fields[0];
    let self_origin = WITNESSES.replacen(carrier, &fields.join("\t"), 1);
    assert_eq!(
        check(&self_origin, &adj::TABLES).unwrap_err(),
        "self-origin"
    );
}

/// Proves (partial): B-LIM-01. Re-derive every B negative scan and every cited line,
/// including uncatalogued aliases and helpers; Python is never executed by this gate.
#[test]
fn b_negative_proof_occurrences_and_line_pins_equal_the_exact_oracle() {
    assert!(
        oracle_blobs::oracle_present(),
        "pinned oracle required for this review gate"
    );
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let paths: Vec<_> = parsed.scope.keys().cloned().collect();
    let blobs = oracle_blobs::blobs(&paths);
    let entries: BTreeMap<_, _> = parsed
        .entries
        .iter()
        .chain(&parsed.forwarded_thresholds)
        .filter(|e| e.partition == 'B')
        .map(|e| (e.key.as_str(), e))
        .collect();
    let mut checked = 0;
    for (key, proof) in &parsed.proofs {
        if parsed.partitions[key].0 != 'B' {
            continue;
        }
        assert_eq!(entries[key.as_str()].verdict, "source-unbounded");
        for scan in proof.iter().filter(|p| p.kind == "scan") {
            let mut actual = BTreeSet::new();
            for (path, source) in &blobs {
                if scan.path != "*" && scan.path != *path {
                    continue;
                }
                for (index, line) in source.split('\n').enumerate() {
                    if line.contains(&scan.needle) {
                        actual.insert((path.as_str(), index + 1));
                    }
                }
            }
            let declared: BTreeSet<_> = proof
                .iter()
                .filter(|p| {
                    p.kind == "occurrence"
                        && p.needle == scan.needle
                        && (scan.path == "*" || p.path == scan.path)
                })
                .map(|p| (p.path.as_str(), p.line))
                .collect();
            assert_eq!(actual, declared, "{key}: {} {}", scan.path, scan.needle);
            assert_eq!(actual.len(), scan.line, "{key}: scan count");
            assert!(!actual.is_empty(), "{key}: vacuous scan");
        }
        checked += 1;
    }
    assert_eq!(
        checked,
        entries
            .values()
            .filter(|e| e.verdict == "source-unbounded")
            .count(),
        "every negative verdict must execute its scan proof"
    );
    assert!(checked > 0, "must exercise real negative proofs");
    for line in adj::TABLES.pins_b.lines().skip(1) {
        let f: Vec<_> = line.split('\t').collect();
        let source = oracle_blobs::line_of(&blobs[f[0]], f[1].parse().unwrap()).unwrap();
        assert_eq!(
            oracle_blobs::sha256_hex(source.as_bytes()),
            f[2],
            "{}:{}",
            f[0],
            f[1]
        );
    }
    let map = census();
    adj::rows_with(&adj::TABLES, &map, &adj::registry).unwrap();
}

#[test]
fn inherited_b_rows_outside_ru051_are_byte_unchanged() {
    let reviewed: BTreeSet<_> = WITNESSES
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    let mut retained: Vec<_> = adj::TABLES
        .b
        .lines()
        .skip(1)
        .filter(|l| {
            let f: Vec<_> = l.split('\t').collect();
            !reviewed.contains(f[0]) && matches!(f[5], "L1b-B-r1" | "L1b-r13")
        })
        .collect();
    retained.sort_unstable();
    assert_eq!(retained.len(), 151);
    assert_eq!(
        oracle_blobs::sha256_hex((retained.join("\n") + "\n").as_bytes()),
        "4a383db01188e0835e1a9449c5292c572136cfe8cf0970cf905ef1e8f6a707e4"
    );
}

#[test]
fn b_source_unbounded_proofs_reject_missing_read_origin_and_numeric_substitution() {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let entry = parsed
        .entries
        .iter()
        .find(|e| e.partition == 'B' && e.verdict == "source-unbounded")
        .unwrap();
    let map = census();
    for role in ["origin", "read"] {
        let lines: Vec<_> = adj::TABLES
            .proofs_b
            .lines()
            .filter(|l| {
                let f: Vec<_> = l.split('\t').collect();
                !(f[0] == entry.key && f[1] == "hop" && f[5] == role)
            })
            .collect();
        let forged = lines.join("\n") + "\n";
        let tables = adj::Tables {
            proofs_b: &forged,
            ..adj::TABLES
        };
        let errors = adj::rows_with(&tables, &map, &adj::registry).unwrap_err();
        assert!(
            errors
                .iter()
                .any(|e| e.starts_with(&format!("unbounded-proof-{role}:"))
                    && e.ends_with(&entry.key)),
            "{errors:?}"
        );
    }
    let forged =
        adj::TABLES
            .b
            .replacen(&format!("\t{}\t", entry.target), "\tCancelAndCleanup\t", 1);
    let tables = adj::Tables {
        b: &forged,
        ..adj::TABLES
    };
    let errors = adj::rows_with(&tables, &map, &adj::registry).unwrap_err();
    assert!(errors.iter().any(|e|e.starts_with("unbounded-target-not-blocked-here:") && e.ends_with(&entry.key)),"{errors:?}");
}
