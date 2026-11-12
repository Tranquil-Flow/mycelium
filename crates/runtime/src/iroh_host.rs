// SPDX-License-Identifier: AGPL-3.0-only
//! Explicit local-only Router endpoint ownership. No public relays, address
//! lookup, wildcard sockets or default N0 preset. This is the sidecar's local
//! mode, not a production internet-discovery policy.
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
use crate::iroh_lan_filter::{Admission, LanTransport, Pin, from_custom, to_custom};
use budgets::{BudgetId, Entries, MonoInstant};
use iroh::{
    Endpoint, EndpointAddr, RelayMode, TransportAddr,
    endpoint::{Connection, presets},
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
    /// The n0 production preset: relay, address lookup, every direct transport.
    Production,
    /// The n0 preset without any direct IP transport: relay only.
    ForceRelay,
    /// Explicitly configured direct LAN: ONE bound interface address, no relay,
    /// no address lookup, and peers only at allow-listed addresses
    /// ([`LanPolicy`]). Never a default; see [`LoopbackHost::bind_lan`].
    DirectLan,
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
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct LanPolicy {
    extra: Vec<IpAddr>,
    range: bool,
    hosts: Option<Vec<IpAddr>>,
}
impl LanPolicy {
    pub fn private_only() -> Self {
        Self::default()
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
        !never(ip) && (private_lan(ip) || self.extra.contains(&ip))
    }
    /// True in the range form; false = exact pins (the default).
    pub fn range_allowed(&self) -> bool {
        self.range
    }
    pub fn peer_hosts(&self) -> Option<&[IpAddr]> {
        self.hosts.as_deref()
    }
}
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
    pub const fn local_only(self) -> bool {
        matches!(self, Self::LocalOnly)
    }
}
/// Where the relay and address lookup of a network mode come from.
#[derive(Clone, Debug)]
pub enum Reach {
    /// The shipped n0 relays and address lookup.
    Shipped,
    /// One explicit relay and no address lookup: nothing leaves the host unless that relay is
    /// reachable. For tests that must not touch the public relays.
    Hermetic(iroh::RelayUrl),
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
    /// Direct LAN: inbound handshakes in flight, bounded and concurrent.
    handshakes: Mutex<tokio::task::JoinSet<Handshake>>,
    /// Bridges `Endpoint::watch_addr` into the watch channel the address gate reads.
    address_task: Option<tokio::task::JoinHandle<()>>,
    /// Direct LAN: one path watch per admitted connection (see `confine`).
    path_guards: Mutex<Vec<tokio::task::JoinHandle<()>>>,
    /// Direct LAN: what [`LoopbackHost::recycle_lan`] rebinds with.
    reuse: Option<Box<LanReuse>>,
}
/// The bound socket (a CLOEXEC duplicate; the transport owns the original)
/// and identity a direct-LAN endpoint is recycled with, so the address in the
/// caller's signed carrier advert stays exactly valid (B11).
struct LanReuse {
    socket: std::net::UdpSocket,
    secret: iroh::SecretKey,
    alpns: Vec<Vec<u8>>,
}
type Handshake = Result<(Connection, Pin), Error>;
/// The builder of a direct-LAN endpoint: no relay, no address lookup and NO
/// IP transport; the pre-send LAN transport owns the one bound socket (see
/// `iroh_lan_filter`).
fn lan_builder(transport: Arc<LanTransport>) -> iroh::endpoint::Builder {
    Endpoint::builder(presets::Minimal)
        .relay_mode(RelayMode::Disabled)
        .clear_address_lookup()
        .clear_ip_transports()
        .add_custom_transport(transport)
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
/// deadline, given that error and the result of closing the late connection.
fn late_outcome(deadline: Error, closed: Result<(), Error>) -> Error {
    match closed {
        Ok(()) => deadline,
        Err(cleanup) => cleanup,
    }
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
        outside_runtime()?;
        let wait = allowance(clock, deadline)?;
        let alpns: Vec<Vec<u8>> = channels.iter().map(|c| c.alpn().to_vec()).collect();
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
            let mut builder = match mode {
                NetworkMode::LocalOnly => Endpoint::builder(presets::Minimal)
                    .relay_mode(RelayMode::Disabled)
                    .clear_address_lookup()
                    .clear_ip_transports()
                    .bind_addr(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))
                    .map_err(|_| Error::Unavailable)?,
                NetworkMode::DirectLan => {
                    lan_builder(lan_transport.clone().ok_or(Error::Unavailable)?)
                }
                NetworkMode::Production | NetworkMode::ForceRelay => {
                    // The production relay transport; `ForceRelay` removes every direct IP
                    // transport (`sidecar.rs:846-852 @ f4e366e9`).
                    let mut builder = Endpoint::builder(presets::N0);
                    if mode == NetworkMode::ForceRelay {
                        builder = builder.clear_ip_transports();
                    }
                    if let Reach::Hermetic(url) = reach {
                        builder = builder
                            .clear_address_lookup()
                            .relay_mode(RelayMode::Custom(iroh::RelayMap::from(url.clone())));
                    }
                    builder
                }
            }
            .alpns(alpns);
            if let Some(secret) = secret {
                builder = builder.secret_key(secret);
            }
            tokio::time::timeout(wait, builder.bind())
                .await
                .map_err(|_| Error::Expired)?
                .map_err(|_| Error::Unavailable)
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
            _ => Ok(()),
        }
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
    /// TLS authenticates an endpoint, not membership. Pass the connection to
    /// RouterReceiver::new with the independent current allow-list binding.
    ///
    /// Direct LAN: an inbound source outside the admission is ignored before
    /// any answer; an admitted source is pinned exactly, then its handshake
    /// runs CONCURRENTLY with others (at most `RelayConnectionSlots` in flight,
    /// each capped at `SidecarLocalHandshake`), so N stalled handshakes cost one
    /// cap, not N. The call returns the first completed connection, or
    /// `Authentication` for one refused/failed handshake (callers keep
    /// accepting; handshakes still in flight carry over to the next call). The
    /// admitted connection is confined to admitted paths.
    pub fn accept(&self, clock: &impl Clock, deadline: MonoInstant) -> Result<Connection, Error> {
        outside_runtime()?;
        let endpoint = self.endpoint()?;
        let wait = allowance(clock, deadline)?;
        let (connection, pin) = match &self.lan {
            None => (
                self.runtime.block_on(async {
                    tokio::time::timeout(wait, async {
                        let incoming = endpoint.accept().await.ok_or(Error::Unavailable)?;
                        incoming.await.map_err(|_| Error::Authentication)
                    })
                    .await
                    .map_err(|_| Error::Expired)?
                })?,
                None,
            ),
            Some(admission) => {
                let (connection, pin) = self.accept_lan(endpoint, admission, wait)?;
                (connection, Some(pin))
            }
        };
        if let Err(error) = allowance(clock, deadline) {
            return Err(late_outcome(error, self.close_late(connection)));
        }
        self.confine(&connection, pin)?;
        Ok(connection)
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
    fn accept_lan(
        &self,
        endpoint: &Endpoint,
        admission: &Arc<Admission>,
        wait: Duration,
    ) -> Result<(Connection, Pin), Error> {
        enum Next {
            Done(Option<Result<Handshake, tokio::task::JoinError>>),
            Arrived(Option<Box<iroh::endpoint::Incoming>>),
        }
        let handshake = budget(BudgetId::SidecarLocalHandshake)?;
        let slots = count(BudgetId::RelayConnectionSlots)?;
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
                    // An inbound source outside the admission is ignored before
                    // any handshake work or answer, whatever key it presents.
                    let source = match incoming.remote_addr() {
                        iroh::endpoint::IncomingAddr::Custom(custom) => from_custom(&custom),
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
                    let mut pin = match admission.pin_provisional([source]) {
                        Ok(pin) => pin,
                        Err(_) => {
                            incoming.ignore();
                            return Err(Error::Authentication);
                        }
                    };
                    let admission = admission.clone();
                    pool.spawn(async move {
                        // A stalled handshake must not hold the caller's window.
                        // A failed one frees its receive-filter slot at once
                        // (B11; still counted against its host's share).
                        let connection = match tokio::time::timeout(handshake, incoming).await {
                            Ok(Ok(connection)) => connection,
                            Ok(Err(_)) | Err(_) => {
                                pin.refuse();
                                return Err(Error::Authentication);
                            }
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
                        Ok((connection, pin))
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
    /// allow-listed). `Unavailable` in every other mode.
    pub fn admit_peer_hosts(&self, hosts: &[IpAddr]) -> Result<(), Error> {
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
    /// `Unavailable` in every other mode.
    pub fn admit_peer_ids(&self, ids: &[iroh::EndpointId]) -> Result<(), Error> {
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
    /// Direct LAN: datagrams the pre-send filter refused (never sent).
    pub fn lan_refused_datagrams(&self) -> Option<u64> {
        self.lan.as_ref().map(|lan| lan.refused())
    }
    /// Direct LAN: inbound datagrams the receive filter dropped before iroh.
    pub fn lan_dropped_datagrams(&self) -> Option<u64> {
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
    /// rebind leaves the host closed. `Unavailable` in every other mode.
    pub fn recycle_lan(&mut self, clock: &impl Clock, deadline: MonoInstant) -> Result<(), Error> {
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
        outside_runtime()?;
        let endpoint = self.endpoint()?;
        // Reject the entire address, do not silently discard a relay/nonlocal
        // entry and accidentally authorize another network path. Direct LAN:
        // pin exactly the dialed (signed) addresses before the first packet.
        let (target, pin) = self.dial_target(peer)?;
        let wait = allowance(clock, deadline)?;
        let connection = self.runtime.block_on(async {
            tokio::time::timeout(wait, endpoint.connect(target, channel.alpn()))
                .await
                .map_err(|_| Error::Expired)?
                .map_err(|_| Error::Unavailable)
        })?;
        if let Err(error) = allowance(clock, deadline) {
            return Err(late_outcome(error, self.close_late(connection)));
        }
        self.confine(&connection, pin)?;
        Ok(connection)
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
