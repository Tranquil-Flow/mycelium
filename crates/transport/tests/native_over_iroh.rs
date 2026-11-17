// SPDX-License-Identifier: AGPL-3.0-only
//! Frozen L5 outer-sidecar corpus through the actual L9 Iroh carrier.
//! Loopback only; this is not a physical split or a whole-application test.
use budgets::{BudgetId, ClockDomain, Entries, MonoInstant};
use iroh::{Endpoint, EndpointAddr, RelayMode, endpoint::presets};
use runtime::{iroh::IrohTransport, iroh_host::LoopbackHost};
use std::{
    net::{Ipv4Addr, SocketAddrV4, UdpSocket},
    time::{Duration, Instant},
};
use transport::{Channel, Clock, ConnectionFence, Error, Exchange, Transport};

struct Time(Instant);
impl Clock for Time {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(ClockDomain(5), self.0.elapsed().as_millis() as u64)
    }
    fn wall_seconds(&self) -> f64 {
        panic!("carrier must not use wall time")
    }
}
impl Time {
    fn deadline(&self) -> MonoInstant {
        self.monotonic()
            .checked_add(BudgetId::RouterClose.span(Entries::ZERO).unwrap())
            .unwrap()
    }
}
fn allowance() -> Duration {
    Duration::from_millis(BudgetId::RouterClose.span(Entries::ZERO).unwrap().millis())
}
fn bytes(s: &str) -> Vec<u8> {
    assert_eq!(s.len() % 2, 0);
    s.as_bytes()
        .chunks_exact(2)
        .map(|b| u8::from_str_radix(std::str::from_utf8(b).unwrap(), 16).unwrap())
        .collect()
}

#[test]
fn frozen_outer_frames_cross_real_authenticated_router_streams() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let clock = Time(Instant::now());
    let server = rt.block_on(async {
        Endpoint::builder(presets::Minimal)
            .portmapper_config(iroh::endpoint::PortmapperConfig::Disabled)
            .relay_mode(RelayMode::Disabled)
            .clear_address_lookup()
            .clear_ip_transports()
            .bind_addr(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))
            .unwrap()
            .alpns(vec![Channel::Router.alpn().to_vec()])
            .bind()
            .await
            .unwrap()
    });
    let sockets = server.bound_sockets();
    assert_eq!(sockets.len(), 1);
    assert_eq!(sockets[0].ip(), Ipv4Addr::LOCALHOST);
    let peer = EndpointAddr::new(server.id()).with_ip_addr(sockets[0]);
    let mut client = LoopbackHost::bind(&rt, &clock, clock.deadline()).unwrap();
    let client_id = client.address().unwrap().id;
    let mut count = 0;
    for row in transport::qualification::ROUTER_NATIVE_CORPUS_TSV
        .lines()
        .filter(|l| !l.is_empty() && !l.starts_with('#'))
    {
        let row: Vec<_> = row.split('\t').collect();
        let raw = bytes(row[1]);
        let owned = server.clone();
        let responder = rt.spawn(async move {
            tokio::time::timeout(allowance(), async move {
                let connection = owned.accept().await.unwrap().await.unwrap();
                assert_eq!(connection.remote_id(), client_id);
                assert_eq!(connection.alpn(), Channel::Router.alpn());
                let (mut send, mut recv) = connection.accept_bi().await.unwrap();
                let request = recv
                    .read_to_end(protocol::remote::REMOTE_MAX_FRAME_BYTES)
                    .await
                    .unwrap();
                let expected = transport::qualification::ROUTER_NATIVE_CORPUS_TSV
                    .lines()
                    .find(|l| l.starts_with("generation_transfer\t"))
                    .unwrap()
                    .split('\t')
                    .nth(1)
                    .unwrap();
                assert_eq!(
                    request,
                    bytes(expected),
                    "actual outgoing generation framing"
                );
                send.write_all(&raw).await.unwrap();
                send.finish().unwrap();
                connection.closed().await;
            })
            .await
            .unwrap();
        });
        let connection = client.connect(&peer, &clock, clock.deadline()).unwrap();
        let fence = ConnectionFence {
            endpoint_id: *server.id().as_bytes(),
            local_generation: 3,
            remote_generation: 7,
        };
        let mut carrier = IrohTransport::new(connection, &rt, &clock, fence, || fence).unwrap();
        let request = Exchange {
            peer: fence,
            channel: Channel::Router,
            message_id: [5; 16],
            payload: b"\0\xffMYAC".to_vec(),
            deadline: clock.deadline(),
            maximum_response_bytes: 0,
        };
        let result = carrier.exchange(&request);
        let expected = match row[4] {
            "Delivered" => Ok(()),
            "PeerRotated" => Err(Error::Generation),
            // L9 deliberately retires ambiguous attempts rather than retries.
            // ReplayCollision loses its distinct source code: retained seam gap.
            "ReplayCollision" => Err(Error::Unavailable),
            "Retry" if row[0] == "wrong_id" => Err(Error::Correlation),
            "Retry" => Err(Error::Unavailable),
            other => panic!("unknown source outcome {other}"),
        };
        assert_eq!(
            result.map(|r| {
                assert!(r.payload.is_empty());
                assert_eq!(r.authenticated_peer, fence);
            }),
            expected,
            "{}",
            row[0]
        );
        carrier.close(clock.deadline()).unwrap();
        drop(carrier);
        rt.block_on(responder).unwrap();
        count += 1;
    }
    let provenance = transport::qualification::ROUTER_NATIVE_PROVENANCE_JSON;
    assert!(provenance.contains(&format!("\"rows\": {count},")));
    client.close(&clock, clock.deadline()).unwrap();
    drop(client);
    rt.block_on(async {
        tokio::time::timeout(allowance(), server.close())
            .await
            .unwrap()
    });
    drop(server);
    drop(rt);
    // Executor shutdown must actually release the bound UDP socket.
    drop(UdpSocket::bind(sockets[0]).expect("server UDP socket leaked"));
}
