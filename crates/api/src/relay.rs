// SPDX-License-Identifier: AGPL-3.0-only
//! The temporary public relay: `mycelium_a13_macos/public_relay.py` @ f4e366e9 (state machine,
//! quick-tunnel / external providers, in-process relay proxy).
//!
//! * [`Relay`] = `TemporaryHttpsRelay` (`public_relay.py:276-454`): `new` (constructor checks),
//!   `start` (`:322-366`), `status` (`:418-440`), the renew-while-serving lifetime (`:368-410`) and
//!   `close` (`:442-454`). Every failure is the source's closed `ValueError` text.
//! * [`Proxy`] = `SeedRelayProxy` + `_RelayServer` + `_RelayHandler` (`:27-240`): the route table,
//!   the framing rules and the in-process handler results. The verified-TLS hop into the seed is the
//!   host's [`SeedBackend`] (the seed server is not in this workspace yet: SE-L10-SEED-COORD).
//! * The quick-tunnel provider spawns the bundled `cloudflared` exactly as the source does (own
//!   process group, isolated `HOME`, scrubbed environment). Tests give it a fake binary; nothing here
//!   opens a tunnel by itself.
//!
//! Not ported on purpose: `http.server`'s own parse errors for malformed request lines/headers
//! (answered here with a bare `400`/`431`/`414` page-less reply), the `Server`/`Date` headers, and
//! Python's `int()` leniency for a `Content-Length` such as `1_0` or `+5`.
use crate::{
    join_link::RelayOptions,
    persistent::RelayState,
    schema::{PublicError, Raised},
    wire::Json,
};
use budgets::{BudgetId, Entries};
use serde_json::{Map, Value};
use std::{
    collections::VecDeque,
    io::{BufRead, BufReader, Read, Write},
    net::{Shutdown, TcpListener, TcpStream},
    os::unix::{
        fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
        process::CommandExt,
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        Arc, Condvar, Mutex, MutexGuard,
        atomic::{AtomicBool, AtomicUsize, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};

/// `_MAX_FRAME` (`public_relay.py:22`): one HTTP body, request or response.
/// SE-L10-RELAY-BOUNDS (L1b): not in the budget registry yet.
const MAX_FRAME: usize = 1024 * 1024;
/// `threading.BoundedSemaphore(16)` (`:63`): concurrent connections. SE-L10-RELAY-BOUNDS.
const MAX_CONNECTIONS: usize = 16;
/// `_CHUNK_LINE_MAX` (`:36`): a chunk-size line is `readline(10 + 1)`. SE-L10-RELAY-BOUNDS.
const CHUNK_LINE_MAX: usize = 10;
/// Largest chunk size written as hex digits by `_CHUNK_SIZE_RE` (`{1,8}`).
const CHUNK_HEX_DIGITS: usize = 8;
const LOCAL_PREFIXES: [&str; 3] = ["/pairing-window/", "/link-probe/", "/member-report/"];
const SWARM_PREFIX: &str = "/swarm-entry/v1/";
const LOCAL_TYPES: [&str; 3] = [
    "application/json",
    "text/html; charset=utf-8",
    "image/svg+xml",
];
const CHAT_CSP: &str = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'";
const SWARM_CORS: [(&str, &str); 4] = [
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Methods", "POST"),
    ("Access-Control-Allow-Headers", "Content-Type"),
    ("Access-Control-Max-Age", "600"),
];
const PRIVACY_QUICK: &str = "The selected relay terminates HTTPS and can observe control traffic, visitor IP addresses and timing, and web chat prompts and replies in plaintext (they cross this relay); only inference between the Macs uses Iroh. If system DNS fails, relay names use Cloudflare HTTPS DNS with unchanged certificate verification.";

fn span(id: BudgetId) -> Duration {
    id.span(Entries::new(0))
        .map_or(Duration::ZERO, |v| Duration::from_millis(v.millis()))
}
fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    match m.lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    }
}

pub type Clock = Arc<dyn Fn() -> f64 + Send + Sync>;
/// `probe() -> bool`: is a run serving through this relay right now; `Err` is a probe that raised.
pub type Probe = Arc<dyn Fn() -> Result<bool, ()> + Send + Sync>;
/// `installed_relay_binary()` (`:250-255`).
pub type Binary = Arc<dyn Fn() -> Option<PathBuf> + Send + Sync>;

/// What a relay's `close()` reports to a [`ReleaseObserver`] (a test seam; production has none).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReleaseEvent {
    /// The closing thread has published `closed` (status reads "stopped") and has not yet released
    /// the child and the proxy listener.
    Stopping,
    /// A second closer found the relay closing and waits for that release.
    AwaitingRelease,
}
pub type ReleaseObserver = Arc<dyn Fn(ReleaseEvent) + Send + Sync>;

/// The seams of the source: `_clock`, `_lifetime_poll`, the binary lookup and the environment the
/// child inherits. [`Hooks::production`] is what the app uses.
pub struct Hooks {
    pub clock: Clock,
    /// `None`: no lifetime thread (tests step explicitly).
    pub lifetime_poll: Option<Duration>,
    /// `self._ready.wait(45)`.
    pub ready_wait: Duration,
    pub binary: Binary,
    /// The parent environment the child inherits (scrubbed of `TUNNEL_*`/`CLOUDFLARE_*`/`CF_*`/`NO_TLS_VERIFY`).
    pub env: Vec<(String, String)>,
    /// Told where `close()` stands (tests hold a closer between "stopped" and the release).
    pub release_observer: Option<ReleaseObserver>,
}
impl Hooks {
    /// `time.monotonic`, a 1 s lifetime poll, the 45 s ready wait, `installed_relay_binary()` for
    /// the given bundled directory, and the process environment.
    pub fn production(bundled_dir: Option<PathBuf>) -> Self {
        let origin = Instant::now();
        Self {
            clock: Arc::new(move || origin.elapsed().as_secs_f64()),
            lifetime_poll: Some(span(BudgetId::RelayPoll)),
            ready_wait: span(
                BudgetId::PyMyceliumA13MacosPublicRelayTemporaryhttpsrelayStartSelfReadyWaitL355,
            ),
            binary: Arc::new(move || installed_relay_binary(bundled_dir.as_deref())),
            env: std::env::vars().collect(),
            release_observer: None,
        }
    }
    /// Ruling 31 (RELF2): the PACKAGED app's hooks, [`Hooks::production`] except the client
    /// lookup. The relay client is only `client` = (the admitted bundled file, the sha256 its pin
    /// names), never one found on `PATH`; each lookup (one per Start, before the spawn,
    /// `start_quick`) re-checks it with [`verified_relay_binary`]. `None` (no bundle path or no
    /// pin) finds nothing: the Start refuses `relay_client_installation_required`.
    pub fn pinned(client: Option<(PathBuf, String)>) -> Self {
        Self {
            binary: Arc::new(move || {
                client
                    .as_ref()
                    .and_then(|(path, sha256)| verified_relay_binary(path, sha256))
            }),
            ..Self::production(None)
        }
    }
}
/// The pinned relay client as it is NOW: `path` opened without following a symlink (and without
/// blocking on a FIFO), a regular file with an execute bit, whose sha256 is `sha256`. Answers that
/// same `path` (the spawn runs it) or `None`. Not covered: a same-user writer replacing the file
/// between this check and the `exec` that follows it in `start_quick` (macOS has no `fexecve`).
pub fn verified_relay_binary(path: &Path, sha256: &str) -> Option<PathBuf> {
    use sha2::{Digest, Sha256};
    let mut file = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .ok()?;
    let meta = file.metadata().ok()?;
    if !meta.is_file() || meta.permissions().mode() & 0o111 == 0 {
        return None;
    }
    let mut hash = Sha256::new();
    std::io::copy(&mut file, &mut hash).ok()?;
    (format!("{:x}", hash.finalize()) == sha256).then(|| path.to_path_buf())
}
/// `installed_relay_binary` (`public_relay.py:250-255`): `<bundled>/native/cloudflared` when it is an
/// executable file, else `cloudflared` on `PATH`.
pub fn installed_relay_binary(bundled_dir: Option<&Path>) -> Option<PathBuf> {
    let executable = |p: &Path| {
        std::fs::metadata(p).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
    };
    if let Some(dir) = bundled_dir {
        let bundled = dir.join("native").join("cloudflared");
        if executable(&bundled) {
            return Some(bundled);
        }
    }
    std::env::var_os("PATH").and_then(|path| {
        std::env::split_paths(&path)
            .map(|d| d.join("cloudflared"))
            .find(|p| executable(p))
    })
}

/// `provider_event` (`:243-248`): the first `https://<[a-z0-9-]+>.trycloudflare.com` of a cloudflared
/// log line (the `cloudflare` provider only).
pub fn provider_event(provider: &str, line: &str) -> Option<String> {
    if provider != "cloudflare" {
        return None;
    }
    const SCHEME: &str = "https://";
    const SUFFIX: &str = ".trycloudflare.com";
    let bytes = line.as_bytes();
    let mut from = 0;
    while let Some(at) = line[from..].find(SCHEME) {
        let start = from + at;
        let label = start + SCHEME.len();
        let len = bytes[label..]
            .iter()
            .take_while(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || **b == b'-')
            .count();
        if len > 0 && line[label + len..].starts_with(SUFFIX) {
            return Some(line[start..label + len + SUFFIX.len()].to_owned());
        }
        from = start + 1;
    }
    None
}

// ------------------------------------------------------------------------------------------------ proxy

/// What an in-process join route answers (`(status, raw[, content_type])`).
pub struct LocalAnswer {
    pub status: i64,
    pub raw: Vec<u8>,
    pub kind: Option<String>,
}
/// What the chat / swarm-entry routes answer (`(status, content_type, raw)`).
pub struct RichAnswer {
    pub status: i64,
    pub kind: String,
    pub raw: Vec<u8>,
}
pub type LocalHandler = Arc<dyn Fn(&str, &str, &[u8]) -> Result<LocalAnswer, ()> + Send + Sync>;
/// `handler(method, path, headers, body)`: the headers are the public dict (later duplicates win,
/// `Cf-Connecting-Ip` dropped unless the edge is trusted).
pub type RichHandler =
    Arc<dyn Fn(&str, &str, &[(String, String)], &[u8]) -> Result<RichAnswer, ()> + Send + Sync>;
