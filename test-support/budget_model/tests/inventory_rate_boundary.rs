// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/artifact_transport.py.
use budget_model::inventory::{candidates, categories, checked_in, validate};
#[test]
fn rate_eligibility_floor_is_neither_storage_nor_time() {
    let all = candidates().unwrap();
    let c = all
        .iter()
        .find(|c| {
            c.path == "mycelium_live/artifact_transport.py"
                && c.line == 395
                && c.kind == "comparison"
        })
        .unwrap();
    assert_eq!(
        categories::reviewed_quantity(c, "positive-boundary"),
        Some(("bytes-per-second", 1))
    );
    let mut bad = c.clone();
    bad.expression = "maximum_bytes_per_second > 1".into();
    assert_eq!(
        categories::reviewed_quantity(&bad, "positive-boundary"),
        None
    );
    let rows = checked_in().unwrap();
    assert_eq!(validate(&rows), Ok(()));
    let i = rows.iter().position(|r| r.key == c.key).unwrap();
    for wrong_unit in ["bytes", "milliseconds", "entries"] {
        let mut bad = rows.clone();
        bad[i].unit = wrong_unit.into();
        assert!(validate(&bad).is_err());
    }
}
