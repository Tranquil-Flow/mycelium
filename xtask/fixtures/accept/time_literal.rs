// SPDX-License-Identifier: AGPL-3.0-only
fn run(budget: Budget, measured_millis: u64) {
    let wait = Duration::from_secs(budget.stop_seconds());
    let elapsed = Duration::from_millis(measured_millis);
    let percentile = 0.99;
    let count = 30;
    histogram.observe(33.0);
}
