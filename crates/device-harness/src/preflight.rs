// SPDX-License-Identifier: AGPL-3.0-only
use crate::admission::*;
use crate::{Fault, FaultKind, Result};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Marker {
    pub name: String,
    pub device: u64,
    pub inode: u64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Preflight {
    pub power: Power,
    pub network: NetworkClass,
    pub consent: NetworkConsent,
    pub free_bytes: u64,
    pub memory_level: u8,
    pub current: Option<String>,
    pub previous: Option<String>,
    pub program_directories: BTreeSet<String>,
    pub busy: Vec<Marker>,
    pub owned_marker: Option<Marker>,
    pub attempt_spent: bool,
    pub state_readable: bool,
}
pub fn check(v: &Preflight, target: &str) -> Result<()> {
    admit_power(v.power, true)?;
    admit_network(v.network, v.consent)?;
    let refusal = if !v.state_readable {
        Some("preflight_state_unreadable")
    } else if v.attempt_spent {
        Some("attempt_already_spent")
    } else if v
        .busy
        .iter()
        .any(|m| m.name != "DEVICE.busy" || Some(m) != v.owned_marker.as_ref())
    {
        Some("preflight_busy_marker")
    } else if v.free_bytes < 25 * 1024 * 1024 * 1024 || v.memory_level != 1 {
        Some("resource_admission_failed")
    } else if v
        .program_directories
        .iter()
        .any(|s| Some(s) != v.current.as_ref() && Some(s) != v.previous.as_ref())
    {
        Some(
            if v.program_directories.contains(target)
                && v.current.as_deref() != Some(target)
                && v.previous.as_deref() != Some(target)
            {
                "preflight_target_version_stray"
            } else {
                "preflight_stray_programs"
            },
        )
    } else {
        None
    };
    match refusal {
        Some(c) => Err(Fault::new(c, FaultKind::Admission)),
        None => Ok(()),
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ProcessIdentity {
    pub pid: u32,
    pub birth: String,
    pub executable: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct FreshIdentity {
    pub launch_epoch_ms: u64,
    pub control_modified_ms: u64,
    pub expected: ProcessIdentity,
    pub listener: Option<ProcessIdentity>,
    pub control_port: u16,
    pub status_port: u16,
    pub status_http: u16,
    pub status_observed_ms: u64,
    pub expected_exe_sha256: String,
    pub observed_exe_sha256: String,
}
pub fn valid_digest(value: &str, len: usize) -> bool {
    value.len() == len
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
pub fn fresh(v: &FreshIdentity) -> Result<()> {
    if v.control_modified_ms < v.launch_epoch_ms
        || v.status_observed_ms < v.launch_epoch_ms
        || v.status_http != 200
        || v.control_port == 0
        || v.status_port != v.control_port
        || v.expected.pid <= 1
        || v.expected.birth.is_empty()
        || v.expected.executable.is_empty()
        || v.listener.as_ref() != Some(&v.expected)
        || !valid_digest(&v.expected_exe_sha256, 64)
        || v.observed_exe_sha256 != v.expected_exe_sha256
    {
        return Err(Fault::new(
            "fresh_listener_status_identity_unproven",
            FaultKind::Observation,
        ));
    }
    Ok(())
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum InstallDecision {
    Install,
    AlreadyIdentical,
    SetAsidePrevious {
        version: String,
        expected_old_digest: String,
    },
}
#[derive(Clone, Debug)]
pub struct InstallState {
    pub target: String,
    pub current: String,
    pub previous: Option<String>,
    pub expected: String,
    pub existing: Option<String>,
    pub app_running: bool,
}
pub fn install_decision(s: &InstallState) -> Result<InstallDecision> {
    if s.app_running {
        return Err(Fault::new("app_still_running", FaultKind::Admission));
    }
    if !valid_digest(&s.expected, 64) || s.existing.as_ref().is_some_and(|d| !valid_digest(d, 64)) {
        return Err(Fault::new("invalid_digest", FaultKind::Input));
    }
    match &s.existing {
        None => Ok(InstallDecision::Install),
        Some(d) if d == &s.expected => Ok(InstallDecision::AlreadyIdentical),
        Some(_) if s.current == s.target => Err(Fault::new(
            "rollback_current_before_set_aside",
            FaultKind::Admission,
        )),
        Some(d) if s.previous.as_ref() == Some(&s.target) => {
            Ok(InstallDecision::SetAsidePrevious {
                version: s.target.clone(),
                expected_old_digest: d.clone(),
            })
        }
        Some(_) => Err(Fault::new(
            "preflight_target_version_stray",
            FaultKind::Admission,
        )),
    }
}

pub fn cached(a: &serde_json::Value) -> Result<()> {
    if a["downloaded_bytes"].as_u64() == Some(999_604_126)
        && a["total_bytes"].as_u64() == Some(999_604_126)
        && matches!(
            a["state"].as_str(),
            Some("prepared" | "closed" | "failed" | "idle")
        )
        && (a["error_code"].is_null() || a["error_code"] == "prepared_source_revalidation_required")
    {
        Ok(())
    } else {
        Err(Fault::new("model_not_cached", FaultKind::Admission))
    }
}
pub fn cache_complete(a: &serde_json::Value) -> bool {
    matches!(a["state"].as_str(), Some("prepared" | "closed"))
        && a["error_code"].is_null()
        && a["total_bytes"]
            .as_u64()
            .is_some_and(|n| n > 0 && a["downloaded_bytes"].as_u64() == Some(n))
}
