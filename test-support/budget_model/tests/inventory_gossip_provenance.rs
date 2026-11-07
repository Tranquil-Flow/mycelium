// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_gossip/schema.py; O:path:mycelium_gossip/registry.py.
use budget_model::inventory::{bindings, candidates, categories};
#[test]
fn validated_gossip_builders_do_not_lend_their_cap_to_unchecked_registry_entries() {
    let all = candidates().unwrap();
    let cap = all
        .iter()
        .find(|r| r.path == "mycelium_gossip/schema.py" && r.line == 453)
        .unwrap();
    let floor = all
        .iter()
        .find(|r| {
            r.path == "mycelium_gossip/schema.py" && r.line == 452 && r.kind == "validation-minimum"
        })
        .unwrap();
    assert_eq!(
        categories::reviewed_quantity(floor, "literal"),
        Some(("milliseconds", 1))
    );
    for line in [452, 481, 494, 526] {
        let c = all
            .iter()
            .find(|r| r.path == "mycelium_gossip/schema.py" && r.line == line)
            .unwrap();
        assert!(bindings::accepts("gossip-successful-validation", c, cap));
        let mut bad = c.clone();
        bad.expression.push_str(" + 1");
        assert!(!bindings::accepts(
            "gossip-successful-validation",
            &bad,
            cap
        ));
    }
    for line in [234, 258, 271] {
        let c = all
            .iter()
            .find(|r| r.path == "mycelium_gossip/registry.py" && r.line == line)
            .unwrap();
        assert!(!bindings::accepts("gossip-successful-validation", c, cap));
    }
    let mut bad = cap.clone();
    bad.expression = "ttl >= 7 * 24 * 60 * 60 * 1000".into();
    let consumer = all
        .iter()
        .find(|r| r.path == "mycelium_gossip/schema.py" && r.line == 494)
        .unwrap();
    assert!(!bindings::accepts(
        "gossip-successful-validation",
        consumer,
        &bad
    ));
}
#[test]
fn serializer_readouts_cannot_disposition_their_scalar_validation_or_expiry() {
    let all = candidates().unwrap();
    for (path, line) in [
        ("mycelium_gossip/schema.py", 424),
        ("mycelium_platform_neutral/peer_profile.py", 197),
        ("mycelium_seed/coordinator.py", 154),
    ] {
        let c = all
            .iter()
            .find(|r| r.path == path && r.line == line)
            .unwrap();
        assert!(categories::accepts("reviewed-policy-readout", c));
        let mut bad = c.clone();
        bad.expression = format!("{} + 1", bad.expression);
        assert!(!categories::accepts("reviewed-policy-readout", &bad));
    }
    for (path, line) in [
        ("mycelium_gossip/schema.py", 453),
        ("mycelium_gossip/registry.py", 234),
        ("mycelium_platform_neutral/peer_profile.py", 172),
    ] {
        let c = all
            .iter()
            .find(|r| r.path == path && r.line == line)
            .unwrap();
        assert!(!categories::accepts("reviewed-policy-readout", c));
    }
}
