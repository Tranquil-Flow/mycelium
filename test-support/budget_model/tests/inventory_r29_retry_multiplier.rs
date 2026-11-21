// SPDX-License-Identifier: AGPL-3.0-only
//! Exact additive source-ratio discovery; old pending set unchanged.
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared source helper")]
mod oracle_blobs;
#[path = "common/r29_retry_admission.rs"]
#[allow(dead_code, reason = "exact discovery cohort")]
mod r29_retry_admission;
#[path = "common/r30_workflow_admission.rs"]
#[allow(dead_code, reason = "exact additive source discovery cohort")]
mod r30_workflow_admission;
#[path = "common/r31_seed_admission.rs"]
mod r31_seed_admission;
#[path = "common/r32_verifier_admission.rs"]
#[allow(
    dead_code,
    reason = "exact historical four-discovery/five-review projection"
)]
mod r32_verifier_admission;
#[path = "common/r36_stats_admission.rs"]
#[allow(dead_code, reason = "exact three-key historical projection")]
mod r36_stats_admission;
#[path = "common/r50_recovery_forward_admission.rs"]
#[allow(dead_code, reason = "exact two-key historical projection")]
mod r50_recovery_forward_admission;
use budget_model::inventory::{self, adjudication as adj, all_reviewed, candidates};
use budgets::{BudgetId, Permille, table::Value};
use std::collections::{BTreeMap, BTreeSet};
const KEY: &str =
    "mycelium_a13_macos/managed_runtime.py:463:93:manual-bound:outbound_retry_multiplier";
