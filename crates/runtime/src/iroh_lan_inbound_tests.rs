// SPDX-License-Identifier: AGPL-3.0-only
//! Inbound source admission of the direct-LAN transport (lane B9f, REVIEW-laneB9
//! F1).
//!
//! iroh 1.0.2 maps the source of every datagram a custom transport hands up
//! from `CustomEndpoint::poll_recv` (`socket.rs:654-660`) through
//! `AddrMap::get`, which inserts on first sight and never evicts
//! (`mapped_addrs.rs:348-359`). The distinct sources [`LanTransport`] hands up
//! are therefore exactly the inbound growth of that map. These cases drive the
//! real transport endpoint (the object iroh polls) with datagrams from many
//! distinct source sockets on this host's own interfaces and count the distinct
//! sources it returns.
use super::{Admission, LanTransport};
use crate::iroh_host::{LanPolicy, private_lan};
use budgets::{BudgetId as B, Entries};
use iroh::endpoint::transports::{CustomEndpoint, CustomTransport, RecvInfo};
use std::{
    collections::HashSet,
    io::IoSliceMut,
    net::{IpAddr, SocketAddr, UdpSocket},
    sync::Arc,
    task::Poll,
    time::Duration,
};

/// Distinct source sockets per source address (one datagram each).
const SOURCES_PER_IP: usize = 96;

fn count(id: B) -> usize {
    usize::try_from(id.count().unwrap().get()).unwrap()
}
fn span(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap()
}
/// What a QUIC v1 client's first datagram looks like on the wire: a long-header
/// Initial (RFC 9000 17.2.2), padded to the 1200-byte minimum. A host that can
/// only spoof sources can send exactly this.
fn initial_like() -> Vec<u8> {
    let mut datagram = vec![0u8; 1200];
    datagram[0] = 0xc0;
    datagram[1..5].copy_from_slice(&1u32.to_be_bytes());
    datagram
}
/// A short-header (1-RTT) datagram: valid only on an established path.
fn short_header() -> Vec<u8> {
    let mut datagram = vec![0u8; 64];
    datagram[0] = 0x40;
    datagram
}
struct Rig {
    rt: tokio::runtime::Runtime,
    admission: Arc<Admission>,
    endpoint: Box<dyn CustomEndpoint>,
    local: SocketAddr,
}
impl Rig {
    fn new(interface: IpAddr, policy: LanPolicy) -> Self {
        let rt = runtime();
        let admission = Admission::new(
            policy,
            count(B::SidecarQueueCapacity),
            count(B::ReplayCapacity),
        );
        let transport = LanTransport::bind(interface, admission.clone()).unwrap();
        let local = transport.local();
        let endpoint = {
            let _entered = rt.enter();
            transport.bind().unwrap()
        };
        Self {
            rt,
            admission,
            endpoint,
            local,
        }
    }
    /// Every distinct source the transport hands to iroh until it stays quiet
    /// for one receive poll (each one is a new entry in iroh's address map).
    fn drain(&mut self) -> HashSet<String> {
        let quiet = span(B::SidecarReceivePollWait);
        let endpoint = &mut self.endpoint;
        self.rt.block_on(async {
            let mut sources = HashSet::new();
            let mut storage = vec![[0u8; 2048]; 8];
            loop {
                let received = tokio::time::timeout(
                    quiet,
                    std::future::poll_fn(|cx| {
                        let mut bufs: Vec<IoSliceMut<'_>> =
                            storage.iter_mut().map(|b| IoSliceMut::new(b)).collect();
                        let mut metas = [noq_udp::RecvMeta::default(); 8];
                        let mut infos: [RecvInfo; 8] = Default::default();
                        match endpoint.poll_recv(cx, &mut bufs, &mut metas, &mut infos) {
                            Poll::Ready(Ok(n)) => Poll::Ready(
                                infos[..n]
                                    .iter()
                                    .map(|i| format!("{i:?}"))
                                    .collect::<Vec<_>>(),
                            ),
                            Poll::Ready(Err(error)) => panic!("lan transport receive: {error}"),
                            Poll::Pending => Poll::Pending,
                        }
                    }),
                )
                .await;
                match received {
                    Ok(batch) => sources.extend(batch),
                    Err(_quiet) => return sources,
                }
            }
        })
    }
}
/// Send one `datagram` from each of `n` distinct sockets bound to `at`; the
/// sockets stay open (distinct ports) until the caller drops them.
fn flood(at: IpAddr, n: usize, to: SocketAddr, datagram: &[u8]) -> Vec<UdpSocket> {
    (0..n)
        .map(|_| {
            let socket = UdpSocket::bind(SocketAddr::new(at, 0)).unwrap();
            socket.send_to(datagram, to).unwrap();
            socket
        })
        .collect()
}
fn bound(admission_slots: usize, observed: usize, what: &str) {
    assert!(
        observed <= admission_slots,
        "{what}: {observed} distinct inbound sources reached iroh's never-evicting \
         address map (bound {admission_slots})"
    );
}
fn ip(text: &str) -> IpAddr {
    text.parse().unwrap()
}

