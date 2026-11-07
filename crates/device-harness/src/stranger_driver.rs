// SPDX-License-Identifier: AGPL-3.0-only
use crate::{
    Fault, FaultKind, HOSTS, Host, Result,
    effects::Clock,
    http::{self, Method, Request},
    observe::SnapshotStore,
    observe::failure_snapshot,
    preflight::{self, InstallDecision},
    soak::Run,
    stranger::*,
};
use budgets::Span;
use serde_json::{Value, json};

fn shape() -> Fault {
    Fault::new("adapter_reply_mismatch", FaultKind::Observation)
}
fn require(ok: bool, code: &'static str, kind: FaultKind) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(Fault::new(code, kind))
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Phase {
    Preflight,
    Install,
    Configure,
    Pair,
    SeedMember,
    Serve,
    Chat,
    Finish,
}
struct Driver<'a, A, C> {
    io: &'a mut A,
    clock: &'a mut C,
    candidate: Candidate,
    limits: Limits,
    evidence: Evidence,
    relay: Relay,
    end: u64,
    phase_end: u64,
    phase: Phase,
    admitted: bool,
    owner_launched: bool,
    secrets: Vec<String>,
}
impl<A: Adapter, C: Clock> Driver<'_, A, C> {
    fn phase(&mut self, phase: Phase) -> Result<()> {
        self.phase = phase;
        let bound = match phase {
            Phase::Configure => self.limits.configuration,
            Phase::Pair => self.limits.pair,
            Phase::Serve => self.limits.serving,
            _ => self.limits.total,
        };
        self.phase_end = self
            .clock
            .now()
            .checked_add(bound.millis())
            .ok_or(shape())?
            .min(self.end);
        self.evidence.steps.push(format!("{phase:?}"));
        Ok(())
    }
    fn bound(&self) -> Result<Span> {
        let now = self.clock.now();
        let end = self.end.min(self.phase_end);
        if now >= end {
            return Err(Fault::new(
                "attempt_or_phase_deadline",
                FaultKind::Admission,
            ));
        }
        Ok(self.limits.action.min(Span::from_millis(end - now)))
    }
    fn perform(&mut self, e: Effect) -> Result<Reply> {
        if self.admitted {
            self.io.runtime_guard()?;
        }
        let bound = self.bound()?;
        let before = self.clock.now();
        let r = self.io.perform(e, bound)?;
        require(
            self.clock.now() >= before && self.clock.now() - before <= bound.millis(),
            "adapter_operation_bound_exceeded",
            FaultKind::Observation,
        )?;
        Ok(r)
    }
    fn pause(&mut self) -> Result<()> {
        let bound = self.bound()?;
        let before = self.clock.now();
        self.clock.sleep(bound.min(self.limits.poll))?;
        require(
            self.clock.now() > before,
            "clock_not_advancing",
            FaultKind::Observation,
        )
    }
    fn metrics(&mut self) -> Result<()> {
        let mut failure = None;
        let mut samples = Vec::new();
        for host in HOSTS {
            let metrics = match self.io.metrics(host) {
                Ok(m) => {
                    if !m.valid() {
                        failure.get_or_insert(Fault::new(
                            "host_metrics_unobserved",
                            FaultKind::Observation,
                        ));
                    }
                    json!(m)
                }
                Err(f) => {
                    failure.get_or_insert(f);
                    json!({"error":f.code,"kind":f.kind})
                }
            };
            let mailbox = match self.io.mailbox(host) {
                Ok(m) => json!(m),
                Err(f) => {
                    failure.get_or_insert(f);
                    json!({"error":f.code,"kind":f.kind})
                }
            };
            samples.push(json!({"host":host,"metrics":metrics,"mailbox":mailbox}));
        }
        self.evidence.host_samples.push(crate::observe::redact(
            &json!({"elapsed_clock_ms":self.clock.now(),"hosts":samples}),
            &self.secrets,
        ));
        match failure {
            Some(f) => Err(f),
            None => Ok(()),
        }
    }
    fn api(&mut self, host: Host, path: &'static str, body: Option<Value>) -> Result<Value> {
        let is_status = path == "/api/status";
        let r = self.perform(Effect::Api {
            host,
            request: Request {
                path,
                method: if body.is_some() {
                    Method::Post
                } else {
                    Method::Get
                },
                body,
                chat: false,
            },
        });
        // Observation failure cannot suppress load/mailbox collection from either host.
        let metrics = if is_status { self.metrics() } else { Ok(()) };
        let (code, value) = match r? {
            Reply::Http(c, v) => (c, v),
            _ => return Err(shape()),
        };
        metrics?;
        require(
            code == 200 && value["error"].is_null(),
            if is_status {
                "status_refused"
            } else {
                "action_refused"
            },
            if is_status {
                FaultKind::Observation
            } else {
                FaultKind::Product
            },
        )?;
        Ok(value)
    }
    fn quit(&mut self, h: Host) -> Result<()> {
        match self.perform(Effect::Quit(h))? {
            Reply::Quit {
                clean,
                listener_gone,
            } => require(
                clean && listener_gone,
                "apple_event_quit_unproven",
                FaultKind::Cleanup,
            ),
            _ => Err(shape()),
        }
    }
    fn launch(&mut self, h: Host) -> Result<()> {
        if h == Host::Owner {
            self.owner_launched = true;
        }
        match self.perform(Effect::Launch(h))? {
            Reply::Launch(id) => {
                require(
                    id.expected_exe_sha256 == self.candidate.exe_sha256,
                    "launch_candidate_mismatch",
                    FaultKind::Observation,
                )?;
                preflight::fresh(&id)?;
                self.evidence.fresh_launches += 1;
                Ok(())
            }
            _ => Err(shape()),
        }
    }
    fn seed(&mut self, h: Host, role: CacheRole) -> Result<()> {
        match self.perform(Effect::Seed { host: h, role })? {
            Reply::Seed { .. } => Ok(()),
            _ => Err(shape()),
        }
    }
    fn inspect(&mut self, h: Host) -> Result<preflight::InstallState> {
        match self.perform(Effect::InspectInstall(h))? {
            Reply::InstallState(s) => {
                require(
                    s.target == self.candidate.version && s.expected == self.candidate.exe_sha256,
                    "install_candidate_mismatch",
                    FaultKind::Admission,
                )?;
                Ok(s)
            }
            _ => Err(shape()),
        }
    }
    fn prepare_host(&mut self, h: Host) -> Result<()> {
        let mut state = self.inspect(h)?;
        if state.app_running {
            self.quit(h)?;
            state = self.inspect(h)?;
        }
        let decision = preflight::install_decision(&state)?;
        if let InstallDecision::SetAsidePrevious {
            version,
            expected_old_digest,
        } = decision.clone()
        {
            match self.perform(Effect::SetAside {
                host: h,
                version: version.clone(),
                old_digest: expected_old_digest.clone(),
            })? {
                Reply::SetAside {
                    version: v,
                    digest,
                    outside_programs,
                    versions_backup,
                    current_unchanged,
                } => require(
                    v == version
                        && digest == expected_old_digest
                        && outside_programs
                        && versions_backup
                        && current_unchanged,
                    "set_aside_unproven",
                    FaultKind::Cleanup,
                )?,
                _ => return Err(shape()),
            }
            let after = self.inspect(h)?;
            require(
                after.existing.is_none() && after.current == state.current && !after.app_running,
                "set_aside_readback_failed",
                FaultKind::Cleanup,
            )?;
        }
        if decision != InstallDecision::AlreadyIdentical {
            match self.perform(Effect::Install(h))? {
                Reply::Installed {
                    version,
                    exe_sha256,
                } => require(
                    version == self.candidate.version && exe_sha256 == self.candidate.exe_sha256,
                    "installed_identity_mismatch",
                    FaultKind::Observation,
                )?,
                _ => return Err(shape()),
            }
        }
        self.evidence.installs.push((
            h,
            self.candidate.version.clone(),
            self.candidate.exe_sha256.clone(),
        ));
        self.quit(h)?;
        match self.perform(Effect::MoveFresh(h))? {
            Reply::Fresh { moved } => require(moved, "cached_state_missing", FaultKind::Admission)?,
            _ => return Err(shape()),
        }
        self.evidence.fresh_states += 1;
        self.launch(h)?;
        self.quit(h)?;
        if h == Host::Owner {
            self.seed(h, CacheRole::Owner)?;
        }
        self.launch(h)
    }
    fn consent_body(&self, host: Host, mut body: Value) -> Result<Value> {
        use crate::admission::NetworkConsent;
        let consent = self
            .evidence
            .admissions
            .iter()
            .find(|(h, _)| *h == host)
            .ok_or(shape())?
            .1
            .consent;
        body["consent"]["network"] = json!(match consent {
            NetworkConsent::UnmeteredOnly => "unmetered_only",
            NetworkConsent::MeteredAllowed => "metered_allowed",
        });
        Ok(body)
    }
    fn configure(&mut self) -> Result<()> {
        self.phase(Phase::Configure)?;
        self.api(
            Host::Owner,
            "/api/create",
            Some(self.consent_body(
                Host::Owner,
                http::create_body(self.relay == Relay::Quick, self.limits.relay_window)?,
            )?),
        )?;
        if self.relay == Relay::Demo {
            self.api(
                Host::Owner,
                "/api/network/start",
                Some(crate::resume::relay_body("demo", self.limits.relay_window)?),
            )?;
        }
        let s = self.api(Host::Owner, "/api/status", None)?;
        if s["run_model"]["selected"] != "a9-route" {
            self.api(
                Host::Owner,
                "/api/run-model",
                Some(json!({"choice":"a9-route"})),
            )?;
        }
        self.api(Host::Owner, "/api/fast-mode", Some(json!({"enabled":true})))?;
        loop {
            let s = self.api(Host::Owner, "/api/status", None)?;
            if s["run_model"]["selected"] == "a9-route" && s["fast_mode"]["enabled"] == true {
                require(
                    s["run_model"]["locked"] != true && s["fast_mode"]["locked"] != true,
                    "environment_override_detected",
                    FaultKind::Admission,
                )?;
                preflight::cached(&s["acquisition"])?;
                self.evidence.configuration_observed = true;
                return Ok(());
            }
            self.pause()?;
        }
    }
    fn pair(&mut self) -> Result<()> {
        self.phase(Phase::Pair)?;
        require(
            self.limits.pair_window.millis().is_multiple_of(1000),
            "invalid_pair_window",
            FaultKind::Input,
        )?;
        self.api(
            Host::Owner,
            "/api/pair/window/open",
            Some(json!({"ttl_seconds":self.limits.pair_window.millis()/1000})),
        )?;
        let (window_id, link) = loop {
            let s = self.api(Host::Owner, "/api/status", None)?;
            if let Some(windows) = s["join_flow"]["pairing_windows"].as_array() {
                let candidates: Vec<_> = windows
                    .iter()
                    .filter(|w| {
                        matches!(w["state"].as_str(), Some("open" | "claimed"))
                            && w["join_link"].as_str().is_some_and(|s| !s.is_empty())
                    })
                    .collect();
                if candidates.len() == 1 {
                    let w = candidates[0];
                    let id = w["window_id"].as_str().ok_or(shape())?;
                    let link = w["join_link"].as_str().ok_or(shape())?;
                    break (id.to_owned(), link.to_owned());
                }
            }
            self.pause()?;
        };
        self.secrets.push(link.clone());
        let s = self.api(Host::Member, "/api/status", None)?;
        let digest = s["join_flow"]["standing_disclosure"]["digest"]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or(Fault::new(
                "standing_disclosure_missing",
                FaultKind::Observation,
            ))?;
        self.secrets.push(digest.into());
        self.api(
            Host::Member,
            "/api/join/window",
            Some(self.consent_body(
                Host::Member,
                http::join_body(&link, digest, self.limits.member_wall)?,
            )?),
        )?;
        // Interleave public owner/member calls rather than blocking on a remote
        // join process while the owner still needs to approve it.
        let mut approved = false;
        let mut joined = false;
        loop {
            if !approved {
                let s = self.api(Host::Owner, "/api/status", None)?;
                if let Some(windows) = s["join_flow"]["pairing_windows"].as_array() {
                    for w in windows.iter().filter(|w| w["window_id"] == window_id) {
                        require(
                            !matches!(w["state"].as_str(), Some("revoked" | "expired" | "locked")),
                            "pair_window_terminal",
                            FaultKind::Product,
                        )?;
                        if w["state"] == "claimed" {
                            let digest = w["pending"]["request_digest"]
                                .as_str()
                                .filter(|s| !s.is_empty())
                                .ok_or(Fault::new(
                                    "pair_request_digest_missing",
                                    FaultKind::Observation,
                                ))?;
                            self.secrets.push(digest.into());
                            self.api(
                                Host::Owner,
                                "/api/pair/window/approve",
                                Some(json!({"window_id":window_id,"request_digest":digest})),
                            )?;
                            approved = true;
                        }
                    }
                }
            }
            if !joined {
                let p = self.api(Host::Member, "/api/join/window/poll", Some(json!({})))?;
                require(
                    matches!(p["state"].as_str(), Some("waiting_for_owner" | "joined")),
                    "join_terminal_state",
                    FaultKind::Product,
                )?;
                joined = p["membership_state"] == "current" || p["state"] == "joined";
            }
            if joined {
                let s = self.api(Host::Member, "/api/status", None)?;
                if approved && s["authority"]["membership_state"] == "current" {
                    self.evidence.member_current = true;
                    return Ok(());
                }
            }
            self.pause()?;
        }
    }
    fn execute(&mut self) -> Result<()> {
        self.candidate.validate()?;
        for span in [
            self.limits.total,
            self.limits.action,
            self.limits.pair,
            self.limits.serving,
            self.limits.configuration,
            self.limits.poll,
            self.limits.relay_window,
            self.limits.member_wall,
            self.limits.pair_window,
        ] {
            require(
                span != Span::ZERO,
                "invalid_harness_budget",
                FaultKind::Input,
            )?;
        }
        self.phase(Phase::Preflight)?;
        match self.perform(Effect::VerifyPackage)? {
            Reply::Package(c) => {
                require(
                    c == self.candidate,
                    "package_identity_mismatch",
                    FaultKind::Admission,
                )?;
                self.evidence.verified_package = Some(c);
            }
            _ => return Err(shape()),
        }
        // Local power admission precedes remote preflight. Failure diagnostics
        // still observe both hosts through their independent snapshot path.
        match self.perform(Effect::OwnerPower)? {
            Reply::Power(power) => {
                self.evidence.initial_owner_power = Some(power);
                crate::admission::admit_power(power, true)?;
            }
            _ => return Err(shape()),
        }
        // Preserve both recorded admission observations, including the failing host.
        let mut admission_failure = None;
        for host in HOSTS {
            let failure = match self.perform(Effect::Preflight(host)) {
                Ok(Reply::Preflight(p)) => {
                    let result = preflight::check(&p, &self.candidate.version);
                    self.evidence.admissions.push((host, p));
                    result.err()
                }
                Ok(_) => Some(shape()),
                Err(e) => Some(e),
            };
            if let Some(e) = failure {
                let name = if host == Host::Owner {
                    "owner"
                } else {
                    "laptop"
                };
                self.evidence.preflight_errors.push(json!({"host":host,"code":e.code,"kind":e.kind,"message":format!("{}:{name}",e.code)}));
                admission_failure.get_or_insert(e);
            }
        }
        if let Some(e) = admission_failure {
            return Err(e);
        }
        self.admitted = true;
        self.phase(Phase::Install)?;
        for host in HOSTS {
            self.prepare_host(host)?;
        }
        self.configure()?;
        self.pair()?;
        self.phase(Phase::SeedMember)?;
        self.quit(Host::Member)?;
        self.seed(Host::Member, CacheRole::Target)?;
        self.launch(Host::Member)?;
        self.api(Host::Member, "/api/start", Some(json!({})))?;
        let s = self.api(Host::Member, "/api/status", None)?;
        require(
            s["authority"]["membership_state"] == "current",
            "member_start_failed",
            FaultKind::Product,
        )?;
        preflight::cached(&s["acquisition"])?;
        self.phase(Phase::Serve)?;
        self.api(Host::Owner, "/api/demo/start", Some(json!({})))?;
        loop {
            let s = self.api(Host::Owner, "/api/status", None)?;
            if Run::from_status(&s).serving() {
                self.evidence.serving = true;
                break;
            }
            require(
                !matches!(s["owner_run"]["state"].as_str(), Some("failed" | "stopped"))
                    && s["owner_run"]["error_code"].is_null(),
                "route_failed",
                FaultKind::Product,
            )?;
            self.pause()?;
        }
        self.phase(Phase::Chat)?;
        for index in 0..3 {
            match self.perform(Effect::Chat { index })? {
                Reply::Chat(c) => {
                    let ok = c.ok();
                    self.evidence.chats.push(c);
                    require(ok, "chat_failed", FaultKind::Product)?;
                }
                _ => return Err(shape()),
            }
        }
        self.phase(Phase::Finish)?;
        self.evidence.final_serving =
            Run::from_status(&self.api(Host::Owner, "/api/status", None)?).serving();
        require(
            self.evidence.final_serving,
            "route_lost_after_chats",
            FaultKind::Product,
        )
    }
}
pub(super) fn run<A: Adapter, S: SnapshotStore, C: Clock>(
    io: &mut A,
    store: &mut S,
    clock: &mut C,
    candidate: Candidate,
    limits: Limits,
    relay: Relay,
) -> Evidence {
    let deadline = clock.now().checked_add(limits.total.millis());
    let mut d = Driver {
        io,
        clock,
        candidate,
        limits,
        relay,
        evidence: Evidence::default(),
        end: deadline.unwrap_or(0),
        phase_end: 0,
        phase: Phase::Preflight,
        admitted: false,
        owner_launched: false,
        secrets: vec![],
    };
    let result = if deadline.is_none() {
        Err(Fault::new("invalid_harness_budget", FaultKind::Input))
    } else {
        d.execute()
    };
    match result {
        Ok(()) => {
            if evaluate(&d.candidate, &d.evidence) {
                d.evidence.outcome = "passed".into();
            } else {
                d.evidence.outcome = "failed".into();
                d.evidence.first_failure = Some("stranger_evidence_incomplete".into());
            }
        }
        Err(f) => {
            d.evidence.first_failure = Some(f.code.into());
            d.evidence.battery_stop_percentages = d.io.battery_percentages();
            d.evidence.product_failed = Some(f.kind == FaultKind::Product);
            d.evidence.outcome = if f.incomplete() {
                "incomplete"
            } else {
                "failed"
            }
            .into();
            if let Err(e) = failure_snapshot(d.io, store, f, &d.secrets) {
                d.evidence.snapshot_error = Some(e.code.into());
            }
            if d.owner_launched {
                // Explicit bounded cleanup bypasses expired admission/runtime guards.
                let request = Request {
                    path: "/api/demo/stop",
                    method: Method::Post,
                    body: Some(json!({})),
                    chat: false,
                };
                match d.io.perform(
                    Effect::Api {
                        host: Host::Owner,
                        request,
                    },
                    limits.action,
                ) {
                    Ok(Reply::Http(200, v)) if v["error"].is_null() => {}
                    Ok(_) => d.evidence.cleanup_errors.push("demo_stop_failed".into()),
                    Err(e) => d.evidence.cleanup_errors.push(e.code.into()),
                }
            }
        }
    }
    d.evidence
}
