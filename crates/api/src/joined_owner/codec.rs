// SPDX-License-Identifier: AGPL-3.0-only
//! Private JSON-line tokenizer IPC. Both pipe descriptors are nonblocking;
//! mutex admission, partial writes and response reads observe the original
//! operation/first-Stop end. Interrupted framing retires this helper, never a
//! different PID. Failed reap retains the Child for the SAME owner's retry.
//! Explicit close/settle are bounded; Drop is last-resort synchronous custody,
//! not a bounded cleanup API (the OS does not promise a bounded reap).
use super::{fail, span};
use crate::{
    m16_route::{Codec, CodecLimit},
    schema::PublicError,
};
use budgets::BudgetId as B;
use serde_json::{Value, json};
use std::{
    io::{ErrorKind, Read, Write},
    path::Path,
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
    sync::{Mutex, MutexGuard, TryLockError},
    time::Instant,
};

struct Helper {
    child: Option<Child>,
    input: Option<ChildStdin>,
    output: ChildStdout,
    killed: bool,
    cleanup_end: Option<Instant>,
}
pub struct NativeCodec(Mutex<Helper>);
fn unavailable() -> PublicError {
    PublicError::new(503, "tokenizer_unavailable")
}
fn pause(end: Instant) -> Result<(), PublicError> {
    std::thread::sleep(
        (span(B::OwnerPoll)? / 10).min(end.saturating_duration_since(Instant::now())),
    );
    Ok(())
}
fn nonblocking(fd: &impl std::os::fd::AsFd) -> Result<(), PublicError> {
    let flags = rustix::fs::fcntl_getfl(fd).map_err(|_| fail("tokenizer_pipe_failed"))?;
    rustix::fs::fcntl_setfl(fd, flags | rustix::fs::OFlags::NONBLOCK)
        .map_err(|_| fail("tokenizer_pipe_failed"))
}
impl NativeCodec {
    /// Start `<binary> tokenize <tokenizer.json>` using the unchanged helper protocol.
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
            reap_last_resort(&mut child);
            return Err(fail("tokenizer_start_failed"));
        };
        if let Err(e) = nonblocking(&input).and_then(|()| nonblocking(&output)) {
            reap_last_resort(&mut child);
            return Err(e);
        }
        Ok(Self(Mutex::new(Helper {
            child: Some(child),
            input: Some(input),
            output,
            killed: false,
            cleanup_end: None,
        })))
    }
    fn lock_until(
        &self,
        end: impl Fn() -> Instant,
        check: impl Fn() -> Result<(), PublicError>,
    ) -> Result<MutexGuard<'_, Helper>, PublicError> {
        loop {
            // A retry after expiry may observe an already-finished child, but
            // never wait for a new interval or discard the existing handle.
            match self.0.try_lock() {
                Ok(h) => return Ok(h),
                Err(TryLockError::Poisoned(_)) => return Err(unavailable()),
                Err(TryLockError::WouldBlock) => {}
            }
            check()?;
            if Instant::now() >= end() {
                return Err(fail("tokenizer_exit_unproven"));
            }
            pause(end())?;
        }
    }
    fn call(&self, request: Value, limit: &CodecLimit) -> Result<Value, PublicError> {
        limit.check()?;
        let mut h = self.lock_until(|| limit.end(), || limit.check())?;
        let result = (|| {
            let mut bytes = serde_json::to_vec(&request).map_err(|_| unavailable())?;
            bytes.push(b'\n');
            let mut written = 0;
            while written < bytes.len() {
                limit.check()?;
                match h
                    .input
                    .as_mut()
                    .ok_or_else(unavailable)?
                    .write(&bytes[written..])
                {
                    Ok(0) => return Err(unavailable()),
                    Ok(n) => written += n,
                    Err(e) if e.kind() == ErrorKind::Interrupted => continue,
                    Err(e) if e.kind() == ErrorKind::WouldBlock => pause(limit.end())?,
                    Err(_) => return Err(unavailable()),
                }
            }
            // ChildStdin is unbuffered: no blocking flush/read_line wrapper.
            let mut line = Vec::new();
            let mut buf = [0u8; 4096];
            loop {
                limit.check()?;
                match h.output.read(&mut buf) {
                    Ok(0) => return Err(unavailable()),
                    Ok(n) => {
                        if line.len() + n > protocol::remote::REMOTE_MAX_FRAME_BYTES {
                            return Err(fail("tokenizer_response_too_large"));
                        }
                        line.extend_from_slice(&buf[..n]);
                        if let Some(at) = buf[..n].iter().position(|b| *b == b'\n') {
                            if at + 1 != n {
                                return Err(unavailable());
                            }
                            return serde_json::from_slice(&line).map_err(|_| unavailable());
                        }
                    }
                    Err(e) if e.kind() == ErrorKind::Interrupted => continue,
                    Err(e) if e.kind() == ErrorKind::WouldBlock => pause(limit.end())?,
                    Err(_) => return Err(unavailable()),
                }
            }
        })();
        if let Err(e) = &result {
            eprintln!("tokenizer_exchange_failed: {}", e.code);
            h.input.take();
            let end = limit.end();
            h.cleanup_end = Some(h.cleanup_end.map_or(end, |old| old.min(end)));
            if let Err(cleanup) = settle(&mut h, end, true) {
                eprintln!("tokenizer_cleanup_pending: {}", cleanup.code);
                return Err(cleanup);
            }
        }
        result
    }
    /// Probe only; never a signal target. A held exchange returns unavailable.
    pub fn owned_pid(&self) -> Result<Option<u32>, PublicError> {
        let h = self.0.try_lock().map_err(|_| unavailable())?;
        Ok(h.child.as_ref().map(Child::id))
    }
    /// EOF then bounded exit. Any failure retains the child for close retry.
    pub fn close_until(&self, end: Instant) -> Result<(), PublicError> {
        let mut h = self.lock_until(|| end, || Ok(()))?;
        h.input.take();
        h.cleanup_end = Some(h.cleanup_end.map_or(end, |old| old.min(end)));
        settle(&mut h, end, false)
    }
    pub fn close(&self) -> Result<(), PublicError> {
        self.close_until(Instant::now() + span(B::RouterClose)?)
    }
}
/// Never take the Child before a fallible status/kill. try_wait is the proof;
/// expiry preserves the exact handle and the first cleanup end for later retry.
fn settle(h: &mut Helper, end: Instant, force: bool) -> Result<(), PublicError> {
    let end = h.cleanup_end.map_or(end, |old| old.min(end));
    loop {
        let Some(child) = h.child.as_mut() else {
            return Ok(());
        };
        if let Some(status) = child
            .try_wait()
            .map_err(|_| fail("tokenizer_exit_unproven"))?
        {
            let killed = h.killed;
            h.child.take();
            eprintln!("tokenizer_reaped: killed={killed} status={status}");
            return if status.success() || killed {
                Ok(())
            } else {
                Err(fail("tokenizer_exit_failed"))
            };
        }
        if !h.killed && (force || Instant::now() >= end) {
            child.kill().map_err(|_| fail("tokenizer_kill_failed"))?;
            h.killed = true;
            continue;
        }
        if Instant::now() >= end {
            return Err(fail("tokenizer_exit_unproven"));
        }
        pause(end)?;
    }
}
fn reap_last_resort(child: &mut Child) {
    if let Err(e) = child.kill() {
        eprintln!("tokenizer_drop_kill_failed: {e}");
    }
    if let Err(e) = child.wait() {
        eprintln!("tokenizer_drop_reap_failed: {e}");
    }
}
impl Drop for NativeCodec {
    fn drop(&mut self) {
        let h = self.0.get_mut().unwrap_or_else(|p| p.into_inner());
        h.input.take();
        if let Some(child) = h.child.as_mut() {
            reap_last_resort(child);
        }
    }
}
impl Codec for NativeCodec {
    fn encode(&self, text: &str) -> Result<Vec<u32>, PublicError> {
        self.encode_until(
            text,
            &CodecLimit::new(Instant::now() + span(B::ModelSelection)?),
        )
    }
    fn decode(&self, token: u32) -> Result<String, PublicError> {
        self.decode_until(
            token,
            &CodecLimit::new(Instant::now() + span(B::ModelSelection)?),
        )
    }
    fn encode_until(&self, text: &str, limit: &CodecLimit) -> Result<Vec<u32>, PublicError> {
        if text.len() > crate::requests::MAX_REQUEST_BODY {
            return Err(fail("tokenizer_request_too_large"));
        }
        serde_json::from_value(self.call(json!({"encode":text}), limit)?["ids"].take())
            .map_err(|_| unavailable())
    }
    fn decode_until(&self, token: u32, limit: &CodecLimit) -> Result<String, PublicError> {
        Ok(self.call(json!({"decode":token}), limit)?["text"]
            .as_str()
            .ok_or_else(unavailable)?
            .to_owned())
    }
    fn settle_until(&self, end: Instant) -> Result<(), PublicError> {
        let mut h = self.lock_until(|| end, || Ok(()))?;
        if h.input.is_some() {
            return Ok(());
        }
        settle(&mut h, end, true)
    }
}
