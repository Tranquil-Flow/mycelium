// SPDX-License-Identifier: AGPL-3.0-only
//! RU-045 / judgement J1: `swarm_artifacts._integer`'s default `maximum=2**63-1` is an int64 TYPE-RANGE guard,
//! not a policy ceiling. The Rust port enforces it by the `i64` type plus the same refusal at that edge. This
//! test proves the edge against EXECUTED oracle output (FX-L1b-int64-type-guard): the probe imported v0.1.4
//! `mycelium_swarm_artifacts._integer` at f4e366e9 under Python 3.14 and recorded accept/refuse for 11 inputs
//! (probe sha256 60b7e4b8787f4e77e378ef8eb962629bfefc799faa4d9f1808645c7aea7e02e8, fixture sha256 pinned below).
#[path = "common/oracle_blobs.rs"]
#[allow(
    dead_code,
    reason = "shared test helper: each test binary uses a subset"
)]
mod oracle_blobs;

use budget_model::inventory::checked_in;

const FIXTURE: &str = include_str!("../data/fx-l1b-int64-type-guard.tsv");
const FIXTURE_SHA256: &str = "0a9ae30389838e1a4c62c2759e4d2ae31aa510dbb5cd680cc974215609d0ffcd";

/// The port's edge: a wire integer is an `i64`; anything outside the type refuses with the document's code.
fn integer(wire: &str, minimum: i64) -> Result<i64, &'static str> {
    // bool and float are refused by the type check (`type(value) is not int`), never coerced
    let value: i128 = wire.parse().map_err(|_| "probe_invalid")?;
    let value = i64::try_from(value).map_err(|_| "probe_invalid")?;
    if value >= minimum {
        Ok(value)
    } else {
        Err("probe_invalid")
    }
}

#[test]
fn fixture_is_pinned() {
    assert_eq!(oracle_blobs::sha256_hex(FIXTURE.as_bytes()), FIXTURE_SHA256);
    assert_eq!(FIXTURE.lines().count(), 12, "11 cases + header");
}

#[test]
fn the_port_edge_agrees_with_the_executed_oracle_on_every_case() {
    for line in FIXTURE.lines().skip(1) {
        let f: Vec<_> = line.split('\t').collect();
        assert_eq!(f.len(), 4, "{line}");
        let (name, value, outcome, result) = (f[0], f[1], f[2], f[3]);
        let ours = integer(value, 0);
        match outcome {
            "accept" => assert_eq!(
                ours.map(|v| v.to_string()),
                Ok(result.to_string()),
                "{name}"
            ),
            "refuse" => assert_eq!(ours, Err("probe_invalid"), "{name}: {value}"),
            other => panic!("{name}: {other}"),
        }
    }
}

#[test]
fn the_boundary_is_exactly_two_pow_63_minus_one_and_its_overflow() {
    // independent of the fixture: the literal bound and the type bound coincide
    assert_eq!(i64::MAX.to_string(), "9223372036854775807");
    assert_eq!(2i128.pow(63) - 1, i128::from(i64::MAX));
    assert_eq!(integer("9223372036854775807", 0), Ok(i64::MAX));
    assert_eq!(integer("9223372036854775808", 0), Err("probe_invalid"));
    let case = |name: &str| {
        FIXTURE
            .lines()
            .find(|l| l.starts_with(&format!("{name}\t")))
            .map(|l| l.split('\t').nth(2).unwrap())
    };
    assert_eq!(case("int64_max"), Some("accept"));
    assert_eq!(case("int64_max_plus_1"), Some("refuse"));
}

#[test]
fn the_four_swarm_artifacts_rows_still_say_type_guard_not_policy() {
    // 4 family rows read 3 fields whose helper default is the int64 range: the occurrence table verdicts them
    // `type-guard` (not a ceiling) and the family keeps them source-unbounded (no policy ceiling exists).
    let family = include_str!("../data/python-source-unbounded.tsv");
    let occurrences = include_str!("../data/python-field-occurrences.tsv");
    let rows = checked_in().unwrap();
    let mut guarded = 0;
    for (key_prefix, field) in [
        ("mycelium_swarm_artifacts.py:1069:", "disk_reserve_bytes"),
        ("mycelium_swarm_artifacts.py:830:", "maximum_total_bytes"),
        ("mycelium_live/local_preparer.py:1571:", "total_size_bytes"),
        (
            "mycelium_live/member_transport.py:1041:",
            "total_size_bytes",
        ),
    ] {
        let row = rows
            .iter()
            .find(|r| r.key.starts_with(key_prefix))
            .unwrap_or_else(|| panic!("{key_prefix}"));
        assert_eq!(row.disposition, "source-unbounded", "{key_prefix}");
        assert!(
            family
                .lines()
                .any(|l| l.starts_with(&row.key) && l.split('\t').nth(2) == Some(field)),
            "{key_prefix}"
        );
        assert!(
            occurrences
                .lines()
                .any(|l| l.starts_with(&format!("{field}\t"))
                    && l.split('\t').nth(3) == Some("type-guard")),
            "{field}: no type-guard verdict"
        );
        guarded += 1;
    }
    assert_eq!(guarded, 4);
}
