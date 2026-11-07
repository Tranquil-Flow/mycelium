// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_interactive/server.py; O:path:mycelium_platform_neutral/peer_profile.py.
use budget_model::inventory::{Candidate, bindings, candidates, categories};
fn at(path: &str, line: usize, symbol: &str) -> Candidate {
    candidates()
        .unwrap()
        .into_iter()
        .find(|r| r.path == path && r.line == line && r.symbol == symbol)
        .unwrap()
}
#[test]
fn returned_validator_values_keep_the_inclusive_ceiling_and_context_unit() {
    let p = "mycelium_platform_neutral/peer_profile.py";
    let value = at(p, 172, "max_frame_bytes");
    let cap = at(p, 176, "maximum");
    assert_eq!(
        categories::reviewed_quantity(&cap, "integer-argument"),
        Some(("bytes", 1_u64 << 30))
    );
    assert!(bindings::accepts("reviewed-helper-return", &value, &cap));
    let mut bad = value.clone();
    bad.expression = bad.expression.replace("bounded_int(", "unchecked(");
    assert!(!bindings::accepts("reviewed-helper-return", &bad, &cap));
    let other = at(p, 248, "maximum");
    assert!(!bindings::accepts("reviewed-helper-return", &value, &other));
}
#[test]
fn zero_helper_floor_is_not_a_zero_timeout_or_zero_maximum() {
    let floor = at(
        "mycelium_platform_neutral/acceleration_profile.py",
        204,
        "minimum",
    );
    assert_eq!(
        categories::reviewed_quantity(&floor, "helper-zero-floor"),
        Some(("bytes", 0))
    );
    assert_eq!(categories::reviewed_quantity(&floor, "literal"), None);
    let mut bad = floor.clone();
    bad.symbol = "maximum".into();
    assert_eq!(
        categories::reviewed_quantity(&bad, "helper-zero-floor"),
        None
    );
}
#[test]
fn configured_fallback_is_not_the_maximum_accepted_input() {
    let p = "mycelium_interactive/server.py";
    let value = at(p, 306, "timeout");
    let maximum = at(p, 306, "maximum");
    let default = at(p, 306, "default");
    assert_eq!(
        categories::reviewed_quantity(&maximum, "literal"),
        Some(("milliseconds", 25_000))
    );
    assert_eq!(
        categories::reviewed_quantity(&default, "literal"),
        Some(("milliseconds", 15_000))
    );
    assert!(bindings::accepts(
        "reviewed-helper-return",
        &value,
        &maximum
    ));
    assert!(!bindings::accepts(
        "reviewed-helper-return",
        &value,
        &default
    ));
    let mut changed = maximum.clone();
    changed.expression = "25000.0".into();
    assert!(!bindings::accepts(
        "reviewed-helper-return",
        &value,
        &changed
    ));
}
