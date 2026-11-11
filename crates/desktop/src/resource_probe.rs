// SPDX-License-Identifier: AGPL-3.0-only
//! The answers of the packaged `native/resource_probe` helper (`resource_probe.m` @ f4e366e9, the
//! v0.1.4 one-shot, read-only observation the app runs beside its executable; `api::resources`
//! parses it). The helper binary (`src/bin/resource_probe.rs`) only reads the three OS facts; every
//! decision about what they mean is here, so it is testable without the OS:
//!
//! * power: `/usr/bin/pmset -g batt` exit 0 and its text names the power source
//!   (`powerObservation`, resource_probe.m:20-49);
//! * thermal: `NSProcessInfo.thermalState` (`thermalObservation`, :6-18);
//! * network: the first `nw_path_monitor` update within the wait (`networkObservation`, :51-80).
//!
//! Any failure is `unknown`, as in v0.1.4; the line printed is the helper's exact output shape.

/// What pmset said. `None` = pmset could not be started or did not exit 0 (`unknown`).
pub fn power(_pmset_stdout: Option<&[u8]>) -> &'static str {
    "unknown"
}
/// `NSProcessInfoThermalState` (`NSInteger`).
pub fn thermal(_state: isize) -> &'static str {
    "unknown"
}
pub const NW_PATH_STATUS_SATISFIED: i32 = 1;
pub const NW_PATH_STATUS_UNSATISFIED: i32 = 2;
pub const NW_PATH_STATUS_SATISFIABLE: i32 = 3;
/// One path update, `(status, is_expensive, is_constrained)`.
pub fn network(_update: Option<(i32, bool, bool)>) -> &'static str {
    "unknown"
}
/// The helper's stdout (RED stub: not yet the v0.1.4 shape).
pub fn line(power: &str, thermal: &str, network: &str) -> String {
    format!("{power} {thermal} {network}\n")
}
