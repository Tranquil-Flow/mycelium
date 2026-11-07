// SPDX-License-Identifier: AGPL-3.0-only
//! Static exact AST-derived source site, not a measured delivery or native production loop.
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only source helper")]
mod oracle_blobs;
#[path = "common/r27_delivery_wake_admission.rs"]
#[allow(dead_code, reason = "shared exact additive cohort helper")]
mod r27_delivery_wake_admission;
#[path = "common/r28_seed_http_admission.rs"]
mod r28_seed_http_admission;
#[path = "common/r29_retry_admission.rs"]
#[allow(dead_code, reason = "exact additive discovery cohort")]
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
use budget_model::inventory::{self, Candidate};
#[test]
fn exact_addition_preserves_every_prior_census_review_and_witness_byte() {
    assert_eq!(r27_delivery_wake_admission::keys(), BTreeSet::from([KEY]));
    let later = r29_retry_admission::keys();
    for (text, lines, digest) in [
        (
            include_str!("../data/python-candidates.tsv"),
            8256,
            "17cdbe8b0f4645a359b158d0e63ae5aec23f8ac6716dcab4601464c4898ecee8",
        ),
        (
            include_str!("../data/python-bounds.tsv"),
            6957,
            "db31660004f0978e25ae37c31e7a4197b3039c6a28856045e930e77bed28ae2e",
        ),
        (
            include_str!("../data/python-final-review-sites.tsv"),
            151,
            "1718d7e5733c52dce780a25a82d063a85430527c69605d8a0eecaee09a3132b7",
        ),
    ] {
        let prefix: String = text.split_inclusive('\n').take(lines).collect();
        assert_eq!(oracle_blobs::sha256_hex(prefix.as_bytes()), digest);
        assert_eq!(
            text.lines().count()
                - r30_workflow_admission::keys().len()
                - if text == include_str!("../data/python-candidates.tsv") {
                    later.len() + r32_verifier_admission::keys().len()
                } else {
                    0
                },
            lines + 1
        );
        assert_eq!(
            text.lines().nth(lines).unwrap().split('\t').next(),
            Some(KEY)
        );
    }
    let mut scope = String::new();
    for line in include_str!("../data/python-scope.tsv").lines() {
        let mut f: Vec<_> = line.split('\t').map(str::to_string).collect();
        if f[0] == PATH {
            f[3] = (f[3].parse::<usize>().unwrap() - 1 - later.len()).to_string();
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
    assert_eq!(
        oracle_blobs::sha256_hex(scope.as_bytes()),
        "37203fd24997f9924d6f91567a8463d34f191a53d0c433bcad48317a58aab56a"
    );
}
use std::collections::BTreeSet;
const KEY: &str = "mycelium_a13_macos/managed_runtime.py:461:16:assignment:delay";
const NAME: &str = "PyManagedRuntimeDeliveryWakeCeilingL461C16";
const PATH: &str = "mycelium_a13_macos/managed_runtime.py";
fn exact(c: &Candidate) -> bool {
    c.key == KEY
        && c.path == PATH
        && c.line == 461
        && c.column == 16
        && c.scope == "ManagedTargetRuntime._serve"
        && c.symbol == "delay"
        && c.expression == "0.05"
        && c.kind == "assignment"
        && c.source == "delay=.05"
}
#[test]
fn exact_ast_discovery_has_numeric_review_and_original_f4_witness() {
    let rows = inventory::candidates().unwrap();
    let c = rows
        .iter()
        .find(|c| c.key == KEY)
        .expect("missing AST-derived delivery-wake census site");
    assert!(exact(c));
    let reviews = inventory::checked_in().unwrap();
    let r = reviews
        .iter()
        .find(|r| r.key == KEY)
        .expect("missing exact numeric review");
    assert_eq!(
        (&*r.unit, r.value, &*r.budget, &*r.source_sha),
        ("milliseconds", Some(50), NAME, oracle_blobs::ORACLE)
    );
    assert!(r.disposition.is_empty());
    assert_eq!(inventory::validate(std::slice::from_ref(r)), Ok(()));
    for field in [
        "key",
        "scope",
        "symbol",
        "expression",
        "kind",
        "source",
        "column",
        "line",
        "path",
    ] {
        let mut bad = c.clone();
        match field {
            "key" => bad.key.push_str("-wrong"),
            "scope" => bad.scope.push_str(".wrong"),
            "symbol" => bad.symbol.push_str("_wrong"),
            "expression" => bad.expression = "0.1".into(),
            "kind" => bad.kind = "keyword".into(),
            "source" => bad.source = "delay=.1".into(),
            "column" => bad.column += 1,
            "line" => bad.line += 1,
            _ => bad.path.push_str(".wrong"),
        }
        assert!(!exact(&bad), "{field}");
    }
    let mut bad = r.clone();
    bad.source_sha = inventory::SOURCE_SHA.into();
    assert!(inventory::validate(&[bad]).is_err());
    let mut bad = r.clone();
    bad.value = Some(51);
    assert!(inventory::validate(&[bad]).is_err());
    let mut bad = r.clone();
    bad.unit = "entries".into();
    assert!(inventory::validate(&[bad]).is_err());
}
#[test]
fn nonretry_delivery_path_and_distinct_retry_branch_are_source_pinned() {
    assert!(oracle_blobs::oracle_present());
    let blobs = oracle_blobs::blobs(&[PATH.into()]);
    let text = &blobs[PATH];
    assert_eq!(
        oracle_blobs::sha256_hex(text.as_bytes()),
        "a4cdfd23bbc39b0ceb9e14ff423f2cf38a593f32c6a743c8bec6bfea54d9df3c"
    );
    for (line, expected) in [
        (461, "                delay=.05"),
        (
            464,
            "                    delay=self._outbound_retry_backoff",
        ),
        (469, "                    self._stop.wait(delay)"),
        (471, "                    self._outbound_retry_backoff=.1"),
        (479, "                    self.pump.wait_for_wake(delay)"),
    ] {
        assert_eq!(oracle_blobs::line_of(text, line), Some(expected));
    }
    let assignments: Vec<_> = text
        .lines()
        .enumerate()
        .filter(|(i, l)| *i >= 446 && *i < 479 && l.trim_start().starts_with("delay="))
        .map(|(i, _)| i + 1)
        .collect();
    assert_eq!(assignments, vec![461, 464]);
    // These two already-admitted policies are retained, not duplicate declarations.
    for (id, ms) in [
        (
            "PyFinalMyceliumA13MacosManagedRuntimeL463C53CeilingOperand",
            1000,
        ),
        (
            "PyFinalMyceliumA13MacosManagedRuntimeL463C61ComparisonLower",
            100,
        ),
    ] {
        let id = budgets::BudgetId::from_name(id).unwrap();
        assert_eq!(id.span(budgets::Entries::new(0)).unwrap().millis(), ms);
        assert_eq!(
            id.entry().provenance,
            budgets::table::Provenance::Source(
                "mycelium_a13_macos/managed_runtime.py:463 @ f4e366e9"
            )
        );
    }
}
#[test]
fn one_discovered_reviewed_site_never_closes_the_old_seventy() {
    let later = r29_retry_admission::keys();
    let mut all = inventory::candidate_keys().unwrap();
    let mut reviewed: BTreeSet<_> = inventory::all_reviewed()
        .unwrap()
        .into_iter()
        .map(|r| r.key)
        .collect();
    r32_verifier_admission::remove_from(&mut all, &mut reviewed);
    r36_stats_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::lane_a::remove_from_reviewed(&mut reviewed);
    let pending: BTreeSet<_> = all.difference(&reviewed).map(String::as_str).collect();
    let r26: BTreeSet<_> = [
        "mycelium_seed/coordinator.py:1310:20:assignment:request_expiry",
        "mycelium_seed/coordinator.py:1385:20:keyword:previous_deadline",
    ]
    .into_iter()
    .collect();
    let before: BTreeSet<_> = include_str!("../data/python-r26-prior-pending.tsv")
        .lines()
        .skip(1)
        .collect();
    let mut r28 = r28_seed_http_admission::admitted(
        &budget_model::inventory::adjudication::parse(
            &budget_model::inventory::adjudication::TABLES,
        )
        .unwrap(),
    );
    let r31 = r31_seed_admission::admitted(
        &budget_model::inventory::adjudication::parse(
            &budget_model::inventory::adjudication::TABLES,
        )
        .unwrap(),
    );
    assert!(r28.is_disjoint(&r31));
    r28.extend(r31);
    let removed: BTreeSet<_> = r26.union(&r28).copied().collect();
    assert_eq!(pending, before.difference(&removed).copied().collect());
    assert_eq!(
        (all.len(), reviewed.len(), pending.len()),
        (
            8256 + later.len() + r30_workflow_admission::keys().len(),
            8186 + r28.len() + later.len() + r30_workflow_admission::keys().len(),
            70 - r28.len()
        )
    );
    assert!(!before.contains(KEY));
}
