// SPDX-License-Identifier: AGPL-3.0-only
//! Explicit local-only Router endpoint ownership. No public relays, address
//! lookup, wildcard sockets or default N0 preset. This is the sidecar's local
//! mode, not a production internet-discovery policy. The packaged app's choice
//! of the one interface address a direct-LAN endpoint binds
//! (`api::lan_interface`) opens no socket at all (it reads `getifaddrs` and
//! the routing table through `sysctl`), so the no-wildcard rule has no
//! exception on the work path.
//!
//! Direct-LAN paths after the handshake (iroh 1.0.2 + noq 1.3.0, verified in the
//! pinned sources): the endpoint config always enables QUIC multipath and n0 NAT
//! traversal (`iroh-1.0.2/src/endpoint/quic.rs:153-162`; the builder ignores
//! smaller values, `quic.rs:472-481,537-546`). An AUTHENTICATED peer can
//! therefore advertise extra candidates (ADD_ADDRESS / REACH_OUT,
//! `noq-proto-1.3.0/src/n0_nat_traversal.rs:456-506,556-582,789-821`) that this
//! endpoint probes and then opens as paths (`iroh-1.0.2/src/socket/remote_map/
//! remote_state.rs:385-449,515-568,924-946`) from its one bound socket, to any
//! address, outside the allow-list. Relay, address lookup, net-report probes
//! (empty relay map, `socket.rs:794-801`) and the portmapper (feature off) stay
//! disabled; only bound sockets are local candidates (`socket.rs:1955-1961`).
//! The public API has no path filter. Since lane B9 a direct-LAN endpoint has no
//! IP transport and sends only through `iroh_lan_filter::LanTransport`, which
//! checks every datagram's destination BEFORE the socket send (exact pins by
//! default, see [`LanPolicy`]); a candidate outside it never receives a packet.
//! Defence in depth: every connection is still watched and closed
//! (`LAN_PATH_REFUSED`) as soon as any path's remote address is not admitted;
//! see `confine`.
pub use crate::iroh_cleanup::{HostCloseReport, close_endpoint_and_native};
use crate::iroh_cross::{CrossGate, cross_builder, direct_addr_kind};
use crate::iroh_lan_filter::{Admission, LanTransport, Pin, from_custom, to_custom};
use budgets::{BudgetId, Entries, MonoInstant};
use iroh::{
    Endpoint, EndpointAddr, RelayMode, TransportAddr,
    endpoint::{Connection, PortmapperConfig, presets},
};
use std::{
    net::{IpAddr, Ipv4Addr, SocketAddr, SocketAddrV4},
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::sync::watch;
use transport::{Channel, Clock, Error};

/// Which network the sidecar's endpoint lives on (`bind_endpoint`, `sidecar.rs:834-866 @ f4e366e9`).
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NetworkMode {
    /// 127.0.0.1 only, no relay, no address lookup.
    LocalOnly,
    /// Mycelium's relay ([`Reach`]) and every direct transport; no address lookup.
    Production,
    /// [`NetworkMode::Production`] without any direct IP transport: relay only.
    ForceRelay,
    /// Explicitly configured direct LAN: ONE bound interface address, no relay,
    /// no address lookup, and peers only at allow-listed addresses
    /// ([`LanPolicy`]). Never a default; see [`LoopbackHost::bind_lan`].
    DirectLan,
    /// Lane XB2 (ruling 29 B): a direct-LAN choice whose policy crosses networks
    /// ([`LanPolicy::across_networks`]), a deliberate per-run choice. ONE iroh IP socket on the
    /// configured interface address, Mycelium's relay only, no address lookup; peers at their
    /// signed addresses of any unicast class or through the relay (hole punching included), and
    /// handshakes only under admitted keys. See `iroh_cross`.
    CrossNetwork,
}
/// Peer/bind allow-list of the direct-LAN mode: private IPv4 (RFC 1918),
/// IPv4 link-local, IPv6 unique-local (fc00::/7) and IPv6 link-local, plus
/// explicitly configured extra addresses. Unspecified, multicast and broadcast
/// addresses are never allowed, not even explicitly; loopback, public and CGNAT
/// (e.g. tailnet 100.64/10) addresses only when listed.
///
/// Destinations (enforced before every send, B9): by default EXACT PINS, i.e.
/// only the socket addresses this host dialed (the caller's signed peer
/// addresses) or accepted a handshake from; [`LanPolicy::range_form`] opts in
/// to any allow-listed address. Sources: any allow-listed address, or only the
/// exact peer hosts of [`LanPolicy::with_peer_hosts`] when the caller has them;
/// an unpinned source reaches iroh only with a QUIC Initial, through a bounded
/// provisional table (B9f, `iroh_lan_filter` module note). The host of a
/// handshake verified under an admitted peer key is learned and gets its own
/// lane of that table (B14, [`LoopbackHost::lan_learned_hosts`]).
///
/// Where "signed peer addresses" come from, as the packaged callers verify
/// them (not every address carries the seed's own signature): a member's
/// control and carrier addresses are signed by that member's key, accepted
/// only while the seed's current row enrolls that key; a shard source's
/// addresses are inside the seed-signed grant; the owner's address on a
/// member is never in signed data and is learned from a handshake under an
/// admitted key (B14).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct LanPolicy {
    extra: Vec<IpAddr>,
    range: bool,
    hosts: Option<Vec<IpAddr>>,
    /// Lane XB2: `Some` = the cross-network choice ([`LanPolicy::across_networks`]).
    relay: Option<iroh::RelayUrl>,
    /// Lane XA: the user's portmapping choice (default off).
    portmapping: bool,
}
impl LanPolicy {
    pub fn private_only() -> Self {
        Self::default()
    }
    /// Lane XB2 (ruling 29 B), a deliberate per-run choice: work with peers on OTHER networks.
    /// The endpoint registers at `relay` (Mycelium's, both ends share it) and iroh may use relayed
    /// and hole-punched direct paths. ONLY the address class is relaxed: a signed peer address may
    /// be any unicast address (never unspecified, multicast or broadcast); peer keys, exact signed
    /// dial targets and their refusal counts stay (`iroh_cross`).
    pub fn across_networks(mut self, relay: iroh::RelayUrl) -> Self {
        self.relay = Some(relay);
        self
    }
    /// Lane XA (ruling 29 A): ask the local router for a port mapping (UPnP / PCP / NAT-PMP) so a
    /// peer on another network may reach this endpoint directly. A user's choice, DEFAULT OFF:
    /// its SSDP multicast can raise a firewall prompt on macOS. Only an endpoint across networks
    /// uses it ([`LanPolicy::across_networks`]); every other builder disables it.
    pub fn with_portmapping(mut self, on: bool) -> Self {
        self.portmapping = on;
        self
    }
    /// Lane XA: whether the run asked for portmapping.
    pub fn portmapping(&self) -> bool {
        self.portmapping
    }
    /// The relay of the cross-network choice; `None` on the same network.
    pub fn relay(&self) -> Option<&iroh::RelayUrl> {
        self.relay.as_ref()
    }
    /// The run's choice as status names it: `same_network` or `relay`.
    pub fn reach(&self) -> &'static str {
        if self.relay.is_some() {
            "relay"
        } else {
            "same_network"
        }
    }
    /// Add explicitly configured addresses (operator configuration, never discovery).
    pub fn with_explicit(extra: Vec<IpAddr>) -> Result<Self, Error> {
        if extra.iter().any(|ip| never(*ip)) {
            return Err(Error::Authentication);
        }
        Ok(Self {
            extra,
            ..Self::default()
        })
    }
    /// Range form: send to any allow-listed address, not only exact pins
    /// (B8 behaviour; still never outside the allow-list).
    pub fn range_form(mut self) -> Self {
        self.range = true;
        self
    }
    /// Exact peer hosts (taken from signed pairing/offer data): inbound
    /// handshakes only from these addresses. Each must be allow-listed.
    pub fn with_peer_hosts(mut self, hosts: Vec<IpAddr>) -> Result<Self, Error> {
        if hosts.is_empty() || hosts.iter().any(|ip| !self.allows(*ip)) {
            return Err(Error::Authentication);
        }
        self.hosts = Some(hosts);
        Ok(self)
    }
    pub fn allows(&self, ip: IpAddr) -> bool {
        let ip = ip.to_canonical();
        !never(ip) && (self.relay.is_some() || private_lan(ip) || self.extra.contains(&ip))
    }
    /// True in the range form; false = exact pins (the default).
    pub fn range_allowed(&self) -> bool {
        self.range
    }
    pub fn peer_hosts(&self) -> Option<&[IpAddr]> {
        self.hosts.as_deref()
    }
}
/// Lane XB3: the code of a cross-network run whose relay cannot be reached. The run fails
/// closed: no other relay and no address lookup is configured, so nothing falls back to n0.
pub const RELAY_UNREACHABLE: &str = "relay_unreachable";
/// Application close code of a direct-LAN connection that gained a path outside
/// the allow-list (reason `lan_path_refused`).
pub const LAN_PATH_REFUSED: u32 = 3;
/// Application close code of an accepted direct-LAN handshake under a key
/// outside [`LoopbackHost::admit_peer_ids`] (reason `lan_key_refused`).
pub const LAN_KEY_REFUSED: u32 = 4;
fn count(id: BudgetId) -> Result<usize, Error> {
    usize::try_from(id.count().map_err(|_| Error::Unavailable)?.get())
        .map_err(|_| Error::Unavailable)
}
/// Distinct exact destinations one direct-LAN host may pin at once.
fn lan_pin_capacity() -> Result<usize, Error> {
    count(BudgetId::SidecarQueueCapacity)
}
/// One control handshake's cap: a connect on the owner side, an inbound handshake on the member
/// side. On the direct LAN it is the local handshake budget (`SidecarLocalHandshake`, 2 s): the
/// peer is one hop away. Across networks the first handshake of a freshly bound endpoint runs
/// through Mycelium's relay (both ends attach to it, then QUIC handshakes over it) and a mobile
/// hotspot adds its own latency, so the cap is v0.1.4's bound for the same cross-network
/// owner-to-member connect (the managed-control mailbox connect, 30 s). With the 2 s cap the
/// owner's first `consent_status` call timed out on hardware (rv51 hotspot session,
/// `control_connect_timeout ... allowance_ms=2000`) while the member saw the late handshake
/// arrive (`member_control_refused ... accept_Authentication`).
pub fn control_handshake(across_networks: bool) -> Result<Duration, Error> {
    budget(if across_networks {
        BudgetId::PyMyceliumA13MacosManagedControlManagedcontrolmailboxConnectTimeoutL823
    } else {
        BudgetId::SidecarLocalHandshake
    })
}
fn budget(id: BudgetId) -> Result<Duration, Error> {
    Ok(Duration::from_millis(
        id.span(Entries::ZERO)
            .map_err(|_| Error::Unavailable)?
            .millis(),
    ))
}
/// A remote transport address this host may not send to (a relay or a
/// foreign custom address is outside by definition).
fn outside_addr(addr: &TransportAddr, admission: &Admission) -> bool {
    match addr {
        TransportAddr::Ip(addr) => !admission.admits_path(*addr),
        TransportAddr::Custom(custom) => {
            from_custom(custom).is_none_or(|a| !admission.admits_path(a))
        }
        _ => true,
    }
}
/// An address-book entry this host never admitted (see `Admission::admits_seen`).
fn unseen_addr(addr: &TransportAddr, admission: &Admission) -> bool {
    match lan_path_addr(addr) {
        Some(a) => !admission.admits_seen(a),
        None => true,
    }
}
/// Any currently open path of `connection` outside the admitted destinations.
fn outside(connection: &Connection, admission: &Admission) -> bool {
    connection
        .paths()
        .iter()
        .any(|path| outside_addr(path.remote_addr(), admission))
}
/// The UDP peer of a path's remote address on a direct-LAN connection (the
/// pre-send LAN transport's address, or a plain IP); `None` for anything else.
pub fn lan_path_addr(addr: &TransportAddr) -> Option<SocketAddr> {
    match addr {
        TransportAddr::Ip(addr) => Some(*addr),
        TransportAddr::Custom(custom) => from_custom(custom),
        _ => None,
    }
}
/// The address iroh dials for a validated direct-LAN peer: each IP becomes the
/// [`LanTransport`] address (a direct-LAN endpoint has no IP transport).
fn lan_target(peer: &EndpointAddr) -> EndpointAddr {
    EndpointAddr::from_parts(
        peer.id,
        peer.ip_addrs()
            .map(|socket| TransportAddr::Custom(to_custom(*socket))),
    )
}
fn never(ip: IpAddr) -> bool {
    ip.is_unspecified() || ip.is_multicast() || matches!(ip, IpAddr::V4(v4) if v4.is_broadcast())
}
/// RFC 1918 / IPv4 link-local / IPv6 ULA / IPv6 link-local.
pub fn private_lan(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => v4.is_private() || v4.is_link_local(),
        IpAddr::V6(v6) => {
            let first = v6.segments()[0];
            (first & 0xfe00) == 0xfc00 || (first & 0xffc0) == 0xfe80
        }
    }
}
/// Which kind of path `connection` runs on now: `direct` (an IP or direct-LAN path) or `relay`;
/// `None` before iroh selected one. For status and evidence (lane XB2).
pub fn selected_path(connection: &Connection) -> Option<&'static str> {
    connection
        .paths()
        .iter()
        .find(|path| path.is_selected())
        .map(|path| if path.is_relay() { "relay" } else { "direct" })
}
/// Lane XB3 at a run's start: bind a throwaway endpoint across networks on `interface` under
/// `policy` (a fresh key, no portmapping, no channel served) and wait at most
/// `SidecarReadyAddressWindow` (the oracle's wait for a relay address) for its relay.
/// `Err(RELAY_UNREACHABLE)` when it cannot be reached; `Ok` at once for a same-network policy.
pub fn relay_reachable(interface: IpAddr, policy: &LanPolicy) -> Result<(), &'static str> {
    if policy.relay().is_none() {
        return Ok(());
    }
    let unreachable = |_| RELAY_UNREACHABLE;
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .map_err(|_| RELAY_UNREACHABLE)?;
    let clock = crate::iroh_cross::ProbeClock::default();
    let after = |id: BudgetId| -> Result<MonoInstant, &'static str> {
        let span = id.span(Entries::ZERO).map_err(|_| RELAY_UNREACHABLE)?;
        clock
            .monotonic()
            .checked_add(span)
            .map_err(|_| RELAY_UNREACHABLE)
    };
    let mut host = LoopbackHost::bind_lan(
        &runtime,
        &clock,
        after(BudgetId::WorkerStartup)?,
        iroh::SecretKey::generate(),
        &[Channel::Router],
        interface,
        policy.clone().with_portmapping(false),
    )
    .map_err(unreachable)?;
    let ready = host.relay_ready(&clock, after(BudgetId::SidecarReadyAddressWindow)?);
    if let Err(error) = host.close(&clock, after(BudgetId::RouterClose)?) {
        eprintln!("relay_probe_close_unproven:{error:?}");
    }
    drop(host);
    ready
}
/// Lane XB patch 2 (REVIEW XB-01, D4: only our relay): every relay entry of a peer address must be
/// `ours`, the one relay this endpoint registers at; a peer-supplied relay URL that is not (an n0
/// relay, any other) refuses the whole address before anything is sent, so the endpoint id never
/// reaches a relay Mycelium does not operate. With no relay of its own nothing may name one.
pub fn validate_relays(peer: &EndpointAddr, ours: Option<&iroh::RelayUrl>) -> Result<(), Error> {
    if peer.relay_urls().all(|url| Some(url) == ours) {
        Ok(())
    } else {
        Err(Error::Authentication)
    }
}
/// Whole-address check for a direct-LAN dial: every entry is an allow-listed IP
/// with a port; a relay or any disallowed entry refuses the entire address.
pub fn validate_lan_peer(peer: &EndpointAddr, policy: &LanPolicy) -> Result<(), Error> {
    if peer.addrs.is_empty()
        || peer.ip_addrs().count() != peer.addrs.len()
        || peer
            .ip_addrs()
            .any(|addr| addr.port() == 0 || !policy.allows(addr.ip()))
    {
        return Err(Error::Authentication);
    }
    Ok(())
}
impl NetworkMode {
    /// Every mode (lane XB patch 2, XB-09: the structural builder guard iterates it). A new mode
    /// fails to compile in [`NetworkMode::listed`] until it is listed here too.
    pub const ALL: [NetworkMode; 5] = [
        Self::LocalOnly,
        Self::Production,
        Self::ForceRelay,
        Self::DirectLan,
        Self::CrossNetwork,
    ];
    /// The index of `self` in [`NetworkMode::ALL`] (an exhaustive match: no wildcard).
    pub const fn listed(self) -> usize {
        match self {
            Self::LocalOnly => 0,
            Self::Production => 1,
            Self::ForceRelay => 2,
            Self::DirectLan => 3,
            Self::CrossNetwork => 4,
        }
    }
    pub const fn local_only(self) -> bool {
        matches!(self, Self::LocalOnly)
    }
    /// Whether the host itself enforces admitted peer keys ([`LoopbackHost::admit_peer_ids`]):
    /// direct LAN and across networks.
    pub const fn admits_by_key(self) -> bool {
        matches!(self, Self::DirectLan | Self::CrossNetwork)
    }
}
/// The relay Mycelium operates for its own endpoints (ruling 29 B; deployed by the ops lane,
/// `REPORT-OPS-relay.md`, QUIC address discovery on UDP 7842). The ONLY relay a shipped endpoint
/// registers at: no network mode configures an n0 relay, n0 DNS or a pkarr publisher (lane XB1).
pub const MYCELIUM_RELAY_URL: &str = "https://relay.mycelium.now";
/// Where the relay and address lookup of a network mode come from. Neither variant has an
/// address lookup: peers are named by endpoint ids from signed documents, never looked up.
#[derive(Clone, Debug)]
pub enum Reach {
    /// The shipped relay: Mycelium's own ([`MYCELIUM_RELAY_URL`]).
    Shipped,
    /// One explicit relay instead (an override): tests point it at a loopback port nothing
    /// listens on, so nothing leaves the host.
    Hermetic(iroh::RelayUrl),
}
impl Reach {
    /// The one relay this reach registers at.
    pub fn relay_url(&self) -> Result<iroh::RelayUrl, Error> {
        match self {
            Self::Shipped => MYCELIUM_RELAY_URL.parse().map_err(|_| Error::Unavailable),
            Self::Hermetic(url) => Ok(url.clone()),
        }
    }
}
/// The relay and address lookup of a relay-mode endpoint (Production / ForceRelay): exactly
/// the reach's relay, no address lookup (the `presets::N0` relays, DNS lookup and pkarr publish
/// are never configured, so an unreachable relay never degrades to n0).
pub(crate) fn relay_builder(
    mode: NetworkMode,
    reach: &Reach,
) -> Result<iroh::endpoint::Builder, Error> {
    // `ForceRelay` removes every direct IP transport (`sidecar.rs:846-852 @ f4e366e9`).
    let mut builder = Endpoint::builder(presets::Minimal)
        .clear_address_lookup()
        .relay_mode(RelayMode::Custom(iroh::RelayMap::from(reach.relay_url()?)))
        .portmapper_config(PortmapperConfig::Disabled);
    if mode == NetworkMode::ForceRelay {
        builder = builder.clear_ip_transports();
    }
    Ok(builder)
}

