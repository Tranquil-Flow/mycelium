// SPDX-License-Identifier: AGPL-3.0-only
//! Additive packaged already-joined work rendezvous. Existing member-control
//! and joined-shard signed formats are unchanged. The app's enrolled key signs
//! this message inside member_control.signed.v1; addresses are never unsigned.
pub const PROTOCOL: &str = "mycelium.packaged_member_work.v1";
pub const PATH: &str = "/member-work/v1/register";
pub const ACK: &str = "mycelium.packaged_member_work_ack.v1";
/// Bounded volatile rendezvous inbox; one live binding per enrolled member.
pub const MAX_MEMBERS: usize = 256;
pub const FIELDS: &[&str] = &[
    "protocol",
    "registration",
    "control_endpoint_id",
    "direct_addrs",
    "owner_endpoint_id",
    "run_id",
    "deployment_id",
    "issued_at",
    "expires_at",
];
