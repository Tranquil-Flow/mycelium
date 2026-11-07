// SPDX-License-Identifier: AGPL-3.0-only
//! Shared harness for the injected worker-death qualifications. A real owner process is
//! started, the exact pid of one of its two native workers is killed mid-job, and what the
//! owner does next is judged only from outside: the owner's own log line, its exit, and the
//! operating system's process table. Every bound is a registered L1 value.
#![allow(dead_code)]
use budgets::{BudgetId as B, Entries};
use rustix::process::{Pid, Signal, kill_process, kill_process_group};
use std::{
    path::{Path, PathBuf},
    process::{Child, Command},
    time::{Duration, Instant},
};

pub fn duration(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Role {
    Member,
    OwnerLeaf,
}
impl Role {
    /// The first argument the owner passes to the worker it spawns for this role.
    fn argv(self) -> &'static [&'static str] {
        match self {
            Role::Member => &["member", "qualification-member"],
            Role::OwnerLeaf => &["owner-leaf"],
        }
    }
    pub fn node(self) -> &'static str {
        match self {
            Role::Member => "b-member",
            Role::OwnerLeaf => "a-owner",
        }
    }
}
/// Which process of the role is killed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kill {
    /// RU-020: the NATIVE WORKER, the agent's own child. The agent survives and signs.
    Worker,
    /// The member/owner-leaf AGENT process itself (the signer): no signed quorum can exist.
    Agent,
}
pub struct Case {
    pub name: &'static str,
    pub kill: Kill,
    /// Owner log marker (`SLICE_PHASE <marker> ...`) after which the worker is killed.
    pub after: &'static str,
    pub role: Role,
    /// Workers that exist when `after` is reached (the owner leaf is spawned after the member is ready).
    pub workers: usize,
}
/// `(pid, ppid, argv)` for every process.
fn table() -> Vec<(u32, u32, Vec<String>)> {
    let out = Command::new("/bin/ps")
        .args(["-axo", "pid=,ppid=,command="])
        .output()
        .unwrap();
    String::from_utf8_lossy(&out.stdout)
        .lines()
        .filter_map(|line| {
            let mut parts = line.split_whitespace();
            let pid = parts.next()?.parse().ok()?;
            let ppid = parts.next()?.parse().ok()?;
            Some((pid, ppid, parts.map(str::to_owned).collect()))
        })
        .collect()
}
/// The exact child of `owner` that runs `role`'s worker, if it is alive.
pub fn worker_pid(owner: u32, role: Role) -> Option<u32> {
    table().into_iter().find_map(|(pid, ppid, argv)| {
        // The executable path may contain a space (`Slice Development.app`): the role is the
        // argument right after the token that ends the `mycelium-node` path.
        let program = argv.iter().position(|a| a.ends_with("mycelium-node"))?;
        (ppid == owner
            && argv
                .get(program + 1)
                .is_some_and(|a| role.argv().contains(&a.as_str())))
        .then_some(pid)
    })
}
/// The native worker (`mycelium-node native-worker`) that is a child of `agent`, if alive.
pub fn native_worker_of(agent: u32) -> Option<u32> {
    table().into_iter().find_map(|(pid, ppid, argv)| {
        let program = argv.iter().position(|a| a.ends_with("mycelium-node"))?;
        (ppid == agent && argv.get(program + 1).is_some_and(|a| a == "native-worker"))
            .then_some(pid)
    })
}
/// True once the operating system has no process with this pid (not even a zombie).
pub fn gone(pid: u32) -> bool {
    Command::new("/bin/ps")
        .args(["-p", &pid.to_string(), "-o", "pid="])
        .output()
        .unwrap()
        .status
        .code()
        == Some(1)
}
pub fn log_has(path: &Path, marker: &str) -> bool {
    let log = std::fs::read_to_string(path).unwrap_or_default();
    log.lines().any(|line| {
        line == marker
            || line
                .strip_prefix("SLICE_PHASE ")
                .and_then(|rest| rest.split(' ').next())
                == Some(marker)
    })
}
/// The shell's own cleanup verdict line (`L11_CLEANUP:...`), if printed.
pub fn cleanup_line(path: &Path) -> Option<String> {
    std::fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .find(|line| line.starts_with("L11_CLEANUP:"))
        .map(str::to_owned)
}
pub fn failure_line(path: &Path) -> Option<String> {
    std::fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .find_map(|line| {
            line.strip_prefix("slice driver failed: ")
                .map(str::to_owned)
        })
}
/// Owns the started owner process group; whatever a failing assertion leaves behind is
/// killed by exact pid / exact owned group, never by name.
pub struct Owner {
    pub child: Child,
    pub log: PathBuf,
    pub begun: Instant,
    pub workers: Vec<u32>,
    /// Native workers seen under the agents (RU-020).
    pub natives: Vec<u32>,
}
impl Owner {
    pub fn pid(&self) -> u32 {
        self.child.id()
    }
    /// Stop the owner's exact process group (the unreaped leader pins it).
    pub fn stop(&mut self) {
        if self.child.try_wait().ok().flatten().is_none() {
            if let Some(group) = Pid::from_raw(self.child.id() as i32)
                && let Err(error) = kill_process_group(group, Signal::KILL)
            {
                eprintln!("owner group already gone: {error}");
            }
            if let Err(error) = self.child.wait() {
                eprintln!("owner not reaped: {error}");
            }
        }
    }
    pub fn refresh_workers(&mut self) {
        for role in [Role::Member, Role::OwnerLeaf] {
            if let Some(pid) = worker_pid(self.pid(), role) {
                if !self.workers.contains(&pid) {
                    self.workers.push(pid);
                }
                if let Some(native) = native_worker_of(pid)
                    && !self.natives.contains(&native)
                {
                    self.natives.push(native);
                }
            }
        }
    }
}
impl Drop for Owner {
    fn drop(&mut self) {
        // The unreaped leader pins its exact process group.
        self.stop();
        for pid in self.workers.iter().chain(&self.natives) {
            if !gone(*pid)
                && let Some(pid) = Pid::from_raw(*pid as i32)
                && let Err(error) = kill_process(pid, Signal::KILL)
            {
                eprintln!("worker {pid:?} already gone: {error}");
            }
        }
    }
}
/// What the owner did after one of its workers was killed.
pub struct Outcome {
    pub killed: u32,
    pub code: String,
    pub failure_after: Duration,
    pub exit_after_failure: Duration,
    pub exit_success: bool,
    /// Kill to the agent's "survived the worker's death" marker (RU-020 worker kills only).
    pub detected_after: Option<Duration>,
}
/// Wait for `case.after`, kill that role's exact worker pid, then measure.
/// `exits`: whether the owner is expected to wind down by itself (the native owner is); the desktop
/// shell instead keeps a failed generation fenced and prints its cleanup verdict.
pub fn inject(owner: &mut Owner, case: &Case, startup_wall: Duration, exits: bool) -> Outcome {
    let deadline = owner.begun + startup_wall;
    let mut reached: Option<Instant> = None;
    let target = loop {
        owner.refresh_workers();
        let seen = log_has(&owner.log, case.after);
        let agent = worker_pid(owner.pid(), case.role);
        let pid = match case.kill {
            Kill::Agent => agent,
            Kill::Worker => agent.and_then(native_worker_of),
        };
        if seen && let Some(pid) = pid {
            break pid;
        }
        // RU-020 topology: once the phase is reached and the agent is up, its native worker is a
        // separate child process. Without one the member is still a single process.
        if seen && agent.is_some() && case.kill == Kill::Worker {
            let since = *reached.get_or_insert_with(Instant::now);
            assert!(
                since.elapsed() <= duration(B::RouterClose),
                "case {}: `{}` reached but the {} agent has no separate native-worker child",
                case.name,
                case.after,
                case.role.node()
            );
        }
        assert!(
            owner.child.try_wait().unwrap().is_none(),
            "owner exited before `{}` (case {})",
            case.after,
            case.name
        );
        assert!(
            Instant::now() < deadline,
            "case {} never reached `{}`",
            case.name,
            case.after
        );
        std::thread::sleep(duration(B::RegistryPoll));
    };
    owner.refresh_workers();
    let killed_at = Instant::now();
    kill_process(Pid::from_raw(target as i32).unwrap(), Signal::KILL).unwrap();
    // A worker kill is followed by the agents' signed cleanup before the failure line: bound that
    // by the registered stage-cleanup allowance; an agent kill ends the job within the close span.
    let prompt = match case.kill {
        Kill::Worker => duration(B::StageCleanup),
        Kill::Agent => duration(B::RouterClose),
    };
    let mut detected_after = None;
    let failure = loop {
        if detected_after.is_none() && log_has(&owner.log, "agent_survived_worker_death") {
            detected_after = Some(killed_at.elapsed());
        }
        if let Some(code) = failure_line(&owner.log) {
            break Some(code);
        }
        if killed_at.elapsed() > prompt || owner.child.try_wait().unwrap().is_some() {
            break failure_line(&owner.log);
        }
        std::thread::sleep(duration(B::OwnerPoll) / 10);
    };
    let failure_after = killed_at.elapsed();
    let failed_at = Instant::now();
    let code = failure.unwrap_or_default();
    // The owner must now wind down by itself within the registered stage-cleanup allowance.
    // A worker kill is followed by the NEXT Start's whole run (a full request), an agent kill by the
    // owner's own wind-down.
    let exit_deadline = failed_at
        + match case.kill {
            Kill::Worker => duration(B::MemberChosenWall),
            Kill::Agent => duration(B::StageCleanup),
        };
    let status = loop {
        if let Some(status) = owner.child.try_wait().unwrap() {
            break Some(status);
        }
        if !exits && cleanup_line(&owner.log).is_some() {
            break None;
        }
        if Instant::now() >= exit_deadline {
            break None;
        }
        std::thread::sleep(duration(B::RegistryPoll));
    };
    Outcome {
        killed: target,
        code,
        failure_after,
        exit_after_failure: failed_at.elapsed(),
        exit_success: status.is_some_and(|s| s.success()),
        detected_after,
    }
}
/// The shared assertions: prompt, the v0.1.4 terminal code, and verified cleanup.
pub fn assert_prompt_and_clean(owner: &mut Owner, case: &Case, outcome: &Outcome, exits: bool) {
    assert_eq!(
        outcome.code,
        "worker_native_exited",
        "case {}: job outcome after killing {} pid {} (killed→failure line {:?})",
        case.name,
        case.role.node(),
        outcome.killed,
        outcome.failure_after
    );
    let bound = match case.kill {
        Kill::Worker => duration(B::StageCleanup),
        Kill::Agent => duration(B::RouterClose),
    };
    assert!(
        outcome.failure_after <= bound,
        "case {}: failure not prompt: {:?} > {:?}",
        case.name,
        outcome.failure_after,
        bound
    );
    if case.kill == Kill::Worker {
        let detected = outcome
            .detected_after
            .unwrap_or_else(|| panic!("case {}: the agent never reported surviving", case.name));
        assert!(
            detected <= duration(B::RouterClose),
            "case {}: death not detected promptly: {detected:?}",
            case.name
        );
    }
    if exits {
        assert!(
            owner.child.try_wait().unwrap().is_some(),
            "case {}: owner still running {:?} after the failure",
            case.name,
            outcome.exit_after_failure
        );
    } else {
        assert!(
            cleanup_line(&owner.log).is_some(),
            "case {}: the shell never printed a cleanup verdict within {:?}",
            case.name,
            outcome.exit_after_failure
        );
        owner.stop();
    }
    owner.refresh_workers();
    for pid in &owner.workers {
        assert!(gone(*pid), "case {}: worker pid {pid} survived", case.name);
    }
    // A killed agent leaves its native worker without a peer: it must follow within the allowance.
    let orphan_deadline = Instant::now() + duration(B::StageCleanup);
    for pid in &owner.natives {
        while !gone(*pid) && Instant::now() < orphan_deadline {
            std::thread::sleep(duration(B::RegistryPoll));
        }
        assert!(
            gone(*pid),
            "case {}: native worker pid {pid} survived",
            case.name
        );
    }
    match case.kill {
        Kill::Agent => assert!(owner.workers.contains(&outcome.killed)),
        Kill::Worker => assert!(owner.natives.contains(&outcome.killed)),
    }
    assert_eq!(
        owner.workers.len(),
        case.workers,
        "case {}: the agents observed",
        case.name
    );
}
