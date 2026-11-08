// SPDX-License-Identifier: AGPL-3.0-only
//! Trusted local member compute custody. The only proof producer is the owned
//! B6 native child. Versioned member signatures add enrollment and offer binding;
//! they do not attest hostile code or promote physical route readiness.
use super::*;
use crate::native_compiler::{self, worker::Worker};
use stage_contract::joined_native::{RESIDENT_FIELDS, RESIDENT_PROTOCOL as PROTOCOL};
use stage_contract::{native_proof as proof, native_shard as shard};
use std::{
    sync::{Arc, atomic::AtomicBool},
    time::Instant,
};
fn native(e: native_compiler::Error) -> Error {
    fail(e.0)
}

/// Pure binding check, before any executable or Metal context is opened.
pub fn validate_prepare(offer: &Value, prepare: &Value, reference: &Value) -> Result<()> {
    let m = &offer["message"];
    let id = &reference["recorded"]["identity"];
    let range = &prepare["shard"]["range"];
    if prepare["command"] != shard::COMMAND
        || prepare["id"] != digest(offer)?.trim_start_matches("sha256:")
        || prepare["generation"].as_u64().is_none()
        || prepare["generation"] != m["member"]["generation"]
        || prepare["shard"]["protocol"] != shard::PROTOCOL
        || prepare["shard"]["node_id"] != m["member"]["node_id"]
        || prepare["shard"]["recipe"] != m["recipe"]
        || prepare["shard"]["transfer"] != m["transfer"]
        || prepare["shard"]["recipe_digest"] != m["recipe"]["node_plan_digest"]
        || range.as_array().is_none_or(|r| r.len() != 2)
        || prepare["start"] != range[0]
        || prepare["end"] != range[1]
        || prepare["total"] != 28
        || prepare["stage_image"]["revision"] != id["revision"]
        || prepare["stage_image"]["image_digest"] != id["checkpoint_sha256"]
        || prepare["stage_image"]["tensor_manifest"] != id["manifest_sha256"]
    {
        return Err(fail("joined_native_prepare_invalid"));
    }
    Ok(())
}
fn stage(prepare: &Value) -> Value {
    json!({"range":[prepare["start"],prepare["end"]],"request_id":prepare["id"],"member_generation":prepare["generation"]})
}
/// Verify against the independently held offer, the owner's nonce and the
/// owner-ASSIGNED layer range, AND the current seed row. The signed document's
/// claimed key and range are never the authority for themselves.
pub fn verify_resident(
    coordinator: &seed::Coordinator,
    offer: &Value,
    nonce: &str,
    assigned: [u64; 2],
    envelope: &Value,
) -> Result<Value> {
    check_owner(coordinator, offer)?;
    let member = &offer["message"]["member"];
    let message = open(envelope, PROTOCOL, text(member, "verification_key_digest")?)?;
    exact(message, RESIDENT_FIELDS)?;
    if message["member"] != *member
        || message["offer_digest"] != digest(offer)?
        || envelope["signature"]["signer_endpoint_id"] != member["endpoint_id"]
        || message["route_ready"] != false
        || message["member_pid"].as_u64().is_none_or(|pid| pid == 0)
        || !proof::valid_nonce(nonce)
    {
        return Err(fail("joined_native_identity_mismatch"));
    }
    if message["prepare"]["start"] != assigned[0] || message["prepare"]["end"] != assigned[1] {
        return Err(fail("joined_native_range_unassigned"));
    }
    let reference = native_compiler::reference().map_err(native)?;
    validate_prepare(offer, &message["prepare"], &reference)?;
    native_compiler::validate_proof(
        &message["proof"],
        &stage(&message["prepare"]),
        nonce,
        &reference,
    )
    .map_err(native)?;
    Ok(message["proof"].clone())
}

