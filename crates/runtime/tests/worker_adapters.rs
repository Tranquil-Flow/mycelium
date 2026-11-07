// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-FAIL-04 (every cleanup effect runs and no retry erases a failure; the quorum needs the L6 environment, the L8 release and a fresh ticket); B-CHAT-18 (partial: stage generations fenced before settlement).
//!
//! Actual L5 verifier + L6 Supervisor + L8 trait. Environmental/backend effects
//! are deterministic fakes; no subprocess, network, GPU or live reservation.
use budgets::{BudgetId, ClockDomain, Entries, MonoInstant};
use node::{Residue, Supervisor, Ticket, WorkerAdapter, WorkerError};
use router::cleanup::*;
use runtime::worker::*;
use serde_json::{Value, json};
use stage_contract::*;
use std::{cell::RefCell, rc::Rc};
#[derive(Clone)]
struct Fake {
    calls: Rc<RefCell<Vec<&'static str>>>,
    failure: Option<&'static str>,
    stale: bool,
}
impl Fake {
    fn call(&self, s: &'static str) -> Result<()> {
        self.calls.borrow_mut().push(s);
        if self.failure == Some(s) {
            Err(Error::new(ErrorCode::Cleanup, s))
        } else {
            Ok(())
        }
    }
}
impl StageBackend for Fake {
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
        self.call("new")
    }
    fn prefill(&mut self, _: &mut (), s: Step) -> Result<Output> {
        self.call("prefill")?;
        let n = match s.input {
            Input::Tokens(t) => t.len() as u32,
            Input::Hidden(a) => a.shape[1],
        };
        Ok(Output {
            hidden: Activation {
                shape: [1, 1, 1],
                dtype: DType::F32,
                order: ByteOrder::Little,
                layout: Layout::BatchSequenceHidden,
                bytes: vec![0; 4],
            },
            logits: None,
            state_generation: if self.stale {
                s.state_generation + 1
            } else {
                s.state_generation
            },
            committed_position: s.position + n,
            state_bytes: 0,
        })
    }
    fn decode(&mut self, s: &mut (), step: Step) -> Result<Output> {
        self.prefill(s, step)
    }
    fn synchronize(&mut self) -> Result<()> {
        self.call("sync")
    }
    fn export_activation(&self, _: &Activation) -> Result<Vec<u8>> {
        Err(Error::new(ErrorCode::Unsupported, "sim"))
    }
    fn reset(&mut self, _: &mut ()) -> Result<()> {
        self.call("reset")
    }
    fn cancel(&mut self, _: &mut ()) -> Result<()> {
        self.call("cancel")
    }
    fn release_session(&mut self, _: ()) -> Result<()> {
        self.call("release")
    }
    fn close(&mut self) -> Result<()> {
        self.call("close")
    }
}
struct Environment(bool);
impl WorkerAdapter for Environment {
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
fn fake(failure: Option<&'static str>, stale: bool) -> Fake {
    Fake {
        calls: Rc::new(RefCell::new(Vec::new())),
        failure,
        stale,
    }
}
fn at() -> MonoInstant {
    MonoInstant::new(ClockDomain(1), 0)
}
fn ticket() -> Ticket {
    Ticket {
        epoch: 1,
        run: 1,
        node: "node".into(),
        incarnation: "incarnation".into(),
    }
}
fn round() -> CleanupRound {
    let rows: Value = serde_json::from_str(include_str!(
        "../../protocol/fixtures/continuation-cancel-vectors.json"
    ))
    .unwrap();
    let r = &rows[0];
    let c = &r["context"];
    let p = CleanupPeer {
        node: c["node_id"].as_str().unwrap().into(),
        host: c["host_id"].as_str().unwrap().into(),
        process: c["process_id"].clone(),
        run: c["run_id"].as_str().unwrap().into(),
        deployment: c["deployment_id"].as_str().unwrap().into(),
        endpoint: c["endpoint_id"].as_str().unwrap().into(),
        key: c["key"].clone(),
        generation: json!(5),
    };
    let mut round = CleanupRound::new(
        c["subject"].clone(),
        vec![p.clone()],
        at().checked_add(BudgetId::CancelAndCleanup.span(Entries::ZERO).unwrap())
            .unwrap(),
    )
    .unwrap();
    let response = serde_json::from_str(r["envelope_utf8"].as_str().unwrap()).unwrap();
    round.accept(&p.node, &response, || (at(), true)).unwrap();
    round
}
#[test]
fn full_context_replay_uses_real_stage_trait_and_signed_l6_l8_cleanup_adapter() {
    use router::recovery::{Checkpoint, Identity, Policy, Recovery};
    let rows: Value = serde_json::from_str(include_str!(
        "../../protocol/fixtures/continuation-cancel-vectors.json"
    ))
    .unwrap();
    let successor = Identity::from_subject(&rows[0]["context"]["subject"]).unwrap();
    let mut source = successor.clone();
    source.request_attempt = 0;
    source.path = "source".into();
    source.command = "source-command".into();
    source.path_digest = format!("sha256:{}", "c".repeat(64));
    let deadline = at()
        .checked_add(BudgetId::WorkerDefaultWall.span(Entries::ZERO).unwrap())
        .unwrap();
    let mut recovery = Recovery::new(
        Checkpoint {
            source: source.clone(),
            prompt: vec![10, 11],
            committed: vec![12],
            output_limit: 3,
            source_placements: vec!["old".into()],
            expires: deadline,
        },
        at(),
        Policy::registry().unwrap(),
    )
    .unwrap();
    recovery
        .reserve(successor.clone(), vec!["new".into()], at())
        .unwrap();
    recovery.qualified(at(), &source).unwrap();
    recovery.fenced(at(), &source).unwrap();
    let mut stage =
        StageAttempt::new_recovery(fake(None, false), binding(), limits(), 4, successor.clone())
            .unwrap();
    let output = stage.replay_prefill(&recovery, [5; 32]).unwrap();
    assert_eq!(output.committed_position, 3);
    recovery.token(&successor, 0, 14, at()).unwrap();
    recovery.executed(&successor, at()).unwrap();
    assert!(recovery.commit(&source, true, at()).is_err());
    stage.close().unwrap();
    let mut supervisor = Supervisor::new(ticket(), Environment(true));
    let proof = finish_cleanup(round(), &stage, &mut supervisor, &ticket(), || {
        (at(), Some(ticket()))
    })
    .unwrap();
    recovery.cleanup(&successor, proof, at()).unwrap();
    recovery.commit(&source, true, at()).unwrap();
    assert_eq!(recovery.published(), &[(1, 14)]);
}
#[test]
fn durable_l6_reservation_release_requires_every_proof_and_rejects_stale_owner_cas() {
    use budgets::WallExpiry;
    use node::{Caps, Core, Effect, Now, Plan, Planner, PlanningMember};
    struct PlanFixture;
    impl Planner for PlanFixture {
        fn plan(&mut self, m: &[PlanningMember]) -> node::Result<Plan> {
            Ok(Plan {
                assignments: m.iter().map(|m| (m.node.clone(), m.caps.clone())).collect(),
                stop_ms: BudgetId::ObservedStop.span(Entries::ZERO)?.millis(),
                start_ms: BudgetId::WorkerStartup.span(Entries::ZERO)?.millis(),
                safety_ms: BudgetId::WorkerCleanupReserve.span(Entries::ZERO)?.millis(),
            })
        }
    }
    let root = std::env::temp_dir().join(format!("v02-l9-settlement-{}", std::process::id()));
    std::fs::create_dir(&root).unwrap();
    let path = root.join("owner.sqlite3");
    let now = Now {
        mono: at(),
        wall: WallExpiry::from_unix_millis(0),
    };
    let owner = node::Binding {
        swarm_id: "fixture".into(),
        node_id: "owner".into(),
        seed_key_digest: "fixture-seed".into(),
    };
    let caps = Caps {
        storage_bytes: BudgetId::FreshStorageConsent.bytes().unwrap().get(),
        memory_bytes: BudgetId::SavedStorageConsent.bytes().unwrap().get(),
        wall_ms: BudgetId::WorkerDefaultWall
            .span(Entries::ZERO)
            .unwrap()
            .millis(),
        commands: BudgetId::ReplayKeep.count().unwrap().get(),
    };
    let busy = BudgetId::PyMyceliumA13MacosManagedControlManagedcontrolmailboxConnectTimeoutL823
        .span(Entries::ZERO)
        .unwrap();
    let (mut core, _) =
        Core::open_with_policy(&path, owner.clone(), caps.clone(), now, busy).unwrap();
    core.plan(&mut PlanFixture, now).unwrap();
    let effects = core.start(now).unwrap();
    let ticket = match &effects[0] {
        Effect::Start { ticket, .. } => ticket.clone(),
        _ => panic!("start effect"),
    };
    core.started(&ticket, at(), now).unwrap();
    core.cancel(now).unwrap();
    let mut stage = StageAttempt::new(fake(None, false), binding(), limits(), 4).unwrap();
    let mut supervisor = Supervisor::new(ticket.clone(), Environment(true));
    let rows: Value = serde_json::from_str(include_str!(
        "../../protocol/fixtures/continuation-cancel-vectors.json"
    ))
    .unwrap();
    let subject = rows[0]["context"]["subject"].clone();
    let proof = round().finish(|| Ok(()), || (at(), true)).unwrap();
    assert!(
        settle_worker(
            &mut core,
            &stage,
            &mut supervisor,
            &ticket,
            &proof,
            &subject,
            now
        )
        .is_err()
    );
    assert_eq!(core.state().workers.len(), 1);
    stage.close().unwrap();
    let (mut reopened, _) = Core::open_with_policy(&path, owner, caps, now, busy).unwrap();
    assert!(
        settle_worker(
            &mut core,
            &stage,
            &mut supervisor,
            &ticket,
            &proof,
            &subject,
            now
        )
        .is_err()
    );
    assert_eq!(reopened.state().workers.len(), 1);
    let mut wrong = ticket.clone();
    wrong.run += 1;
    assert!(
        settle_worker(
            &mut reopened,
            &stage,
            &mut supervisor,
            &wrong,
            &proof,
            &subject,
            now
        )
        .is_err()
    );
    settle_worker(
        &mut reopened,
        &stage,
        &mut supervisor,
        &ticket,
        &proof,
        &subject,
        now,
    )
    .unwrap();
    assert!(reopened.state().workers.is_empty());
    assert!(
        settle_worker(
            &mut reopened,
            &stage,
            &mut supervisor,
            &ticket,
            &proof,
            &subject,
            now
        )
        .is_err()
    );
    drop(core);
    drop(reopened);
    std::fs::remove_dir_all(root).unwrap();
}
#[test]
fn stage_input_and_output_generations_are_fenced_before_settlement() {
    for stale in [false, true] {
        let f = fake(None, stale);
        let calls = f.calls.clone();
        let mut a = StageAttempt::new(f, binding(), limits(), 4).unwrap();
        let step = Step {
            binding: binding(),
            state_generation: 4,
            position: 0,
            payload: [5; 32],
            input: Input::Tokens(vec![10, 11, 12]),
        };
        let mut bad = step.clone();
        bad.binding.load_generation += 1;
        assert!(a.run(bad, true).is_err());
        assert_eq!(*calls.borrow(), vec!["new"]);
        let output = a.run(step.clone(), true);
        assert_eq!(output.is_err(), stale);
        assert!(a.run(step, true).is_err());
        a.close().unwrap();
        assert!(a.clean());
    }
}
#[test]
fn every_cleanup_effect_runs_and_no_later_retry_erases_failure() {
    for failure in ["cancel", "sync", "release", "close"] {
        let f = fake(Some(failure), false);
        let calls = f.calls.clone();
        let mut a = StageAttempt::new(f, binding(), limits(), 4).unwrap();
        assert!(a.close().is_err());
        assert!(!a.clean());
        assert_eq!(
            *calls.borrow(),
            vec!["new", "cancel", "sync", "release", "close"]
        );
        assert!(a.close().is_err());
        assert!(!a.cleanup_errors().is_empty());
        let mut s = Supervisor::new(ticket(), Environment(true));
        assert!(finish_cleanup(round(), &a, &mut s, &ticket(), || (at(), Some(ticket()))).is_err());
    }
}
#[test]
fn signed_quorum_needs_l6_environment_l8_release_and_fresh_ticket() {
    for mode in 0..5 {
        let mut a = StageAttempt::new(fake(None, false), binding(), limits(), 4).unwrap();
        if mode != 0 {
            a.close().unwrap();
        }
        let mut old = ticket();
        old.run += 1;
        let mut s = Supervisor::new(
            if mode == 2 { old.clone() } else { ticket() },
            Environment(mode != 1),
        );
        let mut reads = 0;
        let proof = finish_cleanup(round(), &a, &mut s, &ticket(), || {
            reads += 1;
            (
                at(),
                Some(if mode == 3 && reads > 1 {
                    old.clone()
                } else {
                    ticket()
                }),
            )
        });
        assert_eq!(proof.is_ok(), mode == 4, "mode {mode}");
    }
}
