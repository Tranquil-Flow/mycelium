// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/route.py; O:path:mycelium_live/command_controller.py.
use budget_model::inventory::{checked_in, validate};
const CURRENT: &str = "941765e882ed3eea9a5576cc53e525c4caa20300";
#[test]
fn new_snapshot_allowance_has_current_identity_and_cannot_be_backdated() {
    let rows = checked_in().unwrap();
    let index = rows
        .iter()
        .position(|r| {
            r.path == "mycelium_live/route.py"
                && r.line == 5330
                && r.symbol == "deadline_monotonic_s"
        })
        .expect("new bounded snapshot operand must be reviewed");
    assert_eq!(rows[index].source_sha, CURRENT);
    assert_eq!(
        rows[index].expression,
        "time.monotonic() + _CLEANUP_SNAPSHOT_SLICE_SECONDS"
    );
    assert!(rows[index].value.is_none());
    assert_eq!(validate(&rows), Ok(()));
    for pin in [
        budget_model::inventory::SOURCE_SHA,
        budget_model::inventory::PREVIOUS_SOURCE_SHA,
    ] {
        let mut bad = rows.clone();
        bad[index].source_sha = pin.into();
        assert!(validate(&bad).is_err(), "new operand cannot be backdated");
    }
}
#[test]
fn historical_removed_timestamp_review_cannot_claim_current_source() {
    let mut rows = checked_in().unwrap();
    let row = rows
        .iter_mut()
        .find(|r| r.path == "mycelium_live/command_controller.py" && r.line == 496)
        .unwrap();
    assert_ne!(row.source_sha, CURRENT);
    row.source_sha = CURRENT.into();
    assert!(validate(&rows).is_err());
}
