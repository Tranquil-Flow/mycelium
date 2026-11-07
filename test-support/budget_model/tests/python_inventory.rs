// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_deadlines.py, O:path:mycelium_a13_macos/mailbox_retention.py.
#[path = "common/r29_retry_admission.rs"]
#[allow(
    dead_code,
    reason = "exact new manual source operand, not historical census"
)]
mod r29_retry_admission;
use budget_model::inventory::{
    CENSUS_SOURCE_SHA, CURRENT_SOURCE_SHA, PREVIOUS_SOURCE_SHA, REPIN_HASH, REPIN_OLD_HASH,
    REPIN_PATH, SOURCE_SHA, candidate_keys, checked_in, require_complete, validate,
};
use budgets::table::{BUDGET_TABLE, Provenance};
use std::collections::{BTreeMap, BTreeSet};

#[test]
fn checked_in_reviewed_inventory_has_no_missing_or_dangling_mapping() {
    let rows = checked_in().unwrap();
    assert!(!rows.is_empty());
    assert_eq!(validate(&rows), Ok(()));
    let candidates: BTreeMap<_, _> = include_str!("../data/python-candidates.tsv")
        .lines()
        .skip(1)
        .map(|line| {
            let fields: Vec<_> = line.split('\t').collect();
            (fields[0], fields)
        })
        .collect();
    for row in &rows {
        let candidate = &candidates[row.key.as_str()];
        assert_eq!(candidate[1], row.path);
        assert_eq!(candidate[2].parse::<usize>().unwrap(), row.line);
        assert_eq!(candidate[3].parse::<usize>().unwrap(), row.column);
        assert_eq!(candidate[4], row.scope);
        assert_eq!(candidate[5], row.symbol);
        assert_eq!(candidate[6], row.expression);
        assert!(
            matches!(
                row.source_sha.as_str(),
                SOURCE_SHA | PREVIOUS_SOURCE_SHA | CURRENT_SOURCE_SHA
            ) || (row.source_sha == CENSUS_SOURCE_SHA
                && include_str!("../data/python-final-review-sites.tsv")
                    .lines()
                    .skip(1)
                    .any(|line| line
                        == format!("{}\t{}\t{}", row.key, CENSUS_SOURCE_SHA, candidate[8])))
        );
    }
}

#[test]
fn every_new_registry_id_is_used_and_has_exact_site_provenance() {
    // RU-042: ids introduced by manual-adjudication verdicts are used by those verdict rows.
    let rows = budget_model::inventory::all_reviewed().unwrap();
    let ids: BTreeSet<_> = BUDGET_TABLE.iter().map(|entry| entry.id.name()).collect();
    assert_eq!(ids.len(), BUDGET_TABLE.len());
    for entry in BUDGET_TABLE {
        assert_eq!(
            budgets::BudgetId::from_name(entry.id.name()),
            Some(entry.id)
        );
        if entry.id.name().starts_with("Py") {
            let matching: Vec<_> = rows
                .iter()
                .filter(|row| {
                    row.budget == entry.id.name()
                        || (row.disposition == "derived" && row.relation == entry.id.name())
                        || (row.disposition == "source-unbounded"
                            && row.relation == entry.id.name())
                })
                .collect();
            assert!(
                !matching.is_empty(),
                "orphan registry id {}",
                entry.id.name()
            );
            match entry.provenance {
                Provenance::Source(source) => {
                    assert!(matching.iter().any(|row| source
                        == format!("{}:{} @ {}", row.path, row.line, &row.source_sha[..8])))
                }
                // Owner ruling option a: a source-unbounded id is Blocked but still cites its exact site.
                Provenance::Blocked(text) if entry.id.name().starts_with("PySourceUnbounded") => {
                    assert!(matching.iter().any(|row| {
                        row.disposition == "source-unbounded"
                            && text.starts_with(&format!(
                                "v0.1.4 ships no ceiling at {}:{} @ {}; field ",
                                row.path,
                                row.line,
                                &row.source_sha[..8]
                            ))
                    }));
                }
                _ => panic!("source value masquerading as measurement"),
            }
        }
    }
    let legacy: BTreeSet<_> = include_str!("../data/legacy-budget-ids.txt")
        .lines()
        .collect();
    assert_eq!(
        ids.iter()
            .copied()
            .filter(|name| !name.starts_with("Py"))
            .collect::<BTreeSet<_>>(),
        legacy
    );
}

#[test]
fn whole_scope_gate_refuses_pending_candidates_instead_of_blessing_subset() {
    let rows = checked_in().unwrap();
    let keys = candidate_keys().unwrap();
    let reviewed: BTreeSet<_> = rows.iter().map(|row| row.key.clone()).collect();
    assert!(reviewed.is_subset(&keys));
    let missing: Vec<_> = keys.difference(&reviewed).collect();
    // A WIP has an executable fail-closed gate, not a pretend complete inventory.
    let result = require_complete(&rows, &keys);
    if missing.is_empty() {
        assert_eq!(result, Ok(()));
    } else {
        let failures = result.unwrap_err();
        assert_eq!(failures.len(), missing.len());
        assert!(
            failures
                .iter()
                .all(|error| error.starts_with("unreviewed-candidate: "))
        );
    }
}

