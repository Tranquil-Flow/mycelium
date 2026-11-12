// SPDX-License-Identifier: AGPL-3.0-only
//! Operator control, independent of blocking receive/native work. Two origins
//! feed ONE serial operation queue (`api::joined_member::work::Dispatcher`):
//! private inherited stdin (local operator, read here) and the owner's signed
//! remote control channel (`remote.rs`). A strictly increasing operation_id
//! binds cancellation and result retrieval to one operation; long operations
//! must carry one.
use api::joined_member::work::{Dispatcher, Operation, Origin};
use serde_json::{Value, json};
use std::{
    io::{BufRead, Read, Write},
    sync::{Arc, mpsc},
};
type Result<T> = std::result::Result<T, String>;
pub fn reply(value: &Value) -> Result<()> {
    let mut output = std::io::stdout().lock();
    writeln!(output, "{value}").map_err(|_| "operator_write_failed")?;
    output.flush().map_err(|_| "operator_write_failed".into())
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
        let (dispatcher, requests) = Dispatcher::new()?;
        let shared = dispatcher.clone();
        let reader = std::thread::spawn(move || {
            let _cancel_on_exit = (!remote).then(|| shared.cancel_on_exit());
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
            shared.cancel_active()?;
            // The local operator is gone: end the operation loop.
            shared.close_input()
        });
        Ok(Self {
            requests,
            dispatcher,
            reader: Some(reader),
        })
    }
    /// Deliver an operation's reply to its origin, then free the queue slot.
    pub fn complete(&self, operation: &Operation, out: Value) -> Result<()> {
        self.dispatcher.complete(operation, out, reply)
    }
    pub fn join(mut self) -> Result<()> {
        // A remote `close` ends the loop while stdin may stay open: never block on it.
        let reader = self.reader.take().ok_or("operator_reader_missing")?;
        if self.dispatcher.closing() && !reader.is_finished() {
            return Ok(());
        }
        reader.join().map_err(|_| "operator_reader_panicked")?
    }
}
