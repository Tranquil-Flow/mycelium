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
//! The public API has no path filter, so a direct-LAN host watches every
//! connection and closes it (`LAN_PATH_REFUSED`) as soon as any path's remote
//! address is outside the allow-list; see `confine`.
pub use crate::iroh_cleanup::{HostCloseReport, close_endpoint_and_native};
use budgets::{BudgetId, Entries, MonoInstant};
use iroh::{
    Endpoint, EndpointAddr, RelayMode, TransportAddr,
    endpoint::{Connection, presets},
};
use std::{
    net::{IpAddr, Ipv4Addr, SocketAddr, SocketAddrV4},
    sync::Mutex,
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
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct LanPolicy {
    extra: Vec<IpAddr>,
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
        Ok(Self { extra })
    }
    pub fn allows(&self, ip: IpAddr) -> bool {
        !never(ip) && (private_lan(ip) || self.extra.contains(&ip))
    }
}
/// Application close code of a direct-LAN connection that gained a path outside
/// the allow-list (reason `lan_path_refused`).
pub const LAN_PATH_REFUSED: u32 = 3;
fn budget(id: BudgetId) -> Result<Duration, Error> {
    Ok(Duration::from_millis(
        id.span(Entries::ZERO)
            .map_err(|_| Error::Unavailable)?
            .millis(),
    ))
}
/// A remote transport address that is not an allow-listed IP (a relay or
/// custom address is outside by definition).
fn outside_addr(addr: &TransportAddr, policy: &LanPolicy) -> bool {
    match addr {
        TransportAddr::Ip(addr) => !policy.allows(addr.ip()),
        _ => true,
    }
}
/// Any currently open path of `connection` outside the allow-list.
fn outside(connection: &Connection, policy: &LanPolicy) -> bool {
    connection
        .paths()
        .iter()
        .any(|path| outside_addr(path.remote_addr(), policy))
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
    /// The direct-LAN allow-list; `Some` exactly in [`NetworkMode::DirectLan`].
    lan: Option<LanPolicy>,
    /// Bridges `Endpoint::watch_addr` into the watch channel the address gate reads.
    address_task: Option<tokio::task::JoinHandle<()>>,
    /// Direct LAN: one path watch per admitted connection (see `confine`).
    path_guards: Mutex<Vec<tokio::task::JoinHandle<()>>>,
}
impl LoopbackHost<'_> {
    fn abort_path_guards(&self) {
        if let Ok(mut guards) = self.path_guards.lock() {
            for guard in guards.drain(..) {
                guard.abort();
            }
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
        let endpoint = runtime.block_on(async {
            let mut builder = match mode {
                NetworkMode::LocalOnly => Endpoint::builder(presets::Minimal)
                    .relay_mode(RelayMode::Disabled)
                    .clear_address_lookup()
                    .clear_ip_transports()
                    .bind_addr(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))
                    .map_err(|_| Error::Unavailable)?,
                NetworkMode::DirectLan => {
                    let (interface, _) = lan.as_ref().ok_or(Error::Unavailable)?;
                    Endpoint::builder(presets::Minimal)
                        .relay_mode(RelayMode::Disabled)
                        .clear_address_lookup()
                        .clear_ip_transports()
                        .bind_addr(SocketAddr::new(*interface, 0))
                        .map_err(|_| Error::Unavailable)?
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
            lan: lan.map(|(_, policy)| policy),
            address_task: None,
            path_guards: Mutex::new(Vec::new()),
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
            (NetworkMode::DirectLan, Some(policy)) => validate_lan_peer(peer, policy),
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
        if !self.mode.local_only() && self.mode != NetworkMode::DirectLan {
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
    /// Direct LAN: one inbound handshake gets at most `SidecarLocalHandshake`
    /// (a peer that starts a handshake and never finishes it is dropped as
    /// `Authentication`, so callers keep accepting within their own deadline),
    /// and the admitted connection is confined to allow-listed paths.
    pub fn accept(&self, clock: &impl Clock, deadline: MonoInstant) -> Result<Connection, Error> {
        outside_runtime()?;
        let endpoint = self.endpoint()?;
        let wait = allowance(clock, deadline)?;
        let handshake = budget(BudgetId::SidecarLocalHandshake)?;
        let connection = self.runtime.block_on(async {
            tokio::time::timeout(wait, async {
                let incoming = endpoint.accept().await.ok_or(Error::Unavailable)?;
                let Some(policy) = &self.lan else {
                    return incoming.await.map_err(|_| Error::Authentication);
                };
                // Direct LAN: an inbound path from outside the allow-list is
                // refused before any handshake work, whatever key it presents.
                let allowed = matches!(incoming.remote_addr(),
                    iroh::endpoint::IncomingAddr::Ip(addr) if policy.allows(addr.ip()));
                if !allowed {
                    incoming.refuse();
                    return Err(Error::Authentication);
                }
                // A stalled handshake must not hold the caller's window.
                tokio::time::timeout(handshake, incoming)
                    .await
                    .map_err(|_| Error::Authentication)?
                    .map_err(|_| Error::Authentication)
            })
            .await
            .map_err(|_| Error::Expired)?
        })?;
        if let Err(error) = allowance(clock, deadline) {
            connection.close(0u32.into(), b"deadline");
            return Err(error);
        }
        self.confine(&connection)?;
        Ok(connection)
    }
    /// Direct LAN: refuse a connection that already runs over a path outside
    /// the allow-list, then watch it for its lifetime and close it
    /// (`LAN_PATH_REFUSED`) once holepunching/multipath has opened one. A no-op
    /// in every other mode. Each tick checks the open paths AND the endpoint's
    /// address book for the peer, which keeps every address a path was ever
    /// opened to (`Source::Connection`), so a short-lived extra path that iroh
    /// already closed as redundant is still caught. The public API offers no
    /// pre-send filter: probes to a peer-advertised candidate can leave before
    /// the first tick (see the module note). The watch holds only a weak handle
    /// and ends with the connection; close/drop of the host aborts any left.
    fn confine(&self, connection: &Connection) -> Result<(), Error> {
        let Some(policy) = self.lan.clone() else {
            return Ok(());
        };
        if outside(connection, &policy) {
            connection.close(LAN_PATH_REFUSED.into(), b"lan_path_refused");
            return Err(Error::Authentication);
        }
        let tick = budget(BudgetId::SidecarReceivePollWait)?;
        let weak = connection.weak_handle();
        let (endpoint, remote) = (self.endpoint()?.clone(), connection.remote_id());
        let guard = self.runtime.spawn(async move {
            let closed = weak.closed();
            tokio::pin!(closed);
            loop {
                tokio::select! {
                    _ = &mut closed => return,
                    () = tokio::time::sleep(tick) => {}
                }
                let known = endpoint.remote_info(remote).await;
                let Some(connection) = weak.upgrade() else {
                    return;
                };
                let seen = known.is_some_and(|info| {
                    info.addrs()
                        .any(|known| outside_addr(known.addr(), &policy))
                });
                if seen || outside(&connection, &policy) {
                    connection.close(LAN_PATH_REFUSED.into(), b"lan_path_refused");
                    return;
                }
            }
        });
        let mut guards = self.path_guards.lock().map_err(|_| Error::Unavailable)?;
        guards.retain(|g| !g.is_finished());
        guards.push(guard);
        Ok(())
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
        // entry and accidentally authorize another network path.
        self.validate_peer(peer)?;
        let wait = allowance(clock, deadline)?;
        let connection = self.runtime.block_on(async {
            tokio::time::timeout(wait, endpoint.connect(peer.clone(), channel.alpn()))
                .await
                .map_err(|_| Error::Expired)?
                .map_err(|_| Error::Unavailable)
        })?;
        if let Err(error) = allowance(clock, deadline) {
            connection.close(0u32.into(), b"deadline");
            return Err(error);
        }
        self.confine(&connection)?;
        Ok(connection)
    }
    /// Closes admission before waiting. A failed/expired close retains endpoint
    /// ownership for an explicit retry; it never grants native cleanup proof.
    /// Endpoint clones are deliberately not exposed by this host. The host
    /// runtime must also be drained/dropped for OS socket-release proof: Iroh's
    /// aborted internal tasks can retain sockets until the executor reaps them.
    pub fn close(&mut self, clock: &impl Clock, deadline: MonoInstant) -> Result<(), Error> {
        self.admitting = false;
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
