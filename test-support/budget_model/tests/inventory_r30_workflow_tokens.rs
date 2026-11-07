// SPDX-License-Identifier: AGPL-3.0-only
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "source helper")]
mod oracle_blobs;
#[path = "common/r30_workflow_admission.rs"]
#[allow(dead_code, reason = "exact additive cohort")]
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
use std::collections::BTreeSet;
const KEY: &str =
    "mycelium_a13_macos/workflow.py:206:66:manual-bound:verified_challenge_token_limit.maximum";
const ID: &str = "PyManagedWorkflowVerifiedChallengeTokenLimitL206C66";
const PATH: &str = "mycelium_a13_macos/workflow.py";
fn exact(c: &Candidate) -> bool {
    c.key == KEY
        && c.path == PATH
        && c.line == 206
        && c.column == 66
        && c.scope == "ManagedServingWorkflow._challenge_event"
        && c.symbol == "verified_challenge_token_limit.maximum"
        && c.expression == "4096"
        && c.kind == "manual-bound"
        && c.source == SOURCE_LINE
}
#[test]
fn exact_callback_operand_has_independent_count_policy_and_f4_witness() {
    assert_eq!(r30_workflow_admission::keys(), BTreeSet::from([KEY]));
    let all = inventory::candidates().unwrap();
    let c = all.iter().find(|c| c.key == KEY).unwrap();
    assert!(exact(c));
    let rows = inventory::checked_in().unwrap();
    let r = rows.iter().find(|r| r.key == KEY).unwrap();
    assert_eq!(
        (&*r.unit, r.value, &*r.budget, &*r.source_sha),
        ("entries", Some(4096), ID, oracle_blobs::ORACLE)
    );
    inventory::validate(std::slice::from_ref(r)).unwrap();
    for field in [
        "scope",
        "symbol",
        "expression",
        "kind",
        "column",
        "line",
        "path",
        "source",
    ] {
        let mut bad = c.clone();
        match field {
            "scope" => bad.scope.push_str(".wrong"),
            "symbol" => bad.symbol.push_str("wrong"),
            "expression" => bad.expression = "4097".into(),
            "kind" => bad.kind = "keyword".into(),
            "column" => bad.column += 1,
            "line" => bad.line += 1,
            "path" => bad.path.push_str("wrong"),
            _ => bad.source.push_str("wrong"),
        };
        assert!(!exact(&bad), "{field}");
    }
    let mut bad = r.clone();
    bad.value = Some(4097);
    assert!(inventory::validate(&[bad]).is_err());
    let mut bad = r.clone();
    bad.unit = "milliseconds".into();
    assert!(inventory::validate(&[bad]).is_err());
}
#[test]
fn original_exact_int_guard_and_token_count_consumers_are_source_pinned() {
    assert!(oracle_blobs::oracle_present());
    let text = &oracle_blobs::blobs(&[PATH.into()])[PATH];
    assert_eq!(
        oracle_blobs::sha256_hex(text.as_bytes()),
        "78061c846354953010b0ac603aebc5d5f5c1192baafe48c97a3f3e4b51516b51"
    );
    for (line, expected) in SOURCE_LINES {
        assert_eq!(oracle_blobs::line_of(text, *line), Some(*expected));
    }
    assert_eq!(&text.lines().nth(205).unwrap()[66..70], "4096");
    let raw = include_str!("../data/python-r30-source-argument.json");
    assert_eq!(oracle_blobs::sha256_hex(raw.as_bytes()), FIXTURE_SHA);
    assert!(raw.contains("\"case_count\": 22"));
    assert!(raw.contains("full-max-then-next-refused"));
    assert!(raw.contains("\"case\": \"bool\""));
    assert!(raw.contains("no workflow launch"));
}
#[test]
fn exact_one_discovery_review_preserves_prefixes_scope_and_old_sixty_nine() {
    for (name, n, h) in PRIOR {
        let text = match *name {
            "python-candidates.tsv" => include_str!("../data/python-candidates.tsv"),
            "python-bounds.tsv" => include_str!("../data/python-bounds.tsv"),
            "python-final-review-sites.tsv" => {
                include_str!("../data/python-final-review-sites.tsv")
            }
            _ => panic!("fixture"),
        };
        let historic: String = text
            .split_inclusive('\n')
            .filter(|l| !r32_verifier_admission::keys().contains(l.split('\t').next().unwrap()))
            .collect();
        let text = historic.as_str();
        assert_eq!(text.lines().count(), n + 1);
        let prefix: String = text.split_inclusive('\n').take(*n).collect();
        assert_eq!(oracle_blobs::sha256_hex(prefix.as_bytes()), *h);
        assert_eq!(text.lines().last().unwrap().split('\t').next(), Some(KEY));
    }
    let mut old = String::new();
    for l in include_str!("../data/python-scope.tsv").lines() {
        let mut f: Vec<_> = l.split('\t').map(String::from).collect();
        if f[0] == PATH {
            assert_eq!(f[3], "14");
            f[3] = "13".into();
        }
        if f[0] == r32_verifier_admission::PATH {
            assert_eq!(f[3], "18");
            f[3] =
                (f[3].parse::<usize>().unwrap() - r32_verifier_admission::keys().len()).to_string();
        }
        old.push_str(&f.join("\t"));
        old.push('\n');
    }
    assert_eq!(oracle_blobs::sha256_hex(old.as_bytes()), SCOPE_SHA);
    let all = inventory::candidate_keys().unwrap();
    let rows = inventory::all_reviewed().unwrap();
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
    let mut all = all;
    r32_verifier_admission::remove_from(&mut all, &mut reviewed);
    r36_stats_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::lane_a::remove_from_reviewed(&mut reviewed);
    let pending: BTreeSet<_> = all.difference(&reviewed).map(String::as_str).collect();
    let old: BTreeSet<_> = include_str!("../data/python-r28-prior-pending.tsv")
        .lines()
        .skip(1)
        .filter(|k| *k != "mycelium_seed/http.py:968:8:assignment:expires")
        .collect();
    assert_eq!(pending, old);
    assert_eq!((all.len(), reviewed.len(), pending.len()), (8258, 8189, 69));
}

