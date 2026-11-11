// SPDX-License-Identifier: AGPL-3.0-only
//! Additive private worker-link command (class A, lane EP0): reset the stage
//! session of a PREPARED native worker between requests of one route epoch.
//!
//! The request is exactly `{"command": COMMAND}` with no payload. The worker
//! releases its one session (dropping that request's KV), synchronizes and
//! clears its allocator cache, refuses with `native_residue` if more memory is
//! active than right after prepare (the resident weights), and opens a fresh
//! session under the SAME prepare binding and limits. Weights stay resident.
//! The reply is `{"ok":true,"session":{..}}` whose object has exactly
//! REPLY_FIELDS; `active_bytes <= baseline_bytes` always holds in a valid reply.
//! Any refusal ends the worker, as for every other command. Existing
//! prepare/step/close/resident-proof frames are unchanged.
pub const COMMAND: &str = "session_reset_v1";
pub const REPLY_PROTOCOL: &str = "mycelium.native_session_reset.v1";
pub const REPLY_FIELDS: &[&str] = &[
    "protocol",
    "state_generation",
    "active_bytes",
    "baseline_bytes",
];
#[cfg(test)]
mod tests {
    #[test]
    fn the_reset_command_is_distinct_from_every_existing_worker_command() {
        for existing in [
            "prepare",
            "step",
            "close",
            crate::native_proof::COMMAND,
            crate::native_shard::COMMAND,
        ] {
            assert_ne!(super::COMMAND, existing);
        }
        assert!(super::REPLY_PROTOCOL.ends_with(".v1"));
    }
}
