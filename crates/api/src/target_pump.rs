// SPDX-License-Identifier: AGPL-3.0-only
//! Same-incarnation durable signed poll/ACK replay; uncertain native work is never retried.
//! Unresolved prior journals are refused on reopen: this is not crash-resume recovery.
use crate::managed_carrier::TargetFence;
use crate::{
    bootstrap_process::{BootstrapProcess, ProcessControl, TrustedBootstrap},
    bootstrap_stage::BootstrapStage,
    preferences::private_state,
};
use budgets::{BudgetId as B, Entries, MonoInstant};
use protocol::{
    identity::Identity,
    managed_receipt::{ExpectedReceipt, ReceiptKind},
};
use serde_json::{Value, json};
use std::{
    fs::{File, OpenOptions},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::Path,
    time::{Duration, Instant},
};
use transport::Clock;
pub type Result<T> = std::result::Result<T, &'static str>;
pub trait TargetHost: Clock + Send {
    fn check(&self, binding: &Value) -> Result<()>;
    fn signer(&self) -> &Identity;
    fn owner_key(&self) -> Value;
    fn owner_node(&self) -> String;
    fn swarm(&self) -> String;
    fn nonce(&self) -> Result<String>;
    fn exchange(
        &self,
        envelope: &Value,
        deadline: MonoInstant,
        fence: &TargetFence,
    ) -> Result<Value>;
    fn stage_ack(&self, offer: &Value, root: &Path, removed: bool) -> Result<()>;
}
fn span(id: B) -> Result<budgets::Span> {
    id.span(Entries::ZERO).map_err(|_| "budget_unavailable")
}
fn state_limit() -> Result<u64> {
    B::PyMyceliumA13MacosControlPumpTargetcontrolpumpStateMaxBytesL124
        .bytes()
        .map(|b| b.get())
        .map_err(|_| "budget_unavailable")
}
/// Recoverable transport outcomes are explicit; arbitrary errors remain fatal.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StepOutcome {
    Empty,
    Completed {
        stopped: bool,
    },
    OutboundRetryable {
        operation: &'static str,
        cause: &'static str,
    },
    OutboundRequeued {
        operation: &'static str,
        cause: &'static str,
    },
}
enum ExchangeOutcome {
    Reply(Value),
    Deferred(StepOutcome),
}
fn request_kind(kind: ReceiptKind) -> Result<(&'static str, B)> {
    match kind {
        ReceiptKind::Poll => Ok((
            "mycelium.managed_control.target_poll.v1",
            B::PyFinalMyceliumA13MacosManagedControlTtlSecondsL2424C29,
        )),
        ReceiptKind::Ack => Ok((
            "mycelium.managed_control.target_ack.v1",
            B::PyFinalMyceliumA13MacosManagedControlTtlSecondsL2508C29,
        )),
        _ => Err("control_request_invalid"),
    }
}
fn time_limits() -> Result<protocol::TimeLimits> {
    Ok(protocol::TimeLimits {
        max_ttl_seconds: span(B::ControlTtl)?.millis() as f64 / 1000.0,
        clock_skew_seconds: span(B::ControlClockSkew)?.millis() as f64 / 1000.0,
    })
}
// Exact f4 control_pump.py:72-101 classes. This is deliberately NOT all Err.
fn retryable(code: &str) -> bool {
    matches!(
        code,
        "managed_control_unreachable"
            | "managed_control_response_timeout"
            | "managed_control_internal_error"
            | "managed_control_response_invalid"
            | "managed_control_response_signature_invalid"
    )
}
fn unusable(code: &str) -> bool {
    matches!(
        code,
        "managed_control_message_expired"
            | "managed_control_member_stale"
            | "managed_control_sequence_replayed"
            | "managed_control_nonce_replayed"
            | "managed_control_conflicting_retry"
            | "managed_control_prepared_request_invalid"
    )
}
pub struct TargetPump<H: TargetHost> {
    host: H,
    root: std::path::PathBuf,
    state: Value,
    scope: Value,
    offer: Value,
    stage: BootstrapStage,
    process: BootstrapProcess,
    fence: TargetFence,
    _lock: File,
    closed: bool,
}
impl<H: TargetHost> TargetPump<H> {
    pub fn start(
        root: &Path,
        host: H,
        scope: Value,
        offer: Value,
        stage: BootstrapStage,
        program: &TrustedBootstrap,
        fence: TargetFence,
    ) -> std::result::Result<Self, crate::bootstrap_process::CleanupFailure>
    where
        H: 'static,
    {
        let limit = state_limit()?;
        host.check(&scope)?;
        protocol::managed::validate_binding(&scope).map_err(|e| e.0)?;
        if host.signer().verification_key_digest() != scope["target_key_digest"]
            || host.signer().endpoint_id() != scope["target_endpoint_id"]
        {
            return Err("control_worker_identity_mismatch".into());
        }
        let lock = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(root.join("owner.lock"))
            .map_err(|_| "control_state_invalid")?;
        let m = lock.metadata().map_err(|_| "control_state_invalid")?;
        if !m.is_file()
            || m.nlink() != 1
            || m.uid() != rustix::process::geteuid().as_raw()
            || m.mode() & 0o7777 != 0o600
        {
            return Err("control_state_invalid".into());
        }
        lock.try_lock()
            .map_err(|_| "control_pump_already_running")?;
        let authority = json!({"swarm_id":host.swarm(),"owner_key":host.owner_key(),"target_key_digest":scope["target_key_digest"]});
        let prior = private_state::load(&root.join("state.json"), limit)
            .map_err(|e| e.0)?
            .map(Value::Object);
        let sequence = if let Some(prior) = prior {
            if prior["protocol"] != "mycelium.a13.api_target_control.v1"
                || prior["authority"] != authority
                || prior["phase"] != "closed"
                || !prior["outbound"].is_null()
                || !prior["pending"].is_null()
            {
                return Err("control_restart_requires_reconciliation".into());
            }
            prior["sequence"].as_u64().ok_or("control_state_invalid")?
        } else {
            0
        };
        stage.verify()?;
        host.stage_ack(&offer, stage.path(), false)?;
        host.check(&scope)?;
        let mut state = json!({"protocol":"mycelium.a13.api_target_control.v1","authority":authority,"binding":scope,"sequence":sequence,"phase":"prepared","outbound":null,"pending":null,"native_stop_receipt":null,"process":null,"stage_root":stage.path(),"source_sha":program.source_sha,"failure":null,"cleanup_error":null});
        private_state::store(&root.join("state.json"), &state, limit).map_err(|e| e.0)?;
        let process = match BootstrapProcess::spawn(
            program,
            json!({"node_id":scope["target_node_id"],"run_id":scope["run_id"],"deployment_id":scope["deployment_id"]}),
            stage.path(),
        ) {
            Ok(process) => process,
            Err(failure) => {
                state["phase"] = if failure.owner_retained() {
                    "cleanup_unproven"
                } else {
                    "failed"
                }
                .into();
                state["failure"] = failure.code().into();
                state["cleanup_error"] = failure
                    .cleanup_error()
                    .map(Value::from)
                    .unwrap_or(Value::Null);
                if let Ok(Some(birth)) = failure.process_identity() {
                    state["process"] = json!({"pid":birth.pid,"parent_pid":birth.parent_pid,"pgid":birth.pgid,"start_seconds":birth.start_seconds,"start_microseconds":birth.start_microseconds});
                }
                if let Err(error) = private_state::store(&root.join("state.json"), &state, limit) {
                    eprintln!("failed bootstrap state publication: {}", error.0);
                }
                return Err(failure);
            }
        };
        let birth = match process.control().birth() {
            Ok(birth) => birth,
            Err(code) => {
                let control = process.control();
                let mut process = process;
                return Err(crate::bootstrap_process::CleanupFailure::owned(
                    code,
                    code,
                    control,
                    move || process.close(),
                ));
            }
        };
        state["process"] = json!({"pid":birth.pid,"parent_pid":birth.parent_pid,"pgid":birth.pgid,"start_seconds":birth.start_seconds,"start_microseconds":birth.start_microseconds});
        let mut this = Self {
            host,
            root: root.into(),
            state,
            scope,
            offer,
            stage,
            process,
            fence,
            _lock: lock,
            closed: false,
        };
        let initialized = this.check().and_then(|()| {
            this.state["phase"] = "running".into();
            this.save()
        });
        if let Err(code) = initialized {
            this.state["failure"] = code.into();
            return match this.close() {
                Ok(()) => Err(code.into()),
                Err(cleanup) => {
                    let control = this.process_control();
                    Err(crate::bootstrap_process::CleanupFailure::owned(
                        code,
                        cleanup,
                        control,
                        move || this.close(),
                    ))
                }
            };
        }
        Ok(this)
    }
    fn save(&self) -> Result<()> {
        private_state::store(&self.root.join("state.json"), &self.state, state_limit()?)
            .map_err(|e| e.0)
    }
    fn check(&self) -> Result<()> {
        self.fence.check().map_err(|e| e.0)?;
        self.host.check(&self.scope)?;
        self.stage.verify()
    }
    pub fn process_control(&self) -> ProcessControl {
        self.process.control()
    }
    pub fn record_failure(&mut self, code: &str) -> Result<()> {
        self.state["failure"] = code.into();
        self.save()
    }
    fn prepare(&mut self, kind: ReceiptKind, fields: Value) -> Result<()> {
        self.check()?;
        let (protocol, ttl) = request_kind(kind)?;
        let seq = self.state["sequence"]
            .as_u64()
            .and_then(|v| v.checked_add(1))
            .ok_or("control_sequence_overflow")?;
        let now = self.host.wall_seconds();
        let mut statement = self.scope.clone();
        for (k,v) in json!({"protocol":protocol,"swarm_id":self.host.swarm(),"sequence":seq,"nonce":self.host.nonce()?,"request_id":self.host.nonce()?,"issued_at":now,"expires_at":now+span(ttl)?.millis() as f64/1000.0}).as_object().ok_or("control_request_invalid")? { statement[k]=v.clone(); }
        for (k, v) in fields.as_object().ok_or("control_request_invalid")? {
            statement[k] = v.clone();
        }
        let envelope = json!({"protocol":protocol::managed::SIGNED_ENVELOPE_PROTOCOL,"signature":self.host.signer().sign(&statement).map_err(|_|"managed_control_sign_failed")?,"verification_key":self.host.signer().public_key_record(),"statement":statement});
        self.state["sequence"] = seq.into();
        self.state["outbound"] = envelope;
        self.save() // exact signed request durable before any exchange
    }
    fn replay_inner(&self, kind: ReceiptKind) -> Result<Value> {
        let envelope = &self.state["outbound"];
        // Verify our OWN durable signature before classifying expiry. A corrupt
        // own signature is fatal, never a transient owner-reply signature fault.
        let statement = protocol::managed::verify(
            envelope,
            request_kind(kind)?.0,
            &self.host.signer().public_key_record(),
            self.host.signer().endpoint_id(),
            self.host.wall_seconds(),
            time_limits()?,
        )
        .map_err(|e| e.0)?;
        let mut fields = vec![
            "protocol",
            "swarm_id",
            "sequence",
            "nonce",
            "request_id",
            "issued_at",
            "expires_at",
        ];
        fields.extend(
            self.scope
                .as_object()
                .ok_or("control_state_invalid")?
                .keys()
                .map(String::as_str),
        );
        if matches!(kind, ReceiptKind::Ack) {
            fields.extend([
                "command_id",
                "claim_token",
                "disposition",
                "native_response",
            ]);
        }
        if statement
            .as_object()
            .is_none_or(|s| s.len() != fields.len() || fields.iter().any(|k| !s.contains_key(*k)))
            || statement["swarm_id"] != self.host.swarm()
        {
            return Err("managed_control_prepared_request_invalid");
        }
        if self
            .scope
            .as_object()
            .ok_or("control_state_invalid")?
            .iter()
            .any(|(k, v)| statement[k] != *v)
        {
            return Err("managed_control_member_stale");
        }
        let remaining = statement["expires_at"]
            .as_f64()
            .ok_or("managed_control_time_invalid")?
            - self.host.wall_seconds();
        if remaining <= 0.0 {
            return Err("managed_control_message_expired");
        }
        // Fresh transport attempt, NOT a renewed signed request/operation lease.
        let allowance =
            span(B::PyMyceliumA13MacosManagedControlManagedcontrolclientInitTimeoutL1933)?
                .min(budgets::Span::from_millis((remaining * 1000.0) as u64));
        let end = self
            .host
            .monotonic()
            .checked_add(allowance)
            .map_err(|_| "managed_control_timeout")?;
        let response = self.host.exchange(envelope, end, &self.fence)?;
        let key = self.host.owner_key();
        let swarm = self.host.swarm();
        let owner = self.host.owner_node();
        let native =
            &self.state["pending"]["delivery"]["command_envelope"]["statement"]["native_command"];
        let body = protocol::managed_receipt::verify(
            &response,
            &ExpectedReceipt {
                kind,
                owner_key: &key,
                request_id: statement["request_id"]
                    .as_str()
                    .ok_or("control_request_invalid")?,
                swarm_id: &swarm,
                owner_node_id: &owner,
                binding: &self.scope,
                native_command: native,
                now: self.host.wall_seconds(),
            },
            time_limits()?,
        )
        .map_err(|e| e.0)?;
        if end
            .remaining(self.host.monotonic())
            .map_err(|_| "managed_control_timeout")?
            == budgets::Span::ZERO
        {
            return Err("managed_control_response_timeout");
        }
        if body["expires_at"]
            .as_f64()
            .is_none_or(|t| t <= self.host.wall_seconds())
        {
            return Err("managed_control_message_expired");
        }
        Ok(body)
    }
    fn request(&mut self, kind: ReceiptKind, fields: Value) -> Result<ExchangeOutcome> {
        // Current authority/cancellation are NOT mailbox replay rejections, even
        // when a local guard uses the same member-stale spelling.
        self.check()?;
        if self.state["outbound"].is_null() {
            self.prepare(kind, fields)?;
        }
        self.save()?; // an earlier failed store must never permit an unjournalled send
        let result = self.replay_inner(kind);
        self.check()?;
        let operation = if matches!(kind, ReceiptKind::Poll) {
            "poll"
        } else {
            "ack"
        };
        match result {
            Ok(body) => {
                self.state["outbound"] = Value::Null;
                Ok(ExchangeOutcome::Reply(body))
            }
            Err(cause) if retryable(cause) => {
                Ok(ExchangeOutcome::Deferred(StepOutcome::OutboundRetryable {
                    operation,
                    cause,
                }))
            }
            Err(cause) if unusable(cause) => {
                self.state["outbound"] = Value::Null;
                self.save()?;
                Ok(ExchangeOutcome::Deferred(StepOutcome::OutboundRequeued {
                    operation,
                    cause,
                }))
            }
            Err(cause) => Err(cause),
        }
    }
    /// Compatibility view; worker scheduling consumes the explicit outcome.
    pub fn step(&mut self) -> Result<bool> {
        Ok(matches!(
            self.step_outcome()?,
            StepOutcome::Completed { stopped: true }
        ))
    }
    /// Replay the exact outbound before any fresh request. Native uncertainty is
    /// never permission to execute again; only a durable result can rebuild ACK.
    pub fn step_outcome(&mut self) -> Result<StepOutcome> {
        if self.closed {
            return Err("control_pump_closed");
        }
        self.check()?;
        let resumed = !self.state["pending"].is_null();
        if !resumed {
            let delivery = match self.request(ReceiptKind::Poll, json!({}))? {
                ExchangeOutcome::Reply(body) => body,
                ExchangeOutcome::Deferred(outcome) => return Ok(outcome),
            };
            let phase = match delivery["delivery_kind"].as_str() {
                Some("empty") => {
                    self.save()?;
                    return Ok(StepOutcome::Empty);
                }
                Some("command") => "ready",
                Some("reconciliation_required") => "reconciliation_required",
                _ => return Err("control_reconciliation_required"),
            };
            self.state["pending"] =
                json!({"delivery":delivery,"phase":phase,"native_response":null,"error_code":null});
            self.save()?;
        }
        if resumed
            && matches!(
                self.state["pending"]["phase"].as_str(),
                Some("native_started" | "reconciliation_required")
            )
            && self.state["outbound"].is_null()
        {
            return Err("control_reconciliation_required");
        }
        let delivery = self.state["pending"]["delivery"].clone();
        let command = delivery["command_envelope"]["statement"]["native_command"].clone();
        if self.state["pending"]["phase"] == "ready" {
            self.check()?;
            if command["run_id"] != self.scope["run_id"]
                || command["deployment_id"] != self.scope["deployment_id"]
            {
                return Err("control_worker_identity_mismatch");
            }
            let expires = delivery["command_envelope"]["statement"]["expires_at"]
                .as_f64()
                .ok_or("managed_control_time_invalid")?;
            let remaining = expires - self.host.wall_seconds();
            if !remaining.is_finite() || remaining <= 0.0 {
                return Err("managed_control_expired");
            }
            let allowance = Duration::from_millis(
                span(B::PyMyceliumNodeProcessPhysicalnodeprocessInitResponseTimeoutSecondsL1964)?
                    .millis(),
            )
            .min(Duration::from_secs_f64(remaining));
            self.state["pending"]["phase"] = "native_started".into();
            self.save()?; // uncertainty durable BEFORE the actual pipe write
            let result = self.process.call(&command, Instant::now() + allowance);
            match result {
                Ok(response) => {
                    if command["command"] == "stop" {
                        self.state["native_stop_receipt"] = response.clone();
                    }
                    self.state["pending"]["native_response"] = response;
                    self.state["pending"]["phase"] = "native_returned".into();
                }
                Err(code) => {
                    self.state["pending"]["phase"] = "reconciliation_required".into();
                    self.state["pending"]["error_code"] = code.into();
                }
            }
            self.save()?; // retain actual output even if authority just expired
            self.check()?;
            if self.host.wall_seconds() >= expires {
                return Err("managed_control_expired");
            }
        }
        let response = self.state["pending"]["native_response"].clone();
        let disposition = if response.is_null() {
            "reconciliation_required"
        } else if response["ok"] == true {
            "completed"
        } else {
            "failed"
        };
        match self.request(ReceiptKind::Ack,json!({"command_id":delivery["command_id"],"claim_token":delivery["claim_token"],"disposition":disposition,"native_response":response}))? {
            ExchangeOutcome::Deferred(outcome)=>return Ok(outcome),
            ExchangeOutcome::Reply(_)=>{},
        }
        self.state["pending"] = Value::Null;
        self.save()?;
        Ok(StepOutcome::Completed {
            stopped: command["command"] == "stop" && response["ok"] == true,
        })
    }
    pub fn close(&mut self) -> Result<()> {
        if self.closed {
            return Ok(());
        }
        let process = self.process.close();
        let stage = self.stage.remove();
        let ack = if stage.is_ok() {
            self.host.stage_ack(&self.offer, self.stage.path(), true)
        } else {
            Err("bootstrap_stage_cleanup_unproven")
        };
        let result = process.and(stage).and(ack);
        self.state["phase"] = if result.is_ok() {
            "closed"
        } else {
            "cleanup_unproven"
        }
        .into();
        self.state["cleanup_error"] = result.err().map(Value::from).unwrap_or(Value::Null);
        self.save()?;
        self.closed = result.is_ok();
        result
    }
}
impl<H: TargetHost> Drop for TargetPump<H> {
    fn drop(&mut self) {
        if let Err(code) = self.close() {
            eprintln!("target pump cleanup: {code}");
        }
    }
}
