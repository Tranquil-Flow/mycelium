// SPDX-License-Identifier: AGPL-3.0-only
//! Shared private link between a member AGENT (the signer; it holds the key, the NodeControl
//! endpoint and the Router carrier) and its NATIVE WORKER (the compute child), RU-020.
//!
//! v0.1.4 runs the member agent in the app process and the compute worker as its own
//! session-leader child (`worker.py:1002-1060`, executed in `r9-oracle/`): the worker can die
//! while the agent lives on and signs the cleanup. This is the agent's side of that split: one
//! `UnixStream` socketpair end per worker, inherited as the worker's descriptor 1, so no
//! filesystem path and no TCP socket exists. The link carries a bounded JSON head and a bounded
//! binary payload; a peer that is gone is a typed [`LinkError::Closed`], never a silent hang.
mod deadline;
use protocol::router_wire::MAX_PAYLOAD_BYTES;
use serde_json::Value;
use std::{
    io::{ErrorKind, Read, Write},
    os::unix::net::UnixStream,
    time::Duration,
};

/// Why the link failed. Closed set; codes are for logs, never for the public API.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LinkError {
    /// The peer is gone (EOF, reset, broken pipe): the worker died or was killed.
    Closed,
    /// The peer is alive but silent past the allowance.
    Timeout,
    /// An absolute request deadline expired.
    Deadline,
    /// The owner cancelled this request.
    Cancelled,
    /// The bytes are not a frame of this link.
    Frame(&'static str),
    /// The peer answered `{"error": code}`.
    Remote(String),
}
impl std::fmt::Display for LinkError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Deadline => f.write_str("worker_link_deadline"),
            Self::Cancelled => f.write_str("worker_link_cancelled"),
            Self::Closed => f.write_str("worker_link_closed"),
            Self::Timeout => f.write_str("worker_link_timeout"),
            Self::Frame(code) => f.write_str(code),
            Self::Remote(code) => write!(f, "worker_remote:{code}"),
        }
    }
}
impl std::error::Error for LinkError {}

fn io_error(error: &std::io::Error) -> LinkError {
    match error.kind() {
        ErrorKind::WouldBlock | ErrorKind::TimedOut => LinkError::Timeout,
        _ => LinkError::Closed,
    }
}

