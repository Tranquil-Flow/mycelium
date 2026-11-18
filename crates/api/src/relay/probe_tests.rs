// SPDX-License-Identifier: AGPL-3.0-only
//! Lane ST patch round 1 (review ST-01): the relay's probe lines over a failing sink, and a panic
//! inside a connection, leave the listener and every connection slot as they were.
use super::*;
use ::seed::probe::{Recorder, Sink};
use std::{
    io::{Read, Write},
    sync::atomic::AtomicUsize,
};

const PROMPT: Duration = Duration::from_secs(5);

/// A sink whose every write fails (EIO / ENOSPC on the log file).
struct Failing;
impl Sink for Failing {
    fn write_line(&mut self, _line: &[u8]) -> std::io::Result<()> {
        Err(std::io::Error::other("probe sink failed"))
    }
}
fn failing_recorder() -> &'static Recorder {
    Box::leak(Box::new(Recorder::with_sink(4, Box::new(Failing))))
}

/// A seed hop that always fails (`502 relay_backend_unavailable`, logged).
struct Unavailable;
impl SeedBackend for Unavailable {
    fn request(&self, _: &str, _: &str, _: &str, _: &[u8]) -> Result<BackendReply, BackendError> {
        Err(BackendError::Unavailable)
    }
}
/// A seed hop that panics (stands for any future panic inside a connection's handling).
struct Panicking;
impl SeedBackend for Panicking {
    fn request(&self, _: &str, _: &str, _: &str, _: &[u8]) -> Result<BackendReply, BackendError> {
        panic!("a hop panicked");
    }
}

/// One `GET` through the proxy; the whole answer (empty when the connection closed unanswered).
fn get(port: u16, path: &str) -> String {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).expect("connect to the proxy");
    stream.set_read_timeout(Some(PROMPT)).unwrap_or(());
    write!(stream, "GET {path} HTTP/1.1\r\nHost: relay\r\n\r\n").unwrap_or(());
    let mut answer = Vec::new();
    stream.read_to_end(&mut answer).unwrap_or(0);
    String::from_utf8_lossy(&answer).into_owned()
}
fn wait_slots(proxy: &Proxy, what: &str) {
    let started = Instant::now();
    while proxy.shared.slots.load(Ordering::SeqCst) != MAX_CONNECTIONS {
        assert!(
            started.elapsed() < PROMPT,
            "{what}: {} of {MAX_CONNECTIONS} slots free",
            proxy.shared.slots.load(Ordering::SeqCst)
        );
        thread::sleep(Duration::from_millis(5));
    }
}

/// ST-01 acceptance (4): three times the slot count of logged hop failures over a failing sink;
/// every one is answered `502` and every slot comes back.
#[test]
fn logged_hop_failures_over_a_failing_sink_leave_every_slot_recoverable() {
    let proxy = Proxy::start_in(0, false, failing_recorder()).expect("proxy");
    proxy.bind_seed(Some(Arc::new(Unavailable)));
    for n in 0..3 * MAX_CONNECTIONS {
        let answer = get(proxy.port(), "/seed/identity");
        assert!(
            answer.starts_with("HTTP/1.0 502 ") && answer.contains("relay_backend_unavailable"),
            "request {n}: {answer:?}"
        );
    }
    wait_slots(&proxy, "after the logged 502s");
    proxy.close();
}

/// The slot release is panic-safe: a connection that panics still returns its slot.
#[test]
fn a_panicking_connection_still_returns_its_slot() {
    let proxy = Proxy::start(0, false).expect("proxy");
    proxy.bind_seed(Some(Arc::new(Panicking)));
    for _ in 0..3 {
        assert_eq!(get(proxy.port(), "/seed/identity"), "", "closed unanswered");
    }
    wait_slots(&proxy, "after three panicking connections");
    proxy.close();
}

/// ST-01 acceptance (5): the accept loop logs a slot refusal and an accept error over a failing
/// sink and keeps accepting until it is stopped.
#[test]
fn the_accept_loop_survives_a_diagnostic_write_failure() {
    let shared = Arc::new(ProxyShared {
        stop: AtomicBool::new(false),
        // every slot taken: an accepted connection is refused (and logged)
        slots: AtomicUsize::new(0),
        trust_client_ip: false,
        handlers: Mutex::new(Handlers {
            backend: None,
            local: None,
            chat: None,
            swarm: None,
        }),
        probe: failing_recorder(),
    });
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("listener");
    let port = listener.local_addr().expect("address").port();
    let calls = AtomicUsize::new(0);
    let ended = thread::scope(|s| {
        s.spawn(|| {
            accept_with(
                || {
                    let n = calls.fetch_add(1, Ordering::SeqCst) + 1;
                    match n {
                        1 | 3 => {
                            let _client = TcpStream::connect(("127.0.0.1", port))?;
                            listener.accept().map(|(stream, _)| stream)
                        }
                        _ => {
                            if n >= 4 {
                                shared.stop.store(true, Ordering::SeqCst);
                            }
                            // EMFILE: out of descriptors
                            Err(std::io::Error::from_raw_os_error(24))
                        }
                    }
                },
                &shared,
                Duration::from_millis(1),
            );
        })
        .join()
    });
    assert!(ended.is_ok(), "the accept thread died on a probe line");
    assert_eq!(
        calls.load(Ordering::SeqCst),
        4,
        "the loop kept accepting until stopped"
    );
}