const SOURCE_LINE: &str = "or type(limit) is not int or not 0 < limit <= 4096):";
const FIXTURE_SHA: &str = "a37e0c07056b1d5b003e39ecd951e747d26c174ed47f96219e9352781c58e8e3";
const SCOPE_SHA: &str = "5b732722c80881de49597c7b697afe3a94920c338a527cf196c2291976f5dcbc";
const PRIOR: &[(&str, usize, &str)] = &[
    (
        "python-candidates.tsv",
        8258,
        "3030b1cc39ca549040d639cd24dc223bad4f3ad0a4507db29f461adefb0a7913",
    ),
    (
        "python-bounds.tsv",
        6958,
        "513648b09c9227c926cb844ff4f7ca2ac7af7b2a7c0c6988efb649f634b3ab7c",
    ),
    (
        "python-final-review-sites.tsv",
        152,
        "80bea2cf79de1c6bce08ff02fbf3e38d377e87cc7e10a9a96d87e89b5b2a339b",
    ),
];
const SOURCE_LINES: &[(usize, &str)] = &[
    (
        195,
        "    def _challenge_event(self, deployment_id, request_id, kind, index, limit):",
    ),
    (
        196,
        "        # Only the supervisor's verified-route sink supplies token indices.",
    ),
    (
        197,
        "        # Never call a user callback, do RPC, or retain token values under this lock.",
    ),
    (198, "        import re"),
    (199, "        with self._lock:"),
    (
        200,
        "            if (self.stop_event.is_set() or self._state!='starting'",
    ),
    (
        201,
        "                    or self._progress_identity is None",
    ),
    (
        202,
        "                    or deployment_id!=self._progress_identity[1]",
    ),
    (203, "                    or type(request_id) is not str"),
    (204, "                    or len(request_id)!=40"),
    (
        205,
        "                    or re.fullmatch(r'startup-[0-9a-f]{32}',request_id) is None",
    ),
    (
        206,
        "                    or type(limit) is not int or not 0 < limit <= 4096):",
    ),
    (207, "                return"),
    (208, "            if kind=='begin' and index == -1:"),
    (209, "                if self._challenge_request is None:"),
    (
        210,
        "                    self._challenge_request=request_id",
    ),
    (211, "                    self._challenge_limit=limit"),
    (212, "                return"),
    (
        213,
        "            if (kind=='token' and request_id==self._challenge_request",
    ),
    (
        214,
        "                    and limit==self._challenge_limit and type(index) is int",
    ),
    (
        215,
        "                    and index==self._challenge_count and index<limit):",
    ),
    (216, "                self._challenge_count+=1"),
    (
        242,
        "                'challenge_progress':({'verified_token_count':self._challenge_count}",
    ),
    (
        332,
        "                **({'on_challenge_event':self._challenge_event}",
    ),
    (
        333,
        "                   if self._progress_identity is not None else {}))",
    ),
];
