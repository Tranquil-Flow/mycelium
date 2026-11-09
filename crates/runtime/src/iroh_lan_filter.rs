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
//! black-holed by iroh itself.
//!
//! Receive filter (B9f, REVIEW-laneB9 F1). iroh maps the source of every
//! datagram a custom transport hands up (`socket.rs:654-660`) into an address
//! map that never evicts (`mapped_addrs.rs:348-359`), so [`LanEndpoint::
//! poll_recv`] drops a datagram BEFORE iroh sees it unless its source is
//! allow-listed and either pinned, or provisionally admitted: a QUIC Initial
//! from a source [`Admission::may_accept`] admits, held in a fixed-size table
//! (`RelayConnectionSlots` entries, each for one `SidecarLocalHandshake`) that
//! fails closed when full. New sources therefore reach iroh at most at that
//! table's rate; iroh's map itself still has no eviction (upstream).
//!
//! Per-host share (B11, REVIEW-laneB9f (1)). The table is keyed by socket
//! address, so one LAN host sending Initials from many ports could hold every
//! slot for a whole handshake budget. A source IP may hold at most a quarter of
//! the table ([`PROVISIONAL_SHARES`]), counting its live entries AND its held
//! provisional pins. A handshake the host refuses (an unadmitted key, or a
//! failed handshake: [`Pin::refuse`]) frees its slot for other hosts at once,
//! but still counts against its own host's share until the entry would have
//! expired, so being refused never lets a host cycle ports faster. At most
//! `RelayConnectionSlots` refused entries are kept, so new sources still reach
//! iroh at most at twice the table size per handshake budget.
//!
//! Admission. Range form ([`LanPolicy::range_form`]): any allow-listed address.
//! Exact pins (the default): only socket addresses this host dialed (the
//! caller's signed peer addresses) or accepted an inbound handshake from, each
//! held by a [`Pin`] for the connection's lifetime plus a close drain. Pins are
//! in two classes (B9f, F2): AUTHENTICATED (dialed signed addresses, and
//! accepts whose key the host verified against [`Admission::set_keys`]) and
//! PROVISIONAL (accept side before or without that verification), each with
//! its own capacity, so unauthenticated handshakes can never exhaust or evict
//! the pins of authenticated peers.
use crate::iroh_host::LanPolicy;
use budgets::{BudgetId, Entries};
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
    time::{Duration, Instant},
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

