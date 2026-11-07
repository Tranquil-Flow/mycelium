// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:scripts/build_a3_capacity_evidence.py; O:path:scripts/external_reviewer_preflight.py.
//! L1b round 7, rules `r2-cli-byte-default-any-name` and `r2-cli-string-default`: a numeric byte CLI default whose
//! option name carries `bytes` but no max/limit/reserve token is a configured byte *quantity*, and an exact
//! decimal-string CLI/env default consumed by `float()` is a configured seconds amount. Both map to a new
//! registry id with an exact `path:line @ ef9ae61d` provenance and the literal's exact conversion.
#[path = "common/r32_verifier_admission.rs"]
#[allow(
    dead_code,
    reason = "exact four-discovery addition to the bounds-only gap"
)]
mod r32_verifier_admission;

#[path = "common/r29_retry_admission.rs"]
#[allow(
    dead_code,
    reason = "exact additive manual review outside bounds-only count"
)]
mod r29_retry_admission;
use budget_model::inventory::{
    Bound, Candidate, candidate_keys, candidates, checked_in, cli_literals, validate,
};
use budgets::{
    BudgetId,
    table::{Provenance, Value},
};
use std::collections::{BTreeMap, BTreeSet};

/// (key, rule, unit, value, registry id). Values are the independent arithmetic of the source literal.
const CASES: &[(&str, &str, &str, u64, &str)] = &[
    (
        "scripts/a13_operator/packaged_app_daemon.py:12:16:cli-default:A13_DAEMON_KEEPALIVE_SECONDS",
        "decimal-string",
        "milliseconds",
        15 * 1000,
        "PyCliLiteralScriptsA13OperatorPackagedAppDaemonA13DaemonKeepaliveSecondsL12C16",
    ),
    (
        "scripts/a13_operator/round14_rpc.py:663:40:cli-default:A13_HANDSHAKE_DEADLINE_S",
        "decimal-string",
        "milliseconds",
        180 * 1000,
        "PyCliLiteralScriptsA13OperatorRound14RpcA13HandshakeDeadlineSL663C40",
    ),
    (
        "scripts/a13_operator/round14_rpc.py:80:40:cli-default:A13_HANDSHAKE_DEADLINE_S",
        "decimal-string",
        "milliseconds",
        180 * 1000,
        "PyCliLiteralScriptsA13OperatorRound14RpcA13HandshakeDeadlineSL80C40",
    ),
    (
        "scripts/build_a3_capacity_evidence.py:381:55:cli-default:--workspace-bytes",
        "byte-quantity",
        "bytes",
        512 * 1024 * 1024,
        "PyCliLiteralScriptsBuildA3CapacityEvidenceWorkspaceBytesL381C55",
    ),
    (
        "scripts/build_m13_physical_control_plane.py:423:60:cli-default:--memory-ab-fast-bytes",
        "byte-quantity",
        "bytes",
        450_000_000,
        "PyCliLiteralScriptsBuildM13PhysicalControlPlaneMemoryAbFastBytesL423C60",
    ),
    (
        "scripts/build_m17_model_feasibility.py:91:55:cli-default:--workspace-bytes",
        "byte-quantity",
        "bytes",
        512 * 1024 * 1024,
        "PyCliLiteralScriptsBuildM17ModelFeasibilityWorkspaceBytesL91C55",
    ),
    (
        "scripts/external_reviewer_preflight.py:31:61:cli-default:--required-memory-bytes",
        "byte-quantity",
        "bytes",
        4 * 1024 * 1024 * 1024,
        "PyCliLiteralScriptsExternalReviewerPreflightRequiredMemoryBytesL31C61",
    ),
    (
        "scripts/external_reviewer_preflight.py:32:59:cli-default:--required-disk-bytes",
        "byte-quantity",
        "bytes",
        8 * 1024 * 1024 * 1024,
        "PyCliLiteralScriptsExternalReviewerPreflightRequiredDiskBytesL32C59",
    ),
    (
        "scripts/run_a5_benchmark_supervisor.py:380:54:cli-default:--maximum-lifetime-seconds",
        "decimal-string",
        "milliseconds",
        21_600 * 1000,
        "PyCliLiteralScriptsRunA5BenchmarkSupervisorMaximumLifetimeSecondsL380C54",
    ),
];