/// Owner-side resident-proof challenges: ONE fresh nonce per member and
/// assignment, consumed by the first proof that verifies. A replayed, foreign,
/// or other-member nonce is refused; nonces are never shared between stages.
#[derive(Default)]
pub struct Challenges {
    outstanding: std::sync::Mutex<std::collections::BTreeMap<String, (String, [u64; 2])>>,
}
/// A stage admitted from a consumed challenge; only `Challenges::admit` builds it.
#[derive(Clone, Debug)]
pub struct Admitted {
    offer: Value,
    proof: Value,
    nonce: String,
    range: [u64; 2],
}
impl Admitted {
    pub fn offer(&self) -> &Value {
        &self.offer
    }
    pub fn proof(&self) -> &Value {
        &self.proof
    }
    pub fn nonce(&self) -> &str {
        &self.nonce
    }
    pub fn range(&self) -> [u64; 2] {
        self.range
    }
}
impl Challenges {
    pub fn new() -> Self {
        Self::default()
    }
    /// Issue a fresh nonce for this member's assigned range; it replaces any
    /// earlier unconsumed nonce for the same member.
    pub fn issue(&self, node_id: &str, range: [u64; 2]) -> Result<String> {
        if node_id.is_empty() || range[0] >= range[1] || range[1] > 28 {
            return Err(fail("joined_native_challenge_invalid"));
        }
        let nonce = crate::app_host::hex(
            &crate::app_host::random(16).map_err(|_| fail("joined_native_challenge_invalid"))?,
        );
        let mut held = self
            .outstanding
            .lock()
            .map_err(|_| fail("joined_native_challenge_poisoned"))?;
        held.retain(|_, (node, _)| node != node_id);
        held.insert(nonce.clone(), (node_id.to_owned(), range));
        Ok(nonce)
    }
    /// Verify the proof against its member's outstanding challenge, then consume it.
    pub fn admit(
        &self,
        coordinator: &seed::Coordinator,
        offer: &Value,
        envelope: &Value,
    ) -> Result<Admitted> {
        let nonce = envelope["message"]["proof"]["nonce"]
            .as_str()
            .ok_or(fail("joined_native_nonce_unknown"))?
            .to_owned();
        let node = text(&offer["message"]["member"], "node_id")?;
        let mut held = self
            .outstanding
            .lock()
            .map_err(|_| fail("joined_native_challenge_poisoned"))?;
        let range = match held.get(&nonce) {
            Some((owner, range)) if owner == node => *range,
            _ => return Err(fail("joined_native_nonce_unknown")),
        };
        verify_resident(coordinator, offer, &nonce, range, envelope)?;
        held.remove(&nonce);
        Ok(Admitted {
            offer: offer.clone(),
            proof: envelope.clone(),
            nonce,
            range,
        })
    }
}

