// SPDX-License-Identifier: AGPL-3.0-only
//! Class-A member-signed resident-proof binding. The joined-shard envelope's
//! existing signature covers this entire message. The seed's current enrollment,
//! not a self-declared key, authorizes verification. This is trusted-local-worker
//! custody evidence, not hostile-code attestation or physical route readiness.
//! Legacy native prepare/step/close frames and artifact-grant bytes are unchanged.
pub const RESIDENT_PROTOCOL: &str = "mycelium.joined_native.resident.v1";
pub const RESIDENT_FIELDS: &[&str] = &[
    "protocol",
    "member",
    "offer_digest",
    "prepare",
    "proof",
    "member_pid",
    "route_ready",
];