/// One answer of the verified-TLS seed hop (`HTTPSConnection(...).getresponse()`), body read up to
/// `MAX_FRAME + 1` bytes.
pub struct BackendReply {
    pub status: u16,
    pub content_type: String,
    pub body: Vec<u8>,
}
/// Why the seed hop failed. `Unavailable` is the source's `OSError`/`HTTPException` (`502
/// relay_backend_unavailable`); `CertificateRejected` is `ssl.SSLCertVerificationError`, which is
/// ALSO a `ValueError` and is therefore caught by the handler's `except ValueError` first (`400
/// relay_frame_invalid`, vector `d_untrusted_ca`).
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BackendError {
    Unavailable,
    CertificateRejected,
}
/// The canonical seed hop.
pub trait SeedBackend: Send + Sync {
    fn request(
        &self,
        method: &str,
        path: &str,
        content_type: &str,
        body: &[u8],
    ) -> Result<BackendReply, BackendError>;
}

struct Handlers {
    backend: Option<Arc<dyn SeedBackend>>,
    local: Option<LocalHandler>,
    chat: Option<RichHandler>,
    swarm: Option<RichHandler>,
}
/// What the accept loop and every connection thread share. The listener is NOT here: the accept
/// thread owns it, so it is closed when `Proxy::close` has joined that thread, even while proxied
/// requests are still in flight (v0.1.4 `server_close()`, `public_relay.py:240`).
struct ProxyShared {
    stop: AtomicBool,
    slots: AtomicUsize,
    trust_client_ip: bool,
    handlers: Mutex<Handlers>,
}
/// Lane B, B12: the relay's fork boundary. A child forked while a socket is open holds its own copy
/// of that socket until its exec closes it (std marks it close-on-exec, and macOS has no atomic
/// `SOCK_CLOEXEC`: `socket()` then `FIOCLEX`). A proxy closed in that window still has its listener
/// bound in the child, so a relay started right after cannot bind the same port, and a child forked
/// between `socket()` and `FIOCLEX` keeps it for its whole life. The relay's own child spawn
/// (the quick tunnel's client) and its proxy listener's bind and close take this boundary, so
/// neither can land inside the other's window; a caller of this module that forks beside a relay
/// (and its tests) takes the same boundary around its spawn. v0.1.4 spawns with Python's
/// `close_fds=True` under the GIL, which has neither window.
struct ForkBoundary {
    lock: Mutex<()>,
    waiting: AtomicUsize,
}
static FORKS: ForkBoundary = ForkBoundary {
    lock: Mutex::new(()),
    waiting: AtomicUsize::new(0),
};
/// Enter the relay's fork boundary (see [`ForkBoundary`]): held across a spawn, or across a socket's
/// bind or close whose port must not be held by a child.
pub fn fork_boundary() -> MutexGuard<'static, ()> {
    FORKS.waiting.fetch_add(1, Ordering::SeqCst);
    let entered = FORKS
        .lock
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    FORKS.waiting.fetch_sub(1, Ordering::SeqCst);
    entered
}
/// How many threads wait to enter the fork boundary now (a test observes a close that waits for a
/// spawn in flight).
pub fn fork_boundary_waiting() -> usize {
    FORKS.waiting.load(Ordering::SeqCst)
}
/// `SeedRelayProxy`: a loopback HTTP server the public front forwards into.
pub struct Proxy {
    shared: Arc<ProxyShared>,
    port: u16,
    thread: Mutex<Option<thread::JoinHandle<()>>>,
}
impl Proxy {
    /// `SeedRelayProxy(port, trust_client_ip).start()`; an `OSError` is the caller's.
    pub fn start(port: u16, trust_client_ip: bool) -> std::io::Result<Arc<Proxy>> {
        // B12: no relay spawn lands between the listener's `socket()` and its close-on-exec.
        let listener = {
            let _forks = fork_boundary();
            TcpListener::bind(("127.0.0.1", port))?
        };
        let port = listener.local_addr()?.port();
        let shared = Arc::new(ProxyShared {
            stop: AtomicBool::new(false),
            slots: AtomicUsize::new(MAX_CONNECTIONS),
            trust_client_ip,
            handlers: Mutex::new(Handlers {
                backend: None,
                local: None,
                chat: None,
                swarm: None,
            }),
        });
        let accept = shared.clone();
        let handle = thread::Builder::new()
            .name("a13-seed-relay".into())
            .spawn(move || accept_loop(&listener, &accept))?;
        Ok(Arc::new(Proxy {
            shared,
            port,
            thread: Mutex::new(Some(handle)),
        }))
    }
    pub fn port(&self) -> u16 {
        self.port
    }
    /// `http://127.0.0.1:<port>`.
    pub fn origin(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }
    /// `bind_seed`; `None` unbinds (`server.backend = None`).
    pub fn bind_seed(&self, backend: Option<Arc<dyn SeedBackend>>) {
        lock(&self.shared.handlers).backend = backend;
    }
    pub fn seed_bound(&self) -> bool {
        lock(&self.shared.handlers).backend.is_some()
    }
    pub fn bind_local(&self, handler: Option<LocalHandler>) {
        lock(&self.shared.handlers).local = handler;
    }
    pub fn bind_chat(&self, handler: Option<RichHandler>) {
        lock(&self.shared.handlers).chat = handler;
    }
    pub fn bind_swarm_entry(&self, handler: Option<RichHandler>) {
        lock(&self.shared.handlers).swarm = handler;
    }
    /// `SeedRelayProxy.close`: stop accepting, join the accept thread (in-flight connections finish).
    /// B12: the listener is closed inside the fork boundary, so a relay spawn in flight (which holds
    /// a copy of it until its exec) has exec'd first and no later one can copy it.
    pub fn close(&self) {
        if self.shared.stop.swap(true, Ordering::SeqCst) {
            return;
        }
        let _forks = fork_boundary();
        // unblock `accept`
        TcpStream::connect(("127.0.0.1", self.port))
            .map(drop)
            .unwrap_or(());
        if let Some(handle) = lock(&self.thread).take() {
            handle.join().unwrap_or(());
        }
    }
}
fn accept_loop(listener: &TcpListener, shared: &Arc<ProxyShared>) {
    loop {
        let Ok((stream, _)) = listener.accept() else {
            if shared.stop.load(Ordering::SeqCst) {
                return;
            }
            continue;
        };
        if shared.stop.load(Ordering::SeqCst) {
            return;
        }
        let timeout = span(
            BudgetId::PyMyceliumA13MacosPublicRelayRelayserverProcessRequestRequestSettimeoutL66,
        );
        stream.set_read_timeout(Some(timeout)).unwrap_or(());
        stream.set_write_timeout(Some(timeout)).unwrap_or(());
        // `slots.acquire(blocking=False)`; none left: the connection is closed unanswered
        let acquired = shared
            .slots
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_sub(1))
            .is_ok();
        if !acquired {
            // Log-only probe (RV42 S1 RCA): the public front answers its own 502 for this one.
            eprintln!(
                "l11_relay_slots_exhausted slots={MAX_CONNECTIONS} wall_ms={}",
                ::seed::probe::wall_ms()
            );
            stream.shutdown(Shutdown::Both).unwrap_or(());
            continue;
        }
        let conn = shared.clone();
        let spawned = thread::Builder::new()
            .name("a13-seed-relay-conn".into())
            .spawn(move || {
                handle(&conn, stream);
                conn.slots.fetch_add(1, Ordering::SeqCst);
            });
        if spawned.is_err() {
            shared.slots.fetch_add(1, Ordering::SeqCst);
        }
    }
}