/// Sole owner of both inert custody and the child. No shard disposal is exposed
/// until explicit successful native close/reap. Drop never refunds byte debt.
pub struct NativeShard {
    received: JoinedShard,
    worker: Option<Worker>,
    started: Option<Instant>,
    wall: std::time::Duration,
    remaining: u64,
    cleanup_proven: bool,
    consent: serde_json::Map<String, Value>,
}
impl NativeShard {
    pub fn new(received: JoinedShard) -> Result<Self> {
        Self::try_new(received).map_err(|refused| refused.0)
    }
    /// As `new`, but a refusal hands the received custody back to the caller,
    /// so its byte debt stays owned and can still be retired.
    pub fn try_new(received: JoinedShard) -> std::result::Result<Self, Box<(Error, JoinedShard)>> {
        let caps = (|| {
            let consent = standing_consent::load(&received.root)
                .map_err(|_| fail("joined_shard_consent_required"))?
                .ok_or(fail("joined_shard_consent_required"))?;
            let seconds = consent["caps"]["max_wall_seconds"]
                .as_u64()
                .filter(|n| *n > 0)
                .ok_or(fail("joined_native_consent_invalid"))?;
            let remaining = consent["caps"]["max_commands"]
                .as_u64()
                .filter(|n| *n > 0)
                .ok_or(fail("joined_native_consent_invalid"))?;
            Ok((consent, seconds, remaining))
        })();
        let (consent, seconds, remaining) = match caps {
            Ok(caps) => caps,
            Err(error) => return Err(Box::new((error, received))),
        };
        Ok(Self {
            received,
            worker: None,
            started: None,
            wall: std::time::Duration::from_secs(seconds),
            remaining,
            cleanup_proven: true,
            consent,
        })
    }
    fn check(&self, current: &dyn Fn() -> Result<Value>) -> Result<()> {
        if current()? != self.received.offer["message"]["member"] {
            return Err(fail("joined_shard_member_changed"));
        }
        let consent = standing_consent::load(&self.received.root)
            .map_err(|_| fail("joined_shard_consent_required"))?
            .ok_or(fail("joined_shard_consent_required"))?;
        let m = &self.received.offer["message"]["member"];
        if consent["state"] != "granted"
            || consent["binding"]
                != json!({"swarm_id":m["swarm_id"],"node_id":m["node_id"],"seed_key_digest":m["seed_key_digest"]})
        {
            return Err(fail("joined_shard_consent_required"));
        }
        if consent != self.consent {
            return Err(fail("joined_native_consent_changed"));
        }
        if self.started.is_some_and(|s| s.elapsed() >= self.wall) || self.remaining == 0 {
            return Err(fail("joined_native_consent_exhausted"));
        }
        Ok(())
    }
    /// Prepares only the received file. Range comes from the independently
    /// supplied assignment, then the B6 loader checks exact tensor ownership.
    #[allow(clippy::too_many_arguments)]
    pub fn prepare(
        &mut self,
        range: [usize; 2],
        tokens: usize,
        nonce: &str,
        signer: &Identity,
        current: &dyn Fn() -> Result<Value>,
        cancel: Arc<AtomicBool>,
        forbidden_source: &Path,
    ) -> Result<Value> {
        let wall = budgets::BudgetId::MemberChosenWall
            .span(budgets::Entries::ZERO)
            .map_err(|_| fail("budget_unavailable"))?;
        self.prepare_until(
            range,
            tokens,
            nonce,
            signer,
            current,
            cancel,
            forbidden_source,
            Instant::now() + std::time::Duration::from_millis(wall.millis()).min(self.wall),
        )
    }
    /// One allowance covers custody hashing, launch, prepare and resident proof.
    #[allow(clippy::too_many_arguments)]
    pub fn prepare_until(
        &mut self,
        range: [usize; 2],
        tokens: usize,
        nonce: &str,
        signer: &Identity,
        current: &dyn Fn() -> Result<Value>,
        cancel: Arc<AtomicBool>,
        forbidden_source: &Path,
        deadline: Instant,
    ) -> Result<Value> {
        let deadline = deadline.min(Instant::now() + self.wall);
        let checkpoint = || {
            if cancel.load(std::sync::atomic::Ordering::SeqCst) {
                Err(fail("compiler_stopped"))
            } else if Instant::now() >= deadline {
                Err(fail("native_worker_deadline"))
            } else {
                Ok(())
            }
        };
        checkpoint()?;
        self.check(current)?;
        // A fresh worker per serving epoch is allowed only after the previous one's
        // close/reap was proven. The consent wall and command budget keep running
        // from the FIRST prepare; a new worker never resets the granted lifetime.
        if self.worker.is_some()
            || !self.cleanup_proven
            || !proof::valid_nonce(nonce)
            || range[0] >= range[1]
            || range[1] > 28
            || tokens == 0
        {
            return Err(fail("joined_native_prepare_invalid"));
        }
        if signer.verification_key_digest()
            != self.received.offer["message"]["member"]["verification_key_digest"]
            || signer.endpoint_id() != self.received.offer["message"]["member"]["endpoint_id"]
        {
            return Err(fail("joined_native_identity_mismatch"));
        }
        // Admission is repeated through each bounded verification read.
        if self.started.is_none() {
            self.started = Some(Instant::now());
        }
        if self.remaining == 0 {
            return Err(fail("joined_native_consent_exhausted"));
        }
        self.remaining -= 1;
        let offer_member = self.received.offer["message"]["member"].clone();
        self.received.shard.verify(&|| {
            checkpoint()?;
            if current()? != offer_member {
                return Err(fail("joined_shard_member_changed"));
            }
            Ok(())
        })?;
        let reference = native_compiler::reference().map_err(native)?;
        let id = &reference["recorded"]["identity"];
        let m = &self.received.offer["message"];
        let consent = standing_consent::load(&self.received.root)
            .map_err(|_| fail("joined_shard_consent_required"))?
            .ok_or(fail("joined_shard_consent_required"))?;
        let memory = consent["caps"]["max_memory_bytes"]
            .as_u64()
            .filter(|n| *n > 0)
            .ok_or(fail("joined_native_consent_invalid"))?;
        let request_id = digest(&self.received.offer)?;
        let prepare = json!({"command":shard::COMMAND,"checkpoint":self.received.path(),
            "start":range[0],"end":range[1],"total":28,"memory":memory,"tokens":tokens,
            "id":request_id.trim_start_matches("sha256:"),"generation":m["member"]["generation"],
            "stage_image":{"model_id":"Qwen/Qwen3-0.6B","revision":id["revision"],
                "image_digest":id["checkpoint_sha256"],"tensor_manifest":id["manifest_sha256"],
                "profile":"packed_int8","layers":{"start":range[0],"end":range[1]},
                "total_layers":28,"source_metadata":{"header_sha256":id["header_sha256"]}},
            "shard":{"protocol":shard::PROTOCOL,"node_id":m["member"]["node_id"],"range":range,
                "transfer":m["transfer"],"recipe":m["recipe"],"recipe_digest":m["recipe"]["node_plan_digest"]}});
        validate_prepare(&self.received.offer, &prepare, &reference)?;
        let config = native_compiler::Config::from_environment()
            .map_err(native)?
            .ok_or(fail("native_binary_pin_required"))?;
        self.check(current)?;
        checkpoint()?;
        self.cleanup_proven = false;
        match Worker::start_scoped_until(
            &config,
            &prepare,
            cancel.clone(),
            forbidden_source,
            deadline,
        ) {
            Ok(worker) => self.worker = Some(worker),
            Err(e) => {
                // A failed start already killed and reaped its exact child; only
                // a failed kill/reap leaves the release unproven (debt retained).
                self.cleanup_proven = e.0 != "native_cleanup_unproven";
                return Err(native(e));
            }
        }
        self.check(current)?;
        let worker = self
            .worker
            .as_mut()
            .ok_or(fail("joined_native_not_prepared"))?;
        // The prepare operation's flag governs this exchange only.
        let (reply, body) = worker
            .request_controlled(
                &json!({"command":proof::COMMAND,"protocol":proof::REQUEST_PROTOCOL,"nonce":nonce}),
                &[],
                deadline,
                cancel.clone(),
            )
            .map_err(native)?;
        if reply["ok"] != true
            || !body.is_empty()
            || reply["proof"]["worker_pid"] != worker.pid().map_err(native)?
        {
            return Err(fail("native_proof_binding_mismatch"));
        }
        native_compiler::validate_proof(&reply["proof"], &stage(&prepare), nonce, &reference)
            .map_err(native)?;
        checkpoint()?;
        self.check(current)?;
        signed(
            signer,
            json!({"protocol":PROTOCOL,"member":current()?,"offer_digest":digest(&self.received.offer)?,
            "prepare":prepare,"proof":reply["proof"],"member_pid":std::process::id(),"route_ready":false}),
        )
    }
    pub fn step(
        &mut self,
        input: Option<&[u32]>,
        payload: &[u8],
        current: &dyn Fn() -> Result<Value>,
    ) -> Result<Vec<u8>> {
        self.check(current)?;
        self.remaining -= 1;
        let command = match input {
            Some(ids) => json!({"command":"step","tokens":ids}),
            None => json!({"command":"step"}),
        };
        let (reply, bytes) = self
            .worker
            .as_mut()
            .ok_or(fail("joined_native_not_prepared"))?
            .request(&command, payload)
            .map_err(native)?;
        if reply != json!({"ok":true}) {
            return Err(fail("native_step_reply_invalid"));
        }
        self.check(current)?;
        Ok(bytes)
    }
    /// Operation and consent bounds both apply; a new operator command cannot
    /// reset the serving worker's granted lifetime.
    pub fn step_until(
        &mut self,
        input: Option<&[u32]>,
        payload: &[u8],
        current: &dyn Fn() -> Result<Value>,
        cancel: Arc<AtomicBool>,
        deadline: Instant,
    ) -> Result<Vec<u8>> {
        self.check(current)?;
        let end = self.started.ok_or(fail("joined_native_not_prepared"))? + self.wall;
        self.remaining -= 1;
        let command = match input {
            Some(ids) => json!({"command":"step","tokens":ids}),
            None => json!({"command":"step"}),
        };
        let (reply, bytes) = self
            .worker
            .as_mut()
            .ok_or(fail("joined_native_not_prepared"))?
            .request_controlled(&command, payload, deadline.min(end), cancel)
            .map_err(native)?;
        if reply != json!({"ok":true}) {
            return Err(fail("native_step_reply_invalid"));
        }
        self.check(current)?;
        Ok(bytes)
    }
    pub fn close(&mut self) -> Result<Value> {
        let worker = self
            .worker
            .as_mut()
            .ok_or(fail("joined_native_not_prepared"))?;
        // A failed graceful close keeps the worker owned for abort().
        let stats = worker.close_in_place().map_err(native)?;
        self.worker = None;
        self.cleanup_proven = true;
        Ok(stats)
    }
    /// After an interrupted step the socket framing is not reusable: kill and
    /// reap the exact owned child. Cleanup is proven only by the OS exit report;
    /// the evidence says `context_closed:false`, never a graceful close.
    pub fn abort(&mut self) -> Result<Value> {
        let worker = self
            .worker
            .take()
            .ok_or(fail("joined_native_not_prepared"))?;
        let evidence = worker.kill_and_reap().map_err(native)?;
        self.cleanup_proven = true;
        Ok(evidence)
    }
    pub fn worker_pid(&self) -> Option<u32> {
        self.worker.as_ref().and_then(|w| w.pid().ok())
    }
    /// The offer this compute custody serves, for route binding checks.
    pub fn offer(&self) -> &Value {
        &self.received.offer
    }
    pub fn discard(&mut self) -> Result<()> {
        if self.worker.is_some() || !self.cleanup_proven {
            return Err(fail("joined_native_cleanup_required"));
        }
        self.received.discard()
    }
}
/// Decodes real native final-stage logits; no expected token enters execution.
pub fn greedy(payload: &[u8]) -> Result<u32> {
    native_compiler::worker::greedy(payload).map_err(native)
}
