// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:physical_inference_node.py; O:path:scripts/run_transport_probe_sidecar.py.
//! Multi-target binding edges: `NativeSidecarProcess.startup_timeout` has exactly two
//! production callers (`PhysicalNodeService._new_sidecar_process` and
//! `scripts/run_transport_probe_sidecar.py`), so its store and four reads map to both.
use budget_model::inventory::{
    Bound, Candidate, bindings, candidates, checked_in, multi_target, validate,
};
use std::collections::BTreeMap;

const NODE: &str = "physical_inference_node.py";
const PROBE: &str = "scripts/run_transport_probe_sidecar.py";
const STORE: &str = "physical_inference_node.py:1769:8:assignment:self.startup_timeout";
const READS: [&str; 4] = [
    "physical_inference_node.py:1882:12:positional-call:reader.join",
    "physical_inference_node.py:1901:55:keyword:timeout",
    "physical_inference_node.py:1961:42:positional-timeout:select.select.timeout",
    "physical_inference_node.py:1978:44:keyword:timeout",
];
const CALLER_NODE: &str = "physical_inference_node.py:2511:12:keyword:startup_timeout";
const CALLER_PROBE: &str = "scripts/run_transport_probe_sidecar.py:87:8:keyword:startup_timeout";
const RELATION: &str =
    "PyUpperPhysicalInferenceNodeL2511C12+PyScriptsRunTransportProbeSidecarMainStartupTimeoutL87";

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

fn ok(key: &str, edges: &str) -> bool {
    let (rows, map) = world();
    multi_target::validate_with(row(&rows, key), &rows, &map, edges)
}

#[test]
fn startup_timeout_store_and_reads_map_to_both_production_callers() {
    let (rows, map) = world();
    for key in std::iter::once(STORE).chain(READS) {
        let r = row(&rows, key);
        assert_eq!(r.disposition, "derived", "{key}");
        assert!(r.budget.is_empty() && r.value.is_none(), "{key}");
        assert_eq!(r.unit, "milliseconds", "{key}");
        assert_eq!(r.relation, RELATION, "{key}");
        assert!(r.review.starts_with(multi_target::PREFIX), "{key}");
        assert!(multi_target::validate(r, &rows, &map), "{key}");
        // The single-target validator never admits a multi-target row.
        assert!(!bindings::validate(r, &rows, &map), "{key}");
    }
    assert!(validate(&rows).is_ok());
    // Both targets are the reviewed literal rows, each with its own caller span.
    let edges = multi_target::edges_of(multi_target::EDGES, STORE).unwrap();
    let keys: Vec<_> = edges.iter().map(|e| e.target_key).collect();
    assert_eq!(keys, [CALLER_NODE, CALLER_PROBE]);
    for e in &edges {
        assert_eq!(e.group_size, 2);
        assert!(
            e.reference.contains("exact-span-sha256="),
            "{}",
            e.target_key
        );
        let t = &map[e.target_key];
        assert!(
            e.reference.contains(&format!("{}:", t.path)),
            "{}",
            e.target_key
        );
        assert_eq!(row(&rows, e.target_key).value, Some(30_000));
    }
    // Every key in the table is a reviewed multi-target row, nothing else.
    let sources = multi_target::source_keys(multi_target::EDGES);
    assert_eq!(sources.len(), 5);
    for key in sources {
        assert!(row(&rows, key).review.starts_with(multi_target::PREFIX));
    }
}

#[test]
fn the_caller_census_is_exactly_the_two_reviewed_keyword_sites() {
    let (_, map) = world();
    let callers: Vec<_> = map
        .values()
        .filter(|c| c.kind == "keyword" && c.symbol == "startup_timeout")
        .map(|c| c.key.as_str())
        .collect();
    assert_eq!(callers, [CALLER_NODE, CALLER_PROBE]);
    assert_eq!(map[CALLER_NODE].path, NODE);
    assert_eq!(map[CALLER_PROBE].path, PROBE);
    // The constructor field has one store; every read is in the same class.
    let stores: Vec<_> = map
        .values()
        .filter(|c| c.kind == "assignment" && c.symbol == "self.startup_timeout")
        .collect();
    assert_eq!(stores.len(), 1);
    assert_eq!(stores[0].key, STORE);
    for key in READS {
        assert!(map[key].scope.starts_with("NativeSidecarProcess."), "{key}");
        assert_eq!(map[key].path, NODE);
    }
}

/// The checked-in table with the lines selected by `keep` and optional extras.
fn table(keep: impl Fn(&str) -> bool, extra: &[&str]) -> String {
    let mut out: Vec<String> = multi_target::EDGES
        .lines()
        .filter(|l| l.starts_with("key\t") || keep(l))
        .map(String::from)
        .collect();
    out.extend(extra.iter().map(|s| (*s).to_string()));
    out.join("\n") + "\n"
}

#[test]
fn a_missing_target_is_refused_for_the_store_and_every_read() {
    for key in std::iter::once(STORE).chain(READS) {
        assert!(ok(key, multi_target::EDGES), "{key} baseline");
        // Dropping either caller edge leaves one target under a group of two.
        for dropped in [CALLER_NODE, CALLER_PROBE] {
            let text = table(
                |l| !(l.starts_with(key) && l.contains(&format!("\t{dropped}\t"))),
                &[],
            );
            assert!(!ok(key, &text), "{key} without {dropped}");
        }
        // Shrinking the declared group to hide the missing caller is refused too:
        // the keyword census still has two callers and a group needs at least two.
        let text = table(
            |l| !(l.starts_with(key) && l.contains(&format!("\t{CALLER_PROBE}\t"))),
            &[],
        )
        .replace("\t2\t", "\t1\t");
        assert!(!ok(key, &text), "{key} group of one");
    }
}

