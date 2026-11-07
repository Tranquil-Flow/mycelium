// SPDX-License-Identifier: AGPL-3.0-only
//! Process-boundary pieces of the v0.1.4 sidecar (`native/iroh_transport/src/sidecar.rs`,
//! `local_ipc.rs`, `ready_address.rs @ f4e366e9`): the bootstrap secret handed over an
//! inherited pipe, the endpoint-secret file, the owner-only Unix-domain socket, the
//! loopback-channel owner authorization, the ready / address-changed lines and the address
//! readiness gate. The listener loops live in [`crate::sidecar_listener`]. Every behaviour is
//! pinned by vectors recorded by executing the oracle's own functions
//! (`fixtures/sidecar-oracle-vectors.json`).
//!
//! This module is safe code (the crate guard `spawn_boundary_ownership` forbids
//! an unsafe-code allowance in `runtime`). The one operation that needs `unsafe` in the oracle,
//! turning the inherited `--bootstrap-fd N` number into an owned descriptor
//! (`File::from_raw_fd`), lives in `node::fds` (seam S-L9-2): [`read_bootstrap_descriptor`]
//! adopts the number through `adopt_inherited_descriptor` and fails closed where the oracle
//! aborts (exit 134, `r9-oracle-closed-fd.txt`). The parent side, [`spawn_sidecar`], hands the
//! pipe's read end to the child only through `NativeChild::spawn_passing`, the single spawn
//! boundary (v0.1.4's `pass_fds`). Secrets are wiped best-effort (zero fill kept alive with
//! `black_box`) instead of `zeroize`.
use iroh::EndpointAddr;
use node::{
    WorkerError,
    fds::{AdoptionRefusal, DescriptorKind, adopt_inherited_descriptor, refusal},
    process::NativeChild,
};
use rustix::{fs::OFlags, process::geteuid};
use std::{
    fmt,
    fs::{self, File},
    future::Future,
    io::{self, Read, Write},
    net::{Ipv4Addr, SocketAddr, SocketAddrV4},
    os::{
        fd::{AsRawFd, OwnedFd, RawFd},
        unix::fs::{FileTypeExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    },
    path::{Path, PathBuf},
    pin::pin,
    process::Command,
    time::Duration,
};
use tokio::{
    net::{TcpListener, TcpSocket, TcpStream, UnixListener, UnixStream},
    sync::watch,
    time::timeout,
};

/// Length of the bootstrap and endpoint secrets.
pub const SECRET_BYTES: usize = 32;
const LOOPBACK_BACKLOG: u32 = 16;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SidecarError {
    InvalidBootstrapFd,
    BootstrapNotPipe,
    InvalidBootstrapLength,
    Filesystem,
    SocketSecurity,
    SocketBind,
    EndpointBind,
    EndpointSecretSecurity,
    InvalidNetworkMode,
    ReadyOutput,
    Runtime,
    ArtifactSource,
    /// A network mode this entry does not serve (only `--local-only`; RU-022).
    NetworkModeUnsupported,
}
impl SidecarError {
    pub const fn code(self) -> &'static str {
        match self {
            Self::InvalidBootstrapFd => "invalid_bootstrap_fd",
            Self::BootstrapNotPipe => "bootstrap_not_pipe",
            Self::InvalidBootstrapLength => "invalid_bootstrap_length",
            Self::Filesystem => "filesystem_error",
            Self::SocketSecurity => "socket_security_error",
            Self::SocketBind => "socket_bind_error",
            Self::EndpointBind => "endpoint_bind_error",
            Self::EndpointSecretSecurity => "endpoint_secret_security_error",
            Self::InvalidNetworkMode => "invalid_network_mode",
            Self::ReadyOutput => "ready_output_error",
            Self::Runtime => "runtime_error",
            Self::ArtifactSource => "artifact_source_invalid",
            Self::NetworkModeUnsupported => "network_mode_unsupported",
        }
    }
}
impl fmt::Display for SidecarError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code())
    }
}
impl std::error::Error for SidecarError {}

