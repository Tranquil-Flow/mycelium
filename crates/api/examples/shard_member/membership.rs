// SPDX-License-Identifier: AGPL-3.0-only
//! Member-owned renewal, including while the compute socket is busy.
use api::{app_host, member::Session, peer, seed::SeedOptions};
use budgets::{BudgetId, Entries};
use protocol::identity::Identity;
#[cfg(test)]
use std::sync::mpsc;
use std::{
    path::PathBuf,
    sync::{Arc, Condvar, Mutex},
    thread::JoinHandle,
    time::Duration,
};
/// The member's clock (seconds since the epoch): `app_host::now` in the binary.
pub type Clock = Arc<dyn Fn() -> f64 + Send + Sync>;
/// What the renewal thread and the member's operations share.
struct Shared {
    session: Arc<Mutex<Option<Session>>>,
    root: PathBuf,
    options: SeedOptions,
    signer: Identity,
    clock: Clock,
    error: Mutex<Option<String>>,
}
impl Shared {
    /// One renewal pass (`peer.renew`) through the seed.
    fn renew_once(&self) -> Result<(), String> {
        let random = |n| {
            app_host::random(n)
                .map(|b| app_host::hex(&b))
                .unwrap_or_default()
        };
        let clock = || (self.clock)();
        let env = peer::JoinEnv {
            signer: &self.signer,
            clock: &clock,
            random_hex: &random,
            seed: &self.options,
        };
        let mut held = self.session.lock().map_err(|_| "member_renewal_poisoned")?;
        if let Err(code) = peer::renew(&self.root, true, held.as_mut(), &env) {
            if final_refusal(code.code) {
                *self.error.lock().map_err(|_| "member_renewal_poisoned")? =
                    Some(code.code.to_owned());
            } else {
                // The next pass tries again; until then the member's own lease decides
                // (`_renew_loop` only records `renewal_unavailable`, app.py:917-927).
                eprintln!("member renew: {} (next pass retries)", code.code);
            }
            return Err(code.code.to_owned());
        }
        Ok(())
    }
}
/// The shipped v0.1.4 app's rule (`_renew_loop`, app.py:911-927 over `RemoteMacMember.renew`,
/// peer.py:1037-1060): every failed pass is tried again on the next one, and the member stays
/// current only while its OWN lease lasts (`_control_snapshot`, peer.py:625-640). A refusal is
/// final when v0.1.4 records a terminal disposition for it (`_translate`, peer.py:300-325:
/// revoked, generation stale, unknown, expired; the snapshot then refuses at once), or when the
/// seed no longer answers this session's identity at all (`seed_message_mismatch`: the generation
/// moved on, `mycelium_seed/coordinator.py:1354-1362`; the shipped app's snapshot refuses that
/// member at once through its retained-binding check, peer.py:631-638). Everything else (relay
/// 5xx, seed 500, a truncated reply, transport) is retried.
pub fn final_refusal(code: &str) -> bool {
    matches!(
        code,
        "peer_member_revoked"
            | "peer_generation_stale"
            | "peer_member_unknown"
            | "peer_member_expired"
            | "seed_message_mismatch"
    )
}
/// The renewal schedule's stop rule (the same for the binary's thread and a test's): a failed pass
/// of any kind keeps the schedule, only a final refusal ([`final_refusal`]) ends it (`_renew_loop`
/// never stops on a failure, app.py:911-927).
pub fn ends_schedule(pass: &Result<(), String>) -> bool {
    pass.as_ref().is_err_and(|code| final_refusal(code))
}
/// What happens around each renewal pass: the binary waits one keepalive interval (or is
/// stopped); a test feeds the passes itself ([`Membership::ticked`]).
trait Schedule: Send {
    /// Wait for the next pass; `false` ends the schedule.
    fn next(&mut self) -> bool;
    /// What the pass returned.
    fn passed(&mut self, _pass: &Result<(), String>) {}
    /// The schedule has ended.
    fn ended(&mut self) {}
}
/// The renewal thread's loop, whatever drives its passes.
fn run_schedule(shared: &Shared, schedule: &mut dyn Schedule) {
    while schedule.next() {
        let pass = shared.renew_once();
        let end = ends_schedule(&pass);
        schedule.passed(&pass);
        if end {
            break;
        }
    }
    schedule.ended();
}
type Stop = Arc<(Mutex<bool>, Condvar)>;
/// The binary's schedule: one pass per keepalive interval until the member is dropped.
struct Interval {
    stop: Stop,
    every: Duration,
}
impl Schedule for Interval {
    fn next(&mut self) -> bool {
        let guard = self.stop.0.lock().unwrap();
        let (guard, _) = self
            .stop
            .1
            .wait_timeout_while(guard, self.every, |stop| !*stop)
            .unwrap();
        !*guard
    }
}
/// What a ticked member's renewal thread reports to its test.
#[cfg(test)]
#[derive(Debug, PartialEq)]
pub enum Beat {
    /// One pass ran and returned this.
    Pass(Result<(), String>),
    /// The schedule ended (no further pass runs).
    Ended,
}
/// A test's schedule: one pass per tick; the schedule ends when the ticks stop.
#[cfg(test)]
struct Ticks {
    ticks: mpsc::Receiver<()>,
    beats: mpsc::Sender<Beat>,
}
#[cfg(test)]
impl Schedule for Ticks {
    fn next(&mut self) -> bool {
        self.ticks.recv().is_ok()
    }
    fn passed(&mut self, pass: &Result<(), String>) {
        self.beats.send(Beat::Pass(pass.clone())).unwrap_or(());
    }
    fn ended(&mut self) {
        self.beats.send(Beat::Ended).unwrap_or(());
    }
}
pub struct Membership {
    pub session: Arc<Mutex<Option<Session>>>,
    shared: Arc<Shared>,
    stop: Stop,
    /// The renewal thread; it hands its schedule back when the schedule ends (a final refusal), so a
    /// resume can run the same schedule again.
    thread: Mutex<Option<JoinHandle<Box<dyn Schedule>>>>,
}
impl Membership {
    /// The binary's member: wall clock, renewal thread on the keepalive schedule.
    pub fn new(
        session: Session,
        root: PathBuf,
        signer: &Identity,
        options: SeedOptions,
    ) -> Result<Self, String> {
        Self::build(
            session,
            root,
            signer,
            options,
            Arc::new(app_host::now),
            |stop| {
                let every = Duration::from_millis(
                    BudgetId::PyFinalMyceliumSeedCoordinatorKeepaliveIntervalSecondsL182C44
                        .span(Entries::ZERO)
                        .map_err(|_| "budget_unavailable")?
                        .millis(),
                );
                Ok(Some(Box::new(Interval {
                    stop: stop.clone(),
                    every,
                })))
            },
        )
    }
    /// The same member on the caller's clock and schedule: no renewal thread, the caller runs
    /// each pass with [`Membership::renew_once`] (`tests/grant_window.rs`).
    #[allow(dead_code)]
    pub fn driven(
        session: Session,
        root: PathBuf,
        signer: &Identity,
        options: SeedOptions,
        clock: Clock,
    ) -> Result<Self, String> {
        Self::build(session, root, signer, options, clock, |_| Ok(None))
    }
    /// The same member with the binary's renewal thread and loop, each pass run on a tick the test
    /// sends over `ticks` and reported on `beats` with the schedule's end (`tests/grant_window.rs`).
    /// The test drops its tick sender before the member (the member joins the thread on drop).
    #[cfg(test)]
    pub fn ticked(
        session: Session,
        root: PathBuf,
        signer: &Identity,
        options: SeedOptions,
        clock: Clock,
        ticks: mpsc::Receiver<()>,
        beats: mpsc::Sender<Beat>,
    ) -> Result<Self, String> {
        Self::build(session, root, signer, options, clock, |_| {
            Ok(Some(Box::new(Ticks { ticks, beats })))
        })
    }
    fn build(
        session: Session,
        root: PathBuf,
        signer: &Identity,
        options: SeedOptions,
        clock: Clock,
        schedule: impl FnOnce(&Stop) -> Result<Option<Box<dyn Schedule>>, String>,
    ) -> Result<Self, String> {
        let signer = Identity::from_secret(signer.endpoint_id(), &signer.secret_bytes())
            .map_err(|_| "identity_failed")?;
        let session = Arc::new(Mutex::new(Some(session)));
        let stop = Arc::new((Mutex::new(false), Condvar::new()));
        let shared = Arc::new(Shared {
            session: session.clone(),
            root,
            options,
            signer,
            clock,
            error: Mutex::new(None),
        });
        let Some(mut schedule) = schedule(&stop)? else {
            return Ok(Self {
                session,
                shared,
                stop,
                thread: Mutex::new(None),
            });
        };
        let s = shared.clone();
        let thread = std::thread::spawn(move || {
            run_schedule(&s, schedule.as_mut());
            schedule
        });
        Ok(Self {
            session,
            shared,
            stop,
            thread: Mutex::new(Some(thread)),
        })
    }
    /// One renewal pass now, exactly as the renewal thread runs it (`tests/grant_window.rs` drives
    /// it on its own schedule; this binary only runs it from the thread).
    #[allow(dead_code)]
    pub fn renew_once(&self) -> Result<(), String> {
        self.shared.renew_once()
    }
    pub fn resume(&self, signer: &Identity) -> Result<(), String> {
        let random = |n| {
            app_host::random(n)
                .map(|b| app_host::hex(&b))
                .unwrap_or_default()
        };
        let clock = || (self.shared.clock)();
        let env = peer::JoinEnv {
            signer,
            clock: &clock,
            random_hex: &random,
            seed: &self.shared.options,
        };
        let renewed = {
            let mut held = self.session.lock().map_err(|_| "member_renewal_poisoned")?;
            // No renewal can race the actual seed generation transition.
            *held = peer::start(&self.shared.root, signer, true, &env)
                .map_err(|e| e.error.code.to_owned())?
                .session;
            peer::renew(&self.shared.root, true, held.as_mut(), &env)
                .map(drop)
                .map_err(|e| e.code.to_owned())
        };
        // A new generation: the refusal that fenced the old one no longer applies (v0.1.4
        // `_resume` clears the recorded disposition, peer.py:1018) and the renewal schedule runs
        // again (`_renew_loop` never ends, app.py:911-927).
        self.rearm()?;
        renewed
    }
    /// Clear a final refusal and restart the schedule it ended.
    fn rearm(&self) -> Result<(), String> {
        let fenced = self
            .shared
            .error
            .lock()
            .map_err(|_| "member_renewal_poisoned")?
            .take()
            .is_some();
        if !fenced {
            return Ok(());
        }
        let mut slot = self.thread.lock().map_err(|_| "member_renewal_poisoned")?;
        if let Some(ended) = slot.take() {
            // A final refusal ends the loop right after it is recorded: this join is that end.
            let mut schedule = ended.join().map_err(|_| "member_renewal_join_failed")?;
            let s = self.shared.clone();
            *slot = Some(std::thread::spawn(move || {
                run_schedule(&s, schedule.as_mut());
                schedule
            }));
        }
        Ok(())
    }
    pub fn current(
        &self,
        signer: &Identity,
    ) -> Result<serde_json::Value, api::compiler_inputs::Error> {
        if self
            .shared
            .error
            .lock()
            .map_err(|_| api::compiler_inputs::Error("member_renewal_poisoned"))?
            .is_some()
        {
            return Err(api::compiler_inputs::Error("member_renewal_failed"));
        }
        self.session
            .lock()
            .map_err(|_| api::compiler_inputs::Error("member_renewal_poisoned"))?
            .as_ref()
            .ok_or(api::compiler_inputs::Error("joined_shard_not_joined"))?
            .shard_identity(signer, (self.shared.clock)())
            .map_err(|_| api::compiler_inputs::Error("joined_shard_lease_expired"))
    }
}
impl Drop for Membership {
    fn drop(&mut self) {
        *self.stop.0.lock().unwrap() = true;
        self.stop.1.notify_all();
        let thread = match self.thread.get_mut() {
            Ok(slot) => slot.take(),
            Err(poisoned) => poisoned.into_inner().take(),
        };
        if let Some(thread) = thread
            && thread.join().is_err()
        {
            eprintln!("member_renewal_join_failed");
        }
    }
}
