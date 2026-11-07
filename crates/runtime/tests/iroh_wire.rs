// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-NET-09 (the Router transfer prefix and ack are the pinned sidecar bytes; forged, late, wrong-kind and non-empty acks cannot claim delivery).
//!
//! Independent exact legacy carrier bytes; no endpoint, socket or relay.
use budgets::{ClockDomain, MonoInstant};
use protocol::remote::{RemoteKind, encode_remote_frame};
use runtime::iroh_wire::*;
use transport::*;
fn request() -> Exchange {
    Exchange {
        peer: ConnectionFence {
            endpoint_id: [7; 32],
            local_generation: 2,
            remote_generation: 3,
        },
        channel: Channel::Router,
        message_id: [5; 16],
        payload: vec![9, 10],
        deadline: MonoInstant::new(ClockDomain(1), 1),
        maximum_response_bytes: 0,
    }
}
#[test]
fn router_transfer_prefix_and_ack_match_pinned_sidecar_without_channel_guessing() {
    let mut r = request();
    let mut expected = vec![1, 1];
    expected.extend([5; 16]);
    expected.extend([0, 0, 0, 10]);
    expected.extend([0, 0, 0, 0, 0, 0, 0, 2, 9, 10]);
    assert_eq!(transfer(&r).unwrap(), expected);
    let ack = encode_remote_frame(RemoteKind::Ack, r.message_id, b"").unwrap();
    assert_eq!(acknowledgement(&ack, &r), Ok(()));
    for kind in [Channel::NodeControl, Channel::ArtifactChunk] {
        r.channel = kind;
        assert_eq!(transfer(&r), Err(Error::Channel));
    }
}
#[test]
fn forged_late_wrong_kind_and_nonempty_ack_cannot_claim_delivery() {
    let r = request();
    for (kind, id, payload, want) in [
        (RemoteKind::Ack, [4; 16], b"".as_slice(), Error::Correlation),
        (
            RemoteKind::Ack,
            r.message_id,
            b"text".as_slice(),
            Error::Unavailable,
        ),
        (
            RemoteKind::Error,
            r.message_id,
            b"peer_rotated".as_slice(),
            Error::Generation,
        ),
        (
            RemoteKind::Error,
            r.message_id,
            b"replay_collision".as_slice(),
            Error::Unavailable,
        ),
    ] {
        assert_eq!(
            acknowledgement(&encode_remote_frame(kind, id, payload).unwrap(), &r),
            Err(want)
        );
    }
    let mut bytes = encode_remote_frame(RemoteKind::Ack, r.message_id, b"").unwrap();
    bytes.push(0);
    assert_eq!(acknowledgement(&bytes, &r), Err(Error::Unavailable));
}
