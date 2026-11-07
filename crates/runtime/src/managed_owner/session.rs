// SPDX-License-Identifier: AGPL-3.0-only
//! Locally interruptible sessions over the real durable CLIENT producer.
//! The host factory borrows its existing signer/member/transport state. It must
//! honor `CallFence` during held IO; this module owns no endpoint or native worker.
use super::*;
mod factory;
#[cfg(test)]
mod tests;
pub use factory::ManagedSessionFactory;
use std::{
    collections::BTreeMap,
    sync::{Arc, Condvar, Mutex, MutexGuard, TryLockError, atomic::AtomicBool},
    time::Duration,
};
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionError(pub String);
impl From<OwnerError> for SessionError {
    fn from(e: OwnerError) -> Self {
        Self(e.0.to_owned())
    }
}
pub type SessionResult<T> = std::result::Result<T, SessionError>;
fn poisoned() -> OwnerError {
    OwnerError("managed_owner_state_unavailable")
}
#[derive(Default)]
struct Wake {
    generation: Mutex<u64>,
    changed: Condvar,
    #[cfg(feature = "iroh-adapter")]
    asynchronous: tokio::sync::Notify,
}
impl Wake {
    fn signal(&self) -> Result<()> {
        let mut g = self.generation.lock().map_err(|_| poisoned())?;
        *g = g.wrapping_add(1);
        self.changed.notify_all();
        #[cfg(feature = "iroh-adapter")]
        self.asynchronous.notify_waiters();
        Ok(())
    }
}
#[derive(Default)]
struct Phase {
    interrupted: Option<String>,
    completed: bool,
}
#[derive(Default)]
struct Waiter {
    serial: Mutex<()>,
    phase: Mutex<Phase>,
}
impl Waiter {
    fn interrupt(&self, code: &str) -> Result<()> {
        let mut p = self.phase.lock().map_err(|_| poisoned())?;
        if !p.completed && p.interrupted.is_none() {
            p.interrupted = Some(code.to_owned());
        }
        Ok(())
    }
    fn finish(&self) -> Result<()> {
        let mut p = self.phase.lock().map_err(|_| poisoned())?;
        if p.interrupted.is_some() {
            return Err(OwnerError("managed_control_command_interrupted"));
        }
        p.completed = true;
        Ok(())
    }
}
#[derive(Clone)]
pub struct CallFence {
    closed: Arc<AtomicBool>,
    wake: Arc<Wake>,
    waiter: Option<Arc<Waiter>>,
}
impl CallFence {
    pub fn check(&self) -> Result<()> {
        if self.closed.load(Ordering::Acquire) {
            return Err(OwnerError("managed_owner_closed"));
        }
        if let Some(waiter) = &self.waiter
            && waiter
                .phase
                .lock()
                .map_err(|_| poisoned())?
                .interrupted
                .is_some()
        {
            return Err(OwnerError("managed_control_command_interrupted"));
        }
        Ok(())
    }
    /// Wakeable bounded wait for a host's held IO loop. Not a remote cancellation.
    pub fn wait_for(&self, allowance: Span) -> Result<()> {
        let generation = self.wake.generation.lock().map_err(|_| poisoned())?;
        self.check()?;
        let (_guard, _timed) = self
            .wake
            .changed
            .wait_timeout(generation, Duration::from_millis(allowance.millis()))
            .map_err(|_| poisoned())?;
        self.check()
    }
    /// Local async cancellation; register before observing state to avoid lost wakes.
    #[cfg(feature = "iroh-adapter")]
    pub async fn cancelled(&self) -> OwnerError {
        loop {
            let notified = self.wake.asynchronous.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if let Err(error) = self.check() {
                return error;
            }
            notified.await;
        }
    }
    fn publish<R>(&self, publish: impl FnOnce() -> Result<R>) -> Result<R> {
        let _publication = self.wake.generation.lock().map_err(|_| poisoned())?;
        self.check()?;
        publish()
    }
    fn error(&self, error: OwnerError) -> SessionError {
        if let Some(waiter) = &self.waiter
            && let Ok(p) = waiter.phase.lock()
            && let Some(code) = &p.interrupted
        {
            return SessionError(code.clone());
        }
        error.into()
    }
}
pub trait SessionHost: OwnerHost {
    fn exchange_fenced(
        &mut self,
        prepared: &Value,
        deadline: budgets::MonoInstant,
        fence: &CallFence,
    ) -> Result<Value>;
    fn wait_fenced(
        &mut self,
        allowance: Span,
        deadline: budgets::MonoInstant,
        fence: &CallFence,
    ) -> Result<()>;
}
struct FencedHost<'a, H> {
    host: &'a mut H,
    fence: &'a CallFence,
}
impl<H: SessionHost> transport::Clock for FencedHost<'_, H> {
    fn monotonic(&self) -> budgets::MonoInstant {
        self.host.monotonic()
    }
    fn wall_seconds(&self) -> f64 {
        self.host.wall_seconds()
    }
}
impl<H: SessionHost> OwnerHost for FencedHost<'_, H> {
    fn check_local(&self) -> Result<()> {
        self.fence.check()?;
        self.host.check_local()
    }
    fn signer(&self) -> &Identity {
        self.host.signer()
    }
    fn current_member(&mut self, node: &str) -> Result<Value> {
        self.check_local()?;
        self.host.current_member(node)
    }
    fn nonce(&mut self) -> Result<String> {
        self.check_local()?;
        self.host.nonce()
    }
    fn exchange(&mut self, p: &Value, d: budgets::MonoInstant) -> Result<Value> {
        self.check_local()?;
        self.host.exchange_fenced(p, d, self.fence)
    }
    fn wait(&mut self, a: Span, d: budgets::MonoInstant) -> Result<()> {
        self.check_local()?;
        self.host.wait_fenced(a, d, self.fence)
    }
}
fn locked_before<'a, T>(
    lock: &'a Mutex<T>,
    host: &impl OwnerHost,
    deadline: budgets::MonoInstant,
    fence: &CallFence,
) -> Result<MutexGuard<'a, T>> {
    loop {
        remaining(host, deadline)?;
        fence.check()?;
        match lock.try_lock() {
            Ok(guard) => {
                remaining(host, deadline)?;
                fence.check()?;
                return Ok(guard);
            }
            Err(TryLockError::Poisoned(_)) => return Err(poisoned()),
            Err(TryLockError::WouldBlock) => fence.wait_for(
                span(B::PyUpperMyceliumA13MacosControlSessionL72C27)?
                    .min(remaining(host, deadline)?),
            )?,
        }
    }
}
pub struct SharedManagedOwner<F> {
    factory: F,
    owner: Mutex<Option<ManagedControlOwner>>,
    closed: Arc<AtomicBool>,
    wake: Arc<Wake>,
    max_commands: u64,
}
impl<F, H> SharedManagedOwner<F>
where
    F: Fn() -> Result<H>,
    H: SessionHost,
{
    pub fn new(owner: ManagedControlOwner, factory: F) -> Arc<Self> {
        let max_commands = owner.limits.max_commands;
        Arc::new(Self {
            factory,
            owner: Mutex::new(Some(owner)),
            closed: Arc::new(AtomicBool::new(false)),
            wake: Arc::new(Wake::default()),
            max_commands,
        })
    }
    fn fence(&self, waiter: Option<Arc<Waiter>>) -> CallFence {
        CallFence {
            closed: self.closed.clone(),
            wake: self.wake.clone(),
            waiter,
        }
    }
    pub fn session(
        self: &Arc<Self>,
        node: &str,
        run: &str,
        deployment: &str,
        timeout: Span,
    ) -> SessionResult<ManagedNodeSession<F>> {
        if timeout == Span::ZERO
            || timeout > span(B::PyMyceliumA13MacosControlSessionMaxSessionTimeoutSecondsL124)?
        {
            return Err(OwnerError("node_process_arguments_invalid").into());
        }
        let mut host = (self.factory)()?;
        let fence = self.fence(None);
        fence.check()?;
        let deadline = host
            .monotonic()
            .checked_add(timeout)
            .map_err(|_| OwnerError("node_process_arguments_invalid"))?;
        let guard = locked_before(&self.owner, &host, deadline, &fence)?;
        guard
            .as_ref()
            .ok_or(OwnerError("managed_owner_closed"))?
            .guard()?;
        let binding = target(&mut host, node, &json!(run), &json!(deployment))?.1;
        drop(guard);
        Ok(ManagedNodeSession {
            owner: self.clone(),
            timeout,
            state: Mutex::new(SessionState {
                binding,
                waiters: BTreeMap::new(),
                closed: false,
                closing: false,
                stop_id: None,
                stop_response: None,
                cleanup_error: None,
            }),
            close_lock: Mutex::new(()),
        })
    }
    /// Local closure wakes waits and releases this journal lock; never sends stop.
    pub fn close(&self) -> Result<()> {
        {
            let _publication = self.wake.generation.lock().map_err(|_| poisoned())?;
            self.closed.store(true, Ordering::Release);
        }
        self.wake.signal()?;
        let owned = self.owner.lock().map_err(|_| poisoned())?.take();
        drop(owned);
        Ok(())
    }
}
struct SessionState {
    binding: Value,
    waiters: BTreeMap<String, Arc<Waiter>>,
    closed: bool,
    closing: bool,
    stop_id: Option<String>,
    stop_response: Option<Value>,
    cleanup_error: Option<String>,
}
pub struct ManagedNodeSession<F> {
    owner: Arc<SharedManagedOwner<F>>,
    timeout: Span,
    state: Mutex<SessionState>,
    close_lock: Mutex<()>,
}
impl<F, H> ManagedNodeSession<F>
where
    F: Fn() -> Result<H>,
    H: SessionHost,
{
    /// Detached current binding, including any generation adopted by a failed call.
    pub fn binding(&self) -> SessionResult<Value> {
        Ok(self.state.lock().map_err(|_| poisoned())?.binding.clone())
    }
    pub fn timeout(&self) -> Span {
        self.timeout
    }
    pub fn send(&self, id: &str, command: &str, payload: Value) -> SessionResult<Value> {
        let host = (self.owner.factory)()?;
        let deadline = host
            .monotonic()
            .checked_add(self.timeout)
            .map_err(|_| OwnerError("node_process_arguments_invalid"))?;
        self.send_with(host, id, command, payload, deadline)
    }
    pub fn send_before(
        &self,
        id: &str,
        command: &str,
        payload: Value,
        deadline: budgets::MonoInstant,
    ) -> SessionResult<Value> {
        self.send_with((self.owner.factory)()?, id, command, payload, deadline)
            .map_err(|e| {
                if e.0 == "managed_control_response_timeout" {
                    SessionError("command_deadline_exceeded".to_owned())
                } else {
                    e
                }
            })
    }
    fn send_with(
        &self,
        mut host: H,
        id: &str,
        command: &str,
        payload: Value,
        deadline: budgets::MonoInstant,
    ) -> SessionResult<Value> {
        remaining(&host, deadline)?;
        let local = self.owner.fence(None);
        let (waiter, retiring, native) = {
            let mut state = locked_before(&self.state, &host, deadline, &local)?;
            let native = json!({"protocol":protocol::native::CONTROL_PROTOCOL,"command_id":id,"run_id":state.binding["run_id"],"deployment_id":state.binding["deployment_id"],"command":command,"payload":payload});
            protocol::native::validate_command(&native).map_err(OwnerError::from)?;
            if state.closed || (state.closing && state.stop_id.as_deref() != Some(id)) {
                return Err(OwnerError("managed_session_closed").into());
            }
            if !state.waiters.contains_key(id)
                && state.waiters.len() as u64 >= self.owner.max_commands
            {
                return Err(OwnerError("managed_owner_command_limit_exceeded").into());
            }
            let waiter = state
                .waiters
                .entry(id.to_owned())
                .or_insert_with(|| Arc::new(Waiter::default()))
                .clone();
            let retiring = if command == "stop" {
                state.closing = true;
                state.stop_id = Some(id.to_owned());
                state
                    .waiters
                    .iter()
                    .filter(|(key, _)| key.as_str() != id)
                    .map(|(_, v)| v.clone())
                    .collect::<Vec<_>>()
            } else {
                Vec::new()
            };
            (waiter, retiring, native)
        };
        for previous in retiring {
            previous.interrupt("managed_session_closed")?;
        }
        self.owner.wake.signal()?;
        let fence = self.owner.fence(Some(waiter.clone()));
        let mut host = FencedHost {
            host: &mut host,
            fence: &fence,
        };
        let result = (|| -> Result<Value> {
            let _duplicate = locked_before(&waiter.serial, &host, deadline, &fence)?;
            {
                let mut owner = locked_before(&self.owner.owner, &host, deadline, &fence)?;
                let mut binding = self.state.lock().map_err(|_| poisoned())?.binding.clone();
                let result = owner
                    .as_mut()
                    .ok_or(OwnerError("managed_owner_closed"))?
                    .begin_exchange(&mut host, &mut binding, &native, deadline);
                self.state.lock().map_err(|_| poisoned())?.binding = binding;
                result?;
            }
            loop {
                let response = {
                    let mut owner = locked_before(&self.owner.owner, &host, deadline, &fence)?;
                    let mut binding = self.state.lock().map_err(|_| poisoned())?.binding.clone();
                    let response = owner
                        .as_mut()
                        .ok_or(OwnerError("managed_owner_closed"))?
                        .poll_exchange(&mut host, &mut binding, &native, deadline);
                    self.state.lock().map_err(|_| poisoned())?.binding = binding;
                    response?
                };
                if let Some(response) = response {
                    return fence.publish(|| {
                        waiter.finish()?;
                        if command == "stop" {
                            self.state.lock().map_err(|_| poisoned())?.stop_response =
                                Some(response.clone());
                        }
                        Ok(response)
                    });
                }
                host.wait(
                    span(B::PyUpperMyceliumA13MacosControlSessionL545C12)?
                        .min(remaining(&host, deadline)?),
                    deadline,
                )?;
            }
        })();
        result.map_err(|error| fence.error(error))
    }
    pub fn interrupt_command(&self, id: &str, code: &str) -> SessionResult<()> {
        let identifier = id.as_bytes();
        let reason = code.as_bytes();
        if identifier.is_empty()
            || identifier.len() > 128
            || !identifier[0].is_ascii_alphanumeric()
            || !identifier
                .iter()
                .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(b))
            || reason.is_empty()
            || reason.len() > 80
            || !reason[0].is_ascii_lowercase()
            || !reason
                .iter()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'_')
        {
            return Err(OwnerError("managed_control_interrupt_invalid").into());
        }
        let waiter = {
            let mut state = self.state.lock().map_err(|_| poisoned())?;
            if state.closed {
                return Ok(());
            }
            if !state.waiters.contains_key(id)
                && state.waiters.len() as u64 >= self.owner.max_commands
            {
                return Err(OwnerError("managed_owner_command_limit_exceeded").into());
            }
            state
                .waiters
                .entry(id.to_owned())
                .or_insert_with(|| Arc::new(Waiter::default()))
                .clone()
        };
        waiter.interrupt(code)?;
        self.owner.wake.signal()?;
        Ok(())
    }
    /// Retries the SAME stop intent. Error is also retained as cleanup_error.
    pub fn close(&self) -> SessionResult<()> {
        let _close = self.close_lock.lock().map_err(|_| poisoned())?;
        let needs_id = {
            let mut state = self.state.lock().map_err(|_| poisoned())?;
            if state.closed {
                return Ok(());
            }
            state.closing = true;
            state.stop_id.is_none()
        };
        let generated = if needs_id {
            let mut host = (self.owner.factory)()?;
            Some(format!("managed-stop-{}", host.nonce()?))
        } else {
            None
        };
        let id = {
            let mut state = self.state.lock().map_err(|_| poisoned())?;
            if state.stop_id.is_none() {
                state.stop_id = generated;
            }
            state.stop_id.clone().expect("allocated stop")
        };
        let existing = self
            .state
            .lock()
            .map_err(|_| poisoned())?
            .stop_response
            .clone();
        if existing.is_none()
            && let Err(error) = self.send(&id, "stop", json!({}))
        {
            self.state.lock().map_err(|_| poisoned())?.cleanup_error = Some(error.0.clone());
            return Err(error);
        }
        let mut state = self.state.lock().map_err(|_| poisoned())?;
        state.cleanup_error = if state
            .stop_response
            .as_ref()
            .is_some_and(|v| v["ok"] == true)
        {
            None
        } else {
            Some("managed_native_stop_rejected".to_owned())
        };
        state.closed = true;
        Ok(())
    }
    pub fn stop_command_id(&self) -> Option<String> {
        self.state.lock().ok().and_then(|s| s.stop_id.clone())
    }
    pub fn stop_response(&self) -> Option<Value> {
        self.state.lock().ok().and_then(|s| s.stop_response.clone())
    }
    pub fn cleanup_error(&self) -> Option<String> {
        match self.state.lock() {
            Ok(s) => s.cleanup_error.clone(),
            Err(_) => Some(poisoned().0.to_owned()),
        }
    }
    /// Logical compatibility only; not Unix PID/birth/exit evidence.
    pub fn returncode(&self) -> Option<i32> {
        self.state.lock().ok().and_then(|s| {
            if s.closed && s.stop_response.as_ref().is_some_and(|r| r["ok"] == true) {
                Some(0)
            } else {
                None
            }
        })
    }
    pub fn stderr(&self) -> &[u8] {
        b""
    }
}