struct Req {
    method: String,
    path: String,
    headers: Vec<(String, String)>,
    reader: BufReader<TcpStream>,
}
impl Req {
    fn all<'a>(&'a self, name: &'a str) -> impl Iterator<Item = &'a str> {
        self.headers
            .iter()
            .filter(move |(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
    fn first<'a>(&'a self, name: &'a str) -> Option<&'a str> {
        self.all(name).next()
    }
}
enum Framing {
    Frame,
    Backend,
}
/// `rfile.readline(limit)`: up to and including the newline, or `limit` bytes.
fn read_line_limited(reader: &mut impl BufRead, limit: usize) -> std::io::Result<Vec<u8>> {
    let mut line = Vec::new();
    while line.len() < limit {
        let buf = reader.fill_buf()?;
        if buf.is_empty() {
            break;
        }
        let room = limit - line.len();
        let take = buf
            .iter()
            .take(room)
            .position(|b| *b == b'\n')
            .map_or(buf.len().min(room), |p| p + 1);
        line.extend_from_slice(&buf[..take]);
        reader.consume(take);
        if line.ends_with(b"\n") {
            break;
        }
    }
    Ok(line)
}
fn read_exact_or_short(reader: &mut impl Read, n: usize) -> std::io::Result<Vec<u8>> {
    let mut out = Vec::with_capacity(n.min(MAX_FRAME));
    reader.take(n as u64).read_to_end(&mut out)?;
    Ok(out)
}
fn reason(status: u16) -> &'static str {
    match status {
        100 => "Continue",
        101 => "Switching Protocols",
        102 => "Processing",
        103 => "Early Hints",
        200 => "OK",
        201 => "Created",
        202 => "Accepted",
        203 => "Non-Authoritative Information",
        204 => "No Content",
        205 => "Reset Content",
        206 => "Partial Content",
        207 => "Multi-Status",
        208 => "Already Reported",
        226 => "IM Used",
        300 => "Multiple Choices",
        301 => "Moved Permanently",
        302 => "Found",
        303 => "See Other",
        304 => "Not Modified",
        305 => "Use Proxy",
        307 => "Temporary Redirect",
        308 => "Permanent Redirect",
        400 => "Bad Request",
        401 => "Unauthorized",
        402 => "Payment Required",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        406 => "Not Acceptable",
        407 => "Proxy Authentication Required",
        408 => "Request Timeout",
        409 => "Conflict",
        410 => "Gone",
        411 => "Length Required",
        412 => "Precondition Failed",
        413 => "Content Too Large",
        414 => "URI Too Long",
        415 => "Unsupported Media Type",
        416 => "Range Not Satisfiable",
        417 => "Expectation Failed",
        418 => "I'm a Teapot",
        421 => "Misdirected Request",
        422 => "Unprocessable Content",
        423 => "Locked",
        424 => "Failed Dependency",
        425 => "Too Early",
        426 => "Upgrade Required",
        428 => "Precondition Required",
        429 => "Too Many Requests",
        431 => "Request Header Fields Too Large",
        451 => "Unavailable For Legal Reasons",
        500 => "Internal Server Error",
        501 => "Not Implemented",
        502 => "Bad Gateway",
        503 => "Service Unavailable",
        504 => "Gateway Timeout",
        505 => "HTTP Version Not Supported",
        506 => "Variant Also Negotiates",
        507 => "Insufficient Storage",
        508 => "Loop Detected",
        510 => "Not Extended",
        511 => "Network Authentication Required",
        _ => "",
    }
}
/// `_RelayHandler._reply`.
fn reply(
    stream: &mut TcpStream,
    status: u16,
    raw: &[u8],
    content_type: &str,
    csp: bool,
    extra: &[(&str, &str)],
) {
    let mut head = format!(
        "HTTP/1.0 {status} {}\r\nContent-Type: {content_type}\r\n",
        reason(status)
    );
    for (name, value) in extra {
        head.push_str(&format!("{name}: {value}\r\n"));
    }
    if csp {
        head.push_str(&format!(
            "Content-Security-Policy: {CHAT_CSP}\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\n"
        ));
    }
    head.push_str(&format!(
        "Content-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n",
        raw.len()
    ));
    stream.write_all(head.as_bytes()).unwrap_or(());
    stream.write_all(raw).unwrap_or(());
    stream.flush().unwrap_or(());
}
fn error_reply(stream: &mut TcpStream, status: u16, code: &str, extra: &[(&str, &str)]) {
    reply(
        stream,
        status,
        format!("{{\"error\":{{\"code\":\"{code}\"}}}}").as_bytes(),
        "application/json",
        false,
        extra,
    );
}
/// `http.server.BaseHTTPRequestHandler.send_error` for the one case the handler reaches with a
/// well-formed request: an unsupported method (`501`). Python 3.14's page.
fn unsupported_method(stream: &mut TcpStream, method: &str, head_only: bool) {
    let body = format!(
        "<!DOCTYPE HTML>\n<html lang=\"en\">\n    <head>\n        <meta charset=\"utf-8\">\n        <style type=\"text/css\">\n            :root {{\n                color-scheme: light dark;\n            }}\n        </style>\n        <title>Error response</title>\n    </head>\n    <body>\n        <h1>Error response</h1>\n        <p>Error code: 501</p>\n        <p>Message: Unsupported method ('{method}').</p>\n        <p>Error code explanation: 501 - Server does not support this operation.</p>\n    </body>\n</html>\n"
    );
    let head = format!(
        "HTTP/1.0 501 Not Implemented\r\nConnection: close\r\nContent-Type: text/html;charset=utf-8\r\nContent-Length: {}\r\n\r\n",
        body.len()
    );
    stream.write_all(head.as_bytes()).unwrap_or(());
    if !head_only {
        stream.write_all(body.as_bytes()).unwrap_or(());
    }
    stream.flush().unwrap_or(());
}
fn bare(stream: &mut TcpStream, status: u16) {
    let head = format!(
        "HTTP/1.0 {status} {}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n",
        reason(status)
    );
    stream.write_all(head.as_bytes()).unwrap_or(());
}

fn parse_request(stream: &TcpStream) -> Result<Req, u16> {
    let max_line = span_bytes();
    let mut reader = BufReader::new(stream.try_clone().map_err(|_| 400u16)?);
    let line = read_line_limited(&mut reader, max_line + 1).map_err(|_| 408u16)?;
    if line.is_empty() {
        return Err(0);
    }
    if line.len() > max_line {
        return Err(414);
    }
    let text = String::from_utf8_lossy(&line)
        .trim_end_matches(['\r', '\n'])
        .to_owned();
    let words: Vec<&str> = text.split_whitespace().collect();
    if words.len() != 3 || !words[2].starts_with("HTTP/") {
        return Err(400);
    }
    let mut headers: Vec<(String, String)> = Vec::new();
    loop {
        let raw = read_line_limited(&mut reader, max_line + 1).map_err(|_| 408u16)?;
        if raw.len() > max_line {
            return Err(431);
        }
        if raw.is_empty() || raw == b"\r\n" || raw == b"\n" {
            break;
        }
        if headers.len() >= 100 {
            return Err(431);
        }
        let line = String::from_utf8_lossy(&raw)
            .trim_end_matches(['\r', '\n'])
            .to_owned();
        if line.starts_with([' ', '\t']) {
            if let Some(last) = headers.last_mut() {
                last.1.push(' ');
                last.1.push_str(line.trim());
            }
            continue;
        }
        let Some((name, value)) = line.split_once(':') else {
            return Err(400);
        };
        headers.push((name.to_owned(), value.trim().to_owned()));
    }
    Ok(Req {
        method: words[0].to_owned(),
        path: words[1].to_owned(),
        headers,
        reader,
    })
}
/// Python's `readline(65537)` limit on a request/header line (`http.server._MAXLINE`).
fn span_bytes() -> usize {
    usize::try_from(
        BudgetId::PyRelayProviderLineCeiling
            .bytes()
            .map_or(0, |b| b.get()),
    )
    .unwrap_or(0)
}

fn handle(shared: &Arc<ProxyShared>, mut stream: TcpStream) {
    let mut req = match parse_request(&stream) {
        Ok(req) => req,
        Err(0) => return,
        Err(status) => return bare(&mut stream, status),
    };
    match req.method.as_str() {
        "GET" | "POST" => forward(shared, &mut req, &mut stream),
        "OPTIONS" => options(&req, &mut stream),
        other => unsupported_method(&mut stream, other, other == "HEAD"),
    }
    stream.shutdown(Shutdown::Both).unwrap_or(());
}
fn route_unknown(path: &str) -> bool {
    // The packaged member's work binding (`stage_contract::member_work::PATH`) is served by the
    // seed's `Inbox` extension, so it must reach the backend like `/seed/` and `/managed-control/`.
    // Without it the relay answers `relay_route_unknown` (404) and the member's publication loop
    // retries against a route that does exist.
    let known = path.starts_with("/seed/")
        || path.starts_with("/managed-control/")
        || path == stage_contract::member_work::PATH
        || path.starts_with(SWARM_PREFIX)
        || LOCAL_PREFIXES.iter().any(|p| path.starts_with(p))
        || chat_path(path);
    !known || ["..", "%", "?", "#", "\\"].iter().any(|v| path.contains(v))
}
fn chat_path(path: &str) -> bool {
    path == "/chat" || path.starts_with("/chat/")
}
fn options(req: &Req, stream: &mut TcpStream) {
    let bad = !req.path.starts_with(SWARM_PREFIX)
        || ["..", "%", "?", "#", "\\"]
            .iter()
            .any(|v| req.path.contains(v))
        || req.first("Content-Length").is_some_and(|v| v != "0")
        || req.first("Transfer-Encoding").is_some();
    if bad {
        return error_reply(stream, 404, "relay_route_unknown", &[]);
    }
    reply(stream, 204, b"", "application/json", false, &SWARM_CORS);
}
fn forward(shared: &Arc<ProxyShared>, req: &mut Req, stream: &mut TcpStream) {
    let started = Instant::now();
    if route_unknown(&req.path) {
        return error_reply(stream, 404, "relay_route_unknown", &[]);
    }
    if chat_path(&req.path) {
        return local(shared, req, stream, Kind::Chat);
    }
    if req.path.starts_with(SWARM_PREFIX) {
        return local(shared, req, stream, Kind::Swarm);
    }
    if LOCAL_PREFIXES.iter().any(|p| req.path.starts_with(p)) {
        return local(shared, req, stream, Kind::Join);
    }
    if req.first("Transfer-Encoding").is_some()
        || req.first("Cookie").is_some()
        || req.first("Authorization").is_some()
    {
        return error_reply(stream, 400, "relay_headers_invalid", &[]);
    }
    // seed / managed-control: a framed body, then the verified-TLS hop
    let body = match read_framed(req, Framing::Backend) {
        Ok(body) => body,
        Err(Framed::Invalid) => return error_reply(stream, 400, "relay_frame_invalid", &[]),
        Err(Framed::Io) => {
            backend_unavailable_probe(&req.path, "request_read", started);
            return error_reply(stream, 502, "relay_backend_unavailable", &[]);
        }
    };
    let backend = lock(&shared.handlers).backend.clone();
    let Some(backend) = backend else {
        return error_reply(stream, 503, "relay_seed_not_bound", &[]);
    };
    let content_type = req
        .first("Content-Type")
        .unwrap_or("application/json")
        .to_owned();
    let payload: &[u8] = if req.method == "POST" { &body } else { &[] };
    match backend.request(&req.method, &req.path, &content_type, payload) {
        Ok(answer) if answer.body.len() > MAX_FRAME => {
            error_reply(stream, 400, "relay_frame_invalid", &[]);
        }
        Ok(answer) => reply(
            stream,
            answer.status,
            &answer.body,
            &answer.content_type,
            false,
            &[],
        ),
        Err(BackendError::CertificateRejected) => {
            error_reply(stream, 400, "relay_frame_invalid", &[])
        }
        Err(BackendError::Unavailable) => {
            backend_unavailable_probe(&req.path, "hop", started);
            error_reply(stream, 502, "relay_backend_unavailable", &[]);
        }
    }
}
/// Log-only probe (RV42 S1 relay-502 RCA): the proxy's OWN `502 relay_backend_unavailable`, with
/// the route (no query: `route_unknown` refused any `?`), what failed (`request_read`: the body from
/// the public front; `hop`: the verified-TLS hop into the seed, 5 s per step) and the time since the
/// request head was read. A joined Mac logs every 502 as `code=None`; this line tells the proxy's
/// own answer from the front's.
fn backend_unavailable_probe(path: &str, stage: &str, started: Instant) {
    eprintln!(
        "l11_relay_backend_unavailable path={path} stage={stage} elapsed_ms={} wall_ms={}",
        started.elapsed().as_millis(),
        ::seed::probe::wall_ms()
    );
}
enum Framed {
    Invalid,
    Io,
}
/// The `Content-Length` rules shared by the forwarded and the in-process routes (`:84-91`).
fn read_framed(req: &mut Req, mode: Framing) -> Result<Vec<u8>, Framed> {
    let io_error = |_| match mode {
        Framing::Frame => Framed::Invalid,
        Framing::Backend => Framed::Io,
    };
    let lengths: Vec<String> = req.all("Content-Length").map(str::to_owned).collect();
    let post = req.method == "POST";
    if lengths.len() > 1 || (post && lengths.len() != 1) {
        return Err(Framed::Invalid);
    }
    let length: i64 = match lengths.first() {
        Some(v) => v.trim().parse().map_err(|_| Framed::Invalid)?,
        None => 0,
    };
    let in_range = usize::try_from(length).is_ok_and(|l| l <= MAX_FRAME);
    if !in_range || (!post && length != 0) {
        return Err(Framed::Invalid);
    }
    let length = usize::try_from(length).map_err(|_| Framed::Invalid)?;
    let body = read_exact_or_short(&mut req.reader, length).map_err(io_error)?;
    if body.len() != length {
        return Err(Framed::Invalid);
    }
    Ok(body)
}
/// `_read_chunked`: strict, bounded chunked body (no extensions, no trailers).
fn read_chunked(req: &mut Req) -> Result<Vec<u8>, ()> {
    let mut body = Vec::new();
    loop {
        let line = read_line_limited(&mut req.reader, CHUNK_LINE_MAX + 1).map_err(|_| ())?;
        let digits = line.strip_suffix(b"\r\n").ok_or(())?;
        if digits.is_empty()
            || digits.len() > CHUNK_HEX_DIGITS
            || !digits.iter().all(u8::is_ascii_hexdigit)
        {
            return Err(());
        }
        let size = usize::from_str_radix(std::str::from_utf8(digits).map_err(|_| ())?, 16)
            .map_err(|_| ())?;
        if size == 0 {
            break;
        }
        if body.len() + size > MAX_FRAME {
            return Err(());
        }
        let data = read_exact_or_short(&mut req.reader, size).map_err(|_| ())?;
        let crlf = read_exact_or_short(&mut req.reader, 2).map_err(|_| ())?;
        if data.len() != size || crlf != b"\r\n" {
            return Err(());
        }
        body.extend_from_slice(&data);
    }
    let end = read_exact_or_short(&mut req.reader, 2).map_err(|_| ())?;
    if end != b"\r\n" {
        return Err(());
    }
    Ok(body)
}
#[derive(Clone, Copy, PartialEq)]
enum Kind {
    Join,
    Chat,
    Swarm,
}
/// `_RelayHandler._local`: the owner app answers in-process.
fn local(shared: &Arc<ProxyShared>, req: &mut Req, stream: &mut TcpStream, kind: Kind) {
    let codings: Vec<String> = req.all("Transfer-Encoding").map(str::to_owned).collect();
    let chunked = codings.len() == 1 && codings[0].trim().eq_ignore_ascii_case("chunked");
    if (!codings.is_empty()
        && (!chunked || req.method != "POST" || req.all("Content-Length").next().is_some()))
        || req.first("Cookie").is_some()
        || req.first("Authorization").is_some()
    {
        return error_reply(stream, 400, "relay_headers_invalid", &[]);
    }
    let body = if chunked {
        read_chunked(req).map_err(|()| Framed::Invalid)
    } else {
        read_framed(req, Framing::Frame)
    };
    let Ok(body) = body else {
        return error_reply(stream, 400, "relay_frame_invalid", &[]);
    };
    let (cors, unbound, failed): (&[(&str, &str)], &str, &str) = match kind {
        Kind::Swarm => (
            &SWARM_CORS,
            "relay_swarm_entry_not_bound",
            "relay_swarm_entry_failed",
        ),
        Kind::Chat => (&[], "relay_chat_not_bound", "relay_chat_failed"),
        Kind::Join => (&[], "relay_join_not_bound", "relay_join_failed"),
    };
    let handlers = {
        let h = lock(&shared.handlers);
        (h.local.clone(), h.chat.clone(), h.swarm.clone())
    };
    let valid_status = |s: i64| (200..=599).contains(&s);
    match kind {
        Kind::Join => {
            let Some(handler) = handlers.0 else {
                return error_reply(stream, 503, unbound, cors);
            };
            let answer = handler(&req.method, &req.path, &body);
            match answer {
                Ok(a)
                    if valid_status(a.status)
                        && a.raw.len() <= MAX_FRAME
                        && LOCAL_TYPES
                            .contains(&a.kind.as_deref().unwrap_or("application/json")) =>
                {
                    let kind = a.kind.unwrap_or_else(|| "application/json".to_owned());
                    let csp = kind != "application/json";
                    reply(
                        stream,
                        u16::try_from(a.status).unwrap_or(500),
                        &a.raw,
                        &kind,
                        csp,
                        &[],
                    );
                }
                _ => error_reply(stream, 500, failed, &[]),
            }
        }
        Kind::Chat | Kind::Swarm => {
            let handler = if kind == Kind::Chat {
                handlers.1
            } else {
                handlers.2
            };
            let Some(handler) = handler else {
                return error_reply(stream, 503, unbound, cors);
            };
            let public = public_headers(&req.headers, shared.trust_client_ip);
            match handler(&req.method, &req.path, &public, &body) {
                Ok(a)
                    if valid_status(a.status)
                        && a.raw.len() <= MAX_FRAME
                        && (kind == Kind::Chat || a.kind == "application/json") =>
                {
                    reply(
                        stream,
                        u16::try_from(a.status).unwrap_or(500),
                        &a.raw,
                        &a.kind,
                        kind == Kind::Chat,
                        cors,
                    );
                }
                _ => error_reply(stream, 500, failed, cors),
            }
        }
    }
}
/// `_public_headers`: a dict (later duplicates win, first position kept).
fn public_headers(headers: &[(String, String)], trust: bool) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for (k, v) in headers {
        match out.iter_mut().find(|(name, _)| name == k) {
            Some(existing) => existing.1 = v.clone(),
            None => out.push((k.clone(), v.clone())),
        }
    }
    if !trust {
        out.retain(|(k, _)| !k.eq_ignore_ascii_case("cf-connecting-ip"));
    }
    out
}

// ------------------------------------------------------------------------------------------------ relay

#[derive(Clone, Copy, PartialEq)]
enum Provider {
    Cloudflare,
    External,
}
struct State {
    closed: bool,
    /// The closer has released the child and the proxy listener (`closed` is published first).
    released: bool,
    started: bool,
    process: Option<Child>,
    proxy: Option<Arc<Proxy>>,
    public_origin: Option<String>,
    error_code: Option<&'static str>,
    ready: bool,
    lines: VecDeque<String>,
    /// The log reader and the channel it signals on when it ends (`reader.join(2)`).
    reader: Option<(thread::JoinHandle<()>, mpsc::Receiver<()>)>,
    window: f64,
    granted: f64,
    deadline: Option<f64>,
    renewals: u64,
    renewal_stopped: Option<&'static str>,
    serving_probe: Probe,
    lifetime: Option<thread::JoinHandle<()>>,
    root: PathBuf,
}
struct Inner {
    provider: Provider,
    chosen_origin: Option<String>,
    proxy_port: Option<u16>,
    cloudflare_front: bool,
    auto_renew: bool,
    long_lived: bool,
    renewal_cap: f64,
    hooks: Hooks,
    state: Mutex<State>,
    ready: Condvar,
    wake: Condvar,
    /// Unit tests only: run on the lifetime thread once it decided the window expired, before it
    /// closes the relay (no other seam can order a closer into that gap).
    #[cfg(test)]
    expired_hold: Mutex<Option<Arc<dyn Fn() + Send + Sync>>>,
}
/// `TemporaryHttpsRelay`.
pub struct Relay {
    inner: Arc<Inner>,
}
/// The lifetime loop's view of one relay (`_lifetime_loop`'s `self`): it steps and reports the
/// relay without its host's locks, and it never closes the relay on drop.
#[derive(Clone)]
pub struct Lifetime(Arc<Inner>);
impl Lifetime {
    /// One `_lifetime_step`, as the lifetime thread runs it.
    pub fn step(&self) {
        lifetime_step(&self.0);
    }
    /// `status()` of the same relay.
    pub fn status(&self) -> Json {
        status_of(&self.0)
    }
}
impl Relay {
    /// `TemporaryHttpsRelay(state_root, provider, **spec)`; the checks run in the source's order.
    pub fn new(
        root: &Path,
        provider: &Value,
        spec: &Map<String, Value>,
        hooks: Hooks,
    ) -> Result<Self, &'static str> {
        let provider = match provider.as_str() {
            Some("cloudflare") => Provider::Cloudflare,
            Some("external") => Provider::External,
            _ => return Err("relay_provider_invalid"),
        };
        let cap_default = span(BudgetId::RelayGrantedWindowCap).as_secs_f64();
        let (mut chosen, mut port, mut front, mut auto_renew, mut long_lived, mut cap) =
            (None, None, false, true, false, cap_default);
        match provider {
            Provider::Cloudflare => {
                let set = |k: &str| spec.get(k).is_some_and(|v| !v.is_null());
                let truthy = |k: &str| match spec.get(k) {
                    None | Some(Value::Null | Value::Bool(false)) => false,
                    Some(Value::Number(n)) => n.as_f64() != Some(0.0),
                    Some(Value::String(s)) => !s.is_empty(),
                    Some(Value::Array(a)) => !a.is_empty(),
                    Some(Value::Object(o)) => !o.is_empty(),
                    Some(Value::Bool(true)) => true,
                };
                let cap_changed = spec.get("max_renewed_seconds").is_some_and(|v| match v {
                    Value::Number(n) => n.as_f64() != Some(cap_default),
                    _ => true,
                });
                if set("public_origin")
                    || set("proxy_port")
                    || truthy("cloudflare_front")
                    || spec
                        .get("long_lived")
                        .is_some_and(|v| v != &Value::Bool(false))
                    || spec
                        .get("auto_renew")
                        .is_some_and(|v| v != &Value::Bool(true))
                    || cap_changed
                {
                    return Err("relay_provider_invalid");
                }
            }
            Provider::External => {
                let options = RelayOptions::external(spec)?;
                chosen = Some(options.public_origin);
                port = Some(options.proxy_port);
                front = options.cloudflare_front;
                auto_renew = options.auto_renew;
                long_lived = options.long_lived;
                cap = options.renewal_cap_seconds;
            }
        }
        let state = State {
            closed: false,
            released: false,
            started: false,
            process: None,
            proxy: None,
            public_origin: None,
            error_code: None,
            ready: false,
            lines: VecDeque::new(),
            reader: None,
            window: 0.0,
            granted: 0.0,
            deadline: None,
            renewals: 0,
            renewal_stopped: None,
            serving_probe: Arc::new(|| Ok(false)),
            lifetime: None,
            root: root.to_path_buf(),
        };
        Ok(Self {
            inner: Arc::new(Inner {
                provider,
                chosen_origin: chosen,
                proxy_port: port,
                cloudflare_front: front,
                auto_renew,
                long_lived,
                renewal_cap: cap,
                hooks,
                state: Mutex::new(state),
                ready: Condvar::new(),
                wake: Condvar::new(),
                #[cfg(test)]
                expired_hold: Mutex::new(None),
            }),
        })
    }
    /// `set_serving_probe`.
    pub fn set_serving_probe(&self, probe: Probe) {
        lock(&self.inner.state).serving_probe = probe;
    }
    /// The relay proxy, once started (`self.proxy`).
    pub fn proxy(&self) -> Option<Arc<Proxy>> {
        lock(&self.inner.state).proxy.clone()
    }
    /// `self._proxy_port` (external only).
    pub fn proxy_port(&self) -> Option<u16> {
        self.inner.proxy_port
    }
    pub fn error_code(&self) -> Option<String> {
        lock(&self.inner.state).error_code.map(str::to_owned)
    }
    /// `status()['public_origin']`.
    pub fn public_origin(&self) -> Option<String> {
        lock(&self.inner.state).public_origin.clone()
    }
    /// `start(acknowledged=, max_seconds=)`.
    pub fn start(&self, acknowledged: &Value, max_seconds: &Value) -> Result<(), &'static str> {
        if acknowledged != &Value::Bool(true) {
            return Err("relay_approval_required");
        }
        let seconds = match max_seconds {
            Value::Number(n) => n.as_i64().filter(|s| (30..=3600).contains(s)),
            _ => None,
        };
        let Some(seconds) = seconds else {
            return Err("relay_budget_invalid");
        };
        {
            let st = lock(&self.inner.state);
            if st.process.is_some() || st.proxy.is_some() || st.closed || st.started {
                return Err("relay_not_startable");
            }
        }
        let outcome = match self.inner.provider {
            Provider::External => self.start_external(seconds),
            Provider::Cloudflare => self.start_quick(seconds),
        };
        if outcome.is_err() && self.inner.provider == Provider::Cloudflare {
            self.close();
        }
        outcome
    }
    fn start_external(&self, seconds: i64) -> Result<(), &'static str> {
        let root = lock(&self.inner.state).root.clone();
        private_directory(&root, true)?;
        let proxy = Proxy::start(
            self.inner.proxy_port.unwrap_or(0),
            self.inner.cloudflare_front,
        )
        .map_err(|_| "relay_proxy_port_unavailable")?;
        let mut st = lock(&self.inner.state);
        if st.closed {
            // closed while it started ([`StartGuard::close`]): nothing listens for it
            drop(st);
            proxy.close();
            return Err("relay_not_startable");
        }
        st.proxy = Some(proxy);
        st.started = true;
        st.public_origin = self.inner.chosen_origin.clone();
        self.start_lifetime(&mut st, seconds);
        Ok(())
    }
    fn start_quick(&self, seconds: i64) -> Result<(), &'static str> {
        let Some(binary) = (self.inner.hooks.binary)() else {
            return Err("relay_client_installation_required");
        };
        let root = lock(&self.inner.state).root.clone();
        private_directory(&root, true)?;
        let home = root.join("isolated-home");
        private_directory(&home, true)?;
        let mut command = Command::new(binary);
        command.env_clear();
        for (k, v) in &self.inner.hooks.env {
            if !(k.starts_with("TUNNEL_")
                || k.starts_with("CLOUDFLARE_")
                || k.starts_with("CF_")
                || k == "NO_TLS_VERIFY")
            {
                command.env(k, v);
            }
        }
        command.env("HOME", &home);
        let proxy = Proxy::start(0, true).map_err(|_| "relay_start_failed")?;
        // A relay closed while it starts ([`StartGuard::close`], the app's Quit) spawns nothing; the
        // state stays locked until the child is stored, so a close that follows always reaps it.
        let mut st = lock(&self.inner.state);
        if st.closed {
            drop(st);
            proxy.close();
            return Err("relay_start_failed");
        }
        st.proxy = Some(proxy.clone());
        let grace = format!("{}s", span(BudgetId::PyRelayCloudflaredGrace).as_secs());
        command
            .args(["--no-autoupdate", "tunnel", "--url"])
            .arg(proxy.origin())
            .args([
                "--protocol",
                "http2",
                "--metrics",
                "127.0.0.1:0",
                "--grace-period",
            ])
            .arg(grace)
            .arg("--management-diagnostics=false")
            .current_dir(&root)
            .stdin(Stdio::null())
            .process_group(0);
        let (read_end, write_end) = std::io::pipe().map_err(|_| "relay_start_failed")?;
        command
            .stdout(write_end.try_clone().map_err(|_| "relay_start_failed")?)
            .stderr(write_end);
        let child = {
            // B12: the client is forked inside the relay's fork boundary.
            let _forks = fork_boundary();
            command.spawn()
        }
        .map_err(|_| "relay_start_failed")?;
        drop(command);
        let weak = self.inner.clone();
        let (ended, reader_ended) = mpsc::channel();
        let reader = thread::Builder::new()
            .name("a13-relay-log".into())
            .spawn(move || {
                read_log(&weak, read_end);
                ended.send(()).unwrap_or(());
            })
            .map_err(|_| "relay_start_failed")?;
        st.process = Some(child);
        st.started = true;
        st.reader = Some((reader, reader_ended));
        // `close` ends the wait: a relay closed while it waits for its origin does not start.
        let (guard, _) = self
            .inner
            .ready
            .wait_timeout_while(st, self.inner.hooks.ready_wait, |s| !s.ready && !s.closed)
            .unwrap_or_else(|p| {
                let (g, t) = p.into_inner();
                (g, t)
            });
        let mut st = guard;
        let exited = st
            .process
            .as_mut()
            .is_none_or(|c| !matches!(c.try_wait(), Ok(None)));
        if !st.ready || st.error_code.is_some() || exited || st.closed {
            return Err("relay_start_failed");
        }
        self.start_lifetime(&mut st, seconds);
        Ok(())
    }
    fn start_lifetime(&self, st: &mut State, seconds: i64) {
        st.window = seconds as f64;
        st.granted = st.window;
        st.deadline = if self.inner.long_lived {
            None
        } else {
            Some((self.inner.hooks.clock)() + st.window)
        };
        if !self.inner.long_lived
            && let Some(poll) = self.inner.hooks.lifetime_poll
        {
            let inner = self.inner.clone();
            st.lifetime = thread::Builder::new()
                .name("a13-relay-lifetime".into())
                .spawn(move || lifetime_loop(&inner, poll))
                .ok();
        }
    }
    /// `_lifetime_step`: renew shortly before expiry while serving (bounded), otherwise close at the
    /// deadline.
    pub fn step(&self) {
        lifetime_step(&self.inner);
    }
    /// What the lifetime thread holds: the relay's own state only, never its host's locks.
    pub fn lifetime(&self) -> Lifetime {
        Lifetime(self.inner.clone())
    }
    /// `status()`.
    pub fn status(&self) -> Json {
        status_of(&self.inner)
    }
    /// `close()`: idempotent; terminates the child, closes the proxy, writes `relay-tail.log`.
    pub fn close(&self) {
        close(&self.inner);
    }
}
impl Drop for Relay {
    fn drop(&mut self) {
        close(&self.inner);
    }
}
/// `TemporaryHttpsRelay.status()`.
fn status_of(inner: &Arc<Inner>) -> Json {
    {
        let mut st = lock(&inner.state);
        let running = match inner.provider {
            Provider::External => st.proxy.is_some() && !st.closed && st.error_code.is_none(),
            Provider::Cloudflare => {
                let alive = st
                    .process
                    .as_mut()
                    .is_some_and(|c| matches!(c.try_wait(), Ok(None)));
                alive && !st.closed && st.error_code.is_none()
            }
        };
        let left = match st.deadline {
            Some(deadline) if !st.closed => Json::from(
                ((deadline - (inner.hooks.clock)())
                    .round_ties_even()
                    .max(0.0)) as i64,
            ),
            _ => Json::Null,
        };
        let mut value = crate::jobj! {
            "state" => if running { "relay_process_running" } else { "stopped" },
            "provider" => if inner.provider == Provider::External { "external" } else { "cloudflare" },
            "public_origin" => st.public_origin.as_deref().map_or(Json::Null, Json::from),
            "error_code" => st.error_code.map_or(Json::Null, Json::from),
            "seed_bound" => st.proxy.as_ref().is_some_and(|p| p.seed_bound()),
            "privacy" => PRIVACY_QUICK,
            "relay_host_trust" => "public_webpki",
            "auto_renew" => inner.auto_renew,
            "long_lived" => inner.long_lived,
            "renewals" => st.renewals,
            "expires_in_seconds" => left,
            "renewal_cap_seconds" => inner.renewal_cap as i64,
            "renewal_stopped" => st.renewal_stopped.map_or(Json::Null, Json::from),
        };
        if inner.provider == Provider::External {
            let host = inner
                .chosen_origin
                .as_deref()
                .unwrap_or("")
                .trim_start_matches("https://");
            let target = format!("http://127.0.0.1:{}", inner.proxy_port.unwrap_or(0));
            value.set(
                "privacy",
                Json::from(format!(
                    "Your own address ({host}) is served by the front you pointed at this Mac (for example a named Cloudflare tunnel or a reverse proxy). That front terminates HTTPS and can observe control traffic, visitor IP addresses and timing, and web chat prompts and replies in plaintext (they cross it); only inference between the Macs uses Iroh. Mycelium starts no tunnel for it: you must point this hostname at {target} yourself."
                )),
            );
            value.set("local_forward_target", Json::from(target));
        }
        value
    }
}

