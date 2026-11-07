// SPDX-License-Identifier: AGPL-3.0-only
fn configure(bytes: u64, timeout: u64, deadline: f64, cache_ttl: u64) {}
fn run(budget: Budget) {
    configure(2048, budget.stop(), budget.expiry(), budget.lease());
    measure(30, 0.5);
}
