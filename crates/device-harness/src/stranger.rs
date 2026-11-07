// SPDX-License-Identifier: AGPL-3.0-only
//! Fresh install state machine. Effects are capabilities, not shell snippets;
//! no app launch, SSH or model download is possible without a supplied adapter.
use crate::{
    Fault, FaultKind, Host, Result,
    effects::Clock,
    http::{Chat, Request},
    observe::{Observer, SnapshotStore},
    preflight::*,
};
use budgets::Span;
use serde::{Deserialize, Serialize};
use serde_json::Value;
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Candidate {
    pub version: String,
    pub source_sha: String,
    pub zip_sha256: String,
    pub exe_sha256: String,
}
impl Candidate {
    pub fn validate(&self) -> Result<()> {
        let (base, suffix) = self
            .version
            .split_once('-')
            .map_or((self.version.as_str(), None), |(base, suffix)| {
                (base, Some(suffix))
            });
        let version_ok = !self.version.is_empty()
            && self
                .version
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-'))
            && base.split('.').count() == 3
            && base
                .split('.')
                .all(|part| !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit()))
            && suffix.is_none_or(|s| !s.is_empty());
        if !version_ok
            || !valid_digest(&self.source_sha, 40)
            || !valid_digest(&self.zip_sha256, 64)
            || !valid_digest(&self.exe_sha256, 64)
        {
            return Err(Fault::new("invalid_candidate_identity", FaultKind::Input));
        }
        Ok(())
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CacheRole {
    Owner,
    Target,
}
/// Not Debug/Serialize: HTTP bodies contain private join material.
pub enum Effect {
    VerifyPackage,
    OwnerPower,
    Preflight(Host),
    InspectInstall(Host),
    SetAside {
        host: Host,
        version: String,
        old_digest: String,
    },
    Install(Host),
    Quit(Host),
    MoveFresh(Host),
    Launch(Host),
    Seed {
        host: Host,
        role: CacheRole,
    },
    Api {
        host: Host,
        request: Request,
    },
    Chat {
        index: usize,
    },
}
pub enum Reply {
    Package(Candidate),
    Power(crate::admission::Power),
    Preflight(Preflight),
    InstallState(InstallState),
    SetAside {
        version: String,
        digest: String,
        outside_programs: bool,
        versions_backup: bool,
        current_unchanged: bool,
    },
    Installed {
        version: String,
        exe_sha256: String,
    },
    Quit {
        clean: bool,
        listener_gone: bool,
    },
    Fresh {
        moved: bool,
    },
    Launch(FreshIdentity),
    Seed {
        cloned: usize,
    },
    Http(u16, Value),
    Chat(Chat),
}
pub trait Adapter: Observer {
    fn perform(&mut self, effect: Effect, bound: Span) -> Result<Reply>;
    /// Check both hosts; a real blocking adapter must also honor this watch while
    /// a call is in flight. No implicit default that could disable the guard.
    fn runtime_guard(&mut self) -> Result<()>;
    fn battery_percentages(&self) -> [Option<u8>; 2];
}
#[derive(Clone, Copy, Debug)]
pub struct Limits {
    pub total: Span,
    pub action: Span,
    pub pair: Span,
    pub serving: Span,
    pub configuration: Span,
    pub poll: Span,
    pub relay_window: Span,
    pub member_wall: Span,
    pub pair_window: Span,
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Evidence {
    #[serde(default)]
    pub initial_owner_power: Option<crate::admission::Power>,
    pub verified_package: Option<Candidate>,
    pub admissions: Vec<(Host, Preflight)>,
    #[serde(default)]
    pub preflight_errors: Vec<Value>,
    pub installs: Vec<(Host, String, String)>,
    pub fresh_launches: usize,
    pub fresh_states: usize,
    pub configuration_observed: bool,
    pub member_current: bool,
    pub serving: bool,
    pub final_serving: bool,
    pub chats: Vec<Chat>,
    pub steps: Vec<String>,
    pub outcome: String,
    pub first_failure: Option<String>,
    #[serde(default)]
    pub battery_stop_percentages: [Option<u8>; 2],
    pub snapshot_error: Option<String>,
    pub cleanup_errors: Vec<String>,
    pub product_failed: Option<bool>,
    pub host_samples: Vec<Value>,
}
pub fn evaluate(candidate: &Candidate, e: &Evidence) -> bool {
    candidate.validate().is_ok()
        && e.verified_package.as_ref() == Some(candidate)
        && crate::HOSTS.iter().all(|h| {
            e.admissions.iter().filter(|(host, _)| host == h).count() == 1
                && e.admissions
                    .iter()
                    .filter(|(host, _)| host == h)
                    .all(|(_, p)| check(p, &candidate.version).is_ok())
                && e.installs.iter().any(|(host, v, d)| {
                    host == h && v == &candidate.version && d == &candidate.exe_sha256
                })
        })
        && e.fresh_states == 2
        && e.fresh_launches >= 5
        && e.configuration_observed
        && e.member_current
        && e.serving
        && e.final_serving
        && e.chats.len() == 3
        && e.chats.iter().all(Chat::ok)
        && !e.host_samples.is_empty()
        && e.first_failure.is_none()
        && e.cleanup_errors.is_empty()
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Relay {
    Quick,
    Demo,
}
pub fn run_with_relay<A: Adapter, S: SnapshotStore, C: Clock>(
    io: &mut A,
    store: &mut S,
    clock: &mut C,
    candidate: Candidate,
    limits: Limits,
    relay: Relay,
) -> Evidence {
    crate::stranger_driver::run(io, store, clock, candidate, limits, relay)
}
pub struct DeviceOptions {
    pub input: crate::package_driver::DeviceInput,
    pub staging_root: String,
    pub attempt_id: String,
    pub staging_limits: crate::package_driver::StageLimits,
    pub limits: Limits,
    pub relay: Relay,
}
pub fn run_device_owned<
    I: crate::package_driver::StageIo,
    A: Adapter,
    S: SnapshotStore,
    C: Clock,
>(
    stager: &mut I,
    io: &mut A,
    store: &mut S,
    clock: &mut C,
    options: DeviceOptions,
    attempt: crate::attempt::Attempt,
) -> std::result::Result<Value, crate::attempt::FinalizeFailure> {
    let result = crate::package_driver::with_device(
        stager,
        &options.input,
        &options.staging_root,
        &options.attempt_id,
        options.staging_limits,
        |prepared| {
            let e = run_with_relay(
                io,
                store,
                clock,
                prepared.candidate.clone(),
                options.limits,
                options.relay,
            );
            let mut value = serde_json::to_value(&e).map_err(|_| {
                Fault::new("stranger_result_encoding_failed", FaultKind::Observation)
            })?;
            value["stranger_path_passed"] = Value::Bool(e.outcome == "passed");
            value["incomplete"] = Value::Bool(e.outcome == "incomplete");
            value["error"] = e.first_failure.map(Value::String).unwrap_or(Value::Null);
            Ok(value)
        },
    );
    let value = match result {
        Ok(v) => v,
        Err(e) => {
            serde_json::json!({"stranger_path_passed":false,"outcome":if e.incomplete(){"incomplete"}else{"failed"},"incomplete":e.incomplete(),"error":e.code,"release_qualified":false})
        }
    };
    attempt.finish(&value)?;
    Ok(value)
}
pub fn run_owned<A: Adapter, S: SnapshotStore, C: Clock>(
    io: &mut A,
    store: &mut S,
    clock: &mut C,
    candidate: Candidate,
    limits: Limits,
    attempt: crate::attempt::Attempt,
) -> std::result::Result<Evidence, crate::attempt::FinalizeFailure> {
    let evidence = run(io, store, clock, candidate, limits);
    let value = serde_json::to_value(&evidence);
    // Finish even if evidence serialization fails: retain a static failure and release the lease.
    match value {
        Ok(mut value) => {
            value["release_qualified"] = Value::Bool(false);
            value["stranger_path_passed"] = Value::Bool(evidence.outcome == "passed");
            value["incomplete"] = Value::Bool(evidence.outcome == "incomplete");
            value["error"] = evidence
                .first_failure
                .clone()
                .map(Value::String)
                .unwrap_or(Value::Null);
            attempt.finish(&value)?;
            Ok(evidence)
        }
        Err(_) => {
            let first = crate::Fault::new(
                "stranger_result_encoding_failed",
                crate::FaultKind::Observation,
            );
            let cleanup = attempt.finish(&serde_json::json!({"outcome":"incomplete","error":first.code,"release_qualified":false}));
            Err(crate::attempt::FinalizeFailure {
                first,
                cleanup: cleanup.err().map(|f| f.first),
            })
        }
    }
}
pub fn run<A: Adapter, S: SnapshotStore, C: Clock>(
    io: &mut A,
    store: &mut S,
    clock: &mut C,
    candidate: Candidate,
    limits: Limits,
) -> Evidence {
    run_with_relay(io, store, clock, candidate, limits, Relay::Quick)
}
