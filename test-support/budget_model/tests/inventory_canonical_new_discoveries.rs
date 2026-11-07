// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_router/mlx_runtime.py; O:path:mycelium_live/artifact_transport.py.
use budget_model::inventory::{candidates, categories, checked_in};

#[test]
fn b_review_dependencies_and_nested_guard_remain_in_canonical_discovery() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    for (path, line, kind, expression) in [
        (
            "mycelium_live/artifact_transport.py",
            44,
            "assignment",
            "64 * 1024",
        ),
        (
            "mycelium_router/mlx_runtime.py",
            2770,
            "comparison",
            "floor <= item.position <= ceiling + 1",
        ),
    ] {
        let site = all
            .iter()
            .find(|c| c.path == path && c.line == line && c.kind == kind)
            .expect("source-review discovery must not remain hidden in proposal-only fixtures");
        assert_eq!(site.expression, expression);
        if let Some(review) = rows.iter().find(|r| r.key == site.key) {
            assert_ne!(
                review.unit, "not-applicable",
                "an enforcing dependency is not a record waiver"
            );
        }
        for category in [
            "reviewed-nonscalar-slot",
            "reviewed-observation-slot",
            "reviewed-policy-readout",
            "reviewed-structural-slot",
        ] {
            assert!(
                !categories::accepts(category, site),
                "no blanket waiver: {}",
                site.key
            );
        }
    }
}
