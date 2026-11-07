// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a6_product_fault_seams.py; O:path:mycelium_qualification/qualifier.py.
use budget_model::inventory::{candidates, categories};

#[test]
fn reviewed_boolean_state_comparisons_do_not_waive_numeric_enforcement() {
    let rows = candidates().unwrap();
    for (path, line) in [
        ("mycelium_a6_product_fault_seams.py", 521),
        ("mycelium_qualification/qualifier.py", 1080),
        ("mycelium_live/artifact_provisioner.py", 620),
    ] {
        let c = rows
            .iter()
            .find(|r| r.path == path && r.line == line && r.kind == "comparison")
            .unwrap();
        assert!(
            categories::accepts("reviewed-state-comparison", c),
            "{}",
            c.key
        );
        let mut bad = c.clone();
        bad.expression = "elapsed >= timeout".into();
        assert!(!categories::accepts("reviewed-state-comparison", &bad));
        bad = c.clone();
        bad.scope = "unreviewed".into();
        assert!(!categories::accepts("reviewed-state-comparison", &bad));
        bad = c.clone();
        bad.kind = "keyword".into();
        assert!(!categories::accepts("reviewed-state-comparison", &bad));
    }
    for (path, line) in [
        ("mycelium_a13_macos/managed_control.py", 2226),
        ("mycelium_live/router_port.py", 3334),
        ("mycelium_swarm_directory/directory.py", 171),
        ("scripts/report_a11_request_bootstrap.py", 97),
    ] {
        let c = rows
            .iter()
            .find(|r| r.path == path && r.line == line && r.kind == "comparison")
            .unwrap();
        assert!(
            !categories::accepts("reviewed-state-comparison", c),
            "{}",
            c.key
        );
    }
}
