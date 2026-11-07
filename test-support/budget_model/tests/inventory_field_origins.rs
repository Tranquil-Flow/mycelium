// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/router_port.py; O:path:mycelium_live/command_controller.py.
//! L1b round 10, class/field resolution slice 1 (`CLASS-FIELD-DESIGN.md` section 6): `envelope.maximum_request_bytes`
//! and `envelope.maximum_response_bytes` in `LiveRouterPort._admit_serialized` (router_port.py:896/:897) and
//! `_register_recovery_command` (:2664/:2665). `envelope` is a local bound once by `envelope = CommandEnvelope(...)`;
//! `CommandEnvelope` is a frozen dataclass (command_controller.py:155-185) whose two fields have no default and whose
//! only two constructor calls (router_port.py:860 and :2640) pass them by keyword to reviewed literals. Each read
//! therefore derives from the ordered `+` of both writer rows. The census cannot show the class, the binding or
//! the completeness of the constructor calls, so `python-field-origins.tsv` pins them with exact-span hashes
//! (re-derived from the oracle by the last tests); Rust re-checks everything the census shows.
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
use budget_model::inventory::{
    Bound, Candidate, candidate_keys, candidates, checked_in, field_origins, validate,
};
use budgets::BudgetId;
use budgets::table::Value;
use std::collections::{BTreeMap, BTreeSet};

const RP: &str = "mycelium_live/router_port.py";
const CC: &str = "mycelium_live/command_controller.py";
const ORACLE: &str = "f4e366e98a1f705dfaf182897044500d290dbd7f";

const REQ_2649: &str = "mycelium_live/router_port.py:2649:12:keyword:maximum_request_bytes";
const REQ_869: &str = "mycelium_live/router_port.py:869:16:keyword:maximum_request_bytes";
const RES_2650: &str = "mycelium_live/router_port.py:2650:12:keyword:maximum_response_bytes";
const RES_870: &str = "mycelium_live/router_port.py:870:16:keyword:maximum_response_bytes";
const ID_REQ_2649: &str = "PyFinalMyceliumLiveRouterPortMaximumRequestBytesL2649C12";
const ID_REQ_869: &str = "PyFinalMyceliumLiveRouterPortMaximumRequestBytesL869C16";
const ID_RES_2650: &str = "PyFinalMyceliumLiveRouterPortMaximumResponseBytesL2650C12";
const ID_RES_870: &str = "PyFinalMyceliumLiveRouterPortMaximumResponseBytesL870C16";

/// (read key, scope, field, [(writer key, registry id)] in ascending key order, writer value in bytes)
type Case = (
    &'static str,
    &'static str,
    &'static str,
    [(&'static str, &'static str); 2],
    u64,
);
const CASES: [Case; 4] = [
    (
        "mycelium_live/router_port.py:896:20:dict-field:maximum_request_bytes",
        "LiveRouterPort._admit_serialized",
        "maximum_request_bytes",
        [(REQ_2649, ID_REQ_2649), (REQ_869, ID_REQ_869)],
        131_072,
    ),
    (
        "mycelium_live/router_port.py:897:20:dict-field:maximum_response_bytes",
        "LiveRouterPort._admit_serialized",
        "maximum_response_bytes",
        [(RES_2650, ID_RES_2650), (RES_870, ID_RES_870)],
        16_777_216,
    ),
    (
        "mycelium_live/router_port.py:2664:16:dict-field:maximum_request_bytes",
        "LiveRouterPort._register_recovery_command",
        "maximum_request_bytes",
        [(REQ_2649, ID_REQ_2649), (REQ_869, ID_REQ_869)],
        131_072,
    ),
    (
        "mycelium_live/router_port.py:2665:16:dict-field:maximum_response_bytes",
        "LiveRouterPort._register_recovery_command",
        "maximum_response_bytes",
        [(RES_2650, ID_RES_2650), (RES_870, ID_RES_870)],
        16_777_216,
    ),
];
/// Slice 2 (`self-attribute`): (read key, path, class, method, field, writer key, writer line, relation)
type SelfCase = (
    &'static str,
    &'static str,
    &'static str,
    &'static str,
    &'static str,
    &'static str,
    usize,
    &'static str,
);
const CLAMP_KEYS: [&str; 2] = [
    "mycelium_a13_macos/token_owner.py:94:12:assignment:self._lease_deadline",
    "mycelium_a13_macos/token_owner.py:138:16:assignment:self._lease_deadline",
];
const SELF_CASES: [SelfCase; 7] = [
    (
        "mycelium_a13_macos/token_owner.py:185:41:keyword:process_deadline_monotonic_s",
        "mycelium_a13_macos/token_owner.py",
        "ManagedRouteTokenOwner",
        "prepare",
        "deadline",
        "mycelium_a13_macos/token_owner.py:32:8:assignment:self.deadline",
        32,
        "PyClockMyceliumA13MacosWorkflowL493C8",
    ),
    (
        "physical_inference_node.py:4895:12:keyword:command_timeout",
        "physical_inference_node.py",
        "PhysicalNodeService",
        "_infer_start_legacy",
        "command_timeout",
        "physical_inference_node.py:2195:8:assignment:self.command_timeout",
        2195,
        "PyPhysicalInferenceNodeParseArgsCommandTimeoutL6667",
    ),
    // Slice 4: the keyword `process_deadline_monotonic_s=self._process_deadline` in `EntryAdmissionPolicy.admit`;
    // the one store is `__init__`'s forward of the required parameter (closed by `forward-callers`).
    (
        "mycelium_node/token_admission.py:92:12:keyword:process_deadline_monotonic_s",
        "mycelium_node/token_admission.py",
        "EntryAdmissionPolicy",
        "admit",
        "_process_deadline",
        "mycelium_node/token_admission.py:56:8:assignment:self._process_deadline",
        56,
        "PyClockMyceliumA13MacosWorkflowL493C8",
    ),
    // Slice 5 (rule `self-clamp`): `self._lease_deadline = min(self.deadline, lease)` is bounded by the first
    // operand, whose stores are the reviewed ones of `self.deadline` (token_owner.py:32).
    (
        "mycelium_a13_macos/token_owner.py:94:12:assignment:self._lease_deadline",
        "mycelium_a13_macos/token_owner.py",
        "ManagedRouteTokenOwner",
        "_refresh_lease",
        "deadline",
        "mycelium_a13_macos/token_owner.py:32:8:assignment:self.deadline",
        32,
        "PyClockMyceliumA13MacosWorkflowL493C8",
    ),
    (
        "mycelium_a13_macos/token_owner.py:138:16:assignment:self._lease_deadline",
        "mycelium_a13_macos/token_owner.py",
        "ManagedRouteTokenOwner",
        "install",
        "deadline",
        "mycelium_a13_macos/token_owner.py:32:8:assignment:self.deadline",
        32,
        "PyClockMyceliumA13MacosWorkflowL493C8",
    ),
    // Slice 6: `_PrefillWaitBudget.command_timeout` is stored once, by `float(command_timeout)` in `__init__` (closed by
    // `forward-callers` from the call site physical_inference_node.py:4885).
    (
        "physical_inference_node.py:564:12:keyword:command_timeout",
        "physical_inference_node.py",
        "_PrefillWaitBudget",
        "__init__",
        "command_timeout",
        "physical_inference_node.py:549:8:assignment:self.command_timeout",
        549,
        "PyPhysicalInferenceNodeParseArgsCommandTimeoutL6667",
    ),
    (
        "physical_inference_node.py:664:12:keyword:command_timeout",
        "physical_inference_node.py",
        "_PrefillWaitBudget",
        "_extend",
        "command_timeout",
        "physical_inference_node.py:549:8:assignment:self.command_timeout",
        549,
        "PyPhysicalInferenceNodeParseArgsCommandTimeoutL6667",
    ),
];
/// Same file, same kind of read, but the writers are not reviewed (or there are none): must stay pending.
const N_PENDING_WRITER: &str =
    "mycelium_router/a10_qualification.py:424:9:dict-field:maximum_bytes";
const N_PENDING_WRITER_KEYWORD: &str =
    "mycelium_router/a10_qualification.py:412:9:keyword:maximum_bytes";
const N_NO_WRITER: &str = "mycelium_node/__main__.py:1155:8:assignment:lease_expires_at";

type World = (Vec<Bound>, BTreeMap<String, Candidate>);

