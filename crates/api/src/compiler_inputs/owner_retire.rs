// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's retirement of dead compile runs from its storage ledger (lane PHY item 1).
use super::*;

/// What one retirement pass did: the runs whose entries it removed, and the runs it left charged
/// with the reason (never silently).
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Retirement {
    pub retired: Vec<String>,
    pub refused: Vec<(String, &'static str)>,
}

/// RED stub: retires nothing yet.
pub fn retire_dead_runs(_host: &Path) -> Result<Retirement> {
    Ok(Retirement::default())
}
