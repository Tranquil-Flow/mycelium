// SPDX-License-Identifier: AGPL-3.0-only
//! Promoted from the independently executed S4CB2 owner-probes, with fault rescue.
use super::*;
use std::os::unix::fs::PermissionsExt;

fn setup() -> (Fixture, PackagedChainPorts, RunState, Env) {
    let mut f = Fixture::new();
    let ports = PackagedChainPorts::new(&f.host, None, None);
    let seed = f.host.with(|c| c.authority.seed.clone().unwrap());
    seed.exchange
        .set_owner_control(Some(OwnerControl::new(ports.work.owner.public())));
    seed.exchange
        .announce(Announce {
            phase: "compile".into(),
            run_id: Some("review-run".into()),
            deployment_id: Some("review-dep".into()),
            ..Default::default()
        })
        .unwrap();
    f.agent.tick(&f.member);
    let local = f
        .host
        .with(|c| c.run.contributor.as_ref().unwrap().node.clone());
    let mut state = RunState::new("review-run", "review-dep", owner_run::Reason::Start);
    state.participants = vec![local.clone(), f.remote.clone()];
    state.local_node = Some(local);
    let wall = BudgetId::MemberChosenWall
        .span(Entries::ZERO)
        .unwrap()
        .millis()
        / 1000;
    state.worker_wall_limits = state
        .participants
        .iter()
        .cloned()
        .map(|n| (n, wall))
        .collect();
    (f, ports, state, Env { at: Instant::now() })
}

struct Rescue {
    work: Work,
    mode: Option<PathBuf>,
}
impl Drop for Rescue {
    fn drop(&mut self) {
        if let Some(path) = &self.mode
            && let Err(e) = fs::set_permissions(path, fs::Permissions::from_mode(0o700))
        {
            eprintln!("s4cb3_mode_rescue_failed:{e}");
        }
        if let Err(e) = self.work.teardown() {
            eprintln!("s4cb3_owner_rescue_failed:{e}");
        }
    }
}

#[test]
fn review_source_parent_substitution_must_refuse_real_transfer() {
    let (f, mut ports, state, mut env) = setup();
    let _rescue = Rescue {
        work: ports.work.clone(),
        mode: None,
    };
    let plan = f.plan(&state);
    let source = f.root.join("stage-shards");
    let escaped = f.root.join("outside-original-parent");
    fs::rename(&source, &escaped).unwrap();
    std::os::unix::fs::symlink(&escaped, &source).unwrap();
    ports
        .work
        .compose(&state, plan, Arc::new(NoCodec), &mut env)
        .unwrap();
    let transferred = ports.work.node_slice_plans();
    let flow = ports.work.workflow_state().unwrap();
    let closed = ports.close_work();
    println!(
        "REVIEW_PARENT_SUBSTITUTION accepted={} transferred={} cleanup={closed:?}",
        transferred.is_ok(),
        flow["transferred"]
    );
    assert!(
        transferred.is_err(),
        "real shard source followed a substituted parent through its content alias"
    );
    closed.unwrap();
}

#[test]
fn source_file_replacement_after_admission_refuses_transfer() {
    source_replacement(false, false);
}
#[test]
fn source_file_replacement_after_verification_refuses_transfer() {
    source_replacement(false, true);
}
#[test]
fn source_parent_escape_after_verification_refuses_transfer() {
    source_replacement(true, true);
}
fn source_replacement(parent: bool, after_verification: bool) {
    let (f, mut ports, state, mut env) = setup();
    let _rescue = Rescue {
        work: ports.work.clone(),
        mode: None,
    };
    let plan = f.plan(&state);
    let name = plan.0.document["node_files"][&state.participants[0]][0]["path"]
        .as_str()
        .unwrap()
        .strip_prefix("deployment/")
        .unwrap()
        .to_owned();
    let root = f.root.clone();
    let substitute = move || {
        let shards = root.join("stage-shards");
        if parent {
            let escaped = root.join("private-escaped");
            fs::rename(&shards, &escaped).unwrap();
            std::os::unix::fs::symlink(&escaped, &shards).unwrap();
        } else {
            let path = shards.join(name);
            let bytes = fs::read(&path).unwrap();
            fs::rename(&path, root.join("original-retained")).unwrap();
            fs::write(&path, bytes).unwrap();
        }
    };
    ports
        .work
        .compose(&state, plan, Arc::new(NoCodec), &mut env)
        .unwrap();
    if after_verification {
        lock(&ports.work.resident)
            .local
            .as_ref()
            .unwrap()
            .on_receive(Box::new(move |_| substitute()));
    } else {
        substitute();
    }
    let result = ports.work.node_slice_plans();
    let closed = ports.close_work();
    assert!(
        result.is_err(),
        "substituted source reached a real member: {result:?}"
    );
    closed.unwrap();
}

