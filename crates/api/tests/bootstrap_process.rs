// SPDX-License-Identifier: AGPL-3.0-only
//! Real admitted Rust helper through the API-owned boundary, no model or GPU.
//! Proves: B-RUN-25 model-free hello/Stop and real same-host refusal only.
use api::bootstrap_process::{BootstrapProcess, TrustedBootstrap};
use budgets::{BudgetId as B, Entries};
use runtime::observe::{CloseFailed, HelloSession, HelloSessions, collect_hello_observations};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs,
    path::Path,
    time::{Duration, Instant},
};
const RUN: &str = "fleet-qwen-20261029T040000Z";
const DEP: &str = "api-bootstrap-deployment";
fn timeout() -> Duration {
    Duration::from_millis(
        B::PyMyceliumNodeProcessPhysicalnodeprocessInitResponseTimeoutSecondsL1964
            .span(Entries::ZERO)
            .unwrap()
            .millis(),
    )
}
fn program() -> TrustedBootstrap {
    let path =
        std::env::var("MYCELIUM_BOOTSTRAP_TEST_CONFIG").expect("explicit admitted artifact config");
    let v: Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
    TrustedBootstrap::open(
        Path::new(v["path"].as_str().unwrap()),
        v["sha256"].as_str().unwrap(),
        v["source_sha"].as_str().unwrap(),
    )
    .unwrap()
}
#[test]
#[ignore = "requires independently pinned admitted Rust helper"]
fn trusted_program_rejects_changed_digest_and_noncanonical_source_before_spawn() {
    let config = std::env::var("MYCELIUM_BOOTSTRAP_TEST_CONFIG").unwrap();
    let v: Value = serde_json::from_slice(&fs::read(config).unwrap()).unwrap();
    let path = Path::new(v["path"].as_str().unwrap());
    let sha = v["sha256"].as_str().unwrap();
    let source = v["source_sha"].as_str().unwrap();
    let mut wrong = sha.to_string();
    wrong.replace_range(..1, if sha.starts_with('0') { "1" } else { "0" });
    assert!(matches!(
        TrustedBootstrap::open(path, &wrong, source),
        Err("native_bootstrap_executable_changed")
    ));
    assert!(matches!(
        TrustedBootstrap::open(path, sha, &source.to_uppercase()),
        Err("native_bootstrap_configuration_invalid")
    ));
    assert!(matches!(
        TrustedBootstrap::open(Path::new("relative-helper"), sha, source),
        Err("native_bootstrap_configuration_invalid")
    ));
}
fn start(node: &str) -> BootstrapProcess {
    BootstrapProcess::spawn(
        &program(),
        json!({"node_id":node,"run_id":RUN,"deployment_id":DEP}),
        &fs::canonicalize(std::env::temp_dir()).unwrap(),
    )
    .unwrap()
}
fn command(id: &str, op: &str) -> Value {
    json!({"protocol":protocol::native::CONTROL_PROTOCOL,"command_id":id,"run_id":RUN,"deployment_id":DEP,"command":op,"payload":{}})
}
#[test]
#[ignore = "requires independently pinned admitted Rust helper"]
fn actual_api_helper_normal_stop_eof_reap_and_same_physical_identity() {
    let mut a = start("a");
    let mut b = start("b");
    let x = a
        .call(&command("hello-a", "hello"), Instant::now() + timeout())
        .unwrap();
    let y = b
        .call(&command("hello-b", "hello"), Instant::now() + timeout())
        .unwrap();
    assert_eq!(x["result"]["state"], "NEW");
    assert_eq!(x["route_ready"], false);
    assert_eq!(x["result"]["host_id"], y["result"]["host_id"]);
    assert_eq!(x["result"]["boot_id"], y["result"]["boot_id"]);
    assert_eq!(x["result"]["process_id"], a.control().birth().unwrap().pid);
    for (id, child) in [("stop-a", &mut a), ("stop-b", &mut b)] {
        let pid = rustix::process::Pid::from_raw(
            child.control().birth().unwrap().pid.try_into().unwrap(),
        )
        .unwrap();
        let stopped = child
            .call(&command(id, "stop"), Instant::now() + timeout())
            .unwrap();
        assert_eq!(
            stopped["result"],
            json!({"state":"STOPPED","final_observation":null})
        );
        assert_eq!(
            rustix::process::test_kill_process_group(pid),
            Err(rustix::io::Errno::SRCH)
        );
        child.close().unwrap();
    }
}
#[test]
#[ignore = "requires independently pinned admitted Rust helper"]
fn actual_api_child_does_not_keep_concurrent_inheritable_socket() {
    use std::io::Read;
    let (mut read, write) = std::os::unix::net::UnixStream::pair().unwrap();
    rustix::io::fcntl_setfd(&write, rustix::io::FdFlags::empty()).unwrap();
    read.set_read_timeout(Some(timeout())).unwrap();
    let mut child = start("fd-check");
    drop(write);
    assert_eq!(
        read.read(&mut [0u8; 1]).unwrap(),
        0,
        "live helper must not retain unrelated socket"
    );
    child
        .call(&command("stop-fd", "stop"), Instant::now() + timeout())
        .unwrap();
}
#[test]
#[ignore = "requires independently pinned admitted Rust helper"]
fn actual_api_helper_forced_cancel_is_not_stop_and_rejects_scope() {
    let mut child = start("cancel");
    let mut foreign = command("foreign", "hello");
    foreign["run_id"] = "old-run".into();
    assert_eq!(
        child.call(&foreign, Instant::now() + timeout()),
        Err("native_command_scope_invalid")
    );
    assert_eq!(
        child.call(&command("expired", "hello"), Instant::now()),
        Err("native_control_timeout")
    );
    let pid =
        rustix::process::Pid::from_raw(child.control().birth().unwrap().pid.try_into().unwrap())
            .unwrap();
    child.control().cancel().unwrap();
    child.close().unwrap();
    assert_eq!(
        rustix::process::test_kill_process_group(pid),
        Err(rustix::io::Errno::SRCH)
    );
    assert!(
        child
            .call(&command("late", "hello"), Instant::now() + timeout())
            .is_err()
    );
}
#[test]
#[ignore = "requires independently pinned admitted Rust helper"]
fn forced_cancel_reaps_real_helper_and_owned_nonroot_group_member() {
    // A separately owned, benign group member exercises group-wide cleanup.
    // It is not represented as a native helper probe or native output.
    use std::os::unix::process::CommandExt;
    use std::process::{Command, Stdio};
    let mut helper = start("group-cancel");
    let birth = helper.control().birth().unwrap();
    let pgid = rustix::process::Pid::from_raw(birth.pgid.try_into().unwrap()).unwrap();
    let mut command = Command::new("/bin/sleep");
    command
        .arg(timeout().as_secs().to_string())
        .env_clear()
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .process_group(birth.pgid.try_into().unwrap());
    node::fds::confine_descriptors(&mut command).unwrap();
    let mut member = command.spawn().unwrap();
    let pid = rustix::process::Pid::from_raw(member.id().try_into().unwrap()).unwrap();
    assert_eq!(rustix::process::getpgid(Some(pid)).unwrap(), pgid);
    let reaper = std::thread::spawn(move || member.wait());
    let cancelled = helper.control().cancel();
    let member_status = reaper.join().unwrap().unwrap();
    cancelled.unwrap();
    helper.close().unwrap();
    assert!(
        !member_status.success(),
        "owned group member must be signalled, not outwaited"
    );
    assert_eq!(
        rustix::process::test_kill_process_group(pgid),
        Err(rustix::io::Errno::SRCH)
    );
    assert_eq!(
        rustix::process::test_kill_process(pid),
        Err(rustix::io::Errno::SRCH)
    );
}
struct Session {
    process: BootstrapProcess,
    stopped: bool,
}
impl HelloSession for Session {
    fn send_hello(&mut self, id: &str) -> Result<Value, Option<String>> {
        self.process
            .call(&command(id, "hello"), Instant::now() + timeout())
            .map_err(|e| Some(e.into()))
    }
    fn close(&mut self) -> Result<(), CloseFailed> {
        self.process
            .call(
                &command("observer-stop", "stop"),
                Instant::now() + timeout(),
            )
            .map_err(|_| CloseFailed)?;
        self.stopped = true;
        Ok(())
    }
    fn cleanup_error(&self) -> bool {
        !self.stopped
    }
    fn stop_response_ok(&self) -> Option<bool> {
        Some(self.stopped)
    }
}
struct Factory(Vec<String>);
impl HelloSessions for Factory {
    fn run_id(&self) -> &str {
        RUN
    }
    fn deployment_id(&self) -> &str {
        DEP
    }
    fn bound_nodes(&self) -> BTreeSet<String> {
        self.0.iter().cloned().collect()
    }
    fn open(&mut self, node: &str, _: f64) -> Result<Box<dyn HelloSession + '_>, Option<String>> {
        Ok(Box::new(Session {
            process: start(node),
            stopped: false,
        }))
    }
    fn binding_current(&mut self, _: &str) -> bool {
        true
    }
}
#[test]
#[ignore = "requires independently pinned admitted Rust helper"]
fn actual_api_process_observer_preserves_same_host_refusal() {
    // Unsigned factory adapter is labelled; all identity bytes come from real helpers.
    let nodes = vec!["a".into()];
    // Fixed oracle run clock only; OS host/boot identities remain actual.
    let wall = || 1793246400.0;
    let one = collect_hello_observations(
        &mut Factory(nodes.clone()),
        RUN,
        DEP,
        &nodes,
        &mut { wall },
        &mut || "0123456789abcdef".into(),
    )
    .unwrap();
    assert_eq!(one.observations.len(), 1);
    let nodes = vec!["a".into(), "b".into()];
    let error = collect_hello_observations(
        &mut Factory(nodes.clone()),
        RUN,
        DEP,
        &nodes,
        &mut { wall },
        &mut || "0123456789abcdef".into(),
    )
    .unwrap_err();
    assert_eq!(error.code(), "managed_observation_hosts_not_distinct");
}
