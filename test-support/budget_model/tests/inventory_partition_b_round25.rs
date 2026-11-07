// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_router/transports/iroh.py; O:path:mycelium_live/route.py.
use budget_model::inventory::{Bound, candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

const IROH: &str = "mycelium_router/transports/iroh.py";
const ROUTE: &str = "mycelium_live/route.py";
const WINDOW: &str = "PyMyceliumRouterContractsRouterconfigLiveHopCollectionWindowSecondsL171";
// Same unit, different declaration (the iroh poll interval default).
const OTHER: &str = "PyMyceliumRouterTransportsIrohIrohtransportPollIntervalSecondsL1";

fn at<'a>(rows: &'a [Bound], path: &str, line: usize) -> &'a Bound {
    rows.iter()
        .find(|r| r.path == path && r.line == line && r.review.starts_with("partition-b-family:"))
        .unwrap_or_else(|| panic!("missing B family row {path}:{line}"))
}

fn all_candidates() -> BTreeMap<String, budget_model::inventory::Candidate> {
    candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect()
}

#[test]
fn b_hop_collection_deadline_state_carries_only_the_router_window_field() {
    let all = all_candidates();
    let rows = checked_in().unwrap();
    for line in [3755, 3756, 3773, 3897] {
        let r = at(&rows, IROH, line);
        assert_eq!(r.disposition, "derived", "{line}");
        assert_eq!(r.unit, "milliseconds", "{line}");
        assert_eq!(r.relation, WINDOW, "{line}");
        assert!(proof::validate(r, &rows, &all), "{line}");
        let mut bad = r.clone();
        bad.relation = OTHER.into();
        assert!(
            !proof::validate(&bad, &rows, &all),
            "{line}: wrong same-unit ID"
        );
    }
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}

#[test]
fn b_hop_collection_deadline_needs_every_none_writer_reviewed() {
    let all = all_candidates();
    let rows = checked_in().unwrap();
    // Four reviewed inactive resets (974, 3751, 3895, 3904) are the only other writers.
    for gone in [974, 3751, 3895, 3904] {
        let without: Vec<_> = rows
            .iter()
            .filter(|x| !(x.path == IROH && x.line == gone))
            .cloned()
            .collect();
        for reader in [3773, 3897] {
            assert!(
                !proof::validate(at(&rows, IROH, reader), &without, &all),
                "reader {reader} without writer {gone}"
            );
        }
    }
    // The reader's offset needs the window row.
    let without_window: Vec<_> = rows
        .iter()
        .filter(|x| !(x.path == IROH && x.line == 3755))
        .cloned()
        .collect();
    assert!(!proof::validate(
        at(&rows, IROH, 3756),
        &without_window,
        &all
    ));
    // Poll caps and the dispatcher helper remain separate sites.
    assert!(
        !rows
            .iter()
            .any(|x| x.path == IROH && x.line == 3776 && x.review.starts_with("partition-b-"))
    );
}

#[test]
fn b_route_local_cleanup_deadline_and_probe_hold_keep_their_own_ids() {
    let all = all_candidates();
    let rows = checked_in().unwrap();
    let cancel = at(&rows, ROUTE, 2451);
    assert_eq!(cancel.unit, "milliseconds");
    assert!(proof::validate(cancel, &rows, &all));
    let floor = at(&rows, ROUTE, 2783);
    assert!(proof::validate(floor, &rows, &all));
    for (row, wrong) in [
        (cancel, "PyMyceliumDeadlinesCleanupReceiptPollSecondsL217"),
        (floor, "PyMyceliumDeadlinesCleanupReceiptPollSecondsL217"),
    ] {
        let mut bad = row.clone();
        bad.relation = wrong.into();
        assert!(!proof::validate(&bad, &rows, &all), "{}", row.line);
    }
    // Round 11 keeps the `budget - reserve` subtraction (:213) unreviewed: it stays pending.
    assert!(
        !rows
            .iter()
            .any(|x| x.path == ROUTE && x.line == 213 && x.review.starts_with("partition-b-"))
    );
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}
