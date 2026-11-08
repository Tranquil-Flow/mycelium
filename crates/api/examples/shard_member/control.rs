// SPDX-License-Identifier: AGPL-3.0-only
//! Operator control, independent of blocking receive/native work. Two origins
//! feed ONE serial operation queue: private inherited stdin (local operator) and
//! the owner's signed remote control channel (`remote.rs`). A strictly
//! increasing operation_id binds cancellation and result retrieval to one
//! operation; long operations must carry one.
use budgets::{BudgetId, Entries};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    io::{BufRead, Read, Write},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};
type Result<T> = std::result::Result<T, String>;
/// Operations that block the member for long: each needs its own operation_id.
pub const LONG: &[&str] = &["receive", "prepare_native", "native_step", "serve"];
/// Finished remote results kept for retrieval (oldest dropped first).
const KEPT_RESULTS: usize = 16;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Origin {
    Stdin,
    Remote,
}
type Active = Arc<Mutex<Option<(Option<u64>, Arc<AtomicBool>)>>>;
struct CancelOnExit(Active);
impl Drop for CancelOnExit {
    fn drop(&mut self) {
        match self.0.lock() {
            Ok(active) => {
                if let Some((_, cancel)) = active.as_ref() {
                    cancel.store(true, Ordering::SeqCst);
                }
            }
            Err(_) => eprintln!("operator_state_poisoned"),
        }
    }
}
pub struct Operation {
    pub request: Value,
    pub cancelled: Arc<AtomicBool>,
    pub deadline: Instant,
    pub origin: Origin,
    pub id: Option<u64>,
}
impl Operation {
    pub fn check(&self) -> Result<()> {
        if self.cancelled.load(Ordering::SeqCst) {
            Err("member_operation_cancelled".into())
        } else if Instant::now() >= self.deadline {
            Err("member_operation_deadline".into())
        } else {
            Ok(())
        }
    }
}
pub fn reply(value: &Value) -> Result<()> {
    let mut output = std::io::stdout().lock();
    writeln!(output, "{value}").map_err(|_| "operator_write_failed")?;
    output.flush().map_err(|_| "operator_write_failed".into())
}
/// Queue admission shared by both origins.
pub struct Dispatcher {
    /// Dropped on stdin EOF when stdin is the only origin (owner gone: exit).
    tx: Mutex<Option<mpsc::SyncSender<Result<Operation>>>>,
    active: Active,
    last_id: Mutex<u64>,
    results: Mutex<BTreeMap<u64, Value>>,
    wall: Duration,
    closing: AtomicBool,
}
impl Dispatcher {
    /// Cancellation of the active operation with exactly this id.
    pub fn cancel(&self, id: Option<u64>) -> Result<Value> {
        let current = self.active.lock().map_err(|_| "operator_state_poisoned")?;
        let matched = id.is_some() && current.as_ref().is_some_and(|(held, _)| *held == id);
        if let Some((_, flag)) = current.as_ref().filter(|_| matched) {
            flag.store(true, Ordering::SeqCst);
        }
        Ok(json!({"operation_id":id,"cancel_requested":matched,"cleanup_proven":false}))
    }
    /// Admit one operation; `Err(code)` is an immediate refusal reply.
    pub fn admit(&self, request: Value, origin: Origin) -> std::result::Result<(), &'static str> {
        let command = request["command"]
            .as_str()
            .ok_or("operator_command_invalid")?;
        if self.closing.load(Ordering::SeqCst) {
            return Err("operator_closing");
        }
        let mut current = self.active.lock().map_err(|_| "operator_state_poisoned")?;
        if current.is_some() {
            return Err("operator_busy");
        }
        let id = request.get("operation_id").map(|v| v.as_u64());
        // Long operations must be individually cancellable (F6); remote results
        // are retrieved by id, so every remote operation carries one.
        if id.is_none() && (LONG.contains(&command) || origin == Origin::Remote) {
            return Err("operator_operation_id_required");
        }
        let mut last = self.last_id.lock().map_err(|_| "operator_state_poisoned")?;
        if id.is_some_and(|id| id.is_none_or(|id| id <= *last)) {
            return Err("operator_operation_id_invalid");
        }
        let id = id.flatten();
        if let Some(id) = id {
            *last = id;
        }
        let cancelled = Arc::new(AtomicBool::new(false));
        *current = Some((id, cancelled.clone()));
        if command == "close" {
            self.closing.store(true, Ordering::SeqCst);
        }
        self.tx
            .lock()
            .map_err(|_| "operator_state_poisoned")?
            .as_ref()
            .ok_or("operator_closing")?
            .try_send(Ok(Operation {
                request,
                cancelled,
                deadline: Instant::now() + self.wall,
                origin,
                id,
            }))
            .map_err(|_| "operator_dispatch_failed")
    }
    /// Remote result retrieval: the finished reply (removed), or pending.
    pub fn result(&self, id: u64) -> Result<Value> {
        let mut results = self.results.lock().map_err(|_| "operator_state_poisoned")?;
        Ok(match results.remove(&id) {
            Some(reply) => json!({"operation_id":id,"finished":true,"reply":reply}),
            None => json!({"operation_id":id,"finished":false}),
        })
    }
    pub fn active_id(&self) -> Option<u64> {
        self.active
            .lock()
            .ok()
            .and_then(|a| a.as_ref().and_then(|(id, _)| *id))
    }
}
pub struct Input {
    pub requests: mpsc::Receiver<Result<Operation>>,
    pub dispatcher: Arc<Dispatcher>,
    reader: Option<std::thread::JoinHandle<Result<()>>>,
}
impl Input {
    /// `remote`: the signed control channel is an origin too, so stdin EOF
    /// neither ends the member nor cancels a remotely requested operation.
    pub fn start(remote: bool) -> Result<Self> {
        let cap =
            api::preferences::private_state::private_json_limit().map_err(|e| e.0.to_owned())?;
        let wall = Duration::from_millis(
            BudgetId::MemberChosenWall
                .span(Entries::ZERO)
                .map_err(|_| "budget_unavailable")?
                .millis(),
        );
        let (tx, requests) = mpsc::sync_channel(1);
        let dispatcher = Arc::new(Dispatcher {
            tx: Mutex::new(Some(tx)),
            active: Arc::new(Mutex::new(None)),
            last_id: Mutex::new(0),
            results: Mutex::new(BTreeMap::new()),
            wall,
            closing: AtomicBool::new(false),
        });
        let shared = dispatcher.clone();
        let reader = std::thread::spawn(move || {
            let _cancel_on_exit = (!remote).then(|| CancelOnExit(shared.active.clone()));
            let mut input = std::io::stdin().lock();
            loop {
                let mut line = Vec::new();
                let n = Read::by_ref(&mut input)
                    .take(cap + 1)
                    .read_until(b'\n', &mut line)
                    .map_err(|_| "operator_read_failed")?;
                if n == 0 {
                    break;
                }
                if line.len() as u64 > cap || !line.ends_with(b"\n") {
                    return Err("operator_frame_invalid".into());
                }
                let request: Value =
                    serde_json::from_slice(&line).map_err(|_| "operator_json_invalid")?;
                let command = request["command"]
                    .as_str()
                    .ok_or("operator_command_invalid")?;
                if command == "cancel_operation" {
                    let id = request["operation_id"].as_u64().filter(|id| *id > 0);
                    reply(&json!({"ok":true,"result":shared.cancel(id)?}))?;
                    continue;
                }
                let closing = command == "close";
                if let Err(code) = shared.admit(request, Origin::Stdin) {
                    reply(&json!({"ok":false,"code":code}))?;
                    continue;
                }
                if closing {
                    return Ok(());
                }
            }
            if remote {
                return Ok(());
            }
            if let Some((_, cancel)) = shared
                .active
                .lock()
                .map_err(|_| "operator_state_poisoned")?
                .as_ref()
            {
                cancel.store(true, Ordering::SeqCst);
            }
            // The local operator is gone: end the operation loop.
            shared
                .tx
                .lock()
                .map_err(|_| "operator_state_poisoned")?
                .take();
            Ok(())
        });
        Ok(Self {
            requests,
            dispatcher,
            reader: Some(reader),
        })
    }
    /// Deliver an operation's reply to its origin, then free the queue slot.
    pub fn complete(&self, operation: &Operation, out: Value) -> Result<()> {
        let d = &self.dispatcher;
        if operation.origin == Origin::Remote {
            let mut results = d.results.lock().map_err(|_| "operator_state_poisoned")?;
            if let Some(id) = operation.id {
                results.insert(id, out);
            }
            while results.len() > KEPT_RESULTS {
                results.pop_first();
            }
        } else {
            reply(&out)?;
        }
        d.active
            .lock()
            .map_err(|_| "operator_state_poisoned")?
            .take();
        Ok(())
    }
    pub fn join(mut self) -> Result<()> {
        // A remote `close` ends the loop while stdin may stay open: never block on it.
        let reader = self.reader.take().ok_or("operator_reader_missing")?;
        if self.dispatcher.closing.load(Ordering::SeqCst) && !reader.is_finished() {
            return Ok(());
        }
        reader.join().map_err(|_| "operator_reader_panicked")?
    }
}
