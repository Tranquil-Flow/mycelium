// SPDX-License-Identifier: AGPL-3.0-only
//! Pre-send destination filter of the direct-LAN carrier (lane B9).
//!
//! Iroh 1.0.2 always runs QUIC multipath + n0 NAT traversal, so an
//! authenticated peer can make this endpoint probe any address it advertises
//! (`ADD_ADDRESS` / `REACH_OUT`, see the `iroh_host` module note). The public
//! builder has no path filter, but every datagram an endpoint sends goes
//! through one of its transports (`iroh-1.0.2/src/socket/transports.rs:
//! 1157-1238`), and a destination no transport accepts is black-holed there.
//! A direct-LAN endpoint therefore has NO IP transport at all
//! (`clear_ip_transports`, no `bind_addr`) and exactly one custom transport
//! (`unstable-custom-transports`, `endpoint.rs:812-815`): [`LanTransport`]
//! owns the one bound UDP socket and [`LanSender::poll_send`] checks every
//! destination against [`Admission::may_send`] before the `sendto`. A refused
//! destination is dropped (counted) and nothing leaves the socket; QUIC sees
//! loss. An IP candidate a peer advertises has no transport and is
//! black-holed by iroh itself. Receiving is not filtered: no inbound datagram
//! can make this endpoint send anywhere [`Admission`] does not admit.
//!
//! Admission. Range form ([`LanPolicy::range_form`]): any allow-listed address.
//! Exact pins (the default): only socket addresses this host dialed (the
//! caller's signed peer addresses) or accepted an inbound handshake from, each
//! held by a [`Pin`] for the connection's lifetime plus a close drain.
use crate::iroh_host::LanPolicy;
use iroh::endpoint::transports::{
    CustomEndpoint, CustomSender, CustomTransport, RecvInfo, Transmit,
};
use iroh_base::CustomAddr;
use std::{
    collections::{BTreeSet, HashMap, HashSet, VecDeque},
    io,
    net::{IpAddr, SocketAddr},
    sync::{
        Arc, Mutex, RwLock,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    task::{Context, Poll},
};
use transport::Error;

/// Transport id of [`LanTransport`] addresses. Local to one endpoint: custom
/// addresses are never published (no address lookup, no relay) and never on
/// the wire, where datagrams are plain UDP QUIC packets.
pub(crate) const LAN_TRANSPORT_ID: u64 = 0x6d79_6c6e;

fn canonical(addr: SocketAddr) -> SocketAddr {
    SocketAddr::new(addr.ip().to_canonical(), addr.port())
}
/// The transport address iroh uses for the real UDP peer `addr`.
pub(crate) fn to_custom(addr: SocketAddr) -> CustomAddr {
    CustomAddr::from_parts(LAN_TRANSPORT_ID, canonical(addr).to_string().as_bytes())
}
/// The UDP peer behind a [`LanTransport`] address; `None` for any other.
pub(crate) fn from_custom(addr: &CustomAddr) -> Option<SocketAddr> {
    if addr.id() != LAN_TRANSPORT_ID {
        return None;
    }
    std::str::from_utf8(addr.data())
        .ok()?
        .parse::<SocketAddr>()
        .ok()
        .map(canonical)
}

/// Which destinations a direct-LAN endpoint may send to and which sources it
/// may accept handshakes from. Shared by the host and its transport.
#[derive(Debug)]
pub(crate) struct Admission {
    policy: LanPolicy,
    /// Exact pins: destination -> live holders.
    pins: RwLock<HashMap<SocketAddr, usize>>,
    capacity: usize,
    /// Every destination ever pinned (oldest dropped past `remembered`): the
    /// endpoint's address book keeps a returning peer's earlier, released
    /// addresses, which were admitted then and are no evidence of a leak.
    history: Mutex<(HashSet<SocketAddr>, VecDeque<SocketAddr>)>,
    remembered: usize,
    /// Exact peer hosts from signed data: when set, inbound handshakes only
    /// from these IPs (dialed peers are pinned exactly anyway).
    hosts: RwLock<Option<BTreeSet<IpAddr>>>,
    /// Set once the host closes: pins stop releasing so the endpoint's own
    /// CONNECTION_CLOSE frames still reach admitted peers.
    closing: AtomicBool,
    refused: AtomicU64,
    /// Fault injection: the path monitor treats every path as not admitted
    /// (the sender is unaffected, so close frames still go out). Proves that a
    /// connection is watched at all and that the monitor alone closes it.
    #[cfg(test)]
    pub(crate) distrust_for_test: AtomicBool,
}
impl Admission {
    pub(crate) fn new(policy: LanPolicy, capacity: usize, remembered: usize) -> Arc<Self> {
        let hosts = policy.peer_hosts().map(|h| h.iter().copied().collect());
        Arc::new(Self {
            policy,
            pins: RwLock::new(HashMap::new()),
            capacity,
            history: Mutex::new((HashSet::new(), VecDeque::new())),
            remembered,
            hosts: RwLock::new(hosts),
            closing: AtomicBool::new(false),
            refused: AtomicU64::new(0),
            #[cfg(test)]
            distrust_for_test: AtomicBool::new(false),
        })
    }
    pub(crate) fn policy(&self) -> &LanPolicy {
        &self.policy
    }
    /// The pre-send check, evaluated for every datagram.
    pub(crate) fn may_send(&self, dst: SocketAddr) -> bool {
        let dst = canonical(dst);
        if !self.policy.allows(dst.ip()) {
            return false;
        }
        if self.policy.range_allowed() {
            return true;
        }
        self.pins
            .read()
            .map(|pins| pins.contains_key(&dst))
            .unwrap_or(false)
    }
    /// The monitor's view of an open path (= the pre-send check).
    pub(crate) fn admits_path(&self, addr: SocketAddr) -> bool {
        #[cfg(test)]
        if self.distrust_for_test.load(Ordering::SeqCst) {
            return false;
        }
        self.may_send(addr)
    }
    /// The monitor's view of an address-book entry: admitted now, or pinned
    /// earlier on this host (a returning peer's released address).
    pub(crate) fn admits_seen(&self, addr: SocketAddr) -> bool {
        #[cfg(test)]
        if self.distrust_for_test.load(Ordering::SeqCst) {
            return false;
        }
        let addr = canonical(addr);
        self.may_send(addr)
            || self
                .history
                .lock()
                .map(|history| history.0.contains(&addr))
                .unwrap_or(false)
    }
    /// Inbound handshake admission (before any answer is sent).
    pub(crate) fn may_accept(&self, src: SocketAddr) -> bool {
        let src = canonical(src);
        if !self.policy.allows(src.ip()) {
            return false;
        }
        match self.hosts.read() {
            Ok(hosts) => hosts.as_ref().is_none_or(|h| h.contains(&src.ip())),
            Err(_) => false,
        }
    }
    /// Replace the exact peer-host pins (each must be allow-listed).
    pub(crate) fn set_hosts(&self, hosts: &[IpAddr]) -> Result<(), Error> {
        if hosts.is_empty() || hosts.iter().any(|ip| !self.policy.allows(*ip)) {
            return Err(Error::Authentication);
        }
        let mut current = self.hosts.write().map_err(|_| Error::Unavailable)?;
        *current = Some(hosts.iter().map(|ip| ip.to_canonical()).collect());
        Ok(())
    }
    /// Admit exactly `addrs` as destinations until the returned [`Pin`] drops.
    /// Fails closed once `capacity` distinct destinations are pinned.
    pub(crate) fn pin(
        self: &Arc<Self>,
        addrs: impl IntoIterator<Item = SocketAddr>,
    ) -> Result<Pin, Error> {
        let addrs: Vec<SocketAddr> = addrs.into_iter().map(canonical).collect();
        if addrs.is_empty() || addrs.iter().any(|a| !self.policy.allows(a.ip())) {
            return Err(Error::Authentication);
        }
        let mut pins = self.pins.write().map_err(|_| Error::Unavailable)?;
        let fresh = addrs.iter().filter(|a| !pins.contains_key(*a)).count();
        if pins.len() + fresh > self.capacity {
            return Err(Error::Unavailable);
        }
        for addr in &addrs {
            *pins.entry(*addr).or_insert(0) += 1;
        }
        drop(pins);
        let mut history = self.history.lock().map_err(|_| Error::Unavailable)?;
        let (seen, order) = &mut *history;
        for addr in &addrs {
            if seen.insert(*addr) {
                order.push_back(*addr);
            }
        }
        while order.len() > self.remembered {
            if let Some(oldest) = order.pop_front() {
                seen.remove(&oldest);
            }
        }
        Ok(Pin {
            admission: self.clone(),
            addrs,
        })
    }
    pub(crate) fn close(&self) {
        self.closing.store(true, Ordering::SeqCst);
    }
    /// Datagrams the filter refused to send (never left the socket).
    pub(crate) fn refused(&self) -> u64 {
        self.refused.load(Ordering::SeqCst)
    }
}
/// Holds exact destination pins; releases them on drop (not while closing).
#[derive(Debug)]
pub(crate) struct Pin {
    admission: Arc<Admission>,
    addrs: Vec<SocketAddr>,
}
impl Drop for Pin {
    fn drop(&mut self) {
        if self.admission.closing.load(Ordering::SeqCst) {
            return;
        }
        let Ok(mut pins) = self.admission.pins.write() else {
            // A poisoned table fails closed for every later send (may_send
            // reads `false`), so the pins are never silently widened.
            return;
        };
        for addr in &self.addrs {
            if let Some(count) = pins.get_mut(addr) {
                *count -= 1;
                if *count == 0 {
                    pins.remove(addr);
                }
            }
        }
    }
}

/// Factory iroh calls once from `Builder::bind`.
#[derive(Debug)]
pub(crate) struct LanTransport {
    socket: Mutex<Option<std::net::UdpSocket>>,
    local: SocketAddr,
    admission: Arc<Admission>,
}
impl LanTransport {
    /// Bind exactly `interface` (port 0) now, so the address is known before
    /// iroh builds the endpoint.
    pub(crate) fn bind(interface: IpAddr, admission: Arc<Admission>) -> io::Result<Self> {
        let socket = std::net::UdpSocket::bind(SocketAddr::new(interface, 0))?;
        socket.set_nonblocking(true)?;
        let local = socket.local_addr()?;
        Ok(Self {
            socket: Mutex::new(Some(socket)),
            local,
            admission,
        })
    }
    pub(crate) fn local(&self) -> SocketAddr {
        self.local
    }
}
impl CustomTransport for LanTransport {
    fn bind(&self) -> io::Result<Box<dyn CustomEndpoint>> {
        let socket = self
            .socket
            .lock()
            .map_err(|_| io::Error::other("lan_transport_poisoned"))?
            .take()
            .ok_or_else(|| io::Error::other("lan_transport_already_bound"))?;
        let socket = Arc::new(tokio::net::UdpSocket::from_std(socket)?);
        let local = to_custom(self.local);
        Ok(Box::new(LanEndpoint {
            socket,
            watch: n0_watcher::Watchable::new(vec![local.clone()]),
            local,
            admission: self.admission.clone(),
        }))
    }
}
#[derive(Debug)]
struct LanEndpoint {
    socket: Arc<tokio::net::UdpSocket>,
    watch: n0_watcher::Watchable<Vec<CustomAddr>>,
    local: CustomAddr,
    admission: Arc<Admission>,
}
impl CustomEndpoint for LanEndpoint {
    fn watch_local_addrs(&self) -> n0_watcher::Direct<Vec<CustomAddr>> {
        self.watch.watch()
    }
    fn create_sender(&self) -> Arc<dyn CustomSender> {
        Arc::new(LanSender {
            socket: self.socket.clone(),
            admission: self.admission.clone(),
        })
    }
    fn poll_recv(
        &mut self,
        cx: &mut Context,
        bufs: &mut [io::IoSliceMut<'_>],
        metas: &mut [noq_udp::RecvMeta],
        recv_infos: &mut [RecvInfo],
    ) -> Poll<io::Result<usize>> {
        let slots = bufs.len().min(metas.len()).min(recv_infos.len());
        let mut filled = 0;
        while filled < slots {
            let mut read = tokio::io::ReadBuf::new(&mut bufs[filled][..]);
            match self.socket.poll_recv_from(cx, &mut read) {
                Poll::Pending => break,
                Poll::Ready(Err(error)) if filled == 0 => return Poll::Ready(Err(error)),
                // The datagrams already filled are returned; the error recurs
                // on the next poll if it persists.
                Poll::Ready(Err(_)) => break,
                Poll::Ready(Ok(source)) => {
                    let len = read.filled().len();
                    let meta = &mut metas[filled];
                    meta.len = len;
                    meta.stride = len;
                    meta.ecn = None;
                    meta.dst_ip = None;
                    recv_infos[filled] = RecvInfo::new(to_custom(source), Some(self.local.clone()));
                    filled += 1;
                }
            }
        }
        if filled == 0 {
            Poll::Pending
        } else {
            Poll::Ready(Ok(filled))
        }
    }
}
#[derive(Debug)]
struct LanSender {
    socket: Arc<tokio::net::UdpSocket>,
    admission: Arc<Admission>,
}
impl CustomSender for LanSender {
    fn is_valid_send_addr(&self, addr: &CustomAddr) -> bool {
        addr.id() == LAN_TRANSPORT_ID
    }
    fn poll_send(
        &self,
        cx: &mut Context,
        dst: &CustomAddr,
        _src: Option<&CustomAddr>,
        transmit: &Transmit<'_>,
    ) -> Poll<io::Result<()>> {
        let Some(dst) = from_custom(dst) else {
            return Poll::Ready(Err(io::Error::other("lan_custom_addr_invalid")));
        };
        if !self.admission.may_send(dst) {
            // The pre-send filter: nothing reaches the socket.
            self.admission.refused.fetch_add(1, Ordering::SeqCst);
            return Poll::Ready(Ok(()));
        }
        let size = transmit
            .segment_size
            .unwrap_or(transmit.contents.len())
            .max(1);
        for (index, datagram) in transmit.contents.chunks(size).enumerate() {
            match self.socket.poll_send_to(cx, datagram, dst) {
                Poll::Ready(Ok(_sent)) => {}
                Poll::Ready(Err(error)) => return Poll::Ready(Err(error)),
                Poll::Pending if index == 0 => return Poll::Pending,
                // Later segments of a partly sent batch are lost; QUIC recovers.
                Poll::Pending => return Poll::Ready(Ok(())),
            }
        }
        Poll::Ready(Ok(()))
    }
}

#[cfg(all(test, unix))]
#[path = "iroh_lan_filter_tests.rs"]
mod tests;