fn read_log(inner: &Arc<Inner>, pipe: std::io::PipeReader) {
    let mut reader = BufReader::new(pipe);
    let ceiling = span_bytes();
    while let Ok(raw) = read_line_limited(&mut reader, ceiling + 1) {
        if raw.is_empty() {
            break;
        }
        if raw.len() > ceiling {
            terminate(inner);
            break;
        }
        let line = String::from_utf8_lossy(&raw);
        if let Some(origin) = provider_event("cloudflare", &line) {
            {
                let mut st = lock(&inner.state);
                st.lines.push_back(format!(
                    "{{\"event\": \"opened\", \"origin\": \"{origin}\"}}\n"
                ));
                let cap = BudgetId::PyMyceliumA13MacosPublicRelayTemporaryhttpsrelayInitMaxlenL320
                    .count()
                    .map_or(0, |c| usize::try_from(c.get()).unwrap_or(0));
                while st.lines.len() > cap {
                    st.lines.pop_front();
                }
            }
            if !observe_origin(inner, &origin) {
                let withdraw = inner.clone();
                thread::Builder::new()
                    .name("a13-relay-withdraw".into())
                    .spawn(move || close(&withdraw))
                    .map(drop)
                    .unwrap_or(());
                break;
            }
        }
    }
}
/// `observe_origin`.
fn observe_origin(inner: &Arc<Inner>, origin: &str) -> bool {
    let mut st = lock(&inner.state);
    match st.public_origin.as_deref() {
        None => {
            st.public_origin = Some(origin.to_owned());
            st.ready = true;
            inner.ready.notify_all();
            true
        }
        Some(current) if current == origin => true,
        Some(_) => {
            st.error_code = Some("relay_origin_changed");
            if let Some(proxy) = &st.proxy {
                proxy.bind_seed(None);
            }
            drop(st);
            terminate(inner);
            false
        }
    }
}
/// `process.terminate()` when it is still running (SIGTERM to the child only).
fn terminate(inner: &Arc<Inner>) {
    let mut st = lock(&inner.state);
    if let Some(child) = st.process.as_mut()
        && matches!(child.try_wait(), Ok(None))
        && let Some(pid) = rustix::process::Pid::from_raw(i32::try_from(child.id()).unwrap_or(0))
    {
        rustix::process::kill_process(pid, rustix::process::Signal::TERM).unwrap_or(());
    }
}
thread_local! {
    /// Set on a relay's own lifetime thread: it never waits for a release, because the closer it
    /// would wait for may be joining it.
    static ON_LIFETIME_THREAD: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}
