// SPDX-License-Identifier: AGPL-3.0-only
//! Explicit gate over a REAL installed bundle (never in the default run), no GPU and no window:
//! the installed `mycelium` executable is also the member agent's native-worker child
//! (`mycelium native-worker`, stdout = the private link, RU-020). Preview 9's `bundle_chat` failed
//! because the packaged executable did not know that role (`no_arguments_accepted`, the run settled
//! Failed, no `result.json`). Here the installed executable is spawned in the role, the harness is
//! the agent end of the link, and nothing is prepared, so MLX/Metal is never initialised:
//!
//! a command before `prepare` is answered `worker_not_prepared` over the link and the worker then
//! exits non-zero with that code on stderr (the role passed bundle admission and runs the worker
//! loop inside the installed bundle). Closing a link before the worker has set it up fails its
//! socket option with EINVAL on macOS, so "exit 0 on a closed link" is not asserted here.
//!
//! ```text
//! MYCELIUM_PROBE_BUNDLE_APP="/path/probe/Mycelium.app" \
//!   cargo test --offline --locked -j 2 -p desktop --features tauri-shell --test bundle_worker_role \
//!   -- --ignored --nocapture --test-threads=1
//! ```
//!
//! Scratch `HOME`, TEST publisher key, our own unreaped child killed by exact pid on any path.
#![cfg(all(feature = "tauri-shell", target_os = "macos"))]
mod support;
use budgets::BudgetId;
use node_service::worker_link::Link;
use serde_json::json;
use std::{
    os::{fd::OwnedFd, unix::net::UnixStream},
    path::PathBuf,
    process::{Child, Command, Stdio},
    time::Instant,
};
use support::{budget, install_scratch};

struct Spawned {
    child: Child,
    link: Option<Link>,
}
impl Drop for Spawned {
    fn drop(&mut self) {
        if matches!(self.child.try_wait(), Ok(None)) {
            if let Err(error) = self.child.kill() {
                eprintln!("worker kill failed: {error}");
            }
            if let Err(error) = self.child.wait() {
                eprintln!("worker wait failed: {error}");
            }
        }
    }
}
fn spawn(executable: &std::path::Path, home: &std::path::Path) -> Spawned {
    let (agent_end, worker_end) = UnixStream::pair().unwrap();
    let child = Command::new(executable)
        .arg("native-worker")
        .env("HOME", home)
        .stdin(Stdio::null())
        .stdout(Stdio::from(OwnedFd::from(worker_end)))
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    Spawned {
        child,
        link: Some(Link::new(agent_end, budget(BudgetId::MemberChosenWall)).unwrap()),
    }
}
fn wait_exit(spawned: &mut Spawned) -> std::process::ExitStatus {
    let deadline = Instant::now() + budget(BudgetId::StageCleanup);
    loop {
        if let Some(status) = spawned.child.try_wait().unwrap() {
            return status;
        }
        assert!(Instant::now() < deadline, "the worker did not exit");
        std::thread::sleep(budget(BudgetId::RegistryPoll));
    }
}
fn stderr_of(spawned: &mut Spawned) -> String {
    use std::io::Read;
    let mut text = String::new();
    if let Some(mut pipe) = spawned.child.stderr.take() {
        pipe.read_to_string(&mut text).unwrap();
    }
    text
}

#[test]
#[ignore = "explicit gate over a real built bundle; scratch HOME, nothing prepared, no GPU"]
fn the_installed_bundle_runs_the_native_worker_role_and_answers_over_its_link() {
    let app = PathBuf::from(std::env::var("MYCELIUM_PROBE_BUNDLE_APP").expect("bundle"));
    assert!(
        app.starts_with(std::env::var("HOME").unwrap())
            && app.to_string_lossy().contains("/.hermes/cache/scratch/"),
        "only a lane scratch bundle may be installed: {}",
        app.display()
    );
    let (fake, fake_home, executable) = install_scratch(&app, "v02-l11-worker-role");
    let outcome = std::panic::catch_unwind(|| {
        // A command before `prepare`: the worker says why over the link, then dies like any worker
        // that failed a command.
        let mut worker = spawn(&executable, &fake_home);
        let mut link = worker.link.take().unwrap();
        let reply = link
            .send(&json!({"command": "step", "tokens": [1]}), &[])
            .and_then(|()| link.receive());
        let (head, _) = reply.unwrap_or_else(|error| {
            let status = wait_exit(&mut worker);
            panic!(
                "no reply over the link ({error:?}); exit {status:?}; stderr {:?}",
                stderr_of(&mut worker)
            )
        });
        assert_eq!(head["error"], "worker_not_prepared", "{head}");
        drop(link);
        let status = wait_exit(&mut worker);
        let stderr = stderr_of(&mut worker);
        assert!(!status.success(), "a failed command must fail the worker");
        assert!(stderr.contains("worker_not_prepared"), "{stderr}");
        assert!(!stderr.contains("no_arguments_accepted"), "{stderr}");
    });
    if let Err(error) = std::fs::remove_dir_all(&fake) {
        eprintln!("cleanup_failed:{:?}", error.kind());
    }
    if let Err(panic) = outcome {
        std::panic::resume_unwind(panic);
    }
}
