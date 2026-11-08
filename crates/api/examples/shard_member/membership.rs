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
pub struct Membership {
    pub session: Arc<Mutex<Option<Session>>>,
    stop: Arc<(Mutex<bool>, Condvar)>,
    error: Arc<Mutex<Option<String>>>,
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
        let error = Arc::new(Mutex::new(None));
        let (s, c, e) = (session.clone(), stop.clone(), error.clone());
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
                let random = |n| {
                    app_host::random(n)
                        .map(|b| app_host::hex(&b))
                        .unwrap_or_default()
                };
                let env = peer::JoinEnv {
                    signer: &signer,
                    clock: &app_host::now,
                    random_hex: &random,
                    seed: &options,
                };
                if let Err(code) = peer::renew(&root, true, s.lock().unwrap().as_mut(), &env) {
                    *e.lock().unwrap() = Some(code.code.to_owned());
                    break;
                }
            }
        });
        Ok(Self {
            session,
            stop,
            error,
            thread: Some(thread),
        })
    }
    pub fn current(
        &self,
        signer: &Identity,
    ) -> Result<serde_json::Value, api::compiler_inputs::Error> {
        if self
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
