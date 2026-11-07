// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/app.py; O:path:scripts/a8_run_physical_gate.py; O:path:physical_inference_node.py.
//! L1b round 7, rule `r2-param-forward-flow-edge` / `r2-instant-carrier-edge` (callers engine):
//! a REQUIRED parameter that a callee forwards unchanged derives from every production call site, and the
//! call sites are pinned in `python-forward-callers.tsv` (AST call graph of the oracle at f4e366e9: unique
//! callee name, no subclass, no non-call reference except annotations, every call passes the parameter by
//! keyword, parameter never rebound). Each target is a reviewed census keyword row; the row's relation is the
//! ordered `+` join of the targets' ids; an absolute instant stays a typed instant.
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only oracle helper")]
mod oracle_blobs;
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
use budget_model::inventory::{Bound, Candidate, callers, candidates, checked_in, validate};
use std::collections::{BTreeMap, BTreeSet};

/// (source key, rule, unit, [(target key, target registry id)])
type Case = (
    &'static str,
    &'static str,
    &'static str,
    &'static [(&'static str, &'static str)],
);
const CASES: &[Case] = &[
    (
        "mycelium_a13_macos/app.py:191:52:keyword:max_bytes",
        "required-parameter",
        "bytes",
        &[
            (
                "mycelium_a13_macos/app.py:284:37:keyword:max_bytes",
                "PyFinalMyceliumA13MacosAppMaxBytesL284C37",
            ),
            (
                "mycelium_a13_macos/app.py:306:64:keyword:max_bytes",
                "PyFinalMyceliumA13MacosAppMaxBytesL306C64",
            ),
        ],
    ),
    (
        "mycelium_a13_macos/authority.py:209:44:keyword:max_bytes",
        "required-parameter",
        "bytes",
        &[(
            "mycelium_a13_macos/authority.py:231:46:keyword:max_bytes",
            "PyMyceliumA13MacosAuthorityMaxPrivateJsonBytesL96",
        )],
    ),
    (
        "mycelium_a13_macos/package_manifest.py:78:71:keyword:expires_at",
        "instant-carrier",
        "milliseconds",
        &[(
            "scripts/finalize_a13_macos.py:192:41:keyword:expires_at",
            "PyExactScriptsFinalizeA13MacosL192C41",
        )],
    ),
    (
        "mycelium_a13_macos/token_owner.py:32:8:assignment:self.deadline",
        "instant-carrier",
        "milliseconds",
        &[(
            "mycelium_a13_macos/workflow.py:496:48:keyword:process_deadline_monotonic_s",
            "PyClockMyceliumA13MacosWorkflowL493C8",
        )],
    ),
    (
        "mycelium_a13_macos/worker.py:540:12:keyword:response_timeout_seconds",
        "required-parameter",
        "milliseconds",
        &[(
            "mycelium_a13_macos/worker.py:1029:16:keyword:timeout_seconds",
            "PyNativeApprovedWallTimeoutCeiling",
        )],
    ),
    (
        "mycelium_a13_windows/package_manifest.py:101:71:keyword:expires_at",
        "instant-carrier",
        "milliseconds",
        &[(
            "scripts/build_a13_windows.py:200:8:keyword:expires_at",
            "PyExactScriptsBuildA13WindowsL200C8",
        )],
    ),
    (
        "physical_inference_node.py:2195:8:assignment:self.command_timeout",
        "required-parameter",
        "milliseconds",
        &[(
            "physical_inference_node.py:6691:12:keyword:command_timeout",
            "PyPhysicalInferenceNodeParseArgsCommandTimeoutL6667",
        )],
    ),
    (
        "scripts/a8_run_physical_gate.py:700:12:keyword:maximum_size",
        "required-parameter",
        "bytes",
        &[
            (
                "scripts/a8_run_physical_gate.py:1244:24:keyword:maximum_size",
                "PyExactScriptsA8RunPhysicalGateL1244C24",
            ),
            (
                "scripts/a8_run_physical_gate.py:765:33:keyword:maximum_size",
                "PyExactScriptsA8RunPhysicalGateL765C33",
            ),
            (
                "scripts/a8_run_physical_gate.py:769:22:keyword:maximum_size",
                "PyExactScriptsA8RunPhysicalGateL769C22",
            ),
            (
                "scripts/a8_run_physical_gate.py:782:12:keyword:maximum_size",
                "PyExactScriptsA8RunPhysicalGateL782C12",
            ),
            (
                "scripts/a8_run_physical_gate.py:790:12:keyword:maximum_size",
                "PyExactScriptsA8RunPhysicalGateL790C12",
            ),
        ],
    ),
    // Round 8 (parent decision a): two differently-budgeted callers share this sink; the `+` relation of
    // the callers' own values (literal 120 s preflight, `self.timeout_s` init timeout) answers it.
    (
        "scripts/run_a10_physical_gate.py:1401:12:keyword:timeout",
        "required-parameter",
        "milliseconds",
        &[
            (
                "scripts/run_a10_physical_gate.py:1413:46:keyword:timeout",
                "PyScriptsRunA10PhysicalGateBrowsersessionsphasePreflightTimeoutL1413",
            ),
            (
                "scripts/run_a10_physical_gate.py:1432:34:keyword:timeout",
                "PyScriptsRunA10PhysicalGateBrowsersessionsphaseInitTimeoutSL1359",
            ),
        ],
    ),
    // Round 11 (class/field slice 3): the constructor callee `OwnerTokenPath.__init__` has ONE production call
    // site (token_owner.py:182, keyword-only), whose `process_deadline_monotonic_s=self.deadline` row was closed
    // by the round-10 `self-attribute` rule; the parameter is required (keyword-only, no default).
    (
        "mycelium_live/token_path_owner.py:43:8:assignment:self._process_deadline",
        "instant-carrier",
        "milliseconds",
        &[(
            "mycelium_a13_macos/token_owner.py:185:41:keyword:process_deadline_monotonic_s",
            "PyClockMyceliumA13MacosWorkflowL493C8",
        )],
    ),
    (
        "mycelium_live/token_path_owner.py:48:12:keyword:process_deadline_monotonic_s",
        "instant-carrier",
        "milliseconds",
        &[(
            "mycelium_a13_macos/token_owner.py:185:41:keyword:process_deadline_monotonic_s",
            "PyClockMyceliumA13MacosWorkflowL493C8",
        )],
    ),
    // Round 11 slice 4: `EntryAdmissionPolicy.__init__` has ONE production call site (token_path_owner.py:46), whose
    // keyword row :48 was closed by slice 3 (the chain OwnerTokenPath -> EntryAdmissionPolicy).
    (
        "mycelium_node/token_admission.py:56:8:assignment:self._process_deadline",
        "instant-carrier",
        "milliseconds",
        &[(
            "mycelium_live/token_path_owner.py:48:12:keyword:process_deadline_monotonic_s",
            "PyClockMyceliumA13MacosWorkflowL493C8",
        )],
    ),
    // Round 11 slice 5: the lease deadline travels the same constructor chain; its one live call site
    // token_owner.py:186 reads `self._lease_deadline` (closed by `self-attribute` over the two clamps).
    (
        "mycelium_live/token_path_owner.py:44:8:assignment:self._lease_deadline",
        "instant-carrier",
        "milliseconds",
        &[(
            "mycelium_a13_macos/token_owner.py:186:20:keyword:lease_deadline_monotonic_s",
            "PyClockMyceliumA13MacosWorkflowL493C8+PyClockMyceliumA13MacosWorkflowL493C8",
        )],
    ),
    (
        "mycelium_live/token_path_owner.py:49:12:keyword:lease_deadline_monotonic_s",
        "instant-carrier",
        "milliseconds",
        &[(
            "mycelium_a13_macos/token_owner.py:186:20:keyword:lease_deadline_monotonic_s",
            "PyClockMyceliumA13MacosWorkflowL493C8+PyClockMyceliumA13MacosWorkflowL493C8",
        )],
    ),
    (
        "mycelium_node/token_admission.py:57:8:assignment:self._lease_deadline",
        "instant-carrier",
        "milliseconds",
        &[(
            "mycelium_live/token_path_owner.py:49:12:keyword:lease_deadline_monotonic_s",
            "PyClockMyceliumA13MacosWorkflowL493C8+PyClockMyceliumA13MacosWorkflowL493C8",
        )],
    ),
    // Round 11 slice 6: `_PrefillWaitBudget.__init__` stores `float(command_timeout)`; its one call site
    // (physical_inference_node.py:4885) passes `self.command_timeout`, the row closed by the round-10 self-attribute rule.
    (
        "physical_inference_node.py:549:8:assignment:self.command_timeout",
        "required-parameter",
        "milliseconds",
        &[(
            "physical_inference_node.py:4895:12:keyword:command_timeout",
            "PyPhysicalInferenceNodeParseArgsCommandTimeoutL6667",
        )],
    ),
];

