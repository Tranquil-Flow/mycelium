// SPDX-License-Identifier: AGPL-3.0-only
//! Additive native-worker evidence contracts (class A).
//!
//! Existing prepare/step/close frames are unchanged. The request has exactly
//! REQUEST_FIELDS, no payload, and a lowercase 128-bit random nonce. A proof is
//! a JSON object with exactly PROOF_FIELDS. `weights` is the backend's complete
//! resident-name -> component-name -> {shape,payload_bytes,payload_sha256,dtype}
//! map; `weights_digest` is protocol canonical SHA256 of that map. It is never
//! the source-file digest. Checkpoint/manifest hashes are lowercase bare hex.
//! Request identity, load generation and layer range come from the live binding.
//! PID and nonce-scoped host/boot identities are observed by the worker process.
//! This private socket proof is not a signed remote member attestation.
//!
//! The native-local plan has PLAN_FIELDS; its signature covers all other fields
//! including its verification key. `documents` binds canonical digests of every
//! input/output document except the plan and its final completion sentinel.
//! Stage order, membership, source/binary pins, resident proofs and the challenge
//! are checked against the signed plan and independently recorded reference.
//! Compilation qualification never grants remote execution or serving readiness.

pub const REQUEST_PROTOCOL: &str = "mycelium.native_resident_request.v1";
pub const PROOF_PROTOCOL: &str = "mycelium.native_resident_proof.v1";
pub const COMMAND: &str = "resident_proof_v1";
pub const REQUEST_FIELDS: &[&str] = &["command", "protocol", "nonce"];
pub const PROOF_FIELDS: &[&str] = &[
    "protocol",
    "nonce",
    "request_id",
    "load_generation",
    "start",
    "end",
    "checkpoint_sha256",
    "tensor_manifest_sha256",
    "revision",
    "profile",
    "worker_pid",
    "host_id",
    "boot_id",
    "weights",
    "weights_digest",
];
pub const PLAN_PROTOCOL: &str = "mycelium.native_local_operator_plan.v1";
pub const CHECKPOINT_PROTOCOL: &str = "mycelium.native_local_challenge.v1";
pub const TEMPLATE_PROTOCOL: &str = "mycelium.native_local_template.v1";
pub const REPORT_PROTOCOL: &str = "mycelium.native_local_build_report.v1";
pub const CACHE_PROTOCOL: &str = "mycelium.native_local_cache.v1";
pub const CHECKPOINT_FIELDS: &[&str] = &[
    "protocol",
    "state",
    "run_id",
    "deployment_id",
    "reference_digest",
    "prompt_token_ids",
    "output_token_ids",
    "proofs",
    "steps",
    "cleanup",
];
pub const TEMPLATE_FIELDS: &[&str] = &[
    "protocol",
    "members",
    "verification_keys",
    "source_manifest",
];
pub const REPORT_FIELDS: &[&str] = &[
    "protocol",
    "run_id",
    "route_ready",
    "scope",
    "build_seconds",
    "challenge_output_token_ids",
];
pub const CACHE_FIELDS: &[&str] = &["protocol", "cache_hit", "compilation_cache_key"];
pub const PLAN_FIELDS: &[&str] = &[
    "protocol",
    "run_id",
    "deployment_id",
    "epoch",
    "scope",
    "route_ready",
    "nonce",
    "stages",
    "members",
    "source",
    "binary",
    "reference_digest",
    "documents",
    "verification_keys",
    "signer",
    "signature",
];
pub const CHALLENGE_OUTPUT_TOKENS: usize = 4;
/// Independent Python oracle, not a result of the compiler under qualification.
pub const QWEN3_PACKED_ORACLE: &str = include_str!("../data/qwen3-packed-oracle.json");

pub fn valid_nonce(nonce: &str) -> bool {
    nonce.len() == 32
        && nonce
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

#[cfg(test)]
mod tests {
    #[test]
    fn nonce_is_canonical_and_version_is_explicit() {
        assert!(super::valid_nonce("00112233445566778899aabbccddeeff"));
        for nonce in ["", "00112233445566778899AABBCCDDEEFF", "00", "../../root"] {
            assert!(!super::valid_nonce(nonce));
        }
        assert_eq!(
            super::REQUEST_PROTOCOL,
            "mycelium.native_resident_request.v1"
        );
        assert_eq!(super::PROOF_PROTOCOL, "mycelium.native_resident_proof.v1");
    }
}
