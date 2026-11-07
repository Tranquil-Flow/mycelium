// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-FAIL-06 (a dead worker is detected and settles Failed from the surviving agent's signed quorum); B-FAIL-04 (Failed needs every gate normal settlement needs); B-CHAT-19 (settles once, never becomes completed).
//!
//! RU-020 / RU-026: a request whose compute worker died settles `Terminal::Failed` from the
//! normal signed cleanup quorum; no `StageAttempt` change is needed.
//!
//! v0.1.4's member agent survives its native worker and signs the stage-cleanup receipt
//! (`FX-SLICE1-member-process-topology`, executed by SLICE-1 round 9). The agent's signing gate
//! is the L6 environmental observation (`process_gone && resources_gone`, current ticket), not
//! the worker-side `StageAttempt`, which belonged to the dead process. SLICE-1 `3db6f43` builds
//! exactly that (`node-service/src/native/child.rs` `close`, `native.rs` quorum + `settle_failed`)
//! and never calls `StageAttempt`. These tests pin the two halves that stay in this lane:
//!
//! 1. the strict `StageAttempt` gate (`finish_cleanup`) still refuses a dead worker, so the
//!    sticky-error rule is unchanged and visible;
//! 2. `CleanupRound::finish` + `RequestOwner::settle` reach `Failed` with the dead worker's peer
//!    contributing its receipt, and keep every refusal (missing receipt, unproven observation,
//!    stale ticket, second settle, `Completed` after `Failed`, unproven completion).
use budgets::{BudgetId, ClockDomain, Entries, MonoInstant};
use node::{Residue, Supervisor, Ticket, WorkerAdapter, WorkerError};
use router::cleanup::{CleanupPeer, CleanupProof, CleanupRound, RequestOwner, Terminal};
use runtime::worker::{StageAttempt, finish_cleanup};
use serde_json::{Value, json};
use stage_contract::*;
use std::{cell::Cell, rc::Rc};

/// A backend whose process is gone after `die()`: every call then fails like a closed link.
#[derive(Clone)]
struct Mortal {
    dead: Rc<Cell<bool>>,
    /// A proxy whose `close` only drops the local link answers Ok even though the worker is gone.
    close_ok: bool,
}
impl Mortal {
    fn gone(&self) -> Result<()> {
        if self.dead.get() {
            Err(Error::new(ErrorCode::Closed, "worker_link_closed"))
        } else {
            Ok(())
        }
    }
}
impl StageBackend for Mortal {
    type Session = ();
    fn capabilities(&self) -> Capabilities {
        Capabilities {
            architectures: vec![Architecture::Qwen2],
            profiles: vec![NumericsProfile::MlxExplicitF32],
            state: StateKind::AttentionKv,
            rewind: false,
            wire_export: false,
            device: "sim".into(),
            build: "sim".into(),
        }
    }
    fn new_session(&mut self, _: Binding, _: Limits) -> Result<()> {
        self.gone()
    }
    fn prefill(&mut self, _: &mut (), step: Step) -> Result<Output> {
        self.gone()?;
        Ok(Output {
            hidden: Activation {
                shape: [1, 1, 1],
                dtype: DType::F32,
                order: ByteOrder::Little,
                layout: Layout::BatchSequenceHidden,
                bytes: vec![0; 4],
            },
            logits: None,
            state_generation: step.state_generation,
            committed_position: step.position
                + match step.input {
                    Input::Tokens(t) => t.len() as u32,
                    Input::Hidden(a) => a.shape[1],
                },
            state_bytes: 0,
        })
    }
    fn decode(&mut self, s: &mut (), step: Step) -> Result<Output> {
        self.prefill(s, step)
    }
    fn synchronize(&mut self) -> Result<()> {
        self.gone()
    }
    fn export_activation(&self, _: &Activation) -> Result<Vec<u8>> {
        Err(Error::new(ErrorCode::Unsupported, "sim"))
    }
    fn reset(&mut self, _: &mut ()) -> Result<()> {
        self.gone()
    }
    fn cancel(&mut self, _: &mut ()) -> Result<()> {
        self.gone()
    }
    fn release_session(&mut self, _: ()) -> Result<()> {
        self.gone()
    }
    fn close(&mut self) -> Result<()> {
        if self.close_ok { Ok(()) } else { self.gone() }
    }
}

/// The L6 environmental observation of the agent's reaped child.
struct Reaped(bool);
impl WorkerAdapter for Reaped {
    fn request_stop(&mut self) -> std::result::Result<(), WorkerError> {
        Ok(())
    }
    fn observe(&mut self) -> std::result::Result<Residue, WorkerError> {
        Ok(Residue {
            process_gone: self.0,
            resources_gone: self.0,
        })
    }
}