/// How long a second closer waits for the first one's release: the most the release itself may
/// take (`public_relay.py:446-448` process waits and reader join, `:239` proxy thread join).
fn release_wait() -> Duration {
    [
        BudgetId::PyMyceliumA13MacosPublicRelayTemporaryhttpsrelayCloseSelfProcessWaitL446,
        BudgetId::PyMyceliumA13MacosPublicRelayTemporaryhttpsrelayCloseSelfProcessWaitL447,
        BudgetId::PyMyceliumA13MacosPublicRelayTemporaryhttpsrelayCloseSelfReaderJoinL448,
        BudgetId::PyMyceliumA13MacosPublicRelaySeedrelayproxyCloseSelfThreadJoinL239,
    ]
    .into_iter()
    .map(span)
    .sum()
}
fn lifetime_loop(inner: &Arc<Inner>, poll: Duration) {
    ON_LIFETIME_THREAD.with(|on| on.set(true));
    loop {
        {
            let st = lock(&inner.state);
            if st.closed {
                return;
            }
            let (st, _) = inner
                .wake
                .wait_timeout(st, poll)
                .unwrap_or_else(|p| p.into_inner());
            if st.closed {
                return;
            }
        }
        lifetime_step(inner);
    }
}
fn lifetime_step(inner: &Arc<Inner>) {
    let probe = {
        let st = lock(&inner.state);
        if st.closed || st.deadline.is_none() {
            return;
        }
        st.serving_probe.clone()
    };
    let margin = span(BudgetId::RelayRenewalLead).as_secs_f64();
    let expired = {
        let st = lock(&inner.state);
        let Some(deadline) = st.deadline else { return };
        if st.closed {
            return;
        }
        let now = (inner.hooks.clock)();
        let left = deadline - now;
        if left <= 0.0 {
            true
        } else if inner.auto_renew
            && st.renewal_stopped.is_none()
            && left <= margin.min(st.window / 3.0)
        {
            drop(st);
            let serving = probe().unwrap_or(false);
            let mut st = lock(&inner.state);
            if serving && !st.closed {
                if st.granted + st.window > inner.renewal_cap {
                    st.renewal_stopped = Some("relay_renewal_cap_reached");
                } else {
                    st.granted += st.window;
                    st.renewals += 1;
                    st.deadline = Some(now + st.window);
                }
            }
            return;
        } else {
            return;
        }
    };
    if expired {
        #[cfg(test)]
        if let Some(hold) = lock(&inner.expired_hold).clone() {
            hold();
        }
        close(inner);
    }
}
/// `close()`: like v0.1.4, which holds the relay lock through the whole release
/// (`public_relay.py:439-454`), a caller that finds the relay already closing returns only once the
/// child and the proxy listener are released, so a Start after "stopped" can bind the same port.
/// The relay's own lifetime thread never waits (its closer may be joining it).
fn close(inner: &Arc<Inner>) {
    let (process, proxy, reader, root, lines, lifetime) = {
        let mut st = lock(&inner.state);
        if st.closed {
            if st.released || ON_LIFETIME_THREAD.with(std::cell::Cell::get) {
                return;
            }
            drop(st);
            if let Some(observer) = &inner.hooks.release_observer {
                observer(ReleaseEvent::AwaitingRelease);
            }
            let st = lock(&inner.state);
            let (st, _) = inner
                .wake
                .wait_timeout_while(st, release_wait(), |st| !st.released)
                .unwrap_or_else(|p| p.into_inner());
            if !st.released {
                eprintln!("relay_release_wait_exceeded");
            }
            return;
        }
        st.closed = true;
        inner.wake.notify_all();
        // a start still waiting for its origin gives up now (`Relay::start_quick`)
        inner.ready.notify_all();
        (
            st.process.take(),
            st.proxy.take(),
            st.reader.take(),
            st.root.clone(),
            st.lines.clone(),
            st.lifetime.take(),
        )
    };
    if let Some(observer) = &inner.hooks.release_observer {
        observer(ReleaseEvent::Stopping);
    }
    if let Some(mut child) = process {
        if matches!(child.try_wait(), Ok(None)) {
            if let Some(pid) =
                rustix::process::Pid::from_raw(i32::try_from(child.id()).unwrap_or(0))
            {
                rustix::process::kill_process(pid, rustix::process::Signal::TERM).unwrap_or(());
            }
            reap(
                child,
                span(BudgetId::PyMyceliumA13MacosPublicRelayTemporaryhttpsrelayCloseSelfProcessWaitL446),
                span(BudgetId::PyMyceliumA13MacosPublicRelayTemporaryhttpsrelayCloseSelfProcessWaitL447),
            );
        }
        if let Some((reader, ended)) = reader {
            // `reader.join(2)`: a reader still blocked on the pipe is left to end by itself
            let limit = span(
                BudgetId::PyMyceliumA13MacosPublicRelayTemporaryhttpsrelayCloseSelfReaderJoinL448,
            );
            if ended.recv_timeout(limit).is_ok() {
                reader.join().unwrap_or(());
            }
        }
    }
    if let Some(proxy) = proxy {
        proxy.close();
    }
    if let Some(lifetime) = lifetime
        && lifetime.thread().id() != thread::current().id()
    {
        lifetime.join().unwrap_or(());
    }
    if root.exists() {
        let path = root.join("relay-tail.log");
        let symlink = std::fs::symlink_metadata(&path).is_ok_and(|m| m.file_type().is_symlink());
        if !symlink
            && let Ok(mut file) = std::fs::OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .mode(0o600)
                .open(&path)
        {
            file.write_all(lines.iter().cloned().collect::<String>().as_bytes())
                .unwrap_or(());
        }
    }
    lock(&inner.state).released = true;
    inner.wake.notify_all();
}
/// `process.wait(limit)` then, if it did not end, `kill()` and `wait(limit)` (`public_relay.py:445-449`).
/// The child is owned by a waiter thread so no polling is needed.
fn reap(mut child: Child, term_wait: Duration, kill_wait: Duration) {
    let pid = child.id();
    let (done, ended) = mpsc::channel();
    let waiter = thread::spawn(move || {
        done.send(child.wait()).unwrap_or(());
    });
    if ended.recv_timeout(term_wait).is_err() {
        if let Some(pid) = rustix::process::Pid::from_raw(i32::try_from(pid).unwrap_or(0)) {
            rustix::process::kill_process(pid, rustix::process::Signal::KILL).unwrap_or(());
        }
        ended.recv_timeout(kill_wait).map(drop).unwrap_or(());
    }
    waiter.join().unwrap_or(());
}
/// `authority._private_directory(path, create=True)`: a real directory owned by this user, mode 0700.
fn private_directory(path: &Path, create: bool) -> Result<(), &'static str> {
    const REFUSED: &str = "authority_state_path_invalid";
    if create && !path.exists() && !path.is_symlink() {
        std::fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(path)
            .map_err(|_| REFUSED)?;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))
            .map_err(|_| REFUSED)?;
    }
    let meta = std::fs::symlink_metadata(path).map_err(|_| REFUSED)?;
    if !meta.is_dir()
        || meta.file_type().is_symlink()
        || meta.uid() != rustix::process::getuid().as_raw()
        || meta.permissions().mode() & 0o7777 != 0o700
    {
        return Err(REFUSED);
    }
    Ok(())
}

