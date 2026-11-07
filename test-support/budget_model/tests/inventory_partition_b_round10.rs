// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_router/scheduler.py; O:path:mycelium_router/relay.py.
use budget_model::inventory::{candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

const RETRY_DEFAULT: &str =
    "PyMyceliumRouterContractsRouterconfigBackpressureRetryAfterSecondsL161";

#[test]
fn b_backpressure_retry_hint_derives_from_router_config_only_via_the_sole_raise_site() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for (path, line) in [
        ("mycelium_router/scheduler.py", 23),
        ("mycelium_router/relay.py", 1398),
        ("mycelium_router/relay.py", 1839),
        ("mycelium_router/relay.py", 2244),
    ] {
        let r = rows
            .iter()
            .find(|r| {
                r.path == path && r.line == line && r.review.starts_with("partition-b-family:")
            })
            .unwrap_or_else(|| panic!("missing B family row {path}:{line}"));
        assert_eq!(r.disposition, "derived");
        assert_eq!(r.unit, "milliseconds");
        assert_eq!(r.relation, RETRY_DEFAULT);
        assert!(r.value.is_none() && r.budget.is_empty());
        assert!(proof::validate(r, &rows, &all), "{path}:{line}");
        // The 1 ms max() floor at scheduler.py:52 is a different, lower-domain ID.
        for wrong in [
            "PyQuantityMyceliumRouterSchedulerL52C6",
            "PyMyceliumUiGatewayConfigGatewayconfigUpstreamTimeoutSecondsL65",
        ] {
            let mut bad = r.clone();
            bad.relation = wrong.into();
            assert_ne!(bad.relation, r.relation);
            assert!(!proof::validate(&bad, &rows, &all), "wrong ID {wrong}");
        }
        let mut bad = r.clone();
        bad.expression = bad.expression.replace("error.", "exc.");
        if bad.expression != r.expression {
            assert!(!proof::validate(&bad, &rows, &all));
        }
        bad = r.clone();
        bad.expression.push_str(" + 1");
        assert!(!proof::validate(&bad, &rows, &all));
    }
    // Other retry-hint sources are not this scheduler's configured default.
    let (path, line) = ("mycelium_router/a10_batch_admission.py", 623);
    assert!(
        !rows.iter().any(|r| r.path == path && r.line == line),
        "{path}:{line} keeps its own policy authority and stays pending"
    );
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}
