// SPDX-License-Identifier: AGPL-3.0-only
// BACKLOG 8 / L6 round 7: `Core::plan` and `continue_rollover` call the `Planner` INSIDE the
// transition, i.e. under whatever lock the application keeps the Core in. A planner that
// blocks, does I/O or re-enters the Core would reproduce the 2026-10-27 starvation. Rust cannot
// forbid a trait implementation from blocking, so purity is enforced structurally: the planner
// runs on an immutable `PlanningRequest` snapshot OUTSIDE the Core (`plan_request` takes `&self`,
// `apply_plan` re-validates against the live state), and the inline entry points are built from
// exactly those two steps. These tests pin that composition.
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant, WallExpiry};
use node::*;
use std::{
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
        mpsc,
    },
    time::Duration,
};
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Temp(PathBuf);
impl Drop for Temp {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}
fn temp() -> Temp {
    let p = std::env::temp_dir().join(format!(
        "v02-l6-pure-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::SeqCst)
    ));
    std::fs::create_dir(&p).unwrap();
    Temp(p)
}
fn ms(b: B) -> u64 {
    b.span(Entries::ZERO).unwrap().millis()
}
fn now(t: u64) -> Now {
    Now {
        mono: MonoInstant::new(ClockDomain(7), t),
        wall: WallExpiry::from_unix_millis(t),
    }
}
fn binding(node: &str) -> Binding {
    Binding {
        swarm_id: "swarm-fixture".into(),
        node_id: node.into(),
        seed_key_digest: "fixture-seed".into(),
    }
}
fn caps(owner: bool) -> Caps {
    Caps {
        storage_bytes: if owner {
            B::FreshStorageConsent
        } else {
            B::SavedStorageConsent
        }
        .bytes()
        .unwrap()
        .get(),
        memory_bytes: B::SavedStorageConsent.bytes().unwrap().get(),
        wall_ms: ms(if owner {
            B::WorkerDefaultWall
        } else {
            B::MemberChosenWall
        }),
        commands: B::ReplayKeep.count().unwrap().get(),
    }
}
fn bounds() -> ConsentBounds {
    ConsentBounds {
        minimum_storage: budgets::Bytes::ZERO,
        policy_storage: B::FreshStorageConsent.bytes().unwrap(),
        minimum_memory: budgets::Bytes::ZERO,
        maximum_memory: B::FreshStorageConsent.bytes().unwrap(),
        device_memory_ceiling: B::FreshStorageConsent.bytes().unwrap(),
        minimum_wall: budgets::Span::ZERO,
        maximum_wall: B::WorkerMaximumWall.span(Entries::ZERO).unwrap(),
        maximum_commands: B::ReplayCapacity.count().unwrap(),
    }
}
fn member() -> control::MemberBinding {
    control::MemberBinding {
        target_node_id: "member".into(),
        target_endpoint_id: "endpoint-member".into(),
        target_key_digest: "fixture-key".into(),
        membership_generation: "2".into(),
        member_incarnation: "incarnation-2".into(),
        run_id: "fixture".into(),
        deployment_id: "fixture".into(),
    }
}
fn grant() -> Consent {
    Consent::record(
        true,
        &disclosure_digest(),
        caps(false),
        binding("member"),
        &bounds(),
        0,
    )
    .unwrap()
}
fn plan_for(m: &[PlanningMember]) -> Plan {
    Plan {
        assignments: m.iter().map(|m| (m.node.clone(), m.caps.clone())).collect(),
        stop_ms: ms(B::ObservedStop),
        start_ms: ms(B::WorkerStartup),
        safety_ms: ms(B::WorkerCleanupReserve),
    }
}
struct Stub;
impl Planner for Stub {
    fn plan(&mut self, m: &[PlanningMember]) -> Result<Plan> {
        Ok(plan_for(m))
    }
}
fn report(c: &mut Core, t: u64) {
    let epoch = c.state().epoch;
    c.report(
        "member",
        "2",
        epoch,
        WallExpiry::from_unix_millis(t + ms(B::MembershipTtl)),
        now(t),
    )
    .unwrap();
}
fn ready(t: &Temp, name: &str) -> Core {
    let (mut c, e) = Core::open(&t.0.join(name), binding("owner"), caps(true), now(0)).unwrap();
    assert!(e.is_empty());
    c.join(&member()).unwrap();
    c.consent(grant()).unwrap();
    report(&mut c, 0);
    c
}
/// Fail by deadline instead of hanging the suite when the code under test deadlocks.
fn within<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> T {
    let (tx, rx) = mpsc::channel();
    let worker = std::thread::spawn(move || tx.send(f()));
    let value = rx
        .recv_timeout(Duration::from_millis(ms(B::WorkerStartup)))
        .expect("operation deadlocked or exceeded its deadline");
    worker
        .join()
        .expect("operation thread panicked")
        .expect("result receiver alive");
    value
}
/// Reads the Core through the application's shared lock WHILE it is planning.
struct Reentrant(Arc<Mutex<Core>>);
impl Planner for Reentrant {
    fn plan(&mut self, m: &[PlanningMember]) -> Result<Plan> {
        // With the planner inside the transition this lock is held by the caller and
        // `try_lock` fails (a blocking `lock()` would deadlock).
        let core = self
            .0
            .try_lock()
            .expect("planner ran while the Core lock was held");
        assert_eq!(core.state().phase, RunPhase::Idle);
        Ok(plan_for(m))
    }
}
#[test]
fn planner_runs_outside_the_core_lock_and_may_read_the_core() {
    let t = temp();
    let shared = Arc::new(Mutex::new(ready(&t, "a.db")));
    let request = shared.lock().unwrap().plan_request(now(1)).unwrap();
    let plan = within({
        let shared = shared.clone();
        let members = request.members().to_vec();
        move || Reentrant(shared).plan(&members).unwrap()
    });
    shared
        .lock()
        .unwrap()
        .apply_plan(request, plan, now(1))
        .unwrap();
    assert_eq!(shared.lock().unwrap().state().phase, RunPhase::Planned);
}
#[test]
fn split_and_inline_planning_commit_the_identical_state() {
    let t = temp();
    let mut inline = ready(&t, "inline.db");
    let mut split = ready(&t, "split.db");
    inline.plan(&mut Stub, now(1)).unwrap();
    let request = split.plan_request(now(1)).unwrap();
    let plan = Stub.plan(request.members()).unwrap();
    split.apply_plan(request, plan, now(1)).unwrap();
    assert_eq!(inline.state(), split.state());
}
#[test]
fn a_request_made_before_the_state_changed_is_refused_without_effect() {
    let t = temp();
    let mut core = ready(&t, "stale.db");
    let request = core.plan_request(now(1)).unwrap();
    let plan = Stub.plan(request.members()).unwrap();
    // Anything that changes live state while the planner ran invalidates its answer.
    report(&mut core, 2);
    let before = core.state().clone();
    let err = core.apply_plan(request, plan, now(3)).unwrap_err();
    assert!(
        matches!(err, Error::Refused("planning_request_stale")),
        "{err:?}"
    );
    assert_eq!(core.state(), &before);
}
#[test]
fn a_plan_for_other_members_is_still_refused_by_the_core() {
    let t = temp();
    let mut core = ready(&t, "other.db");
    let request = core.plan_request(now(1)).unwrap();
    let mut plan = Stub.plan(request.members()).unwrap();
    plan.assignments.remove("member");
    let err = core.apply_plan(request, plan, now(1)).unwrap_err();
    assert!(
        matches!(err, Error::Refused("plan_member_set_mismatch")),
        "{err:?}"
    );
    assert_eq!(core.state().phase, RunPhase::Idle);
}
#[test]
fn a_request_cannot_be_applied_after_its_lease_expired() {
    let t = temp();
    let mut core = ready(&t, "expired.db");
    let request = core.plan_request(now(1)).unwrap();
    let plan = Stub.plan(request.members()).unwrap();
    let late = ms(B::MembershipTtl) + 1;
    let err = core.apply_plan(request, plan, now(late)).unwrap_err();
    assert!(
        !matches!(err, Error::Refused("planning_request_stale")),
        "{err:?}"
    );
    assert_eq!(core.state().phase, RunPhase::Idle);
}
#[test]
fn the_inline_entry_points_are_exactly_request_then_apply() {
    // The structural guarantee: there is no second planner call site inside a transition.
    let lifecycle = include_str!("../src/lifecycle.rs");
    let continuation = include_str!("../src/continuation.rs");
    let calls = |source: &str| source.matches("planner.plan(").count();
    assert_eq!(
        calls(lifecycle),
        1,
        "only Core::plan may invoke the planner"
    );
    assert_eq!(
        calls(continuation),
        1,
        "only continue_rollover may invoke the planner"
    );
    for (file, source, entry) in [
        ("lifecycle.rs", lifecycle, "pub fn plan("),
        ("continuation.rs", continuation, "pub fn continue_rollover("),
    ] {
        let start = source.find(entry).unwrap();
        let body = &source[start..];
        let body = &body[..body.find("\n    }\n").unwrap()];
        assert!(
            body.contains("planner.plan(")
                && (body.contains("plan_request(") || body.contains("rollover_request(")),
            "{file}: {entry} must be request -> planner -> apply"
        );
    }
}
struct Gone;
impl WorkerAdapter for Gone {
    fn request_stop(&mut self) -> std::result::Result<(), WorkerError> {
        Ok(())
    }
    fn observe(&mut self) -> std::result::Result<Residue, WorkerError> {
        Ok(Residue {
            process_gone: true,
            resources_gone: true,
        })
    }
}
/// A Core whose first run hit its wall-limit rollover and whose cleanup is proven: the next
/// step is `continue_rollover` with fresh descriptors. Returns the rollover instant.
fn rolled_core(t: &Temp, name: &str) -> (Core, u64) {
    let mut core = ready(t, name);
    core.plan(&mut Stub, now(1)).unwrap();
    let tickets: Vec<_> = core
        .start(now(1))
        .unwrap()
        .into_iter()
        .map(|e| match e {
            Effect::Start { ticket, .. } => ticket,
            _ => panic!("unexpected effect"),
        })
        .collect();
    for ticket in &tickets {
        core.started(ticket, now(1).mono, now(1)).unwrap();
    }
    let at = core.state().rollover_at.unwrap().ticks;
    let mut renewal = ms(B::LeaseRenewalInterval);
    while renewal < at {
        report(&mut core, renewal);
        renewal += ms(B::LeaseRenewalInterval);
    }
    report(&mut core, at - 1);
    let stops = core.tick(now(at)).unwrap();
    for effect in stops {
        if let Effect::Stop { ticket, .. } = effect {
            let proof = Supervisor::new(ticket, Gone).observe_cleanup().unwrap();
            core.cleaned(&proof, now(at)).unwrap();
        }
    }
    (core, at)
}
fn fresh_reports(at: u64) -> [RolloverReport; 1] {
    [RolloverReport {
        binding: member(),
        expiry: WallExpiry::from_unix_millis(at + ms(B::MembershipTtl)),
    }]
}
#[test]
fn rollover_planning_also_runs_outside_the_core_lock() {
    let t = temp();
    let (core, at) = rolled_core(&t, "roll.db");
    let reports = fresh_reports(at);
    let shared = Arc::new(Mutex::new(core));
    let request = shared
        .lock()
        .unwrap()
        .rollover_request(&reports, now(at))
        .unwrap();
    let plan = within({
        let shared = shared.clone();
        let members = request.members().to_vec();
        move || Reentrant(shared).plan(&members).unwrap()
    });
    let effects = shared
        .lock()
        .unwrap()
        .finish_rollover(request, plan, now(at))
        .unwrap();
    assert!(effects.iter().any(|e| matches!(e, Effect::Start { .. })));
}
#[test]
fn a_rollover_request_made_before_the_state_changed_is_refused_without_effect() {
    let t = temp();
    let (mut core, at) = rolled_core(&t, "roll-stale.db");
    let reports = fresh_reports(at);
    let request = core.rollover_request(&reports, now(at)).unwrap();
    let plan = Stub.plan(request.members()).unwrap();
    // The member renewed while the planner ran: the answer was computed for older state.
    let epoch = core.state().epoch;
    core.report(
        "member",
        "2",
        epoch,
        WallExpiry::from_unix_millis(at + 1 + ms(B::MembershipTtl)),
        now(at + 1),
    )
    .unwrap();
    let before = core.state().clone();
    let err = core
        .finish_rollover(request, plan, now(at + 1))
        .unwrap_err();
    assert!(
        matches!(err, Error::Refused("planning_request_stale")),
        "{err:?}"
    );
    assert_eq!(core.state(), &before);
}
#[test]
fn split_and_inline_rollover_commit_the_identical_state() {
    let t = temp();
    let (mut inline, at) = rolled_core(&t, "roll-inline.db");
    let (mut split, _) = rolled_core(&t, "roll-split.db");
    let reports = fresh_reports(at);
    let a = inline
        .continue_rollover(&reports, &mut Stub, now(at))
        .unwrap();
    let request = split.rollover_request(&reports, now(at)).unwrap();
    let plan = Stub.plan(request.members()).unwrap();
    let b = split.finish_rollover(request, plan, now(at)).unwrap();
    assert_eq!(a.len(), b.len());
    assert_eq!(inline.state(), split.state());
}
