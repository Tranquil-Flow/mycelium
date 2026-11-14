// SPDX-License-Identifier: AGPL-3.0-only
//! A loopback UDP relay between one QUIC client and one server that can park a
//! handshake in flight: it forwards every client datagram to the server, and
//! HOLDS every server datagram (the server's handshake answers) while held.
//! The first server datagram is an event (`answered`): the server pulled the
//! client's Initial and is mid-handshake. `release` delivers the held
//! datagrams in order and forwards from then on; `freeze` drops every later
//! client datagram instead (a peer that went silent mid-handshake, which never
//! even sends its own close). Nothing is decided by
//! elapsed time: the socket read timeout only lets the relay threads see
//! `Drop`. Shared by the runtime and api suites (`#[path]`).
use std::{
    net::{IpAddr, Ipv4Addr, SocketAddr, UdpSocket},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    time::Duration,
};

struct Shared {
    stop: AtomicBool,
    /// Set by `freeze`: client datagrams are dropped from then on.
    frozen: AtomicBool,
    /// `Some` while held: the server's datagrams not yet delivered.
    held: Mutex<Option<Vec<Vec<u8>>>>,
    client: Mutex<Option<SocketAddr>>,
    front: UdpSocket,
    back: UdpSocket,
}
impl Shared {
    fn deliver(&self, datagram: &[u8]) {
        let client = *self.client.lock().unwrap();
        if let Some(client) = client {
            // A lost datagram is QUIC loss, as on a real link.
            if self.front.send_to(datagram, client).is_err() {
                eprintln!("udp hold: a server datagram was not delivered");
            }
        }
    }
}
pub struct Hold {
    front: SocketAddr,
    shared: Arc<Shared>,
    answered: mpsc::Receiver<()>,
    relays: Vec<std::thread::JoinHandle<()>>,
}
impl Hold {
    /// Relay to `server` (a loopback socket), holding the server's datagrams.
    /// `poll` is how often the relay threads look for `Drop`.
    pub fn start(server: SocketAddr, poll: Duration) -> Self {
        let local = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0);
        let front = UdpSocket::bind(local).unwrap();
        let back = UdpSocket::bind(SocketAddr::new(server.ip(), 0)).unwrap();
        back.connect(server).unwrap();
        front.set_read_timeout(Some(poll)).unwrap();
        back.set_read_timeout(Some(poll)).unwrap();
        let address = front.local_addr().unwrap();
        let shared = Arc::new(Shared {
            stop: AtomicBool::new(false),
            frozen: AtomicBool::new(false),
            held: Mutex::new(Some(Vec::new())),
            client: Mutex::new(None),
            front,
            back,
        });
        let (tell, answered) = mpsc::channel();
        let up = shared.clone();
        let upward = std::thread::spawn(move || {
            let mut buffer = vec![0u8; 65536];
            while !up.stop.load(Ordering::SeqCst) {
                if let Ok((n, from)) = up.front.recv_from(&mut buffer) {
                    if up.frozen.load(Ordering::SeqCst) {
                        continue;
                    }
                    *up.client.lock().unwrap() = Some(from);
                    if up.back.send(&buffer[..n]).is_err() {
                        eprintln!("udp hold: a client datagram was not forwarded");
                    }
                }
            }
        });
        let down = shared.clone();
        let downward = std::thread::spawn(move || {
            let mut buffer = vec![0u8; 65536];
            let mut first = Some(tell);
            while !down.stop.load(Ordering::SeqCst) {
                if let Ok(n) = down.back.recv(&mut buffer) {
                    let mut held = down.held.lock().unwrap();
                    match held.as_mut() {
                        Some(queue) => queue.push(buffer[..n].to_vec()),
                        None => down.deliver(&buffer[..n]),
                    }
                    drop(held);
                    if let Some(tell) = first.take() {
                        // The test may already have stopped listening.
                        if tell.send(()).is_err() {
                            eprintln!("udp hold: nobody waits for the server's answer");
                        }
                    }
                }
            }
        });
        Self {
            front: address,
            shared,
            answered,
            relays: vec![upward, downward],
        }
    }
    /// Where the client dials instead of the server.
    pub fn address(&self) -> SocketAddr {
        self.front
    }
    /// Whether the server has sent its first datagram, waiting at most `within`.
    pub fn answered(&self, within: Duration) -> bool {
        self.answered.recv_timeout(within).is_ok()
    }
    /// Drop every client datagram from now on (the server keeps waiting for them).
    pub fn freeze(&self) {
        self.shared.frozen.store(true, Ordering::SeqCst);
    }
    /// Deliver what was held, in order, and forward everything from now on.
    pub fn release(&self) {
        let mut held = self.shared.held.lock().unwrap();
        for datagram in held.take().unwrap_or_default() {
            self.shared.deliver(&datagram);
        }
    }
}
impl Drop for Hold {
    fn drop(&mut self) {
        self.shared.stop.store(true, Ordering::SeqCst);
        for relay in self.relays.drain(..) {
            if relay.join().is_err() {
                eprintln!("udp hold: a relay thread panicked");
            }
        }
    }
}
