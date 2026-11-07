// SPDX-License-Identifier: AGPL-3.0-only
mod support;
// O:contract:mycelium.full_context_recovery_intent.v1
// O:contract:mycelium.full_context_recovery_qualification.v1
// O:contract:mycelium.full_context_recovery_runtime.v1
// O:contract:mycelium.full_context_recovery_gate.v1
#[test]
fn recovery_positive_bytes_and_negative_corpus() {
    support::corpus(
        include_str!("../fixtures/continued-recovery-vectors.json"),
        |p, v| protocol::schemas::validate_as(p, v, 0),
    );
}