fn world() -> World {
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

fn with(rows: &[Bound], m: Bound) -> Vec<Bound> {
    let mut all = rows.to_vec();
    let i = all.iter().position(|r| r.key == m.key).unwrap();
    all[i] = m;
    all
}

/// The table with the line of `key` replaced by `f(columns)`.
fn edit(key: &str, f: impl Fn(Vec<String>) -> Vec<String>) -> String {
    let mut out = Vec::new();
    for line in field_origins::TABLE.lines() {
        if line.split('\t').next() == Some(key) {
            let cols = f(line.split('\t').map(String::from).collect());
            out.push(cols.join("\t"));
        } else {
            out.push(line.to_string());
        }
    }
    out.join("\n") + "\n"
}

fn ok_with(rows: &[Bound], map: &BTreeMap<String, Candidate>, key: &str, table: &str) -> bool {
    field_origins::validate_with(row(rows, key), rows, map, table)
}

/// Spans of a reference column, replaced through `f` and re-joined.
fn respan(reference: &str, f: impl Fn(Vec<String>) -> Vec<String>) -> String {
    f(reference.split(" | ").map(String::from).collect()).join(" | ")
}

#[test]
fn four_command_envelope_reads_derive_from_both_constructor_calls() {
    let (rows, map) = world();
    let reviewed: BTreeSet<&str> = rows
        .iter()
        .filter(|r| r.review.starts_with("field-origin:local-constructor; "))
        .map(|r| r.key.as_str())
        .collect();
    assert_eq!(reviewed, CASES.iter().map(|c| c.0).collect::<BTreeSet<_>>());
    for (key, scope, field, writers, bytes) in CASES {
        let r = row(&rows, key);
        assert_eq!(r.path, RP);
        assert_eq!(r.scope, scope);
        assert_eq!(r.expression, format!("envelope.{field}"));
        assert_eq!(r.disposition, "derived", "{key}");
        assert!(r.budget.is_empty() && r.value.is_none(), "{key}");
        assert_eq!(r.unit, "bytes", "{key}");
        assert!(
            r.review.starts_with("field-origin:local-constructor; "),
            "{key}"
        );
        assert_eq!(
            r.relation,
            format!("{}+{}", writers[0].1, writers[1].1),
            "{key}"
        );
        assert!(field_origins::validate(r, &rows, &map), "{key}");
        // The writers are the two constructor keywords: independent literal values from the oracle text.
        for (writer, id) in writers {
            let w = row(&rows, writer);
            assert_eq!(w.budget, id, "{writer}");
            assert_eq!(w.value, Some(bytes), "{writer}");
            assert_eq!(
                BudgetId::from_name(id).unwrap().entry().value,
                Value::Storage(budgets::Bytes::new(bytes)),
                "{id}"
            );
            assert_eq!(map[writer].symbol, field);
            assert_eq!(map[writer].kind, "keyword");
        }
    }
    assert!(validate(&rows).is_ok());
}

#[test]
fn the_table_has_exactly_the_four_entries_in_one_shape() {
    let lines: Vec<&str> = field_origins::TABLE.lines().collect();
    assert_eq!(lines[0], field_origins::HEADER);
    assert_eq!(
        lines.len(),
        13,
        "4 local-constructor + 5 self-attribute + 2 self-clamp + 1 self-attribute with an inactive initialiser"
    );
    let keys: BTreeSet<&str> = lines[1..]
        .iter()
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(
        keys,
        CASES
            .iter()
            .map(|c| c.0)
            .chain(SELF_CASES.iter().map(|c| c.0))
            .chain([LEASE_READ])
            .collect::<BTreeSet<_>>()
    );
    for (line, case) in lines[1..].iter().zip(CASES) {
        let f: Vec<&str> = line.split('\t').collect();
        assert_eq!(f.len(), 8);
        assert_eq!(f[1], "local-constructor");
        assert_eq!(f[2], "envelope");
        assert_eq!(f[3], CC);
        assert_eq!(f[4], "CommandEnvelope");
        assert_eq!(f[5], case.2);
        assert_eq!(f[6], format!("{};{}", case.3[0].0, case.3[1].0));
        // class, field, function, bind (the call in the read's own function), one ctor.
        let labels: Vec<&str> = f[7]
            .split(" | ")
            .map(|s| s.split(' ').next().unwrap())
            .collect();
        assert_eq!(labels, ["class", "field", "function", "bind", "ctor"]);
    }
}

#[test]
fn the_neighbouring_attribute_reads_stay_pending() {
    let (rows, map) = world();
    let reviewed: BTreeSet<&str> = rows.iter().map(|r| r.key.as_str()).collect();
    let pending: BTreeSet<String> = candidate_keys()
        .unwrap()
        .into_iter()
        .filter(|k| !reviewed.contains(k.as_str()))
        .collect();
    for key in [N_PENDING_WRITER, N_PENDING_WRITER_KEYWORD, N_NO_WRITER] {
        assert!(pending.contains(key), "{key} must stay pending");
    }
    // A forgery for `capability.maximum_bytes` that is otherwise consistent (spans around the census lines,
    // a derived row, a table entry) is refused only because its writer row (a10_qualification.py:412) is
    // pending; making that writer a reviewed row makes the same forgery pass, so nothing else refused it.
    let c = &map[N_PENDING_WRITER];
    let w = &map[N_PENDING_WRITER_KEYWORD];
    let forged = Bound {
        key: c.key.clone(),
        path: c.path.clone(),
        line: c.line,
        column: c.column,
        scope: c.scope.clone(),
        symbol: c.symbol.clone(),
        expression: c.expression.clone(),
        unit: "bytes".into(),
        value: None,
        semantic: "forged".into(),
        authority: "forged".into(),
        budget: String::new(),
        disposition: "derived".into(),
        review: "field-origin:local-constructor; forged".into(),
        relation: ID_REQ_869.into(),
        source_sha: "ef9ae61de0222c78d83c8133f8f709619394bf51".into(),
    };
    let zero = "0".repeat(64);
    let span = |label: &str, path: &str, a: usize, b: usize| {
        format!("{label} {path}:{a}-{b} @ {ORACLE}; exact-span-sha256={zero}")
    };
    let reference = [
        span("class", CC, 155, 185),
        span("field", CC, 167, 167),
        span("function", &c.path, c.line - 24, c.line + 6),
        span("bind", &c.path, w.line - 2, w.line + 3),
    ]
    .join(" | ");
    let table = format!(
        "{}{}\tlocal-constructor\tcapability\t{CC}\tCommandEnvelope\tmaximum_bytes\t{}\t{reference}\n",
        field_origins::TABLE,
        c.key,
        w.key
    );
    let mut all = rows.clone();
    all.push(forged.clone());
    assert!(!field_origins::validate_with(&forged, &all, &map, &table));
    let mut writer = row(&rows, REQ_869).clone();
    writer.key = w.key.clone();
    writer.path = w.path.clone();
    writer.line = w.line;
    writer.column = w.column;
    writer.scope = w.scope.clone();
    writer.symbol = w.symbol.clone();
    writer.expression = w.expression.clone();
    all.push(writer);
    assert!(
        field_origins::validate_with(&forged, &all, &map, &table),
        "the forgery must be consistent apart from the pending writer"
    );
}

#[test]
fn each_table_check_is_refused_when_broken_for_every_read() {
    let (rows, map) = world();
    for (key, _, _, writers, _) in CASES {
        assert!(ok_with(&rows, &map, key, field_origins::TABLE), "{key}");
        let r = row(&rows, key).clone();
        let t = field_origins::TABLE;
        let other_field = if r.expression.ends_with("request_bytes") {
            "maximum_response_bytes"
        } else {
            "maximum_request_bytes"
        };
        let table_edits: Vec<(&str, String)> = vec![
            ("header", t.replacen("key\trule", "key\trules", 1)),
            (
                "rule",
                edit(key, |mut c| {
                    c[1] = "other".into();
                    c
                }),
            ),
            (
                "receiver",
                edit(key, |mut c| {
                    c[2] = "env".into();
                    c
                }),
            ),
            (
                "class-file",
                edit(key, |mut c| {
                    c[3] = RP.into();
                    c
                }),
            ),
            (
                "field",
                edit(key, |mut c| {
                    c[5] = other_field.into();
                    c
                }),
            ),
            (
                "class-name",
                edit(key, |mut c| {
                    c[4] = "1Bad".into();
                    c
                }),
            ),
            (
                "class-file-ext",
                edit(key, |mut c| {
                    c[3] = "mycelium_live/command_controller.txt".into();
                    c
                }),
            ),
            (
                "writers-swapped",
                edit(key, |mut c| {
                    c[6] = format!("{};{}", writers[1].0, writers[0].0);
                    c
                }),
            ),
            (
                "writers-duplicate",
                edit(key, |mut c| {
                    c[6] = format!("{};{}", writers[0].0, writers[0].0);
                    c
                }),
            ),
            (
                "writers-one",
                edit(key, |mut c| {
                    c[6] = writers[0].0.into();
                    c
                }),
            ),
            (
                "writers-unknown",
                edit(key, |mut c| {
                    c[6] = format!("{};{}x", writers[0].0, writers[1].0);
                    c
                }),
            ),
            (
                "writers-not-keyword",
                edit(key, |mut c| {
                    c[6] = format!("{};{key}", writers[0].0);
                    c
                }),
            ),
            (
                "writers-other-field",
                edit(key, |mut c| {
                    let other = if other_field.ends_with("response_bytes") {
                        [RES_2650, RES_870]
                    } else {
                        [REQ_2649, REQ_869]
                    };
                    c[6] = format!("{};{}", other[0], other[1]);
                    c
                }),
            ),
            (
                "ref-drop-class",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        s.remove(0);
                        s
                    });
                    c
                }),
            ),
            (
                "ref-drop-ctor",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        s.pop();
                        s
                    });
                    c
                }),
            ),
            (
                "ref-extra-ctor",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        let last = s.last().unwrap().clone();
                        s.push(last);
                        s
                    });
                    c
                }),
            ),
            (
                "ref-labels-swapped",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        s.swap(0, 1);
                        s
                    });
                    c
                }),
            ),
            (
                "ref-bind-as-ctor",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        s[3] = s[3].replacen("bind ", "ctor ", 1);
                        s
                    });
                    c
                }),
            ),
            (
                "ref-ctor-as-bind",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        s[4] = s[4].replacen("ctor ", "bind ", 1);
                        s
                    });
                    c
                }),
            ),
            (
                "ref-hash-short",
                edit(key, |mut c| {
                    c[7].pop();
                    c
                }),
            ),
            (
                "ref-hash-upper",
                edit(key, |mut c| {
                    let at = c[7].rfind("exact-span-sha256=").unwrap() + "exact-span-sha256=".len();
                    let tail = c[7][at..].to_uppercase();
                    c[7].truncate(at);
                    c[7].push_str(&tail);
                    c
                }),
            ),
            (
                "ref-oracle-short",
                edit(key, |mut c| {
                    c[7] = c[7].replacen(ORACLE, "f4e366e9", 1);
                    c
                }),
            ),
            (
                "ref-no-hash-marker",
                edit(key, |mut c| {
                    c[7] = c[7].replacen("exact-span-sha256=", "span=", 1);
                    c
                }),
            ),
            (
                "ref-not-py",
                edit(key, |mut c| {
                    c[7] = c[7].replacen("command_controller.py:", "command_controller.txt:", 1);
                    c
                }),
            ),
            (
                "ref-class-path",
                edit(key, |mut c| {
                    c[7] = c[7].replacen(&format!("class {CC}:"), &format!("class {RP}:"), 1);
                    c
                }),
            ),
            (
                "ref-field-outside-class",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        s[1] = s[1]
                            .replacen(":167-167", ":300-300", 1)
                            .replacen(":168-168", ":300-300", 1);
                        s
                    });
                    c
                }),
            ),
            (
                "ref-field-path",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        s[1] = s[1].replacen(CC, RP, 1);
                        s
                    });
                    c
                }),
            ),
            (
                "ref-function-not-covering-read",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        let (head, rest) = s[2].split_once(" @ ").unwrap();
                        let head = head.rsplit_once(':').unwrap().0.to_string();
                        let rest = rest.to_string();
                        s[2] = format!("{head}:1-2 @ {rest}");
                        s
                    });
                    c
                }),
            ),
            (
                "ref-function-path",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        s[2] = s[2].replacen(
                            &format!("function {RP}:"),
                            &format!("function {CC}:"),
                            1,
                        );
                        s
                    });
                    c
                }),
            ),
            (
                "ref-bind-after-read",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        let rest = s[3].split_once(" @ ").unwrap().1.to_string();
                        s[3] = format!("bind {RP}:{}-{} @ {rest}", r.line, r.line + 1);
                        s
                    });
                    c
                }),
            ),
            (
                "ref-bind-path",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        s[3] = s[3].replacen(&format!("bind {RP}:"), &format!("bind {CC}:"), 1);
                        s
                    });
                    c
                }),
            ),
            (
                "ref-bind-outside-function",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        let rest = s[3].split_once(" @ ").unwrap().1.to_string();
                        s[3] = format!("bind {RP}:30-31 @ {rest}");
                        s
                    });
                    c
                }),
            ),
            (
                "ref-ctor-inside-function",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        let f = s[2].split_once(' ').unwrap().1.to_string();
                        s[4] = format!("ctor {f}");
                        s
                    });
                    c
                }),
            ),
            (
                "ref-ctor-without-writer",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        let rest = s[4].split_once(" @ ").unwrap().1.to_string();
                        s[4] = format!("ctor {RP}:3000-3001 @ {rest}");
                        s
                    });
                    c
                }),
            ),
            (
                "ref-ctor-wrong-path",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        s[4] = s[4].replacen(&format!("ctor {RP}:"), &format!("ctor {CC}:"), 1);
                        s
                    });
                    c
                }),
            ),
            (
                "ref-trailing-text",
                edit(key, |mut c| {
                    c[7].push_str(" x");
                    c
                }),
            ),
            (
                "ref-first-after-last",
                edit(key, |mut c| {
                    c[7] = c[7].replacen(":155-185", ":185-155", 1);
                    c
                }),
            ),
            (
                "ref-line-zero",
                edit(key, |mut c| {
                    c[7] = c[7].replacen(":155-185", ":0-185", 1);
                    c
                }),
            ),
            (
                "empty-column",
                edit(key, |mut c| {
                    c[5] = String::new();
                    c
                }),
            ),
            (
                "no-entry",
                t.lines()
                    .filter(|l| l.split('\t').next() != Some(key))
                    .collect::<Vec<_>>()
                    .join("\n")
                    + "\n",
            ),
            (
                "duplicate-entry",
                format!("{t}{}\n", t.lines().find(|l| l.starts_with(key)).unwrap()),
            ),
        ];
        for (name, table) in &table_edits {
            assert!(!ok_with(&rows, &map, key, table), "{key}: {name}");
        }
    }
}

