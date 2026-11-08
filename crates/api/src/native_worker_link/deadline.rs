// SPDX-License-Identifier: AGPL-3.0-only
//! Compiler-only absolute bounds; legacy link callers retain their framing and policy.
use super::*;
use std::{
    io,
    sync::atomic::{AtomicBool, Ordering},
    time::Instant,
};

struct DeadlineIo<'a> {
    stream: &'a mut UnixStream,
    deadline: Instant,
    cancel: &'a AtomicBool,
    interval: Duration,
}
impl DeadlineIo<'_> {
    fn remaining(&self) -> Result<Duration, LinkError> {
        if self.cancel.load(Ordering::SeqCst) {
            return Err(LinkError::Cancelled);
        }
        self.deadline
            .checked_duration_since(Instant::now())
            .filter(|d| !d.is_zero())
            .map(|d| d.min(self.interval))
            .ok_or(LinkError::Deadline)
    }
    fn wait(&self) -> io::Result<Duration> {
        self.remaining().map_err(io::Error::other)
    }
}
fn retry(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        ErrorKind::Interrupted | ErrorKind::WouldBlock | ErrorKind::TimedOut
    )
}
impl Read for DeadlineIo<'_> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        loop {
            let wait = self.wait()?;
            let result = match self.stream.set_read_timeout(Some(wait)) {
                Ok(()) => self.stream.read(buf),
                Err(e) if e.kind() == ErrorKind::InvalidInput => {
                    // Like the publisher's DeadlineTcp: macOS can reject the
                    // timeout after EOF with a complete reply still buffered.
                    // Never fall back to the preceding, longer blocking timeout.
                    eprintln!("worker_timeout_update_nonblocking: {e}");
                    self.stream.set_nonblocking(true)?;
                    let result = self.stream.read(buf);
                    self.stream.set_nonblocking(false)?;
                    result
                }
                Err(e) => return Err(e),
            };
            self.wait()?; // late bytes are not an admitted completion
            match result {
                Err(e) if retry(&e) => continue,
                other => return other,
            }
        }
    }
}
impl Write for DeadlineIo<'_> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        loop {
            let wait = self.wait()?;
            self.stream.set_write_timeout(Some(wait))?;
            let result = self.stream.write(buf);
            self.wait()?;
            match result {
                Err(e) if retry(&e) => continue,
                other => return other,
            }
        }
    }
    fn flush(&mut self) -> io::Result<()> {
        self.wait()?;
        let result = self.stream.flush();
        self.wait()?;
        result
    }
}
impl Link {
    /// A terminal exchange: on error discard the link, since a partial frame
    /// must never be retried. The same deadline covers serialization, send,
    /// all frame parts, JSON decoding, and the final completion check.
    pub fn request_until(
        &mut self,
        head: &Value,
        payload: &[u8],
        deadline: Instant,
        cancel: &AtomicBool,
        interval: Duration,
    ) -> Result<(Value, Vec<u8>), LinkError> {
        if interval.is_zero() {
            return Err(LinkError::Frame("worker_poll_interval_invalid"));
        }
        let mut bounded = Link {
            stream: DeadlineIo {
                stream: &mut self.stream,
                deadline,
                cancel,
                interval,
            },
            frame_wait: self.frame_wait,
            read_wait: self.read_wait,
        };
        bounded.stream.remaining()?;
        let result = bounded
            .send(head, payload)
            .and_then(|()| bounded.frame(None));
        bounded.stream.remaining()?;
        result
    }
}
