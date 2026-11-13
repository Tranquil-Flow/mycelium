// SPDX-License-Identifier: AGPL-3.0-only
//! Trusted, model-free physical-node helper. An owned unreaped child pins signal
//! authority; saved numeric PID/PGID observations never restore that authority.
use budgets::{BudgetId as B, Entries};
use rustix::process::{
    Pid, Signal, WaitId, WaitIdOptions, kill_process_group, test_kill_process_group, waitid,
};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::unix::{fs::MetadataExt, process::CommandExt},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{Arc, Mutex, mpsc},
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
type Result<T> = std::result::Result<T, &'static str>;
/// The v0.1.4 oracle commit this tree ports; `source_sha` of a program sealed into the bundle.
const SEALED_PROGRAM_SOURCE: &str = "f4e366e98a1f705dfaf182897044500d290dbd7f";
/// The admitted hello program for this installation.
///
/// `bootstrap-program.json` is the record an operator writes to name the program; no installed copy of
/// this product ever wrote one -- only test fixtures do -- so both the member's stage and the owner
/// contributor's stage stopped with `native_bootstrap_unavailable` on every packaged run. The model-free
/// child ships inside the signed bundle beside the executable, so its own bytes are the pin: the digest
/// is read from the file that will be executed and the source revision is the oracle commit ported here.
pub fn installed_program(root: &Path) -> Result<TrustedBootstrap> {
    match admitted(root) {
        Err("native_bootstrap_unavailable") => sealed(),
        other => other,
    }
}
fn admitted(root: &Path) -> Result<TrustedBootstrap> {
    let config = crate::preferences::private_state::load(
        &root.join("bootstrap-program.json"),
        crate::preferences::private_state::private_json_limit().map_err(|e| e.0)?,
    )
    .map_err(|e| e.0)?
    .ok_or("native_bootstrap_unavailable")?;
    let config = Value::Object(config);
    if config.as_object().is_none_or(|o| o.len() != 4)
        || config["protocol"] != "mycelium.bootstrap_program.v1"
    {
        return Err("native_bootstrap_configuration_invalid");
    }
    let text = |k: &str| {
        config[k]
            .as_str()
            .ok_or("native_bootstrap_configuration_invalid")
    };
    TrustedBootstrap::open(
        Path::new(text("path")?),
        text("sha256")?,
        text("source_sha")?,
    )
}
/// The child sealed beside this executable (`<exe dir>/native/mycelium-hello-bootstrap`).
fn sealed() -> Result<TrustedBootstrap> {
    let exe = std::env::current_exe().map_err(|_| "native_bootstrap_unavailable")?;
    let path = exe
        .parent()
        .ok_or("native_bootstrap_unavailable")?
        .join("native")
        .join("mycelium-hello-bootstrap");
    let bytes = fs::read(&path).map_err(|_| "native_bootstrap_unavailable")?;
    let digest = protocol::canonical::sha256_prefixed(&bytes);
    let sha256 = digest
        .strip_prefix("sha256:")
        .ok_or("native_bootstrap_unavailable")?;
    TrustedBootstrap::open(&path, sha256, SEALED_PROGRAM_SOURCE)
}
fn duration(id: B) -> Result<Duration> {
    id.span(Entries::ZERO)
        .map(|s| Duration::from_millis(s.millis()))
        .map_err(|_| "budget_unavailable")
}
fn remaining(end: Instant) -> Result<Duration> {
    end.checked_duration_since(Instant::now())
        .filter(|d| !d.is_zero())
        .ok_or("native_control_timeout")
}
#[derive(Clone)]
pub struct TrustedBootstrap {
    path: PathBuf,
    sha256: String,
    pub source_sha: String,
    device: u64,
    inode: u64,
}
impl TrustedBootstrap {
    /// Only the owner-private configuration may supply this record, never an
    /// arriving offer, archive or native command. External artifact qualification
    /// must pin both this digest and its independently admitted source receipt.
    pub fn open(path: &Path, sha256: &str, source_sha: &str) -> Result<Self> {
        if !path.is_absolute()
            || sha256.len() != 64
            || source_sha.len() != 40
            || !sha256
                .bytes()
                .chain(source_sha.bytes())
                .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        {
            return Err("native_bootstrap_configuration_invalid");
        }
        let meta = fs::symlink_metadata(path).map_err(|_| "native_bootstrap_unavailable")?;
        if !meta.is_file()
            || meta.uid() != rustix::process::geteuid().as_raw()
            || meta.mode() & 0o022 != 0
            || meta.mode() & 0o111 == 0
        {
            return Err("native_bootstrap_executable_invalid");
        }
        let canonical =
            fs::canonicalize(path).map_err(|_| "native_bootstrap_executable_invalid")?;
        if canonical != path {
            return Err("native_bootstrap_executable_invalid");
        }
        let this = Self {
            path: canonical,
            sha256: sha256.into(),
            source_sha: source_sha.into(),
            device: meta.dev(),
            inode: meta.ino(),
        };
        this.verify()?;
        Ok(this)
    }
    fn verify(&self) -> Result<()> {
        let meta =
            fs::symlink_metadata(&self.path).map_err(|_| "native_bootstrap_executable_changed")?;
        if !meta.is_file() || (meta.dev(), meta.ino()) != (self.device, self.inode) {
            return Err("native_bootstrap_executable_changed");
        }
        let mut file =
            fs::File::open(&self.path).map_err(|_| "native_bootstrap_executable_changed")?;
        let mut digest = Sha256::new();
        std::io::copy(&mut file, &mut digest).map_err(|_| "native_bootstrap_executable_changed")?;
        if format!("{:x}", digest.finalize()) != self.sha256 {
            return Err("native_bootstrap_executable_changed");
        }
        Ok(())
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProcessBirth {
    pub pid: u32,
    pub parent_pid: u32,
    pub pgid: u32,
    pub start_seconds: u64,
    pub start_microseconds: u64,
}
struct OwnedChild {
    child: Child,
    birth: ProcessBirth,
    reaped: bool,
    exit: Option<std::process::ExitStatus>,
    #[cfg(test)]
    cleanup_refusals: usize,
}
impl OwnedChild {
    fn pid(&self) -> Result<Pid> {
        i32::try_from(self.birth.pid)
            .ok()
            .and_then(Pid::from_raw)
            .ok_or("native_process_identity_invalid")
    }
    fn ready(&self) -> Result<bool> {
        if self.reaped {
            return Ok(true);
        }
        waitid(
            WaitId::Pid(self.pid()?),
            WaitIdOptions::EXITED | WaitIdOptions::NOHANG | WaitIdOptions::NOWAIT,
        )
        .map(|v| v.is_some())
        .map_err(|_| "native_process_ownership_lost")
    }
    fn signal(&self, signal: Signal) -> Result<()> {
        if self.reaped {
            return Err("native_process_ownership_lost");
        }
        self.ready()?;
        // waitid(NOWAIT) proves this is still our unreaped child; no numeric PID re-adoption.
        if rustix::process::getpgid(Some(self.pid()?))
            .map_err(|_| "native_process_identity_changed")?
            != self.pid()?
        {
            return Err("native_process_identity_changed");
        }
        match kill_process_group(self.pid()?, signal) {
            Ok(()) | Err(rustix::io::Errno::SRCH) => Ok(()),
            Err(_) => Err("native_process_signal_failed"),
        }
    }
    fn gone(&self) -> Result<bool> {
        match test_kill_process_group(self.pid()?) {
            Err(rustix::io::Errno::SRCH) => Ok(true),
            Ok(()) | Err(rustix::io::Errno::PERM) => Ok(false),
            Err(_) => Err("native_process_group_unproven"),
        }
    }
    fn reap(&mut self) -> Result<()> {
        if !self.reaped && self.ready()? {
            self.exit = Some(
                self.child
                    .wait()
                    .map_err(|_| "native_process_reap_failed")?,
            );
            self.reaped = true;
        }
        Ok(())
    }
    fn wait_gone(&mut self, end: Instant) -> Result<bool> {
        let poll = duration(B::PyUpperMyceliumNodeProcessL2906C12)?;
        loop {
            // Do not release the leader's PID while a surviving descendant may
            // still need a group signal. A reaped number never grants authority.
            if self.ready()? && self.gone()? {
                self.reap()?;
                return Ok(true);
            }
            if !self.reaped && self.ready()? {
                // Preserve the unreaped leader while signalling its descendants.
                // Darwin may refuse a signal to an all-zombie group; that refusal
                // is not absence. Reap the exact exited child, then prove ESRCH.
                self.reap()?;
                if self.gone()? {
                    return Ok(true);
                }
            }
            let Ok(left) = remaining(end) else {
                return Ok(false);
            };
            thread::sleep(poll.min(left));
        }
    }
    fn terminate(&mut self, end: Instant) -> Result<()> {
        if self.reaped {
            return if self.gone()? {
                Ok(())
            } else {
                Err("native_process_group_unproven")
            };
        }
        // Signal only the positively-owned group. EOF or TERM can race the
        // next getpgid/kill while the child is entering its exited state. A
        // signal refusal is NOT cleanup proof, but must not bypass the existing
        // absolute-deadline waitid/reap + group-ESRCH proof either.
        let signalled = self
            .signal(Signal::TERM)
            .and_then(|()| self.signal(Signal::KILL));
        if let Err(code) = signalled {
            eprintln!("bootstrap process cleanup phase=signal_refusal code={code}");
        }
        if self.wait_gone(end)? {
            Ok(())
        } else {
            signalled.and(Err("native_process_group_unproven"))
        }
    }
}
#[derive(Clone)]
pub struct ProcessControl(Arc<Mutex<OwnedChild>>);
impl ProcessControl {
    pub fn birth(&self) -> Result<ProcessBirth> {
        self.0
            .lock()
            .map(|s| s.birth.clone())
            .map_err(|_| "native_process_state_poisoned")
    }
    pub fn cancel(&self) -> Result<()> {
        let end = Instant::now()
            + duration(B::PyMyceliumNodeProcessPhysicalnodeprocessInitShutdownTimeoutSecondsL1965)?;
        let mut child = self.0.lock().map_err(|_| "native_process_state_poisoned")?;
        #[cfg(test)]
        if child.cleanup_refusals > 0 {
            child.cleanup_refusals -= 1;
            return Err("injected_cleanup_refusal");
        }
        child.terminate(end)
    }
}
/// A failure may still own a live child, pipes and reader. Keep this value in
/// the root owner until retry proves cleanup; converting it to a string drops
/// that retry capability. No saved PID can reconstruct it.
pub struct CleanupFailure {
    code: &'static str,
    cleanup_error: Option<&'static str>,
    control: Option<ProcessControl>,
    owner: Option<Box<dyn FnMut() -> Result<()> + Send>>,
}
impl std::fmt::Debug for CleanupFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CleanupFailure")
            .field("code", &self.code)
            .field("cleanup_error", &self.cleanup_error)
            .field("owner_retained", &self.owner.is_some())
            .finish()
    }
}
impl From<&'static str> for CleanupFailure {
    fn from(code: &'static str) -> Self {
        Self {
            code,
            cleanup_error: None,
            control: None,
            owner: None,
        }
    }
}
impl CleanupFailure {
    pub fn code(&self) -> &'static str {
        self.code
    }
    pub fn cleanup_error(&self) -> Option<&'static str> {
        self.cleanup_error
    }
    pub fn owner_retained(&self) -> bool {
        self.owner.is_some()
    }
    pub fn process_identity(&self) -> Result<Option<ProcessBirth>> {
        self.control.as_ref().map(ProcessControl::birth).transpose()
    }
    pub fn owned(
        code: &'static str,
        cleanup: &'static str,
        control: ProcessControl,
        owner: impl FnMut() -> Result<()> + Send + 'static,
    ) -> Self {
        Self {
            code,
            cleanup_error: Some(cleanup),
            control: Some(control),
            owner: Some(Box::new(owner)),
        }
    }
    pub fn retry(&mut self) -> Result<()> {
        if let Some(owner) = self.owner.as_mut()
            && let Err(code) = owner()
        {
            self.cleanup_error = Some(code);
            return Err(code);
        }
        self.owner.take();
        self.control.take();
        self.cleanup_error = None;
        Ok(())
    }
}
impl Drop for CleanupFailure {
    fn drop(&mut self) {
        if let Err(code) = self.retry() {
            eprintln!("retained process owner cleanup: {code}");
        }
    }
}
pub struct BootstrapProcess {
    control: ProcessControl,
    input: Option<ChildStdin>,
    output: Option<mpsc::Receiver<Result<Option<Value>>>>,
    reader: Option<JoinHandle<()>>,
    scope: Value,
    clean: bool,
}
impl BootstrapProcess {
    pub fn spawn(
        program: &TrustedBootstrap,
        scope: Value,
        root: &Path,
    ) -> std::result::Result<Self, CleanupFailure> {
        Self::spawn_inner(program, scope, root, |_| Ok(()))
    }
    fn spawn_inner(
        program: &TrustedBootstrap,
        scope: Value,
        root: &Path,
        after_exec: impl FnOnce(&ProcessControl) -> Result<()>,
    ) -> std::result::Result<Self, CleanupFailure> {
        program.verify()?;
        if scope.as_object().is_none_or(|s| s.len() != 3) {
            return Err("native_command_scope_invalid".into());
        }
        let probe = serde_json::json!({"protocol":protocol::native::CONTROL_PROTOCOL,"command":"hello","command_id":scope["node_id"],"run_id":scope["run_id"],"deployment_id":scope["deployment_id"],"payload":{}});
        protocol::native::validate_command(&probe).map_err(|_| "native_command_scope_invalid")?;
        let capacity = B::PyMyceliumNodeProcessPhysicalnodeprocessExchangeFrameMaxsizeL3055
            .count()
            .map_err(|_| "budget_unavailable")?
            .get()
            .try_into()
            .map_err(|_| "budget_unavailable")?;
        let maximum = B::PyMyceliumNodeProcessMaxControlFrameBytesL36
            .bytes()
            .map_err(|_| "budget_unavailable")?
            .get();
        let mut command = Command::new(&program.path);
        command
            .env_clear()
            .current_dir(root)
            .process_group(0)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        node::fds::confine_descriptors(&mut command)
            .map_err(|_| "native_bootstrap_descriptor_confinement_failed")?;
        // Launch-attempt wall observation, NOT a kernel birth certificate.
        // Resolve every fallible observation before there can be a child.
        let observed = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|_| "native_process_clock_invalid")?;
        let (tx, rx) = mpsc::sync_channel(capacity);
        let mut child = command
            .spawn()
            .map_err(|_| "native_bootstrap_spawn_failed")?;
        let pid = child.id();
        let birth = ProcessBirth {
            pid,
            parent_pid: std::process::id(),
            pgid: pid,
            start_seconds: observed.as_secs(),
            start_microseconds: observed.subsec_micros().into(),
        };
        // Taking the pipe options cannot fail. Install their owning process
        // before validating them or starting a fallible reader thread.
        let input = child.stdin.take();
        let output = child.stdout.take();
        let control = ProcessControl(Arc::new(Mutex::new(OwnedChild {
            child,
            birth,
            reaped: false,
            exit: None,
            #[cfg(test)]
            cleanup_refusals: 0,
        })));
        let mut this = Self {
            control,
            input,
            output: Some(rx),
            reader: None,
            scope,
            clean: false,
        };
        let initialized = (|| {
            if this.input.is_none() {
                return Err("native_bootstrap_stdio_unavailable");
            }
            let output = output.ok_or("native_bootstrap_stdio_unavailable")?;
            let reader = thread::Builder::new()
                .spawn(move || {
                    let mut reader = BufReader::new(output);
                    loop {
                        let mut line = Vec::new();
                        let frame =
                            match (&mut reader).take(maximum + 1).read_until(b'\n', &mut line) {
                                Ok(0) => Ok(None),
                                Ok(_)
                                    if line.len() as u64 <= maximum
                                        && line.last() == Some(&b'\n') =>
                                {
                                    protocol::strict::object(&line, maximum as usize)
                                        .map(Some)
                                        .map_err(|_| "native_response_invalid")
                                }
                                _ => Err("native_response_invalid"),
                            };
                        let finished = !matches!(&frame, Ok(Some(_)));
                        if tx.send(frame).is_err() || finished {
                            break;
                        }
                    }
                })
                .map_err(|_| "native_stdout_reader_unavailable")?;
            this.reader = Some(reader);
            after_exec(&this.control)?;
            program.verify()?;
            this.write(&this.scope.clone())
        })();
        if let Err(error) = initialized {
            return match this.close() {
                Ok(()) => Err(error.into()),
                Err(cleanup) => {
                    let control = this.control();
                    Err(CleanupFailure::owned(error, cleanup, control, move || {
                        this.close()
                    }))
                }
            };
        }
        Ok(this)
    }
    pub fn control(&self) -> ProcessControl {
        self.control.clone()
    }
    fn write(&mut self, value: &Value) -> Result<()> {
        let mut bytes =
            protocol::canonical::to_bytes(value).map_err(|_| "native_command_invalid")?;
        bytes.push(b'\n');
        self.input
            .as_mut()
            .ok_or("native_process_closed")?
            .write_all(&bytes)
            .map_err(|_| "native_process_write_failed")
    }
    fn receive(&self, end: Instant) -> Result<Option<Value>> {
        self.output
            .as_ref()
            .ok_or("native_process_closed")?
            .recv_timeout(remaining(end)?)
            .map_err(|_| "native_control_timeout")?
    }
    pub fn call(&mut self, command: &Value, end: Instant) -> Result<Value> {
        protocol::native::validate_command(command).map_err(|_| "native_command_invalid")?;
        if command["run_id"] != self.scope["run_id"]
            || command["deployment_id"] != self.scope["deployment_id"]
        {
            return Err("native_command_scope_invalid");
        }
        if !matches!(command["command"].as_str(), Some("hello" | "stop")) {
            return Err("native_bootstrap_command_not_admitted");
        }
        remaining(end)?;
        self.write(command)?;
        let response = self.receive(end)?.ok_or("native_process_closed")?;
        let status = if response["ok"] == true {
            "completed"
        } else {
            "failed"
        };
        protocol::native::validate_response(
            &response,
            command["command_id"]
                .as_str()
                .ok_or("native_command_invalid")?,
            self.scope["node_id"]
                .as_str()
                .ok_or("native_command_invalid")?,
            status,
        )
        .map_err(|_| "native_response_invalid")?;
        if command["command"] == "stop" {
            if response["ok"] != true || response["result"]["state"] != "STOPPED" {
                return Err("native_stop_unproven");
            }
            if self.receive(end)?.is_some() {
                return Err("native_stop_eof_unproven");
            }
            self.input.take();
            let mut child = self
                .control
                .0
                .lock()
                .map_err(|_| "native_process_state_poisoned")?;
            if !child.wait_gone(end)? {
                return Err("native_stop_cleanup_unproven");
            }
            if !child.exit.is_some_and(|status| status.success()) {
                return Err("native_stop_exit_unproven");
            }
            self.clean = true;
            drop(child);
            self.join_reader()?;
        }
        Ok(response)
    }
    fn join_reader(&mut self) -> Result<()> {
        if let Some(reader) = self.reader.take() {
            reader.join().map_err(|_| "native_stdout_reader_panicked")?;
        }
        Ok(())
    }
    pub fn close(&mut self) -> Result<()> {
        self.input.take();
        if !self.clean {
            self.control.cancel()?;
            self.clean = true;
        }
        self.output.take();
        self.join_reader()
    }
}
impl Drop for BootstrapProcess {
    fn drop(&mut self) {
        if let Err(code) = self.close() {
            eprintln!("bootstrap process cleanup: {code}");
        }
    }
}
#[cfg(test)]
mod ownership_tests {
    use super::*;
    #[test]
    #[ignore = "requires independently pinned admitted Rust helper"]
    fn exited_helper_signal_refusal_is_diagnostic_even_when_cleanup_is_proved() {
        struct Reap(Option<std::process::Child>);
        impl Drop for Reap {
            fn drop(&mut self) {
                if let Some(child) = self.0.as_mut() {
                    if let Err(error) = child.kill() {
                        eprintln!("test child cleanup phase=kill kind={:?}", error.kind());
                    }
                    if let Err(error) = child.wait() {
                        eprintln!("test child cleanup phase=reap kind={:?}", error.kind());
                    }
                }
            }
        }
        let child = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "bootstrap_process::ownership_tests::exited_helper_cleanup_reaps_exact_child_after_pipe_eof",
                "--ignored",
                "--nocapture",
                "--test-threads=1",
            ])
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .unwrap();
        let mut owned = Reap(Some(child));
        let end = Instant::now()
            + duration(B::PyMyceliumNodeProcessPhysicalnodeprocessInitShutdownTimeoutSecondsL1965)
                .unwrap();
        loop {
            if owned.0.as_mut().unwrap().try_wait().unwrap().is_some() {
                break;
            }
            assert!(
                Instant::now() < end,
                "exited-child diagnostic subprocess exceeded deadline"
            );
            thread::sleep(duration(B::PyUpperMyceliumNodeProcessL2906C12).unwrap());
        }
        let output = owned.0.take().unwrap().wait_with_output().unwrap();
        assert!(
            output.status.success(),
            "real EOF cleanup regression must pass"
        );
        let stderr = String::from_utf8(output.stderr).unwrap();
        assert!(
            stderr.contains("bootstrap process cleanup phase=signal_refusal code=native_process_identity_changed"),
            "the actual getpgid refusal must remain visible despite proven reap"
        );
    }

    #[test]
    #[ignore = "requires independently pinned admitted Rust helper"]
    fn exited_helper_cleanup_reaps_exact_child_after_pipe_eof() {
        let config = std::env::var("MYCELIUM_BOOTSTRAP_TEST_CONFIG").unwrap();
        let v: Value = serde_json::from_slice(&fs::read(config).unwrap()).unwrap();
        let program = TrustedBootstrap::open(
            Path::new(v["path"].as_str().unwrap()),
            v["sha256"].as_str().unwrap(),
            v["source_sha"].as_str().unwrap(),
        )
        .unwrap();
        let mut process=BootstrapProcess::spawn(&program,serde_json::json!({"node_id":"exited-child","run_id":"run","deployment_id":"deployment"}),&fs::canonicalize(std::env::temp_dir()).unwrap()).unwrap();
        let control = process.control();
        let birth = control.birth().unwrap();
        process.input.take(); // real helper sees EOF and exits; leave it UNREAPED
        let end = Instant::now()
            + duration(B::PyMyceliumNodeProcessPhysicalnodeprocessInitShutdownTimeoutSecondsL1965)
                .unwrap();
        loop {
            if control.0.lock().unwrap().ready().unwrap() {
                break;
            }
            assert!(Instant::now() < end, "helper did not exit after EOF");
            thread::yield_now();
        }
        assert_eq!(
            process.close(),
            Ok(()),
            "owned exited child must be reaped, not re-adopted"
        );
        assert_eq!(control.birth().unwrap(), birth);
        assert!(control.0.lock().unwrap().reaped);
        assert_eq!(
            test_kill_process_group(Pid::from_raw(birth.pgid as i32).unwrap()),
            Err(rustix::io::Errno::SRCH)
        );
    }
    #[test]
    #[ignore = "requires independently pinned admitted Rust helper"]
    fn retained_post_exec_failure_retries_same_child_and_joins_reader() {
        let config = std::env::var("MYCELIUM_BOOTSTRAP_TEST_CONFIG").unwrap();
        let v: Value = serde_json::from_slice(&fs::read(config).unwrap()).unwrap();
        let program = TrustedBootstrap::open(
            Path::new(v["path"].as_str().unwrap()),
            v["sha256"].as_str().unwrap(),
            v["source_sha"].as_str().unwrap(),
        )
        .unwrap();
        let mut safety = None;
        let result = BootstrapProcess::spawn_inner(
            &program,
            serde_json::json!({"node_id":"retry-child","run_id":"run","deployment_id":"deployment"}),
            &fs::canonicalize(std::env::temp_dir()).unwrap(),
            |control| {
                control.0.lock().unwrap().cleanup_refusals = 2;
                safety = Some(control.clone());
                Err("injected_post_exec_failure")
            },
        );
        let mut failure = match result {
            Err(failure) => failure,
            Ok(_) => panic!("injected post-exec failure accepted"),
        };
        let control = safety.unwrap();
        let birth = control.birth().unwrap();
        assert_eq!(failure.code(), "injected_post_exec_failure");
        assert!(failure.owner_retained());
        assert_eq!(failure.retry(), Err("injected_cleanup_refusal"));
        assert!(failure.owner_retained());
        assert_eq!(failure.process_identity().unwrap(), Some(birth.clone()));
        failure.retry().unwrap();
        assert!(!failure.owner_retained());
        failure.retry().unwrap();
        let pid = Pid::from_raw(birth.pid.try_into().unwrap()).unwrap();
        assert_eq!(test_kill_process_group(pid), Err(rustix::io::Errno::SRCH));
        assert!(control.0.lock().unwrap().reaped);
    }
    #[test]
    #[ignore = "requires independently pinned admitted Rust helper"]
    fn post_exec_failure_and_cleanup_refusal_retains_owned_retry_capability() {
        let config = std::env::var("MYCELIUM_BOOTSTRAP_TEST_CONFIG").unwrap();
        let v: Value = serde_json::from_slice(&fs::read(config).unwrap()).unwrap();
        let program = TrustedBootstrap::open(
            Path::new(v["path"].as_str().unwrap()),
            v["sha256"].as_str().unwrap(),
            v["source_sha"].as_str().unwrap(),
        )
        .unwrap();
        let mut safety = None;
        let result = BootstrapProcess::spawn_inner(
            &program,
            serde_json::json!({"node_id":"failure-child","run_id":"run","deployment_id":"deployment"}),
            &fs::canonicalize(std::env::temp_dir()).unwrap(),
            |control| {
                control.0.lock().unwrap().cleanup_refusals = 2;
                safety = Some(control.clone());
                Err("injected_post_exec_failure")
            },
        );
        assert!(result.is_err());
        let control = safety.unwrap();
        let retained = Arc::strong_count(&control.0) > 1;
        // The separate test safety handle always cleans up BEFORE asserting RED.
        control.0.lock().unwrap().cleanup_refusals = 0;
        control.cancel().unwrap();
        assert!(
            retained,
            "post-exec failure discarded the sole production cleanup owner after cleanup refusal"
        );
    }
}