/// One end of the link.
pub struct Link<S = UnixStream> {
    stream: S,
    frame_wait: Duration,
    read_wait: Duration,
}
impl Link {
    /// `wait` bounds every write, and every read once a frame has begun.
    pub fn new(stream: UnixStream, wait: Duration) -> std::io::Result<Self> {
        stream.set_read_timeout(Some(wait))?;
        stream.set_write_timeout(Some(wait))?;
        Ok(Self {
            stream,
            frame_wait: wait,
            read_wait: wait,
        })
    }
}
impl<S: Read + Write> Link<S> {
    /// Frame: `u32 BE head length | head JSON | u32 BE payload length | payload`.
    pub fn send(&mut self, head: &Value, payload: &[u8]) -> Result<(), LinkError> {
        let head =
            serde_json::to_vec(head).map_err(|_| LinkError::Frame("worker_link_head_invalid"))?;
        let head_len = u32::try_from(head.len())
            .ok()
            .filter(|_| head.len() <= protocol::remote::REMOTE_MAX_FRAME_BYTES)
            .ok_or(LinkError::Frame("worker_link_head_too_large"))?;
        let payload_len = u32::try_from(payload.len())
            .ok()
            .filter(|_| payload.len() <= MAX_PAYLOAD_BYTES)
            .ok_or(LinkError::Frame("worker_link_payload_too_large"))?;
        let mut frame =
            Vec::with_capacity(head.len() + payload.len() + 2 * std::mem::size_of::<u32>());
        frame.extend(head_len.to_be_bytes());
        frame.extend(&head);
        frame.extend(payload_len.to_be_bytes());
        frame.extend(payload);
        self.stream.write_all(&frame).map_err(|e| io_error(&e))?;
        self.stream.flush().map_err(|e| io_error(&e))
    }
    /// `first` is the already-read first byte of the length, if any.
    fn length(
        &mut self,
        limit: usize,
        too_large: &'static str,
        first: Option<u8>,
    ) -> Result<usize, LinkError> {
        let mut raw = [0; std::mem::size_of::<u32>()];
        let have = usize::from(first.is_some());
        if let Some(byte) = first {
            raw[0] = byte;
        }
        self.stream
            .read_exact(&mut raw[have..])
            .map_err(|e| io_error(&e))?;
        usize::try_from(u32::from_be_bytes(raw))
            .ok()
            .filter(|n| *n <= limit)
            .ok_or(LinkError::Frame(too_large))
    }
    /// Read exactly `len` bytes without trusting the declared length for the allocation.
    fn exact(&mut self, len: usize) -> Result<Vec<u8>, LinkError> {
        let mut bytes = Vec::new();
        let read = (&mut self.stream)
            .take(len as u64)
            .read_to_end(&mut bytes)
            .map_err(|e| io_error(&e))?;
        if read == len {
            Ok(bytes)
        } else {
            Err(LinkError::Closed)
        }
    }
}
impl Link {
    /// Best effort: macOS refuses `setsockopt` on a socket whose peer already closed (EINVAL), and
    /// the read that follows then reports the buffered bytes or the end of the stream anyway.
    fn read_timeout(&mut self, wait: Duration) {
        if self.read_wait != wait && self.stream.set_read_timeout(Some(wait)).is_ok() {
            self.read_wait = wait;
        }
    }
    /// The first byte of the next frame, or `None` if the peer stayed silent for `wait`. A frame
    /// that has begun is always read whole (bounded by the frame allowance), so a poll never
    /// leaves the stream half-read.
    fn first_byte(&mut self, wait: Duration) -> Result<Option<u8>, LinkError> {
        self.read_timeout(wait);
        let mut byte = [0; 1];
        let read = self.stream.read(&mut byte);
        self.read_timeout(self.frame_wait);
        match read {
            Ok(0) => Err(LinkError::Closed),
            Ok(_) => Ok(Some(byte[0])),
            Err(e) => match io_error(&e) {
                LinkError::Timeout => Ok(None),
                other => Err(other),
            },
        }
    }
    /// Wait up to `wait` for a frame: `Ok(None)` is silence, not a failure (the agent uses it to
    /// look at its worker between polls, like v0.1.4's 0.25 s `_watch`).
    pub fn poll(&mut self, wait: Duration) -> Result<Option<(Value, Vec<u8>)>, LinkError> {
        match self.first_byte(wait)? {
            Some(first) => self.frame(Some(first)).map(Some),
            None => Ok(None),
        }
    }
    pub fn receive(&mut self) -> Result<(Value, Vec<u8>), LinkError> {
        self.poll(self.frame_wait)?.ok_or(LinkError::Timeout)
    }
}
impl<S: Read + Write> Link<S> {
    fn frame(&mut self, first: Option<u8>) -> Result<(Value, Vec<u8>), LinkError> {
        let head_len = self.length(
            protocol::remote::REMOTE_MAX_FRAME_BYTES,
            "worker_link_head_too_large",
            first,
        )?;
        let head = self.exact(head_len)?;
        let head: Value = serde_json::from_slice(&head)
            .map_err(|_| LinkError::Frame("worker_link_head_invalid"))?;
        let payload_len = self.length(MAX_PAYLOAD_BYTES, "worker_link_payload_too_large", None)?;
        let payload = self.exact(payload_len)?;
        Ok((head, payload))
    }
}
impl Link {
    /// Send one command and read its reply; an `{"error": code}` reply is [`LinkError::Remote`].
    pub fn request(&mut self, head: &Value, payload: &[u8]) -> Result<(Value, Vec<u8>), LinkError> {
        self.send(head, payload)?;
        let (reply, body) = self.receive()?;
        match reply.get("error").and_then(Value::as_str) {
            Some(code) => Err(LinkError::Remote(code.to_owned())),
            None => Ok((reply, body)),
        }
    }
}