// ------------------------------------------------------------------------------------------------ owner side

/// The authority and app seams of `DemoApp._start_public_relay` (`app.py:676-740`) that this crate
/// does not own: the swarm authority, the seed server and the owner routes behind the relay.
pub trait EndpointHost {
    /// `authority.status().get('swarm_created')`.
    fn swarm_created(&self) -> bool;
    /// `authority._network_configuration()['public_origin']`, when configured.
    fn configured_public_origin(&self) -> Option<String>;
    /// `authority.configure_public_endpoint(public_origin=, port=, acknowledged=True)` (no swarm yet).
    fn configure_public_endpoint(&mut self, origin: &str, port: u16) -> Result<(), PublicError>;
    /// `endpoint_recovery.attach_public_endpoint` (a swarm created without a relay).
    fn attach_public_endpoint(&mut self, origin: &str, port: u16) -> Result<(), PublicError>;
    /// `endpoint_recovery.relocate_authority` (the swarm moves to a new origin): the signed recovery
    /// card from the old origin to the new one (lane B, B10), `None` from a host that signs none.
    fn relocate_authority(&mut self, origin: &str, port: u16)
    -> Result<Option<Value>, PublicError>;
    /// `authority.seed_server` as the relay's verified-TLS hop; `None` while no seed server exists.
    fn seed_backend(&self) -> Option<Arc<dyn SeedBackend>>;
    /// `self.running = True`.
    fn set_running(&mut self);
    /// `_bind_public_routes` (pairing-window, chat and swarm-entry handlers).
    fn bind_public_routes(&mut self, proxy: &Arc<Proxy>);
    /// `_materialize_pending_windows`.
    fn materialize_pending_windows(&mut self);
    /// `getattr(self, 'local_contributor', None) is not None`: the owner's own contributor is
    /// enrolled (whatever its Session slot holds while a renewal has it out). A host without one
    /// answers `false`.
    fn local_contributor_present(&self) -> bool {
        false
    }
}

