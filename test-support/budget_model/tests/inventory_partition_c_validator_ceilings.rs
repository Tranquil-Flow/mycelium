// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_membership/contracts.py; O:path:mycelium_a13_windows/onboarding.py; O:path:mycelium_mobile/android_onboarding.py; O:path:mycelium_mobile/a12_activation.py; O:path:mycelium_mobile/artifact_grant.py; O:path:mycelium_internet/physical.py.
use budget_model::inventory::{bindings, candidates, checked_in, validate};

// (source path, source line, target path, target line, ceiling symbol)
const SITES: [(&str, usize, &str, usize, &str); 10] = [
    (
        "mycelium_membership/contracts.py",
        445,
        "mycelium_membership/contracts.py",
        30,
        "MAX_MESSAGE_TTL_SECONDS",
    ),
    (
        "mycelium_membership/contracts.py",
        517,
        "mycelium_membership/contracts.py",
        30,
        "MAX_MESSAGE_TTL_SECONDS",
    ),
    (
        "mycelium_membership/contracts.py",
        549,
        "mycelium_membership/contracts.py",
        30,
        "MAX_MESSAGE_TTL_SECONDS",
    ),
    (
        "mycelium_membership/contracts.py",
        618,
        "mycelium_membership/contracts.py",
        30,
        "MAX_MESSAGE_TTL_SECONDS",
    ),
    (
        "mycelium_a13_windows/onboarding.py",
        268,
        "mycelium_a13_windows/onboarding.py",
        93,
        "_REQUEST_TTL_MAX",
    ),
    (
        "mycelium_mobile/android_onboarding.py",
        336,
        "mycelium_mobile/android_onboarding.py",
        111,
        "_REQUEST_TTL_MAX",
    ),
    (
        "mycelium_mobile/a12_activation.py",
        978,
        "mycelium_mobile/a12_activation.py",
        138,
        "MAX_PROFILE_TTL_MS",
    ),
    (
        "mycelium_mobile/artifact_grant.py",
        280,
        "mycelium_mobile/artifact_grant.py",
        38,
        "MAX_GRANT_TTL_MS",
    ),
    (
        "mycelium_mobile/artifact_grant.py",
        350,
        "mycelium_mobile/artifact_grant.py",
        39,
        "MAX_REQUEST_TTL_MS",
    ),
    (
        "mycelium_internet/physical.py",
        2027,
        "mycelium_internet/physical.py",
        1746,
        "_TRANSPORT_REPORT_MAX_AGE_MS",
    ),
];

const RULE: &str = "validated-required-ceiling";

#[test]
fn c_validator_ceilings_bind_only_their_enforced_constant() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    for (path, line, tp, tl, symbol) in SITES {
        let s = all
            .iter()
            .find(|r| r.path == path && r.line == line && r.kind == "assignment")
            .unwrap();
        let t = all
            .iter()
            .find(|r| r.path == tp && r.line == tl && r.symbol == symbol)
            .unwrap();
        assert!(bindings::accepts(RULE, s, t), "{}", s.key);
        let row = rows.iter().find(|r| r.key == s.key).unwrap();
        assert_eq!(row.disposition, "derived", "{}", s.key);
        assert!(row.review.contains("exact-span-sha256="), "{}", s.key);

        // Every source/target identity field is part of the reviewed witness.
        for field in 0..9 {
            for is_target in [false, true] {
                let mut bad = if is_target { t.clone() } else { s.clone() };
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
                assert!(
                    !if is_target {
                        bindings::accepts(RULE, s, &bad)
                    } else {
                        bindings::accepts(RULE, &bad, t)
                    },
                    "{} field {field} target={is_target}",
                    s.key
                );
            }
        }

        // The role is not another rule's evidence, and a sibling module declaration
        // in the same file is not the enforced ceiling.
        for other in [
            "upper-cap-reference",
            "reviewed-clock-offset",
            "reviewed-helper-return",
            "reviewed-local-comparison",
            "constructor-field",
        ] {
            assert!(!bindings::accepts(other, s, t), "{} as {other}", s.key);
        }
        for sibling in all.iter().filter(|r| {
            r.path == tp && r.scope == "<module>" && r.kind == "assignment" && r.key != t.key
        }) {
            assert!(
                !bindings::accepts(RULE, s, sibling),
                "{} must not accept sibling {}",
                s.key,
                sibling.key
            );
        }

        // Existence/unit checks alone do not prove provenance: retarget to another
        // valid same-unit ID.
        let mut bad = rows.clone();
        let row = bad.iter_mut().find(|r| r.key == s.key).unwrap();
        row.relation = if row.relation == "RegistryPoll" {
            "RegistryPoll2".into()
        } else {
            "RegistryPoll".into()
        };
        assert!(validate(&bad).is_err(), "{} unrelated same-unit ID", s.key);
    }
}

#[test]
fn c_validator_ceilings_do_not_cover_neighbouring_unchecked_expiries() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    // `_validate_assignment_offer` (660) and `_validate_drain_ack` (724) re-read the expiry
    // after `_validate_common`'s MAX_MESSAGE_TTL_SECONDS check on the same mapping. Coordinator
    // adjudication moved them from this pending pin to exact reviewed records
    // (inventory_canonical_membership_rereads cites the controls); never a looser check.
    for line in [660usize, 724] {
        let s = all
            .iter()
            .find(|r| {
                r.path == "mycelium_membership/contracts.py"
                    && r.line == line
                    && r.kind == "assignment"
            })
            .unwrap();
        let row = rows
            .iter()
            .find(|r| r.key == s.key)
            .expect("exact reviewed record");
        assert_eq!(row.disposition, "derived", "{}", s.key);
        assert_eq!(
            row.relation, "PyMyceliumMembershipContractsMaxMessageTtlSecondsL30",
            "{}",
            s.key
        );
    }
    // The platform-neutral from_mapping validators only require expires_at > issued_at.
    // Those sites stay pending, not covered by borrowed IDs.
    for (path, line) in [
        ("mycelium_platform_neutral/acceleration_profile.py", 156),
        ("mycelium_platform_neutral/peer_profile.py", 336),
    ] {
        let s = all
            .iter()
            .find(|r| r.path == path && r.line == line && r.kind == "assignment")
            .unwrap();
        assert!(rows.iter().all(|r| r.key != s.key), "{} reviewed", s.key);
    }
}