/// 32 secret bytes; wiped on drop, never printed.
pub struct BootstrapSecret([u8; SECRET_BYTES]);
impl BootstrapSecret {
    /// An owned copy of already-validated secret bytes (the caller wipes its own).
    pub const fn from_bytes(bytes: [u8; SECRET_BYTES]) -> Self {
        Self(bytes)
    }
    pub fn as_bytes(&self) -> &[u8; SECRET_BYTES] {
        &self.0
    }
}
impl fmt::Debug for BootstrapSecret {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("BootstrapSecret([REDACTED])")
    }
}
fn wipe(bytes: &mut [u8]) {
    bytes.fill(0);
    std::hint::black_box(&*bytes);
}
impl Drop for BootstrapSecret {
    fn drop(&mut self) {
        wipe(&mut self.0);
    }
}

fn exactly_32<R: Read>(reader: R) -> Option<BootstrapSecret> {
    let mut bytes = Vec::with_capacity(SECRET_BYTES + 1);
    let read = reader.take(SECRET_BYTES as u64 + 1).read_to_end(&mut bytes);
    if read.is_err() || bytes.len() != SECRET_BYTES {
        wipe(&mut bytes);
        return None;
    }
    let mut secret = [0_u8; SECRET_BYTES];
    secret.copy_from_slice(&bytes);
    wipe(&mut bytes);
    Some(BootstrapSecret(secret))
}

/// Exactly 32 bytes then EOF (local_ipc.rs:39-54); short, long and trailing input refused.
pub fn parse_bootstrap_secret<R: Read>(reader: R) -> Result<BootstrapSecret, SidecarError> {
    exactly_32(reader).ok_or(SidecarError::InvalidBootstrapLength)
}

/// Consumes an inherited pipe descriptor (sidecar.rs:234-262): it must be a FIFO carrying
/// exactly 32 bytes before EOF, and it is closed on success and on every failure. The raw
/// `--bootstrap-fd` number must first be adopted into an `OwnedFd` (seam S-L9-2, see the
/// module note).
pub fn read_bootstrap_secret(pipe: OwnedFd) -> Result<BootstrapSecret, SidecarError> {
    let pipe = File::from(pipe);
    let metadata = pipe
        .metadata()
        .map_err(|_| SidecarError::InvalidBootstrapFd)?;
    if !metadata.file_type().is_fifo() {
        return Err(SidecarError::BootstrapNotPipe);
    }
    exactly_32(pipe).ok_or(SidecarError::InvalidBootstrapLength)
}

/// The raw `--bootstrap-fd N` of the sidecar (seam S-L9-2): adopted through the audited
/// `node::fds::adopt_inherited_descriptor` (fail closed), then read as in
/// [`read_bootstrap_secret`]. Call once at start-up, before other threads open or close
/// descriptors. Deliberate deviations from v0.1.4 (`sidecar.rs:234-262`): a closed or never-open
/// number is refused as `invalid_bootstrap_fd` where the oracle aborts (SIGABRT, exit 134,
/// `r9-oracle-closed-fd.txt`), descriptors 0-2 are refused where the oracle adopts them, and a
/// number adopted twice is refused where the oracle has two owners of one descriptor.
pub fn read_bootstrap_descriptor(fd: std::os::fd::RawFd) -> Result<BootstrapSecret, SidecarError> {
    let pipe = adopt_inherited_descriptor(fd, DescriptorKind::Fifo).map_err(|error| {
        match refusal(&error).map(AdoptionRefusal::sidecar_code) {
            Some("bootstrap_not_pipe") => SidecarError::BootstrapNotPipe,
            // Every other refusal, and any error that is not a refusal, fails closed.
            _ => SidecarError::InvalidBootstrapFd,
        }
    })?;
    read_bootstrap_secret(pipe)
}

/// Parent side of `--bootstrap-fd N` (v0.1.4 `pass_fds`): the 32 secret bytes are written into
/// a fresh pipe whose write end is closed before the fork, then the read end is handed to the
/// child only through `NativeChild::spawn_passing` (the single spawn boundary; never a
/// hand-rolled fork hook). `attach` receives the read end's number to put on the child's
/// command line. The parent's copy of the read end lives until `spawn_passing` returned.
pub fn spawn_with_bootstrap(
    command: &mut Command,
    root: &Path,
    secret: &[u8; SECRET_BYTES],
    attach: impl FnOnce(&mut Command, RawFd),
) -> Result<NativeChild, WorkerError> {
    let (reader, writer) = rustix::pipe::pipe().map_err(io::Error::from)?;
    let mut writer = File::from(writer);
    writer.write_all(secret)?;
    drop(writer);
    let number = reader.as_raw_fd();
    attach(command, number);
    NativeChild::spawn_passing(command, root, &[number])
}
/// [`spawn_with_bootstrap`] with the shipped attachment: `--bootstrap-fd <number>`.
pub fn spawn_sidecar(
    command: &mut Command,
    root: &Path,
    secret: &[u8; SECRET_BYTES],
) -> Result<NativeChild, WorkerError> {
    spawn_with_bootstrap(command, root, secret, |command, fd| {
        command.arg("--bootstrap-fd").arg(fd.to_string());
    })
}