#[test]
fn each_bounds_row_column_and_writer_row_is_checked() {
    let (rows, map) = world();
    type Mutation = Box<dyn Fn(&mut Bound)>;
    for (key, _, _, writers, _) in CASES {
        let base = row(&rows, key).clone();
        let muts: Vec<(&str, Mutation)> = vec![
            (
                "disposition",
                Box::new(|b| b.disposition = "reviewed".into()),
            ),
            (
                "disposition-empty",
                Box::new(|b| b.disposition = String::new()),
            ),
            ("budget", Box::new(|b| b.budget = ID_REQ_869.into())),
            ("value", Box::new(|b| b.value = Some(131_072))),
            ("unit-ms", Box::new(|b| b.unit = "milliseconds".into())),
            ("unit-entries", Box::new(|b| b.unit = "entries".into())),
            ("relation-empty", Box::new(|b| b.relation = String::new())),
            (
                "relation-one",
                Box::new(|b| b.relation = b.relation.split('+').next().unwrap().into()),
            ),
            (
                "relation-swapped",
                Box::new(|b| {
                    let mut p: Vec<&str> = b.relation.split('+').collect();
                    p.reverse();
                    b.relation = p.join("+");
                }),
            ),
            (
                "relation-three",
                Box::new(|b| b.relation = format!("{}+{}", b.relation, ID_REQ_869)),
            ),
            (
                "relation-other-id",
                Box::new(|b| b.relation = ID_RES_870.to_string() + "+" + ID_RES_2650),
            ),
            (
                "review-rule",
                Box::new(|b| b.review = "field-origin:unknown-rule; x".into()),
            ),
            (
                "review-prefix",
                Box::new(|b| b.review = "forward-callers:local-constructor; x".into()),
            ),
            ("line", Box::new(|b| b.line += 1)),
            ("column", Box::new(|b| b.column += 1)),
            ("scope", Box::new(|b| b.scope = "other".into())),
            ("symbol", Box::new(|b| b.symbol = "other_bytes".into())),
            (
                "expression",
                Box::new(|b| b.expression = "envelope.other".into()),
            ),
            ("path", Box::new(|b| b.path = "other.py".into())),
        ];
        for (name, m) in &muts {
            let mut b = base.clone();
            m(&mut b);
            let all = with(&rows, b);
            assert!(
                !field_origins::validate_with(row(&all, key), &all, &map, field_origins::TABLE),
                "{key}: {name}"
            );
        }
        // Writer rows: gone, unreviewed, another unit, another kind of mapping.
        let w = writers[0].0;
        let wb = row(&rows, w).clone();
        let wmuts: Vec<(&str, Mutation)> = vec![
            (
                "writer-disposition",
                Box::new(|b| {
                    b.budget = String::new();
                    b.disposition = "not-a-bound".into();
                }),
            ),
            ("writer-unit", Box::new(|b| b.unit = "entries".into())),
            (
                "writer-budget-unit",
                Box::new(|b| b.budget = "WorkerStartup".into()),
            ),
            (
                "writer-budget-unknown",
                Box::new(|b| b.budget = "NoSuchRegistryId".into()),
            ),
            (
                "writer-derived-empty",
                Box::new(|b| {
                    b.budget = String::new();
                    b.disposition = "derived".into();
                    b.relation = String::new();
                }),
            ),
            (
                "writer-derived-and-budget",
                Box::new(|b| b.disposition = "derived".into()),
            ),
        ];
        for (name, m) in &wmuts {
            let mut b = wb.clone();
            m(&mut b);
            let all = with(&rows, b);
            assert!(
                !field_origins::validate_with(row(&all, key), &all, &map, field_origins::TABLE),
                "{key}: {name}"
            );
        }
        let without: Vec<Bound> = rows.iter().filter(|r| r.key != w).cloned().collect();
        assert!(
            !field_origins::validate_with(row(&without, key), &without, &map, field_origins::TABLE),
            "{key}: writer row missing"
        );
    }
}

#[test]
fn census_visible_facts_refuse_a_consistently_forged_table() {
    let (rows, map) = world();
    let key = CASES[0].0;
    let c = map[key].clone();
    let table = field_origins::TABLE;
    assert!(ok_with(&rows, &map, key, table));
    let extra = |kind: &str, scope: &str, symbol: &str| {
        let mut m = map.clone();
        m.insert(
            "forged".into(),
            Candidate {
                key: "forged".into(),
                path: c.path.clone(),
                line: 1,
                column: 1,
                scope: scope.into(),
                symbol: symbol.into(),
                expression: "x".into(),
                kind: kind.into(),
                source: "x".into(),
            },
        );
        m
    };
    // A census assignment / parameter of the receiver in the read's scope is a second root.
    for kind in ["assignment", "parameter"] {
        assert!(
            !ok_with(&rows, &extra(kind, &c.scope, "envelope"), key, table),
            "{kind}"
        );
    }
    // The store-completeness scan belongs to `self-attribute` only: a census assignment whose symbol is the
    // read's own text, in a scope under the class name, is irrelevant to a local-constructor read.
    assert!(ok_with(
        &rows,
        &extra(
            "assignment",
            "CommandEnvelope.__init__",
            "envelope.maximum_request_bytes"
        ),
        key,
        table
    ));
    // Other kinds, other scopes and other names do not matter.
    assert!(ok_with(
        &rows,
        &extra("keyword", &c.scope, "envelope"),
        key,
        table
    ));
    assert!(ok_with(
        &rows,
        &extra("assignment", "elsewhere", "envelope"),
        key,
        table
    ));
    assert!(ok_with(
        &rows,
        &extra("assignment", &c.scope, "other"),
        key,
        table
    ));
    // The census row itself: a different expression is refused even when row and table agree on it.
    for (expression, receiver) in [
        ("self.maximum_request_bytes", "self"),
        ("cls.maximum_request_bytes", "cls"),
        ("envelope.sub.maximum_request_bytes", "envelope"),
        ("envelope", "envelope"),
        ("envelope.", "envelope"),
        (".maximum_request_bytes", ""),
        ("1envelope.maximum_request_bytes", "1envelope"),
    ] {
        let mut m = map.clone();
        m.get_mut(key).unwrap().expression = expression.into();
        let mut b = row(&rows, key).clone();
        b.expression = expression.into();
        let all = with(&rows, b);
        let t = edit(key, |mut cols| {
            if !receiver.is_empty() {
                cols[2] = receiver.into();
            }
            cols
        });
        assert!(
            !field_origins::validate_with(row(&all, key), &all, &m, &t),
            "{expression}"
        );
    }
    // A writer census row of another kind or symbol, even with a reviewed bounds row, is refused.
    for (kind, symbol) in [
        ("assignment", "maximum_request_bytes"),
        ("keyword", "timeout_s"),
    ] {
        let mut m = map.clone();
        m.get_mut(REQ_869).unwrap().kind = kind.into();
        m.get_mut(REQ_869).unwrap().symbol = symbol.into();
        assert!(!ok_with(&rows, &m, key, table), "{kind} {symbol}");
    }
    // Two writers in one constructor span, or a writer outside every span, break the one-to-one cover.
    let moved = {
        let mut m = map.clone();
        m.get_mut(REQ_2649).unwrap().path = CC.into();
        m
    };
    assert!(!ok_with(&rows, &moved, key, table));
    let in_function = {
        let mut m = map.clone();
        m.get_mut(REQ_2649).unwrap().line = m[REQ_869].line;
        m
    };
    assert!(!ok_with(&rows, &in_function, key, table));
    // The binding span's writer must live in the read's own scope.
    let other_scope = {
        let mut m = map.clone();
        m.get_mut(REQ_869).unwrap().scope = "LiveRouterPort.elsewhere".into();
        m
    };
    assert!(!ok_with(&rows, &other_scope, key, table));
}

/// A candidate map in which the census rows `edits` are changed (key -> change).
fn tweak(
    map: &BTreeMap<String, Candidate>,
    key: &str,
    f: impl Fn(&mut Candidate),
) -> BTreeMap<String, Candidate> {
    let mut m = map.clone();
    f(m.get_mut(key).unwrap());
    m
}

#[test]
fn the_census_row_is_compared_with_the_reviewed_row_column_by_column() {
    let (rows, map) = world();
    for (key, ..) in CASES {
        type Edit = Box<dyn Fn(&mut Candidate)>;
        let edits: Vec<(&str, Edit)> = vec![
            ("path", Box::new(|c| c.path = "elsewhere.py".into())),
            ("line", Box::new(|c| c.line += 1)),
            ("column", Box::new(|c| c.column += 1)),
            ("scope", Box::new(|c| c.scope = "elsewhere".into())),
            ("symbol", Box::new(|c| c.symbol = "elsewhere".into())),
            (
                "expression",
                Box::new(|c| c.expression = "envelope.maximum_other".into()),
            ),
        ];
        for (name, f) in &edits {
            let m = tweak(&map, key, f);
            assert!(
                !ok_with(&rows, &m, key, field_origins::TABLE),
                "{key}: {name}"
            );
        }
    }
}

#[test]
fn an_unknown_rule_is_refused_even_when_row_and_table_agree_on_it() {
    let (rows, map) = world();
    let key = CASES[0].0;
    let mut b = row(&rows, key).clone();
    b.review = "field-origin:other-rule; x".into();
    let all = with(&rows, b);
    let t = edit(key, |mut c| {
        c[1] = "other-rule".into();
        c
    });
    assert!(!field_origins::validate_with(
        row(&all, key),
        &all,
        &map,
        &t
    ));
}

#[test]
fn a_dotted_field_is_refused_even_when_census_row_table_and_writers_agree() {
    let (rows, map) = world();
    let key = CASES[0].0;
    let mut m = map.clone();
    m.get_mut(key).unwrap().expression = "envelope.sub.maximum_request_bytes".into();
    for w in [REQ_2649, REQ_869] {
        m.get_mut(w).unwrap().symbol = "sub.maximum_request_bytes".into();
    }
    let mut b = row(&rows, key).clone();
    b.expression = "envelope.sub.maximum_request_bytes".into();
    let all = with(&rows, b);
    let t = edit(key, |mut c| {
        c[5] = "sub.maximum_request_bytes".into();
        c
    });
    assert!(!field_origins::validate_with(row(&all, key), &all, &m, &t));
}

#[test]
fn only_a_rebinding_in_the_reads_own_file_and_scope_is_a_second_root() {
    let (rows, map) = world();
    let key = CASES[0].0;
    let c = &map[key];
    let mut m = map.clone();
    m.insert(
        "forged".into(),
        Candidate {
            key: "forged".into(),
            path: "elsewhere.py".into(),
            line: 1,
            column: 1,
            scope: c.scope.clone(),
            symbol: "envelope".into(),
            expression: "x".into(),
            kind: "assignment".into(),
            source: "x".into(),
        },
    );
    assert!(ok_with(&rows, &m, key, field_origins::TABLE));
}

#[test]
fn writer_rows_must_be_reviewed_in_the_reads_own_unit() {
    let (rows, map) = world();
    let key = CASES[0].0;
    // The read and both writers claim milliseconds while the registry ids are byte limits.
    let mut all = rows.clone();
    for k in [key, REQ_2649, REQ_869] {
        let i = all.iter().position(|r| r.key == k).unwrap();
        all[i].unit = "milliseconds".into();
    }
    assert!(!field_origins::validate_with(
        row(&all, key),
        &all,
        &map,
        field_origins::TABLE
    ));
}