#[test]
fn review_cleanup_failure_must_retain_retry_owner_and_not_become_success() {
    let (f, mut ports, state, mut env) = setup();
    let mut rescue = Rescue {
        work: ports.work.clone(),
        mode: None,
    };
    ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap();
    ports.work.node_slice_plans().unwrap();
    let runs = owner_ports::contributor_dir(&f.root.join("owner")).join("chain-runs");
    let roots: Vec<_> = fs::read_dir(&runs)
        .unwrap()
        .map(|r| r.unwrap().path())
        .filter(|p| p.join("stage-shards").is_dir())
        .collect();
    assert_eq!(roots.len(), 1);
    let stage = roots[0].join("stage-shards");
    rescue.mode = Some(stage.clone());
    fs::set_permissions(&stage, fs::Permissions::from_mode(0o755)).unwrap();
    let first = ports.close_work();
    let retained = ports.work.retained();
    let second = ports.close_work();
    let bytes_remain = stage.is_dir();
    assert!(
        ports.work.member_work_retained(),
        "the unresolved member Work owner was dropped before its proof"
    );
    fs::set_permissions(&stage, fs::Permissions::from_mode(0o700)).unwrap();
    rescue.mode = None;
    let settled = ports.close_work();
    println!(
        "REVIEW_CLEANUP first={first:?} second={second:?} retained={retained} bytes_remain={bytes_remain} settled={settled:?}"
    );
    assert!(first.is_err(), "fault must reach actual cleanup");
    assert!(
        second.is_err(),
        "retry falsely reports successful cleanup with retained bytes and no retry owner"
    );
    assert!(retained, "cleanup debt lost its handles");
    settled.unwrap();
    assert!(
        !stage.exists(),
        "same production owner must release custody after fault restoration"
    );
    ports.close_work().unwrap();
}

/// The member-side network adapter owns the same cleanup debt independently of the owner run:
/// a real identity fault on the remote member's retained stage keeps the exact Service in its
/// slot across repeated closes, and only restoring the mode lets THAT owner release it.
#[test]
fn network_service_close_retries_only_its_unresolved_cleanup_owner() {
    let (f, mut ports, state, mut env) = setup();
    ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap();
    ports.work.node_slice_plans().unwrap();
    let stage = stage_root(&f.root.join("member"));
    fs::set_permissions(&stage, fs::Permissions::from_mode(0o755)).unwrap();
    f.network.close_work();
    assert!(
        stage.is_dir(),
        "a failed close must not unlink the member's withheld bytes"
    );
    f.network.close_work();
    assert!(
        stage.is_dir(),
        "a repeated close must not become success while bytes remain"
    );
    fs::set_permissions(&stage, fs::Permissions::from_mode(0o700)).unwrap();
    f.network.close_work();
    assert!(
        !stage.exists(),
        "the same Service owner must release custody after fault restoration"
    );
    f.network.close_work();
}

/// F2-R1: a panic in the member operation loop before its custody handoff used to drop the
/// only member `work::Work`. `close` reported the panic once, the empty slot made
/// `cleanup_pending` false, and the NEXT close returned Ok while the stage bytes remained —
/// the same false success this lane exists to kill. The Service must keep the absence of
/// proof: a sticky Err on every later close and a pending cleanup slot.
#[test]
fn panicked_member_work_handoff_must_not_turn_a_later_close_into_success() {
    let (f, mut ports, state, mut env) = setup();
    // Rescue first: restoring the private fixture and releasing the composed owners runs
    // even if an assertion below fails.
    let _rescue = Rescue {
        work: ports.work.clone(),
        mode: None,
    };
    ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap();
    ports.work.node_slice_plans().unwrap();
    let stage = stage_root(&owner_ports::contributor_dir(&f.root.join("owner")));
    assert!(
        stage.is_dir(),
        "the local member must own real stage bytes before the panic"
    );
    let mut service = lock(&ports.work.resident)
        .local
        .take()
        .expect("the composed run owns its local member Service");
    // The hook panics inside the worker thread immediately before the custody handoff, so
    // the real unwind (not an error return) drops the only `work::Work`.
    service.on_handoff_panic();
    let first = service.close();
    let second = service.close();
    let pending = service.cleanup_pending();
    let bytes_remain = stage.is_dir();
    println!(
        "REVIEW_PANIC_HANDOFF first={first:?} second={second:?} pending={pending} bytes_remain={bytes_remain}"
    );
    assert_eq!(
        first,
        Err("member_work_panicked".to_owned()),
        "the first close must report the panicked worker"
    );
    assert_eq!(
        second,
        Err("member_work_unprovable".to_owned()),
        "a later close falsely reported success while the panicked worker's bytes remain"
    );
    assert!(
        pending,
        "the unprovable Service was not retained as pending cleanup"
    );
    assert!(
        bytes_remain,
        "the panicked worker's stage bytes must not vanish unproven"
    );
}

fn stage_root(root: &Path) -> PathBuf {
    let runs = root.join("chain-runs");
    let mut dirs: Vec<_> = fs::read_dir(&runs)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    dirs.sort();
    dirs.into_iter()
        .find(|path| path.join("stage-shards").is_dir())
        .expect("the member retained one stage root")
        .join("stage-shards")
}
