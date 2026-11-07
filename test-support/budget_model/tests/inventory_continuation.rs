// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_node/process.py; O:path:mycelium_a13_macos/token_progress_pump.py.
use budget_model::inventory::{Candidate, candidates, categories, checked_in, validate};

fn real(path: &str, symbol: &str, expression: &str) -> Candidate {
    candidates()
        .unwrap()
        .into_iter()
        .find(|r| r.path == path && r.symbol == symbol && r.expression == expression)
        .unwrap_or_else(|| panic!("missing candidate: {path} {symbol} {expression}"))
}

#[test]
fn diagnostic_fields_are_observations_not_configured_waits() {
    for (path, symbol, expression) in [
        (
            "mycelium_node/process.py",
            "popen_ms",
            "(budget_started - popen_started) * 1000",
        ),
        (
            "mycelium_router/mlx_runtime.py",
            "retained_result_count",
            "len(self._replays)",
        ),
        (
            "mycelium_mobile/pixel_stage.py",
            "duration_ms",
            "(time.monotonic() - started) * 1000.0",
        ),
        (
            "mycelium_layer_planner/gossip_adapter.py",
            "jitter_ms",
            "jitter_ms",
        ),
        (
            "mycelium_seed/state.py",
            "expected_heartbeat_message_id",
            "heartbeat_message_id",
        ),
    ] {
        let row = real(path, symbol, expression);
        assert!(categories::accepts("diagnostic-field", &row), "{}", row.key);
        let mut mutated = row.clone();
        mutated.symbol = "timeout_seconds".into();
        assert!(!categories::accepts("diagnostic-field", &mutated));
        mutated = row.clone();
        mutated.path = "unreviewed.py".into();
        assert!(!categories::accepts("diagnostic-field", &mutated));
        mutated = row;
        mutated.kind = "comparison".into();
        assert!(!categories::accepts("diagnostic-field", &mutated));
    }
    let wait = real(
        "mycelium_a13_macos/token_progress_pump.py",
        "deadline_monotonic_s",
        "time.monotonic() + 0.5",
    );
    assert!(!categories::accepts("diagnostic-field", &wait));
}

#[test]
fn monotonic_deadline_maps_only_the_allowance_not_an_absolute_timestamp() {
    let row = real(
        "mycelium_a13_macos/token_progress_pump.py",
        "deadline_monotonic_s",
        "time.monotonic() + 0.5",
    );
    assert_eq!(categories::monotonic_allowance(&row), Some(500));
    let row2 = real(
        "mycelium_a13_macos/app.py",
        "self.launch_expires",
        "time.monotonic() + 120",
    );
    assert_eq!(categories::monotonic_allowance(&row2), Some(120000));
    for expression in [
        "time.time() + 0.5",
        "time.monotonic() - 0.5",
        "time.monotonic() + timeout",
        "time.monotonic() + 0.0001",
        "time.monotonic() + 18446744073709551615",
        "time.monotonic() + 0.5 + dynamic",
        "time.monotonic() + -1",
        "time.monotonic() + 0",
    ] {
        let mut changed = row.clone();
        changed.expression = expression.into();
        assert_eq!(
            categories::monotonic_allowance(&changed),
            None,
            "{expression}"
        );
    }
    let mut changed = row;
    changed.kind = "comparison".into();
    assert_eq!(categories::monotonic_allowance(&changed), None);
}

#[test]
fn reviewed_state_fields_do_not_waive_runtime_limits() {
    for (path, symbol, expression) in [
        ("mycelium_live/token_path_owner.py", "refresh_lease", "None"),
        (
            "mycelium_node/membership.py",
            "heartbeat_sequence",
            "self._heartbeat_sequence",
        ),
        (
            "mycelium_seed/operator.py",
            "output_lease",
            "private_directory_lease(output_root, create=True)",
        ),
        (
            "mycelium_router/transports/iroh.py",
            "stall_resends",
            "progress.stall_resends",
        ),
        (
            "mycelium_a13_macos/managed_classes.py",
            "windows_native_iroh",
            "'numpy'",
        ),
        ("mycelium_a13_macos/peer.py", "lease_state", "lease_state"),
    ] {
        let row = real(path, symbol, expression);
        assert!(
            categories::accepts("reviewed-state-field", &row),
            "{}",
            row.key
        );
        let mut changed = row.clone();
        changed.scope = "unreviewed_scope".into();
        assert!(!categories::accepts("reviewed-state-field", &changed));
        changed = row.clone();
        changed.kind = "comparison".into();
        assert!(!categories::accepts("reviewed-state-field", &changed));
        changed = row;
        changed.symbol = "timeout".into();
        assert!(!categories::accepts("reviewed-state-field", &changed));
    }
    for row in candidates().unwrap().iter().filter(|r| {
        matches!(
            r.symbol.as_str(),
            "timeout" | "deadline_monotonic_s" | "stalled_windows" | "self.max_entries"
        )
    }) {
        assert!(
            !categories::accepts("reviewed-state-field", row),
            "{}",
            row.key
        );
    }
}

#[test]
fn manual_supplement_preserves_the_prefill_resend_count() {
    let rows = candidates().unwrap();
    let candidate = rows.iter().find(|r| {
        r.path == "mycelium_router/transports/iroh.py" && r.line == 183 && r.symbol == "max_resends"
    });
    assert!(
        candidate.is_some(),
        "keyword discovery missed the prefill resend count"
    );
    let candidate = candidate.unwrap();
    assert_eq!(candidate.expression, "2");
    assert_eq!(categories::numeric_literal(candidate), Some(("entries", 2)));
    let rows = checked_in().unwrap();
    let bound = rows.iter().find(|r| r.key == candidate.key).unwrap();
    assert_eq!(bound.value, Some(2));
    assert_eq!(bound.unit, "entries");
    assert_eq!(
        budgets::BudgetId::from_name(&bound.budget)
            .unwrap()
            .count()
            .unwrap()
            .get(),
        2
    );
}

#[test]
fn deadline_allowance_claim_cannot_launder_an_unrelated_duration() {
    let mut rows = checked_in().unwrap();
    let row = rows
        .iter_mut()
        .find(|r| !r.budget.is_empty() && r.unit == "milliseconds")
        .unwrap();
    row.review = "deadline-category:monotonic-literal; forged".into();
    assert!(validate(&rows).is_err());
}