#[test]
fn the_pinned_span_geometry_is_checked_edge_by_edge() {
    let (rows, map) = world();
    let key = CASES[0].0;
    let line = map[key].line;
    let t = field_origins::TABLE;
    let spans = |f: &dyn Fn(&mut Vec<String>)| {
        edit(key, |mut c| {
            c[7] = respan(&c[7], |mut s| {
                f(&mut s);
                s
            });
            c
        })
    };
    let relocate = |span: &str, label: &str, path: &str, first: usize, last: usize| {
        let rest = span.split_once(" @ ").unwrap().1;
        format!("{label} {path}:{first}-{last} @ {rest}")
    };
    // field span reversed but inside the class: only the first <= last rule refuses it
    let reversed = spans(&|s| s[1] = relocate(&s[1], "field", CC, 168, 167));
    assert!(!ok_with(&rows, &map, key, &reversed), "reversed span");
    // field span below the class start / above its end
    let below = spans(&|s| s[1] = relocate(&s[1], "field", CC, 100, 100));
    assert!(!ok_with(&rows, &map, key, &below), "field below class");
    // function starting after the binding statement
    let late_function = spans(&|s| s[2] = relocate(&s[2], "function", RP, 861, 934));
    assert!(
        !ok_with(&rows, &map, key, &late_function),
        "function starts inside the binding"
    );
    // binding statement ending on the read line
    let late_bind = spans(&|s| s[3] = relocate(&s[3], "bind", RP, 860, line));
    assert!(
        !ok_with(&rows, &map, key, &late_bind),
        "binding reaches the read"
    );
    // binding span in another file that still holds a same-scope writer
    let moved = tweak(&map, REQ_869, |w| w.path = CC.into());
    let other_file_bind = spans(&|s| s[3] = relocate(&s[3], "bind", CC, 860, 871));
    assert!(
        !ok_with(&rows, &moved, key, &other_file_bind),
        "binding in another file"
    );
    // a constructor span in ANOTHER file may overlap the function's line numbers
    let moved_ctor = tweak(&map, REQ_2649, |w| {
        w.path = CC.into();
        w.line = 748;
    });
    let overlapping_ctor = spans(&|s| s[4] = relocate(&s[4], "ctor", CC, 745, 750));
    assert!(
        ok_with(&rows, &moved_ctor, key, &overlapping_ctor),
        "ctor elsewhere"
    );
    // ... but not one in the function's own file
    let same_file = tweak(&map, REQ_2649, |w| w.line = 748);
    let inside = spans(&|s| s[4] = relocate(&s[4], "ctor", RP, 745, 750));
    assert!(
        !ok_with(&rows, &same_file, key, &inside),
        "ctor inside the function"
    );
    // a hash with a non-hex letter, an unrelated row with empty columns, an extra column
    let bad_hex = edit(key, |mut c| {
        let at = c[7].rfind("exact-span-sha256=").unwrap() + "exact-span-sha256=".len();
        c[7].replace_range(at..at + 1, "g");
        c
    });
    assert!(!ok_with(&rows, &map, key, &bad_hex), "non-hex hash");
    let unrelated = format!("{t}other\t\t\t\t\t\t\t\n");
    assert!(
        !ok_with(&rows, &map, key, &unrelated),
        "unrelated malformed row"
    );
    let wide = edit(key, |mut c| {
        c.push("extra".into());
        c
    });
    assert!(!ok_with(&rows, &map, key, &wide), "nine columns");
}

#[test]
fn the_writer_order_and_the_span_count_are_checked_when_everything_else_is_consistent() {
    let (rows, map) = world();
    let key = CASES[0].0;
    // Writers swapped in the table AND the relation reversed in the row: only the ascending-order rule refuses.
    let swapped = edit(key, |mut c| {
        c[6] = format!("{REQ_869};{REQ_2649}");
        c
    });
    let mut b = row(&rows, key).clone();
    b.relation = format!("{ID_REQ_869}+{ID_REQ_2649}");
    let all = with(&rows, b);
    assert!(!field_origins::validate_with(
        row(&all, key),
        &all,
        &map,
        &swapped
    ));
    // Both writers inside the binding span and the constructor span dropped: the span count refuses.
    let one_call = tweak(&map, REQ_2649, |w| w.line = 868);
    let dropped = edit(key, |mut c| {
        c[7] = respan(&c[7], |mut s| {
            s.pop();
            s
        });
        c
    });
    assert!(!ok_with(&rows, &one_call, key, &dropped));
}

#[test]
fn three_constructor_calls_derive_in_key_order_and_double_cover_is_refused() {
    let (rows, mut map) = world();
    let key = CASES[0].0;
    // A third, synthetic constructor keyword row (reviewed with the same registry id) outside the function.
    let extra_key = "mycelium_live/router_port.py:3000:1:keyword:maximum_request_bytes";
    map.insert(
        extra_key.into(),
        Candidate {
            key: extra_key.into(),
            path: RP.into(),
            line: 3000,
            column: 1,
            scope: "LiveRouterPort.other".into(),
            symbol: "maximum_request_bytes".into(),
            expression: "131072".into(),
            kind: "keyword".into(),
            source: "maximum_request_bytes=131_072,".into(),
        },
    );
    let mut writer = row(&rows, REQ_869).clone();
    writer.key = extra_key.into();
    writer.line = 3000;
    writer.column = 1;
    writer.scope = "LiveRouterPort.other".into();
    let mut all = rows.clone();
    all.push(writer);
    let i = all.iter().position(|r| r.key == key).unwrap();
    all[i].relation = format!("{ID_REQ_2649}+{ID_REQ_869}+{ID_REQ_869}");
    let table = |ctors: [(usize, usize); 2]| {
        edit(key, |mut c| {
            c[6] = format!("{REQ_2649};{extra_key};{REQ_869}");
            c[7] = respan(&c[7], |mut s| {
                s.truncate(4);
                let template = s[3].clone();
                for (first, last) in ctors {
                    let rest = template.split_once(" @ ").unwrap().1;
                    s.push(format!("ctor {RP}:{first}-{last} @ {rest}"));
                }
                s
            });
            c
        })
    };
    let good = table([(2640, 2651), (2990, 3010)]);
    assert!(field_origins::validate_with(
        row(&all, key),
        &all,
        &map,
        &good
    ));
    // The same writer covered by both constructor spans, the third covered by none.
    let doubled = table([(2640, 2651), (2645, 2655)]);
    assert!(!field_origins::validate_with(
        row(&all, key),
        &all,
        &map,
        &doubled
    ));
    // One span holding two writers.
    let merged = table([(2640, 3010), (2990, 3010)]);
    assert!(!field_origins::validate_with(
        row(&all, key),
        &all,
        &map,
        &merged
    ));
}

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

fn oracle_grep(args: &[&str]) -> Vec<String> {
    let oracle = oracle_blobs::oracle_dir_or_panic();
    let out = std::process::Command::new("git")
        .arg("-C")
        .arg(oracle)
        .args(["grep", "-n"])
        .args(args)
        .output()
        .unwrap();
    String::from_utf8(out.stdout)
        .unwrap()
        .lines()
        .map(String::from)
        .collect()
}

fn span_text(text: &str, first: usize, last: usize) -> Vec<&str> {
    text.split('\n')
        .skip(first - 1)
        .take(last - first + 1)
        .collect()
}

fn mentions(line: &str, word: &str) -> Vec<usize> {
    let bytes = line.as_bytes();
    let is_word = |b: u8| b.is_ascii_alphanumeric() || b == b'_';
    line.match_indices(word)
        .filter(|(i, _)| {
            (*i == 0 || !is_word(bytes[i - 1]))
                && bytes.get(i + word.len()).is_none_or(|b| !is_word(*b))
        })
        .map(|(i, _)| i)
        .collect()
}

#[test]
fn the_pinned_spans_hash_to_the_oracle_text() {
    use sha2::{Digest, Sha256};
    let mut checked = 0;
    for line in field_origins::TABLE.lines().skip(1) {
        let f: Vec<&str> = line.split('\t').collect();
        for span in f[7].split(" | ") {
            let (label, rest) = span.split_once(' ').unwrap();
            let (location, rest) = rest.split_once(" @ ").unwrap();
            let hash = rest.split_once("exact-span-sha256=").unwrap().1;
            let (path, lines) = location.rsplit_once(':').unwrap();
            let (a, b) = lines.split_once('-').unwrap();
            let (a, b): (usize, usize) = (a.parse().unwrap(), b.parse().unwrap());
            let text = oracle_text(path);
            let got: String = Sha256::digest(span_text(&text, a, b).join("\n").as_bytes())
                .iter()
                .map(|x| format!("{x:02x}"))
                .collect();
            assert_eq!(got, hash, "{} {label} {path}:{a}-{b}", f[0]);
            checked += 1;
        }
    }
    assert_eq!(
        checked, 46,
        "4 entries x (class, field, function, bind, ctor) + 5 x (class, method, store) + the lease read (class, method, 3 stores)"
    );
}

