// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-RUN-25 scoped real CPU hello/stop only; not signed API or two-host qualification.
//! Expected NEW identity/STOPPED and denial codes: executed f4 oracle, L6-evidence/r14/oracle-a.json.
use budgets::{BudgetId as B, Entries};
use protocol::native::{CONTROL_PROTOCOL, validate_response};
use runtime::observe::{CloseFailed, HelloSession, HelloSessions, collect_hello_observations};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    io::{BufRead, BufReader, Write},
    process::{Child, ChildStdin, Command, ExitStatus, Stdio},
    sync::mpsc::{self, Receiver},
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
const RUN: &str = "fleet-qwen-20261029T040000Z";
const DEP: &str = "bootstrap-deployment";
fn duration(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
struct Helper {
    child: Child,
    input: Option<ChildStdin>,
    lines: Receiver<String>,
    reader: Option<JoinHandle<()>>,
    node: String,
    stopped: bool,
}
impl Helper {
    fn raw(node: &str) -> Self {
        let mut command = Command::new(env!("CARGO_BIN_EXE_mycelium-hello-bootstrap"));
        command
            .env_clear()
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        node::fds::confine_descriptors(&mut command).unwrap();
        let mut child = command.spawn().unwrap();
        let input = child.stdin.take();
        let output = child.stdout.take().unwrap();
        let (tx, lines) = mpsc::channel();
        let reader = thread::spawn(move || {
            for line in BufReader::new(output).lines() {
                let line = line.expect("child stdout read");
                if tx.send(line).is_err() {
                    return;
                }
            }
        });
        Self {
            child,
            input,
            lines,
            reader: Some(reader),
            node: node.into(),
            stopped: false,
        }
    }
    fn new(node: &str) -> Self {
        let mut h = Self::raw(node);
        h.write(&json!({"node_id":node,"run_id":RUN,"deployment_id":DEP}));
        h
    }
    fn write(&mut self, value: &Value) {
        writeln!(self.input.as_mut().unwrap(), "{value}").unwrap();
        self.input.as_mut().unwrap().flush().unwrap();
    }
    fn frame(&mut self, v: &Value) -> Value {
        self.write(v);
        let line = self
            .lines
            .recv_timeout(duration(B::WorkerStartup))
            .expect("actual child response before startup deadline");
        serde_json::from_str(&line).unwrap()
    }
    fn call(&mut self, id: &str, op: &str, payload: Value) -> Value {
        let v = self.frame(&json!({"protocol":CONTROL_PROTOCOL,"command_id":id,"run_id":RUN,"deployment_id":DEP,"command":op,"payload":payload}));
        validate_response(
            &v,
            id,
            &self.node,
            if v["ok"] == true {
                "completed"
            } else {
                "failed"
            },
        )
        .unwrap();
        v
    }
    fn wait(&mut self) -> ExitStatus {
        let deadline =
            Instant::now() + duration(B::PyMyceliumA13MacosWorkerManagedworkerCloseTimeoutL1621);
        loop {
            if let Some(status) = self.child.try_wait().unwrap() {
                return status;
            }
            assert!(
                Instant::now() < deadline,
                "child not reaped by original close allowance"
            );
            thread::yield_now();
        }
    }
    fn eof(&mut self) {
        assert!(
            matches!(
                self.lines.recv_timeout(duration(B::WorkerStartup)),
                Err(mpsc::RecvTimeoutError::Disconnected)
            ),
            "actual EOF required, not a timeout"
        );
        self.reader.take().unwrap().join().unwrap();
    }
    fn stop(&mut self) {
        let v = self.call("stop-command", "stop", json!({}));
        assert_eq!(
            v["ok"], true,
            "stop must be a native response, not pipe closure"
        );
        assert_eq!(
            v["result"],
            json!({"state":"STOPPED","final_observation":null})
        );
        assert!(self.wait().success());
        self.eof();
        self.stopped = true;
    }
}
impl Drop for Helper {
    fn drop(&mut self) {
        self.input.take();
        match self.child.try_wait() {
            Ok(Some(_)) => {}
            Ok(None) => {
                if let Err(e) = self.child.kill() {
                    eprintln!("owned_helper_kill:{:?}", e.kind());
                }
                if let Err(e) = self.child.wait() {
                    eprintln!("owned_helper_wait:{:?}", e.kind());
                }
            }
            Err(e) => eprintln!("owned_helper_observe:{:?}", e.kind()),
        }
        if let Some(reader) = self.reader.take() {
            assert!(reader.join().is_ok());
        }
    }
}
#[test]
fn hello_stop_real_process_and_same_boot_identity() {
    let mut a = Helper::new("node-a");
    let mut b = Helper::new("node-b");
    let x = a.call("hello-a", "hello", json!({}));
    let y = b.call("hello-b", "hello", json!({}));
    assert_eq!(x["ok"], true, "real hello must succeed");
    assert_eq!(y["ok"], true);
    assert_eq!(x["result"]["state"], "NEW");
    assert_eq!(x["result"]["run_id"], RUN);
    assert_eq!(x["result"]["deployment_id"], DEP);
    assert_eq!(x["result"]["node_id"], "node-a");
    assert_eq!(x["result"]["process_id"], a.child.id());
    assert_eq!(y["result"]["process_id"], b.child.id());
    assert_ne!(a.child.id(), b.child.id());
    assert_eq!(
        x["result"]["host_id"], y["result"]["host_id"],
        "same physical host must never become two hosts"
    );
    assert_eq!(x["result"]["boot_id"], y["result"]["boot_id"]);
    let keys: BTreeSet<_> = x["result"]
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    assert_eq!(
        keys,
        [
            "protocol",
            "run_id",
            "deployment_id",
            "node_id",
            "host_id",
            "boot_id",
            "process_id",
            "endpoint_id",
            "peer_generation",
            "state",
            "route_ready"
        ]
        .into()
    );
    assert_eq!(x["result"]["route_ready"], false);
    assert_eq!(x["result"]["endpoint_id"], Value::Null);
    assert_eq!(x["result"]["peer_generation"], 0);
    a.stop();
    b.stop();
}
#[test]
fn wrong_scope_and_model_commands_are_refused_without_poisoning_hello() {
    let mut h = Helper::new("node-a");
    for (key, value, code) in [
        ("run_id", "other-run", "run_id_mismatch"),
        (
            "deployment_id",
            "other-deployment",
            "deployment_id_mismatch",
        ),
    ] {
        let mut command = json!({"protocol":CONTROL_PROTOCOL,"command_id":"wrong-scope","run_id":RUN,"deployment_id":DEP,"command":"hello","payload":{}});
        command[key] = json!(value);
        let v = h.frame(&command);
        validate_response(&v, "wrong-scope", "node-a", "failed").unwrap();
        assert_eq!(v["error"]["code"], code, "scope binding must refuse");
    }
    for (op, payload, code) in [
        (
            "configure",
            json!({"assignment_file":"assignment","artifact_report_file":"report","graph":{},"device_states":{},"load_generation":1}),
            "model_execution_not_authorized",
        ),
        (
            "start",
            json!({"peer":{},"local_generation":1}),
            "model_load_not_admitted",
        ),
        (
            "infer_start",
            json!({"request":{}}),
            "model_load_not_admitted",
        ),
        (
            "infer_decode",
            json!({"request_id":"request","count":1}),
            "model_load_not_admitted",
        ),
        ("health", json!({}), "invalid_state_for_health"),
        (
            "cancel",
            json!({"request_id":"request"}),
            "invalid_state_for_cancel",
        ),
        ("snapshot", json!({}), "invalid_state_for_snapshot"),
        (
            "snapshot",
            json!({"cleanup_subject":{}}),
            "invalid_cleanup_subject",
        ),
        (
            "snapshot",
            json!({"cleanup_subject":null,"receipt_only":0}),
            "invalid_snapshot_fields",
        ),
        (
            "unknown",
            json!({}),
            "managed_control_operation_unsupported",
        ),
        (
            "hello",
            json!({"exec":"forbidden"}),
            "managed_control_execution_input_forbidden",
        ),
    ] {
        let v = h.call("refusal", op, payload);
        assert_eq!(v["error"]["code"], code, "forbidden operation {op}");
    }
    assert_eq!(
        h.call("after-refusal", "hello", json!({}))["result"]["state"],
        "NEW"
    );
    h.stop();
}
#[test]
fn eof_and_parent_failure_close_and_reap_real_children() {
    let mut h = Helper::new("node-a");
    assert_eq!(h.call("hello", "hello", json!({}))["ok"], true);
    h.input.take();
    assert!(h.wait().success());
    h.eof();
    let mut h = Helper::new("node-late-close");
    assert_eq!(h.call("hello", "hello", json!({}))["ok"], true);
    let pid = h.child.id();
    drop(h);
    assert_eq!(
        rustix::process::test_kill_process(rustix::process::Pid::from_raw(pid as i32).unwrap()),
        Err(rustix::io::Errno::SRCH)
    );
    // Scope cancellation drops the real owning Child guard, including partial bootstrap input.
    let mut h = Helper::raw("node-a");
    h.input
        .as_mut()
        .unwrap()
        .write_all(b"{\"node_id\":")
        .unwrap();
    let pid = h.child.id();
    drop(h);
    assert!(
        rustix::process::test_kill_process(rustix::process::Pid::from_raw(pid as i32).unwrap())
            .is_err()
    );
}
#[test]
fn malformed_bootstrap_and_native_frames_fail_closed() {
    for bootstrap in [
        json!({}),
        json!({"node_id":"a","run_id":RUN,"deployment_id":DEP,"extra":true}),
        json!({"node_id":"a\nsecret","run_id":RUN,"deployment_id":DEP}),
    ] {
        let mut h = Helper::raw("node-a");
        h.write(&bootstrap);
        assert!(!h.wait().success());
        h.eof();
    }
    let mut h = Helper::new("node-a");
    h.input.as_mut().unwrap().write_all(b"not-json\n").unwrap();
    assert!(!h.wait().success());
    h.eof();
}
#[test]
fn overlong_frame_is_rejected_and_reaped() {
    let mut h = Helper::new("node-a");
    assert_eq!(h.call("hello", "hello", json!({}))["ok"], true);
    let oversized = vec![b' '; protocol::managed::MAX_FRAME_BYTES + 1];
    match h.input.as_mut().unwrap().write_all(&oversized) {
        Ok(()) => {}
        Err(error) => assert_eq!(error.kind(), std::io::ErrorKind::BrokenPipe),
    }
    assert!(!h.wait().success());
    h.eof();
}
impl HelloSession for Helper {
    fn send_hello(&mut self, id: &str) -> Result<Value, Option<String>> {
        Ok(self.call(id, "hello", json!({})))
    }
    fn close(&mut self) -> Result<(), CloseFailed> {
        self.stop();
        Ok(())
    }
    fn cleanup_error(&self) -> bool {
        !self.stopped
    }
    fn stop_response_ok(&self) -> Option<bool> {
        Some(self.stopped)
    }
}
struct Factory {
    nodes: Vec<String>,
}
impl HelloSessions for Factory {
    fn run_id(&self) -> &str {
        RUN
    }
    fn deployment_id(&self) -> &str {
        DEP
    }
    fn bound_nodes(&self) -> BTreeSet<String> {
        self.nodes.iter().cloned().collect()
    }
    fn open(
        &mut self,
        node: &str,
        _timeout: f64,
    ) -> Result<Box<dyn HelloSession + '_>, Option<String>> {
        Ok(Box::new(Helper::new(node)))
    }
    fn binding_current(&mut self, _node: &str) -> bool {
        true
    }
}
#[test]
fn unchanged_observer_accepts_one_real_helper_and_refuses_same_host_pair() {
    // Factory is an explicitly unsigned test adapter; responses are actual subprocess JSONL.
    let nodes = vec!["node-a".into()];
    let result = collect_hello_observations(
        &mut Factory {
            nodes: nodes.clone(),
        },
        RUN,
        DEP,
        &nodes,
        &mut || 1793246400.0,
        &mut || "0123456789abcdef".into(),
    )
    .unwrap();
    assert_eq!(result.observations.len(), 1);
    let nodes = vec!["node-a".into(), "node-b".into()];
    let error = collect_hello_observations(
        &mut Factory {
            nodes: nodes.clone(),
        },
        RUN,
        DEP,
        &nodes,
        &mut || 1793246400.0,
        &mut || "0123456789abcdef".into(),
    )
    .unwrap_err();
    assert_eq!(error.code(), "managed_observation_hosts_not_distinct");
}
