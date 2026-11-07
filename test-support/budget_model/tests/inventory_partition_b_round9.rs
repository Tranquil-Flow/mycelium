// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_ui_gateway/sessions.py; O:path:mycelium_ui_gateway/upstream.py; O:path:mycelium_ui_gateway/validation.py; O:path:mycelium_ui_gateway/app.py.
use budget_model::inventory::{Bound, candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

const CONFIG_TTL: &str = "PyMyceliumUiGatewayConfigGatewayconfigSessionTtlSecondsL57";
const CONFIG_REQS: &str = "PyMyceliumUiGatewayConfigGatewayconfigMaxRequestsPerSessionL59";
const CONFIG_UP: &str = "PyMyceliumUiGatewayConfigGatewayconfigUpstreamTimeoutSecondsL65";
const CONFIG_STREAM: &str = "PyMyceliumUiGatewayConfigGatewayconfigMaxStreamSecondsL64";
const SWARM_UPPER: &str = "PyUpperGuardMyceliumUiGatewayValidationL756C16";

fn row<'a>(rows: &'a [Bound], path: &str, line: usize) -> &'a Bound {
    rows.iter()
        .find(|r| r.path == path && r.line == line && r.review.starts_with("partition-b-family:"))
        .unwrap_or_else(|| panic!("missing B family row {path}:{line}"))
}

#[test]
fn b_ui_gateway_constructor_fields_derive_only_from_the_sole_config_caller() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    let s = "mycelium_ui_gateway/sessions.py";
    let u = "mycelium_ui_gateway/upstream.py";
    let a = "mycelium_ui_gateway/app.py";
    let v = "mycelium_ui_gateway/validation.py";
    for (path, line, unit, relation) in [
        (s, 45, "milliseconds", CONFIG_TTL),
        (s, 47, "entries", CONFIG_REQS),
        (s, 90, "milliseconds", CONFIG_TTL),
        (a, 454, "milliseconds", CONFIG_TTL),
        (u, 60, "milliseconds", CONFIG_UP),
        (u, 169, "milliseconds", CONFIG_UP),
        (u, 170, "milliseconds", CONFIG_STREAM),
        (u, 261, "milliseconds", CONFIG_UP),
        (u, 332, "milliseconds", CONFIG_UP),
        (u, 358, "milliseconds", CONFIG_STREAM),
        (u, 369, "milliseconds", CONFIG_STREAM),
        (v, 751, "milliseconds", SWARM_UPPER),
    ] {
        let r = row(&rows, path, line);
        assert_eq!(r.disposition, "derived");
        assert_eq!(r.unit, unit);
        assert_eq!(r.relation, relation, "{path}:{line}");
        assert!(r.value.is_none() && r.budget.is_empty());
        assert!(proof::validate(r, &rows, &all), "{path}:{line}");
        // Same-unit wrong ID: existence and unit alone must not satisfy provenance.
        let wrong = if unit == "entries" {
            "PyMyceliumUiGatewayConfigGatewayconfigMaxRequestBodyBytesL60"
        } else if relation == CONFIG_TTL {
            CONFIG_UP
        } else {
            CONFIG_TTL
        };
        let mut bad = r.clone();
        bad.relation = if unit == "entries" {
            "PyPrefillMaximumResends".into()
        } else {
            wrong.into()
        };
        assert!(
            !proof::validate(&bad, &rows, &all),
            "wrong ID {path}:{line}"
        );
        // The pinned expression is not malleable.
        bad = r.clone();
        bad.expression.push_str(" + 1");
        assert!(!proof::validate(&bad, &rows, &all));
    }
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}

#[test]
fn b_ui_gateway_unregistered_config_count_and_other_pending_sites_stay_pending() {
    let rows = checked_in().unwrap();
    // config.stream_buffer_chunks has no catalogued declaration in this inventory: the
    // queue size cannot borrow an unrelated ID. Other required constructor inputs also
    // remain pending.
    let (path, line) = ("mycelium_ui_gateway/upstream.py", 184);
    assert!(
        !rows.iter().any(|r| r.path == path && r.line == line),
        "{path}:{line} must remain an unreviewed candidate"
    );
}
