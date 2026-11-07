// SPDX-License-Identifier: AGPL-3.0-only
//! Port of the v0.1.4 `mycelium_layer_planner` snapshot planner and topology replanner
//! (`plan_snapshot`, `replan_for_event`, `simulate_bundle`) at f4e366e9.
//!
//! This is the evidence-snapshot route planner (`mycelium.route_plan.v2`), distinct from
//! the A13 resource placement in [`crate::placement`]. Output is placement intent only.
pub mod contracts;
pub mod plan;
pub mod replan;
pub mod replicate;
pub mod search;

pub use plan::{RoutePlan, plan_snapshot};
pub use replan::{Outcome, TopologyEvent, replan_for_event, simulate_bundle, simulate_bundle_file};
