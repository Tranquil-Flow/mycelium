// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_interactive/swarm.py; O:path:mycelium_a13_macos/managed_control.py.
use budget_model::inventory::{Candidate, bindings, candidates, categories, checked_in, validate};
use std::collections::BTreeMap;
fn mutate(c: &Candidate, field: usize) -> Candidate {
    let mut m = c.clone();
    match field {
        0 => m.key.push('!'),
        1 => m.path.push('!'),
        2 => m.line += 1,
        3 => m.column += 1,
        4 => m.scope.push('!'),
        5 => m.symbol.push('!'),
        6 => m.expression.push('!'),
        7 => m.kind.push('!'),
        8 => m.source.push('!'),
        _ => unreachable!(),
    }
    assert_ne!(m, *c);
    m
}
#[test]
fn a_round2_source_edges_require_exact_witnesses_and_correct_policy() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for (key, target, rule) in [
        (
            "mycelium_a13_macos/chain_compile.py:426:12:positional-call:thread.join",
            "mycelium_deadlines.py:217:0:assignment:CLEANUP_RECEIPT_POLL_SECONDS",
            "reviewed-composed-ceiling",
        ),
        (
            "mycelium_a13_macos/demo_chain.py:1220:8:assignment:grace",
            "mycelium_a13_macos/demo_chain.py:267:24:dict-field:member_restart_grace",
            "typed-config-field",
        ),
        (
            "mycelium_a13_macos/local_contributor.py:38:12:keyword:timeout",
            "mycelium_a13_macos/peer.py:135:8:assignment:self._timeout",
            "resolved-optional-value",
        ),
        (
            "mycelium_a13_macos/local_contributor.py:62:61:keyword:timeout",
            "mycelium_a13_macos/peer.py:135:8:assignment:self._timeout",
            "resolved-optional-value",
        ),
        (
            "mycelium_a13_macos/managed_control.py:2181:16:positional-call:sock.settimeout",
            "mycelium_a13_macos/managed_control.py:1985:8:assignment:self.timeout",
            "reviewed-helper-return",
        ),
        (
            "mycelium_a13_macos/managed_control.py:2253:44:keyword:timeout",
            "mycelium_a13_macos/managed_control.py:1985:8:assignment:self.timeout",
            "reviewed-helper-return",
        ),
        (
            "mycelium_a13_macos/public_relay.py:395:72:comparison:left",
            "mycelium_a13_macos/public_relay.py:287:0:assignment:RENEWAL_MARGIN_SECONDS",
            "reviewed-composed-ceiling",
        ),
        (
            "mycelium_a13_macos/swarm_publish.py:442:22:positional-call:self._stop.wait",
            "mycelium_a13_macos/swarm_publish.py:437:38:parameter:interval",
            "resolved-optional-value",
        ),
        (
            "mycelium_a13_macos/token_policy.py:44:8:keyword:clock_uncertainty_ms",
            "mycelium_a13_macos/managed_control.py:97:0:assignment:MAX_CLOCK_SKEW_SECONDS",
            "resolved-optional-value",
        ),
        (
            "physical_inference_node.py:967:8:assignment:self.limit_bytes",
            "physical_inference_node.py:966:42:parameter:limit_bytes",
            "resolved-optional-value",
        ),
        (
            "physical_inference_node.py:2094:8:assignment:deadline",
            "physical_inference_node.py:2079:4:assignment:socket_conflict_grace_seconds",
            "reviewed-clock-offset",
        ),
        (
            "mycelium_interactive/swarm.py:702:12:assignment:deadline",
            "mycelium_interactive/swarm.py:43:0:assignment:_MAX_LONG_POLL_SECONDS",
            "reviewed-helper-return",
        ),
        (
            "mycelium_interactive/swarm.py:757:41:keyword:timeout",
            "mycelium_interactive/swarm.py:43:0:assignment:_MAX_LONG_POLL_SECONDS",
            "reviewed-helper-return",
        ),
        (
            "mycelium_a13_macos/adaptive_host.py:877:16:dict-field:expires_at",
            "mycelium_a13_macos/adaptive_host.py:50:4:dict-field:stale_after_s",
            "reviewed-clock-offset",
        ),
        (
            "mycelium_a13_macos/adaptive_host.py:1045:8:assignment:interval",
            "mycelium_a13_macos/adaptive_host.py:49:4:dict-field:sample_interval_s",
            "resolved-optional-value",
        ),
    ] {
        let c = &all[key];
        let t = &all[target];
        assert!(bindings::accepts(rule, c, t), "missing reviewed edge {key}");
        for field in 0..9 {
            assert!(
                !bindings::accepts(rule, &mutate(c, field), t),
                "source {key} {field}"
            );
            assert!(
                !bindings::accepts(rule, c, &mutate(t, field)),
                "target {key} {field}"
            );
        }
        let bound = rows
            .iter()
            .find(|r| r.key == key)
            .expect("accepted source review");
        assert_eq!(bound.disposition, "derived");
        assert!(bound.value.is_none() && bound.budget.is_empty());
        let mut wrong = rows.clone();
        let b = wrong.iter_mut().find(|r| r.key == key).unwrap();
        let replacement = if b.unit == "bytes" {
            "PyThresholdPhysicalInferenceNodeL883C0"
        } else {
            "RegistryPoll"
        };
        let replacement = if b.relation == replacement {
            "PyReturnedMyceliumGatewayEventAdapterL247C48"
        } else {
            replacement
        };
        assert_ne!(b.relation, replacement);
        b.relation = replacement.into();
        assert!(validate(&wrong).is_err(), "wrong policy {key}");
    }
    assert_eq!(validate(&rows), Ok(()));
}
#[test]
fn a_round2_postvalidation_caps_do_not_exempt_raw_input_or_unrelated_waits() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let target = &all["mycelium_interactive/swarm.py:43:0:assignment:_MAX_LONG_POLL_SECONDS"];
    for key in [
        "mycelium_interactive/swarm.py:697:8:assignment:timeout",
        "mycelium_interactive/swarm.py:832:8:assignment:timeout",
        "mycelium_interactive/swarm.py:913:37:keyword:timeout",
        "mycelium_a13_macos/control_pump.py:381:14:positional-call:self._delivery_wakeup.wait",
    ] {
        let c = &all[key];
        assert!(!bindings::accepts("reviewed-helper-return", c, target));
        assert!(!bindings::accepts("resolved-optional-value", c, target));
        assert!(!categories::accepts("reviewed-structural-slot", c));
    }
}
#[test]
fn a_round2_lease_change_predicates_are_not_expiry_waivers() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    for key in [
        "mycelium_a13_macos/token_owner.py:79:35:comparison:previous[0]",
        "mycelium_a13_macos/token_policy.py:52:11:comparison:previous.get('lease_expires_at_unix_ms')",
        "mycelium_node/token_provisioning.py:157:11:comparison:latest['lease_expires_at_unix_ms']",
    ] {
        let c = &all[key];
        assert!(categories::accepts("reviewed-structural-slot", c));
        for field in 0..9 {
            assert!(!categories::accepts(
                "reviewed-structural-slot",
                &mutate(c, field)
            ));
        }
    }
    for key in [
        "mycelium_a13_macos/token_owner.py:94:12:assignment:self._lease_deadline",
        "mycelium_node/token_provisioning.py:147:4:assignment:lease",
    ] {
        assert!(!categories::accepts("reviewed-structural-slot", &all[key]));
    }
}
#[test]
fn a_round2_buffer_sentinel_and_envelope_are_exact_byte_quantities() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for (key, mode, value) in [
        (
            "mycelium_node/token_progress.py:24:0:assignment:MAX_PROGRESS_BYTES",
            "a-progress-envelope-bytes",
            1049600,
        ),
        (
            "mycelium_node/identity.py:79:60:keyword:max_bytes",
            "a-identity-sentinel-bytes",
            33,
        ),
    ] {
        let c = &all[key];
        assert_eq!(
            categories::reviewed_quantity(c, mode),
            Some(("bytes", value))
        );
        for field in 0..9 {
            assert_eq!(categories::reviewed_quantity(&mutate(c, field), mode), None);
        }
        let b = rows
            .iter()
            .find(|r| r.key == key)
            .expect("accepted byte ceiling");
        assert_eq!(b.value, Some(value));
        assert_eq!(b.unit, "bytes");
        let mut wrong = rows.clone();
        wrong.iter_mut().find(|r| r.key == key).unwrap().budget =
            "PyThresholdPhysicalInferenceNodeL883C0".into();
        assert!(validate(&wrong).is_err());
        let unrelated = &all["mycelium_a13_macos/control_pump.py:381:14:positional-call:self._delivery_wakeup.wait"];
        assert_eq!(categories::reviewed_quantity(unrelated, mode), None);
    }
}
