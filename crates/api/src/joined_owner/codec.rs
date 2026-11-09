// SPDX-License-Identifier: AGPL-3.0-only
//! The selected snapshot's tokenizer through the pinned native binary's private
//! `tokenize` helper (one JSON line per call over private pipes). The child is
//! exactly owned: `close` requires its clean exit after EOF; dropping an
//! unclosed codec kills and reaps it.
use super::fail;
use crate::{m16_route::Codec, schema::PublicError};
use serde_json::{Value, json};
use std::{
    io::{BufRead, BufReader, Write},
    path::Path,
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
    sync::Mutex,
};

struct Helper {
    child: Option<Child>,
    input: Option<ChildStdin>,
    output: BufReader<ChildStdout>,
}
pub struct NativeCodec(Mutex<Helper>);
fn unavailable() -> PublicError {
    PublicError::new(503, "tokenizer_unavailable")
}
impl NativeCodec {
    /// Start `<binary> tokenize <tokenizer.json>`.
    pub fn start(binary: &Path, tokenizer: &Path) -> Result<Self, PublicError> {
        let mut child = Command::new(binary)
            .arg("tokenize")
            .arg(tokenizer)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|_| fail("tokenizer_start_failed"))?;
        let (Some(input), Some(output)) = (child.stdin.take(), child.stdout.take()) else {
            reap(&mut child);
            return Err(fail("tokenizer_start_failed"));
        };
        Ok(Self(Mutex::new(Helper {
            child: Some(child),
            input: Some(input),
            output: BufReader::new(output),
        })))
    }
    fn call(&self, request: Value) -> Result<Value, PublicError> {
        let mut h = self.0.lock().map_err(|_| unavailable())?;
        let input = h.input.as_mut().ok_or_else(unavailable)?;
        writeln!(input, "{request}").map_err(|_| unavailable())?;
        input.flush().map_err(|_| unavailable())?;
        let mut line = String::new();
        h.output.read_line(&mut line).map_err(|_| unavailable())?;
        serde_json::from_str(&line).map_err(|_| unavailable())
    }
    /// EOF ends the helper; its clean exit is required (exact owned child).
    pub fn close(&self) -> Result<(), PublicError> {
        let mut h = self.0.lock().map_err(|_| unavailable())?;
        h.input.take();
        let mut child = h.child.take().ok_or_else(|| fail("tokenizer_closed"))?;
        let status = child.wait().map_err(|_| fail("tokenizer_exit_unproven"))?;
        if status.success() {
            Ok(())
        } else {
            Err(fail("tokenizer_exit_failed"))
        }
    }
}
fn reap(child: &mut Child) {
    if let Err(e) = child.kill() {
        eprintln!("tokenizer kill: {e}");
    }
    if let Err(e) = child.wait() {
        eprintln!("tokenizer reap: {e}");
    }
}
impl Drop for NativeCodec {
    fn drop(&mut self) {
        let h = match self.0.get_mut() {
            Ok(h) => h,
            Err(poisoned) => poisoned.into_inner(),
        };
        h.input.take();
        if let Some(mut child) = h.child.take() {
            reap(&mut child);
        }
    }
}
impl Codec for NativeCodec {
    fn encode(&self, text: &str) -> Result<Vec<u32>, PublicError> {
        serde_json::from_value(self.call(json!({"encode":text}))?["ids"].clone())
            .map_err(|_| unavailable())
    }
    fn decode(&self, token: u32) -> Result<String, PublicError> {
        Ok(self.call(json!({"decode":token}))?["text"]
            .as_str()
            .ok_or_else(unavailable)?
            .to_owned())
    }
}
