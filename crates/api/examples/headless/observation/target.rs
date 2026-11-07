// SPDX-License-Identifier: AGPL-3.0-only
//! API-owned local target. Only this owner joins; worker captures weak authority.
use super::*;
use api::{
    bootstrap_process::{CleanupFailure, ProcessControl, TrustedBootstrap},
    bootstrap_stage::BootstrapStage,
    managed_carrier::TargetFence,
    target_pump::{StepOutcome, TargetHost, TargetPump},
};
use std::thread::JoinHandle;

const DELIVERY_WAKE: BudgetId = BudgetId::PyManagedRuntimeDeliveryWakeCeilingL461C16;

#[derive(Default)]
pub(super) struct Wake {
    generation: Mutex<u64>,
    changed: std::sync::Condvar,
}
impl Wake {
    pub(super) fn notify(&self) -> OResult<()> {
        let updated = locked(&self.generation).map(|mut g| *g = g.wrapping_add(1));
        self.changed.notify_all();
        updated
    }
    fn generation(&self) -> OResult<u64> {
        Ok(*locked(&self.generation)?)
    }
    fn wait(&self, generation: u64, fence: &TargetFence) -> OResult<()> {
        let g = locked(&self.generation)?;
        let ceiling = DELIVERY_WAKE
            .span(Entries::ZERO)
            .map_err(|_| OwnerError("budget_unavailable"))?;
        let (g, _) = self
            .changed
            .wait_timeout_while(g, std::time::Duration::from_millis(ceiling.millis()), |g| {
                *g == generation && fence.check().is_ok()
            })
            .map_err(|_| OwnerError("target_wake_poisoned"))?;
        drop(g);
        fence.check()
    }
}
// f4 managed_runtime.py:155,447-479. Init/reset IDs retain their admitted
// Operational/ef9 provenance; the complete scheduling source is f4.
struct Cadence {
    retry: Span,
}
impl Cadence {
    fn new() -> OResult<Self> {
        Ok(Self {
            retry: Self::span(
                BudgetId::PyFinalMyceliumA13MacosManagedRuntimeSelfOutboundRetryBackoffL155C8,
            )?,
        })
    }
    fn span(id: BudgetId) -> OResult<Span> {
        id.span(Entries::ZERO)
            .map_err(|_| OwnerError("budget_unavailable"))
    }
    fn delay(&mut self, retryable: bool) -> OResult<Span> {
        if retryable {
            let budgets::table::Value::Ratio(ratio) =
                BudgetId::PyManagedRuntimeOutboundRetryMultiplierL463C93
                    .entry()
                    .value
            else {
                return Err(OwnerError("budget_unavailable"));
            };
            self.retry = self
                .retry
                .checked_mul(ratio.get())
                .and_then(|v| v.checked_div(1000))
                .map_err(|_| OwnerError("budget_unavailable"))?
                .max(Self::span(
                    BudgetId::PyFinalMyceliumA13MacosManagedRuntimeL463C61ComparisonLower,
                )?)
                .min(Self::span(
                    BudgetId::PyFinalMyceliumA13MacosManagedRuntimeL463C53CeilingOperand,
                )?);
            Ok(self.retry)
        } else {
            self.retry = Self::span(
                BudgetId::PyFinalMyceliumA13MacosManagedRuntimeSelfOutboundRetryBackoffL471C20,
            )?;
            Self::span(DELIVERY_WAKE)
        }
    }
    fn wait(
        &mut self,
        outcome: StepOutcome,
        wake: &Wake,
        generation: u64,
        fence: &TargetFence,
    ) -> OResult<()> {
        let retryable = matches!(outcome, StepOutcome::OutboundRetryable { .. });
        let delay = self.delay(retryable)?;
        if retryable {
            // Source469: ONLY Stop can shorten this wait, never delivery wake.
            fence.wait_for(delay)
        } else {
            wake.wait(generation, fence)
        }
    }
}
pub(crate) struct Worker {
    scope: Value,
    fence: TargetFence,
    process: ProcessControl,
    wake: Arc<Wake>,
    join: Option<JoinHandle<std::result::Result<(), CleanupFailure>>>,
    failure: Option<CleanupFailure>,
}
impl Worker {
    fn close(&mut self) -> OResult<()> {
        let cancelled = self.fence.cancel();
        let notified = self.wake.notify();
        let stopped = self.process.cancel().map_err(OwnerError);
        let joined = match self.join.take() {
            Some(join) => match join
                .join()
                .map_err(|_| OwnerError("target_worker_panicked"))?
            {
                Ok(()) => Ok(()),
                Err(failure) => {
                    let code = failure.code();
                    self.failure = Some(failure);
                    Err(OwnerError(code))
                }
            },
            None => Ok(()),
        };
        let retried = match self.failure.as_mut() {
            Some(failure) => failure.retry().map_err(OwnerError),
            None => Ok(()),
        };
        if retried.is_ok() {
            self.failure.take();
        }
        cancelled
            .and(notified)
            .and(stopped)
            .and(joined)
            .and(retried)
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        if let Err(e) = self.close() {
            eprintln!("target worker close: {}", e.0);
        }
    }
}
pub(crate) fn close(core: &Shared) -> OResult<()> {
    // Move owners out only while doing blocking cleanup, and put a refused
    // owner back intact. A later Stop can retry the exact incarnation.
    let (worker, failure) = {
        let mut c = locked(core)?;
        (c.bootstrap.take(), c.bootstrap_failure.take())
    };
    let mut result = Ok(());
    if let Some(mut worker) = worker
        && let Err(error) = worker.close()
    {
        locked(core)?.bootstrap = Some(worker);
        result = Err(error);
    }
    if let Some(mut failure) = failure
        && let Err(code) = failure.retry()
    {
        locked(core)?.bootstrap_failure = Some(failure);
        result = Err(OwnerError(code));
    }
    result
}
struct Host {
    pinned: PinnedHost,
    signer: Arc<Identity>,
    scope: Value,
    offer: Value,
    swarm: String,
    owner: String,
}
impl Clock for Host {
    fn monotonic(&self) -> MonoInstant {
        self.pinned.monotonic()
    }
    fn wall_seconds(&self) -> f64 {
        self.pinned.wall_seconds()
    }
}
impl Host {
    fn current(&self, active: bool) -> OResult<()> {
        let seed = self.pinned.pin()?;
        let node = self.scope["target_node_id"]
            .as_str()
            .ok_or(OwnerError("managed_control_member_stale"))?;
        if binding(
            &self.pinned.member(node)?,
            &self.scope["run_id"],
            &self.scope["deployment_id"],
        )? != self.scope
        {
            return Err(OwnerError("managed_control_member_stale"));
        }
        if active {
            let core = self
                .pinned
                .core
                .upgrade()
                .ok_or(OwnerError("managed_owner_closed"))?;
            let demo = locked(&core)?.demo.clone();
            if !demo.active() {
                return Err(OwnerError("owner_run_not_active"));
            }
            if let Some(code) = resource_check(&core) {
                return Err(OwnerError(code));
            }
            let a = seed.exchange.announcement(Some(node)).to_value();
            if a["run_id"] != self.scope["run_id"]
                || a["deployment_id"] != self.scope["deployment_id"]
                || a["bootstrap_offer_id"] != self.offer["offer_id"]
                || a["phase"] != "observe"
            {
                return Err(OwnerError("offer_transfer_not_authorized"));
            }
            if self.offer["expires_at"]
                .as_f64()
                .is_none_or(|t| t <= self.wall_seconds())
            {
                return Err(OwnerError("offer_expired"));
            }
        }
        Ok(())
    }
    fn ack(&self, root: &Path, removed: bool) -> OResult<()> {
        self.current(!removed)?;
        let now = self.wall_seconds();
        let ttl = BudgetId::PyQuantityMyceliumA13MacosManagedOffersL371C28
            .span(Entries::ZERO)
            .map_err(|_| OwnerError("budget_unavailable"))?;
        let mut s = self.scope.clone();
        s["run_id"] = "stage-discovery".into();
        s["deployment_id"] = "stage-discovery".into();
        let protocol = if removed {
            "mycelium.managed_stage_cleanup_ack.v1"
        } else {
            "mycelium.managed_stage_ack.v1"
        };
        let ack = if removed {
            json!({"protocol":"mycelium.controller_remote_cleanup_ack.v1","node_id":self.scope["target_node_id"],"staging_root":root,"removed":true})
        } else {
            json!({"protocol":"mycelium.controller_remote_stage_ack.v1","node_id":self.scope["target_node_id"],"staging_root":root,"archive_digest":self.offer["archive_digest"],"archive_size_bytes":self.offer["archive_bytes"]})
        };
        for (k,v) in json!({"protocol":protocol,"swarm_id":self.swarm,"sequence":1,"nonce":nonce()?,"request_id":nonce()?,"issued_at":now,"expires_at":now+ttl.millis() as f64/1000.0,"offer_id":self.offer["offer_id"],"ack":ack}).as_object().ok_or(OwnerError("offer_ack_invalid"))? {s[k]=v.clone();}
        if !removed {
            s["offset"] = 0.into();
        }
        let envelope = json!({"protocol":protocol::managed::SIGNED_ENVELOPE_PROTOCOL,"verification_key":self.signer.public_key_record(),"signature":self.signer.sign(&s).map_err(|_|OwnerError("managed_control_sign_failed"))?,"statement":s});
        let end = self
            .monotonic()
            .checked_add(ttl)
            .map_err(|_| OwnerError("managed_control_timeout"))?;
        let response = self.pinned.raw(&json!({"envelope":envelope}), end, None)?;
        let expected = if removed {
            "mycelium.managed_stage_cleanup_state.v1"
        } else {
            "mycelium.managed_stage_bytes.v1"
        };
        let key = self.pinned.signer.public_key_record();
        let body = protocol::managed::verify(
            &response,
            expected,
            &key,
            self.pinned.signer.endpoint_id(),
            self.wall_seconds(),
            managed_time_limits()?,
        )
        .map_err(OwnerError::from)?;
        if body["request_id"] != s["request_id"]
            || body["swarm_id"] != self.swarm
            || body["owner_node_id"] != self.owner
            || body["offer"] != self.offer
            || self
                .scope
                .as_object()
                .ok_or(OwnerError("offer_ack_invalid"))?
                .iter()
                .any(|(k, v)| k != "run_id" && k != "deployment_id" && body[k] != *v)
            || body["run_id"] != "stage-discovery"
            || body["deployment_id"] != "stage-discovery"
            || (removed && body["ack"] != ack)
        {
            return Err(OwnerError("offer_response_invalid"));
        }
        if end
            .remaining(self.monotonic())
            .map_err(|_| OwnerError("managed_control_timeout"))?
            == Span::ZERO
        {
            return Err(OwnerError("managed_control_timeout"));
        }
        self.current(!removed)
    }
}
impl TargetHost for Host {
    fn check(&self, scope: &Value) -> api::target_pump::Result<()> {
        if *scope != self.scope {
            return Err("managed_control_run_binding_mismatch");
        }
        self.current(true).map_err(|e| e.0)
    }
    fn signer(&self) -> &Identity {
        &self.signer
    }
    fn owner_key(&self) -> Value {
        self.pinned.signer.public_key_record()
    }
    fn owner_node(&self) -> String {
        self.owner.clone()
    }
    fn swarm(&self) -> String {
        self.swarm.clone()
    }
    fn nonce(&self) -> api::target_pump::Result<String> {
        nonce().map_err(|e| e.0)
    }
    fn exchange(
        &self,
        envelope: &Value,
        end: MonoInstant,
        fence: &TargetFence,
    ) -> api::target_pump::Result<Value> {
        self.current(true).map_err(|e| e.0)?;
        let seed = self.pinned.pin().map_err(|e| e.0)?;
        let managed = locked(&seed.managed)
            .map_err(|e| e.0)?
            .as_ref()
            .cloned()
            .ok_or("managed_owner_unavailable")?;
        let bytes =
            protocol::canonical::to_bytes(envelope).map_err(|_| "managed_control_json_invalid")?;
        let response = managed
            .carrier
            .call_target(&self.pinned.clock, &bytes, end, fence)
            .map_err(|e| e.0)?;
        self.current(true).map_err(|e| e.0)?;
        protocol::strict::object(&response, protocol::managed::MAX_FRAME_BYTES)
            .map_err(|_| "managed_control_json_invalid")
    }
    fn stage_ack(&self, offer: &Value, root: &Path, removed: bool) -> api::target_pump::Result<()> {
        if *offer != self.offer {
            return Err("offer_changed");
        }
        self.ack(root, removed).map_err(|e| e.0)
    }
}
pub(super) fn start(
    core: &Shared,
    pinned: PinnedHost,
    signer: Arc<Identity>,
    scope: Value,
    offer: Value,
    archive: Vec<u8>,
) -> OResult<()> {
    let root = locked(core)?.root.clone();
    if locked(core)?.bootstrap_failure.is_some() {
        return Err(OwnerError("control_restart_requires_reconciliation"));
    }
    if let Some(worker) = locked(core)?.bootstrap.as_ref() {
        return if worker.scope == scope && worker.join.as_ref().is_some_and(|j| !j.is_finished()) {
            Ok(())
        } else {
            Err(OwnerError("control_restart_requires_reconciliation"))
        };
    }
    let config = private_state::load(
        &root.join("bootstrap-program.json"),
        private_state::private_json_limit().map_err(|e| OwnerError(e.0))?,
    )
    .map_err(|e| OwnerError(e.0))?
    .ok_or(OwnerError("native_bootstrap_unavailable"))?;
    let config = Value::Object(config);
    if config.as_object().is_none_or(|o| o.len() != 4)
        || config["protocol"] != "mycelium.bootstrap_program.v1"
    {
        return Err(OwnerError("native_bootstrap_configuration_invalid"));
    }
    let text = |k: &str| {
        config[k]
            .as_str()
            .ok_or(OwnerError("native_bootstrap_configuration_invalid"))
    };
    let program = TrustedBootstrap::open(
        Path::new(text("path")?),
        text("sha256")?,
        text("source_sha")?,
    )
    .map_err(OwnerError)?;
    let seed = pinned.pin()?;
    let host = Host {
        pinned,
        signer,
        scope: scope.clone(),
        offer: offer.clone(),
        swarm: seed.coordinator.swarm_id().into(),
        owner: seed.coordinator.seed_node_id().into(),
    };
    host.current(true)?;
    let root = root.join("local-contributor/native-bootstrap");
    fs::create_dir_all(&root).map_err(|_| OwnerError("bootstrap_stage_create_failed"))?;
    fs::set_permissions(&root, std::os::unix::fs::PermissionsExt::from_mode(0o700))
        .map_err(|_| OwnerError("bootstrap_stage_create_failed"))?;
    let stage = BootstrapStage::create(&root.join("stage"), &scope, &offer, &archive)
        .map_err(OwnerError)?;
    let fence = TargetFence::default();
    let mut pump = match TargetPump::start(
        &root,
        host,
        scope.clone(),
        offer,
        stage,
        &program,
        fence.clone(),
    ) {
        Ok(pump) => pump,
        Err(failure) => {
            let code = failure.code();
            locked(core)?.bootstrap_failure = Some(failure);
            return Err(OwnerError(code));
        }
    };
    let process = pump.process_control();
    let wake = locked(&seed.managed)?
        .as_ref()
        .ok_or(OwnerError("managed_owner_unavailable"))?
        .wake
        .clone();
    let worker_fence = fence.clone();
    let owner_wake = wake.clone();
    let join = thread::spawn(move || {
        let result: OResult<()> = (|| {
            let mut cadence = Cadence::new()?;
            loop {
                let generation = wake.generation()?;
                let outcome = pump.step_outcome().map_err(OwnerError)?;
                if matches!(outcome, StepOutcome::Completed { stopped: true }) {
                    while worker_fence.check().is_ok() {
                        let g = wake.generation()?;
                        wake.wait(g, &worker_fence)?;
                    }
                    break;
                }
                cadence.wait(outcome, &wake, generation, &worker_fence)?;
            }
            Ok(())
        })();
        let recorded = if let Err(e) = &result {
            pump.record_failure(e.0)
        } else {
            Ok(())
        };
        if let Err(cleanup) = pump.close() {
            let control = pump.process_control();
            return Err(CleanupFailure::owned(
                cleanup,
                cleanup,
                control,
                move || pump.close(),
            ));
        }
        recorded.map_err(CleanupFailure::from)?;
        if worker_fence.check().is_err() {
            Ok(())
        } else {
            result.map_err(|e| CleanupFailure::from(e.0))
        }
    });
    let mut worker = Worker {
        scope,
        fence,
        process,
        wake: owner_wake,
        join: Some(join),
        failure: None,
    };
    let mut c = locked(core)?;
    if c.bootstrap.is_some() || c.closed || !c.running {
        drop(c);
        if let Err(error) = worker.close() {
            let control = worker.process.clone();
            locked(core)?.bootstrap_failure = Some(CleanupFailure::owned(
                error.0,
                error.0,
                control,
                move || worker.close().map_err(|e| e.0),
            ));
            return Err(error);
        }
        return Err(OwnerError("managed_owner_closed"));
    }
    c.bootstrap = Some(worker);
    Ok(())
}
#[cfg(test)]
mod cadence_tests {
    use super::*;
    #[test]
    fn retry_multiplies_before_wait_caps_and_resets() {
        let mut cadence = Cadence::new().unwrap();
        let actual: Vec<_> = (0..5)
            .map(|_| cadence.delay(true).unwrap().millis())
            .collect();
        assert_eq!(actual, [200, 400, 800, 1000, 1000]);
        assert_eq!(cadence.delay(false).unwrap().millis(), 50);
        assert_eq!(cadence.delay(true).unwrap().millis(), 200);
    }
    #[test]
    fn actual_retry_wait_ignores_delivery_wake_but_stop_interrupts() {
        let wake = Arc::new(Wake::default());
        let fence = TargetFence::default();
        let generation = wake.generation().unwrap();
        wake.notify().unwrap(); // already queued delivery MUST NOT shorten retry
        let mut cadence = Cadence::new().unwrap();
        let retry = StepOutcome::OutboundRetryable {
            operation: "ack",
            cause: "managed_control_unreachable",
        };
        let started = std::time::Instant::now();
        cadence.wait(retry, &wake, generation, &fence).unwrap();
        assert!(started.elapsed().as_millis() >= 200);
        let f = fence.clone();
        let w = wake.clone();
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        let join =
            std::thread::spawn(move || tx.send(cadence.wait(retry, &w, generation, &f)).unwrap());
        fence.cancel().unwrap();
        let safety = Cadence::span(
            BudgetId::PyMyceliumNodeProcessPhysicalnodeprocessInitShutdownTimeoutSecondsL1965,
        )
        .unwrap();
        let result = rx.recv_timeout(std::time::Duration::from_millis(safety.millis()));
        join.join().unwrap();
        assert!(result.unwrap().is_err());
    }
    #[test]
    fn actual_unnotified_wait_returns_at_the_admitted_periodic_ceiling() {
        use std::sync::mpsc;
        let wake = Arc::new(Wake::default());
        let fence = TargetFence::default();
        let w = wake.clone();
        let f = fence.clone();
        let g = wake.generation().unwrap();
        let (tx, rx) = mpsc::sync_channel(1);
        let start = std::time::Instant::now();
        let thread = std::thread::spawn(move || {
            tx.send(w.wait(g, &f)).unwrap();
        });
        // Test safety bound, not a replacement for the source scheduling bound.
        let safety =
            BudgetId::PyMyceliumNodeProcessPhysicalnodeprocessInitShutdownTimeoutSecondsL1965
                .span(Entries::ZERO)
                .unwrap();
        let received = rx.recv_timeout(std::time::Duration::from_millis(safety.millis()));
        let elapsed = start.elapsed();
        fence.cancel().unwrap();
        wake.notify().unwrap();
        thread.join().unwrap();
        assert!(
            matches!(received, Ok(Ok(()))),
            "missing finite periodic wake ceiling"
        );
        assert!(
            elapsed
                >= std::time::Duration::from_millis(
                    DELIVERY_WAKE.span(Entries::ZERO).unwrap().millis()
                ),
            "zero-delay spin is not a source-compatible wait"
        );
    }
    #[test]
    fn consumer_selects_exact_source_id_and_cancelled_wait_cannot_advance() {
        // Static producer-consumer identity assertion, NOT latency proof.
        assert_eq!(
            DELIVERY_WAKE,
            BudgetId::PyManagedRuntimeDeliveryWakeCeilingL461C16
        );
        let wake = Wake::default();
        let fence = TargetFence::default();
        let g = wake.generation().unwrap();
        fence.cancel().unwrap();
        wake.notify().unwrap();
        assert!(wake.wait(g, &fence).is_err());
    }
}