/// Loopback (explicitly allowed): one pinned peer plus a flood of unpinned
/// sources, half Initial-shaped, half short-header. The pinned peer's traffic
/// is delivered; the unpinned flood reaches iroh only up to the provisional
/// slot bound, whatever it looks like.
#[test]
fn inbound_sources_reaching_iroh_are_bounded_on_loopback() {
    let loopback = ip("127.0.0.1");
    let mut rig = Rig::new(loopback, LanPolicy::with_explicit(vec![loopback]).unwrap());
    let peer = UdpSocket::bind(SocketAddr::new(loopback, 0)).unwrap();
    let _pin = rig.admission.pin([peer.local_addr().unwrap()]).unwrap();
    peer.send_to(&short_header(), rig.local).unwrap();
    let pinned = rig.drain();
    assert_eq!(
        pinned.len(),
        1,
        "the pinned peer's datagram must be delivered"
    );
    let _initials = flood(loopback, SOURCES_PER_IP, rig.local, &initial_like());
    let _short = flood(loopback, SOURCES_PER_IP, rig.local, &short_header());
    let flooded = rig.drain();
    bound(
        count(B::RelayConnectionSlots),
        flooded.len(),
        "loopback flood",
    );
    peer.send_to(&short_header(), rig.local).unwrap();
    assert_eq!(
        rig.drain().len(),
        1,
        "the pinned peer must still be delivered after the flood"
    );
}

/// Real interfaces of THIS host only: the transport bound to the private LAN
/// address (`MYCELIUM_LAN_IP`, private allow-list) receives Initial-shaped
/// datagrams from many ports on that address and on every other local address
/// in `MYCELIUM_LAN_EXTRA_SOURCES` (comma-separated, e.g. 127.0.0.1 and a
/// tailnet address: outside the allow-list). Nothing from outside the
/// allow-list reaches iroh; in-range unpinned sources only up to the bound.
#[test]
#[ignore = "needs MYCELIUM_LAN_IP (+ optional MYCELIUM_LAN_EXTRA_SOURCES) of this host"]
fn inbound_sources_reaching_iroh_are_bounded_on_real_interfaces() {
    let lan = ip(&std::env::var("MYCELIUM_LAN_IP").expect("MYCELIUM_LAN_IP"));
    assert!(private_lan(lan), "{lan} is not a private LAN address");
    let extra: Vec<IpAddr> = std::env::var("MYCELIUM_LAN_EXTRA_SOURCES")
        .unwrap_or_default()
        .split(',')
        .filter(|s| !s.is_empty())
        .map(ip)
        .collect();
    let mut rig = Rig::new(lan, LanPolicy::private_only());
    let mut outside = 0;
    for at in &extra {
        assert!(!private_lan(*at), "{at} must be outside the allow-list");
        let _held = flood(*at, SOURCES_PER_IP, rig.local, &initial_like());
        let reached = rig.drain();
        eprintln!("{at}: {} distinct sources reached iroh", reached.len());
        outside += reached.len();
    }
    let _held = flood(lan, SOURCES_PER_IP, rig.local, &initial_like());
    let inside = rig.drain();
    eprintln!("{lan}: {} distinct sources reached iroh", inside.len());
    assert_eq!(
        outside, 0,
        "sources outside the allow-list reached iroh's address map"
    );
    bound(
        count(B::RelayConnectionSlots),
        inside.len(),
        "in-range flood",
    );
}

/// B9 (owner ruling 16 Nov (2 a), ledger rows 92-93): the owner's step-4 Local Network probe (one
/// one-byte datagram, `api::local_network::Udp`, to a joined Mac's signed work address) used to be
/// dropped unread by the joined Mac's receive filter (`member_control_filter dropped_in=1` on Mac B
/// in row 111), so Mac B sent nothing on the LAN until the owner's step-6 dial and its own Local
/// Network alert came after the compile, costing a run. The joined Mac's listener now answers that
/// probe with one one-byte datagram to its source: B's first LAN packet, which raises B's alert at
/// step 4 too. It answers only the exact probe, only to an allow-listed source, at most once per
/// source host per handshake window, and the probe still never reaches iroh.
#[test]
fn the_joined_macs_listener_answers_the_owners_local_network_probe_once() {
    let loopback = ip("127.0.0.1");
    let mut rig = Rig::new(loopback, LanPolicy::with_explicit(vec![loopback]).unwrap());
    let owner = UdpSocket::bind(SocketAddr::new(loopback, 0)).unwrap();
    owner
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    owner.send_to(&[0], rig.local).unwrap();
    assert!(rig.drain().is_empty(), "the probe must never reach iroh");
    let mut reply = [9u8; 16];
    let (n, from) = owner
        .recv_from(&mut reply)
        .expect("the joined Mac's listener did not answer the owner's probe");
    assert_eq!((n, reply[0], from), (1, 0, rig.local));
    // A second probe from the same host inside the window: no second answer (no reflector), and
    // a datagram that is not the probe is never answered.
    owner.send_to(&[0], rig.local).unwrap();
    let other = UdpSocket::bind(SocketAddr::new(loopback, 0)).unwrap();
    other
        .set_read_timeout(Some(Duration::from_millis(400)))
        .unwrap();
    other.send_to(&[1], rig.local).unwrap();
    other.send_to(&short_header(), rig.local).unwrap();
    rig.drain();
    owner
        .set_read_timeout(Some(Duration::from_millis(400)))
        .unwrap();
    assert!(owner.recv_from(&mut reply).is_err(), "answered twice");
    assert!(other.recv_from(&mut reply).is_err(), "answered a non-probe");
}

/// B9: a probe from a source outside the allow-list is never answered (the listener sends nothing
/// the transport's own policy would refuse).
#[test]
fn a_probe_from_outside_the_allow_list_is_not_answered() {
    let loopback = ip("127.0.0.1");
    // Loopback bound, but the policy allows only the private ranges: 127.0.0.1 is outside.
    let mut rig = Rig::new(loopback, LanPolicy::private_only());
    let stranger = UdpSocket::bind(SocketAddr::new(loopback, 0)).unwrap();
    stranger
        .set_read_timeout(Some(Duration::from_millis(400)))
        .unwrap();
    stranger.send_to(&[0], rig.local).unwrap();
    assert!(rig.drain().is_empty());
    let mut reply = [9u8; 4];
    assert!(
        stranger.recv_from(&mut reply).is_err(),
        "answered a stranger"
    );
}
