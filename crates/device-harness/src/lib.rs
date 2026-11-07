// SPDX-License-Identifier: AGPL-3.0-only
//! R7 device harness. Pure policy and deterministic state machines, with effects
//! behind injected adapters. Fake qualification is not physical device evidence.
pub mod admission;
pub mod attempt;
pub mod audit;
pub mod bundle_admission;
pub mod cli;
pub mod effects;
pub mod evidence_inputs;
pub mod harness_budgets;
pub mod host_observer;
pub mod host_state;
pub mod host_tools;
pub mod http;
pub mod keeper;
pub mod lifecycle;
pub mod member;
pub mod member_watch;
pub mod minimum_os;
pub mod native;
pub mod native_files;
pub mod native_io;
pub mod native_os;
pub mod native_session;
pub mod native_staging;
pub mod native_wire;
pub mod observe;
pub mod ownership;
pub mod package;
pub mod package_driver;
pub mod preflight;
pub mod private_fs;
pub mod process;
pub mod release;
pub mod release_github;
pub mod relocation;
pub mod resume;
pub mod retention;
pub mod schedule;
pub mod site;
pub mod soak;
pub mod soak_driver;
pub mod stop_command;
pub mod stranger;
mod stranger_driver;
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Host {
    Owner,
    Member,
}
pub const HOSTS: [Host; 2] = [Host::Owner, Host::Member];

/// Only locally defined static codes may enter diagnostics; never raw I/O errors.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Fault {
    pub code: &'static str,
    pub kind: FaultKind,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FaultKind {
    Product,
    Observation,
    Admission,
    Cleanup,
    Input,
}
impl Fault {
    pub const fn new(code: &'static str, kind: FaultKind) -> Self {
        Self { code, kind }
    }
    pub fn incomplete(self) -> bool {
        self.kind != FaultKind::Product
    }
}
impl std::fmt::Display for Fault {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} ({:?})", self.code, self.kind)
    }
}
impl std::error::Error for Fault {}
pub type Result<T> = std::result::Result<T, Fault>;