/// `DemoApp.public_relay` and the relay-related methods of the app.
pub struct OwnerRelay {
    root: PathBuf,
    relay: Option<Relay>,
    hooks: Box<dyn Fn() -> Hooks + Send>,
    guard: Option<StartGuard>,
}

/// The app's hold on the relay [`OwnerRelay::start_public_relay`] is bringing up. v0.1.4 runs
/// Start and `close` under one `self.guard` (`_action`, app.py:1344; `DemoApp.close`, app.py:1831),
/// so Quit closes the relay a Start brings up, and once closed every action answers
/// `application_closed` (app.py:1345). Here the start does not hold the app's locks while it waits
/// for its origin: [`StartGuard::close`] closes the relay being started at once instead (its ready
/// wait ends, a child it spawned is reaped within the relay's own close budgets) and every later
/// start is refused `application_closed` before it spawns anything.
#[derive(Clone, Default)]
pub struct StartGuard(Arc<Mutex<Guarded>>);
#[derive(Default)]
struct Guarded {
    closed: bool,
    /// The relay most recently adopted (closing a closed relay again does nothing).
    relay: Option<Arc<Inner>>,
}
impl StartGuard {
    /// Close the relay being started (if any) and refuse every later start.
    pub fn close(&self) {
        let relay = {
            let mut guarded = lock(&self.0);
            guarded.closed = true;
            guarded.relay.take()
        };
        if let Some(inner) = relay {
            close(&inner);
        }
    }
    pub fn closed(&self) -> bool {
        lock(&self.0).closed
    }
    /// The relay a start is about to bring up; refused (and closed) once the guard closed.
    fn adopt(&self, relay: &Relay) -> Result<(), PublicError> {
        let mut guarded = lock(&self.0);
        if guarded.closed {
            drop(guarded);
            relay.close();
            return Err(PublicError::new(400, "application_closed"));
        }
        // Adopting a new relay must close the one it displaces. The guard holds `Arc<Inner>`, and
        // `Inner` has no `Drop` — only `Relay` does — so simply overwriting the slot orphaned the
        // previous relay's child process (`cloudflared`), which then outlived every later Quit.
        let previous = guarded.relay.replace(relay.inner.clone());
        drop(guarded);
        if let Some(previous) = previous.filter(|p| !Arc::ptr_eq(p, &relay.inner)) {
            close(&previous);
        }
        Ok(())
    }
}
fn value_error() -> PublicError {
    // a builtin `ValueError` escapes to the handler: 409 `action_not_completed`
    PublicError::new(409, "action_not_completed").raised_by(Raised::Builtin("ValueError"))
}
fn free_seed_port(avoid: Option<u16>) -> Result<u16, PublicError> {
    loop {
        let port = TcpListener::bind(("127.0.0.1", 0))
            .and_then(|l| l.local_addr())
            .map_err(|_| PublicError::new(409, "action_not_completed"))?
            .port();
        if Some(port) != avoid {
            return Ok(port);
        }
    }
}
impl OwnerRelay {
    /// `root` is the app's state root; the relay keeps its files under `<root>/public-relay`.
    pub fn new(root: &Path, hooks: impl Fn() -> Hooks + Send + 'static) -> Self {
        Self {
            root: root.to_path_buf(),
            relay: None,
            hooks: Box::new(hooks),
            guard: None,
        }
    }
    /// The relay each later start brings up is adopted by `guard` (the app's Quit closes it).
    pub fn set_start_guard(&mut self, guard: StartGuard) {
        self.guard = Some(guard);
    }
    /// `self.public_relay` and its `status()['state']`.
    pub fn state(&self) -> RelayState {
        match &self.relay {
            None => RelayState::Absent,
            Some(relay) => {
                if relay.is_stopped() {
                    RelayState::Stopped
                } else {
                    RelayState::Running
                }
            }
        }
    }
    /// `self.public_relay.close(); self.public_relay = None`.
    pub fn close(&mut self) {
        if let Some(relay) = self.relay.take() {
            relay.close();
        }
    }
    /// `public_relay.status().get('public_origin')`; `None` for an absent or empty origin.
    pub fn origin(&self) -> Option<String> {
        self.relay
            .as_ref()
            .and_then(Relay::public_origin)
            .filter(|o| !o.is_empty())
    }
    /// The `network` block of `GET /api/status`.
    pub fn network_status(&self) -> Json {
        match &self.relay {
            None => crate::jobj! {"state" => "private"},
            Some(relay) => relay.status(),
        }
    }
    /// The running relay's lifetime handle (what its lifetime thread steps), if there is one.
    pub fn lifetime(&self) -> Option<Lifetime> {
        self.relay.as_ref().map(Relay::lifetime)
    }
    /// `self.public_relay.proxy.bind_seed(self.authority.seed_server)` after the seed exists.
    pub fn bind_seed(&self, backend: Option<Arc<dyn SeedBackend>>) {
        if let Some(proxy) = self.relay.as_ref().and_then(Relay::proxy) {
            proxy.bind_seed(backend);
        }
    }
    /// `_new_relay` (`app.py:754-768`): a not-yet-started relay for a validated request body; bad
    /// owner-chosen values are bounded `DemoError` codes (400).
    fn new_relay(&self, body: &Map<String, Value>, probe: &Probe) -> Result<Relay, PublicError> {
        let external = body.get("provider").is_some_and(|v| v == "external");
        let relay_root = self.root.join("public-relay");
        let relay = if external {
            Relay::new(
                &relay_root,
                &Value::String("external".into()),
                body,
                (self.hooks)(),
            )
            .map_err(|code| PublicError::new(400, code))?
        } else {
            let provider = body
                .get("provider")
                .cloned()
                .unwrap_or(Value::String("cloudflare".into()));
            Relay::new(&relay_root, &provider, &Map::new(), (self.hooks)())
                .map_err(|code| PublicError::new(400, code))?
        };
        relay.set_serving_probe(probe.clone());
        Ok(relay)
    }
    /// `_start_public_relay` (`app.py:676-740`) after the closed/role/shape checks the persistent
    /// backend runs. A running relay refuses (`network_requires_fresh_owner`); one whose window ran
    /// out is closed and forgotten so the same Start reopens it. Any failure closes the relay and
    /// leaves the swarm as it was.
    pub fn start_public_relay(
        &mut self,
        body: &Map<String, Value>,
        host: &mut dyn EndpointHost,
        probe: Probe,
    ) -> Result<Json, PublicError> {
        match self.state() {
            RelayState::Running => {
                return Err(PublicError::new(400, "network_requires_fresh_owner"));
            }
            RelayState::Stopped => self.close(),
            RelayState::Absent => {}
        }
        let external = body.get("provider").is_some_and(|v| v == "external");
        let existing = host.swarm_created();
        let relay = self.new_relay(body, &probe)?;
        if let Some(guard) = &self.guard {
            guard.adopt(&relay)?;
        }
        let outcome = (|| -> Result<(), PublicError> {
            relay
                .start(&body["relay_acknowledged"], &body["max_seconds"])
                .map_err(|_| value_error())?;
            let origin = relay.public_origin().unwrap_or_default();
            let port = free_seed_port(relay.proxy_port())?;
            let configured = if existing {
                host.configured_public_origin()
            } else {
                None
            };
            if external && configured.as_deref() == Some(origin.as_str()) {
                // a stable owner-chosen address that did not change: the retained authority answers on it
                if let Some(proxy) = relay.proxy() {
                    proxy.bind_seed(host.seed_backend());
                }
                host.set_running();
            } else if !existing {
                host.configure_public_endpoint(&origin, port)?;
            } else {
                if host.configured_public_origin().is_none() {
                    host.attach_public_endpoint(&origin, port)?;
                } else {
                    host.relocate_authority(&origin, port)?;
                }
                if let Some(proxy) = relay.proxy() {
                    proxy.bind_seed(host.seed_backend());
                }
                host.set_running();
            }
            Ok(())
        })();
        if let Err(error) = outcome {
            relay.close();
            return Err(error);
        }
        if external {
            self.remember_external_address(body)?;
        }
        if let Some(proxy) = relay.proxy() {
            host.bind_public_routes(&proxy);
        }
        host.materialize_pending_windows();
        let status = relay.status();
        self.relay = Some(relay);
        Ok(status)
    }
    /// `/api/network/restart` (app.py:1444-1475 @ f4e366e9) after the persistent backend's closed
    /// check and the dispatch's role/swarm/shape check (lane B, B10): an external restart takes the
    /// address it does not name from the remembered one; the new relay is validated before the old
    /// one is closed; once it runs, the seed moves to its origin with a signed recovery card
    /// (`relocate_authority`), unless an external address did not change (no card then). Any
    /// failure after the start closes the new relay. Answers `{'network', 'recovery_card'}`.
    pub fn restart_public_relay(
        &mut self,
        body: &Map<String, Value>,
        host: &mut dyn EndpointHost,
        probe: Probe,
    ) -> Result<Json, PublicError> {
        let external = body.get("provider").is_some_and(|v| v == "external");
        let spec = if external {
            let mut spec = self.saved_external_address()?;
            spec.extend(body.clone());
            spec
        } else {
            body.clone()
        };
        // app.py:1451-1453: a present contributor refuses any restart that would relocate the
        // seed it is enrolled on (lane B review B-01): only an external restart on the origin the
        // retained authority answers on may proceed, judged on the merged spec, before anything
        // is closed, configured, signed or reopened.
        if host.local_contributor_present() {
            let unchanged = external
                && host.configured_public_origin().is_some_and(|configured| {
                    crate::join_link::validate_external_origin(
                        spec.get("public_origin").unwrap_or(&Value::Null),
                    )
                    .is_ok_and(|origin| origin == configured)
                });
            if !unchanged {
                return Err(PublicError::new(
                    400,
                    "network_stop_local_contributor_required",
                ));
            }
        }
        if external && !(spec.contains_key("public_origin") && spec.contains_key("proxy_port")) {
            return Err(PublicError::new(400, "relay_origin_invalid"));
        }
        // validated before the old relay is touched
        let fresh = self.new_relay(&spec, &probe)?;
        if let Some(old) = self.relay.take() {
            old.close();
        }
        if let Some(guard) = &self.guard {
            guard.adopt(&fresh)?;
        }
        fresh
            .start(&body["relay_acknowledged"], &body["max_seconds"])
            .map_err(|_| value_error())?;
        let outcome = (|| -> Result<Option<Value>, PublicError> {
            let origin = fresh.public_origin().unwrap_or_default();
            let port = free_seed_port(fresh.proxy_port())?;
            let card = if external && host.configured_public_origin().as_deref() == Some(&origin) {
                None // the address did not change: members need no recovery card
            } else {
                host.relocate_authority(&origin, port)?
            };
            if let Some(proxy) = fresh.proxy() {
                proxy.bind_seed(host.seed_backend());
            }
            Ok(card)
        })();
        let card = match outcome {
            Ok(card) => card,
            Err(error) => {
                fresh.close();
                return Err(error);
            }
        };
        host.set_running();
        if external {
            self.remember_external_address(&spec)?;
        }
        if let Some(proxy) = fresh.proxy() {
            host.bind_public_routes(&proxy);
        }
        let network = fresh.status();
        self.relay = Some(fresh);
        Ok(Json::from(serde_json::json!({
            "network": network.to_value(),
            "recovery_card": card,
        })))
    }
    /// `_saved_external_address`: what `_remember_external_address` kept, `{}` when nothing.
    fn saved_external_address(&self) -> Result<Map<String, Value>, PublicError> {
        let limit = crate::preferences::private_state::private_json_limit().unwrap_or(0);
        crate::preferences::private_state::load(&self.root.join("external-origin.json"), limit)
            .map(Option::unwrap_or_default)
            .map_err(|e| PublicError::new(409, e.0).raised_by(Raised::Authority))
    }
    /// `_remember_external_address`: kept so a later restart reuses it.
    fn remember_external_address(&self, spec: &Map<String, Value>) -> Result<(), PublicError> {
        let value = serde_json::json!({
            "public_origin": spec["public_origin"],
            "proxy_port": spec["proxy_port"],
            "cloudflare_front": spec.get("cloudflare_front") == Some(&Value::Bool(true)),
        });
        let limit = crate::preferences::private_state::private_json_limit().unwrap_or(0);
        crate::preferences::private_state::store(
            &self.root.join("external-origin.json"),
            &value,
            limit,
        )
        .map_err(|e| PublicError::new(409, e.0).raised_by(Raised::Authority))
    }
}
impl Relay {
    /// `status()['state'] == 'stopped'`.
    pub fn is_stopped(&self) -> bool {
        let inner = &self.inner;
        let mut st = lock(&inner.state);
        let running = match inner.provider {
            Provider::External => st.proxy.is_some() && !st.closed && st.error_code.is_none(),
            Provider::Cloudflare => {
                let alive = st
                    .process
                    .as_mut()
                    .is_some_and(|c| matches!(c.try_wait(), Ok(None)));
                alive && !st.closed && st.error_code.is_none()
            }
        };
        !running
    }
}

