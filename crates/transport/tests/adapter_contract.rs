// SPDX-License-Identifier: AGPL-3.0-only
use budgets::{ClockDomain, MonoInstant};
use std::cell::Cell;
use transport::*;
struct Time(Cell<u64>);
impl Clock for Time {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(ClockDomain(1), self.0.get())
    }
    fn wall_seconds(&self) -> f64 {
        1000.0
    }
}
struct Adapter<'a> {
    reply: Result<Response, Error>,
    calls: usize,
    finish: u64,
    clock: &'a Time,
}
impl Transport for Adapter<'_> {
    fn exchange(&mut self, _: &Exchange) -> Result<Response, Error> {
        self.calls += 1;
        self.clock.0.set(self.finish);
        self.reply.clone()
    }
    fn cancel(&mut self, _: [u8; 16], _: MonoInstant) -> Result<(), Error> {
        Err(Error::Cancelled)
    }
    fn close(&mut self, _: MonoInstant) -> Result<(), Error> {
        Err(Error::CleanupUnproven)
    }
}
fn request() -> Exchange {
    Exchange {
        peer: ConnectionFence {
            endpoint_id: [7; 32],
            local_generation: 2,
            remote_generation: 3,
        },
        channel: Channel::Router,
        message_id: [5; 16],
        payload: b"hello".to_vec(),
        deadline: MonoInstant::new(ClockDomain(1), 10),
        maximum_response_bytes: 16,
    }
}
fn reply(r: &Exchange) -> Response {
    Response {
        authenticated_peer: r.peer,
        channel: r.channel,
        message_id: r.message_id,
        payload: b"ack".to_vec(),
    }
}

#[test]
fn authenticated_fence_and_alpn_corpus() {
    let req = request();
    let clock = Time(Cell::new(0));
    let good = reply(&req);
    let mut adapter = Adapter {
        reply: Ok(good.clone()),
        calls: 0,
        finish: 1,
        clock: &clock,
    };
    assert_eq!(exchange(&mut adapter, &req, &clock).unwrap(), b"ack");
    let mut cases = Vec::new();
    let mut r = good.clone();
    r.authenticated_peer.endpoint_id = [8; 32];
    cases.push((r, Error::Authentication));
    let mut r = good.clone();
    r.authenticated_peer.remote_generation += 1;
    cases.push((r, Error::Generation));
    let mut r = good.clone();
    r.authenticated_peer.local_generation += 1;
    cases.push((r, Error::Generation));
    let mut r = good.clone();
    r.channel = Channel::ArtifactChunk;
    cases.push((r, Error::Channel));
    let mut r = good.clone();
    r.message_id = [6; 16];
    cases.push((r, Error::Correlation));
    let mut r = good;
    r.payload = vec![0; 17];
    cases.push((r, Error::FrameTooLarge));
    for (r, e) in cases {
        adapter.reply = Ok(r);
        assert_eq!(exchange(&mut adapter, &req, &clock), Err(e));
    }
    assert_eq!(Channel::Router.alpn(), b"mycelium.iroh.sidecar.v1");
    assert_eq!(Channel::NodeControl.alpn(), b"mycelium.node_control.v1");
    assert_eq!(Channel::ArtifactChunk.alpn(), b"mycelium.artifact_chunk.v1");
}

#[test]
fn expired_before_dispatch_or_during_exchange_is_not_success() {
    let req = request();
    let clock = Time(Cell::new(11));
    let mut adapter = Adapter {
        reply: Ok(reply(&req)),
        calls: 0,
        finish: 12,
        clock: &clock,
    };
    assert_eq!(exchange(&mut adapter, &req, &clock), Err(Error::Expired));
    assert_eq!(adapter.calls, 0);
    clock.0.set(0);
    assert_eq!(exchange(&mut adapter, &req, &clock), Err(Error::Expired));
    assert_eq!(adapter.calls, 1);
    clock.0.set(0);
    let mut wrong = req.clone();
    wrong.deadline = MonoInstant::new(ClockDomain(2), 10);
    assert_eq!(
        exchange(&mut adapter, &wrong, &clock),
        Err(Error::ClockDomain)
    );
    assert_eq!(adapter.calls, 1);
}

#[test]
fn failure_cancel_and_cleanup_are_explicit() {
    let req = request();
    let clock = Time(Cell::new(0));
    let mut adapter = Adapter {
        reply: Err(Error::Unavailable),
        calls: 0,
        finish: 1,
        clock: &clock,
    };
    assert_eq!(
        exchange(&mut adapter, &req, &clock),
        Err(Error::Unavailable)
    );
    assert_eq!(
        adapter.cancel(req.message_id, req.deadline),
        Err(Error::Cancelled)
    );
    assert_eq!(adapter.close(req.deadline), Err(Error::CleanupUnproven));
}