/// Neighbours that look alike and must stay pending (reason in the comment).
const STAY_PENDING: &[&str] = &[
    // defaulted parameter: the default is a second root
    "mycelium_iroh_sidecar/client.py:1007:20:keyword:socket_timeout",
    // callee name defined more than once (ambiguous caller census)
    "mycelium_a13_macos/controller_staging.py:82:65:keyword:timeout_seconds",
    // a call site passes the parameter positionally (no census row for that argument)
    "mycelium_a13_macos/app.py:816:62:dict-field:ttl_seconds",
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

fn ok_with(rows: &[Bound], map: &BTreeMap<String, Candidate>, key: &str, edges: &str) -> bool {
    callers::validate_with(row(rows, key), rows, map, edges)
}

/// The checked-in table with the lines selected by `keep` and optional extras.
fn table(keep: impl Fn(&str) -> bool, extra: &[String]) -> String {
    let mut out: Vec<String> = callers::EDGES
        .lines()
        .filter(|l| l.starts_with("key\t") || keep(l))
        .map(String::from)
        .collect();
    out.extend(extra.iter().cloned());
    out.join("\n") + "\n"
}

#[test]
fn sixteen_forwarded_parameters_derive_from_every_call_site() {
    let (rows, map) = world();
    for (key, rule, unit, targets) in CASES {
        let r = row(&rows, key);
        assert_eq!(r.disposition, "derived", "{key}");
        assert!(r.budget.is_empty() && r.value.is_none(), "{key}");
        assert_eq!(r.unit, *unit, "{key}");
        assert!(
            r.review.starts_with(&format!("{}{rule};", callers::PREFIX)),
            "{key}"
        );
        let ids: Vec<_> = targets.iter().map(|t| t.1).collect();
        assert_eq!(r.relation, ids.join("+"), "{key}");
        assert!(callers::validate(r, &rows, &map), "{key}");
        let edges = callers::edges_of(callers::EDGES, key).unwrap();
        let keys: Vec<_> = edges.iter().map(|e| e.target_key).collect();
        assert_eq!(
            keys,
            targets.iter().map(|t| t.0).collect::<Vec<_>>(),
            "{key}"
        );
        for (e, (tk, id)) in edges.iter().zip(*targets) {
            assert_eq!(e.rule, *rule);
            assert_eq!(e.group_size, targets.len());
            assert_eq!(e.callee, format!("{}::{}", map[*key].path, map[*key].scope));
            assert!(e.reference.contains("exact-span-sha256="), "{tk}");
            assert!(e.reference.contains(&format!("{}:", map[*tk].path)), "{tk}");
            // The target is a reviewed row of the same unit whose id is the independently listed one.
            let t = row(&rows, tk);
            assert_eq!(
                if t.budget.is_empty() {
                    &t.relation
                } else {
                    &t.budget
                },
                id,
                "{tk}"
            );
            assert_eq!(t.unit, *unit, "{tk}");
        }
    }
    assert!(validate(&rows).is_ok());
    let sources = callers::source_keys(callers::EDGES);
    assert_eq!(sources, CASES.iter().map(|c| c.0).collect::<BTreeSet<_>>());
}

#[test]
fn a_missing_or_shrunk_call_site_set_is_refused() {
    let (rows, map) = world();
    for (key, _, _, targets) in CASES {
        assert!(ok_with(&rows, &map, key, callers::EDGES), "{key} baseline");
        for (tk, _) in *targets {
            let text = table(
                |l| !(l.starts_with(*key) && l.contains(&format!("\t{tk}\t"))),
                &[],
            );
            // Dropping any caller: a group_size that still claims n, or no edge at all, is refused.
            assert!(!ok_with(&rows, &map, key, &text), "{key} without {tk}");
        }
        // Dropping a caller AND lowering the declared size is a different lie; the size must equal the edge count
        // and the remaining set must still equal the pinned set, which the stricter check below pins by count.
        if targets.len() > 1 {
            let text = table(
                |l| !(l.starts_with(*key) && l.contains(&format!("\t{}\t", targets[0].0))),
                &[],
            );
            let n = targets.len();
            let text = text
                .lines()
                .map(|l| {
                    if l.starts_with(*key) {
                        l.replace(&format!("\t{n}\t"), &format!("\t{}\t", n - 1))
                    } else {
                        l.to_string()
                    }
                })
                .collect::<Vec<_>>()
                .join("\n")
                + "\n";
            assert!(
                !ok_with(&rows, &map, key, &text),
                "{key}: a shrunk group must not hide a pinned call site"
            );
        }
    }
}

#[test]
fn an_extra_target_or_a_pending_target_is_refused() {
    let (rows, map) = world();
    let key = CASES[0].0; // app.py:191, two callers
    let extra = format!(
        "{key}\tmycelium_a13_macos/app.py:816:62:dict-field:ttl_seconds\trequired-parameter\t3\tmycelium_a13_macos/app.py::_win32_private_file\tmycelium_a13_macos/app.py:816-816 @ f4e366e98a1f705dfaf182897044500d290dbd7f; exact-span-sha256=00"
    );
    let text = table(|_| true, &[extra]);
    assert!(
        !ok_with(&rows, &map, key, &text),
        "wrong symbol/kind target"
    );
    // A pending target (its row removed from the reviewed set) is refused.
    let pending_target = CASES[0].3[0].0;
    let reduced: Vec<Bound> = rows
        .iter()
        .filter(|r| r.key != pending_target)
        .cloned()
        .collect();
    assert!(!callers::validate_with(
        row(&reduced, key),
        &reduced,
        &map,
        callers::EDGES
    ));
    // A target that names another parameter is refused.
    let swapped = table(|_| true, &[]).replace(
        "app.py:284:37:keyword:max_bytes\trequired-parameter",
        "app.py:283:37:keyword:max_bytes\trequired-parameter",
    );
    assert!(
        !ok_with(&rows, &map, key, &swapped),
        "target that is not a census row"
    );
}

#[test]
fn relation_unit_value_budget_and_rule_mutations_are_refused() {
    let (rows, map) = world();
    for (key, rule, unit, targets) in CASES {
        let base = row(&rows, key).clone();
        let check = |mutated: Bound, what: &str| {
            let mut all = rows.clone();
            let i = all.iter().position(|r| r.key == *key).unwrap();
            all[i] = mutated.clone();
            assert!(
                !callers::validate_with(&mutated, &all, &map, callers::EDGES),
                "{key}: {what}"
            );
            assert!(validate(&all).is_err(), "{key}: {what} (whole validator)");
        };
        let mut m = base.clone();
        m.relation = targets
            .iter()
            .rev()
            .map(|t| t.1)
            .collect::<Vec<_>>()
            .join("+");
        if targets.len() > 1 {
            check(m, "relation order");
        }
        let mut m = base.clone();
        m.relation = targets[0].1.to_string();
        if targets.len() > 1 {
            check(m, "relation drops a target");
        }
        let mut m = base.clone();
        m.unit = if *unit == "bytes" {
            "milliseconds".into()
        } else {
            "bytes".into()
        };
        check(m, "unit");
        let mut m = base.clone();
        m.value = Some(1);
        check(m, "value on a derived row");
        let mut m = base.clone();
        m.budget = targets[0].1.to_string();
        m.disposition = String::new();
        check(m, "budget instead of derived");
        let mut m = base.clone();
        let other = if *rule == "instant-carrier" {
            "required-parameter"
        } else {
            "instant-carrier"
        };
        m.review = m.review.replacen(rule, other, 1);
        check(m, "rule tag");
        let mut m = base.clone();
        m.expression = format!("{}_", m.expression);
        check(m, "expression");
        let mut m = base.clone();
        m.scope = format!("{}x", m.scope);
        check(m, "scope");
    }
}

#[test]
fn an_instant_carrier_needs_a_millisecond_row_and_a_typed_producer() {
    let (rows, map) = world();
    // The rule tag is part of the pinned table: switching the table rule alone is refused too.
    for (key, rule, _, _) in CASES {
        let other = if *rule == "instant-carrier" {
            "required-parameter"
        } else {
            "instant-carrier"
        };
        let text = table(|_| true, &[])
            .lines()
            .map(|l| {
                if l.starts_with(*key) {
                    l.replace(&format!("\t{rule}\t"), &format!("\t{other}\t"))
                } else {
                    l.to_string()
                }
            })
            .collect::<Vec<_>>()
            .join("\n")
            + "\n";
        assert!(!ok_with(&rows, &map, key, &text), "{key}");
    }
    // instant-carrier on a byte row is refused outright.
    let key = CASES[0].0;
    let text = table(|_| true, &[]).replace("required-parameter", "instant-carrier");
    assert!(!ok_with(&rows, &map, key, &text));
}

#[test]
fn neighbours_stay_pending_and_a_forged_review_is_refused() {
    let (rows, map) = world();
    let reviewed: BTreeSet<_> = rows.iter().map(|r| r.key.as_str()).collect();
    for key in STAY_PENDING {
        assert!(!reviewed.contains(key), "{key} must stay pending");
        assert!(map.contains_key(*key), "{key} census");
        assert!(
            callers::edges_of(callers::EDGES, key).unwrap().is_empty(),
            "{key} has no pinned call sites"
        );
    }
    // Round 12 (RU-033): the row whose callers are source-unbounded (Blocked) rows is no longer pending; it is closed
    // by the separate propagation rule (inventory_unbounded_propagation), and THIS engine still refuses it: a
    // derived row may not point at a Blocked id.
    const BLOCKED_CALLERS: &str =
        "mycelium_a7_standby_qualification.py:340:6:dict-field:reserved_kv_bytes";
    assert_eq!(row(&rows, BLOCKED_CALLERS).disposition, "source-unbounded");
    assert!(
        callers::edges_of(callers::EDGES, BLOCKED_CALLERS)
            .unwrap()
            .is_empty()
    );
    let mut derived = row(&rows, BLOCKED_CALLERS).clone();
    derived.disposition = "derived".into();
    derived.review = "forward-callers:required-parameter; forged".into();
    assert!(!callers::validate(&derived, &rows, &map));
    // Forge: reuse another row's edges and review for a neighbour: refused (no edge for the key; defaulted parameter).
    let neighbour = STAY_PENDING[0];
    let c = &map[neighbour];
    let mut forged = row(&rows, CASES[1].0).clone();
    forged.key = neighbour.to_string();
    forged.path = c.path.clone();
    forged.line = c.line;
    forged.column = c.column;
    forged.scope = c.scope.clone();
    forged.symbol = c.symbol.clone();
    forged.expression = c.expression.clone();
    assert!(!callers::validate_with(
        &forged,
        &rows,
        &map,
        callers::EDGES
    ));
    // Even with the table lines copied over the neighbour, a parameter that has its own census row (default) is refused.
    let (src, tgt) = (CASES[1].0, CASES[1].3[0].0);
    let line = format!(
        "{neighbour}\t{tgt}\trequired-parameter\t1\t{}::{}\tmycelium_a13_macos/authority.py:231-231 @ f4e366e98a1f705dfaf182897044500d290dbd7f; exact-span-sha256=00",
        c.path, c.scope
    );
    let _ = src;
    let text = table(|_| true, &[line]);
    assert!(!callers::validate_with(&forged, &rows, &map, &text));
    // A malformed or re-headed table is refused.
    assert!(!ok_with(
        &rows,
        &map,
        CASES[1].0,
        &callers::EDGES.replacen("key\t", "k\t", 1)
    ));
    assert!(!ok_with(&rows, &map, CASES[1].0, ""));
}

#[test]
fn single_target_binding_validator_never_admits_these_rows() {
    let (rows, map) = world();
    for (key, ..) in CASES {
        assert!(
            !budget_model::inventory::bindings::validate(row(&rows, key), &rows, &map),
            "{key}"
        );
    }
}

#[test]
fn total_pending_after_the_forward_callers_batch() {
    let reviewed: BTreeSet<_> = checked_in().unwrap().into_iter().map(|r| r.key).collect();
    let pending = budget_model::inventory::candidate_keys()
        .unwrap()
        .difference(&reviewed)
        .count();
    assert_eq!(
        pending,
        1299 + r29_retry_admission::keys().len() + r32_verifier_admission::keys().len()
    ); // exact new manual review is outside the bounds-only view // R17 +12 tuple-floor operands; manual reviews separate. // R16 +8 census operands; manual reviews counted separately. // +570 RU-051 keyword operands. // 850 - 9 forward-callers (8 + a10:1401) - 9 cli literals - 83 x-exclusions - 2 input guards - 7 symbolic uppers - 4 round-10 field-origin reads - 2 round-11 self-attribute reads (inventory_field_origins) - 2 round-11 OwnerTokenPath constructor forwards - 1 round-11 EntryAdmissionPolicy constructor forward + 1 self-attribute read (inventory_field_origins) - 6 slice 5 (2 self-clamps, 1 lease self-attribute read, 3 lease constructor forwards) - 1 slice 6 float() forward - 2 slice 6 self-attribute reads - 11 round-12 validated-document-field rows (inventory_validated_document_field) - 4 round-12 RU-033 propagated source-unbounded rows (inventory_unbounded_propagation) + 8 RU-044 reverted rows + 3 RU-044 discovery rows (round 13, inventory_ru044_revert) + 6 round-14 census rows (inventory_discovery_extension_r4)
}

#[test]
fn census_table_and_second_root_mutations_are_each_refused() {
    let (rows, map) = world();
    let two = CASES[0]; // app.py:191, two callers (bytes)
    let ms = CASES[4]; // worker.py:540, one caller (milliseconds)
    // Target census row of another kind or symbol.
    for (what, f) in [
        (
            "kind",
            (|c: &mut Candidate| c.kind = "assignment".into()) as fn(&mut Candidate),
        ),
        ("symbol", |c: &mut Candidate| c.symbol = "other_name".into()),
    ] {
        let mut m = map.clone();
        f(m.get_mut(two.3[0].0).unwrap());
        assert!(
            !callers::validate_with(row(&rows, two.0), &rows, &m, callers::EDGES),
            "target {what}"
        );
    }
    // Source census row of the wrong kind, or whose expression is not a bare identifier.
    for (what, f) in [
        (
            "kind",
            (|c: &mut Candidate| c.kind = "comparison".into()) as fn(&mut Candidate),
        ),
        ("expression", |c: &mut Candidate| {
            c.expression = "max_bytes + 1".into()
        }),
    ] {
        let mut m = map.clone();
        f(m.get_mut(two.0).unwrap());
        assert!(
            !callers::validate_with(row(&rows, two.0), &rows, &m, callers::EDGES),
            "source {what}"
        );
    }
    // Callee other than the enclosing function; declared size that lies in either direction; descending order.
    let n = two.3.len();
    let callee = format!("{}::{}", map[two.0].path, map[two.0].scope);
    let other = table(|_| true, &[]).replace(&callee, "mycelium_a13_macos/app.py::elsewhere");
    assert!(!ok_with(&rows, &map, two.0, &other), "callee");
    for lie in [n + 1, n - 1] {
        let text = table(|_| true, &[])
            .lines()
            .map(|l| {
                if l.starts_with(two.0) {
                    l.replace(&format!("\t{n}\t"), &format!("\t{lie}\t"))
                } else {
                    l.to_string()
                }
            })
            .collect::<Vec<_>>()
            .join("\n")
            + "\n";
        assert!(!ok_with(&rows, &map, two.0, &text), "declared size {lie}");
    }
    let mut own: Vec<String> = callers::EDGES
        .lines()
        .filter(|l| l.starts_with(two.0))
        .map(String::from)
        .collect();
    own.reverse();
    let reversed = table(|l| !l.starts_with(two.0), &own);
    let mut r = row(&rows, two.0).clone();
    r.relation = two
        .3
        .iter()
        .rev()
        .map(|t| t.1)
        .collect::<Vec<_>>()
        .join("+");
    let mut all = rows.clone();
    let i = all.iter().position(|x| x.key == two.0).unwrap();
    all[i] = r.clone();
    assert!(
        !callers::validate_with(&r, &all, &map, &reversed),
        "descending targets with a matching relation"
    );
    // A target row whose recorded unit disagrees with the source row.
    let mut all = rows.clone();
    let i = all.iter().position(|x| x.key == two.3[0].0).unwrap();
    all[i].unit = "entries".into();
    assert!(
        !callers::validate_with(row(&all, two.0), &all, &map, callers::EDGES),
        "target unit"
    );
    // The instant-carrier rule on a byte row is refused by the millisecond requirement alone.
    let text = table(|_| true, &[])
        .lines()
        .map(|l| {
            if l.starts_with(two.0) {
                l.replace("\trequired-parameter\t", "\tinstant-carrier\t")
            } else {
                l.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
        + "\n";
    let mut r = row(&rows, two.0).clone();
    r.review = r
        .review
        .replacen("required-parameter", "instant-carrier", 1);
    let mut all = rows.clone();
    let i = all.iter().position(|x| x.key == two.0).unwrap();
    all[i] = r.clone();
    assert!(
        !callers::validate_with(&r, &all, &map, &text),
        "instant carrier on bytes"
    );
    // A defaulted parameter (its own census row) is a second root even when a keyword target would fit:
    // worker.py:627 forwards `timeout_seconds` (default None, parameter row at :620) and the reviewed
    // call site worker.py:1029 passes a `timeout_seconds` keyword of the same unit.
    let key = "mycelium_a13_macos/worker.py:627:12:keyword:timeout_seconds";
    let c = &map[key];
    assert!(map.values().any(|p| p.kind == "parameter"
        && p.path == c.path
        && p.scope == c.scope
        && p.symbol == "timeout_seconds"));
    let mut forged = row(&rows, ms.0).clone();
    forged.key = key.into();
    forged.path = c.path.clone();
    forged.line = c.line;
    forged.column = c.column;
    forged.scope = c.scope.clone();
    forged.symbol = c.symbol.clone();
    forged.expression = c.expression.clone();
    let line = format!(
        "{key}\t{}\trequired-parameter\t1\t{}::{}\tmycelium_a13_macos/worker.py:1029-1029 @ f4e366e98a1f705dfaf182897044500d290dbd7f; exact-span-sha256=00",
        ms.3[0].0, c.path, c.scope
    );
    let text = table(|_| true, &[line]);
    // Sanity: with the parameter row hidden from the census the very same forged row validates, so the
    // second-root check is the only thing refusing it.
    let mut hidden = map.clone();
    hidden.retain(|_, p| {
        !(p.kind == "parameter"
            && p.path == c.path
            && p.scope == c.scope
            && p.symbol == "timeout_seconds")
    });
    let mut with_forged = rows.clone();
    with_forged.push(forged.clone());
    assert!(
        callers::validate_with(&forged, &with_forged, &hidden, &text),
        "control: forged row is otherwise valid"
    );
    assert!(
        !callers::validate_with(&forged, &with_forged, &map, &text),
        "defaulted parameter is a second root"
    );
}

#[test]
fn a_forwarded_value_must_be_the_callers_own_no_arithmetic() {
    let (rows, map) = world();
    let (key, _, _, targets) = CASES[8]; // run_a10:1401, divergent callers
    assert!(key.contains("run_a10_physical_gate.py:1401"));
    assert!(ok_with(&rows, &map, key, callers::EDGES), "baseline");
    // The callers' own values are accepted: a literal, constant arithmetic of literals, a dotted name.
    for (target, expr) in [
        (targets[0].0, "120"),
        (targets[0].0, "2 * 60"),
        (targets[0].0, "1_000"),
        (targets[0].0, "5 - 1"),
        (targets[0].0, "10 / 2"),
        (targets[0].0, "30 + 30"),
        (targets[1].0, "self.timeout_s"),
        (targets[1].0, "timeout_s"),
    ] {
        let mut m = map.clone();
        m.get_mut(target).unwrap().expression = expr.into();
        assert!(ok_with(&rows, &m, key, callers::EDGES), "{target}: {expr}");
    }
    // Arithmetic over a caller's variable, a call or a conditional modifies the value: refused, whichever
    // caller carries it.
    for target in [targets[0].0, targets[1].0] {
        for expr in [
            "self.timeout_s - 1",
            "self.timeout_s + 30",
            "min(120, self.timeout_s)",
            "self.timeout_s * 2",
            "120 if fast else self.timeout_s",
            "self.timeout()",
            "-self.timeout_s",
            "",
            "120 +",
            "12abc",
            "2x",
            "1e3",
            "120 if",
        ] {
            let mut m = map.clone();
            m.get_mut(target).unwrap().expression = expr.into();
            assert!(!ok_with(&rows, &m, key, callers::EDGES), "{target}: {expr}");
        }
    }
}

#[test]
fn the_divergent_callers_row_is_the_ordered_plus_of_both_registered_ids() {
    let (rows, map) = world();
    let (key, _, _, targets) = CASES[8];
    let base = row(&rows, key).clone();
    assert_eq!(
        base.relation,
        "PyScriptsRunA10PhysicalGateBrowsersessionsphasePreflightTimeoutL1413+PyScriptsRunA10PhysicalGateBrowsersessionsphaseInitTimeoutSL1359"
    );
    // Both registered ids exist as numeric millisecond entries and are different quantities (120 s vs a
    // caller-supplied timeout): the relation is a join, not an equality.
    let ids: Vec<_> = targets.iter().map(|t| t.1).collect();
    assert_ne!(ids[0], ids[1]);
    // A single-caller relation, a repeated id or an unregistered id never passes.
    for relation in [
        ids[0].to_string(),
        ids[1].to_string(),
        format!("{}+{}", ids[0], ids[0]),
        format!("{}+PyNoSuchBudget", ids[0]),
        format!("{}+{}+{}", ids[0], ids[1], ids[1]),
    ] {
        let mut m = base.clone();
        m.relation = relation.clone();
        let mut all = rows.clone();
        let i = all.iter().position(|r| r.key == key).unwrap();
        all[i] = m.clone();
        assert!(
            !callers::validate_with(&m, &all, &map, callers::EDGES),
            "{relation}"
        );
        assert!(validate(&all).is_err(), "{relation} (whole validator)");
    }
    // The single-target binding validator never admits it.
    assert!(!budget_model::inventory::bindings::validate(
        &base, &rows, &map
    ));
}

#[test]
fn a_forged_review_kind_on_the_shared_sink_is_refused() {
    let (rows, map) = world();
    let key = CASES[8].0;
    let c = &map[key];
    // The only review kind the pins allow on the divergent-callers sink is the forward-callers one: a
    // not-a-bound disposition under another review prefix does not validate (no value, no clock producer).
    for review in [
        "x-exclusion: absolute instant",
        "category:reviewed-structural-slot; timeout",
        "forward-callers:instant-carrier; run_a10:1401",
        "forward-callers:",
        "",
    ] {
        let mut forged = row(&rows, key).clone();
        forged.disposition = "not-a-bound".into();
        forged.relation = String::new();
        forged.review = review.into();
        let mut all = rows.clone();
        let i = all.iter().position(|r| r.key == key).unwrap();
        all[i] = forged.clone();
        assert!(validate(&all).is_err(), "{review}");
        assert!(!callers::validate_with(&forged, &all, &map, callers::EDGES));
    }
    // A derived row with the right relation but another review kind is not a callers row either.
    let mut other = row(&rows, key).clone();
    other.review = other
        .review
        .replacen("forward-callers:", "clock-offset:", 1);
    let mut all = rows.clone();
    let i = all.iter().position(|r| r.key == key).unwrap();
    all[i] = other.clone();
    assert!(!callers::validate_with(&other, &all, &map, callers::EDGES));
    assert!(validate(&all).is_err());
    assert_eq!(c.kind, "keyword");
}

// ---------------------------------------------------------------------------------------------------
// Round 11 slice 3: the facts the census cannot show about the constructor callee `OwnerTokenPath`, re-derived
// from the oracle text (fails when the checkout is absent).
// ---------------------------------------------------------------------------------------------------
const ORACLE: &str = "f4e366e98a1f705dfaf182897044500d290dbd7f";
const OWNER_FILE: &str = "mycelium_live/token_path_owner.py";
const CALLER_FILE: &str = "mycelium_a13_macos/token_owner.py";

fn oracle_text(path: &str) -> String {
    let oracle = oracle_blobs::oracle_dir_or_panic();
    let show = std::process::Command::new("git")
        .arg("-C")
        .arg(oracle)
        .args(["show", &format!("{ORACLE}:{path}")])
        .output()
        .unwrap();
    assert!(show.status.success(), "{path}");
    String::from_utf8(show.stdout).unwrap()
}

fn span_text(text: &str, first: usize, last: usize) -> Vec<&str> {
    text.split('\n')
        .skip(first - 1)
        .take(last - first + 1)
        .collect()
}

/// Top-level comma-separated arguments of the call whose text starts at `open` (just after the `(`).
fn call_arguments(text: &str) -> Vec<String> {
    let (mut depth, mut current, mut out) = (0usize, String::new(), Vec::new());
    for ch in text.chars() {
        match ch {
            '(' | '[' | '{' => {
                depth += 1;
                current.push(ch);
            }
            ')' | ']' | '}' if depth == 0 => break,
            ')' | ']' | '}' => {
                depth -= 1;
                current.push(ch);
            }
            ',' if depth == 0 => out.push(std::mem::take(&mut current)),
            _ => current.push(ch),
        }
    }
    // A trailing comma (`f(a=1,\n)`) leaves an empty last argument.
    if !current.trim().is_empty() {
        out.push(current);
    }
    out.into_iter().map(|a| a.trim().to_string()).collect()
}

/// The oracle facts of one constructor callee that the census cannot show.
struct Ctor {
    class: &'static str,
    class_file: &'static str,
    class_line: usize,
    /// `__init__` first and last line.
    init: (usize, usize),
    /// How many lines from `init.0` hold the parameter list.
    signature_lines: usize,
    param: &'static str,
    /// Lines of `__init__` holding a `param=` (a call keyword, never a rebinding).
    keyword_lines: &'static [usize],
    /// Every in-scope line that mentions the class name: (file, line, fragment the line must contain).
    mentions: &'static [(&'static str, usize, &'static str)],
    call_file: &'static str,
    call: (usize, usize),
    /// The call argument the pinned census row stands for.
    call_argument: &'static str,
    edge_keys: &'static [&'static str],
}

fn check_constructor(c: &Ctor) {
    use sha2::{Digest, Sha256};
    let owner = oracle_text(c.class_file);
    let caller = oracle_text(c.call_file);
    // The class: plain `class C:` (no base, no decorator), defined once.
    assert_eq!(
        span_text(&owner, c.class_line, c.class_line)[0],
        format!("class {}:", c.class)
    );
    assert!(
        !span_text(&owner, c.class_line - 1, c.class_line - 1)[0]
            .trim_start()
            .starts_with('@')
    );
    // `__init__`: the parameter is keyword-only (after a bare `*`) and has no default.
    let signature = span_text(&owner, c.init.0, c.init.0 + c.signature_lines - 1).join(" ");
    let (_, after_star) = signature.split_once("*,").expect("keyword-only marker");
    let params = call_arguments(after_star);
    let name_of = |p: &String| {
        p.split([':', '=', ' '])
            .next()
            .unwrap_or_default()
            .to_string()
    };
    let declared: Vec<&String> = params.iter().filter(|p| name_of(p) == c.param).collect();
    assert_eq!(declared.len(), 1, "{params:?}");
    assert!(!declared[0].contains('='), "required: {}", declared[0]);
    // The parameter is never rebound inside `__init__`: its only `name=` lines are the pinned call keywords.
    let assigned: Vec<usize> = span_text(&owner, c.init.0, c.init.1)
        .iter()
        .enumerate()
        .filter(|(_, l)| {
            let t = l.trim_start();
            t.starts_with(&format!("{} =", c.param))
                || t.starts_with(&format!("{}=", c.param))
                || t.contains(&format!(" {} =", c.param))
        })
        .map(|(i, _)| c.init.0 + i)
        .collect();
    assert_eq!(assigned, c.keyword_lines);
    // The class name occurs exactly where pinned: import, definition, the one call, annotations/`isinstance`.
    let mut hits = Vec::new();
    for path in include_str!("../data/python-scope.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
    {
        for (i, l) in oracle_text(path).split('\n').enumerate() {
            if l.contains(c.class) {
                hits.push((path.to_string(), i + 1, l.to_string()));
            }
        }
    }
    assert_eq!(
        hits.iter().map(|h| (h.0.as_str(), h.1)).collect::<Vec<_>>(),
        c.mentions.iter().map(|m| (m.0, m.1)).collect::<Vec<_>>()
    );
    for (hit, m) in hits.iter().zip(c.mentions) {
        assert!(hit.2.contains(m.2), "{}:{}: {}", m.0, m.1, hit.2);
    }
    // The call: keyword-only (no positional argument, no `*`/`**`), passing the parameter as the pinned row says.
    let call = span_text(&caller, c.call.0, c.call.1).join("\n");
    let args = call_arguments(call.split_once(&format!("{}(", c.class)).unwrap().1);
    assert!(args.len() >= 6, "{args:?}");
    for a in &args {
        let (name, _) = a
            .split_once('=')
            .unwrap_or_else(|| panic!("positional {a}"));
        assert!(
            !name.is_empty() && name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_'),
            "{a}"
        );
    }
    assert!(args.iter().any(|a| a == c.call_argument), "{args:?}");
    // The pinned call-site span of every edge hashes to the oracle text.
    for key in c.edge_keys {
        let edges = callers::edges_of(callers::EDGES, key).unwrap();
        assert_eq!(edges.len(), 1, "{key}");
        let (location, rest) = edges[0].reference.split_once(" @ ").unwrap();
        let hash = rest
            .split_once("exact-span-sha256=")
            .unwrap()
            .1
            .split(';')
            .next()
            .unwrap();
        assert_eq!(
            location,
            format!("{}:{}-{}", c.call_file, c.call.0, c.call.1),
            "{key}"
        );
        let got: String =
            Sha256::digest(span_text(&caller, c.call.0, c.call.1).join("\n").as_bytes())
                .iter()
                .map(|x| format!("{x:02x}"))
                .collect();
        assert_eq!(got, hash, "{key}");
        assert_eq!(
            edges[0].callee,
            format!("{}::{}.__init__", c.class_file, c.class),
            "{key}"
        );
    }
}

#[test]
fn the_owner_token_path_constructor_facts_hold_in_the_oracle() {
    check_constructor(&Ctor {
        class: "OwnerTokenPath",
        class_file: OWNER_FILE,
        class_line: 22,
        init: (23, 51),
        signature_lines: 5,
        param: "process_deadline_monotonic_s",
        keyword_lines: &[48],
        mentions: &[
            (CALLER_FILE, 16, "import OwnerTokenPath"),
            (CALLER_FILE, 182, "OwnerTokenPath(signer="),
            (OWNER_FILE, 22, "class OwnerTokenPath:"),
        ],
        call_file: CALLER_FILE,
        call: (182, 187),
        call_argument: "process_deadline_monotonic_s=self.deadline",
        edge_keys: &[
            "mycelium_live/token_path_owner.py:43:8:assignment:self._process_deadline",
            "mycelium_live/token_path_owner.py:48:12:keyword:process_deadline_monotonic_s",
        ],
    });
}

#[test]
fn the_entry_admission_policy_constructor_facts_hold_in_the_oracle() {
    check_constructor(&Ctor {
        class: "EntryAdmissionPolicy",
        class_file: "mycelium_node/token_admission.py",
        class_line: 36,
        init: (45, 62),
        signature_lines: 5,
        param: "process_deadline_monotonic_s",
        keyword_lines: &[],
        mentions: &[
            (OWNER_FILE, 13, "import EntryAdmissionPolicy"),
            (OWNER_FILE, 46, "EntryAdmissionPolicy(scope_pins="),
            (
                "mycelium_node/token_admission.py",
                36,
                "class EntryAdmissionPolicy:",
            ),
            (
                "physical_inference_node.py",
                4609,
                "import EntryAdmissionPolicy, valid_start_grant_shape",
            ),
            (
                "physical_inference_node.py",
                4616,
                "isinstance(policy, EntryAdmissionPolicy)",
            ),
        ],
        call_file: OWNER_FILE,
        call: (46, 51),
        call_argument: "process_deadline_monotonic_s=process_deadline_monotonic_s",
        edge_keys: &["mycelium_node/token_admission.py:56:8:assignment:self._process_deadline"],
    });
}

#[test]
fn the_lease_deadline_constructor_facts_hold_in_the_oracle() {
    check_constructor(&Ctor {
        class: "OwnerTokenPath",
        class_file: OWNER_FILE,
        class_line: 22,
        init: (23, 51),
        signature_lines: 5,
        param: "lease_deadline_monotonic_s",
        keyword_lines: &[49],
        mentions: &[
            (CALLER_FILE, 16, "import OwnerTokenPath"),
            (CALLER_FILE, 182, "OwnerTokenPath(signer="),
            (OWNER_FILE, 22, "class OwnerTokenPath:"),
        ],
        call_file: CALLER_FILE,
        call: (182, 187),
        call_argument: "lease_deadline_monotonic_s=self._lease_deadline",
        edge_keys: &[
            "mycelium_live/token_path_owner.py:44:8:assignment:self._lease_deadline",
            "mycelium_live/token_path_owner.py:49:12:keyword:lease_deadline_monotonic_s",
        ],
    });
    check_constructor(&Ctor {
        class: "EntryAdmissionPolicy",
        class_file: "mycelium_node/token_admission.py",
        class_line: 36,
        init: (45, 62),
        signature_lines: 5,
        param: "lease_deadline_monotonic_s",
        keyword_lines: &[],
        mentions: &[
            (OWNER_FILE, 13, "import EntryAdmissionPolicy"),
            (OWNER_FILE, 46, "EntryAdmissionPolicy(scope_pins="),
            (
                "mycelium_node/token_admission.py",
                36,
                "class EntryAdmissionPolicy:",
            ),
            (
                "physical_inference_node.py",
                4609,
                "import EntryAdmissionPolicy, valid_start_grant_shape",
            ),
            (
                "physical_inference_node.py",
                4616,
                "isinstance(policy, EntryAdmissionPolicy)",
            ),
        ],
        call_file: OWNER_FILE,
        call: (46, 51),
        call_argument: "lease_deadline_monotonic_s=lease_deadline_monotonic_s",
        edge_keys: &["mycelium_node/token_admission.py:57:8:assignment:self._lease_deadline"],
    });
}

#[test]
fn the_prefill_wait_budget_constructor_facts_hold_in_the_oracle() {
    const NODE: &str = "physical_inference_node.py";
    check_constructor(&Ctor {
        class: "_PrefillWaitBudget",
        class_file: NODE,
        class_line: 494,
        init: (510, 566),
        signature_lines: 10,
        param: "command_timeout",
        keyword_lines: &[564],
        mentions: &[
            (NODE, 494, "class _PrefillWaitBudget:"),
            (NODE, 4885, "budget = _PrefillWaitBudget("),
            (NODE, 4915, "budget: _PrefillWaitBudget,"),
        ],
        call_file: NODE,
        call: (4885, 4896),
        call_argument: "command_timeout=self.command_timeout",
        edge_keys: &["physical_inference_node.py:549:8:assignment:self.command_timeout"],
    });
    // The wrapper `float(command_timeout)` is the builtin: the file never rebinds, imports or defines `float`.
    let source = oracle_text(NODE);
    assert_eq!(
        span_text(&source, 549, 549)[0].trim(),
        "self.command_timeout = float(command_timeout)"
    );
    for (number, text) in source.split('\n').enumerate() {
        let t = text.trim_start();
        assert!(!t.starts_with("def float"), "{number}: {text}");
        assert!(!t.starts_with("float ="), "{number}: {text}");
        assert!(!t.starts_with("float="), "{number}: {text}");
        assert!(!t.starts_with("class float"), "{number}: {text}");
        assert!(!text.contains(" as float"), "{number}: {text}");
        assert!(
            !(t.starts_with("import ") || t.starts_with("from ")) || !text.contains("float"),
            "{number}: {text}"
        );
    }
    assert!(!source.contains("global float"));
}

#[test]
fn only_a_bare_float_wrapper_is_the_same_flow_as_the_parameter() {
    let (rows, map) = world();
    let key = "physical_inference_node.py:549:8:assignment:self.command_timeout";
    let forged = |expression: &str| {
        let mut m = map.clone();
        m.get_mut(key).unwrap().expression = expression.into();
        let mut b = row(&rows, key).clone();
        b.expression = expression.into();
        let mut all = rows.clone();
        let i = all.iter().position(|r| r.key == key).unwrap();
        all[i] = b;
        ok_with(&all, &m, key, callers::EDGES)
    };
    assert!(forged("float(command_timeout)"), "the control");
    assert!(
        forged("command_timeout"),
        "the bare parameter is the original rule"
    );
    for refused in [
        "float(command_timeout, 1)",
        "float()",
        "int(command_timeout)",
        "float(float(command_timeout))",
        "float( command_timeout)",
        "float(command_timeout )",
        "float(command_timeout) + 1",
        "float(command_timeout",
        "float(self.command_timeout)",
        "Float(command_timeout)",
        "xfloat(command_timeout)",
        "float(1)",
        "float(other)",
        "float(command_timeout))",
        "(float(command_timeout))",
    ] {
        assert!(!forged(refused), "{refused}");
    }
}