#[test]
fn an_extra_target_is_refused_for_the_store_and_every_read() {
    let ui = "mycelium_ui_gateway/app.py:290:12:keyword:startup_timeout_seconds";
    for key in std::iter::once(STORE).chain(READS) {
        assert!(ok(key, multi_target::EDGES), "{key} baseline");
        let rule = multi_target::edges_of(multi_target::EDGES, key).unwrap()[0].rule;
        let extra = format!(
            "{key}\t{ui}\t{rule}\t3\tmycelium_ui_gateway/app.py:290-290 @ f4e366e98a1f705dfaf182897044500d290dbd7f; exact-span-sha256=00"
        );
        // Other edges of the key keep a declared size of two: size mismatch.
        assert!(!ok(key, &table(|_| true, &[&extra])), "{key} +ui (size 2)");
        // With a consistent group of three the unrelated keyword is still not a
        // caller of this parameter and is neither in the census nor the relation.
        let text = table(|_| true, &[&extra]).replace("\t2\t", "\t3\t");
        assert!(!ok(key, &text), "{key} +ui (size 3)");
        // A duplicate of an existing target is not a third caller.
        let dup = format!("{key}\t{CALLER_NODE}\t{rule}\t3\tduplicate");
        assert!(
            !ok(key, &table(|_| true, &[&dup]).replace("\t2\t", "\t3\t")),
            "{key} dup"
        );
    }
}

#[test]
fn relation_unit_rule_and_order_must_match_exactly() {
    let (rows, map) = world();
    let text = multi_target::EDGES;
    for key in std::iter::once(STORE).chain(READS) {
        let good = row(&rows, key).clone();
        let check = |bad: &Bound| multi_target::validate_with(bad, &rows, &map, text);
        assert!(check(&good), "{key}");
        let mut bad = good.clone();
        bad.relation = "PyUpperPhysicalInferenceNodeL2511C12".into();
        assert!(!check(&bad), "{key} single id");
        bad.relation = "PyScriptsRunTransportProbeSidecarMainStartupTimeoutL87+PyUpperPhysicalInferenceNodeL2511C12".into();
        assert!(!check(&bad), "{key} reversed");
        bad.relation = format!("{RELATION}+PyUpperPhysicalInferenceNodeL2511C12");
        assert!(!check(&bad), "{key} extra id");
        bad = good.clone();
        bad.unit = "bytes".into();
        assert!(!check(&bad), "{key} unit");
        bad = good.clone();
        bad.value = Some(30_000);
        assert!(!check(&bad), "{key} value");
        bad = good.clone();
        bad.disposition = String::new();
        bad.budget = "PyUpperPhysicalInferenceNodeL2511C12".into();
        assert!(!check(&bad), "{key} direct mapping");
        bad = good.clone();
        bad.review = bad
            .review
            .replacen("constructor-field-read", "x", 1)
            .replacen("constructor-parameter-store", "x", 1);
        assert!(!check(&bad), "{key} rule");
        bad = good.clone();
        bad.expression.push('!');
        assert!(!check(&bad), "{key} expression");
    }
    // The rule is specific to its shape: reads cannot use the store rule and v.v.
    let swapped = text
        .replace("constructor-parameter-store", "TMP")
        .replace("constructor-field-read", "constructor-parameter-store")
        .replace("TMP", "constructor-field-read");
    for key in std::iter::once(STORE).chain(READS) {
        let mut r = row(&rows, key).clone();
        r.review = r
            .review
            .replace("constructor-field-read", "TMP")
            .replace("constructor-parameter-store", "constructor-field-read")
            .replace("TMP", "constructor-parameter-store");
        assert!(
            !multi_target::validate_with(&r, &rows, &map, &swapped),
            "{key}"
        );
    }
}

#[test]
fn neighbouring_and_sibling_sites_are_not_admitted_by_the_family() {
    let (rows, map) = world();
    // Sibling stores/reads of other classes and the other sidecar sites have no edges.
    let others = [
        "mycelium_ui_gateway/upstream.py:169:8:assignment:self._startup_timeout_seconds",
        "mycelium_ui_gateway/upstream.py:261:33:keyword:timeout",
        CALLER_NODE,
        CALLER_PROBE,
    ];
    for key in others {
        let r = row(&rows, key);
        assert!(!multi_target::validate(r, &rows, &map), "{key}");
        assert!(!r.review.starts_with(multi_target::PREFIX), "{key}");
    }
    // A read row cannot borrow another reviewed row's edges by key swapping.
    let mut r = row(&rows, READS[0]).clone();
    r.key = READS[1].into();
    assert!(!multi_target::validate(&r, &rows, &map));
    // A malformed or re-headed table is refused wholesale.
    assert!(!ok(STORE, "key\ttarget_key\n"));
    assert!(!ok(STORE, ""));
}

#[test]
fn a_read_requires_the_unique_reviewed_store_in_the_same_class() {
    let (mut rows, map) = world();
    // Remove the reviewed store row: reads lose their witness.
    rows.retain(|r| r.key != STORE);
    for key in READS {
        let r = rows.iter().find(|r| r.key == key).unwrap();
        assert!(
            !multi_target::validate_with(r, &rows, &map, multi_target::EDGES),
            "{key}"
        );
    }
    // A second store of the field makes reads ambiguous.
    let mut map2 = map.clone();
    let mut twin = map[STORE].clone();
    twin.key = "physical_inference_node.py:1770:8:assignment:self.startup_timeout".into();
    twin.line = 1770;
    map2.insert(twin.key.clone(), twin);
    let (rows, _) = world();
    for key in READS {
        assert!(
            !multi_target::validate_with(row(&rows, key), &rows, &map2, multi_target::EDGES),
            "{key}"
        );
    }
}
