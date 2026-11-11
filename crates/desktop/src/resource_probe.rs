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
pub fn power(pmset_stdout: Option<&[u8]>) -> &'static str {
    // `[[NSString alloc] initWithData:… encoding:NSUTF8StringEncoding]` is nil for bytes that are
    // not UTF-8, and `containsString:` on nil is NO: such output is `unknown`.
    let Some(text) = pmset_stdout.and_then(|bytes| std::str::from_utf8(bytes).ok()) else {
        return "unknown";
    };
    if text.contains("Now drawing from 'AC Power'") {
        "external"
    } else if text.contains("Now drawing from 'Battery Power'") {
        "battery"
    } else {
        "unknown"
    }
}

/// `NSProcessInfoThermalState` (`NSInteger`): Nominal 0, Fair 1, Serious 2, Critical 3.
pub fn thermal(state: isize) -> &'static str {
    match state {
        0 => "nominal",
        1 => "fair",
        2 => "serious",
        3 => "critical",
        _ => "unknown",
    }
}

/// `nw_path_status_t` values (Network/path.h): invalid 0, satisfied 1, unsatisfied 2, satisfiable 3.
pub const NW_PATH_STATUS_SATISFIED: i32 = 1;
pub const NW_PATH_STATUS_UNSATISFIED: i32 = 2;
pub const NW_PATH_STATUS_SATISFIABLE: i32 = 3;

/// One path update, `(status, is_expensive, is_constrained)`; `None` = no update arrived in the wait.
pub fn network(update: Option<(i32, bool, bool)>) -> &'static str {
    match update {
        Some((NW_PATH_STATUS_SATISFIED, expensive, constrained)) => {
            if expensive || constrained {
                "metered"
            } else {
                "unmetered"
            }
        }
        Some((NW_PATH_STATUS_UNSATISFIED | NW_PATH_STATUS_SATISFIABLE, _, _)) => "unavailable",
        _ => "unknown",
    }
}

/// The helper's stdout: `NSJSONSerialization` of the three-key dictionary (compact, the key order
/// the v0.1.4 helper prints) and a newline. The values are always from the fixed vocabularies above.
pub fn line(power: &str, thermal: &str, network: &str) -> String {
    format!("{{\"power\":\"{power}\",\"thermal\":\"{thermal}\",\"network\":\"{network}\"}}\n")
}
