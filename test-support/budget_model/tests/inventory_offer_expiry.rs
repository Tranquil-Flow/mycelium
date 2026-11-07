// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_swarm_directory/directory.py.
use budget_model::inventory::{Candidate, bindings, candidates};
fn at(line: usize) -> Candidate {
    candidates()
        .unwrap()
        .into_iter()
        .find(|c| c.path == "mycelium_swarm_directory/directory.py" && c.line == line)
        .unwrap()
}
#[test]
fn directory_offer_validity_preserves_inclusive_age_and_limit_origin() {
    let source = at(327);
    let target = at(47);
    assert!(bindings::accepts("directory-offer-age", &source, &target));
    for expression in [
        "now - v['received'] < self.limits.offer_ttl",
        "now - v['at'] <= self.limits.offer_ttl",
        "now - v['received'] <= self.limits.heartbeat_ttl",
    ] {
        let mut bad = source.clone();
        bad.expression = expression.into();
        assert!(!bindings::accepts("directory-offer-age", &bad, &target));
    }
    assert!(!bindings::accepts("directory-offer-age", &source, &at(45)));
    let mut bad = target.clone();
    bad.scope = "ForeignLimits".into();
    assert!(!bindings::accepts("directory-offer-age", &source, &bad));
}
#[test]
fn required_batch_window_and_raw_gossip_record_are_not_directory_defaults() {
    for (path, line) in [
        ("mycelium_router/a10_batch_admission.py", 840),
        ("mycelium_gossip/registry.py", 258),
    ] {
        let source = candidates()
            .unwrap()
            .into_iter()
            .find(|c| c.path == path && c.line == line)
            .unwrap();
        assert!(!bindings::accepts("directory-offer-age", &source, &at(47)));
    }
}