#[cfg(test)]
mod lifetime_guard_tests {
    //! GRF2 (review F6): the relay's own lifetime thread never waits for a release. When it has
    //! decided the window expired and another closer (Stop, Quit, a Start) wins the close, that
    //! closer joins the lifetime thread; were the lifetime thread to wait for the closer's release
    //! it would stall both for the whole release wait (`release_wait`, 20 s). The order cannot be
    //! forced through the public seams (the clock is read under the state lock, and the release
    //! observer fires only inside `close()`), hence the unit-test-only `expired_hold`.
    use super::*;
    use std::sync::atomic::AtomicU64;

    #[derive(Debug, PartialEq)]
    enum Seen {
        /// The lifetime thread decided the window expired and is held before its `close()`.
        Expired,
        Release(ReleaseEvent),
        /// The other closer's `close()` returned.
        Closed,
    }

    #[test]
    fn a_lifetime_thread_whose_close_is_won_by_another_closer_returns_without_waiting() {
        let root =
            std::env::temp_dir().join(format!("relay-lifetime-guard-{}", std::process::id()));
        let port = TcpListener::bind("127.0.0.1:0")
            .and_then(|l| l.local_addr())
            .map(|a| a.port())
            .expect("an ephemeral loopback port");
        let now_ms = Arc::new(AtomicU64::new(1_000_000));
        let (seen, events) = mpsc::channel::<Seen>();
        let clock = now_ms.clone();
        let observed = Mutex::new(seen.clone());
        let hooks = Hooks {
            clock: Arc::new(move || clock.load(Ordering::SeqCst) as f64 / 1000.0),
            lifetime_poll: Some(span(BudgetId::RelayPoll)),
            ready_wait: span(BudgetId::RelayPoll),
            binary: Arc::new(|| None),
            env: Vec::new(),
            release_observer: Some(Arc::new(move |event| {
                lock(&observed).send(Seen::Release(event)).unwrap_or(());
            })),
        };
        let spec: Map<String, Value> = serde_json::from_value(serde_json::json!({
            "public_origin": "https://relay.mycelium-test.org",
            "proxy_port": port,
            "auto_renew": false,
        }))
        .expect("spec");
        let relay = Relay::new(&root, &Value::from("external"), &spec, hooks).expect("relay");
        let (release, released) = mpsc::channel::<()>();
        let held = Mutex::new((seen.clone(), released));
        *lock(&relay.inner.expired_hold) = Some(Arc::new(move || {
            let held = lock(&held);
            held.0.send(Seen::Expired).unwrap_or(());
            held.1.recv().unwrap_or(());
        }));
        relay
            .start(&Value::Bool(true), &Value::from(30))
            .expect("external relay started");
        let wait = span(BudgetId::RouterClose);
        // The 30 s window runs out; wake the lifetime thread (it would also wake on its next poll).
        now_ms.fetch_add(30_001, Ordering::SeqCst);
        relay.inner.wake.notify_all();
        assert_eq!(events.recv_timeout(wait), Ok(Seen::Expired));
        // Another closer wins: it publishes "stopped", then joins the held lifetime thread.
        let closer = thread::spawn(move || {
            relay.close();
            seen.send(Seen::Closed).unwrap_or(());
            relay
        });
        assert_eq!(
            events.recv_timeout(wait),
            Ok(Seen::Release(ReleaseEvent::Stopping))
        );
        // The lifetime thread goes on to its own close(): it must return at once, so the
        // closer's join (and its release) completes.
        release.send(()).expect("the lifetime thread is held");
        assert_eq!(
            events.recv_timeout(wait),
            Ok(Seen::Closed),
            "the lifetime thread must not wait for a release its closer joins it for"
        );
        let relay = closer.join().expect("closer");
        assert_eq!(relay.status().to_value()["state"], "stopped");
        assert!(events.try_recv().is_err(), "no closer waited for a release");
        drop(relay);
        std::fs::remove_dir_all(&root).expect("scratch root");
    }
}

#[cfg(test)]
mod member_work_route_tests {
    //! A joined Mac registers its packaged work binding at
    //! `stage_contract::member_work::PATH`, which the owner's seed serves from
    //! `member_control::joined::inbox::Inbox`. The relay forwards only the paths in its own
    //! table, so a missing prefix answers `relay_route_unknown` (404) and the member's
    //! publication loop retries forever.
    use super::*;

    #[test]
    fn the_member_work_registration_is_forwarded_and_not_refused_as_unknown() {
        assert!(!route_unknown(stage_contract::member_work::PATH));
        assert!(!route_unknown("/member-work/v1/register"));
        assert!(route_unknown("/member-work"));
        assert!(route_unknown("/member-work/../seed/identity"));
    }
}
