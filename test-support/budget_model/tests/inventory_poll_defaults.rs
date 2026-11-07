// SPDX-License-Identifier: AGPL-3.0-only
// Obligation: O:path:mycelium_iroh_sidecar/client.py.
use budget_model::inventory::{bindings, candidates, checked_in, validate};
#[test]
fn explicit_poll_selection_preserves_the_configured_fallback_not_a_fixed_wait() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    let get = |line, symbol: &str| {
        all.iter()
            .find(|c| {
                c.path == "mycelium_iroh_sidecar/client.py" && c.line == line && c.symbol == symbol
            })
            .unwrap()
    };
    for (parameter, selection, deadline) in [(706, 712, 715), (753, 761, 764)] {
        let p = get(parameter, "timeout");
        let s = get(selection, "maximum_wait");
        let d = get(deadline, "deadline");
        assert!(bindings::accepts(
            "optional-parameter-default",
            p,
            get(173, "self._timeout")
        ));
        assert!(bindings::accepts("resolved-poll-default", s, p));
        assert!(bindings::accepts(
            "monotonic-offset-same-scope-binding",
            d,
            s
        ));
        let mut bad = s.clone();
        bad.expression = bad.expression.replace("single_poll or", "single_poll and");
        assert!(!bindings::accepts("resolved-poll-default", &bad, p));
        let mut bad = d.clone();
        bad.expression = bad.expression.replace("time.monotonic()", "time.time()");
        assert!(!bindings::accepts(
            "monotonic-offset-same-scope-binding",
            &bad,
            s
        ));
        assert!(!bindings::accepts(
            "resolved-poll-default",
            s,
            get(parameter + 1, "wait_seconds")
        ));
        let r = rows.iter().find(|r| r.key == d.key).unwrap();
        assert!(r.value.is_none());
        assert_eq!(r.unit, "milliseconds");
    }
    assert!(!bindings::accepts(
        "optional-parameter-default",
        get(978, "deadline"),
        get(173, "self._timeout")
    ));
    assert_eq!(validate(&rows), Ok(()));
}