#[test]
fn the_oracle_text_establishes_the_negative_facts_the_table_pins() {
    let (controller, port) = (oracle_text(CC), oracle_text(RP));
    // Class: a frozen dataclass without bases; both fields are annotation-only (no default).
    let class = span_text(&controller, 155, 185);
    assert_eq!(class[0].trim(), "@dataclass(frozen=True, slots=True)");
    assert_eq!(class[1].trim(), "class CommandEnvelope:");
    assert_eq!(class[12].trim(), "maximum_request_bytes: int");
    assert_eq!(class[13].trim(), "maximum_response_bytes: int");
    assert_eq!(
        span_text(&controller, 167, 167)[0].trim(),
        "maximum_request_bytes: int"
    );
    assert_eq!(
        span_text(&controller, 168, 168)[0].trim(),
        "maximum_response_bytes: int"
    );
    // The only methods are `__post_init__`; it validates and assigns nothing.
    let defs: Vec<&str> = class
        .iter()
        .copied()
        .filter(|l| l.trim().starts_with("def "))
        .collect();
    assert_eq!(defs, ["    def __post_init__(self) -> None:"]);
    assert!(
        class
            .iter()
            .all(|l| !l.contains("self.maximum_request_bytes =")
                && !l.contains("object.__setattr__"))
    );
    // Every non-test mention of the class: its definition, annotations, the export and router_port's import
    // and two constructor calls. A subclass, alias or third construction would show up here.
    let mentions_of_class = oracle_grep(&["CommandEnvelope", ORACLE, "--", "*.py", ":!tests"]);
    let want = [
        format!("{ORACLE}:{CC}:156:class CommandEnvelope:"),
        format!("{ORACLE}:{CC}:265:    envelope: CommandEnvelope"),
        format!(
            "{ORACLE}:{CC}:337:    def register(self, envelope: CommandEnvelope) -> MutationResult:"
        ),
        format!("{ORACLE}:{CC}:946:    \"CommandEnvelope\","),
        format!("{ORACLE}:{RP}:29:    CommandEnvelope,"),
        format!("{ORACLE}:{RP}:860:            envelope = CommandEnvelope("),
        format!("{ORACLE}:{RP}:2640:        envelope = CommandEnvelope("),
    ];
    assert_eq!(mentions_of_class, want);
    // router_port.py is the only module that imports command_controller; neither file rewrites attributes
    // dynamically (`setattr`, `__dict__`, `vars(`) or copies an envelope with `replace`.
    let importers = oracle_grep(&[
        "-E",
        r"(from|import) .*command_controller",
        ORACLE,
        "--",
        "*.py",
        ":!tests",
    ]);
    assert_eq!(
        importers,
        [format!(
            "{ORACLE}:{RP}:25:from .command_controller import ("
        )]
    );
    for word in ["setattr", "__dict__", "vars(", "replace(envelope", "copy("] {
        for path in [CC, RP] {
            let hits = oracle_grep(&["-F", word, ORACLE, "--", path]);
            assert!(hits.is_empty(), "{path} {word}: {hits:?}");
        }
    }
    // Both constructor calls are keyword-only and bind `envelope` in the function that reads it; the function
    // never rebinds it and has no nested definition.
    for (first, last, fn_first, fn_last, reads) in [
        (860usize, 871usize, 742usize, 934usize, [896usize, 897usize]),
        (2640, 2651, 2598, 2680, [2664, 2665]),
    ] {
        let call = span_text(&port, first, last);
        assert!(
            call[0].trim() == "envelope = CommandEnvelope(" && call[call.len() - 1].trim() == ")"
        );
        for arg in &call[1..call.len() - 1] {
            let arg = arg.trim();
            let (name, value) = arg.split_once('=').unwrap();
            assert!(
                name.bytes().all(|b| b.is_ascii_lowercase() || b == b'_'),
                "{arg}"
            );
            assert!(!value.is_empty() && !arg.starts_with('*'));
        }
        let function = span_text(&port, fn_first, fn_last);
        for (offset, text) in function.iter().enumerate() {
            let at = fn_first + offset;
            let hits = mentions(text, "envelope");
            if at == first {
                assert_eq!(hits.len(), 1);
            } else if (first..=last).contains(&at) {
                assert!(hits.is_empty(), "{at}: {text}");
            } else {
                for i in hits {
                    // Only attribute reads and call-argument uses, never a binding form.
                    let after = &text[i + "envelope".len()..];
                    assert!(
                        after.starts_with('.') || after.starts_with(')') || after.starts_with(','),
                        "{at}: {text}"
                    );
                    if let Some(attribute) = after.strip_prefix('.') {
                        let name_end = attribute
                            .find(|ch: char| !(ch.is_ascii_alphanumeric() || ch == '_'))
                            .unwrap_or(attribute.len());
                        let next = attribute[name_end..].trim_start();
                        assert!(
                            !next.starts_with('=') || next.starts_with("=="),
                            "{at}: attribute store {text}"
                        );
                    }
                }
            }
            if at != fn_first {
                let t = text.trim_start();
                assert!(
                    !(t.starts_with("def ")
                        || t.starts_with("async def ")
                        || t.starts_with("lambda")
                        || t.starts_with("class ")),
                    "{at}: nested definition {t}"
                );
            }
            for banned in [
                "global envelope",
                "nonlocal envelope",
                "del envelope",
                "as envelope",
                "for envelope",
            ] {
                assert!(!text.contains(banned), "{at}: {text}");
            }
        }
        for read in reads {
            assert!(
                span_text(&port, read, read)[0].contains("envelope.maximum_"),
                "{read}"
            );
        }
    }
    // The writer rows are the keyword lines of the two calls.
    for (_, _, field, writers, _) in CASES {
        for (key, _) in writers {
            let line: usize = key.split(':').nth(1).unwrap().parse().unwrap();
            assert!(
                span_text(&port, line, line)[0]
                    .trim()
                    .starts_with(&format!("{field}=")),
                "{key}"
            );
        }
    }
}

#[test]
fn the_pending_count_is_pinned() {
    let rows = checked_in().unwrap();
    let reviewed: BTreeSet<_> = rows.iter().map(|r| r.key.clone()).collect();
    let pending = candidate_keys().unwrap().difference(&reviewed).count();
    assert_eq!(
        pending,
        1299 + r29_retry_admission::keys().len() + r32_verifier_admission::keys().len()
    ); // exact new manual review is outside the bounds-only view // R17 +12 tuple-floor operands; manual reviews separate. // R16 +8 census operands; manual reviews counted separately. // +570 RU-051 keyword operands. // 726 - 4 CommandEnvelope reads (slice 1) - 2 self-attribute reads (slice 2) - 2 OwnerTokenPath forwards (slice 3) - 1 EntryAdmissionPolicy forward (inventory_forward_callers) - 1 self-attribute read (slice 4) - 6 slice 5 (2 self-clamps, 1 lease read, 3 lease constructor forwards) - 1 slice 6 float() forward - 2 slice 6 self-attribute reads - 11 round-12 validated-document-field rows (inventory_validated_document_field) - 4 round-12 RU-033 propagated source-unbounded rows (inventory_unbounded_propagation) + 8 RU-044 reverted rows + 3 RU-044 discovery rows (round 13, inventory_ru044_revert) + 6 round-14 census rows (inventory_discovery_extension_r4)
}

// ---------------------------------------------------------------------------------------------------
// Slice 2: `self.attr` reads (rule `self-attribute`). A read `self.field` in a method of class `C` is one of the
// values the stores `self.field = ...` of `C` carry, when the class is closed: every store is a reviewed census
// assignment row (the census lists them all, so completeness is census-visible), `self` is the method's first
// parameter and is never rebound, there is no subclass and no attribute write from outside, no dynamic
// attribute write on its instances. The pinned spans are class, method and one `store` per writer.
// ---------------------------------------------------------------------------------------------------

fn span_of(reference: &str, index: usize) -> (String, usize, usize) {
    let span = reference.split(" | ").nth(index).unwrap();
    let location = span.split_once(' ').unwrap().1.split_once(" @ ").unwrap().0;
    let (path, lines) = location.rsplit_once(':').unwrap();
    let (a, b) = lines.split_once('-').unwrap();
    (path.into(), a.parse().unwrap(), b.parse().unwrap())
}

fn reference_of(key: &str) -> String {
    field_origins::TABLE
        .lines()
        .find(|l| l.starts_with(key))
        .unwrap()
        .split('\t')
        .nth(7)
        .unwrap()
        .to_string()
}

/// `reference` with span `index` moved to `path:first-last` (label and hash kept).
fn move_span(reference: &str, index: usize, path: &str, first: usize, last: usize) -> String {
    respan(reference, |mut s| {
        let (label, rest) = s[index].split_once(' ').unwrap();
        let tail = rest.split_once(" @ ").unwrap().1;
        s[index] = format!("{label} {path}:{first}-{last} @ {tail}");
        s
    })
}

#[test]
fn two_self_attribute_reads_derive_from_the_reviewed_stores_of_their_class() {
    let (rows, map) = world();
    let reviewed: BTreeSet<&str> = rows
        .iter()
        .filter(|r| {
            r.review.starts_with("field-origin:self-attribute; ")
                || r.review.starts_with("field-origin:self-clamp; ")
        })
        .map(|r| r.key.as_str())
        .collect();
    assert_eq!(
        reviewed,
        SELF_CASES
            .iter()
            .map(|c| c.0)
            .chain([LEASE_READ])
            .collect::<BTreeSet<_>>()
    );
    for (key, path, class, method, field, writer, line, relation) in SELF_CASES {
        let r = row(&rows, key);
        assert_eq!(r.path, path);
        assert_eq!(r.scope, format!("{class}.{method}"));
        // The two clamp rows read the attribute as the first operand of `min(.., lease)`.
        if CLAMP_KEYS.contains(&key) {
            assert_eq!(r.expression, format!("min(self.{field}, lease)"));
        } else {
            assert_eq!(r.expression, format!("self.{field}"));
        }
        assert_eq!(r.disposition, "derived", "{key}");
        assert!(r.budget.is_empty() && r.value.is_none(), "{key}");
        assert_eq!(r.unit, "milliseconds", "{key}");
        assert_eq!(r.relation, relation, "{key}");
        assert!(field_origins::validate(r, &rows, &map), "{key}");
        // The writer is the one store of the class: a reviewed derived row with the same relation.
        let w = row(&rows, writer);
        assert_eq!(
            (w.disposition.as_str(), w.relation.as_str()),
            ("derived", relation)
        );
        assert_eq!(map[writer].kind, "assignment");
        assert_eq!(map[writer].symbol, format!("self.{field}"));
        assert_eq!(map[writer].line, line);
        assert_eq!(map[writer].scope, format!("{class}.__init__"));
    }
    // Independent literals: the registry values of the two ids (seconds in the oracle, milliseconds here).
    let ms = |id: &str| match BudgetId::from_name(id).unwrap().entry().value {
        Value::Duration(span) => span.millis(),
        other => panic!("{id}: {other:?}"),
    };
    assert_eq!(ms("PyClockMyceliumA13MacosWorkflowL493C8"), 1_800_000);
    assert_eq!(
        ms("PyPhysicalInferenceNodeParseArgsCommandTimeoutL6667"),
        30_000
    );
    assert!(validate(&rows).is_ok());
}

