// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_seed/__main__.py; O:path:scripts/inventory_member_models.py; O:path:mycelium_speculative_decode_execution.py; O:path:scripts/run_a3_model_qualification_gate.py; O:path:mycelium_model_catalog.py.
use budget_model::inventory::{candidates, categories, checked_in};

const READOUTS: [&str; 5] = [
    "mycelium_seed/__main__.py:408:20:dict-field:expires_at",
    "scripts/inventory_member_models.py:102:16:dict-field:valid_until_unix_ms",
    "mycelium_speculative_decode_execution.py:1233:12:dict-field:cooldown_seconds",
    "scripts/run_a3_model_qualification_gate.py:920:12:dict-field:valid_until_unix_ms",
    "mycelium_model_catalog.py:1920:8:dict-field:evidence_valid_until_unix_ms",
];

#[test]
fn c_policy_readouts_are_exact_output_fields_not_grants() {
    let all = candidates().unwrap();
    let reviewed = checked_in().unwrap();
    for key in READOUTS {
        let c = all.iter().find(|c| c.key == key).unwrap();
        assert!(categories::accepts("reviewed-policy-readout", c), "{key}");
        let b = reviewed.iter().find(|b| b.key == key).unwrap();
        assert!(
            b.review.starts_with("category:reviewed-policy-readout;"),
            "{key}"
        );
        assert_eq!(b.disposition, "not-a-bound", "{key}");
        assert!(b.value.is_none(), "{key}");
        assert!(b.review.contains("exact-span-sha256="), "{key}");
        // No other non-bound category may claim the same field.
        for other in [
            "reviewed-nonscalar-slot",
            "reviewed-structural-slot",
            "reviewed-observation-slot",
        ] {
            assert!(!categories::accepts(other, c), "{key} as {other}");
        }
        for field in 0..9 {
            let mut changed = c.clone();
            match field {
                0 => changed.key.push('!'),
                1 => changed.path.push('!'),
                2 => changed.line += 1,
                3 => changed.column += 1,
                4 => changed.scope.push('!'),
                5 => changed.symbol.push('!'),
                6 => changed.expression.push('!'),
                7 => changed.kind.push('!'),
                8 => changed.source.push('!'),
                _ => unreachable!(),
            }
            assert!(
                !categories::accepts("reviewed-policy-readout", &changed),
                "{key} field {field}"
            );
        }
    }
}

#[test]
fn c_readouts_do_not_cover_the_producers_or_consuming_authorities() {
    let all = candidates().unwrap();
    let reviewed = checked_in().unwrap();
    // The raw evidence read, the pairing-window invite TTL and the signed qualification
    // record that carries an expiry as a grant are not output echoes.
    for key in [
        "mycelium_model_catalog.py:852:8:keyword:valid_until_unix_ms",
        "mycelium_seed/pairing.py:274:8:assignment:ttl",
        "mycelium_qualification/replica.py:142:8:dict-field:expires_at_unix_ms",
        "mycelium_product_spine/region_declarations.py:149:12:dict-field:expires_at_unix_ms",
        "mycelium_speculative_decode_execution.py:1150:8:dict-field:transfer_ms",
    ] {
        let c = all.iter().find(|c| c.key == key).unwrap();
        assert!(!categories::accepts("reviewed-policy-readout", c), "{key}");
        // Round 9 (RU-023) narrowing: the `transfer_ms` record field is reviewed, but only as a measured
        // observation (`observation-flow:record-field;`), never as a policy readout or a producer.
        let observation = key.ends_with("dict-field:transfer_ms");
        assert!(
            reviewed.iter().all(|b| b.key != key
                || (observation && b.review.starts_with("observation-flow:record-field; "))),
            "{key} reviewed"
        );
    }
}