#[test]
fn repin_preserves_reviewed_identity_and_discovers_owner_stop_receipt_fields() {
    let rows = checked_in().unwrap();
    let old = rows
        .iter()
        .find(|row| row.path == REPIN_PATH && row.line == 525)
        .unwrap();
    assert_eq!(old.source_sha, PREVIOUS_SOURCE_SHA);
    assert_eq!(old.expression, "15");
    let relocation = include_str!("../data/python-repin-relocations.tsv");
    let records: Vec<_> = relocation.lines().skip(1).collect();
    assert_eq!(records.len(), 1);
    assert_eq!(
        records[0],
        format!(
            "{}\t{}\t525\t{}\t540",
            old.key, PREVIOUS_SOURCE_SHA, SOURCE_SHA
        )
    );
    let candidates = include_str!("../data/python-candidates.tsv");
    for (line, column, symbol, expression) in [
        (333, 56, "max_attempts", "_MAX_WORKER_REPLACEMENTS"),
        (334, 20, "recorded_at_utc_ms", "int(time.time() * 1000)"),
    ] {
        let key = format!("{REPIN_PATH}:{line}:{column}:dict-field:{symbol}");
        let candidate = candidates
            .lines()
            .find(|row| row.starts_with(&format!("{key}\t")))
            .unwrap();
        assert_eq!(candidate.split('\t').nth(6), Some(expression));
    }
}

#[test]
fn source_census_exactly_matches_l0_packaged_paths_and_hashes() {
    // Narrow reader for L0's checked-in pretty-printed array of plain paths.
    // This is not a general JSON parser; unexpected quoting/shape fails closed.
    let baseline = include_str!("../../obligations/data/baseline.json");
    assert!(baseline.contains(PREVIOUS_SOURCE_SHA));
    let repin = include_str!("../data/python-repin.json");
    assert!(repin.contains(SOURCE_SHA));
    assert!(repin.contains(REPIN_HASH));
    assert!(repin.contains(REPIN_OLD_HASH));
    let baseline_compact = baseline.split_whitespace().collect::<Vec<_>>().join(" ");
    let section = baseline
        .split_once("\"packaged_source_paths\": [")
        .unwrap()
        .1
        .split_once(']')
        .unwrap()
        .0;
    let expected: BTreeSet<_> = section
        .lines()
        .filter_map(|line| {
            let line = line.trim().trim_end_matches(',');
            if line.is_empty() {
                return None;
            }
            let path = line.strip_prefix('"').unwrap().strip_suffix('"').unwrap();
            assert!(!path.contains(['"', '\\']));
            Some(path)
        })
        .collect();
    assert_eq!(expected.len(), 725);
    let mut actual = BTreeSet::new();
    let mut expected_candidates = BTreeMap::new();
    let mut source_lines = BTreeMap::new();
    for line in include_str!("../data/python-scope.tsv").lines().skip(1) {
        let fields: Vec<_> = line.split('\t').collect();
        assert_eq!(fields.len(), 4);
        assert!(actual.insert(fields[0]), "duplicate path");
        let original_hash = if fields[0] == REPIN_PATH {
            assert_eq!(fields[1], REPIN_HASH);
            assert_eq!(fields[2], "553");
            // Preserve the historical twenty-site census and enumerate only the
            // newly discovered nested upper/lower operands and exact delivery-wake
            // assignment as additive sites; the historical twenty remain pinned.
            let supplemental = [
                "mycelium_a13_macos/managed_runtime.py:461:16:assignment:delay",
                "mycelium_a13_macos/managed_runtime.py:463:53:ceiling-operand:self._outbound_retry_backoff.maximum",
                "mycelium_a13_macos/managed_runtime.py:463:61:comparison-lower:outbound_retry_backoff.minimum",
            ];
            for key in supplemental {
                assert!(candidate_keys().unwrap().contains(key));
                assert!(
                    include_str!("../data/python-final-review-sites.tsv")
                        .lines()
                        .skip(1)
                        .any(|line| line.starts_with(&format!("{key}\t{CENSUS_SOURCE_SHA}\t")))
                );
            }
            assert_eq!(
                fields[3].parse::<usize>().unwrap()
                    - supplemental.len()
                    - r29_retry_admission::keys().len(),
                20
            );
            REPIN_OLD_HASH
        } else if let Some(delta) = include_str!("../data/python-current-source-delta.tsv")
            .lines()
            .skip(1)
            .map(|line| line.split('\t').collect::<Vec<_>>())
            .find(|delta| delta[0] == fields[0])
        {
            assert_eq!(delta.len(), 6);
            assert_eq!(delta[2], fields[1]);
            assert_eq!(delta[4], fields[2]);
            assert_eq!(delta[5], CENSUS_SOURCE_SHA);
            delta[1]
        } else {
            fields[1]
        };
        assert!(baseline_compact.contains(&format!(
            "\"path\": \"{}\", \"sha256\": \"{}\"",
            fields[0], original_hash
        )));
        expected_candidates.insert(fields[0], fields[3].parse::<usize>().unwrap());
        source_lines.insert(fields[0], fields[2].parse::<usize>().unwrap());
    }
    assert_eq!(actual, expected);
    let mut found = BTreeMap::<&str, usize>::new();
    for line in include_str!("../data/python-candidates.tsv")
        .lines()
        .skip(1)
    {
        let fields: Vec<_> = line.split('\t').collect();
        assert_eq!(fields.len(), 9);
        assert!(actual.contains(fields[1]));
        let line_number = fields[2].parse::<usize>().unwrap();
        assert!(line_number > 0 && line_number <= source_lines[fields[1]]);
        *found.entry(fields[1]).or_default() += 1;
    }
    for (path, expected) in expected_candidates {
        assert_eq!(
            found.get(path).copied().unwrap_or_default(),
            expected,
            "{path}"
        );
    }
}