#[test]
fn each_self_attribute_table_check_is_refused_when_broken() {
    let (rows, map) = world();
    for (key, path, class, method, field, writer, _, _) in SELF_CASES {
        assert!(ok_with(&rows, &map, key, field_origins::TABLE), "{key}");
        let reference = reference_of(key);
        let (_, cfirst, clast) = span_of(&reference, 0);
        let (_, mfirst, mlast) = span_of(&reference, 1);
        let read = map[key].line;
        let t = field_origins::TABLE;
        let other_path = "elsewhere.py";
        let table_edits: Vec<(&str, String)> = vec![
            (
                "rule",
                edit(key, |mut c| {
                    c[1] = "local-constructor".into();
                    c
                }),
            ),
            (
                "receiver",
                edit(key, |mut c| {
                    c[2] = "this".into();
                    c
                }),
            ),
            (
                "class-file",
                edit(key, |mut c| {
                    c[3] = other_path.into();
                    c
                }),
            ),
            (
                "class",
                edit(key, |mut c| {
                    c[4] = "Other".into();
                    c
                }),
            ),
            (
                "class-qualified",
                edit(key, |mut c| {
                    c[4] = format!("{class}.{method}");
                    c
                }),
            ),
            (
                "field",
                edit(key, |mut c| {
                    c[5] = "other".into();
                    c
                }),
            ),
            (
                "writers-unknown",
                edit(key, |mut c| {
                    c[6] = format!("{writer}x");
                    c
                }),
            ),
            (
                "writers-the-read",
                edit(key, |mut c| {
                    c[6] = key.into();
                    c
                }),
            ),
            (
                "writers-keyword",
                edit(key, |mut c| {
                    c[6] = REQ_869.into();
                    c
                }),
            ),
            (
                "writers-duplicate",
                edit(key, |mut c| {
                    c[6] = format!("{writer};{writer}");
                    c
                }),
            ),
            (
                "ref-drop-class",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        s.remove(0);
                        s
                    });
                    c
                }),
            ),
            (
                "ref-drop-store",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        s.pop();
                        s
                    });
                    c
                }),
            ),
            (
                "ref-extra-store",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        let last = s.last().unwrap().clone();
                        s.push(last);
                        s
                    });
                    c
                }),
            ),
            (
                "ref-labels-swapped",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        s.swap(0, 1);
                        s
                    });
                    c
                }),
            ),
            (
                "ref-store-as-ctor",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        s[2] = s[2].replacen("store ", "ctor ", 1);
                        s
                    });
                    c
                }),
            ),
            (
                "ref-method-as-bind",
                edit(key, |mut c| {
                    c[7] = respan(&c[7], |mut s| {
                        s[1] = s[1].replacen("method ", "bind ", 1);
                        s
                    });
                    c
                }),
            ),
            (
                "ref-oracle-short",
                edit(key, |mut c| {
                    c[7] = c[7].replacen(ORACLE, "f4e366e9", 1);
                    c
                }),
            ),
            (
                "ref-hash-short",
                edit(key, |mut c| {
                    c[7].pop();
                    c
                }),
            ),
            (
                "ref-trailing-text",
                edit(key, |mut c| {
                    c[7].push_str(" x");
                    c
                }),
            ),
            (
                "ref-class-path",
                edit(key, |mut c| {
                    c[7] = move_span(&c[7], 0, other_path, cfirst, clast);
                    c
                }),
            ),
            (
                "ref-method-path",
                edit(key, |mut c| {
                    c[7] = move_span(&c[7], 1, other_path, mfirst, mlast);
                    c
                }),
            ),
            (
                "ref-store-path",
                edit(key, |mut c| {
                    c[7] = move_span(&c[7], 2, other_path, map[writer].line, map[writer].line);
                    c
                }),
            ),
            (
                "ref-method-before-class",
                edit(key, |mut c| {
                    c[7] = move_span(&c[7], 1, path, 1, read);
                    c
                }),
            ),
            (
                "ref-method-past-class",
                edit(key, |mut c| {
                    c[7] = move_span(&c[7], 1, path, read, clast + 100);
                    c
                }),
            ),
            (
                "ref-method-ends-before-read",
                edit(key, |mut c| {
                    c[7] = move_span(&c[7], 1, path, read - 5, read - 1);
                    c
                }),
            ),
            (
                "ref-method-starts-after-read",
                edit(key, |mut c| {
                    c[7] = move_span(&c[7], 1, path, read + 1, read + 5);
                    c
                }),
            ),
            (
                "ref-store-before-class",
                edit(key, |mut c| {
                    c[7] = move_span(&c[7], 2, path, 1, map[writer].line);
                    c
                }),
            ),
            (
                "ref-store-past-class",
                edit(key, |mut c| {
                    c[7] = move_span(&c[7], 2, path, map[writer].line, clast + 50);
                    c
                }),
            ),
            (
                "ref-store-without-writer",
                edit(key, |mut c| {
                    c[7] = move_span(&c[7], 2, path, cfirst, cfirst + 1);
                    c
                }),
            ),
            (
                "no-entry",
                t.lines()
                    .filter(|l| l.split('\t').next() != Some(key))
                    .collect::<Vec<_>>()
                    .join("\n")
                    + "\n",
            ),
        ];
        for (name, table) in &table_edits {
            assert!(!ok_with(&rows, &map, key, table), "{key}: {name}");
        }
        let _ = (path, field);
    }
}

#[test]
fn each_self_attribute_bounds_row_column_and_writer_row_is_checked() {
    let (rows, map) = world();
    type Mutation = Box<dyn Fn(&mut Bound)>;
    for (key, _, _, _, _, writer, _, relation) in SELF_CASES {
        let base = row(&rows, key).clone();
        let muts: Vec<(&str, Mutation)> = vec![
            (
                "disposition",
                Box::new(|b| b.disposition = "reviewed".into()),
            ),
            ("budget", Box::new(|b| b.budget = relation.into())),
            ("value", Box::new(|b| b.value = Some(1))),
            ("unit-bytes", Box::new(|b| b.unit = "bytes".into())),
            ("relation-empty", Box::new(|b| b.relation = String::new())),
            (
                "relation-other",
                Box::new(|b| b.relation = ID_REQ_869.into()),
            ),
            (
                "relation-doubled",
                Box::new(|b| b.relation = format!("{0}+{0}", b.relation)),
            ),
            (
                "review-rule",
                Box::new(|b| b.review = "field-origin:other-rule; x".into()),
            ),
            (
                "review-prefix",
                Box::new(|b| b.review = "forward-callers:self-attribute; x".into()),
            ),
            ("line", Box::new(|b| b.line += 1)),
            ("column", Box::new(|b| b.column += 1)),
            ("scope", Box::new(|b| b.scope = "other".into())),
            ("symbol", Box::new(|b| b.symbol = "other".into())),
            (
                "expression",
                Box::new(|b| b.expression = "self.other".into()),
            ),
            ("path", Box::new(|b| b.path = "other.py".into())),
        ];
        for (name, m) in &muts {
            let mut b = base.clone();
            m(&mut b);
            let all = with(&rows, b);
            assert!(
                !field_origins::validate_with(row(&all, key), &all, &map, field_origins::TABLE),
                "{key}: {name}"
            );
        }
        let wb = row(&rows, writer).clone();
        let wmuts: Vec<(&str, Mutation)> = vec![
            (
                "writer-disposition",
                Box::new(|b| {
                    b.budget = String::new();
                    b.disposition = "not-a-bound".into();
                }),
            ),
            ("writer-unit", Box::new(|b| b.unit = "entries".into())),
            (
                "writer-relation-unit",
                Box::new(|b| b.relation = ID_REQ_869.into()),
            ),
            (
                "writer-relation-empty",
                Box::new(|b| b.relation = String::new()),
            ),
        ];
        for (name, m) in &wmuts {
            let mut b = wb.clone();
            m(&mut b);
            let all = with(&rows, b);
            assert!(
                !field_origins::validate_with(row(&all, key), &all, &map, field_origins::TABLE),
                "{key}: {name}"
            );
        }
        let without: Vec<Bound> = rows.iter().filter(|r| r.key != writer).cloned().collect();
        assert!(
            !field_origins::validate_with(row(&without, key), &without, &map, field_origins::TABLE),
            "{key}: writer row missing"
        );
    }
}

#[test]
fn self_attribute_census_facts_refuse_a_consistently_forged_world() {
    let (rows, map) = world();
    let (key, path, class, method, field, writer, _, _) = SELF_CASES[0];
    let table = field_origins::TABLE;
    assert!(ok_with(&rows, &map, key, table));
    let insert = |kind: &str, scope: String, symbol: String, p: &str| {
        let mut m = map.clone();
        m.insert(
            "forged".into(),
            Candidate {
                key: "forged".into(),
                path: p.into(),
                line: 1,
                column: 1,
                scope,
                symbol,
                expression: "x".into(),
                kind: kind.into(),
                source: "x".into(),
            },
        );
        m
    };
    let scope = format!("{class}.{method}");
    // `self` rebound in the method (assignment or parameter row): a second root.
    for kind in ["assignment", "parameter"] {
        assert!(
            !ok_with(
                &rows,
                &insert(kind, scope.clone(), "self".into(), path),
                key,
                table
            ),
            "{kind}"
        );
    }
    assert!(ok_with(
        &rows,
        &insert("assignment", "Other.m".into(), "self".into(), path),
        key,
        table
    ));
    assert!(ok_with(
        &rows,
        &insert("assignment", scope.clone(), "self".into(), "elsewhere.py"),
        key,
        table
    ));
    // Completeness: another store of the attribute in the class that the table does not list.
    let symbol = format!("self.{field}");
    assert!(!ok_with(
        &rows,
        &insert("assignment", format!("{class}.other"), symbol.clone(), path),
        key,
        table
    ));
    assert!(!ok_with(
        &rows,
        &insert(
            "assignment",
            format!("{class}.other.inner"),
            symbol.clone(),
            path
        ),
        key,
        table
    ));
    // ... but a store in another class, another file, or of another kind/attribute is not this class's writer.
    assert!(ok_with(
        &rows,
        &insert("assignment", "Other.__init__".into(), symbol.clone(), path),
        key,
        table
    ));
    assert!(ok_with(
        &rows,
        &insert(
            "assignment",
            format!("{class}.other"),
            symbol.clone(),
            "elsewhere.py"
        ),
        key,
        table
    ));
    assert!(ok_with(
        &rows,
        &insert("keyword", format!("{class}.other"), symbol.clone(), path),
        key,
        table
    ));
    assert!(ok_with(
        &rows,
        &insert(
            "assignment",
            format!("{class}.other"),
            "self.other".into(),
            path
        ),
        key,
        table
    ));
    // The writer census row: wrong kind, symbol, file or scope.
    for (name, f) in [
        (
            "kind",
            Box::new(|c: &mut Candidate| c.kind = "keyword".into()) as Box<dyn Fn(&mut Candidate)>,
        ),
        (
            "symbol",
            Box::new(|c: &mut Candidate| c.symbol = "self.other".into()),
        ),
        (
            "path",
            Box::new(|c: &mut Candidate| c.path = "elsewhere.py".into()),
        ),
        (
            "scope",
            Box::new(|c: &mut Candidate| c.scope = "Other.__init__".into()),
        ),
        (
            "prefix-scope",
            Box::new(|c: &mut Candidate| c.scope = format!("{class}x.__init__")),
        ),
        (
            "nested-scope",
            Box::new(|c: &mut Candidate| c.scope = format!("{class}.__init__.inner")),
        ),
        ("line", Box::new(|c: &mut Candidate| c.line += 1000)),
    ] {
        assert!(
            !ok_with(&rows, &tweak(&map, writer, &f), key, table),
            "writer {name}"
        );
    }
    // The read: receiver other than `self`, a module-level or doubly nested scope, another class.
    let forged = |scope: &str, expression: &str, receiver: &str| {
        let mut m = map.clone();
        m.get_mut(key).unwrap().scope = scope.into();
        m.get_mut(key).unwrap().expression = expression.into();
        let mut b = row(&rows, key).clone();
        b.scope = scope.into();
        b.expression = expression.into();
        let all = with(&rows, b);
        let t = edit(key, |mut c| {
            c[2] = receiver.into();
            c
        });
        field_origins::validate_with(row(&all, key), &all, &m, &t)
    };
    let read = format!("self.{field}");
    assert!(
        forged(&scope, &read, "self"),
        "the control forgery is consistent"
    );
    assert!(
        !forged(&scope, &format!("this.{field}"), "this"),
        "receiver this"
    );
    assert!(!forged(method, &read, "self"), "one-segment scope");
    assert!(
        !forged(&format!("{scope}.inner"), &read, "self"),
        "three-segment scope"
    );
    assert!(
        !forged(&format!("Other.{method}"), &read, "self"),
        "another class"
    );
    assert!(
        !forged(&scope, &format!("self.{field}.x"), "self"),
        "dotted field"
    );
}

