// SPDX-License-Identifier: AGPL-3.0-only
//! `_step_serving` (`demo_chain.py:987-1106 @ f4e366e9`): wait until the run's own workflow serves,
//! require that its startup challenge qualified the route, then publish `serving`.
use super::exchange::ReportsPort;
use super::publish::WorkPort;
use crate::chain::{StepEnv, StepError};
use crate::state::RunState;

pub fn run_serving_step(
    _env: &mut dyn StepEnv,
    _reports: &mut dyn ReportsPort,
    _workflow: &mut dyn WorkPort,
    _state: &mut RunState,
    _interval: f64,
) -> Result<(), StepError> {
    Err(super::not_ported("serving"))
}