/// 32 bytes from an owned, regular, mode-0600 file opened without following links
/// (sidecar.rs:269-300).
pub fn read_endpoint_secret(path: &Path) -> Result<BootstrapSecret, SidecarError> {
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(OFlags::NOFOLLOW.bits() as i32)
        .open(path)
        .map_err(|_| SidecarError::EndpointSecretSecurity)?;
    let metadata = file
        .metadata()
        .map_err(|_| SidecarError::EndpointSecretSecurity)?;
    if !metadata.file_type().is_file()
        || metadata.uid() != geteuid().as_raw()
        || metadata.mode() & 0o7777 != 0o600
        || metadata.len() != SECRET_BYTES as u64
    {
        return Err(SidecarError::EndpointSecretSecurity);
    }
    exactly_32(file).ok_or(SidecarError::EndpointSecretSecurity)
}

/// Binds an owner-only Unix-domain socket (sidecar.rs:869-910): the parent is created, must
/// be a real directory owned by us and is forced to 0700; a stale socket of ours is replaced,
/// anything else at the path is refused; the socket is set to 0600 and re-checked. Needs a
/// Tokio runtime context.
pub fn bind_secure_uds(path: &Path) -> Result<UnixListener, SidecarError> {
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    fs::create_dir_all(parent).map_err(|_| SidecarError::Filesystem)?;
    let parent_metadata = fs::symlink_metadata(parent).map_err(|_| SidecarError::Filesystem)?;
    let effective_uid = geteuid().as_raw();
    if !parent_metadata.file_type().is_dir() || parent_metadata.file_type().is_symlink() {
        return Err(SidecarError::SocketSecurity);
    }
    if parent_metadata.uid() != effective_uid {
        return Err(SidecarError::SocketSecurity);
    }
    fs::set_permissions(parent, fs::Permissions::from_mode(0o700))
        .map_err(|_| SidecarError::SocketSecurity)?;
    let restricted = fs::symlink_metadata(parent).map_err(|_| SidecarError::Filesystem)?;
    if restricted.permissions().mode() & 0o777 != 0o700 {
        return Err(SidecarError::SocketSecurity);
    }
    // A missing path is the normal first start; a stale socket of ours is replaced; anything
    // else (or an unreadable path) is refused.
    let stale = match fs::symlink_metadata(path) {
        Ok(metadata) => Some(metadata),
        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
        Err(_) => return Err(SidecarError::Filesystem),
    };
    if let Some(metadata) = stale {
        if !metadata.file_type().is_socket() || metadata.uid() != effective_uid {
            return Err(SidecarError::SocketSecurity);
        }
        fs::remove_file(path).map_err(|_| SidecarError::SocketSecurity)?;
    }
    let listener = UnixListener::bind(path).map_err(|_| SidecarError::SocketBind)?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))
        .map_err(|_| SidecarError::SocketSecurity)?;
    let socket = fs::symlink_metadata(path).map_err(|_| SidecarError::Filesystem)?;
    if socket.uid() != effective_uid || socket.permissions().mode() & 0o777 != 0o600 {
        return Err(SidecarError::SocketSecurity);
    }
    Ok(listener)
}

/// A connection is admitted only when the peer's uid is ours (sidecar.rs:913-919).
pub fn same_effective_uid(stream: &UnixStream) -> bool {
    stream
        .peer_cred()
        .is_ok_and(|credentials| credentials.uid() == geteuid().as_raw())
}

