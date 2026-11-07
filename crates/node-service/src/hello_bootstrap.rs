// SPDX-License-Identifier: AGPL-3.0-only
//! Model-free physical-control child; NOT node-service::control's slice protocol.
//!
//! The trusted parent selects this executable and writes exactly one private bootstrap record
//! after authenticating membership/offer/archive, consent and current source policy. This process
//! cannot grant that authority: it accepts no executable, archive, filesystem root or load grant.
//! Parent owns the absolute lifetime/deadline and must close pipes and reap on scope cancellation.
//! Source: ManagedTargetRuntime._make_worker -> ManagedWorker -> PhysicalNodeService (f4e366e9).
use budgets::{BudgetId as B, Entries};
use protocol::{canonical::sha256_prefixed, native};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    io::{self, BufRead, Read, Write},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
use zeroize::Zeroizing;

type Result<T> = std::result::Result<T, &'static str>;
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Scope {
    node_id: String,
    run_id: String,
    deployment_id: String,
}
impl Scope {
    fn parse(value: Value) -> Result<Self> {
        let scope: Self = serde_json::from_value(value).map_err(|_| "worker_identity_invalid")?;
        // Reuse the native segment grammar for all three trusted scope identifiers.
        native::validate_command(&json!({"protocol":native::CONTROL_PROTOCOL,
            "command_id":scope.node_id,"run_id":scope.run_id,"deployment_id":scope.deployment_id,
            "command":"hello","payload":{}}))
        .map_err(|_| "worker_identity_invalid")?;
        Ok(scope)
    }
}
fn read_frame(reader: &mut impl BufRead) -> Result<Option<Value>> {
    let mut bytes = Vec::new();
    let read = reader
        .take((protocol::managed::MAX_FRAME_BYTES + 1) as u64)
        .read_until(b'\n', &mut bytes)
        .map_err(|_| "hello_bootstrap_input_failed")?;
    if read == 0 {
        return Ok(None);
    }
    if bytes.len() > protocol::managed::MAX_FRAME_BYTES {
        return Err("managed_control_frame_too_large");
    }
    if bytes.last() != Some(&b'\n') {
        return Err("managed_control_native_command_invalid");
    }
    serde_json::from_slice(&bytes)
        .map(Some)
        .map_err(|_| "managed_control_native_command_invalid")
}
fn duration(id: B) -> Result<Duration> {
    id.span(Entries::ZERO)
        .map(|s| Duration::from_millis(s.millis()))
        .map_err(|_| "hello_bootstrap_budget_invalid")
}
struct Probe {
    child: Child,
    reader: Option<std::thread::JoinHandle<()>>,
}
impl Probe {
    fn drain(&mut self) -> Result<std::sync::mpsc::Receiver<Result<Zeroizing<Vec<u8>>>>> {
        let output = self
            .child
            .stdout
            .take()
            .ok_or("hello_bootstrap_identity_unavailable")?;
        let (send, receive) = std::sync::mpsc::sync_channel(1);
        self.reader = Some(
            std::thread::Builder::new()
                .name("identity-probe-reader".into())
                .spawn(move || {
                    let mut bytes = Zeroizing::new(Vec::new());
                    let read = output
                        .take((protocol::managed::MAX_FRAME_BYTES + 1) as u64)
                        .read_to_end(&mut bytes);
                    let result = match read {
                        Ok(_) if bytes.len() <= protocol::managed::MAX_FRAME_BYTES => Ok(bytes),
                        Ok(_) | Err(_) => Err("hello_bootstrap_identity_unavailable"),
                    };
                    // The receiver can close only after timeout/error; the guard still joins this thread.
                    if let Err(disconnected) = send.send(result) {
                        drop(disconnected);
                    }
                })
                .map_err(|_| "hello_bootstrap_identity_unavailable")?,
        );
        Ok(receive)
    }
}
impl Drop for Probe {
    fn drop(&mut self) {
        let exited = match self.child.try_wait() {
            Ok(status) => status.is_some(),
            Err(e) => {
                eprintln!("identity_probe_observe:{:?}", e.kind());
                false
            }
        };
        if !exited {
            if let Err(e) = self.child.kill() {
                eprintln!("identity_probe_kill:{:?}", e.kind());
            }
            if let Err(e) = self.child.wait() {
                eprintln!("identity_probe_wait:{:?}", e.kind());
            }
        }
        // Kill/wait closes the pipe BEFORE joining, including timeout, over-cap and read errors.
        if let Some(reader) = self.reader.take()
            && reader.join().is_err()
        {
            eprintln!("identity_probe_reader_panicked");
        }
    }
}
/// Only fixed OS executables and fixed arguments, never stdin/environment-selected commands.
fn os_probe(path: &str, args: &[&str], budget: B) -> Result<Zeroizing<String>> {
    let deadline = Instant::now() + duration(budget)?;
    let mut command = Command::new(path);
    command
        .args(args)
        .env_clear()
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    node::fds::confine_descriptors(&mut command)
        .map_err(|_| "hello_bootstrap_identity_unavailable")?;
    let mut probe = Probe {
        child: command
            .spawn()
            .map_err(|_| "hello_bootstrap_identity_unavailable")?,
        reader: None,
    };
    // Drain while the child is running, like source subprocess.run; waiting for exit first
    // deadlocks valid finite output exceeding pipe capacity. One absolute source deadline.
    let receive = probe.drain()?;
    let bytes = receive
        .recv_timeout(deadline.saturating_duration_since(Instant::now()))
        .map_err(|_| "hello_bootstrap_identity_unavailable")??;
    loop {
        if Instant::now() >= deadline {
            return Err("hello_bootstrap_identity_unavailable");
        }
        match probe
            .child
            .try_wait()
            .map_err(|_| "hello_bootstrap_identity_unavailable")?
        {
            Some(status) if status.success() => break,
            Some(_) => return Err("hello_bootstrap_identity_unavailable"),
            None if Instant::now() >= deadline => {
                return Err("hello_bootstrap_identity_unavailable");
            }
            None => std::thread::yield_now(),
        }
    }
    String::from_utf8(bytes.to_vec())
        .map(Zeroizing::new)
        .map_err(|_| "hello_bootstrap_identity_unavailable")
}
fn short_hash(prefix: &str, bytes: &[u8]) -> String {
    // Audited existing sha2 wrapper, not a local cryptographic implementation.
    let digest = sha256_prefixed(bytes);
    format!("{prefix}{}", &digest["sha256:".len().."sha256:".len() + 32])
}
fn derive(run: &str, host: &str, boot: &str) -> (String, String) {
    // remote_probe.py:188-218 @ f4e366e9. Boot input is ALREADY the local boot hash.
    (
        short_hash(
            "host-",
            format!("mycelium.physical_runner.host_identity.v1\0{run}\0{host}").as_bytes(),
        ),
        short_hash(
            "boot-",
            format!("mycelium.physical_runner.boot_identity.v1\0{run}\0{boot}").as_bytes(),
        ),
    )
}
#[cfg(target_os = "macos")]
fn observed_identity() -> Result<(Zeroizing<String>, Zeroizing<String>)> {
    let text = os_probe(
        "/usr/sbin/ioreg",
        &["-rd1", "-c", "IOPlatformExpertDevice"],
        B::PyMyceliumPhysicalRunnerRemoteProbeHostIdentityTimeoutL138,
    )?;
    let host = text
        .lines()
        .find_map(|line| {
            let (key, value) = line.split_once('=')?;
            if key.trim() != "\"IOPlatformUUID\"" {
                return None;
            }
            let v = value.trim().strip_prefix('"')?.strip_suffix('"')?;
            (!v.is_empty() && v.bytes().all(|b| b.is_ascii_hexdigit() || b == b'-'))
                .then(|| v.to_ascii_lowercase())
        })
        .ok_or("hello_bootstrap_identity_unavailable")?;
    let host = Zeroizing::new(host);
    let boot = os_probe(
        "/usr/sbin/sysctl",
        &["-n", "kern.bootsessionuuid"],
        B::PyMyceliumPhysicalRunnerRemoteProbeBootIdentityTimeoutL168,
    )?;
    let boot = boot.trim();
    if boot.is_empty() || !boot.bytes().all(|b| b.is_ascii_hexdigit() || b == b'-') {
        return Err("hello_bootstrap_identity_unavailable");
    }
    Ok((host, Zeroizing::new(boot.to_owned())))
}
#[cfg(target_os = "linux")]
fn observed_identity() -> Result<(Zeroizing<String>, Zeroizing<String>)> {
    let host = std::fs::read_to_string("/etc/machine-id")
        .map_err(|_| "hello_bootstrap_identity_unavailable")?;
    let boot = std::fs::read_to_string("/proc/sys/kernel/random/boot_id")
        .map_err(|_| "hello_bootstrap_identity_unavailable")?;
    if host.trim().is_empty() || boot.trim().is_empty() {
        return Err("hello_bootstrap_identity_unavailable");
    }
    Ok((
        Zeroizing::new(host.trim().to_owned()),
        Zeroizing::new(boot.trim().to_owned()),
    ))
}
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
fn observed_identity() -> Result<(Zeroizing<String>, Zeroizing<String>)> {
    Err("hello_bootstrap_identity_unavailable")
}
fn identity(run: &str) -> Result<(String, String)> {
    let (host, boot) = observed_identity()?;
    Ok(from_observed(run, &host, &boot))
}
fn from_observed(run: &str, host: &str, boot: &str) -> (String, String) {
    // remote_probe.py:156-185: preserve the first boot hash, then the run-domain hash.
    let material = Zeroizing::new(format!("{host}\0{boot}"));
    let boot_hash = Zeroizing::new(short_hash("boot-", material.as_bytes()));
    derive(run, host, &boot_hash)
}
fn snapshot_refusal(payload: &Value) -> &'static str {
    // PhysicalNodeService._snapshot:3938-3992, before any runtime observation.
    if payload.get("receipt_only").is_some_and(|v| !v.is_boolean()) {
        return "invalid_snapshot_fields";
    }
    if let Some(subject) = payload.get("cleanup_subject").filter(|v| !v.is_null()) {
        let fields = [
            "deployment_id",
            "deployment_epoch",
            "qualification_digest",
            "request_id",
            "request_attempt",
            "path_id",
            "path_attempt",
            "path_digest",
            "topology_generation",
            "command_id",
            "cancellation_generation",
            "publisher_generation",
            "absolute_deadline_ms",
        ];
        let shape = subject
            .as_object()
            .is_some_and(|m| m.len() == fields.len() && fields.iter().all(|k| m.contains_key(*k)));
        let text = ["deployment_id", "request_id", "path_id", "command_id"]
            .iter()
            .all(|k| subject[*k].as_str().is_some_and(|v| !v.is_empty()));
        let digest = ["qualification_digest", "path_digest"].iter().all(|k| {
            subject[*k]
                .as_str()
                .is_some_and(|v| v.starts_with("sha256:") && v.chars().count() == 71)
        });
        let integer = |key: &str, positive: bool| {
            subject[key].as_number().is_some_and(|n| {
                let text = n.to_string();
                text.bytes().all(|b| b.is_ascii_digit())
                    && (!positive || text.bytes().any(|b| b != b'0'))
            })
        };
        let numbers = [
            "deployment_epoch",
            "request_attempt",
            "topology_generation",
            "publisher_generation",
            "absolute_deadline_ms",
        ]
        .iter()
        .all(|k| integer(k, true))
            && ["path_attempt", "cancellation_generation"]
                .iter()
                .all(|k| integer(k, false));
        if !(shape && text && digest && numbers) {
            return "invalid_cleanup_subject";
        }
    }
    "invalid_state_for_snapshot"
}
fn dispatch(scope: &Scope, host: &str, boot: &str, v: &Value) -> Result<(Value, bool)> {
    native::validate_command(v).map_err(|e| e.0)?;
    if v["run_id"] != scope.run_id {
        return Err("run_id_mismatch");
    }
    if v["deployment_id"] != scope.deployment_id {
        return Err("deployment_id_mismatch");
    }
    match v["command"]
        .as_str()
        .ok_or("managed_control_native_command_invalid")?
    {
        "hello" => Ok((
            json!({"protocol":native::CONTROL_PROTOCOL,"node_id":scope.node_id,
            "run_id":scope.run_id,"deployment_id":scope.deployment_id,"host_id":host,"boot_id":boot,
            "process_id":std::process::id(),"endpoint_id":null,"peer_generation":0,
            "state":"NEW","route_ready":false}),
            false,
        )),
        "stop" => Ok((json!({"state":"STOPPED","final_observation":null}), true)),
        "configure" => Err("model_execution_not_authorized"),
        "health" => Err("invalid_state_for_health"),
        "snapshot" => Err(snapshot_refusal(&v["payload"])),
        "cancel" => Err("invalid_state_for_cancel"),
        // ManagedWorker._REQUIRES_ADMITTED_LOAD: never admit model work in this child.
        _ => Err("model_load_not_admitted"),
    }
}
pub fn run() -> Result<()> {
    let stdin = io::stdin();
    let mut input = stdin.lock();
    let scope = Scope::parse(read_frame(&mut input)?.ok_or("worker_identity_invalid")?)?;
    let (host, boot) = identity(&scope.run_id)?;
    let stdout = io::stdout();
    let mut output = stdout.lock();
    while let Some(command) = read_frame(&mut input)? {
        // A malformed correlation identifier cannot be safely echoed in a native response.
        let id = command
            .get("command_id")
            .and_then(Value::as_str)
            .ok_or("managed_control_native_command_invalid")?;
        native::validate_command(&json!({"protocol":native::CONTROL_PROTOCOL,"command_id":id,
            "run_id":scope.run_id,"deployment_id":scope.deployment_id,"command":"hello","payload":{}})).map_err(|e|e.0)?;
        let (response, stop) = match dispatch(&scope, &host, &boot, &command) {
            Ok((result, stop)) => (
                json!({"protocol":native::CONTROL_PROTOCOL,"command_id":id,"node_id":scope.node_id,"ok":true,"route_ready":false,"result":result}),
                stop,
            ),
            Err(code) => (
                json!({"protocol":native::CONTROL_PROTOCOL,"command_id":id,"node_id":scope.node_id,"ok":false,"route_ready":false,"error":{"code":code}}),
                false,
            ),
        };
        native::validate_response(
            &response,
            id,
            &scope.node_id,
            if response["ok"] == true {
                "completed"
            } else {
                "failed"
            },
        )
        .map_err(|e| e.0)?;
        serde_json::to_writer(&mut output, &response)
            .map_err(|_| "hello_bootstrap_output_failed")?;
        output
            .write_all(b"\n")
            .and_then(|()| output.flush())
            .map_err(|_| "hello_bootstrap_output_failed")?;
        if stop {
            return Ok(());
        }
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn derivation_matches_executed_oracle_not_pid_or_node() {
        // External actual derive_run_scoped_identity, two identical captures; synthetic inputs.
        assert_eq!(
            derive("bootstrap-run", "fixture-host", "fixture-boot"),
            (
                "host-2d2b0c66df8401a77b09f509df976823".into(),
                "boot-aa621f463d667bf0d7d752c399dde7d7".into()
            )
        );
    }
    #[test]
    fn double_boot_hash_matches_executed_local_oracle() {
        assert_eq!(
            from_observed("bootstrap-run", "fixture-host", "fixture-os-boot"),
            (
                "host-2d2b0c66df8401a77b09f509df976823".into(),
                "boot-fd1e904391c5efaef17e8faaf9b47105".into()
            )
        );
        assert_ne!(
            from_observed("other-run", "fixture-host", "fixture-os-boot"),
            from_observed("bootstrap-run", "fixture-host", "fixture-os-boot")
        );
    }
    #[test]
    fn finite_os_probe_drains_more_than_pipe_capacity_before_exit() {
        let output = os_probe(
            "/usr/bin/printf",
            &["%131072s", ""],
            B::PyMyceliumPhysicalRunnerRemoteProbeHostIdentityTimeoutL138,
        )
        .expect(
            "finite 128KiB OS output below native frame cap must complete within original deadline",
        );
        const {
            assert!(131072 < protocol::managed::MAX_FRAME_BYTES);
        }
        assert_eq!(output.len(), 131072);
        assert!(output.bytes().all(|b| b == b' '));
    }
    #[test]
    fn os_probe_over_cap_refuses_and_reaps() {
        let format = format!("%{}s", protocol::managed::MAX_FRAME_BYTES + 1);
        let result = os_probe(
            "/usr/bin/printf",
            &[&format, ""],
            B::PyMyceliumPhysicalRunnerRemoteProbeHostIdentityTimeoutL138,
        );
        assert!(
            matches!(result, Err("hello_bootstrap_identity_unavailable")),
            "oversized OS output must be refused (contents intentionally not logged)"
        );
    }
    #[test]
    fn probe_drop_kills_reaps_and_joins_blocked_stdout_reader() {
        let mut command = Command::new("/bin/sleep");
        command
            .arg(duration(B::WorkerStartup).unwrap().as_secs().to_string())
            .env_clear()
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        node::fds::confine_descriptors(&mut command).unwrap();
        let mut probe = Probe {
            child: command.spawn().unwrap(),
            reader: None,
        };
        let pid = probe.child.id();
        let receive = probe.drain().unwrap();
        drop(probe);
        assert!(receive.try_recv().unwrap().unwrap().is_empty());
        assert!(matches!(
            receive.try_recv(),
            Err(std::sync::mpsc::TryRecvError::Disconnected)
        ));
        assert_eq!(
            rustix::process::test_kill_process(rustix::process::Pid::from_raw(pid as i32).unwrap()),
            Err(rustix::io::Errno::SRCH)
        );
    }
    #[test]
    fn unavailable_identity_probe_fails_closed() {
        assert_eq!(
            os_probe(
                "/nonexistent-mycelium-identity-probe",
                &[],
                B::PyMyceliumPhysicalRunnerRemoteProbeHostIdentityTimeoutL138
            )
            .unwrap_err(),
            "hello_bootstrap_identity_unavailable"
        );
    }
}