/// Same-shape neighbours that must stay pending.
const STAY_PENDING: &[&str] = &[
    // zero default: needs the zero-boundary rule
    "mycelium_seed/route_assignment.py:899:16:cli-default:--wait-applied",
];

fn world() -> (Vec<Bound>, BTreeMap<String, Candidate>) {
    let map = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    (checked_in().unwrap(), map)
}

fn row<'a>(rows: &'a [Bound], key: &str) -> &'a Bound {
    rows.iter()
        .find(|r| r.key == key)
        .unwrap_or_else(|| panic!("missing row {key}"))
}

fn ok(map: &BTreeMap<String, Candidate>, r: &Bound, table: &str) -> bool {
    cli_literals::validate_with(r, map, table)
}

#[test]
fn nine_cli_defaults_map_to_exact_registry_ids() {
    let (rows, map) = world();
    for (key, rule, unit, value, id) in CASES {
        let r = row(&rows, key);
        assert_eq!(r.budget, *id, "{key}");
        assert_eq!((r.unit.as_str(), r.value), (*unit, Some(*value)), "{key}");
        assert!(r.disposition.is_empty(), "{key}");
        assert!(
            r.review
                .starts_with(&format!("{}{rule};", cli_literals::PREFIX)),
            "{key}"
        );
        assert!(cli_literals::validate(r, &map), "{key}");
        let entry = BudgetId::from_name(id).unwrap().entry();
        match (entry.value, *unit) {
            (Value::Storage(n), "bytes") => assert_eq!(n.get(), *value, "{key}"),
            (Value::Duration(n), "milliseconds") => assert_eq!(n.millis(), *value, "{key}"),
            other => panic!("{key}: {other:?}"),
        }
        let c = &map[*key];
        assert!(
            matches!(entry.provenance, Provenance::Source(text) if text == format!("{}:{} @ ef9ae61d", c.path, c.line)),
            "{key}"
        );
    }
    assert!(validate(&rows).is_ok());
    let table_keys: BTreeSet<_> = cli_literals::TABLE
        .lines()
        .skip(1)
        .filter_map(|l| l.split('\t').next())
        .collect();
    assert_eq!(
        table_keys,
        CASES.iter().map(|c| c.0).collect::<BTreeSet<_>>()
    );
}

#[test]
fn value_unit_rule_id_and_table_mutations_are_refused() {
    let (rows, map) = world();
    for (key, rule, unit, value, id) in CASES {
        let base = row(&rows, key).clone();
        let other_rule = if *rule == "byte-quantity" {
            "decimal-string"
        } else {
            "byte-quantity"
        };
        let neighbour_id = CASES
            .iter()
            .find(|c| c.4 != *id && c.2 == *unit)
            .map(|c| c.4)
            .unwrap();
        let mutants: Vec<(&str, Bound)> = vec![
            (
                "value+1",
                Bound {
                    value: Some(value + 1),
                    ..base.clone()
                },
            ),
            (
                "unit",
                Bound {
                    unit: if *unit == "bytes" {
                        "milliseconds".into()
                    } else {
                        "bytes".into()
                    },
                    ..base.clone()
                },
            ),
            (
                "rule tag",
                Bound {
                    review: base.review.replacen(rule, other_rule, 1),
                    ..base.clone()
                },
            ),
            (
                "other id",
                Bound {
                    budget: neighbour_id.into(),
                    ..base.clone()
                },
            ),
            (
                "disposition",
                Bound {
                    disposition: "derived".into(),
                    ..base.clone()
                },
            ),
            (
                "expression",
                Bound {
                    expression: format!("{}0", base.expression),
                    ..base.clone()
                },
            ),
            (
                "scope",
                Bound {
                    scope: format!("{}x", base.scope),
                    ..base.clone()
                },
            ),
        ];
        for (what, m) in mutants {
            assert!(!ok(&map, &m, cli_literals::TABLE), "{key}: {what}");
            let mut all = rows.clone();
            let i = all.iter().position(|r| r.key == *key).unwrap();
            all[i] = m;
            assert!(validate(&all).is_err(), "{key}: {what} (whole validator)");
        }
        // Table edits: wrong value, wrong rule, wrong id, no consumer hash, extra duplicate line.
        let line = cli_literals::TABLE
            .lines()
            .find(|l| l.starts_with(*key))
            .unwrap();
        for (what, bad) in [
            (
                "table value",
                line.replacen(&format!("\t{value}\t"), &format!("\t{}\t", value + 1), 1),
            ),
            ("table rule", line.replacen(rule, other_rule, 1)),
            ("table id", line.replacen(id, neighbour_id, 1)),
            (
                "table hash",
                line.replacen("exact-span-sha256=", "span=", 1),
            ),
        ] {
            let text = cli_literals::TABLE.replacen(line, &bad, 1);
            assert!(!ok(&map, row(&rows, key), &text), "{key}: {what}");
        }
        let dup = format!("{}\n{line}\n", cli_literals::TABLE.trim_end());
        assert!(
            !ok(&map, row(&rows, key), &dup),
            "{key}: duplicate table line"
        );
    }
    assert!(!ok(&map, row(&rows, CASES[0].0), ""), "empty table");
}

