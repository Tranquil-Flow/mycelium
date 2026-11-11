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
/// What the renewal thread and the member's operations share.
struct Shared {
    session: Arc<Mutex<Option<Session>>>,
    root: PathBuf,
    options: SeedOptions,
    signer: Identity,
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
        let env = peer::JoinEnv {
            signer: &self.signer,
            clock: &app_host::now,
            random_hex: &random,
            seed: &self.options,
        };
        let mut held = self.session.lock().map_err(|_| "member_renewal_poisoned")?;
        if let Err(code) = peer::renew(&self.root, true, held.as_mut(), &env) {
            *self.error.lock().map_err(|_| "member_renewal_poisoned")? = Some(code.code.to_owned());
            return Err(code.code.to_owned());
        }
        Ok(())
    }
}
pub struct Membership {
    pub session: Arc<Mutex<Option<Session>>>,
    shared: Arc<Shared>,
    stop: Arc<(Mutex<bool>, Condvar)>,
    thread: Option<JoinHandle<()>>,
}
impl Membership {
    pub fn new(
        session: Session,
        root: PathBuf,
        signer: &Identity,
        options: SeedOptions,
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
            error: Mutex::new(None),
        });
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
                if s.renew_once().is_err() {
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
    /// One renewal pass now, exactly as the renewal thread runs it.
    pub fn renew_once(&self) -> Result<(), String> {
        self.shared.renew_once()
    }
    pub fn resume(&self, signer: &Identity) -> Result<(), String> {
        let random = |n| {
            app_host::random(n)
                .map(|b| app_host::hex(&b))
                .unwrap_or_default()
        };
        let env = peer::JoinEnv {
            signer,
            clock: &app_host::now,
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
            .shard_identity(signer, app_host::now())
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
