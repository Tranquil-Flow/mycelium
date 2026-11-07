// SPDX-License-Identifier: AGPL-3.0-only
//! F16: every independent cleanup owner is attempted, including after errors.
use budgets::Entries;
use node::{domains::CleanupDomains, *};
use std::sync::{Arc, Mutex};
fn ticket() -> Ticket {
    Ticket {
        epoch: 1,
        run: 1,
        node: "member".into(),
        incarnation: "first".into(),
    }
}
#[derive(Default)]
struct State {
    stops: u64,
    observations: u64,
    fail_stop: bool,
    fail_observe: bool,
    process_gone: bool,
    gone: bool,
}
struct Domain(Arc<Mutex<State>>);
impl WorkerAdapter for Domain {
    fn request_stop(&mut self) -> std::result::Result<(), WorkerError> {
        let mut s = self.0.lock().unwrap();
        s.stops += 1;
        if s.fail_stop {
            Err(WorkerError::Refused("injected_stop"))
        } else {
            Ok(())
        }
    }
    fn observe(&mut self) -> std::result::Result<Residue, WorkerError> {
        let mut s = self.0.lock().unwrap();
        s.observations += 1;
        if s.fail_observe {
            Err(WorkerError::Refused("injected_probe"))
        } else {
            Ok(Residue {
                process_gone: s.gone || s.process_gone,
                resources_gone: s.gone,
            })
        }
    }
}
#[test]
fn cleanup_failure_does_not_skip_other_domains_and_only_unfinished_work_retries() {
    let device = Arc::new(Mutex::new(State {
        fail_stop: true,
        fail_observe: true,
        process_gone: true,
        ..State::default()
    }));
    let process = Arc::new(Mutex::new(State {
        gone: true,
        ..State::default()
    }));
    let mut domains =
        CleanupDomains::new(ticket(), &["device", "process"], Entries::new(2)).unwrap();
    domains
        .bind(&ticket(), "device", Box::new(Domain(device.clone())))
        .unwrap();
    domains
        .bind(&ticket(), "process", Box::new(Domain(process.clone())))
        .unwrap();
    let mut s = Supervisor::new(ticket(), domains);
    assert!(s.request_stop().is_err());
    assert_eq!(
        process.lock().unwrap().stops,
        1,
        "failed device close must not skip process termination"
    );
    assert!(s.observe_cleanup().is_err());
    assert_eq!(
        process.lock().unwrap().observations,
        1,
        "failed device probe must not skip process reaping"
    );
    {
        let mut d = device.lock().unwrap();
        d.fail_stop = false;
        d.fail_observe = false;
    }
    s.request_stop().unwrap();
    assert_eq!(
        process.lock().unwrap().stops,
        1,
        "proven process cleanup must not be repeated"
    );
    assert!(
        !s.observe_cleanup().unwrap().proven(),
        "device residue still pins reservation"
    );
    device.lock().unwrap().gone = true;
    assert!(s.observe_cleanup().unwrap().proven());
    assert!(s.observe_cleanup().unwrap().proven());
    assert_eq!(process.lock().unwrap().observations, 1);
}
// Inert owned children only. A detached session is explicitly inventoried;
// this does not claim discovery of arbitrary unregistered descendants.
fn deadline() -> std::time::Instant {
    std::time::Instant::now()
        + std::time::Duration::from_millis(
            budgets::BudgetId::WorkerStartup
                .span(Entries::ZERO)
                .unwrap()
                .millis(),
        )
}
fn wait(mut predicate: impl FnMut() -> bool) {
    let end = deadline();
    while !predicate() {
        assert!(std::time::Instant::now() < end, "native fixture deadline");
        std::thread::sleep(std::time::Duration::from_millis(
            budgets::BudgetId::OwnerPoll
                .span(Entries::ZERO)
                .unwrap()
                .millis(),
        ));
    }
}
fn child_command(root: &std::path::Path, role: &str) -> std::process::Command {
    let mut c = std::process::Command::new(std::env::current_exe().unwrap());
    c.args(["--exact", "detached_fixture", "--nocapture"])
        .env("L6_DOMAIN_ROOT", root)
        .env("L6_DOMAIN_ROLE", role);
    c
}
#[test]
fn detached_fixture() {
    let Ok(root) = std::env::var("L6_DOMAIN_ROOT") else {
        return;
    };
    let root = std::path::PathBuf::from(root);
    let role = std::env::var("L6_DOMAIN_ROLE").unwrap();
    if role == "detached" {
        rustix::process::setsid().unwrap();
    }
    std::fs::write(
        root.join(format!("{role}-ready")),
        rustix::process::getpgrp().as_raw_pid().to_string(),
    )
    .unwrap();
    let end = deadline();
    while !root.join("exit").exists() && std::time::Instant::now() < end {
        std::thread::sleep(std::time::Duration::from_millis(
            budgets::BudgetId::OwnerPoll
                .span(Entries::ZERO)
                .unwrap()
                .millis(),
        ));
    }
}
struct Temp(std::path::PathBuf);
impl Temp {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "v02-l6-domains-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::create_dir(&p).unwrap();
        Self(p)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        std::fs::write(self.0.join("exit"), b"exit").unwrap();
    }
}
struct NativeOwner(Arc<Mutex<node::process::NativeChild>>);
impl WorkerAdapter for NativeOwner {
    fn request_stop(&mut self) -> std::result::Result<(), WorkerError> {
        self.0.lock().unwrap().request_stop()
    }
    fn observe(&mut self) -> std::result::Result<Residue, WorkerError> {
        self.0.lock().unwrap().observe()
    }
}
struct ReapOnDrop(Arc<Mutex<node::process::NativeChild>>);
impl Drop for ReapOnDrop {
    fn drop(&mut self) {
        self.0.lock().unwrap().request_stop().unwrap();
        wait(|| {
            let r = self.0.lock().unwrap().observe().unwrap();
            r.process_gone && r.resources_gone
        });
    }
}
#[test]
fn explicitly_owned_detached_session_is_stopped_even_when_device_cleanup_fails() {
    let t = Temp::new();
    let mut set =
        CleanupDomains::new(ticket(), &["device", "leader", "detached"], Entries::new(3)).unwrap();
    let mut guards = Vec::new();
    for role in ["leader", "detached"] {
        let child = Arc::new(Mutex::new(
            node::process::NativeChild::spawn(&mut child_command(&t.0, role), &t.0.join(role))
                .unwrap(),
        ));
        guards.push(ReapOnDrop(child.clone()));
        set.bind(&ticket(), role, Box::new(NativeOwner(child)))
            .unwrap();
    }
    wait(|| t.0.join("leader-ready").exists() && t.0.join("detached-ready").exists());
    assert_ne!(
        std::fs::read(t.0.join("leader-ready")).unwrap(),
        std::fs::read(t.0.join("detached-ready")).unwrap()
    );
    let device = Arc::new(Mutex::new(State {
        fail_stop: true,
        ..State::default()
    }));
    set.bind(&ticket(), "device", Box::new(Domain(device.clone())))
        .unwrap();
    let mut supervisor = Supervisor::new(ticket(), set);
    assert!(supervisor.request_stop().is_err());
    wait(|| {
        assert!(!supervisor.observe_cleanup().unwrap().proven());
        !t.0.join("leader").exists() && !t.0.join("detached").exists()
    });
    device.lock().unwrap().gone = true;
    assert!(supervisor.observe_cleanup().unwrap().proven());
    drop(supervisor);
    drop(guards);
    let root = t.0.clone();
    drop(t);
    std::fs::remove_dir_all(root).unwrap();
}
#[test]
fn lost_exact_ownership_never_signals_a_saved_group_even_if_it_names_a_live_process() {
    let t = Temp::new();
    let mut store = control_runtime::Store::open(&t.0.join("state.db")).unwrap();
    let mut live = node::recovery::GroupChild::spawn(
        &mut child_command(&t.0, "leader"),
        &t.0.join("live"),
        &mut store,
        "real-owner",
        ticket(),
    )
    .unwrap();
    wait(|| t.0.join("leader-ready").exists());
    let (_, bytes) = store.load_document("real-owner").unwrap().unwrap();
    store.save_document("lost-owner", None, &bytes).unwrap();
    let mut lost = node::recovery::recover(&store, "lost-owner", &ticket()).unwrap();
    assert!(matches!(
        lost.request_stop(),
        Err(WorkerError::Refused("worker_group_handle_lost"))
    ));
    assert!(
        !live.observe().unwrap().process_gone,
        "a numeric journal identity is not termination authority"
    );
    assert!(!lost.observe_cleanup().unwrap().proven());
    live.request_stop().unwrap();
    wait(|| {
        let r = live.observe().unwrap();
        r.process_gone && r.resources_gone
    });
    drop(live);
    drop(lost);
    drop(store);
    let root = t.0.clone();
    drop(t);
    std::fs::remove_dir_all(root).unwrap();
}
#[test]
fn missing_or_stale_domain_cannot_release_or_replace_the_inventory() {
    let process = Arc::new(Mutex::new(State {
        gone: true,
        ..State::default()
    }));
    let mut d = CleanupDomains::new(ticket(), &["missing", "process"], Entries::new(2)).unwrap();
    let mut stale = ticket();
    stale.run += 1;
    assert!(
        d.bind(&stale, "missing", Box::new(Domain(process.clone())))
            .is_err()
    );
    assert!(
        d.bind(&ticket(), "foreign", Box::new(Domain(process.clone())))
            .is_err()
    );
    d.bind(&ticket(), "process", Box::new(Domain(process.clone())))
        .unwrap();
    assert!(
        d.bind(&ticket(), "process", Box::new(Domain(process.clone())))
            .is_err()
    );
    assert!(d.observe().is_err());
    assert_eq!(process.lock().unwrap().observations, 1);
    assert!(d.request_stop().is_err());
    assert!(CleanupDomains::new(ticket(), &[], Entries::new(2)).is_err());
    assert!(CleanupDomains::new(ticket(), &["same", "same"], Entries::new(2)).is_err());
}
