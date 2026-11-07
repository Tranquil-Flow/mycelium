// SPDX-License-Identifier: AGPL-3.0-only
//! RU-044 (round-6 audit, L1b r12): the 8 rows closed as `source-unbounded` that v0.1.4 actually bounds are
//! REVERTED from that family and resolved as bounded budget rows carrying the v0.1.4 value and source line.
//! A bound v0.1.4 enforces is never classed unbounded. The three bounds that had no census row are added as
//! `manual-bound` discovery rows with registry ids. Independent literals below come from the oracle lines.
#[path = "common/oracle_blobs.rs"]
#[allow(
    dead_code,
    reason = "shared test helper: each test binary uses a subset"
)]
mod oracle_blobs;

use budget_model::inventory::{adjudication as adj, candidates, checked_in};
use budgets::{
    BudgetId,
    table::{Provenance, Value},
};
use std::collections::BTreeSet;

const FAMILY: &str = include_str!("../data/python-source-unbounded.tsv");

/// (row key, expected target id, expected canonical (unit, value), oracle lines that bound it)
type Revert = (
    &'static str,
    &'static str,
    &'static str,
    u64,
    &'static [(&'static str, usize)],
);
const REVERTED: &[Revert] = &[
    (
        "mycelium_a10_batch_policy_control.py:101:8:assignment:window",
        "PyMyceliumA10BatchPolicyControlMaximumWindowMsL44",
        "milliseconds",
        250,
        &[
            ("mycelium_a10_batch_policy_control.py", 114),
            ("mycelium_a10_batch_policy_control.py", 44),
        ],
    ),
    (
        "mycelium_a13_macos/adaptive_host.py:117:4:assignment:reserve",
        "PyShiftMyceliumA13MacosAdaptiveHostMaxBytesL88C0",
        "bytes",
        1_125_899_906_842_624,
        &[
            ("mycelium_a13_macos/adaptive_host.py", 118),
            ("mycelium_a13_macos/adaptive_host.py", 88),
        ],
    ),
    (
        "mycelium_a13_macos/swarm_publish.py:302:35:keyword:max_requests",
        "PyMyceliumSwarmDirectoryDocsMaxGrantRequestsL43",
        "entries",
        10_000,
        &[
            ("mycelium_swarm_directory/docs.py", 285),
            ("mycelium_swarm_directory/docs.py", 43),
        ],
    ),
    (
        "mycelium_a7_operator_plan.py:282:9:assignment:configs[standby_node_id]['standby']['max_checkpoint_bytes']",
        "PyMyceliumA7NodeWiringMaxCheckpointBytesL64",
        "bytes",
        268_435_456,
        &[
            ("mycelium_a7_node_wiring.py", 170),
            ("mycelium_a7_node_wiring.py", 64),
        ],
    ),
    (
        "mycelium_a7_node_wiring.py:360:12:keyword:ack_timeout_seconds",
        "PyMyceliumA7NodeWiringEntryTimeoutSecondsMaximumL182",
        "milliseconds",
        30_000,
        &[("mycelium_a7_node_wiring.py", 182)],
    ),
    (
        "mycelium_a7_node_wiring.py:361:12:keyword:receipt_timeout_seconds",
        "PyMyceliumA7NodeWiringEntryTimeoutSecondsMaximumL182",
        "milliseconds",
        30_000,
        &[("mycelium_a7_node_wiring.py", 182)],
    ),
    (
        "mycelium_a13_macos/standing_consent.py:261:47:keyword:max_commands",
        "PyMyceliumA13MacosStandingConsentMaxCommandsMaximumL103",
        "entries",
        10_000,
        &[("mycelium_a13_macos/standing_consent.py", 103)],
    ),
    (
        "mycelium_gate_requirements/binding.py:248:4:assignment:max_age",
        "PyMyceliumGateRequirementsSpecMaxReportAgeSecondsMaximumL213",
        "milliseconds",
        3_600_000,
        &[("mycelium_gate_requirements/spec.py", 213)],
    ),
];

/// The three bounds that had no census row: (new candidate key, path, line, oracle token whose column is pinned)
const DISCOVERED: &[(&str, &str, usize, &str)] = &[
    (
        "mycelium_a7_node_wiring.py:182:",
        "mycelium_a7_node_wiring.py",
        182,
        "30",
    ),
    (
        "mycelium_a13_macos/standing_consent.py:103:",
        "mycelium_a13_macos/standing_consent.py",
        103,
        "10000",
    ),
    (
        "mycelium_gate_requirements/spec.py:213:",
        "mycelium_gate_requirements/spec.py",
        213,
        "3600",
    ),
];

