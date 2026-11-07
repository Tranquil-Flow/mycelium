// SPDX-License-Identifier: AGPL-3.0-only
use latency_profiles::statistics;
#[test]
fn nearest_rank_is_independent_and_sparse_samples_are_blocked() {
    let s = statistics(&[3000.0, 1000.0, 2000.0], true, true);
    assert_eq!(s.count, 3);
    assert_eq!(s.p50_ms, Some(2000.0));
    assert_eq!(s.p99_ms, None);
    assert_eq!(s.status, "BLOCKED");
    let raw: Vec<_> = (1..=100).map(f64::from).collect();
    let s = statistics(&raw, true, true);
    assert_eq!(s.p50_ms, Some(50.0));
    assert_eq!(s.p99_ms, Some(99.0));
    assert_eq!(s.status, "EMPIRICAL_P99");
    assert_eq!(statistics(&[33_000.0], false, true).p50_ms, None);
    assert_eq!(statistics(&raw, true, false).p99_ms, None);
    assert_eq!(statistics(&raw, false, true).p99_ms, None);
}
