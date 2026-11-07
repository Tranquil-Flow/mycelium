// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/artifact_agent.py.
use budget_model::inventory::{Bound, candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

const P: &str = "mycelium_live/artifact_agent.py";
const TTL: &str = "PyMyceliumLiveArtifactAgentAdvertisementTtlSecondsMaximumL386";
const RATE: &str = "PyMyceliumLiveArtifactAgentMaxBytesPerSecondMaximumL388";

fn at(rows: &[Bound], line: usize) -> &Bound {
    rows.iter()
        .find(|r| r.path == P && r.line == line)
        .unwrap_or_else(|| panic!("missing reviewed row {P}:{line}"))
}

#[test]
fn b_artifact_agent_schema_tuple_maxima_are_upper_ceilings_only() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    let ttl = at(&rows, 386);
    assert_eq!(
        (ttl.unit.as_str(), ttl.value),
        ("milliseconds", Some(86_400_000))
    );
    assert_eq!(ttl.budget, TTL);
    assert!(ttl.semantic.contains("maximum"));
    let rate = at(&rows, 388);
    assert_eq!(
        (rate.unit.as_str(), rate.value),
        ("bytes-per-second", Some(9_223_372_036_854_775_807))
    );
    assert_eq!(rate.budget, RATE);
    for r in [ttl, rate] {
        assert!(proof::validate(r, &rows, &all), "{}", r.key);
        // The tuple's positive minimum (1) is a different policy: it cannot be the value.
        let mut bad = r.clone();
        bad.value = Some(1);
        assert!(!proof::validate(&bad, &rows, &all));
        bad = r.clone();
        bad.unit = if r.unit == "milliseconds" {
            "bytes"
        } else {
            "milliseconds"
        }
        .into();
        assert!(!proof::validate(&bad, &rows, &all));
    }
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}

#[test]
fn b_artifact_agent_validated_config_consumers_follow_the_schema_ceiling() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for (line, unit, relation, wrong) in [
        (501, "milliseconds", TTL, RATE),
        (
            502,
            "milliseconds",
            TTL,
            "PyMyceliumLiveRouteTransportFaultWindowL1373",
        ),
        (
            530,
            "milliseconds",
            TTL,
            "PyMyceliumLiveRouteTransportFaultWindowL1373",
        ),
        (
            329,
            "bytes-per-second",
            RATE,
            "PyRateEligibilityArtifactTransportL395C11",
        ),
        (
            498,
            "bytes-per-second",
            RATE,
            "PyRateEligibilityArtifactTransportL395C11",
        ),
    ] {
        let r = rows
            .iter()
            .find(|r| r.path == P && r.line == line && r.review.starts_with("partition-b-family:"))
            .unwrap_or_else(|| panic!("missing B family row {P}:{line}"));
        assert_eq!(r.disposition, "derived");
        assert_eq!(r.unit, unit);
        assert_eq!(r.relation, relation, "line {line}");
        assert!(proof::validate(r, &rows, &all), "line {line}");
        let mut bad = r.clone();
        bad.relation = wrong.into();
        assert!(!proof::validate(&bad, &rows, &all), "wrong ID at {line}");
    }
    // valid_until (min of manifest expiry and now+ttl) is an uncatalogued writer here, so its
    // dict-field consumer cannot be discharged by this family.
    assert!(!rows.iter().any(|r| r.path == P && r.line == 339));
}