pub(crate) fn validate_local_peer(peer: &EndpointAddr) -> Result<(), Error> {
    if peer.addrs.is_empty()
        || peer.ip_addrs().count() != peer.addrs.len()
        || peer
            .ip_addrs()
            .any(|addr| addr.ip() != Ipv4Addr::LOCALHOST || addr.port() == 0)
    {
        return Err(Error::Authentication);
    }
    Ok(())
}

pub struct LoopbackHost<'a> {
    runtime: &'a tokio::runtime::Runtime,
    endpoint: Option<Endpoint>,
    admitting: bool,
    closing: Option<tokio::task::JoinHandle<()>>,
    mode: NetworkMode,
    /// The direct-LAN admission (allow-list + exact pins, shared with the
    /// pre-send transport); `Some` exactly in [`NetworkMode::DirectLan`].
    lan: Option<Arc<Admission>>,
    /// Direct LAN: the one bound socket (owned by the LAN transport).
    lan_socket: Option<SocketAddr>,
    /// Inbound handshakes in flight (both modes), bounded and concurrent, and
    /// completed connections a window ended on, for the next `accept`.
    handshakes: Mutex<tokio::task::JoinSet<Handshake>>,
    /// Bridges `Endpoint::watch_addr` into the watch channel the address gate reads.
    address_task: Option<tokio::task::JoinHandle<()>>,
    /// Direct LAN: one path watch per admitted connection (see `confine`).
    path_guards: Mutex<Vec<tokio::task::JoinHandle<()>>>,
    /// Direct LAN: what [`LoopbackHost::recycle_lan`] rebinds with.
    reuse: Option<Box<LanReuse>>,
    /// Across networks: the peer gate; `Some` exactly in [`NetworkMode::CrossNetwork`].
    cross: Option<Arc<CrossGate>>,
    /// The one relay this host registers at (relay modes and across networks); a peer address
    /// may name no other relay ([`validate_relays`], lane XB patch 2, D4).
    relay: Option<iroh::RelayUrl>,
}
/// The bound socket (a CLOEXEC duplicate; the transport owns the original)
/// and identity a direct-LAN endpoint is recycled with, so the address in the
/// caller's signed carrier advert stays exactly valid (B11).
struct LanReuse {
    socket: std::net::UdpSocket,
    secret: iroh::SecretKey,
    alpns: Vec<Vec<u8>>,
}
type Handshake = Result<(Connection, Option<Pin>), Error>;
/// Lane XB patch 2 (REVIEW XB-09): THE builder of every product endpoint shape, one per
/// [`NetworkMode`] (the local mailbox carrier of `api::managed_carrier` is [`local_builder`]).
/// Product code builds no iroh endpoint any other way (`runtime/tests/portmapping_default_off.rs`),
/// and a unit test builds every [`NetworkMode::ALL`] shape through this function and reads its
/// final configuration: portmapping off unless a cross-network policy asked, exactly its relay.
pub(crate) fn mode_builder(
    mode: NetworkMode,
    reach: &Reach,
    across: Option<(IpAddr, &LanPolicy)>,
    lan: Option<Arc<LanTransport>>,
) -> Result<iroh::endpoint::Builder, Error> {
    match mode {
        NetworkMode::LocalOnly => local_builder(),
        NetworkMode::DirectLan => Ok(lan_builder(lan.ok_or(Error::Unavailable)?)),
        NetworkMode::CrossNetwork => {
            let (interface, policy) = across.ok_or(Error::Unavailable)?;
            cross_builder(interface, policy)
        }
        NetworkMode::Production | NetworkMode::ForceRelay => relay_builder(mode, reach),
    }
}
/// The builder of a local-only endpoint: 127.0.0.1, no relay, no address lookup, no
/// portmapping. Also the local mailbox carrier's (`api::managed_carrier`).
pub fn local_builder() -> Result<iroh::endpoint::Builder, Error> {
    Endpoint::builder(presets::Minimal)
        .relay_mode(RelayMode::Disabled)
        .clear_address_lookup()
        .clear_ip_transports()
        .bind_addr(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))
        .map(|builder| builder.portmapper_config(PortmapperConfig::Disabled))
        .map_err(|_| Error::Unavailable)
}
/// The builder of a direct-LAN endpoint: no relay, no address lookup and NO
/// IP transport; the pre-send LAN transport owns the one bound socket (see
/// `iroh_lan_filter`).
pub(crate) fn lan_builder(transport: Arc<LanTransport>) -> iroh::endpoint::Builder {
    Endpoint::builder(presets::Minimal)
        .relay_mode(RelayMode::Disabled)
        .clear_address_lookup()
        .clear_ip_transports()
        .add_custom_transport(transport)
        .portmapper_config(PortmapperConfig::Disabled)
}
impl LoopbackHost<'_> {
    fn abort_path_guards(&self) {
        if let Ok(mut guards) = self.path_guards.lock() {
            for guard in guards.drain(..) {
                guard.abort();
            }
        }
        if let Ok(mut handshakes) = self.handshakes.lock() {
            handshakes.abort_all();
        }
    }
}
impl Drop for LoopbackHost<'_> {
    fn drop(&mut self) {
        // Drop is emergency teardown, never a successful close receipt.
        if let Some(task) = self.closing.take() {
            task.abort();
        }
        if let Some(task) = self.address_task.take() {
            task.abort();
        }
        self.abort_path_guards();
    }
}
/// Poll cancellation without restarting the bind/handshake future or its timeout.
async fn interruptible<T>(
    wait: Duration,
    stopped: &dyn Fn() -> bool,
    future: impl std::future::Future<Output = Result<T, Error>>,
) -> Result<T, Error> {
    let poll = budget(BudgetId::OwnerPoll)?;
    tokio::time::timeout(wait, async {
        tokio::pin!(future);
        loop {
            if stopped() {
                return Err(Error::Expired);
            }
            tokio::select! {
                biased;
                result = &mut future => return result,
                () = tokio::time::sleep(poll) => {}
            }
        }
    })
    .await
    .map_err(|_| Error::Expired)?
}