fn binding() -> Binding {
    Binding {
        request: [1; 32],
        assignment: [2; 32],
        load_generation: 3,
        descriptor: StageDescriptor {
            checkpoint: [3; 32],
            revision: "revision".into(),
            tensor_manifest: [4; 32],
            semantics: "fixture".into(),
            architecture: Architecture::Qwen2,
            layers: 0..1,
            embedding: true,
            head: true,
            representation: "float".into(),
            quantizer: "none".into(),
            profile: NumericsProfile::MlxExplicitF32,
            state_schema: "fixture".into(),
        },
    }
}
fn limits() -> Limits {
    Limits {
        weight_bytes: 1024,
        state_bytes: 1024,
        scratch_bytes: 1024,
        max_tokens: 8,
    }
}
fn at(ticks: u64) -> MonoInstant {
    MonoInstant::new(ClockDomain(1), ticks)
}
fn ticket() -> Ticket {
    Ticket {
        epoch: 1,
        run: 1,
        node: "node".into(),
        incarnation: "incarnation".into(),
    }
}
fn vectors() -> Vec<Value> {
    serde_json::from_str(include_str!(
        "../../protocol/fixtures/continuation-cancel-vectors.json"
    ))
    .unwrap()
}
fn peer(context: &Value) -> CleanupPeer {
    CleanupPeer {
        node: context["node_id"].as_str().unwrap().into(),
        host: context["host_id"].as_str().unwrap().into(),
        process: context["process_id"].clone(),
        run: context["run_id"].as_str().unwrap().into(),
        deployment: context["deployment_id"].as_str().unwrap().into(),
        endpoint: context["endpoint_id"].as_str().unwrap().into(),
        key: context["key"].clone(),
        generation: json!(5),
    }
}
/// Round for the recorded vector; `signed` = the surviving agent's receipt arrived.
fn round(signed: bool) -> (CleanupRound, Value) {
    let rows = vectors();
    let row = &rows[0];
    let context = &row["context"];
    let p = peer(context);
    let mut round = CleanupRound::new(
        context["subject"].clone(),
        vec![p.clone()],
        at(0)
            .checked_add(BudgetId::CancelAndCleanup.span(Entries::ZERO).unwrap())
            .unwrap(),
    )
    .unwrap();
    if signed {
        let response = serde_json::from_str(row["envelope_utf8"].as_str().unwrap()).unwrap();
        round.accept(&p.node, &response, || (at(0), true)).unwrap();
    }
    (round, context["subject"].clone())
}
/// A worker that was alive, then died: the agent's stage has sticky errors.
fn dead_stage() -> StageAttempt<Mortal> {
    dead_stage_with(false)
}
fn dead_stage_with(close_ok: bool) -> StageAttempt<Mortal> {
    let dead = Rc::new(Cell::new(false));
    let mut stage = StageAttempt::new(
        Mortal {
            dead: dead.clone(),
            close_ok,
        },
        binding(),
        limits(),
        4,
    )
    .unwrap();
    dead.set(true);
    assert!(
        stage.close().is_err(),
        "a dead worker cannot close its stage"
    );
    stage
}
/// The agent's signing gate (SLICE-1 `3db6f43`): the observation of the reaped child, bound to the
/// current ticket. It does not consult the dead worker's `StageAttempt`.
fn agent_gate(
    observation: &node::CleanupObservation,
    expected: &Ticket,
) -> std::result::Result<(), protocol::ContractError> {
    if observation.ticket() != expected || !observation.proven() {
        return Err(protocol::ContractError("worker_cleanup_unproven"));
    }
    Ok(())
}
fn observe_child(proven: bool, ticket: Ticket) -> node::CleanupObservation {
    Supervisor::new(ticket, Reaped(proven))
        .observe_cleanup()
        .unwrap()
}
fn proof(
    signed: bool,
    proven: bool,
    ticket_of_child: Ticket,
) -> std::result::Result<CleanupProof, String> {
    let (round, _) = round(signed);
    let observation = observe_child(proven, ticket_of_child);
    round
        .finish(
            || agent_gate(&observation, &self::ticket()),
            || (at(0), true),
        )
        .map_err(|e| e.0.to_owned())
}
fn owner() -> RequestOwner {
    RequestOwner::new(round(false).1).unwrap()
}

#[test]
fn the_strict_stage_gate_still_refuses_a_stage_whose_worker_died() {
    // The unchanged rule: a consumed cleanup error is never erased, so a StageAttempt whose
    // process died is never clean() and `finish_cleanup` (the in-process owner's check) refuses
    // it even though the environment proves the child gone. SLICE-1's agent does not use it.
    let mut stage = dead_stage();
    assert!(!stage.clean());
    assert_eq!(
        stage.cleanup_errors().len(),
        4,
        "cancel, sync, release, close"
    );
    assert!(stage.close().is_err(), "a retry cannot erase the failure");
    assert!(!stage.clean());
    let mut supervisor = Supervisor::new(ticket(), Reaped(true));
    let (quorum, _) = round(true);
    let refused = finish_cleanup(quorum, &stage, &mut supervisor, &ticket(), || {
        (at(0), Some(ticket()))
    });
    assert_eq!(refused.err().unwrap().0, "worker_cleanup_unproven");
}

