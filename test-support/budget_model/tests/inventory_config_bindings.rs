// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/control_pump.py; O:path:mycelium_live/supervisor.py.
use budget_model::inventory::{Candidate, bindings, candidates};
fn at(path: &str, line: usize, symbol: &str) -> Candidate {
    candidates()
        .unwrap()
        .into_iter()
        .find(|r| r.path == path && r.line == line && r.symbol == symbol)
        .unwrap()
}
#[test]
fn class_fields_and_bound_parameter_casts_preserve_lexical_origins() {
    for (rule, path, line, symbol, declaration, name) in [
        (
            "class-field",
            "mycelium_a13_macos/control_pump.py",
            159,
            "max_bytes",
            124,
            "_state_max_bytes",
        ),
        (
            "parameter-bound-float-normalization",
            "mycelium_a13_macos/acquisition.py",
            414,
            "self.timeout_seconds",
            405,
            "timeout_seconds",
        ),
    ] {
        let source = at(path, line, symbol);
        let target = at(path, declaration, name);
        assert!(bindings::accepts(rule, &source, &target));
        let mut bad = source.clone();
        bad.scope = "Other.method".into();
        assert!(!bindings::accepts(rule, &bad, &target));
        bad = source.clone();
        bad.expression = "int(timeout_seconds)".into();
        assert!(!bindings::accepts(rule, &bad, &target));
        let mut wrong = target.clone();
        wrong.symbol = "unrelated".into();
        assert!(!bindings::accepts(rule, &source, &wrong));
    }
}
#[test]
fn imported_constant_edges_require_the_reviewed_import_binding() {
    let source = at("mycelium_a7_node_wiring.py", 238, "max_entries");
    let target = at(
        "mycelium_router/terminal_kv_retention.py",
        37,
        "DEFAULT_TERMINAL_KV_RETENTION_ENTRIES",
    );
    assert!(bindings::accepts("imported-constant", &source, &target));
    for field in ["expression", "path"] {
        let mut bad = source.clone();
        if field == "expression" {
            bad.expression = "OTHER_ENTRIES".into();
        } else {
            bad.path = "unreviewed.py".into();
        }
        assert!(!bindings::accepts("imported-constant", &bad, &target));
    }
    let mut bad = target.clone();
    bad.path = "unreviewed.py".into();
    assert!(!bindings::accepts("imported-constant", &source, &bad));
}

#[test]
fn typed_configuration_fields_require_exact_receiver_and_declared_class() {
    let source = at("mycelium_ui_gateway/app.py", 406, "timeout");
    let target = at(
        "mycelium_ui_gateway/config.py",
        65,
        "upstream_timeout_seconds",
    );
    assert!(bindings::accepts("typed-config-field", &source, &target));
    let mut bad = source.clone();
    bad.expression = "other.upstream_timeout_seconds".into();
    assert!(!bindings::accepts("typed-config-field", &bad, &target));
    bad = source.clone();
    bad.scope = "Other.method".into();
    assert!(!bindings::accepts("typed-config-field", &bad, &target));
    let mut wrong = target.clone();
    wrong.scope = "OtherConfig".into();
    assert!(!bindings::accepts("typed-config-field", &source, &wrong));
}

#[test]
fn remaining_monotonic_allowance_keeps_its_original_deadline_binding() {
    let source = at("mycelium_request_gateway/service.py", 775, "timeout");
    let target = at("mycelium_request_gateway/service.py", 769, "deadline");
    assert!(bindings::accepts(
        "remaining-monotonic-local",
        &source,
        &target
    ));
    for expression in [
        "max(0.0, deadline - time.time())",
        "max(1.0, deadline - time.monotonic())",
        "max(0.0, other - time.monotonic())",
        "time.monotonic() - deadline",
    ] {
        let mut bad = source.clone();
        bad.expression = expression.into();
        assert!(!bindings::accepts(
            "remaining-monotonic-local",
            &bad,
            &target
        ));
    }
}