#[test]
fn self_attribute_span_geometry_and_a_second_store_are_checked() {
    let (rows, map) = world();
    let (key, path, class, _, field, writer, line, relation) = SELF_CASES[1];
    // A synthetic second store (reviewed, same relation) in another method of the class, with its own span.
    let second = format!("{path}:2300:8:assignment:self.{field}");
    let mut m = map.clone();
    m.insert(
        second.clone(),
        Candidate {
            key: second.clone(),
            path: path.into(),
            line: 2300,
            column: 8,
            scope: format!("{class}.reconfigure"),
            symbol: format!("self.{field}"),
            expression: field.into(),
            kind: "assignment".into(),
            source: "x".into(),
        },
    );
    let mut w = row(&rows, writer).clone();
    w.key = second.clone();
    w.line = 2300;
    w.column = 8;
    w.scope = format!("{class}.reconfigure");
    let mut all = rows.clone();
    all.push(w);
    let i = all.iter().position(|r| r.key == key).unwrap();
    all[i].relation = format!("{relation}+{relation}");
    let reference = reference_of(key);
    let table = |stores: &[(usize, usize)], writers: String| {
        edit(key, |mut c| {
            c[6] = writers.clone();
            c[7] = respan(&c[7], |mut s| {
                s.truncate(2);
                let template = reference.split(" | ").nth(2).unwrap().to_string();
                for &(first, last) in stores {
                    let tail = template.split_once(" @ ").unwrap().1;
                    s.push(format!("store {path}:{first}-{last} @ {tail}"));
                }
                s
            });
            c
        })
    };
    let ordered = format!("{writer};{second}");
    let good = table(&[(line, line), (2300, 2301)], ordered.clone());
    assert!(
        field_origins::validate_with(row(&all, key), &all, &m, &good),
        "two stores derive"
    );
    // Both spans on the same writer, none on the second: refused.
    assert!(!field_origins::validate_with(
        row(&all, key),
        &all,
        &m,
        &table(&[(line, line), (line, line + 1)], ordered.clone())
    ));
    // One span holding both writers (second missing its own): refused.
    assert!(!field_origins::validate_with(
        row(&all, key),
        &all,
        &m,
        &table(&[(line, 2301), (2300, 2301)], ordered.clone())
    ));
    // One store span holding both writers (a single span for two stores): the span count must match.
    assert!(!field_origins::validate_with(
        row(&all, key),
        &all,
        &m,
        &table(&[(line, 2301)], ordered.clone())
    ));
    // A third store span that holds no writer: every store span must hold one.
    assert!(!field_origins::validate_with(
        row(&all, key),
        &all,
        &m,
        &table(
            &[(line, line), (2300, 2301), (line + 1, line + 1)],
            ordered.clone()
        )
    ));
    // Only the class label wrong (the method and store labels intact): refused.
    let relabelled: String = good
        .lines()
        .map(|l| {
            if l.starts_with(key) {
                l.replacen("\tclass ", "\tctor ", 1)
            } else {
                l.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
        + "\n";
    assert_ne!(relabelled, good);
    assert!(!field_origins::validate_with(
        row(&all, key),
        &all,
        &m,
        &relabelled
    ));
    // Writers in descending key order (the relation is the same either way): the ascending-order rule.
    let swapped = table(&[(2300, 2301), (line, line)], format!("{second};{writer}"));
    assert!(!field_origins::validate_with(
        row(&all, key),
        &all,
        &m,
        &swapped
    ));
    // The second store missing from the table: the census-visible completeness rule refuses.
    let mut single = all.clone();
    let i = single.iter().position(|r| r.key == key).unwrap();
    single[i].relation = relation.into();
    assert!(!ok_with(&single, &m, key, field_origins::TABLE));
}

/// In-scope (production) files of the oracle: the first column of `python-scope.tsv`.
fn scoped_files() -> BTreeSet<&'static str> {
    include_str!("../data/python-scope.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect()
}

/// `git grep -nE` hits (`path:line:text`) restricted to scoped files.
fn scoped_hits(pattern: &str) -> Vec<(String, usize, String)> {
    let scope = scoped_files();
    let prefix = format!("{ORACLE}:");
    let hits = oracle_grep(&["-E", pattern, ORACLE, "--", "*.py"]);
    hits.iter()
        .filter_map(|h| {
            let h = h.strip_prefix(&prefix)?;
            let (path, rest) = h.split_once(':')?;
            let (line, text) = rest.split_once(':')?;
            scope.contains(path).then(|| {
                (
                    path.to_string(),
                    line.parse().unwrap(),
                    text.trim().to_string(),
                )
            })
        })
        .collect()
}

#[test]
fn the_oracle_text_closes_the_classes_of_the_self_attribute_reads() {
    oracle_blobs::oracle_dir_or_panic();
    // Every store of the attribute outside tests, with the class span it falls in (None = not in the class).
    let expected_stores: [(&str, &[(&str, usize)]); 4] = [
        (
            "deadline",
            &[
                ("mycelium_a13_macos/token_owner.py", 32),
                ("mycelium_router/transports/iroh.py", 2363),
                ("physical_inference_node.py", 560),
                ("physical_inference_node.py", 668),
            ],
        ),
        (
            "command_timeout",
            &[
                ("physical_inference_node.py", 549),
                ("physical_inference_node.py", 2195),
            ],
        ),
        // The lease deadline: the class's own three stores, OwnerTokenPath :44, EntryAdmissionPolicy :57 and
        // ParticipantAdmissionPolicy (token_participants.py:56).
        (
            "_lease_deadline",
            &[
                ("mycelium_a13_macos/token_owner.py", 37),
                ("mycelium_a13_macos/token_owner.py", 94),
                ("mycelium_a13_macos/token_owner.py", 138),
                ("mycelium_live/token_path_owner.py", 44),
                ("mycelium_node/token_admission.py", 57),
                ("mycelium_node/token_participants.py", 56),
            ],
        ),
        // OwnerTokenPath (token_path_owner.py:43), EntryAdmissionPolicy (:56, the pinned class) and
        // ParticipantAdmissionPolicy (token_participants.py:55) each hold their own `_process_deadline`.
        (
            "_process_deadline",
            &[
                ("mycelium_live/token_path_owner.py", 43),
                ("mycelium_node/token_admission.py", 56),
                ("mycelium_node/token_participants.py", 55),
            ],
        ),
    ];
    // Files that mention the class by name (importers, constructors, docstrings) outside tests.
    let mentioned_in: [(&str, &[&str]); 4] = [
        (
            "ManagedRouteTokenOwner",
            &[
                "mycelium_a13_macos/token_owner.py",
                "mycelium_a13_macos/workflow.py",
            ],
        ),
        (
            "PhysicalNodeService",
            &["mycelium_node/process.py", "physical_inference_node.py"],
        ),
        (
            "EntryAdmissionPolicy",
            &[
                "mycelium_live/token_path_owner.py",
                "mycelium_node/token_admission.py",
                "physical_inference_node.py",
            ],
        ),
        ("_PrefillWaitBudget", &["physical_inference_node.py"]),
    ];
    let dynamic = scoped_hits(r"setattr\(|__dict__|vars\(");
    // (read key, path, class, method, field, the lines of the stores in table order)
    type OracleCase = (
        &'static str,
        &'static str,
        &'static str,
        &'static str,
        &'static str,
        Vec<usize>,
    );
    let mut oracle_cases: Vec<OracleCase> = SELF_CASES
        .iter()
        .map(|c| (c.0, c.1, c.2, c.3, c.4, vec![c.6]))
        .collect();
    oracle_cases.push((
        LEASE_READ,
        TOKEN_OWNER,
        "ManagedRouteTokenOwner",
        "prepare",
        "_lease_deadline",
        vec![138, 37, 94],
    ));
    for (key, path, class, method, field, store_lines) in oracle_cases {
        let source = oracle_text(path);
        let reference = reference_of(key);
        let (_, cfirst, clast) = span_of(&reference, 0);
        let (_, mfirst, mlast) = span_of(&reference, 1);
        // Class: plain `class C:` (no bases, no decorator); method: first parameter `self`, no decorator.
        assert_eq!(
            span_text(&source, cfirst, cfirst)[0],
            format!("class {class}:")
        );
        assert!(
            !span_text(&source, cfirst - 1, cfirst - 1)[0]
                .trim_start()
                .starts_with('@')
        );
        // `def method(self` (the signature may continue on the next lines).
        let head = span_text(&source, mfirst, (mfirst + 1).min(mlast))
            .join("")
            .replace(' ', "");
        assert!(head.starts_with(&format!("def{method}(self")), "{head}");
        assert!(
            !span_text(&source, mfirst - 1, mfirst - 1)[0]
                .trim_start()
                .starts_with('@')
        );
        assert!((cfirst..=clast).contains(&mfirst) && mlast <= clast);
        // `self` is never rebound in the method and nothing in the class writes attributes dynamically.
        for (offset, text) in span_text(&source, mfirst, mlast).iter().enumerate() {
            for banned in [
                "self =",
                "global self",
                "nonlocal self",
                "del self",
                "as self",
                "for self",
            ] {
                assert!(!text.contains(banned), "{}: {text}", mfirst + offset);
            }
        }
        for (offset, text) in span_text(&source, cfirst, clast).iter().enumerate() {
            for banned in ["setattr(", "__dict__", "vars(", "object.__setattr__"] {
                assert!(!text.contains(banned), "{}: {text}", cfirst + offset);
            }
        }
        // The store statement and its census row.
        for (i, line) in store_lines.iter().enumerate() {
            let (_, sfirst, slast) = span_of(&reference, 2 + i);
            assert_eq!((sfirst, slast), (*line, *line), "{key}");
            assert!(
                span_text(&source, sfirst, sfirst)[0]
                    .replace(' ', "")
                    .starts_with(&format!("self.{field}=")),
                "{key}"
            );
        }
        assert_eq!(
            reference.split(" | ").count(),
            2 + store_lines.len(),
            "{key}"
        );
        // Attribute stores of that name anywhere in the scoped tree: exactly the pinned list; those inside
        // the class are exactly its writer; the others are another class's `self` or a different record.
        let pattern = format!(r"\.{field}[[:space:]]*([-+*/%|&^@]|//|\*\*|<<|>>)?=[^=]");
        let hits = scoped_hits(&pattern);
        let want = expected_stores.iter().find(|e| e.0 == field).unwrap().1;
        let got: Vec<(&str, usize)> = hits.iter().map(|h| (h.0.as_str(), h.1)).collect();
        assert_eq!(got, want, "stores of .{field}");
        let inside: Vec<usize> = hits
            .iter()
            .filter(|h| h.0 == path && (cfirst..=clast).contains(&h.1))
            .map(|h| h.1)
            .collect();
        let mut live = store_lines.clone();
        live.sort();
        assert_eq!(inside, live, "stores inside {class}");
        // No deletion, no subclass, and the class is named only in the pinned files.
        assert!(scoped_hits(&format!(r"del [^#]*\.{field}\b")).is_empty());
        assert!(scoped_hits(&format!(r"class [[:alnum:]_]+\([^)]*{class}")).is_empty());
        let mut files: Vec<String> = scoped_hits(class).into_iter().map(|h| h.0).collect();
        files.sort();
        files.dedup();
        let want_files = mentioned_in.iter().find(|m| m.0 == class).unwrap().1;
        assert_eq!(files, want_files, "files naming {class}");
        // No dynamic attribute write in any file that names, constructs or holds the class.
        for hit in &dynamic {
            assert!(
                !want_files.contains(&hit.0.as_str()),
                "{class}: dynamic write in {}:{}",
                hit.0,
                hit.1
            );
        }
    }
    // The other stores of `.deadline`/`.command_timeout` belong to `_PrefillWaitBudget` (physical_inference_node.py
    // 494-745: `self.` stores at 549, 560, 668) and to the `_PrefillProgress` record (iroh.py:2363, a local
    // `progress` built three lines above), neither of which is a pinned class.
    let node = oracle_text("physical_inference_node.py");
    assert_eq!(
        span_text(&node, 494, 494)[0].trim(),
        "class _PrefillWaitBudget:"
    );
    let iroh = oracle_text("mycelium_router/transports/iroh.py");
    assert!(
        span_text(&iroh, 2358, 2363)
            .iter()
            .any(|l| l.contains("progress = _PrefillProgress("))
    );
}

// ---------------------------------------------------------------------------------------------------
// Slice 5: `self-clamp` (`self.X = min(self.F, other)` is bounded by the first operand) and the inactive-state
// initialiser of a self-attribute read (`self.X = None` in `__init__`, reviewed `inactive-policy-state`).
// ---------------------------------------------------------------------------------------------------
const TOKEN_OWNER: &str = "mycelium_a13_macos/token_owner.py";
const PYCLOCK: &str = "PyClockMyceliumA13MacosWorkflowL493C8";
const LEASE_READ: &str =
    "mycelium_a13_macos/token_owner.py:186:20:keyword:lease_deadline_monotonic_s";
const LEASE_STORES: [&str; 3] = [
    "mycelium_a13_macos/token_owner.py:138:16:assignment:self._lease_deadline",
    "mycelium_a13_macos/token_owner.py:37:8:assignment:self._lease_deadline",
    "mycelium_a13_macos/token_owner.py:94:12:assignment:self._lease_deadline",
];

#[test]
fn a_read_with_an_inactive_initialiser_derives_from_the_live_stores_only() {
    let (rows, map) = world();
    let r = row(&rows, LEASE_READ);
    assert_eq!(r.disposition, "derived");
    assert!(r.budget.is_empty() && r.value.is_none());
    assert_eq!(r.unit, "milliseconds");
    assert_eq!(r.scope, "ManagedRouteTokenOwner.prepare");
    assert_eq!(r.expression, "self._lease_deadline");
    assert!(r.review.starts_with("field-origin:self-attribute; "));
    // The relation is the ordered `+` of the two live stores (the two clamps), never of the initialiser.
    assert_eq!(r.relation, format!("{PYCLOCK}+{PYCLOCK}"));
    assert!(field_origins::validate(r, &rows, &map));
    // The writer list is the three stores of the class in ascending key order.
    let entry = field_origins::TABLE
        .lines()
        .find(|l| l.starts_with(LEASE_READ))
        .unwrap();
    assert_eq!(entry.split('\t').nth(6).unwrap(), LEASE_STORES.join(";"));
    // Initialiser: the `None` store, an exact inactive state with no value; the live stores are the clamps.
    let init = row(&rows, LEASE_STORES[1]);
    assert_eq!(init.disposition, "not-a-bound");
    assert!(
        init.review
            .starts_with("category:reviewed-inactive-state; ")
    );
    assert!(init.budget.is_empty() && init.value.is_none());
    assert_eq!(map[LEASE_STORES[1]].expression, "None");
    for live in [LEASE_STORES[0], LEASE_STORES[2]] {
        let w = row(&rows, live);
        assert_eq!(
            (w.disposition.as_str(), w.relation.as_str()),
            ("derived", PYCLOCK)
        );
        assert!(w.review.starts_with("field-origin:self-clamp; "));
        assert_eq!(map[live].kind, "assignment");
    }
    assert!(validate(&rows).is_ok());
}

#[test]
fn the_inactive_initialiser_needs_the_exact_category_and_a_live_store() {
    let (rows, map) = world();
    let table = field_origins::TABLE;
    assert!(ok_with(&rows, &map, LEASE_READ, table));
    let init = row(&rows, LEASE_STORES[1]).clone();
    type Edit = Box<dyn Fn(&mut Bound)>;
    let breaks: Vec<(&str, Edit)> = vec![
        (
            "other category",
            Box::new(|b| {
                b.review = b
                    .review
                    .replacen("reviewed-inactive-state", "diagnostic-field", 1)
            }),
        ),
        (
            "category prefix only",
            Box::new(|b| {
                b.review =
                    b.review
                        .replacen("reviewed-inactive-state;", "reviewed-inactive-statex;", 1)
            }),
        ),
        (
            "other disposition",
            Box::new(|b| b.disposition = "source-unbounded".into()),
        ),
        ("budget", Box::new(|b| b.budget = PYCLOCK.into())),
        ("value", Box::new(|b| b.value = Some(1))),
        ("derived", Box::new(|b| b.disposition = "derived".into())),
    ];
    for (name, f) in &breaks {
        let mut b = init.clone();
        f(&mut b);
        let all = with(&rows, b);
        assert!(!ok_with(&all, &map, LEASE_READ, table), "{name}");
    }
    // The initialiser alone is not a value: with only it in the class (and a row relation matching the empty
    // join) the read has nothing to derive from.
    let mut only = rows.clone();
    only.retain(|r| r.key != LEASE_STORES[0] && r.key != LEASE_STORES[2]);
    let i = only.iter().position(|r| r.key == LEASE_READ).unwrap();
    only[i].relation = String::new();
    let mut only_map = map.clone();
    only_map.remove(LEASE_STORES[0]);
    only_map.remove(LEASE_STORES[2]);
    let reference = reference_of(LEASE_READ);
    let kept = respan(&reference, |s| {
        let mut out: Vec<String> = s[..2].to_vec();
        out.push(s.iter().find(|x| x.contains(":37-37 ")).unwrap().clone());
        out
    });
    let only_table = edit(LEASE_READ, |mut c| {
        c[6] = LEASE_STORES[1].into();
        c[7] = kept.clone();
        c
    });
    assert!(
        !field_origins::validate_with(row(&only, LEASE_READ), &only, &only_map, &only_table),
        "an inactive initialiser alone derives nothing"
    );
    // The local-constructor rule never admits an inactive writer.
    let (key, _, _, writers, _) = CASES[0];
    let keep = writers[0].0;
    let drop = writers[1].0;
    let mut forged = rows.clone();
    let i = forged.iter().position(|r| r.key == drop).unwrap();
    forged[i].disposition = "not-a-bound".into();
    forged[i].budget = String::new();
    forged[i].relation = String::new();
    forged[i].unit = "not-applicable".into();
    forged[i].value = None;
    forged[i].review = "category:reviewed-inactive-state; x".into();
    let j = forged.iter().position(|r| r.key == key).unwrap();
    forged[j].relation = if keep == REQ_2649 {
        ID_REQ_2649
    } else {
        ID_REQ_869
    }
    .into();
    assert!(!ok_with(&forged, &map, key, table));
    // ... even when the census text of that writer is `None` as well (the rule, not the text, decides).
    let mut none_map = map.clone();
    none_map.get_mut(drop).unwrap().expression = "None".into();
    assert!(!ok_with(&forged, &none_map, key, table));
    // An inactive store must be a literal `None`: another sentinel text is a value-less store nobody reviewed.
    let mut zero_map = map.clone();
    zero_map.get_mut(LEASE_STORES[1]).unwrap().expression = "0.0".into();
    assert!(!ok_with(&rows, &zero_map, LEASE_READ, table));
    assert!(ok_with(&rows, &map, LEASE_READ, table));
}

/// A world in which the census expression and the reviewed expression of `key` are both `expression`.
fn clamp_world(key: &str, expression: &str) -> (Vec<Bound>, BTreeMap<String, Candidate>) {
    let (rows, map) = world();
    let mut m = map.clone();
    m.get_mut(key).unwrap().expression = expression.into();
    let mut b = row(&rows, key).clone();
    b.expression = expression.into();
    (with(&rows, b), m)
}

#[test]
fn a_clamp_needs_every_store_of_the_clamped_attribute_listed() {
    let (rows, map) = world();
    let table = field_origins::TABLE;
    for key in CLAMP_KEYS {
        assert!(ok_with(&rows, &map, key, table), "{key}");
        let insert = |scope: &str, symbol: &str, kind: &str, path: &str| {
            let mut m = map.clone();
            m.insert(
                "forged".into(),
                Candidate {
                    key: "forged".into(),
                    path: path.into(),
                    line: 1,
                    column: 1,
                    scope: scope.into(),
                    symbol: symbol.into(),
                    expression: "x".into(),
                    kind: kind.into(),
                    source: "x".into(),
                },
            );
            m
        };
        // Another store of `self.deadline` in the class that the table does not list: refused.
        assert!(
            !ok_with(
                &rows,
                &insert(
                    "ManagedRouteTokenOwner.other",
                    "self.deadline",
                    "assignment",
                    TOKEN_OWNER
                ),
                key,
                table
            ),
            "{key}"
        );
        // Stores of other attributes, other classes or other files do not matter.
        for (scope, symbol, kind, path) in [
            (
                "ManagedRouteTokenOwner.other",
                "self.other",
                "assignment",
                TOKEN_OWNER,
            ),
            ("Other.other", "self.deadline", "assignment", TOKEN_OWNER),
            (
                "ManagedRouteTokenOwner.other",
                "self.deadline",
                "assignment",
                "elsewhere.py",
            ),
            (
                "ManagedRouteTokenOwner.other",
                "self.deadline",
                "keyword",
                TOKEN_OWNER,
            ),
        ] {
            assert!(
                ok_with(&rows, &insert(scope, symbol, kind, path), key, table),
                "{key}: {scope} {symbol} {kind} {path}"
            );
        }
    }
}

#[test]
fn the_clamp_expression_must_be_min_of_the_attribute_and_one_other_operand() {
    for key in CLAMP_KEYS {
        let table = field_origins::TABLE;
        let accepted = [
            "min(self.deadline, lease)",
            "min(self.deadline, f(a, b))",
            "min(self.deadline, lease[0])",
            "min(self.deadline, {1: 2}[1])",
            "min(self.deadline, 0.5)",
        ];
        for expression in accepted {
            let (rows, map) = clamp_world(key, expression);
            assert!(ok_with(&rows, &map, key, table), "{key}: {expression}");
        }
        let refused = [
            "min(self.deadline)",
            "min(self.deadline, a, b)",
            "min(self.deadline, a, f(b, c))",
            "min(lease, self.deadline)",
            "max(self.deadline, lease)",
            "min(self.other, lease)",
            "min(self.deadline, )",
            "min(self.deadline,lease)",
            "min(self.deadline , lease)",
            "min (self.deadline, lease)",
            "xmin(self.deadline, lease)",
            "min(self.deadline, lease) + 1",
            "min(self.deadline, lease)+min(x, y)",
            "min(self.deadline, 'a')",
            "min(self.deadline, \"a\")",
            "min(self.deadline, f(a)",
            "min(self.deadline, [a)",
            "min(self.deadline, a))",
            "min(self.deadline.x, lease)",
            "min(self.deadline, lease",
            "min(this.deadline, lease)",
            "min(self.deadline, lease)\n",
            "self.deadline",
        ];
        for expression in refused {
            let (rows, map) = clamp_world(key, expression);
            assert!(!ok_with(&rows, &map, key, table), "{key}: {expression:?}");
        }
    }
}

#[test]
fn the_clamp_statements_call_the_builtin_min_on_the_attribute() {
    let source = oracle_text(TOKEN_OWNER);
    for (key, line) in [(CLAMP_KEYS[0], 94), (CLAMP_KEYS[1], 138)] {
        assert!(key.contains(&format!(":{line}:")));
        let statement = span_text(&source, line, line)[0].replace(' ', "");
        assert!(
            statement.starts_with("self._lease_deadline=min(self.deadline,"),
            "{statement}"
        );
    }
    // `min` is never rebound or imported in the file: no definition, assignment, parameter, import or alias.
    for (number, text) in source.split('\n').enumerate() {
        let t = text.trim_start();
        assert!(!t.starts_with("def min"), "{number}: {text}");
        assert!(!t.starts_with("min ="), "{number}: {text}");
        assert!(!t.starts_with("min="), "{number}: {text}");
        assert!(!text.contains(" as min"), "{number}: {text}");
        assert!(!text.contains("import min"), "{number}: {text}");
        assert!(
            !text.contains(", min,") && !text.contains("(min,"),
            "{number}: {text}"
        );
    }
    // Nothing in the file rebinds `min` through a global statement or a lambda/def parameter either.
    assert!(!source.contains("global min"));
}