#[test]
fn a_local_close_that_succeeds_does_not_erase_the_consumed_release_errors() {
    // The agent's proxy can close its own link fine: `closed` is then true, the session is gone,
    // and only the three consumed errors (cancel, sync, release) keep the stage unclean.
    let mut stage = dead_stage_with(true);
    assert!(!stage.clean());
    assert_eq!(stage.cleanup_errors().len(), 3);
    assert!(stage.close().is_err(), "closing again cannot erase them");
    assert!(!stage.clean());
    let mut supervisor = Supervisor::new(ticket(), Reaped(true));
    let (quorum, _) = round(true);
    let refused = finish_cleanup(quorum, &stage, &mut supervisor, &ticket(), || {
        (at(0), Some(ticket()))
    });
    assert_eq!(refused.err().unwrap().0, "worker_cleanup_unproven");
}

#[test]
fn a_dead_worker_settles_failed_from_the_surviving_agents_signed_quorum() {
    // The peer's receipt exists (its agent lived), the child is observed gone at the current
    // ticket: the quorum completes with a stage that is NOT clean, and Failed is accepted.
    let stage = dead_stage();
    assert!(!stage.clean());
    let proof = proof(true, true, ticket()).expect("quorum complete, child gone");
    let mut owner = owner();
    assert_eq!(owner.terminal(), None);
    owner
        .settle(proof, Terminal::Failed, || (at(0), true))
        .unwrap();
    assert_eq!(owner.terminal(), Some(Terminal::Failed));
}

#[test]
fn failed_needs_every_gate_the_normal_settlement_needs() {
    // Withheld signature: the quorum rule is intact (no `finish_peer_unreachable`).
    assert_eq!(
        proof(false, true, ticket()).err().unwrap(),
        "cleanup_quorum_missing"
    );
    // Child not observed gone (process or its resources): the agent must not sign / settle.
    assert_eq!(
        proof(true, false, ticket()).err().unwrap(),
        "worker_cleanup_unproven"
    );
    // Observation of a previous generation.
    let mut stale = ticket();
    stale.run += 1;
    assert_eq!(
        proof(true, true, stale).err().unwrap(),
        "worker_cleanup_unproven"
    );
    // Authority lost before the proof is issued.
    let (quorum, _) = round(true);
    let observation = observe_child(true, ticket());
    let lost = quorum.finish(|| agent_gate(&observation, &ticket()), || (at(0), false));
    assert_eq!(lost.err().unwrap().0, "cleanup_authority_stale");
}

#[test]
fn a_failed_request_settles_once_and_never_becomes_completed() {
    let mut owner = owner();
    owner
        .settle(
            proof(true, true, ticket()).unwrap(),
            Terminal::Failed,
            || (at(0), true),
        )
        .unwrap();
    for outcome in [Terminal::Failed, Terminal::Completed, Terminal::Cancelled] {
        let again = owner.settle(proof(true, true, ticket()).unwrap(), outcome, || {
            (at(0), true)
        });
        assert_eq!(again.err().unwrap().0, "request_already_terminal");
        assert_eq!(owner.terminal(), Some(Terminal::Failed));
    }
}

#[test]
fn failure_does_not_borrow_completion_or_cancellation() {
    // A dead worker never turns an unfinished request into Completed/Cancelled.
    let mut owner = owner();
    let completed = owner.settle(
        proof(true, true, ticket()).unwrap(),
        Terminal::Completed,
        || (at(0), true),
    );
    assert_eq!(completed.err().unwrap().0, "request_completion_unproven");
    let cancelled = owner.settle(
        proof(true, true, ticket()).unwrap(),
        Terminal::Cancelled,
        || (at(0), true),
    );
    assert_eq!(cancelled.err().unwrap().0, "request_cancellation_unproven");
    assert_eq!(owner.terminal(), None, "refusals leave the owner unsettled");
    // A proof for another request is refused for Failed too.
    let other = RequestOwner::new({
        let mut subject = round(false).1;
        subject["request_id"] = json!("request-other");
        subject
    });
    let mut other = other.unwrap();
    let stale = other.settle(
        proof(true, true, ticket()).unwrap(),
        Terminal::Failed,
        || (at(0), true),
    );
    assert_eq!(stale.err().unwrap().0, "cleanup_subject_stale");
}
