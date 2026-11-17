// SPDX-License-Identifier: AGPL-3.0-only
//! Lane XB probe (NETWORK, ignored): two endpoints in this process register at Mycelium's relay
//! (`relay.mycelium.now`, `Reach::Shipped`) with every direct IP transport removed
//! (`NetworkMode::ForceRelay`), so the ONLY path between them is the relay. One dials the other,
//! sends a stage-shard-sized chunk on the artifact channel, the other answers with its digest;
//! the test prints the relayed throughput. Never run by the gates (`--skip probe_`, and it is
//! `#[ignore]`d): the orchestrator runs it once the relay is up:
//! `cargo test -p runtime --features iroh-adapter --test iroh_relay_probe -- --ignored --nocapture`.
#![cfg(all(feature = "iroh-adapter", unix))]
use budgets::{ClockDomain, MonoInstant};
use iroh::SecretKey;
use runtime::iroh_host::{LoopbackHost, MYCELIUM_RELAY_URL, NetworkMode, Reach, selected_path};
use std::time::{Duration, Instant};
use transport::{Channel, Clock};

struct Mono(Instant);
impl Clock for Mono {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(
            ClockDomain(2),
            u64::try_from(self.0.elapsed().as_millis()).unwrap_or(u64::MAX),
        )
    }
    fn wall_seconds(&self) -> f64 {
        panic!("the probe must not consult wall time")
    }
}
fn deadline(clock: &Mono, seconds: u64) -> MonoInstant {
    clock
        .monotonic()
        .checked_add(budgets::Span::from_millis(seconds * 1000))
        .unwrap()
}
/// LAN shard chunk size (2 MiB, XN-PLAN §1 T5), four of them.
const CHUNK: usize = 2 * 1024 * 1024;
const CHUNKS: usize = 4;

#[test]
#[ignore = "network: registers at relay.mycelium.now; run by the orchestrator once the relay is up"]
fn probe_two_endpoints_exchange_a_shard_chunk_through_the_mycelium_relay_only() {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .unwrap();
    let clock = Mono(Instant::now());
    let bind = |key: SecretKey| {
        LoopbackHost::bind_network(
            &rt,
            &clock,
            deadline(&clock, 10),
            key,
            &[Channel::ArtifactChunk],
            NetworkMode::ForceRelay,
            &Reach::Shipped,
        )
        .expect("bound, relay only")
    };
    let (server_key, client_key) = (SecretKey::generate(), SecretKey::generate());
    let mut server = bind(server_key.clone());
    let mut client = bind(client_key.clone());
    let online = Instant::now();
    for host in [&server, &client] {
        rt.block_on(tokio::time::timeout(
            Duration::from_secs(15),
            host.online().unwrap(),
        ))
        .expect("registered at relay.mycelium.now within 15 s");
    }
    let online_ms = online.elapsed().as_millis();
    let address = server.address().unwrap();
    assert_eq!(address.ip_addrs().count(), 0, "direct addresses suppressed");
    assert_eq!(
        address
            .relay_urls()
            .map(|u| u.to_string())
            .collect::<Vec<_>>()
            .iter()
            .map(|u| u.trim_end_matches('/'))
            .collect::<Vec<_>>(),
        vec![MYCELIUM_RELAY_URL],
        "Mycelium's relay and nothing else"
    );
    let payload: Vec<u8> = (0..CHUNK).map(|i| (i % 251) as u8).collect();
    let (connected_ms, transfer_ms, received) = std::thread::scope(|scope| {
        let served = scope.spawn(|| {
            let c = server.accept(&clock, deadline(&clock, 30)).unwrap();
            assert_eq!(c.remote_id(), client_key.public());
            rt.block_on(async {
                let (mut send, mut recv) = c.accept_bi().await.unwrap();
                let bytes = recv.read_to_end(CHUNK * CHUNKS + 1).await.unwrap();
                send.write_all(&(bytes.len() as u64).to_be_bytes())
                    .await
                    .unwrap();
                send.finish().unwrap();
                send.stopped().await.ok();
                bytes.len()
            })
        });
        let began = Instant::now();
        let c = client.connect_channel(&address, Channel::ArtifactChunk, &clock, deadline(&clock, 30))
            .expect("connected through the relay");
        let connected_ms = began.elapsed().as_millis();
        assert_eq!(selected_path(&c), Some("relay"), "the relay is the only path");
        let began = Instant::now();
        let acknowledged = rt.block_on(async {
            let (mut send, mut recv) = c.open_bi().await.unwrap();
            for _ in 0..CHUNKS {
                send.write_all(&payload).await.unwrap();
            }
            send.finish().unwrap();
            recv.read_to_end(8).await.unwrap()
        });
        let transfer_ms = began.elapsed().as_millis();
        assert_eq!(acknowledged, ((CHUNK * CHUNKS) as u64).to_be_bytes().to_vec());
        c.close(0u32.into(), b"done");
        (connected_ms, transfer_ms, served.join().unwrap())
    });
    assert_eq!(received, CHUNK * CHUNKS);
    let mbit = (received as f64 * 8.0) / (transfer_ms.max(1) as f64 / 1000.0) / 1_000_000.0;
    eprintln!(
        "probe_relay relay={MYCELIUM_RELAY_URL} online_ms={online_ms} connect_ms={connected_ms} \
         bytes={received} transfer_ms={transfer_ms} relayed_mbit_s={mbit:.2}"
    );
    client.close(&clock, deadline(&clock, 5)).unwrap();
    server.close(&clock, deadline(&clock, 5)).unwrap();
}
