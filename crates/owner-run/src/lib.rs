// SPDX-License-Identifier: AGPL-3.0-only
//! The owner run chain's orchestrator core (SE-L10-OWNER-RUN): `owner_run.OwnerRun` as a sans-IO
//! state machine, the `demo_app.action` refusal ladder and the execution ceiling. No clock, no
//! thread, no socket: hosts (`api` headless, `node-service` desktop) drive it. Design and step map:
//! `runs/v2/v02/OWNER-RUN.md`.
pub mod action;
pub mod ceiling;
pub mod chain;
pub mod machine;
pub mod owner_chain;
pub mod runner;
pub mod state;
pub mod status;
pub mod steps;

pub use machine::{Clock, Cmd, Identity, OwnerRun, OwnerRunError, Reason, State, StepOutcome};