#[test]
fn census_shape_mutations_are_refused() {
    let (rows, map) = world();
    let byte = CASES[3].0;
    let string = CASES[0].0;
    // A byte default is `type=int`-typed on its own line; the option name must carry `bytes`.
    for (what, f) in [
        (
            "kind",
            (|c: &mut Candidate| c.kind = "keyword".into()) as fn(&mut Candidate),
        ),
        ("option name", |c: &mut Candidate| {
            c.symbol = "--workspace-size".into()
        }),
        ("not an int option", |c: &mut Candidate| {
            c.source = c.source.replace("type=int", "type=str")
        }),
    ] {
        let mut m = map.clone();
        f(m.get_mut(byte).unwrap());
        assert!(
            !ok(&m, row(&rows, byte), cli_literals::TABLE),
            "byte {what}"
        );
    }
    // A decimal string needs an exact quoted decimal and a seconds-named slot.
    for (what, f) in [
        (
            "kind",
            (|c: &mut Candidate| c.kind = "keyword".into()) as fn(&mut Candidate),
        ),
        ("not a decimal", |c: &mut Candidate| {
            c.expression = "'15s'".into()
        }),
        ("not seconds", |c: &mut Candidate| {
            c.symbol = "A13_DAEMON_KEEPALIVE_BYTES".into()
        }),
    ] {
        let mut m = map.clone();
        f(m.get_mut(string).unwrap());
        assert!(
            !ok(&m, row(&rows, string), cli_literals::TABLE),
            "string {what}"
        );
    }
}

#[test]
fn same_shape_neighbours_stay_pending_and_a_forged_line_is_refused() {
    let (rows, map) = world();
    let reviewed: BTreeSet<_> = rows.iter().map(|r| r.key.as_str()).collect();
    for key in STAY_PENDING {
        assert!(!reviewed.contains(key), "{key}");
        let c = &map[*key];
        // Forge a byte-quantity line and row for the zero default: the option name has no `bytes`.
        let mut forged = row(&rows, CASES[3].0).clone();
        forged.key = (*key).into();
        forged.path = c.path.clone();
        forged.line = c.line;
        forged.column = c.column;
        forged.scope = c.scope.clone();
        forged.symbol = c.symbol.clone();
        forged.expression = c.expression.clone();
        let line = format!(
            "{key}\tbyte-quantity\tbytes\t{}\t{}\t{}:1 @ f4e366e98a1f705dfaf182897044500d290dbd7f; exact-span-sha256=00",
            forged.value.unwrap(),
            forged.budget,
            c.path
        );
        let text = format!("{}\n{line}\n", cli_literals::TABLE.trim_end());
        assert!(!ok(&map, &forged, &text), "{key}");
        assert!(
            !ok(&map, &forged, cli_literals::TABLE),
            "{key} (not in the pinned table)"
        );
    }
}

