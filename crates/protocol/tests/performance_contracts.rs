// SPDX-License-Identifier: AGPL-3.0-only
mod support;
#[test]
fn performance_positive_bytes_and_negative_corpus() {
    // O:contract:mycelium.performance_budget.v1 (also v2/v3).
    support::corpus(
        include_str!("../fixtures/continued-performance-vectors.json"),
        |p, v| protocol::schemas::validate_as(p, v, 0),
    );
}
