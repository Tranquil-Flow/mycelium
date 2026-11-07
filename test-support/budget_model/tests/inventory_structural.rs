// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a7_evidence_control.py; O:path:mycelium_router/a10_live_publication.py.
use budget_model::inventory::{candidates, categories, checked_in};
#[test]
fn structural_slots_do_not_discharge_adjacent_time_or_capacity_enforcement() {
    let all = candidates().unwrap();
    for (path, line, symbol) in [
        ("mycelium_a13_macos/durable_requests.py", 72, "key"),
        (
            "mycelium_a7_evidence_control.py",
            412,
            "type(item.get('observed_at_monotonic_ms'))",
        ),
        (
            "mycelium_router/a10_live_publication.py",
            181,
            "interval_ref",
        ),
    ] {
        let c = all
            .iter()
            .find(|r| r.path == path && r.line == line && r.symbol == symbol)
            .unwrap();
        assert!(categories::accepts("reviewed-structural-slot", c));
    }
    for (path, line, symbol) in [
        (
            "mycelium_a7_evidence_control.py",
            412,
            "now_ms - item['observed_at_monotonic_ms']",
        ),
        (
            "mycelium_qualification/replica_track_backend.py",
            366,
            "item['active_reservations']",
        ),
        (
            "mycelium_router/terminal_kv_retention.py",
            106,
            "float(now)",
        ),
    ] {
        let c = all
            .iter()
            .find(|r| r.path == path && r.line == line && r.symbol == symbol)
            .unwrap();
        assert!(!categories::accepts("reviewed-structural-slot", c));
    }
}
#[test]
fn structural_classification_is_exact_including_all_identity_fields() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    let mut count = 0;
    for b in rows
        .iter()
        .filter(|r| r.review.starts_with("category:reviewed-structural-slot;"))
    {
        count += 1;
        let c = all.iter().find(|c| c.key == b.key).unwrap();
        assert!(categories::accepts("reviewed-structural-slot", c));
        for field in 0..9 {
            let mut bad = c.clone();
            match field {
                0 => bad.key.push('!'),
                1 => bad.path.push('!'),
                2 => bad.line += 1,
                3 => bad.column += 1,
                4 => bad.scope.push('!'),
                5 => bad.symbol.push('!'),
                6 => bad.expression.push('!'),
                7 => bad.kind.push('!'),
                8 => bad.source.push('!'),
                _ => unreachable!(),
            }
            assert!(!categories::accepts("reviewed-structural-slot", &bad));
        }
    }
    assert!(count > 0);
}