/// Smallest UDP datagram that may carry a client's QUIC Initial (RFC 9000
/// 14.1; noq drops shorter ones, `noq-proto-1.3.0/src/endpoint.rs:452`).
const QUIC_MIN_INITIAL: usize = 1200;
/// Whether `datagram` can open a QUIC connection at a server: a long-header
/// Initial (type 0b00) of a version noq supports (`noq-proto-1.3.0/src/
/// lib.rs:173-181`), at least [`QUIC_MIN_INITIAL`] bytes. Every other first
/// datagram from an unknown source only earns a stateless reset or version
/// negotiation, which the pre-send filter would refuse anyway.
fn quic_initial(datagram: &[u8]) -> bool {
    let Some((&first, rest)) = datagram.split_first() else {
        return false;
    };
    let Some(version) = rest.get(..4) else {
        return false;
    };
    let version = u32::from_be_bytes([version[0], version[1], version[2], version[3]]);
    datagram.len() >= QUIC_MIN_INITIAL
        && first & 0x80 != 0
        && (first >> 4) & 0x03 == 0
        && matches!(version, 0x0000_0001 | 0xff00_001d..=0xff00_0022)
}
/// Live pin holders per destination, by class (see the module note).
#[derive(Debug, Default)]
struct PinTable {
    authenticated: HashMap<SocketAddr, usize>,
    provisional: HashMap<SocketAddr, usize>,
}
impl PinTable {
    fn contains(&self, addr: &SocketAddr) -> bool {
        self.authenticated.contains_key(addr) || self.provisional.contains_key(addr)
    }
    fn class(&mut self, class: PinClass) -> &mut HashMap<SocketAddr, usize> {
        match class {
            PinClass::Authenticated => &mut self.authenticated,
            PinClass::Provisional => &mut self.provisional,
        }
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum PinClass {
    Authenticated,
    Provisional,
}
fn slots() -> Option<usize> {
    usize::try_from(BudgetId::RelayConnectionSlots.count().ok()?.get()).ok()
}
/// One source host may hold `1 / PROVISIONAL_SHARES` of the provisional
/// table (at least one slot when the table has any): with a quarter each, a
/// flooding host always leaves room for three others.
const PROVISIONAL_SHARES: usize = 4;
fn host_share(capacity: usize) -> usize {
    if capacity == 0 {
        0
    } else {
        (capacity / PROVISIONAL_SHARES).max(1)
    }
}
/// A receive-filter entry: an unpinned source admitted for one QUIC Initial.
#[derive(Clone, Copy, Debug)]
struct Inbound {
    until: Instant,
    /// Its handshake was refused: no longer admits datagrams nor takes a slot,
    /// but counts against its host's share until `until`.
    refused: bool,
}
fn handshake() -> Option<Duration> {
    let span = BudgetId::SidecarLocalHandshake.span(Entries::ZERO).ok()?;
    Some(Duration::from_millis(span.millis()))
}
/// Which destinations a direct-LAN endpoint may send to and which sources it
/// may accept handshakes from. Shared by the host and its transport.
#[derive(Debug)]
pub(crate) struct Admission {
    policy: LanPolicy,
    /// Exact pins: destination -> live holders, by class.
    pins: RwLock<PinTable>,
    /// Distinct authenticated destinations (dialed / key-verified).
    capacity: usize,
    /// Distinct provisional (accept-side, unverified) sources:
    /// `RelayConnectionSlots`, the concurrent handshake bound.
    provisional_capacity: usize,
    /// Receive filter: unpinned sources admitted for one QUIC Initial, until
    /// the instant each entry expires. At most `provisional_capacity` live
    /// (unrefused) entries plus as many refused ones.
    inbound: Mutex<HashMap<SocketAddr, Inbound>>,
    inbound_ttl: Duration,
    /// Live entries plus held provisional pins one source IP may have.
    host_share: usize,
    /// Every destination ever pinned (oldest dropped past `remembered`): the
    /// endpoint's address book keeps a returning peer's earlier, released
    /// addresses, which were admitted then and are no evidence of a leak.
    history: Mutex<(HashSet<SocketAddr>, VecDeque<SocketAddr>)>,
    remembered: usize,
    /// Exact peer hosts from signed data: when set, inbound handshakes only
    /// from these IPs (dialed peers are pinned exactly anyway).
    hosts: RwLock<Option<BTreeSet<IpAddr>>>,
    /// Exact peer keys from signed data: when set, an accepted handshake under
    /// any other key is refused by the host and unpinned at once.
    keys: RwLock<Option<BTreeSet<iroh::EndpointId>>>,
    /// Set once the host closes: pins stop releasing so the endpoint's own
    /// CONNECTION_CLOSE frames still reach admitted peers.
    closing: AtomicBool,
    refused: AtomicU64,
    /// Inbound datagrams the receive filter dropped (never reached iroh).
    dropped: AtomicU64,
    /// Remote addresses this admission let reach iroh's never-evicting
    /// address map (receive-filter entries + fresh authenticated pins): an
    /// upper bound on that map's growth for the endpoint it serves (B11).
    admitted: AtomicU64,
    /// Fault injection: the path monitor treats every path as not admitted
    /// (the sender is unaffected, so close frames still go out). Proves that a
    /// connection is watched at all and that the monitor alone closes it.
    #[cfg(test)]
    pub(crate) distrust_for_test: AtomicBool,
}
impl Admission {
    /// `capacity` authenticated destinations, `remembered` history entries.
    /// The provisional and receive-filter bounds come from the budget table;
    /// an unreadable budget leaves them at zero, i.e. closed (no unpinned
    /// source and no accept is ever admitted).
    pub(crate) fn new(policy: LanPolicy, capacity: usize, remembered: usize) -> Arc<Self> {
        let hosts = policy.peer_hosts().map(|h| h.iter().copied().collect());
        let provisional_capacity = slots().unwrap_or(0);
        Arc::new(Self {
            policy,
            pins: RwLock::new(PinTable::default()),
            capacity,
            provisional_capacity,
            inbound: Mutex::new(HashMap::new()),
            inbound_ttl: handshake().unwrap_or(Duration::ZERO),
            host_share: host_share(provisional_capacity),
            history: Mutex::new((HashSet::new(), VecDeque::new())),
            remembered,
            hosts: RwLock::new(hosts),
            keys: RwLock::new(None),
            closing: AtomicBool::new(false),
            refused: AtomicU64::new(0),
            dropped: AtomicU64::new(0),
            admitted: AtomicU64::new(0),
            #[cfg(test)]
            distrust_for_test: AtomicBool::new(false),
        })
    }
    pub(crate) fn policy(&self) -> &LanPolicy {
        &self.policy
    }
    /// A fresh admission for a recycled endpoint (B11): the same policy,
    /// bounds and signed peer hosts/keys; no pins, history, receive-filter
    /// entries or counters (they described the closed endpoint).
    pub(crate) fn successor(&self) -> Result<Arc<Self>, Error> {
        let next = Self::new(self.policy.clone(), self.capacity, self.remembered);
        let hosts = self.hosts.read().map_err(|_| Error::Unavailable)?.clone();
        let keys = self.keys.read().map_err(|_| Error::Unavailable)?.clone();
        *next.hosts.write().map_err(|_| Error::Unavailable)? = hosts;
        *next.keys.write().map_err(|_| Error::Unavailable)? = keys;
        Ok(next)
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
            .map(|pins| pins.contains(&dst))
            .unwrap_or(false)
    }
    /// The receive filter, evaluated for every inbound datagram BEFORE iroh
    /// sees (and permanently maps) its source; see the module note.
    pub(crate) fn admit_inbound(&self, src: SocketAddr, datagram: &[u8], now: Instant) -> bool {
        let src = canonical(src);
        if !self.policy.allows(src.ip()) {
            return false;
        }
        // The source's host's held provisional pins count against its share.
        let held: HashSet<SocketAddr> = match self.pins.read() {
            Ok(pins) if pins.contains(&src) => return true,
            Ok(pins) => pins
                .provisional
                .keys()
                .filter(|a| a.ip() == src.ip())
                .copied()
                .collect(),
            // A poisoned pin table admits no send, so nothing is worth taking.
            Err(_) => return false,
        };
        let Ok(mut inbound) = self.inbound.lock() else {
            return false;
        };
        if inbound
            .get(&src)
            .is_some_and(|entry| !entry.refused && now < entry.until)
        {
            return true;
        }
        if !quic_initial(datagram) || !self.may_accept(src) {
            return false;
        }
        let taken = |inbound: &HashMap<SocketAddr, Inbound>| {
            inbound
                .values()
                .filter(|e| !e.refused && now < e.until)
                .count()
        };
        if inbound.len() >= self.provisional_capacity {
            inbound.retain(|_, entry| now < entry.until);
        }
        let Some(until) = now.checked_add(self.inbound_ttl) else {
            return false;
        };
        let mut host = held;
        host.extend(
            inbound
                .iter()
                .filter(|(a, e)| a.ip() == src.ip() && now < e.until)
                .map(|(a, _)| *a),
        );
        // A refused entry of `src` itself still counts (it is overwritten,
        // never added to): refusal gives no fresh slot to its own host.
        if host.len() >= self.host_share {
            // This host already holds its share (live entries, refused ones
            // still in their window, held handshakes): fail closed for it only.
            return false;
        }
        if taken(&inbound) >= self.provisional_capacity {
            // Full of live provisional sources: fail closed (the peer's QUIC
            // retransmits its Initial once an entry expires or is refused).
            return false;
        }
        inbound.insert(
            src,
            Inbound {
                until,
                refused: false,
            },
        );
        self.admitted.fetch_add(1, Ordering::SeqCst);
        true
    }
    /// A provisional handshake from `addrs` was refused: their entries stop
    /// admitting and free their slots now, but keep counting against their
    /// host's share until they expire. Past `provisional_capacity` refused
    /// entries a refusal frees nothing early (the bound on what reaches iroh).
    fn refuse_inbound(&self, addrs: &[SocketAddr], now: Instant) {
        let Ok(mut inbound) = self.inbound.lock() else {
            return;
        };
        inbound.retain(|_, entry| now < entry.until);
        for addr in addrs {
            let refused = inbound.values().filter(|e| e.refused).count();
            if let Some(entry) = inbound.get_mut(addr)
                && !entry.refused
                && refused < self.provisional_capacity
            {
                entry.refused = true;
            }
        }
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
    /// Replace the exact peer keys accepted handshakes must present (taken
    /// from signed data; at most `capacity`).
    pub(crate) fn set_keys(&self, keys: &[iroh::EndpointId]) -> Result<(), Error> {
        if keys.is_empty() || keys.len() > self.capacity {
            return Err(Error::Authentication);
        }
        let mut current = self.keys.write().map_err(|_| Error::Unavailable)?;
        *current = Some(keys.iter().copied().collect());
        Ok(())
    }
    /// `None` when no keys are set; otherwise whether `key` is one of them.
    /// A poisoned key table admits nothing.
    pub(crate) fn admits_key(&self, key: &iroh::EndpointId) -> Option<bool> {
        match self.keys.read() {
            Ok(keys) => keys.as_ref().map(|k| k.contains(key)),
            Err(_) => Some(false),
        }
    }
    /// Admit exactly `addrs` (the caller's signed peer addresses) as
    /// AUTHENTICATED destinations until the returned [`Pin`] drops. Fails
    /// closed once `capacity` distinct authenticated destinations are pinned.
    pub(crate) fn pin(
        self: &Arc<Self>,
        addrs: impl IntoIterator<Item = SocketAddr>,
    ) -> Result<Pin, Error> {
        let pin = self.take(addrs, PinClass::Authenticated)?;
        self.remember(&pin.addrs);
        Ok(pin)
    }
    /// Admit an inbound handshake's source as a PROVISIONAL destination (its
    /// own capacity: unauthenticated sources never touch authenticated pins).
    pub(crate) fn pin_provisional(
        self: &Arc<Self>,
        addrs: impl IntoIterator<Item = SocketAddr>,
    ) -> Result<Pin, Error> {
        self.take(addrs, PinClass::Provisional)
    }
    fn limit(&self, class: PinClass) -> usize {
        match class {
            PinClass::Authenticated => self.capacity,
            PinClass::Provisional => self.provisional_capacity,
        }
    }
    fn take(
        self: &Arc<Self>,
        addrs: impl IntoIterator<Item = SocketAddr>,
        class: PinClass,
    ) -> Result<Pin, Error> {
        let addrs: Vec<SocketAddr> = addrs.into_iter().map(canonical).collect();
        if addrs.is_empty() || addrs.iter().any(|a| !self.policy.allows(a.ip())) {
            return Err(Error::Authentication);
        }
        let limit = self.limit(class);
        let mut pins = self.pins.write().map_err(|_| Error::Unavailable)?;
        let table = pins.class(class);
        let fresh = addrs.iter().filter(|a| !table.contains_key(*a)).count();
        if table.len() + fresh > limit {
            return Err(Error::Unavailable);
        }
        if class == PinClass::Authenticated {
            // A dialed destination is mapped by iroh too (`lan_target`).
            self.admitted
                .fetch_add(u64::try_from(fresh).unwrap_or(u64::MAX), Ordering::SeqCst);
        }
        for addr in &addrs {
            *table.entry(*addr).or_insert(0) += 1;
        }
        Ok(Pin {
            admission: self.clone(),
            addrs,
            class,
        })
    }
    /// Record `addrs` as admitted once (see `admits_seen`). A poisoned history
    /// only makes the monitor stricter (unseen = outside), never wider.
    fn remember(&self, addrs: &[SocketAddr]) {
        let Ok(mut history) = self.history.lock() else {
            return;
        };
        let (seen, order) = &mut *history;
        for addr in addrs {
            if seen.insert(*addr) {
                order.push_back(*addr);
            }
        }
        while order.len() > self.remembered {
            if let Some(oldest) = order.pop_front() {
                seen.remove(&oldest);
            }
        }
    }
    pub(crate) fn close(&self) {
        self.closing.store(true, Ordering::SeqCst);
    }
    /// Datagrams the filter refused to send (never left the socket).
    pub(crate) fn refused(&self) -> u64 {
        self.refused.load(Ordering::SeqCst)
    }
    /// Inbound datagrams the receive filter dropped (never reached iroh).
    pub(crate) fn dropped(&self) -> u64 {
        self.dropped.load(Ordering::SeqCst)
    }
    /// See the `admitted` field.
    pub(crate) fn admitted(&self) -> u64 {
        self.admitted.load(Ordering::SeqCst)
    }
}
/// Holds exact destination pins; releases them on drop (not while closing).
#[derive(Debug)]
pub(crate) struct Pin {
    admission: Arc<Admission>,
    addrs: Vec<SocketAddr>,
    class: PinClass,
}
fn release(table: &mut HashMap<SocketAddr, usize>, addrs: &[SocketAddr]) {
    for addr in addrs {
        if let Some(count) = table.get_mut(addr) {
            *count -= 1;
            if *count == 0 {
                table.remove(addr);
            }
        }
    }
}
impl Pin {
    /// A provisional (accept-side) pin whose peer key the host verified moves
    /// to the authenticated class. Fails closed (pin unchanged) when the
    /// authenticated table is full.
    pub(crate) fn promote(&mut self) -> Result<(), Error> {
        if self.class == PinClass::Authenticated {
            return Ok(());
        }
        let capacity = self.admission.capacity;
        let mut pins = self
            .admission
            .pins
            .write()
            .map_err(|_| Error::Unavailable)?;
        let fresh = self
            .addrs
            .iter()
            .filter(|a| !pins.authenticated.contains_key(*a))
            .count();
        if pins.authenticated.len() + fresh > capacity {
            return Err(Error::Unavailable);
        }
        for addr in &self.addrs {
            *pins.authenticated.entry(*addr).or_insert(0) += 1;
        }
        release(&mut pins.provisional, &self.addrs);
        drop(pins);
        self.class = PinClass::Authenticated;
        self.admission.remember(&self.addrs);
        Ok(())
    }
    /// Record a provisional pin's addresses as admitted (an accept whose
    /// caller verifies the key itself) without changing its class.
    pub(crate) fn remember(&self) {
        self.admission.remember(&self.addrs);
    }
    /// A provisional pin whose handshake the host refused (an unadmitted key)
    /// or that failed: released now, and its sources' receive-filter slots
    /// freed for other hosts at once (B11, see the module note).
    pub(crate) fn refuse(self) {
        if self.class == PinClass::Provisional {
            self.admission.refuse_inbound(&self.addrs, Instant::now());
        }
        drop(self);
    }
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
        release(pins.class(self.class), &self.addrs);
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
    #[cfg(test)]
    pub(crate) fn bind(interface: IpAddr, admission: Arc<Admission>) -> io::Result<Self> {
        Self::on(
            std::net::UdpSocket::bind(SocketAddr::new(interface, 0))?,
            admission,
        )
    }
    /// The transport over an already bound socket (a recycled endpoint keeps
    /// its socket, so its signed address stays valid; B11).
    pub(crate) fn on(socket: std::net::UdpSocket, admission: Arc<Admission>) -> io::Result<Self> {
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
        let now = Instant::now();
        let mut filled = 0;
        let mut dropped = 0;
        while filled < slots {
            let mut read = tokio::io::ReadBuf::new(&mut bufs[filled][..]);
            match self.socket.poll_recv_from(cx, &mut read) {
                Poll::Pending => break,
                Poll::Ready(Err(error)) if filled == 0 => return Poll::Ready(Err(error)),
                // The datagrams already filled are returned; the error recurs
                // on the next poll if it persists.
                Poll::Ready(Err(_)) => break,
                Poll::Ready(Ok(source)) => {
                    if !self.admission.admit_inbound(source, read.filled(), now) {
                        // The receive filter: iroh never sees (or maps) this
                        // source; the slot is reused for the next datagram.
                        self.admission.dropped.fetch_add(1, Ordering::SeqCst);
                        dropped += 1;
                        if dropped >= noq_udp::BATCH_SIZE {
                            // Bounded work per poll under a flood: yield, and
                            // be polled again at once.
                            break;
                        }
                        continue;
                    }
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
        if filled > 0 {
            Poll::Ready(Ok(filled))
        } else {
            if dropped >= noq_udp::BATCH_SIZE {
                // Yielded with datagrams possibly still queued: no readiness
                // event would wake us, so wake ourselves.
                cx.waker().wake_by_ref();
            }
            Poll::Pending
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

#[cfg(test)]
#[path = "iroh_lan_admission_tests.rs"]
mod admission_tests;
#[cfg(all(test, unix))]
#[path = "iroh_lan_inbound_tests.rs"]
mod inbound_tests;
#[cfg(all(test, unix))]
#[path = "iroh_lan_filter_tests.rs"]
mod tests;