#[test]
fn total_pending_shrinks_by_exactly_nine() {
    let reviewed: BTreeSet<_> = checked_in().unwrap().into_iter().map(|r| r.key).collect();
    let pending = candidate_keys().unwrap().difference(&reviewed).count();
    assert_eq!(
        pending,
        1299 + r29_retry_admission::keys().len() + r32_verifier_admission::keys().len()
    ); // exact new manual review is outside the bounds-only view // R17 +12 tuple-floor operands; manual reviews separate. // R16 +8 census operands; manual reviews counted separately. // +570 RU-051 keyword operands. // 842 - 9 - 83 x-exclusions - 4 round-10 field-origin reads - 2 round-11 self-attribute reads (inventory_field_origins) - 2 round-11 OwnerTokenPath constructor forwards - 1 EntryAdmissionPolicy constructor forward - 1 round-11 self-attribute read (inventory_forward_callers, inventory_field_origins) - 6 round-11 slice 5 lease-deadline rows - 1 slice 6 float() forward - 2 slice 6 self-attribute reads - 11 round-12 validated-document-field rows (inventory_validated_document_field) - 4 round-12 RU-033 propagated source-unbounded rows (inventory_unbounded_propagation) + 8 RU-044 reverted rows + 3 RU-044 discovery rows (round 13, inventory_ru044_revert) + 6 round-14 census rows (inventory_discovery_extension_r4)
}

/// A consistent mutation of one pinned site: the census candidate and the reviewed row change together (and
/// optionally the table text), so only the rule's own semantic checks can refuse it.
fn consistent(
    key: &str,
    candidate: impl Fn(&mut Candidate),
    row_edit: impl Fn(&mut Bound, &Candidate),
    table_edit: impl Fn(&str) -> String,
) -> bool {
    let (rows, mut map) = world();
    candidate(map.get_mut(key).unwrap());
    let mut r = row(&rows, key).clone();
    row_edit(&mut r, &map[key]);
    ok(&map, &r, &table_edit(cli_literals::TABLE))
}

#[test]
fn each_semantic_check_refuses_a_consistently_mutated_site() {
    let byte = CASES[3].0;
    let string = CASES[0].0;
    let sync = |r: &mut Bound, c: &Candidate| {
        r.symbol = c.symbol.clone();
        r.expression = c.expression.clone();
        r.scope = c.scope.clone();
    };
    let keep = |t: &str| t.to_string();
    // Baselines are valid, so each refusal below is caused by exactly the edit.
    assert!(consistent(byte, |_| {}, |_, _| {}, keep));
    assert!(consistent(string, |_| {}, |_, _| {}, keep));
    // A byte option without `bytes` in its name (or a per-rate name) is not this rule.
    assert!(!consistent(
        byte,
        |c| c.symbol = "--workspace-size".into(),
        sync,
        keep
    ));
    assert!(!consistent(
        byte,
        |c| c.symbol = "--workspace-bytes-per-second".into(),
        sync,
        keep
    ));
    // A seconds string must be a seconds-named slot and its consumer a float() cast.
    assert!(!consistent(
        string,
        |c| c.symbol = "A13_DAEMON_KEEPALIVE_BYTES".into(),
        sync,
        keep
    ));
    assert!(!consistent(
        string,
        |c| c.symbol = "A13_DAEMON_KEEPALIVE".into(),
        sync,
        keep
    ));
    assert!(!consistent(
        string,
        |_| {},
        |_, _| {},
        |t| t.replacen("float(", "int(", 1)
    ));
    // A non-decimal string, a fractional or empty one, and a non-literal expression are refused.
    for bad in ["'1.5'", "''", "'0x10'", "'-3'", "'+15'", "15"] {
        assert!(
            !consistent(string, |c| c.expression = bad.into(), sync, keep),
            "{bad}"
        );
    }
    for bad in ["0", "1 << 20", "4 * x", "2 ** 3 ** 2 ** 99999"] {
        assert!(
            !consistent(byte, |c| c.expression = bad.into(), sync, keep),
            "{bad}"
        );
    }
    // The registry id must be this site's own: same value, other site's provenance is refused.
    let (other_key, other_id) = (CASES[5].0, CASES[5].4);
    assert_eq!(CASES[3].3, CASES[5].3);
    let swap = |t: &str| t.replacen(CASES[3].4, other_id, 1);
    assert!(!consistent(
        byte,
        |_| {},
        |r, _| r.budget = other_id.into(),
        swap
    ));
    let _ = other_key;
}