pub(crate) fn outside_runtime() -> Result<(), Error> {
    if tokio::runtime::Handle::try_current().is_ok() {
        Err(Error::Unavailable)
    } else {
        Ok(())
    }
}
/// Whether the runtime this future runs on keeps running its tasks (the
/// connection drivers, iroh's socket actors) while no caller is inside
/// `block_on`. A current-thread runtime runs them only inside `block_on`.
fn self_driven() -> bool {
    !matches!(
        tokio::runtime::Handle::current().runtime_flavor(),
        tokio::runtime::RuntimeFlavor::CurrentThread
    )
}
/// Await inside the `block_on` that closed `closed` (to `remote`, on
/// `endpoint`), before that `block_on` returns.
///
/// `close()` only queues CONNECTION_CLOSE; quinn/iroh have no "close sent"
/// future. On a self-driven runtime its worker sends the close whatever the
/// caller does next. On a current-thread runtime nothing sends it once the
/// call returns: an owner that waits outside the runtime (a result poll) lets
/// the close timer drain the connection unsent, and the peer's next packet
/// earns a stateless reset instead of the explicit close. There, keep this
/// `block_on` driving until both hops are done:
/// 1. the connection driver built the close and handed that datagram to iroh's
///    socket layer (its `connection_close` frame count; the driver runs on
///    this thread, so a counted frame was already passed to the sender), and
/// 2. iroh's actor for `remote`, which sends datagrams queued for an
///    endpoint-id path, has worked through its inbox: a `remote_info` query
///    queued behind the close datagram has been answered.
///
/// Bounded by `RouterClose`. This does not wait for the 3·PTO drain.
///
/// Known gap (REVIEW-laneB20f (1)): the close frame is counted when noq writes
/// it into a datagram (`poll_transmit`), before the socket send. If that send
/// hits `WouldBlock` (a full socket send buffer), noq keeps the datagram in its
/// `buffered_transmit` and this returns with the close still unsent; the next
/// `block_on` on this runtime sends it. Unlikely on loopback and LAN, and not
/// reproducible deterministically here (it needs the kernel's send buffer full
/// at that instant), so no test names it.
pub(crate) async fn flush_close(
    endpoint: &Endpoint,
    closed: &iroh::endpoint::WeakConnectionHandle,
    remote: iroh::EndpointId,
) -> Result<(), Error> {
    if self_driven() {
        return Ok(());
    }
    tokio::time::timeout(budget(BudgetId::RouterClose)?, async {
        // A connection that is gone was drained by its driver: nothing queued.
        while let Some(connection) = closed.upgrade() {
            if connection.stats().frame_tx.connection_close > 0 {
                drop(connection);
                endpoint.remote_info(remote).await;
                break;
            }
            drop(connection);
            tokio::task::yield_now().await;
        }
    })
    .await
    .map_err(|_| Error::CleanupUnproven)
}
/// Await inside the `block_on` that finished `send`, before it returns: the
/// same as [`flush_close`] for a response's tail and FIN. On a current-thread
/// runtime keep driving until the peer acknowledged the whole stream, stopped
/// it, or the connection ended (then nothing more can be delivered), bounded
/// by `RouterClose`. The reply bytes and their timing are unchanged.
pub(crate) async fn flush_finished(send: &iroh::endpoint::SendStream) -> Result<(), Error> {
    if self_driven() {
        return Ok(());
    }
    tokio::time::timeout(budget(BudgetId::RouterClose)?, send.stopped())
        .await
        .map(drop)
        .map_err(|_| Error::CleanupUnproven)
}
/// The error a call returns for a connection that completed after its
/// deadline: always that deadline error (REVIEW-laneB20f (2)). A close that
/// could not be proven is recorded, never put in the deadline error's place.
fn late_outcome(deadline: Error, closed: Result<(), Error>) -> Error {
    if let Err(cleanup) = closed {
        eprintln!("late connection close unproven: {cleanup:?} (returning {deadline:?})");
    }
    deadline
}
pub(crate) fn allowance(clock: &impl Clock, deadline: MonoInstant) -> Result<Duration, Error> {
    let now = clock.monotonic();
    deadline.same_domain(now).map_err(|_| Error::ClockDomain)?;
    let span = deadline.remaining(now).map_err(|_| Error::Expired)?;
    Ok(Duration::from_millis(span.millis()))
}
impl<'a> LoopbackHost<'a> {
    pub(crate) fn runtime(&self) -> &'a tokio::runtime::Runtime {
        self.runtime
    }
    /// Owns the endpoint, but borrows the host's enabled I/O/time runtime. The
    /// caller must keep driving that runtime while idle if it accepts traffic.
    pub fn bind(
        runtime: &'a tokio::runtime::Runtime,
        clock: &impl Clock,
        deadline: MonoInstant,
    ) -> Result<Self, Error> {
        Self::bind_options(
            runtime,
            clock,
            deadline,
            None,
            &[Channel::Router],
            NetworkMode::LocalOnly,
            &Reach::Shipped,
        )
    }
    fn bind_options(
        runtime: &'a tokio::runtime::Runtime,
        clock: &impl Clock,
        deadline: MonoInstant,
        secret: Option<iroh::SecretKey>,
        channels: &[Channel],
        mode: NetworkMode,
        reach: &Reach,
    ) -> Result<Self, Error> {
        if mode == NetworkMode::DirectLan {
            // Direct LAN needs its interface address and allow-list: bind_lan only.
            return Err(Error::Unavailable);
        }
        Self::bind_inner(
            runtime, clock, deadline, secret, channels, mode, reach, None,
        )
    }
    /// Explicit direct-LAN endpoint: bound to exactly `interface` (port 0), no
    /// relay, no address lookup, no wildcard socket. `interface` itself must be
    /// allowed by `policy`; peers are dialled only at allow-listed addresses.
    pub fn bind_lan(
        runtime: &'a tokio::runtime::Runtime,
        clock: &impl Clock,
        deadline: MonoInstant,
        secret: iroh::SecretKey,
        channels: &[Channel],
        interface: IpAddr,
        policy: LanPolicy,
    ) -> Result<Self, Error> {
        if channels.is_empty() {
            return Err(Error::Channel);
        }
        if !policy.allows(interface) {
            return Err(Error::Authentication);
        }
        Self::bind_inner(
            runtime,
            clock,
            deadline,
            Some(secret),
            channels,
            NetworkMode::DirectLan,
            &Reach::Shipped,
            Some((interface, policy)),
        )
    }
    /// Router-only bind with cancellation observation; identical local/LAN policy.
    /// No helper thread is spawned or abandoned when cancellation wins.
    pub fn bind_router_until(
        runtime: &'a tokio::runtime::Runtime,
        clock: &impl Clock,
        deadline: MonoInstant,
        secret: iroh::SecretKey,
        lan: Option<(IpAddr, LanPolicy)>,
        stopped: &dyn Fn() -> bool,
    ) -> Result<Self, Error> {
        if lan.as_ref().is_some_and(|(ip, policy)| !policy.allows(*ip)) {
            return Err(Error::Authentication);
        }
        let mode = if lan.is_some() {
            NetworkMode::DirectLan
        } else {
            NetworkMode::LocalOnly
        };
        Self::bind_inner_until(
            runtime,
            clock,
            deadline,
            Some(secret),
            &[Channel::Router],
            mode,
            &Reach::Shipped,
            lan,
            stopped,
        )
    }
    #[allow(clippy::too_many_arguments)]
    fn bind_inner(
        runtime: &'a tokio::runtime::Runtime,
        clock: &impl Clock,
        deadline: MonoInstant,
        secret: Option<iroh::SecretKey>,
        channels: &[Channel],
        mode: NetworkMode,
        reach: &Reach,
        lan: Option<(IpAddr, LanPolicy)>,
    ) -> Result<Self, Error> {
        Self::bind_inner_until(
            runtime,
            clock,
            deadline,
            secret,
            channels,
            mode,
            reach,
            lan,
            &|| false,
        )
    }
    #[allow(clippy::too_many_arguments)]
    fn bind_inner_until(
        runtime: &'a tokio::runtime::Runtime,
        clock: &impl Clock,
        deadline: MonoInstant,
        secret: Option<iroh::SecretKey>,
        channels: &[Channel],
        mode: NetworkMode,
        reach: &Reach,
        lan: Option<(IpAddr, LanPolicy)>,
        stopped: &dyn Fn() -> bool,
    ) -> Result<Self, Error> {
        outside_runtime()?;
        let wait = allowance(clock, deadline)?;
        let alpns: Vec<Vec<u8>> = channels.iter().map(|c| c.alpn().to_vec()).collect();
        // Lane XB2: a direct-LAN choice whose policy crosses networks binds the cross-network
        // shape instead (no LAN transport, no pre-send filter: `iroh_cross`).
        let across = match (&lan, mode) {
            (Some((interface, policy)), NetworkMode::DirectLan) if policy.relay().is_some() => {
                Some((*interface, policy.clone()))
            }
            _ => None,
        };
        let (mode, lan) = match across {
            Some(_) => (NetworkMode::CrossNetwork, None),
            None => (mode, lan),
        };
        let cross = across
            .as_ref()
            .map(|(_, policy)| Arc::new(CrossGate::new(policy.clone())));
        let admission = match &lan {
            Some((_, policy)) => Some(Admission::new(
                policy.clone(),
                lan_pin_capacity()?,
                count(BudgetId::ReplayCapacity)?,
            )),
            None => None,
        };
        let (lan_transport, reuse) = match (&lan, &admission, &secret) {
            (Some((interface, _)), Some(admission), Some(secret)) => {
                let socket = std::net::UdpSocket::bind(SocketAddr::new(*interface, 0))
                    .map_err(|_| Error::Unavailable)?;
                let kept = socket.try_clone().map_err(|_| Error::Unavailable)?;
                let transport =
                    LanTransport::on(socket, admission.clone()).map_err(|_| Error::Unavailable)?;
                let reuse = Box::new(LanReuse {
                    socket: kept,
                    secret: secret.clone(),
                    alpns: alpns.clone(),
                });
                (Some(Arc::new(transport)), Some(reuse))
            }
            (Some(_), _, _) => return Err(Error::Unavailable),
            _ => (None, None),
        };
        let lan_socket = lan_transport.as_ref().map(|t| t.local());
        let endpoint = runtime.block_on(async {
            let mut builder = mode_builder(
                mode,
                reach,
                across
                    .as_ref()
                    .map(|(interface, policy)| (*interface, policy)),
                lan_transport.clone(),
            )?
            .alpns(alpns);
            if let Some(secret) = secret {
                builder = builder.secret_key(secret);
            }
            interruptible(wait, stopped, async {
                builder.bind().await.map_err(|_| Error::Unavailable)
            })
            .await
        })?;
        let host = Self {
            runtime,
            endpoint: Some(endpoint),
            admitting: true,
            closing: None,
            mode,
            lan: admission,
            lan_socket,
            handshakes: Mutex::new(tokio::task::JoinSet::new()),
            address_task: None,
            path_guards: Mutex::new(Vec::new()),
            reuse,
            relay: match mode {
                NetworkMode::Production | NetworkMode::ForceRelay => Some(reach.relay_url()?),
                _ => cross.as_ref().and_then(|gate| gate.relay().ok().cloned()),
            },
            cross,
        };
        allowance(clock, deadline)?;
        Ok(host)
    }
    /// Bind a caller-owned endpoint identity; persisted key storage belongs to the host.
    pub fn bind_with_secret(
        runtime: &'a tokio::runtime::Runtime,
        clock: &impl Clock,
        deadline: MonoInstant,
        secret: iroh::SecretKey,
    ) -> Result<Self, Error> {
        Self::bind_options(
            runtime,
            clock,
            deadline,
            Some(secret),
            &[Channel::Router],
            NetworkMode::LocalOnly,
            &Reach::Shipped,
        )
    }
    /// Opt in to services explicitly; Router-only bind defaults stay unchanged.
    pub fn bind_protocols(
        runtime: &'a tokio::runtime::Runtime,
        clock: &impl Clock,
        deadline: MonoInstant,
        secret: iroh::SecretKey,
        channels: &[Channel],
    ) -> Result<Self, Error> {
        if channels.is_empty() {
            return Err(Error::Channel);
        }
        Self::bind_options(
            runtime,
            clock,
            deadline,
            Some(secret),
            channels,
            NetworkMode::LocalOnly,
            &Reach::Shipped,
        )
    }
    /// The sidecar's endpoint in any network mode (`bind_endpoint`, `sidecar.rs:834-866`).
    pub fn bind_network(
        runtime: &'a tokio::runtime::Runtime,
        clock: &impl Clock,
        deadline: MonoInstant,
        secret: iroh::SecretKey,
        channels: &[Channel],
        mode: NetworkMode,
        reach: &Reach,
    ) -> Result<Self, Error> {
        if channels.is_empty() {
            return Err(Error::Channel);
        }
        Self::bind_options(
            runtime,
            clock,
            deadline,
            Some(secret),
            channels,
            mode,
            reach,
        )
    }
    pub const fn mode(&self) -> NetworkMode {
        self.mode
    }
    /// A peer address this host may dial: loopback only in local-only mode (the oracle's
    /// `validate_peer`), anything in the relay modes.
    pub(crate) fn validate_peer(&self, peer: &EndpointAddr) -> Result<(), Error> {
        match (self.mode, &self.lan) {
            (NetworkMode::LocalOnly, _) => validate_local_peer(peer),
            (NetworkMode::DirectLan, Some(lan)) => validate_lan_peer(peer, lan.policy()),
            (NetworkMode::DirectLan, None) => Err(Error::Authentication),
            (NetworkMode::CrossNetwork, _) => match &self.cross {
                Some(gate) => validate_lan_peer(peer, gate.policy()),
                None => Err(Error::Authentication),
            },
            // Relay modes: any IP address (the oracle's rule), but no relay except this host's.
            _ => validate_relays(peer, self.relay.as_ref()),
        }
    }
    /// The one relay this host registers at: the reach's in the relay modes, the policy's across
    /// networks; `None` on the LAN and local only.
    pub fn relay_url(&self) -> Option<&iroh::RelayUrl> {
        self.relay.as_ref()
    }
    /// The LIVE address as a watch channel (`publish_address_updates`, `sidecar.rs:819-832`):
    /// the bind-time snapshot first, then every change, until the host closes.
    pub fn address_watch(&mut self) -> Result<watch::Receiver<EndpointAddr>, Error> {
        use iroh::Watcher;
        let endpoint = self.endpoint()?.clone();
        let (sender, receiver) = watch::channel(endpoint.addr());
        if let Some(previous) = self.address_task.take() {
            previous.abort();
        }
        self.address_task = Some(self.runtime.spawn(async move {
            let mut watcher = endpoint.watch_addr();
            loop {
                let current = watcher.get();
                if *sender.borrow() != current && sender.send(current).is_err() {
                    return;
                }
                if watcher.updated().await.is_err() {
                    return;
                }
            }
        }));
        Ok(receiver)
    }
    /// Resolves when the endpoint has registered at a relay (`Endpoint::online`); never in
    /// local-only mode, where it must not be polled.
    pub fn online(
        &self,
    ) -> Result<std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send>>, Error> {
        let endpoint = self.endpoint()?.clone();
        Ok(Box::pin(async move { endpoint.online().await }))
    }
    /// Lane XB3: across networks, wait (bounded by `deadline`) until the endpoint holds a live
    /// connection to its relay (`Endpoint::online`): without it a peer on another network has no
    /// rendezvous and no fallback path. `Err(RELAY_UNREACHABLE)` when the bound passes (or the
    /// host is closed); the caller fails its run closed with that code. A same-network or
    /// local-only host needs no relay: `Ok` at once.
    pub fn relay_ready(
        &self,
        clock: &impl Clock,
        deadline: MonoInstant,
    ) -> Result<(), &'static str> {
        if self.cross.is_none() {
            return Ok(());
        }
        outside_runtime().map_err(|_| RELAY_UNREACHABLE)?;
        let endpoint = self.endpoint().map_err(|_| RELAY_UNREACHABLE)?;
        let wait = allowance(clock, deadline).map_err(|_| RELAY_UNREACHABLE)?;
        self.runtime
            .block_on(async { tokio::time::timeout(wait, endpoint.online()).await })
            .map_err(|_| RELAY_UNREACHABLE)
    }
    pub(crate) fn endpoint(&self) -> Result<&Endpoint, Error> {
        if !self.admitting {
            return Err(Error::Unavailable);
        }
        self.endpoint.as_ref().ok_or(Error::Unavailable)
    }
    /// Only bound direct addresses are published, never addresses discovered
    /// through interface enumeration, a relay, DNS or a remote announcement.
    pub fn address(&self) -> Result<EndpointAddr, Error> {
        let endpoint = self.endpoint()?;
        if let Some(socket) = self.lan_socket {
            return Ok(EndpointAddr::new(endpoint.id()).with_ip_addr(socket));
        }
        if let Some(gate) = &self.cross {
            // Across networks: the one bound socket (a same-LAN peer reaches it directly) and
            // this host's relay; discovered public addresses travel in-band, never in a signed
            // document.
            return Ok(endpoint
                .bound_sockets()
                .into_iter()
                .fold(EndpointAddr::new(endpoint.id()), |addr, socket| {
                    addr.with_ip_addr(socket)
                })
                .with_relay_url(gate.relay()?.clone()));
        }
        if !self.mode.local_only() {
            // A relay mode publishes what the endpoint currently advertises.
            return Ok(endpoint.addr());
        }
        Ok(endpoint
            .bound_sockets()
            .into_iter()
            .fold(EndpointAddr::new(endpoint.id()), |addr, socket| {
                addr.with_ip_addr(socket)
            }))
    }
    /// Lane XA status: this host's direct addresses with their kind (`local`, `qad`,
    /// `qad_local_port`, `portmapped` only with mapping evidence, else `unknown`:
    /// [`crate::iroh_cross::direct_addr_kind`]).
    pub fn direct_addrs(&self) -> Result<Vec<(SocketAddr, &'static str)>, Error> {
        use iroh::Watcher;
        let endpoint = self.endpoint()?;
        let bound = endpoint.bound_sockets();
        if self.cross.is_none() {
            return Ok(bound.into_iter().map(|s| (s, "local")).collect());
        }
        let report = endpoint.net_report().get();
        let qad: Vec<SocketAddr> = report
            .iter()
            .flat_map(|r| {
                [
                    r.global_v4.map(SocketAddr::V4),
                    r.global_v6.map(SocketAddr::V6),
                ]
            })
            .flatten()
            .collect();
        // Mapping evidence (XB-07): iroh 1.0.2 exposes a granted mapping only through its
        // `metrics` feature (`portmap_external_address_updated`), which this build does not
        // enable: with no evidence a residual address is `unknown`, never `portmapped`, whether
        // or not this Mac chose portmapping.
        let granted = false;
        Ok(endpoint
            .addr()
            .ip_addrs()
            .map(|addr| (*addr, direct_addr_kind(*addr, &bound, &qad, granted)))
            .collect())
    }
    /// TLS authenticates an endpoint, not membership. Pass the connection to
    /// RouterReceiver::new with the independent current allow-list binding.
    ///
    /// Both modes run inbound handshakes CONCURRENTLY in a pool the host owns
    /// (at most `RelayConnectionSlots` in flight, each capped at
    /// `SidecarLocalHandshake`), so N stalled handshakes cost one cap, not N,
    /// and `deadline` bounds only this call's wait, never a handshake already
    /// started: handshakes still in flight when it passes carry over to the
    /// next call (REVIEW-laneLAN-r2 R2-1: callers accept in `OwnerPoll`
    /// windows and call again). The call returns the first completed
    /// connection, or `Authentication` for one refused/failed handshake
    /// (callers keep accepting). A connection that completed as `deadline`
    /// passed is not closed as late: this call returns the deadline error and
    /// hands the connection to the next call, because every caller either
    /// accepts again or closes the host, and the host's close ends it with the
    /// handshakes still in flight.
    ///
    /// Direct LAN: an inbound source outside the admission is ignored before
    /// any answer; an admitted source is pinned exactly before its handshake
    /// answers; the admitted connection is confined to admitted paths.
    /// LocalOnly admits whom it always did: any dialer on 127.0.0.1 (callers
    /// check the key).
    pub fn accept(&self, clock: &impl Clock, deadline: MonoInstant) -> Result<Connection, Error> {
        outside_runtime()?;
        let endpoint = self.endpoint()?;
        let wait = allowance(clock, deadline)?;
        let (connection, pin) = self.accept_pooled(endpoint, self.lan.as_ref(), wait)?;
        if let Err(error) = allowance(clock, deadline) {
            self.hand_on(connection, pin)?;
            return Err(error);
        }
        self.confine(&connection, pin)?;
        self.observe_path(&connection, "in");
        Ok(connection)
    }
    /// Keep a completed, admitted connection for the next [`Self::accept`].
    fn hand_on(&self, connection: Connection, pin: Option<Pin>) -> Result<(), Error> {
        let mut pool = self.handshakes.lock().map_err(|_| Error::Unavailable)?;
        pool.spawn_on(async move { Ok((connection, pin)) }, self.runtime.handle());
        Ok(())
    }
    /// A connection that completed after the caller's deadline: close it with
    /// `deadline` and put that close on the wire before returning (see
    /// [`flush_close`]); otherwise the peer may see a reset instead.
    fn close_late(&self, connection: Connection) -> Result<(), Error> {
        let (closed, remote) = (connection.weak_handle(), connection.remote_id());
        connection.close(0u32.into(), b"deadline");
        drop(connection);
        self.runtime
            .block_on(flush_close(self.endpoint()?, &closed, remote))
    }
    fn accept_pooled(
        &self,
        endpoint: &Endpoint,
        admission: Option<&Arc<Admission>>,
        wait: Duration,
    ) -> Result<(Connection, Option<Pin>), Error> {
        enum Next {
            Done(Option<Result<Handshake, tokio::task::JoinError>>),
            Arrived(Option<Box<iroh::endpoint::Incoming>>),
        }
        let handshake = control_handshake(self.cross.is_some())?;
        let slots = count(BudgetId::RelayConnectionSlots)?;
        let cross = self.cross.clone();
        let mut pool = self.handshakes.lock().map_err(|_| Error::Unavailable)?;
        self.runtime.block_on(async {
            tokio::time::timeout(wait, async {
                loop {
                    let next = tokio::select! {
                        biased;
                        done = pool.join_next(), if !pool.is_empty() => Next::Done(done),
                        incoming = endpoint.accept(), if pool.len() < slots => Next::Arrived(incoming.map(Box::new)),
                    };
                    let incoming = match next {
                        Next::Done(Some(Ok(result))) => return result,
                        // An aborted/panicked handshake task: one failed inbound.
                        Next::Done(Some(Err(_))) => return Err(Error::Authentication),
                        Next::Done(None) => continue,
                        Next::Arrived(None) => return Err(Error::Unavailable),
                        Next::Arrived(Some(incoming)) => *incoming,
                    };
                    let lan = match admission {
                        None => None,
                        Some(admission) => {
                            // An inbound source outside the admission is ignored before
                            // any handshake work or answer, whatever key it presents.
                            let source = match incoming.remote_addr() {
                                iroh::endpoint::IncomingAddr::Custom(custom) => {
                                    from_custom(&custom)
                                }
                                _ => None,
                            };
                            let Some(source) = source.filter(|s| admission.may_accept(*s)) else {
                                incoming.ignore();
                                return Err(Error::Authentication);
                            };
                            // Pinned BEFORE the handshake answers, released if it fails.
                            // Provisional: an unauthenticated source never takes (or
                            // blocks) an authenticated pin. A full provisional table is
                            // one refused inbound, never a reason to stop accepting.
                            match admission.pin_provisional([source]) {
                                Ok(pin) => Some((admission.clone(), source, pin)),
                                Err(_) => {
                                    incoming.ignore();
                                    return Err(Error::Authentication);
                                }
                            }
                        }
                    };
                    let cross = cross.clone();
                    pool.spawn(async move {
                        // A stalled handshake must not hold the caller's window.
                        // A failed one frees its receive-filter slot at once
                        // (B11; still counted against its host's share).
                        let connection = match tokio::time::timeout(handshake, incoming).await {
                            Ok(Ok(connection)) => connection,
                            Ok(Err(_)) | Err(_) => {
                                if let Some((_, _, pin)) = lan {
                                    pin.refuse();
                                }
                                return Err(Error::Authentication);
                            }
                        };
                        // Across networks: an unadmitted key is refused here and counted, never
                        // handed to the caller; without keys the caller verifies the key.
                        if let Some(gate) = cross {
                            if gate.admits_key(&connection.remote_id()) == Some(false) {
                                connection.close(LAN_KEY_REFUSED.into(), b"lan_key_refused");
                                gate.refuse_in();
                                return Err(Error::Authentication);
                            }
                            return Ok((connection, None));
                        }
                        // LocalOnly: the caller verifies the key.
                        let Some((admission, source, mut pin)) = lan else {
                            return Ok((connection, None));
                        };
                        match admission.admits_key(&connection.remote_id()) {
                            // Not an admitted peer key: refused here, unpinned
                            // at once (no close drain for an unauthenticated
                            // peer), its receive-filter slot freed for other
                            // hosts at once (B11), never handed to the caller.
                            Some(false) => {
                                connection.close(LAN_KEY_REFUSED.into(), b"lan_key_refused");
                                pin.refuse();
                                return Err(Error::Authentication);
                            }
                            Some(true) => {
                                if let Err(error) = pin.promote() {
                                    connection.close(0u32.into(), b"lan_pins_exhausted");
                                    return Err(error);
                                }
                                // B14: strictly after the key check, the
                                // admitted key's host is learned (own lane).
                                admission.learn(source, &connection.remote_id());
                            }
                            // No keys set: the caller verifies the key; the pin
                            // stays provisional (its own capacity).
                            None => pin.remember(),
                        }
                        Ok((connection, Some(pin)))
                    });
                }
            })
            .await
            .map_err(|_| Error::Expired)?
        })
    }
    /// Direct LAN: refuse a connection that already runs over a path outside
    /// the admitted destinations, then watch it for its lifetime and close it
    /// (`LAN_PATH_REFUSED`) once holepunching/multipath has opened one. A no-op
    /// in every other mode. Defence in depth behind the pre-send filter (which
    /// never sends to such a path). Each tick checks the open paths AND the
    /// endpoint's address book for the peer, which keeps every address a path
    /// was ever opened to (`Source::Connection`), so a short-lived extra path
    /// that iroh already closed as redundant is still caught. The watch holds
    /// only a weak handle and `pin` (the connection's exact destinations): it
    /// ends with the connection plus one `RouterClose` drain, so the close
    /// frames still reach the peer; close/drop of the host aborts any left.
    pub(crate) fn confine(&self, connection: &Connection, pin: Option<Pin>) -> Result<(), Error> {
        let Some(admission) = self.lan.clone() else {
            return Ok(());
        };
        if outside(connection, &admission) {
            connection.close(LAN_PATH_REFUSED.into(), b"lan_path_refused");
            return Err(Error::Authentication);
        }
        let tick = budget(BudgetId::SidecarReceivePollWait)?;
        let drain = budget(BudgetId::RouterClose)?;
        let weak = connection.weak_handle();
        let (endpoint, remote) = (self.endpoint()?.clone(), connection.remote_id());
        let guard = self.runtime.spawn(async move {
            let held = pin;
            let closed = weak.closed();
            tokio::pin!(closed);
            loop {
                tokio::select! {
                    _ = &mut closed => break,
                    () = tokio::time::sleep(tick) => {}
                }
                let known = endpoint.remote_info(remote).await;
                let Some(connection) = weak.upgrade() else {
                    break;
                };
                // A returning peer's earlier (admitted, since released)
                // addresses stay in the book; only never-admitted ones count.
                let seen = known.is_some_and(|info| {
                    info.addrs()
                        .any(|known| unseen_addr(known.addr(), &admission))
                });
                if seen || outside(&connection, &admission) {
                    connection.close(LAN_PATH_REFUSED.into(), b"lan_path_refused");
                    break;
                }
            }
            // Keep the exact pins while the close frames drain.
            tokio::time::sleep(drain).await;
            drop(held);
        });
        let mut guards = self.path_guards.lock().map_err(|_| Error::Unavailable)?;
        guards.retain(|g| !g.is_finished());
        guards.push(guard);
        Ok(())
    }
    /// Direct LAN: the iroh address to dial for a validated `peer` and the
    /// exact pins of its (signed) addresses, taken before the first packet.
    /// Other modes: the peer unchanged, no pins.
    pub(crate) fn dial_target(
        &self,
        peer: &EndpointAddr,
    ) -> Result<(EndpointAddr, Option<Pin>), Error> {
        if let Some(gate) = &self.cross {
            // Across networks: the signed addresses plus this host's own relay; a refused
            // address is counted (`refused_out`) and nothing is sent. With admitted keys set
            // (signed data), a key outside them is never dialled either (lane XB patch 2,
            // XB-10): no Initial, no path probe reaches its addresses. Without keys the caller
            // verifies the key it dials.
            if gate.admits_key(&peer.id) == Some(false) {
                gate.refuse_out();
                return Err(Error::Authentication);
            }
            if let Err(error) = self.validate_peer(peer) {
                gate.refuse_out();
                return Err(error);
            }
            return Ok((gate.target(peer)?, None));
        }
        self.validate_peer(peer)?;
        match &self.lan {
            None => Ok((peer.clone(), None)),
            Some(admission) => {
                let pin = admission.pin(peer.ip_addrs().copied())?;
                Ok((lan_target(peer), Some(pin)))
            }
        }
    }
    /// Direct LAN: replace the exact peer hosts inbound handshakes are
    /// accepted from (taken from signed pairing/offer data; each must be
    /// allow-listed). Across networks a peer's source is the relay or a punched path, never a
    /// LAN host: nothing to pin, `Ok` (its key is what [`Self::admit_peer_ids`] admits).
    /// `Unavailable` in every other mode.
    pub fn admit_peer_hosts(&self, hosts: &[IpAddr]) -> Result<(), Error> {
        if self.cross.is_some() {
            return Ok(());
        }
        self.lan
            .as_ref()
            .ok_or(Error::Unavailable)?
            .set_hosts(hosts)
    }
    /// Direct LAN: the exact peer keys accepted handshakes must present
    /// (taken from signed data, e.g. the verified route's owner endpoint). A
    /// handshake under any other key is refused by the host and unpinned at
    /// once; an admitted one moves to the authenticated pins. Without keys the
    /// caller verifies the key and accepted pins stay provisional.
    /// Across networks the same, without pins. `Unavailable` in every other mode.
    pub fn admit_peer_ids(&self, ids: &[iroh::EndpointId]) -> Result<(), Error> {
        if let Some(gate) = &self.cross {
            return gate.set_keys(ids);
        }
        self.lan.as_ref().ok_or(Error::Unavailable)?.set_keys(ids)
    }
    #[cfg(test)]
    pub(crate) fn lan_admission(&self) -> Option<&Arc<Admission>> {
        self.lan.as_ref()
    }
    /// Direct LAN: whether the pre-send filter currently admits `destination`
    /// (exact pins, or the range form). `None` in every other mode.
    pub fn lan_admits(&self, destination: SocketAddr) -> Option<bool> {
        self.lan.as_ref().map(|lan| lan.may_send(destination))
    }
    /// Direct LAN: the sources this host learned for its admitted peer keys
    /// (B14: each from a handshake verified under that key; one per key).
    /// Their hosts' Initials have their own provisional lane. `None` in every
    /// other mode.
    pub fn lan_learned_hosts(&self) -> Option<Vec<SocketAddr>> {
        self.lan.as_ref().map(|lan| lan.learned_hosts())
    }
    /// Direct LAN: datagrams the pre-send filter refused (never sent). Across networks: dials
    /// refused before any packet (a signed address outside the policy).
    pub fn lan_refused_datagrams(&self) -> Option<u64> {
        if let Some(gate) = &self.cross {
            return Some(gate.refused_out());
        }
        self.lan.as_ref().map(|lan| lan.refused())
    }
    /// Direct LAN: inbound datagrams the receive filter dropped before iroh. Across networks:
    /// handshakes refused under an unadmitted key.
    pub fn lan_dropped_datagrams(&self) -> Option<u64> {
        if let Some(gate) = &self.cross {
            return Some(gate.refused_in());
        }
        self.lan.as_ref().map(|lan| lan.dropped())
    }
    /// Direct LAN: remote addresses the current endpoint let reach iroh's
    /// address map since it was bound (receive-filter admissions plus fresh
    /// dial pins). iroh 1.0.2 never evicts that map (`mapped_addrs.rs:
    /// 348-359`), so this bounds its growth from above; see `recycle_lan`.
    pub fn lan_sources_admitted(&self) -> Option<u64> {
        self.lan.as_ref().map(|lan| lan.admitted())
    }
    /// Direct LAN: close the endpoint (with the same proof as [`Self::close`])
    /// and bind a fresh one on the SAME socket with the same key, channels,
    /// policy and signed peer hosts/keys. iroh 1.0.2 keeps every remote
    /// address an endpoint ever saw in a map it never evicts; a long-lived
    /// host that recycles at each route epoch boundary drops that map with
    /// the old endpoint, while its address (endpoint id + socket, i.e. the
    /// caller's signed carrier advert) stays exactly valid. Pins, path
    /// watches, handshakes in flight and the address watch end with the old
    /// endpoint (re-request `address_watch`). A failed close keeps the old
    /// endpoint for an explicit retry of this call or of `close`; a failed
    /// rebind leaves the host closed. Across networks: a no-op that keeps the endpoint (its
    /// address is the endpoint id, the bound socket and the relay, all unchanged; keeping it keeps
    /// iroh's per-peer paths, so the next epoch reuses a punched path: lane XB4).
    /// `Unavailable` in every other mode.
    pub fn recycle_lan(&mut self, clock: &impl Clock, deadline: MonoInstant) -> Result<(), Error> {
        if self.cross.is_some() {
            self.endpoint()?;
            return allowance(clock, deadline).map(|_| ());
        }
        let next = match (&self.lan, &self.reuse, &self.endpoint) {
            (Some(lan), Some(_), Some(_)) => lan.successor(std::time::Instant::now())?,
            // Not direct LAN, or already closed: never rebind a released host.
            _ => return Err(Error::Unavailable),
        };
        self.close(clock, deadline)?;
        let reuse = self.reuse.as_ref().ok_or(Error::Unavailable)?;
        let socket = reuse.socket.try_clone().map_err(|_| Error::Unavailable)?;
        let transport =
            Arc::new(LanTransport::on(socket, next.clone()).map_err(|_| Error::Unavailable)?);
        let (secret, alpns) = (reuse.secret.clone(), reuse.alpns.clone());
        let wait = allowance(clock, deadline)?;
        let endpoint = self.runtime.block_on(async {
            tokio::time::timeout(
                wait,
                lan_builder(transport)
                    .alpns(alpns)
                    .secret_key(secret)
                    .bind(),
            )
            .await
            .map_err(|_| Error::Expired)?
            .map_err(|_| Error::Unavailable)
        })?;
        self.handshakes = Mutex::new(tokio::task::JoinSet::new());
        self.lan = Some(next);
        self.endpoint = Some(endpoint);
        self.admitting = true;
        allowance(clock, deadline).map(|_| ())
    }
    pub fn connect(
        &self,
        peer: &EndpointAddr,
        clock: &impl Clock,
        deadline: MonoInstant,
    ) -> Result<Connection, Error> {
        self.connect_channel(peer, Channel::Router, clock, deadline)
    }
    pub fn connect_channel(
        &self,
        peer: &EndpointAddr,
        channel: Channel,
        clock: &impl Clock,
        deadline: MonoInstant,
    ) -> Result<Connection, Error> {
        self.connect_channel_until(peer, channel, clock, deadline, &|| false)
    }
    /// Cancellable dial/handshake, preserving its original absolute deadline.
    pub fn connect_channel_until(
        &self,
        peer: &EndpointAddr,
        channel: Channel,
        clock: &impl Clock,
        deadline: MonoInstant,
        stopped: &dyn Fn() -> bool,
    ) -> Result<Connection, Error> {
        outside_runtime()?;
        let endpoint = self.endpoint()?;
        // Reject the entire address, do not silently discard a relay/nonlocal
        // entry and accidentally authorize another network path. Direct LAN:
        // pin exactly the dialed (signed) addresses before the first packet.
        let (target, pin) = self.dial_target(peer)?;
        let wait = allowance(clock, deadline)?;
        let connection = self.runtime.block_on(async {
            interruptible(wait, stopped, async {
                endpoint
                    .connect(target, channel.alpn())
                    .await
                    .map_err(|_| Error::Unavailable)
            })
            .await
        })?;
        if let Err(error) = allowance(clock, deadline) {
            return Err(late_outcome(error, self.close_late(connection)));
        }
        self.confine(&connection, pin)?;
        self.observe_path(&connection, "out");
        Ok(connection)
    }
    /// Lane XB patch 3 (REVIEW XB2-04): a connection to another Mac (direct LAN, or across
    /// networks) is registered in [`crate::iroh_paths`] for its lifetime, by a weak handle (status
    /// samples its selected path whenever it is read), and its path type is logged:
    /// `l11_path:<relay|direct|none>` when first seen and on every change, `l11_path_closed:<last>`
    /// when it ends (or this host closes). Each route epoch is one connection, so a run's log has
    /// one line per epoch at least. Loopback-only hosts are not another Mac's: nothing is kept.
    /// Patch 4 (REVIEW XB3-03): also called by the owner's member-control dial
    /// (`iroh_control::NodeControlClient`), which connects through this host's endpoint itself.
    pub(crate) fn observe_path(&self, connection: &Connection, direction: &'static str) {
        let reach = match self.mode {
            NetworkMode::CrossNetwork => "relay",
            NetworkMode::DirectLan => "same_network",
            _ => return,
        };
        let Ok(tick) = budget(BudgetId::SidecarReceivePollWait) else {
            return;
        };
        let remote: String = connection
            .remote_id()
            .to_string()
            .chars()
            .take(12)
            .collect();
        let channel = crate::iroh_paths::channel_name(connection.alpn());
        let weak = connection.weak_handle();
        let sampled = weak.clone();
        let registration = crate::iroh_paths::register(
            remote.clone(),
            direction,
            reach,
            channel,
            Box::new(move || {
                sampled
                    .upgrade()
                    .filter(|c| c.close_reason().is_none())
                    .map(|c| crate::iroh_paths::path_kind(&c))
            }),
        );
        let mut log =
            crate::iroh_paths::PathLog::new(remote, direction, reach, channel, registration);
        let task = self.runtime.spawn(async move {
            let closed = weak.closed();
            tokio::pin!(closed);
            while let Some(now) = weak
                .upgrade()
                .filter(|c| c.close_reason().is_none())
                .map(|c| crate::iroh_paths::path_kind(&c))
            {
                log.saw(now);
                tokio::select! {
                    _ = &mut closed => break,
                    () = tokio::time::sleep(tick) => {}
                }
            }
            drop(log);
        });
        if let Ok(mut guards) = self.path_guards.lock() {
            guards.retain(|g| !g.is_finished());
            guards.push(task);
        }
    }
    /// Closes admission before waiting. A failed/expired close retains endpoint
    /// ownership for an explicit retry; it never grants native cleanup proof.
    /// Endpoint clones are deliberately not exposed by this host. The host
    /// runtime must also be drained/dropped for OS socket-release proof: Iroh's
    /// aborted internal tasks can retain sockets until the executor reaps them.
    pub fn close(&mut self, clock: &impl Clock, deadline: MonoInstant) -> Result<(), Error> {
        self.admitting = false;
        if let Some(lan) = &self.lan {
            // Pins stop releasing: the endpoint's close frames still go out.
            lan.close();
        }
        if let Some(task) = self.address_task.take() {
            task.abort();
        }
        self.abort_path_guards();
        outside_runtime()?;
        let wait = allowance(clock, deadline);
        if let Some(endpoint) = &self.endpoint {
            // Iroh 1.0.2 close is not cancel-safe: a second call returns early
            // while `is_closing`. Keep and rejoin the ORIGINAL close task.
            if self.closing.is_none() {
                let owned = endpoint.clone();
                self.closing = Some(self.runtime.spawn(async move {
                    owned.close().await;
                }));
            }
            let duration = wait.as_ref().copied().unwrap_or(Duration::ZERO);
            self.runtime.block_on(async {
                tokio::time::timeout(
                    duration,
                    self.closing.as_mut().ok_or(Error::CleanupUnproven)?,
                )
                .await
                .map_err(|_| Error::CleanupUnproven)?
                .map_err(|_| Error::CleanupUnproven)
            })?;
            if !endpoint.is_closed() {
                return Err(Error::CleanupUnproven);
            }
            self.closing.take();
            self.endpoint.take();
        }
        wait?;
        allowance(clock, deadline)?;
        Ok(())
    }
}