/// Removes the socket file when dropped, and only a socket (sidecar.rs:922-931).
pub struct SocketCleanup(pub PathBuf);
impl Drop for SocketCleanup {
    fn drop(&mut self) {
        if fs::symlink_metadata(&self.0).is_ok_and(|m| m.file_type().is_socket())
            && fs::remove_file(&self.0).is_err()
        {
            // The path is gone or no longer ours; nothing further to remove.
        }
    }
}

// ---- loopback channel (local_ipc.rs) --------------------------------------------------

/// A bound `127.0.0.1` listener on an ephemeral port.
pub struct LoopbackListener {
    listener: TcpListener,
    local: SocketAddrV4,
}
impl LoopbackListener {
    pub fn bind() -> Result<Self, SidecarError> {
        let socket = TcpSocket::new_v4().map_err(|_| SidecarError::SocketBind)?;
        socket
            .bind(SocketAddr::V4(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0)))
            .map_err(|_| SidecarError::SocketBind)?;
        let listener = socket
            .listen(LOOPBACK_BACKLOG)
            .map_err(|_| SidecarError::SocketBind)?;
        let local = match listener.local_addr() {
            Ok(SocketAddr::V4(local)) if *local.ip() == Ipv4Addr::LOCALHOST => local,
            _ => return Err(SidecarError::SocketSecurity),
        };
        Ok(Self { listener, local })
    }
    pub const fn local_addr(&self) -> SocketAddrV4 {
        self.local
    }
    pub const fn local_port(&self) -> u16 {
        self.local.port()
    }
    /// Accepts one connection with Nagle off (every record is a prefix then a body).
    pub async fn accept(&self) -> io::Result<(TcpStream, SocketAddr)> {
        let (stream, peer) = self.listener.accept().await?;
        stream.set_nodelay(true)?;
        Ok((stream, peer))
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct TcpOwnerRow {
    pub local: SocketAddrV4,
    pub remote: SocketAddrV4,
    pub owning_pid: u32,
}
/// The host's view of which process owns each established IPv4 TCP connection.
pub trait ConnectionOwnerTable: Send + Sync {
    fn rows(&self) -> io::Result<Vec<TcpOwnerRow>>;
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LoopbackRejection {
    NotLoopback,
    OwnerLookupFailed,
    OwnerUnknown,
    ForeignOwner,
    AmbiguousOwner,
    InvalidPolicy,
}
impl LoopbackRejection {
    pub const fn code(self) -> &'static str {
        match self {
            Self::NotLoopback => "loopback_peer_not_loopback",
            Self::OwnerLookupFailed => "loopback_owner_lookup_failed",
            Self::OwnerUnknown => "loopback_owner_unknown",
            Self::ForeignOwner => "loopback_owner_foreign",
            Self::AmbiguousOwner => "loopback_owner_ambiguous",
            Self::InvalidPolicy => "loopback_policy_invalid",
        }
    }
}
/// Admits a connection only when its client end is owned by `allowed_pid` and by no other
/// process (local_ipc.rs:216-247). Any lookup failure refuses.
pub fn authorize_loopback_client(
    table: &dyn ConnectionOwnerTable,
    client: SocketAddr,
    server: SocketAddr,
    allowed_pid: u32,
) -> Result<(), LoopbackRejection> {
    if allowed_pid == 0 {
        return Err(LoopbackRejection::InvalidPolicy);
    }
    let (SocketAddr::V4(client), SocketAddr::V4(server)) = (client, server) else {
        return Err(LoopbackRejection::NotLoopback);
    };
    if !client.ip().is_loopback() || *server.ip() != Ipv4Addr::LOCALHOST {
        return Err(LoopbackRejection::NotLoopback);
    }
    let rows = table
        .rows()
        .map_err(|_| LoopbackRejection::OwnerLookupFailed)?;
    let mut owners = rows
        .iter()
        .filter(|row| row.local == client && row.remote == server)
        .map(|row| row.owning_pid)
        .collect::<Vec<_>>();
    owners.sort_unstable();
    owners.dedup();
    match owners.as_slice() {
        [] => Err(LoopbackRejection::OwnerUnknown),
        [owner] if *owner == allowed_pid => Ok(()),
        [_] => Err(LoopbackRejection::ForeignOwner),
        _ => Err(LoopbackRejection::AmbiguousOwner),
    }
}

const OWNER_PID_ROW_BYTES: usize = 6 * 4;
const OWNER_PID_TABLE_HEADER_BYTES: usize = 4;
/// Decodes a `MIB_TCPTABLE_OWNER_PID` buffer: little-endian row count, then
/// `{state, local addr, local port, remote addr, remote port, pid}` rows with addresses and
/// the low 16 bits of ports in network order (local_ipc.rs:258-290).
pub fn decode_owner_pid_table(buffer: &[u8]) -> io::Result<Vec<TcpOwnerRow>> {
    let invalid = || io::Error::new(io::ErrorKind::InvalidData, "tcp owner table");
    let word = |offset: usize| -> io::Result<[u8; 4]> {
        buffer
            .get(offset..offset + 4)
            .and_then(|bytes| bytes.try_into().ok())
            .ok_or_else(invalid)
    };
    let count = u32::from_le_bytes(word(0)?) as usize;
    let needed = count
        .checked_mul(OWNER_PID_ROW_BYTES)
        .and_then(|rows| rows.checked_add(OWNER_PID_TABLE_HEADER_BYTES))
        .ok_or_else(invalid)?;
    if needed > buffer.len() {
        return Err(invalid());
    }
    let endpoint = |address: [u8; 4], port: [u8; 4]| {
        SocketAddrV4::new(
            Ipv4Addr::from(address),
            u16::from_be_bytes([port[0], port[1]]),
        )
    };
    (0..count)
        .map(|index| {
            let row = OWNER_PID_TABLE_HEADER_BYTES + index * OWNER_PID_ROW_BYTES;
            Ok(TcpOwnerRow {
                local: endpoint(word(row + 4)?, word(row + 8)?),
                remote: endpoint(word(row + 12)?, word(row + 16)?),
                owning_pid: u32::from_le_bytes(word(row + 20)?),
            })
        })
        .collect()
}
/// `netstat -anv -p tcp` rows (macOS): only `tcp4` rows whose eleventh column is `name:pid`
/// are kept, so a parse miss can only make an authorization fail (local_ipc.rs:344-363).
pub fn parse_netstat_owner_rows(text: &str) -> Vec<TcpOwnerRow> {
    fn endpoint(token: &str) -> Option<SocketAddrV4> {
        let (address, port) = token.rsplit_once('.')?;
        Some(SocketAddrV4::new(address.parse().ok()?, port.parse().ok()?))
    }
    text.lines()
        .filter_map(|line| {
            let columns = line.split_whitespace().collect::<Vec<_>>();
            if columns.first() != Some(&"tcp4") || columns.len() < 11 {
                return None;
            }
            let (_, pid) = columns[10].rsplit_once(':')?;
            Some(TcpOwnerRow {
                local: endpoint(columns[3])?,
                remote: endpoint(columns[4])?,
                owning_pid: pid.parse().ok()?,
            })
        })
        .collect()
}

// ---- ready line, address lines, readiness gate (ready_address.rs) ---------------------

fn quoted(text: &str) -> Result<String, SidecarError> {
    serde_json::to_string(text).map_err(|_| SidecarError::ReadyOutput)
}
fn address_json(address: &EndpointAddr) -> Result<String, SidecarError> {
    serde_json::to_string(address).map_err(|_| SidecarError::ReadyOutput)
}
/// `{"event":"ready","endpoint_id":..,"endpoint_addr":..,"alpn":..}` (sidecar.rs:504-516).
pub fn posix_ready_record(
    endpoint_id: &str,
    address: &EndpointAddr,
    alpn: &str,
) -> Result<String, SidecarError> {
    Ok(format!(
        "{{\"event\":\"ready\",\"endpoint_id\":{},\"endpoint_addr\":{},\"alpn\":{}}}",
        quoted(endpoint_id)?,
        address_json(address)?,
        quoted(alpn)?
    ))
}
/// The POSIX record with `"local_port":N` appended (local_ipc.rs:404-418).
pub fn loopback_ready_record(
    endpoint_id: &str,
    address: &EndpointAddr,
    alpn: &str,
    local_port: u16,
) -> Result<String, SidecarError> {
    let posix = posix_ready_record(endpoint_id, address, alpn)?;
    Ok(format!(
        "{},\"local_port\":{local_port}}}",
        posix.trim_end_matches('}')
    ))
}
/// `{"event":"address_changed",..}` (ready_address.rs:183-190).
pub fn address_changed_line(endpoint_id: &str, address: &EndpointAddr) -> String {
    match (quoted(endpoint_id), address_json(address)) {
        (Ok(id), Ok(address)) => format!(
            "{{\"event\":\"address_changed\",\"endpoint_id\":{id},\"endpoint_addr\":{address}}}"
        ),
        _ => "{\"event\":\"address_changed\"}".to_owned(),
    }
}
/// One stderr event line, `{"event":".."}` (sidecar.rs:2906-2910).
pub fn event_line(event: &str) -> String {
    quoted(event).map_or_else(
        |_| "{\"event\":\"logging_failure\"}".to_owned(),
        |event| format!("{{\"event\":{event}}}"),
    )
}

/// Named reason when the relay window expires (ready_address.rs:51).
pub const ADDRESS_INCOMPLETE_REASON: &str = "relay_address_not_advertised";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AddressReadiness {
    Online,
    LocalOnly,
    WindowExpired,
}
impl AddressReadiness {
    pub const fn reason(self) -> Option<&'static str> {
        match self {
            Self::Online | Self::LocalOnly => None,
            Self::WindowExpired => Some(ADDRESS_INCOMPLETE_REASON),
        }
    }
    pub const fn event(self) -> &'static str {
        match self {
            Self::Online | Self::LocalOnly => "address_ready",
            Self::WindowExpired => "ready_address_incomplete",
        }
    }
}
fn address_is_usable(address: &EndpointAddr, requires_relay: bool) -> bool {
    if requires_relay {
        address.addrs.iter().any(iroh::TransportAddr::is_relay)
    } else {
        !address.addrs.is_empty()
    }
}
/// Waits (bounded by the caller-supplied `window`; shipped 3 s) for a usable address set and
/// returns the LIVE address with what the gate observed (ready_address.rs:103-125). As in the
/// oracle, a closed watch channel ends the wait and is reported as `Online` / `LocalOnly`
/// according to `requires_relay`.
pub async fn await_advertised_address<F: Future<Output = ()>>(
    addresses: &mut watch::Receiver<EndpointAddr>,
    requires_relay: bool,
    relay_online: F,
    window: Duration,
) -> (EndpointAddr, AddressReadiness) {
    let reached = timeout(
        window,
        wait_for_usable(addresses, requires_relay, relay_online),
    )
    .await;
    let advertised = addresses.borrow_and_update().clone();
    let readiness = match reached {
        Ok(()) if requires_relay => AddressReadiness::Online,
        Ok(()) => AddressReadiness::LocalOnly,
        Err(_elapsed) => AddressReadiness::WindowExpired,
    };
    (advertised, readiness)
}
async fn wait_for_usable<F: Future<Output = ()>>(
    addresses: &mut watch::Receiver<EndpointAddr>,
    requires_relay: bool,
    relay_online: F,
) {
    let mut relay_online = pin!(relay_online);
    let mut online_confirmed = false;
    loop {
        if address_is_usable(&addresses.borrow_and_update(), requires_relay) {
            return;
        }
        tokio::select! {
            () = &mut relay_online, if requires_relay && !online_confirmed => {
                online_confirmed = true;
            }
            changed = addresses.changed() => {
                if changed.is_err() {
                    return;
                }
            }
        }
    }
}
pub trait AddressLineSink {
    fn emit(&mut self, line: &str);
}
/// Re-advertises the address whenever it changes; the value `ready` carried (`published`,
/// passed in to avoid racing a change between the ready line and this task's first poll) is
/// never repeated (ready_address.rs:202-219).
pub async fn republish_address_changes<S: AddressLineSink>(
    endpoint_id: String,
    mut addresses: watch::Receiver<EndpointAddr>,
    published: EndpointAddr,
    mut sink: S,
) {
    let mut published = published;
    loop {
        let current = addresses.borrow_and_update().clone();
        if current != published {
            published = current.clone();
            sink.emit(&address_changed_line(&endpoint_id, &current));
        }
        if addresses.changed().await.is_err() {
            return;
        }
    }
}

/// 32 random bytes for a server nonce, from the OS.
pub fn os_random_nonce() -> io::Result<[u8; 32]> {
    let mut nonce = [0_u8; 32];
    File::open("/dev/urandom")?.read_exact(&mut nonce)?;
    Ok(nonce)
}
