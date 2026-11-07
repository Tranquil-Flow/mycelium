// SPDX-License-Identifier: AGPL-3.0-only
//! Pure compatibility facade. Only extracted wire modules are available here;
//! native networking, compute and node APIs await their owning lanes.
pub use control;
pub use protocol::{
    artifact_grant, assignment, canonical, gossip, identity, live_status, loaded_proof, managed,
    membership, operator_projection, planner_snapshot, public_evidence, remote, route_contract,
    strict,
};
