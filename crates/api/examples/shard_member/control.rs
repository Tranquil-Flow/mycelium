// SPDX-License-Identifier: AGPL-3.0-only
//! Private inherited-stdin control, independent of blocking receive/native work.
//! A monotonically increasing operation_id binds cancellation to one operation.
use budgets::{BudgetId, Entries};
use serde_json::{Value, json};
use std::{
    io::{BufRead, Read, Write},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};
type Result<T> = std::result::Result<T, String>;
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
pub struct Input {
    pub requests: mpsc::Receiver<Result<Operation>>,
    active: Active,
    reader: Option<std::thread::JoinHandle<Result<()>>>,
}
impl Input {
    pub fn start() -> Result<Self> {
        let cap =
            api::preferences::private_state::private_json_limit().map_err(|e| e.0.to_owned())?;
        let wall = Duration::from_millis(
            BudgetId::MemberChosenWall
                .span(Entries::ZERO)
                .map_err(|_| "budget_unavailable")?
                .millis(),
        );
        let (tx, requests) = mpsc::sync_channel(1);
        let active: Active = Arc::new(Mutex::new(None));
        let shared = active.clone();
        let reader = std::thread::spawn(move || {
            let _cancel_on_exit = CancelOnExit(shared.clone());
            let mut input = std::io::stdin().lock();
            let mut last_id = 0;
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
                let mut current = shared.lock().map_err(|_| "operator_state_poisoned")?;
                if command == "cancel_operation" {
                    let id = request["operation_id"].as_u64().filter(|id| *id > 0);
                    let matched =
                        id.is_some() && current.as_ref().is_some_and(|(held, _)| *held == id);
                    if matched {
                        current.as_ref().unwrap().1.store(true, Ordering::SeqCst);
                    }
                    reply(
                        &json!({"ok":true,"result":{"operation_id":id,"cancel_requested":matched,"cleanup_proven":false}}),
                    )?;
                    continue;
                }
                if current.is_some() {
                    reply(&json!({"ok":false,"code":"operator_busy"}))?;
                    continue;
                }
                let id = request.get("operation_id").map(|v| v.as_u64());
                if id.is_some_and(|id| id.is_none_or(|id| id <= last_id)) {
                    reply(&json!({"ok":false,"code":"operator_operation_id_invalid"}))?;
                    continue;
                }
                let id = id.flatten();
                if let Some(id) = id {
                    last_id = id;
                }
                let cancelled = Arc::new(AtomicBool::new(false));
                *current = Some((id, cancelled.clone()));
                let closing = command == "close";
                tx.try_send(Ok(Operation {
                    request,
                    cancelled,
                    deadline: Instant::now() + wall,
                }))
                .map_err(|_| "operator_dispatch_failed")?;
                drop(current);
                if closing {
                    return Ok(());
                }
            }
            if let Some((_, cancel)) = shared
                .lock()
                .map_err(|_| "operator_state_poisoned")?
                .as_ref()
            {
                cancel.store(true, Ordering::SeqCst);
            }
            Ok(())
        });
        Ok(Self {
            requests,
            active,
            reader: Some(reader),
        })
    }
    pub fn finished(&self) -> Result<()> {
        self.active
            .lock()
            .map_err(|_| "operator_state_poisoned")?
            .take();
        Ok(())
    }
    pub fn join(mut self) -> Result<()> {
        self.reader
            .take()
            .ok_or("operator_reader_missing")?
            .join()
            .map_err(|_| "operator_reader_panicked")?
    }
}