const ID: &str = "PyManagedRuntimeOutboundRetryMultiplierL463C93";
const PATH: &str = "mycelium_a13_macos/managed_runtime.py";
const SOURCE_SHA: &str = "a4cdfd23bbc39b0ceb9e14ff423f2cf38a593f32c6a743c8bec6bfea54d9df3c";
#[test]
fn exact_source_ratio_review_uses_existing_manual_engine_not_literal_threshold_extension() {
    assert_eq!(r29_retry_admission::keys(), BTreeSet::from([KEY]));
    let all = candidates().unwrap();
    let c = all.iter().find(|c| c.key == KEY).unwrap();
    assert_eq!(
        (
            &*c.path,
            c.line,
            c.column,
            &*c.scope,
            &*c.symbol,
            &*c.expression,
            &*c.kind
        ),
        (
            PATH,
            463,
            93,
            "ManagedTargetRuntime._serve",
            "outbound_retry_multiplier",
            "2",
            "manual-bound"
        )
    );
    assert_eq!(
        c.source,
        "self._outbound_retry_backoff=min(1.0,max(.1,self._outbound_retry_backoff*2))"
    );
    let rows = all_reviewed().unwrap();
    let row = rows.iter().find(|r| r.key == KEY).unwrap();
    assert_eq!(
        (&*row.unit, row.value, &*row.budget, &*row.source_sha),
        ("permille", Some(2000), ID, oracle_blobs::ORACLE)
    );
    assert!(row.review.starts_with("manual-adjudication:A;bounded;"));
    inventory::validate(std::slice::from_ref(row)).unwrap();
    assert_eq!(
        BudgetId::from_name(ID).unwrap().entry().value,
        Value::Ratio(Permille::new(2000))
    );
    // The narrow generic ratio rule intentionally remains capped at one.
    assert_eq!(inventory::ratio::permille("2"), None);
    assert_eq!(inventory::ratio::permille("1"), Some(1000));
}
#[test]
fn exact_f4_source_and_historical_init_reset_equivalence_are_pinned_not_origin_aliases() {
    let source = oracle_blobs::blobs(&[PATH.into()])[PATH].clone();
    assert_eq!(oracle_blobs::sha256_hex(source.as_bytes()), SOURCE_SHA);
    let old = std::process::Command::new("git")
        .arg("-C")
        .arg(oracle_blobs::oracle_dir())
        .args([
            "show",
            "ef9ae61de0222c78d83c8133f8f709619394bf51:mycelium_a13_macos/managed_runtime.py",
        ])
        .output()
        .unwrap();
    assert!(old.status.success());
    assert_eq!(old.stdout, source.as_bytes());
    let expected = [
        (155, "        self._outbound_retry_backoff=.1"),
        (
            463,
            "                    self._outbound_retry_backoff=min(1.0,max(.1,self._outbound_retry_backoff*2))",
        ),
        (471, "                    self._outbound_retry_backoff=.1"),
    ];
    let parsed = adj::parse(&adj::TABLES).unwrap();
    for (line, text) in expected {
        assert_eq!(oracle_blobs::line_of(&source, line), Some(text));
        assert_eq!(
            parsed.pins[&(PATH.into(), line)],
            oracle_blobs::sha256_hex(text.as_bytes())
        );
    }
    let stores: Vec<_> = source
        .lines()
        .enumerate()
        .filter(|(_, l)| l.trim_start().starts_with("self._outbound_retry_backoff="))
        .map(|(i, _)| i + 1)
        .collect();
    assert_eq!(stores, [155, 463, 471]);
    assert_eq!(
        oracle_blobs::line_of(&source, 469),
        Some("                    self._stop.wait(delay)")
    );
    assert_eq!(
        oracle_blobs::line_of(&source, 479),
        Some("                    self.pump.wait_for_wake(delay)")
    );
    assert_eq!(&source.lines().nth(462).unwrap()[93..94], "2");
}
#[test]
fn one_discovery_one_review_leaves_exact_post_r28_pending_set_unchanged() {
    let mut all: BTreeSet<_> = candidates().unwrap().into_iter().map(|c| c.key).collect();
    let rows = all_reviewed().unwrap();
    let mut reviewed: BTreeSet<_> = rows.iter().map(|r| r.key.clone()).collect();
    assert_eq!(rows.len(), reviewed.len());
    // Historical pre-R31 view; live closure is independently pinned by inventory_r31_seed_remaining.
    let r31 = r31_seed_admission::admitted(
        &budget_model::inventory::adjudication::parse(
            &budget_model::inventory::adjudication::TABLES,
        )
        .unwrap(),
    );
    for key in r31 {
        assert!(reviewed.remove(key));
    }
    r32_verifier_admission::remove_from(&mut all, &mut reviewed);
    r36_stats_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::lane_a::remove_from_reviewed(&mut reviewed);
    let pending: BTreeSet<_> = all.difference(&reviewed).map(String::as_str).collect();
    let expected: BTreeSet<_> = include_str!("../data/python-r28-prior-pending.tsv")
        .lines()
        .skip(1)
        .filter(|k| *k != "mycelium_seed/http.py:968:8:assignment:expires")
        .collect();
    assert_eq!(pending, expected);
    assert_eq!(
        (all.len(), reviewed.len(), pending.len()),
        (
            8257 + r30_workflow_admission::keys().len(),
            8188 + r30_workflow_admission::keys().len(),
            69
        )
    );
    let frozen: BTreeSet<_> = include_str!("../data/python-r17-frozen-b334.tsv")
        .lines()
        .skip(1)
        .collect();
    assert_eq!(pending.intersection(&frozen).count(), 44);
    assert_eq!(pending.difference(&frozen).count(), 25);
    assert!(reviewed.contains(KEY));
}
#[test]
fn prior_prefixes_and_scope_are_preserved_with_one_exact_addition() {
    for (name, prior_lines, prior_hash) in PRIOR {
        let text = match *name {
            "python-candidates.tsv" => include_str!("../data/python-candidates.tsv"),
            "python-adjudication-a.tsv" => include_str!("../data/python-adjudication-a.tsv"),
            "python-adjudication-partitions.tsv" => {
                include_str!("../data/python-adjudication-partitions.tsv")
            }
            _ => panic!("unexpected fixture"),
        };
        assert_eq!(
            text.lines().count()
                - if *name == "python-candidates.tsv"
                    || *name == "python-adjudication-partitions.tsv"
                {
                    r32_verifier_admission::keys().len()
                } else {
                    0
                }
                - if *name == "python-candidates.tsv" {
                    r30_workflow_admission::keys().len()
                } else {
                    0
                },
            prior_lines + 1
        );
        let prefix = text
            .lines()
            .take(*prior_lines)
            .collect::<Vec<_>>()
            .join("\n")
            + "\n";
        assert_eq!(oracle_blobs::sha256_hex(prefix.as_bytes()), *prior_hash);
        assert_eq!(
            text.lines().nth(*prior_lines).unwrap().split('\t').next(),
            Some(KEY)
        );
    }
    let mut scope = String::new();
    for line in include_str!("../data/python-scope.tsv").lines() {
        let mut f: Vec<_> = line.split('\t').map(String::from).collect();
        if f[0] == PATH {
            assert_eq!(f[3], "24");
            f[3] = "23".into();
        }
        if f[0] == "mycelium_a13_macos/workflow.py" {
            f[3] =
                (f[3].parse::<usize>().unwrap() - r30_workflow_admission::keys().len()).to_string();
        }
        if f[0] == r32_verifier_admission::PATH {
            assert_eq!(f[3], "18");
            f[3] =
                (f[3].parse::<usize>().unwrap() - r32_verifier_admission::keys().len()).to_string();
        }
        scope.push_str(&f.join("\t"));
        scope.push('\n');
    }
    assert_eq!(oracle_blobs::sha256_hex(scope.as_bytes()), SCOPE_PRIOR_SHA);
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let e = parsed.entries.iter().find(|e| e.key == KEY).unwrap();
    assert_eq!(e.partition, 'A');
    assert_eq!((&*e.verdict, &*e.target), ("bounded", ID));
    let map: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    assert!(adj::row_of(&parsed, e, &map, &adj::registry).is_ok());
}
#[test]
fn original_ast_arithmetic_fixture_is_not_a_product_runtime_execution_claim() {
    let fixture = include_str!("../data/python-r29-source-argument.json");
    assert_eq!(oracle_blobs::sha256_hex(fixture.as_bytes()), FIXTURE_SHA);
    assert!(fixture.contains("Exact original AST statements executed"));
    assert!(fixture.contains("min(1.0, max(0.1, self._outbound_retry_backoff * 2))"));
}
// Filled once from immutable r28 prefix and independently executed original-source fixture.
const PRIOR: &[(&str, usize, &str)] = &[
    (
        "python-candidates.tsv",
        8257,
        "51861138067135b320d3c371783de5abe65294d5e98f197f8ffc7a528f04461a",
    ),
    (
        "python-adjudication-a.tsv",
        572,
        "85fde7dbe8b80def1ae47f5dd9f63514c592eab6b2da21b8690bcbc2b800ad3c",
    ),
    (
        "python-adjudication-partitions.tsv",
        1300,
        "1d380103733722cb1e3b7e99575af98a82febf6bbf5fd7ea92db2e193fe637f0",
    ),
];
const SCOPE_PRIOR_SHA: &str = "da3504f892669b6b11b8512c68a0a281ccf34e4131ea87d485eb382c41d1a001";
const FIXTURE_SHA: &str = "e4394ceaaf84810dcdf92b22f5cabcff716a27408024887ad5b115a2323880d2";
