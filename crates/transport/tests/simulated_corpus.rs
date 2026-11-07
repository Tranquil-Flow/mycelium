// SPDX-License-Identifier: AGPL-3.0-only
//! Deterministic seam corpus, not a network, TLS or QUIC qualification claim.
use budgets::{ClockDomain, MonoInstant};
use protocol::remote::{RemoteKind, decode_remote_frame, encode_remote_frame};
use std::{cell::Cell, collections::VecDeque};
use transport::*;
struct Time {
    mono: Cell<MonoInstant>,
}
impl Clock for Time {
    fn monotonic(&self) -> MonoInstant {
        self.mono.get()
    }
    fn wall_seconds(&self) -> f64 {
        panic!("transport authority must not use wall time")
    }
}
fn instant(t: u64) -> MonoInstant {
    MonoInstant::new(ClockDomain(1), t)
}
struct Sim<'a> {
    clock: &'a Time,
    finish: MonoInstant,
    calls: usize,
    peer: ConnectionFence,
    channel: Channel,
    frames: VecDeque<Vec<u8>>,
    cleanup: Result<(), Error>,
}
impl Transport for Sim<'_> {
    fn exchange(&mut self, _: &Exchange) -> Result<Response, Error> {
        self.calls += 1;
        self.clock.mono.set(self.finish);
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
    fn cancel(&mut self, _: [u8; 16], _: MonoInstant) -> Result<(), Error> {
        self.calls += 1;
        self.clock.mono.set(self.finish);
        self.cleanup
    }
    fn close(&mut self, _: MonoInstant) -> Result<(), Error> {
        self.calls += 1;
        self.clock.mono.set(self.finish);
        self.cleanup
    }
}
fn request(channel: Channel) -> Exchange {
    Exchange {
        peer: ConnectionFence {
            endpoint_id: [7; 32],
            local_generation: 2,
            remote_generation: 3,
        },
        channel,
        message_id: [5; 16],
        payload: vec![1],
        deadline: instant(10),
        maximum_response_bytes: 16,
    }
}
fn adapter<'a>(clock: &'a Time, r: &Exchange) -> Sim<'a> {
    Sim {
        clock,
        finish: instant(1),
        calls: 0,
        peer: r.peer,
        channel: r.channel,
        frames: VecDeque::new(),
        cleanup: Ok(()),
    }
}
#[test]
fn cancellation_and_cleanup_obey_inclusive_deadline_and_clock_domain() {
    for closing in [false, true] {
        for (start, finish, want, calls) in [
            (instant(10), instant(1), Err(Error::Expired), 0),
            (instant(0), instant(10), Err(Error::Expired), 1),
            (
                MonoInstant::new(ClockDomain(2), 0),
                instant(1),
                Err(Error::ClockDomain),
                0,
            ),
            (
                instant(0),
                MonoInstant::new(ClockDomain(2), 1),
                Err(Error::ClockDomain),
                1,
            ),
            (instant(0), instant(9), Ok(()), 1),
        ] {
            let clock = Time {
                mono: Cell::new(start),
            };
            let r = request(Channel::Router);
            let mut a = adapter(&clock, &r);
            a.finish = finish;
            let got = if closing {
                close(&mut a, r.deadline, &clock)
            } else {
                cancel(&mut a, r.message_id, r.deadline, &clock)
            };
            assert_eq!(got, want, "closing={closing}");
            assert_eq!(a.calls, calls);
        }
        for error in [
            Error::CleanupUnproven,
            Error::Cancelled,
            Error::Unavailable,
            Error::Authentication,
            Error::Generation,
        ] {
            let clock = Time {
                mono: Cell::new(instant(0)),
            };
            let r = request(Channel::Router);
            let mut a = adapter(&clock, &r);
            a.cleanup = Err(error);
            assert_eq!(
                if closing {
                    close(&mut a, r.deadline, &clock)
                } else {
                    cancel(&mut a, r.message_id, r.deadline, &clock)
                },
                Err(error)
            );
        }
    }
}
#[test]
fn all_channels_run_fence_correlation_size_and_wire_fault_corpus() {
    for channel in [
        Channel::Router,
        Channel::NodeControl,
        Channel::ArtifactChunk,
    ] {
        for case in 0..10 {
            let clock = Time {
                mono: Cell::new(instant(0)),
            };
            let r = request(channel);
            let mut a = adapter(&clock, &r);
            let mut id = r.message_id;
            let mut payload = vec![1; 16];
            let mut kind = RemoteKind::Ack;
            let want = match case {
                0 => None,
                1 => {
                    a.peer.endpoint_id = [8; 32];
                    Some(Error::Authentication)
                }
                2 => {
                    a.peer.local_generation = 1;
                    Some(Error::Generation)
                }
                3 => {
                    a.peer.remote_generation = 4;
                    Some(Error::Generation)
                }
                4 => {
                    a.channel = if channel == Channel::Router {
                        Channel::ArtifactChunk
                    } else {
                        Channel::Router
                    };
                    Some(Error::Channel)
                }
                5 => {
                    id = [4; 16];
                    Some(Error::Correlation)
                }
                6 => {
                    payload.push(0);
                    Some(Error::FrameTooLarge)
                }
                7 => {
                    kind = RemoteKind::Error;
                    Some(Error::Unavailable)
                }
                8 | 9 => Some(Error::Unavailable),
                _ => unreachable!(),
            };
            let mut frame = encode_remote_frame(kind, id, &payload).unwrap();
            if case == 8 {
                frame.pop();
            }
            if case == 9 {
                frame.push(0);
            }
            a.frames.push_back(frame);
            let got = exchange(&mut a, &r, &clock);
            match want {
                Some(e) => assert_eq!(got, Err(e), "channel={channel:?} case={case}"),
                None => assert_eq!(got.unwrap(), payload),
            }
        }
    }
}
#[test]
fn delayed_duplicate_and_reordered_frames_never_satisfy_another_request() {
    let clock = Time {
        mono: Cell::new(instant(0)),
    };
    let mut r = request(Channel::Router);
    let mut a = adapter(&clock, &r);
    let old = encode_remote_frame(RemoteKind::Ack, r.message_id, b"old").unwrap();
    a.frames.extend([old.clone(), old]);
    assert_eq!(exchange(&mut a, &r, &clock).unwrap(), b"old");
    r.message_id = [9; 16];
    assert_eq!(exchange(&mut a, &r, &clock), Err(Error::Correlation));
    a.frames
        .push_back(encode_remote_frame(RemoteKind::Ack, r.message_id, b"new").unwrap());
    assert_eq!(exchange(&mut a, &r, &clock).unwrap(), b"new");
    assert_eq!(exchange(&mut a, &r, &clock), Err(Error::Unavailable));
    a.frames
        .push_back(encode_remote_frame(RemoteKind::Ack, r.message_id, b"late").unwrap());
    a.finish = instant(10);
    assert_eq!(exchange(&mut a, &r, &clock), Err(Error::Expired));
    // Connection replacement is independent of request correlation.
    clock.mono.set(instant(0));
    a.finish = instant(1);
    r.peer.local_generation += 1;
    a.frames
        .push_back(encode_remote_frame(RemoteKind::Ack, r.message_id, b"stale").unwrap());
    assert_eq!(exchange(&mut a, &r, &clock), Err(Error::Generation));
}
