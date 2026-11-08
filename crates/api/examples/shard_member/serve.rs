// SPDX-License-Identifier: AGPL-3.0-only
//! Operator `serve`: one owner-signed route epoch through the library stage
//! server (`api::joined_shard::serve`) with this process's native custody.
use super::{Result, control::Operation, membership::Membership};
use api::joined_shard::{native::NativeShard, serve as stage};
use protocol::identity::Identity;
use runtime::iroh_host::LoopbackHost;
use serde_json::Value;
#[allow(clippy::too_many_arguments)]
pub fn serve(
    host: &LoopbackHost<'_>,
    rt: &tokio::runtime::Runtime,
    clock: &api::managed_carrier::HostClock,
    native: &mut NativeShard,
    member: &Membership,
    signer: &Identity,
    route_envelope: &Value,
    proof_digest: &str,
    operation: &Operation,
) -> Result<Value> {
    let current = || member.current(signer);
    stage::serve(
        &stage::Serve {
            host,
            runtime: rt,
            clock,
            current: &current,
            cancelled: operation.cancelled.clone(),
            deadline: operation.deadline,
        },
        native,
        route_envelope,
        proof_digest,
    )
}
