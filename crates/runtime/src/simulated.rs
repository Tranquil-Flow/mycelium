// SPDX-License-Identifier: AGPL-3.0-only
//! Explicitly simulated framed adapter; never a real authenticated connection.
use budgets::MonoInstant;
use protocol::remote::{RemoteKind, decode_remote_frame};
use std::collections::VecDeque;
use transport::*;
pub trait SimClock: Clock {
    fn advance(&self, instant: MonoInstant);
}
pub struct Sim<'a, C: SimClock> {
    pub clock: &'a C,
    pub finish: MonoInstant,
    pub calls: usize,
    pub peer: ConnectionFence,
    pub channel: Channel,
    pub frames: VecDeque<Vec<u8>>,
    pub cleanup: Result<(), Error>,
    pub closed: bool,
}
impl<C: SimClock> Transport for Sim<'_, C> {
    fn exchange(&mut self, _request: &Exchange) -> Result<Response, Error> {
        if self.closed {
            return Err(Error::Unavailable);
        }
        self.calls += 1;
        self.clock.advance(self.finish);
        let bytes = self.frames.pop_front().ok_or(Error::Unavailable)?;
        let frame = decode_remote_frame(&bytes).map_err(|_| Error::Unavailable)?;
        if frame.kind != RemoteKind::Ack {
            return Err(Error::Unavailable);
        }
        Ok(Response {
            authenticated_peer: self.peer,
            channel: self.channel,
            message_id: frame.message_id,
            payload: frame.payload,
        })
    }
    fn cancel(&mut self, _message_id: [u8; 16], _deadline: MonoInstant) -> Result<(), Error> {
        self.calls += 1;
        self.clock.advance(self.finish);
        self.cleanup
    }
    fn close(&mut self, _deadline: MonoInstant) -> Result<(), Error> {
        self.calls += 1;
        self.clock.advance(self.finish);
        self.closed = true;
        self.cleanup?;
        self.frames.clear();
        Ok(())
    }
}
