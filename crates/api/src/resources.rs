// SPDX-License-Identifier: AGPL-3.0-only
//! Resource policy gate: `resources.py` (`policy_violation`, `_parse_probe_output`, `observe_policy`) @ f4e366e9.
//! `policy_violation` and the helper's output schema are exact; the observation itself comes from a one-shot,
//! read-only native helper (`native/resource_probe`, `resource_probe.m`) run with a bound, failing closed to
//! unknowns. This crate has no Foundation bindings, so the helper is a seam (SE-L10-RESOURCE-PROBE).
use budgets::{BudgetId, Entries};
use serde_json::Value;
use std::{
    io::Read,
    path::PathBuf,
    process::{Command, Stdio},
    time::{Duration, Instant},
};

/// `MAX_PROBE_OUTPUT_BYTES` (resources.py:21) and `PROBE_TIMEOUT_SECONDS` (:22): registry ids (SE-L10-ACQ-BOUNDS, L1b).
fn max_probe_output_bytes() -> usize {
    BudgetId::PyMyceliumA13MacosResourcesMaxProbeOutputBytesL21
        .bytes()
        .map_or(0, |b| usize::try_from(b.get()).unwrap_or(usize::MAX))
}
fn probe_timeout() -> Duration {
    BudgetId::PyMyceliumA13MacosResourcesProbeTimeoutSecondsL22
        .span(Entries::ZERO)
        .map_or(Duration::ZERO, |s| Duration::from_millis(s.millis()))
}
const POWER: [&str; 3] = ["external", "battery", "unknown"];
const THERMAL: [&str; 5] = ["nominal", "fair", "serious", "critical", "unknown"];
const NETWORK: [&str; 4] = ["unmetered", "metered", "unavailable", "unknown"];