#[cfg(test)]
#[path = "iroh_relay_tests.rs"]
mod relay_tests;

#[cfg(test)]
#[path = "iroh_cross_tests.rs"]
mod cross_tests;

#[cfg(test)]
mod late_close_tests {
    use super::{Error, late_outcome};

    /// REVIEW-laneB20f (2): a connection that completed after the caller's deadline is closed and
    /// flushed before the call returns, and the call reports the DEADLINE error even when that close
    /// could not be proven; the cleanup failure must not replace it.
    #[test]
    fn a_late_connection_returns_the_deadline_error_even_when_its_close_is_unproven() {
        for deadline in [Error::Expired, Error::ClockDomain] {
            assert_eq!(late_outcome(deadline, Ok(())), deadline);
            assert_eq!(
                late_outcome(deadline, Err(Error::CleanupUnproven)),
                deadline,
                "the late close's cleanup error replaced the deadline error"
            );
        }
    }
}

#[cfg(test)]
mod control_handshake_tests {
    use super::*;
    #[test]
    fn a_cross_network_handshake_gets_the_relay_bound_and_the_lan_keeps_two_seconds() {
        assert_eq!(
            control_handshake(false).unwrap(),
            Duration::from_millis(2_000)
        );
        assert_eq!(
            control_handshake(true).unwrap(),
            Duration::from_millis(30_000)
        );
        // Within the control call ceiling, so a cross-network call is never refused as too long.
        assert!(
            control_handshake(true).unwrap()
                <= budget(BudgetId::NodeControlCallTimeoutCeiling).unwrap()
        );
    }
}