#[test]
fn the_eight_rows_left_the_source_unbounded_family() {
    let reviewed = checked_in().unwrap();
    let family: BTreeSet<&str> = FAMILY
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    for (key, ..) in REVERTED {
        assert!(!family.contains(key), "{key}: still in the family table");
        // no longer reviewed in bounds.tsv: it is adjudicated as a bounded row instead
        assert!(
            reviewed.iter().all(|r| r.key != *key),
            "{key}: still in bounds.tsv"
        );
    }
    assert_eq!(
        family.len(),
        35 - 8,
        "round-6 (24) + o4 (11) minus the eight reverted rows"
    );
    assert_eq!(
        reviewed
            .iter()
            .filter(|r| r.disposition == "source-unbounded")
            .count(),
        35 - 8 + 4,
        "27 family rows + the 4 RU-033 propagated rows"
    );
}

#[test]
fn the_eight_blocked_ids_are_gone_from_the_registry() {
    for id in [
        "PySourceUnboundedMyceliumA10BatchPolicyControlL101C8",
        "PySourceUnboundedMyceliumA13MacosAdaptiveHostL117C4",
        "PySourceUnboundedMyceliumA13MacosSwarmPublishL302C35",
        "PySourceUnboundedMyceliumA7OperatorPlanL282C9",
        "PySourceUnboundedMyceliumA7NodeWiringL360C12",
        "PySourceUnboundedMyceliumA7NodeWiringL361C12",
        "PySourceUnboundedMyceliumA13MacosStandingConsentL261C47",
        "PySourceUnboundedMyceliumGateRequirementsBindingL248C4",
    ] {
        assert!(
            BudgetId::from_name(id).is_none(),
            "{id}: a false 'no ceiling' claim stays in the registry"
        );
    }
}

#[test]
fn each_row_is_adjudicated_bounded_with_the_v014_value_and_its_source_line() {
    let rows = adj::rows().expect("verdicts validate");
    for (key, id, unit, value, lines) in REVERTED {
        let row = rows
            .iter()
            .find(|r| r.key == *key)
            .unwrap_or_else(|| panic!("{key}: no verdict"));
        assert_eq!(row.budget, *id, "{key}");
        assert_eq!(
            (row.unit.as_str(), row.value),
            (*unit, Some(*value)),
            "{key}"
        );
        assert_eq!(
            row.disposition, "",
            "{key}: a bounded row, not a disposition"
        );
        // independent literal: the registry value equals the oracle value (not the implementation's output)
        let entry = BudgetId::from_name(id).unwrap().entry();
        let got = match entry.value {
            Value::Duration(s) => ("milliseconds", s.millis()),
            Value::Storage(b) => ("bytes", b.get()),
            Value::Count(n) => ("entries", n.get()),
            other => panic!("{id}: {other:?}"),
        };
        assert_eq!(got, (*unit, *value), "{id}");
        // the bounding oracle line is cited
        for (path, line) in *lines {
            assert!(
                row.review
                    .contains(&format!("{path}:{line} @ f4e366e9 sha256=")),
                "{key}: {path}:{line}"
            );
        }
        // partition A by RU-044 exception
        let parsed = adj::parse(&adj::TABLES).unwrap();
        assert_eq!(
            parsed.partitions[*key],
            ('A', "ru-044".to_string()),
            "{key}"
        );
    }
}

#[test]
fn the_three_census_gap_bounds_are_new_manual_bound_rows_with_source_registry_ids() {
    let census = candidates().unwrap();
    let blobs = if oracle_blobs::oracle_present() {
        Some(oracle_blobs::blobs(
            &DISCOVERED
                .iter()
                .map(|d| d.1.to_string())
                .collect::<Vec<_>>(),
        ))
    } else {
        None
    };
    for (prefix, path, line, token) in DISCOVERED {
        let new: Vec<_> = census
            .iter()
            .filter(|c| c.key.starts_with(prefix))
            .collect();
        assert_eq!(new.len(), 1, "{prefix}");
        let c = new[0];
        assert_eq!(
            (
                c.path.as_str(),
                c.line,
                c.kind.as_str(),
                c.expression.as_str()
            ),
            (*path, *line, "manual-bound", *token)
        );
        if let Some(blobs) = &blobs {
            let text = oracle_blobs::line_of(&blobs[*path], *line).unwrap();
            assert_eq!(
                text.rfind(token),
                Some(c.column),
                "{prefix}: column of the literal"
            );
        }
    }
    for (_, id, _, _, lines) in REVERTED {
        if let Some((path, line)) = lines.first() {
            let entry = BudgetId::from_name(id).unwrap().entry();
            if let Provenance::Source(text) = entry.provenance
                && (id.contains("EntryTimeout")
                    || id.contains("StandingConsent")
                    || id.contains("ReportAge"))
            {
                assert_eq!(text, format!("{path}:{line} @ f4e366e9"));
            }
        }
    }
}