/// One read-only snapshot of power, thermal state and network.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Observation {
    pub power: String,
    pub thermal: String,
    pub network: String,
}
impl Observation {
    /// `UNKNOWN_OBSERVATION`.
    pub fn unknown() -> Self {
        Self {
            power: "unknown".into(),
            thermal: "unknown".into(),
            network: "unknown".into(),
        }
    }
    /// The status document's `resource_observation` (`{power, thermal, network}`).
    pub fn to_value(&self) -> Value {
        serde_json::json!({"power": self.power, "thermal": self.thermal, "network": self.network})
    }
}
/// `_parse_probe_output`: the helper's bounded exact JSON schema, else unknowns.
pub fn parse_probe_output(payload: &[u8]) -> Observation {
    if payload.len() > max_probe_output_bytes() {
        return Observation::unknown();
    }
    let Ok(Value::Object(map)) = serde_json::from_slice::<Value>(payload) else {
        return Observation::unknown();
    };
    if map.len() != 3 {
        return Observation::unknown();
    }
    let pick = |key: &str, allowed: &[&str]| -> Option<String> {
        map.get(key)
            .and_then(Value::as_str)
            .filter(|v| allowed.contains(v))
            .map(str::to_owned)
    };
    match (
        pick("power", &POWER),
        pick("thermal", &THERMAL),
        pick("network", &NETWORK),
    ) {
        (Some(power), Some(thermal), Some(network)) => Observation {
            power,
            thermal,
            network,
        },
        _ => Observation::unknown(),
    }
}
/// `_resource_check`'s normalisation: anything but a dict of exactly the three string keys is the unknown
/// observation (its values may still be outside the enums: `policy_violation` then answers
/// `resource_observation_invalid`).
pub fn observation_from_value(value: &Value) -> Observation {
    let Some(map) = value.as_object() else {
        return Observation::unknown();
    };
    let text = |key: &str| map.get(key).and_then(Value::as_str).map(str::to_owned);
    match (map.len(), text("power"), text("thermal"), text("network")) {
        (3, Some(power), Some(thermal), Some(network)) => Observation {
            power,
            thermal,
            network,
        },
        _ => Observation::unknown(),
    }
}
/// `policy_violation(policy, observation)`: one bounded guard error code, or `None` when the policy permits.
pub fn policy_violation(policy: &Value, observation: &Observation) -> Option<&'static str> {
    let field = |name: &str| policy.get(name).and_then(Value::as_str);
    if !policy.is_object()
        || !matches!(field("power"), Some("ac_only" | "battery_allowed"))
        || field("thermal") != Some("stop_on_serious")
        || !matches!(field("network"), Some("unmetered_only" | "metered_allowed"))
    {
        return Some("resource_policy_invalid");
    }
    if !POWER.contains(&observation.power.as_str())
        || !THERMAL.contains(&observation.thermal.as_str())
        || !NETWORK.contains(&observation.network.as_str())
    {
        return Some("resource_observation_invalid");
    }
    if field("power") == Some("ac_only") && observation.power != "external" {
        return Some("resource_power_policy_violation");
    }
    if matches!(
        observation.thermal.as_str(),
        "serious" | "critical" | "unknown"
    ) {
        return Some("resource_thermal_policy_violation");
    }
    if matches!(observation.network.as_str(), "unavailable" | "unknown") {
        return Some("resource_network_policy_violation");
    }
    if field("network") == Some("unmetered_only") && observation.network != "unmetered" {
        return Some("resource_network_policy_violation");
    }
    None
}
/// Where the observation comes from.
pub trait Observer: Send + Sync {
    fn observe(&self) -> Observation;
}
/// `observe_policy` on macOS: run the first safe candidate helper once; anything wrong is unknown.
pub struct ProbeObserver {
    pub candidates: Vec<PathBuf>,
}
impl ProbeObserver {
    /// `<dir of the running executable>/native/resource_probe` (resources.py `_resource_probe_candidates`).
    pub fn beside_executable() -> Self {
        let candidates = std::env::current_exe()
            .ok()
            .and_then(|exe| {
                exe.parent()
                    .map(|d| d.join("native").join("resource_probe"))
            })
            .into_iter()
            .collect();
        Self { candidates }
    }
}
fn poll() -> Duration {
    budgets::BudgetId::OwnerPoll
        .span(budgets::Entries::new(0))
        .map_or(Duration::ZERO, |s| Duration::from_millis(s.millis()))
}
fn safe_executable(path: &std::path::Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::symlink_metadata(path).is_ok_and(|m| {
        m.is_file() && !m.file_type().is_symlink() && m.permissions().mode() & 0o100 != 0
    })
}
impl Observer for ProbeObserver {
    fn observe(&self) -> Observation {
        for candidate in &self.candidates {
            if !safe_executable(candidate) {
                continue;
            }
            let Ok(mut child) = Command::new(candidate)
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()
            else {
                continue;
            };
            let Some(mut stdout) = child.stdout.take() else {
                continue;
            };
            let reader = std::thread::spawn(move || {
                let mut out = Vec::new();
                match stdout
                    .by_ref()
                    .take(max_probe_output_bytes() as u64 + 1)
                    .read_to_end(&mut out)
                {
                    Ok(_) => out,
                    Err(_) => Vec::new(),
                }
            });
            let deadline = Instant::now() + probe_timeout();
            let status = loop {
                match child.try_wait() {
                    Ok(Some(status)) => break Some(status),
                    Ok(None) if Instant::now() < deadline => {
                        std::thread::sleep(poll());
                    }
                    _ => {
                        // Timed out (or unwaitable): stop the exact child, as `subprocess.run(timeout=)` does.
                        if child.kill().is_ok() && child.wait().is_err() {
                            return Observation::unknown();
                        }
                        break None;
                    }
                }
            };
            let output = reader.join().unwrap_or_default();
            if status.is_some_and(|s| s.success()) {
                return parse_probe_output(&output);
            }
        }
        Observation::unknown()
    }
}
