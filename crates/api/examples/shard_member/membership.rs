// SPDX-License-Identifier: AGPL-3.0-only
//! Member-owned renewal, including while the compute socket is busy.
use api::{app_host, member::Session, peer, seed::SeedOptions};
use budgets::{BudgetId, Entries};
use protocol::identity::Identity;
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
pub struct Membership {
    pub session: Arc<Mutex<Option<Session>>>,
    shared: Arc<Shared>,
    stop: Arc<(Mutex<bool>, Condvar)>,
    thread: Option<JoinHandle<()>>,
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
            true,
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
        Self::build(session, root, signer, options, clock, false)
    }
    fn build(
        session: Session,
        root: PathBuf,
        signer: &Identity,
        options: SeedOptions,
        clock: Clock,
        scheduled: bool,
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
        if !scheduled {
            return Ok(Self {
                session,
                shared,
                stop,
                thread: None,
            });
        }
        let (s, c) = (shared.clone(), stop.clone());
        let interval = Duration::from_millis(
            BudgetId::PyFinalMyceliumSeedCoordinatorKeepaliveIntervalSecondsL182C44
                .span(Entries::ZERO)
                .map_err(|_| "budget_unavailable")?
                .millis(),
        );
        let thread = std::thread::spawn(move || {
            loop {
                let guard = c.0.lock().unwrap();
                let (guard, _) =
                    c.1.wait_timeout_while(guard, interval, |stop| !*stop)
                        .unwrap();
                if *guard {
                    break;
                }
                drop(guard);
                // Any failure keeps the schedule; only a final refusal ends it.
                if s.renew_once().is_err_and(|code| final_refusal(&code)) {
                    break;
                }
            }
        });
        Ok(Self {
            session,
            shared,
            stop,
            thread: Some(thread),
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
        let mut held = self.session.lock().map_err(|_| "member_renewal_poisoned")?;
        // No renewal can race the actual seed generation transition.
        *held = peer::start(&self.shared.root, signer, true, &env)
            .map_err(|e| e.error.code.to_owned())?
            .session;
        peer::renew(&self.shared.root, true, held.as_mut(), &env).map_err(|e| e.code.to_owned())?;
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
        if let Some(thread) = self.thread.take()
            && thread.join().is_err()
        {
            eprintln!("member_renewal_join_failed");
        }
    }
}
