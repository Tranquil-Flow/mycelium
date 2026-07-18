// SPDX-License-Identifier: AGPL-3.0-or-later
//! Authenticated iroh sidecar runtime.

use std::collections::{HashMap, HashSet, VecDeque};
use std::error::Error;
use std::fmt;
use std::fs;
use std::io::{self, Read};
use std::net::{Ipv4Addr, SocketAddrV4};
use std::num::NonZeroUsize;
use std::os::fd::{FromRawFd, RawFd};
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use iroh::endpoint::{RecvStream, SendStream, VarInt, presets};
use iroh::{Endpoint, EndpointAddr, EndpointId, RelayMode, TransportAddr};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::{Mutex, Notify, OwnedSemaphorePermit, RwLock, Semaphore, watch};
use tokio::time::{Instant, sleep, timeout};
use tokio_util::sync::CancellationToken;
use zeroize::{Zeroize, Zeroizing};

use crate::local::{
    ClientHello, LOCAL_MAX_RECORD_BYTES, MAX_HELLO_BYTES, Record, RecordKind, SequenceGuard,
    ServerHello, SessionKeys, decode_record, derive_session_keys, encode_record,
};
use crate::protocol::{WireError, decode_frame};
use crate::remote::{REMOTE_MAX_FRAME_BYTES, RemoteKind, decode_remote_frame, encode_remote_frame};

pub const IROH_ALPN: &[u8] = b"mycelium.iroh.sidecar.v1";
pub const OPERATIONAL_MAX_FRAME_BYTES: usize = 16 * 1024 * 1024;
pub const DEFAULT_QUEUE_CAPACITY: usize = 128;
pub const RECEIVE_POLL_WAIT: Duration = Duration::from_millis(250);

const STREAM_IO_TIMEOUT: Duration = Duration::from_secs(15);
const LOCAL_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(2);
const INITIAL_RETRY_DELAY: Duration = Duration::from_millis(100);
const MAX_RETRY_DELAY: Duration = Duration::from_secs(5);
const CANCEL_CODE: VarInt = VarInt::from_u32(7);
const REJECT_CODE: VarInt = VarInt::from_u32(8);
const MAX_LOCAL_SESSIONS: usize = 16;
const REMOTE_GENERATION_BYTES: usize = 8;
const REMOTE_MAX_TRANSFER_BYTES: usize = REMOTE_MAX_FRAME_BYTES;

type MessageId = [u8; 16];
type FrameDigest = [u8; 32];

fn frame_digest(frame: &[u8]) -> FrameDigest {
    Sha256::digest(frame).into()
}

#[derive(Debug)]
pub enum IngressError {
    FrameTooLarge,
    InvalidFrame(WireError),
}

impl IngressError {
    pub const fn code(&self) -> &'static str {
        match self {
            Self::FrameTooLarge => "frame_too_large",
            Self::InvalidFrame(_) => "invalid_frame",
        }
    }
}

impl fmt::Display for IngressError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code())
    }
}

impl Error for IngressError {}

/// Applies the operational cap before invoking the canonical Router wire decoder.
pub fn validate_router_ingress(frame: &[u8]) -> Result<(), IngressError> {
    if frame.len() > OPERATIONAL_MAX_FRAME_BYTES {
        return Err(IngressError::FrameTooLarge);
    }
    decode_frame(frame)
        .map(|_| ())
        .map_err(IngressError::InvalidFrame)
}

#[derive(Clone, Debug)]
pub struct SidecarConfig {
    pub uds: PathBuf,
    pub queue_capacity: NonZeroUsize,
    pub local_only: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SidecarError {
    InvalidBootstrapFd,
    BootstrapNotPipe,
    InvalidBootstrapLength,
    Filesystem,
    SocketSecurity,
    SocketBind,
    EndpointBind,
    ReadyOutput,
    Runtime,
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
            Self::ReadyOutput => "ready_output_error",
            Self::Runtime => "runtime_error",
        }
    }
}

impl fmt::Display for SidecarError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code())
    }
}

impl Error for SidecarError {}

/// Consumes an inherited pipe descriptor and accepts exactly 32 bytes before EOF.
/// The descriptor is closed on both success and failure.
pub fn read_bootstrap_secret(fd: RawFd) -> Result<Zeroizing<[u8; 32]>, SidecarError> {
    if fd < 0 {
        return Err(SidecarError::InvalidBootstrapFd);
    }

    // SAFETY: ownership of the explicitly inherited descriptor is transferred to
    // this function exactly once by the CLI entrypoint.
    let mut pipe = unsafe { fs::File::from_raw_fd(fd) };
    let metadata = pipe
        .metadata()
        .map_err(|_| SidecarError::InvalidBootstrapFd)?;
    if !metadata.file_type().is_fifo() {
        return Err(SidecarError::BootstrapNotPipe);
    }

    let mut bytes = Vec::with_capacity(33);
    pipe.by_ref()
        .take(33)
        .read_to_end(&mut bytes)
        .map_err(|_| SidecarError::InvalidBootstrapLength)?;
    if bytes.len() != 32 {
        bytes.zeroize();
        return Err(SidecarError::InvalidBootstrapLength);
    }
    let mut secret = Zeroizing::new([0_u8; 32]);
    secret.copy_from_slice(&bytes);
    bytes.zeroize();
    Ok(secret)
}

#[derive(Serialize)]
struct ReadyEvent<'a> {
    event: &'static str,
    endpoint_id: String,
    endpoint_addr: &'a EndpointAddr,
    alpn: &'static str,
}

/// Runs until SIGINT, SIGTERM/process termination, or a fatal listener error.
pub async fn run_sidecar(
    config: SidecarConfig,
    secret: Zeroizing<[u8; 32]>,
) -> Result<(), SidecarError> {
    let listener = bind_secure_uds(&config.uds)?;
    let _socket_cleanup = SocketCleanup(config.uds.clone());
    let endpoint = bind_endpoint(config.local_only).await?;
    let endpoint_addr = endpoint.addr();
    let endpoint_id = endpoint.id().to_string();

    let state = RuntimeState::new(config.queue_capacity.get(), config.local_only);
    let outbound_task = tokio::spawn(outbound_worker(endpoint.clone(), state.clone()));
    let incoming_task = tokio::spawn(incoming_worker(endpoint.clone(), state.clone()));

    let ready = ReadyEvent {
        event: "ready",
        endpoint_id: endpoint_id.clone(),
        endpoint_addr: &endpoint_addr,
        alpn: std::str::from_utf8(IROH_ALPN).expect("ALPN is ASCII"),
    };
    let ready_json = serde_json::to_string(&ready).map_err(|_| SidecarError::ReadyOutput)?;
    {
        use std::io::Write as _;
        let mut stdout = io::stdout().lock();
        writeln!(stdout, "{ready_json}").map_err(|_| SidecarError::ReadyOutput)?;
        stdout.flush().map_err(|_| SidecarError::ReadyOutput)?;
    }

    let secret = Arc::new(secret);
    let sessions = Arc::new(Semaphore::new(MAX_LOCAL_SESSIONS));
    let next_session = AtomicU64::new(1);
    let listener_result = loop {
        let permit = tokio::select! {
            result = sessions.clone().acquire_owned() => {
                match result {
                    Ok(permit) => permit,
                    Err(_) => break Ok(()),
                }
            }
            signal = tokio::signal::ctrl_c() => {
                let _ = signal;
                break Ok(());
            }
        };

        let accepted = tokio::select! {
            result = listener.accept() => result,
            signal = tokio::signal::ctrl_c() => {
                let _ = signal;
                break Ok(());
            }
        };
        let (stream, _) = match accepted {
            Ok(pair) => pair,
            Err(_) => break Err(SidecarError::Runtime),
        };
        if !same_effective_uid(&stream) {
            drop(permit);
            log_event("local_peer_rejected");
            continue;
        }

        let session_id = next_session.fetch_add(1, Ordering::Relaxed);
        let session_state = state.clone();
        let session_secret = secret.clone();
        let public_id = endpoint_id.clone();
        tokio::spawn(async move {
            let _permit = permit;
            let _ = handle_local_session(
                stream,
                session_id,
                session_secret.as_ref().as_ref(),
                &public_id,
                session_state.clone(),
            )
            .await;
            session_state.redeliver_session(session_id).await;
        });
    };

    endpoint.close().await;
    outbound_task.abort();
    incoming_task.abort();
    listener_result
}

async fn bind_endpoint(local_only: bool) -> Result<Endpoint, SidecarError> {
    let builder = if local_only {
        Endpoint::builder(presets::Minimal)
            .relay_mode(RelayMode::Disabled)
            .clear_ip_transports()
            .bind_addr(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))
            .map_err(|_| SidecarError::EndpointBind)?
    } else {
        Endpoint::builder(presets::N0)
    };
    builder
        .alpns(vec![IROH_ALPN.to_vec()])
        .bind()
        .await
        .map_err(|_| SidecarError::EndpointBind)
}

fn bind_secure_uds(path: &Path) -> Result<UnixListener, SidecarError> {
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    fs::create_dir_all(parent).map_err(|_| SidecarError::Filesystem)?;
    let parent_metadata = fs::symlink_metadata(parent).map_err(|_| SidecarError::Filesystem)?;
    // SAFETY: geteuid has no preconditions and does not dereference pointers.
    let effective_uid = unsafe { libc::geteuid() };
    if !parent_metadata.file_type().is_dir() || parent_metadata.file_type().is_symlink() {
        return Err(SidecarError::SocketSecurity);
    }
    if parent_metadata.uid() != effective_uid {
        return Err(SidecarError::SocketSecurity);
    }
    fs::set_permissions(parent, fs::Permissions::from_mode(0o700))
        .map_err(|_| SidecarError::SocketSecurity)?;
    let restricted_parent = fs::symlink_metadata(parent).map_err(|_| SidecarError::Filesystem)?;
    if restricted_parent.permissions().mode() & 0o777 != 0o700 {
        return Err(SidecarError::SocketSecurity);
    }

    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_socket() && metadata.uid() == effective_uid => {
            fs::remove_file(path).map_err(|_| SidecarError::SocketSecurity)?;
        }
        Ok(_) => return Err(SidecarError::SocketSecurity),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(_) => return Err(SidecarError::Filesystem),
    }

    let listener = UnixListener::bind(path).map_err(|_| SidecarError::SocketBind)?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))
        .map_err(|_| SidecarError::SocketSecurity)?;
    let socket_metadata = fs::symlink_metadata(path).map_err(|_| SidecarError::Filesystem)?;
    if socket_metadata.uid() != effective_uid
        || socket_metadata.permissions().mode() & 0o777 != 0o600
    {
        return Err(SidecarError::SocketSecurity);
    }
    Ok(listener)
}

fn same_effective_uid(stream: &UnixStream) -> bool {
    let Ok(credentials) = stream.peer_cred() else {
        return false;
    };
    // SAFETY: geteuid has no preconditions and does not dereference pointers.
    credentials.uid() == unsafe { libc::geteuid() }
}

struct SocketCleanup(PathBuf);

impl Drop for SocketCleanup {
    fn drop(&mut self) {
        if fs::symlink_metadata(&self.0).is_ok_and(|metadata| metadata.file_type().is_socket()) {
            let _ = fs::remove_file(&self.0);
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConfigurePeerPayload {
    endpoint_id: String,
    endpoint_addr: EndpointAddr,
    generation: u64,
}

#[derive(Serialize)]
struct ErrorPayload<'a> {
    code: &'a str,
}

async fn handle_local_session(
    mut stream: UnixStream,
    session_id: u64,
    secret: &[u8],
    endpoint_id: &str,
    state: Arc<RuntimeState>,
) -> Result<(), SessionError> {
    let hello_bytes = timeout(
        LOCAL_HANDSHAKE_TIMEOUT,
        read_length_prefixed(&mut stream, MAX_HELLO_BYTES),
    )
    .await
    .map_err(|_| SessionError::Timeout)??;
    let hello: ClientHello =
        serde_json::from_slice(&hello_bytes).map_err(|_| SessionError::Protocol)?;
    let client_nonce = hello
        .verify(secret)
        .map_err(|_| SessionError::Authentication)?;

    let mut server_nonce = [0_u8; 32];
    getrandom::fill(&mut server_nonce).map_err(|_| SessionError::Runtime)?;
    let server_hello = ServerHello::new(secret, &client_nonce, &server_nonce, endpoint_id)
        .map_err(|_| SessionError::Runtime)?;
    let encoded_hello = serde_json::to_vec(&server_hello).map_err(|_| SessionError::Runtime)?;
    timeout(
        LOCAL_HANDSHAKE_TIMEOUT,
        write_length_prefixed(&mut stream, &encoded_hello),
    )
    .await
    .map_err(|_| SessionError::Timeout)??;
    let keys = derive_session_keys(secret, &client_nonce, &server_nonce)
        .map_err(|_| SessionError::Runtime)?;
    server_nonce.zeroize();

    authenticated_session_loop(&mut stream, session_id, &keys, state).await
}

async fn authenticated_session_loop(
    stream: &mut UnixStream,
    session_id: u64,
    keys: &SessionKeys,
    state: Arc<RuntimeState>,
) -> Result<(), SessionError> {
    let mut receive_sequence = SequenceGuard::new();
    let mut send_sequence = Some(0_u64);
    loop {
        let encoded = read_length_prefixed(stream, LOCAL_MAX_RECORD_BYTES).await?;
        let record = decode_record(&encoded, &keys.client_to_sidecar, &mut receive_sequence)
            .map_err(|_| SessionError::Authentication)?;
        let confirmed = record.kind == RecordKind::SendConfirmed;
        let response = process_local_record(record, session_id, &state);
        tokio::pin!(response);
        let response = if confirmed {
            loop {
                tokio::select! {
                    response = &mut response => break response,
                    readable = stream.readable() => {
                        readable.map_err(|_| SessionError::Disconnected)?;
                        let mut unexpected = [0_u8; 1];
                        match stream.try_read(&mut unexpected) {
                            Ok(0) => return Ok(()),
                            Ok(_) => return Err(SessionError::Protocol),
                            Err(error) if error.kind() == io::ErrorKind::WouldBlock => continue,
                            Err(_) => return Err(SessionError::Disconnected),
                        }
                    }
                }
            }
        } else {
            response.await
        };
        write_authenticated_record(
            stream,
            response,
            &keys.sidecar_to_client,
            &mut send_sequence,
        )
        .await?;
    }
}

struct ResponseRecord {
    kind: RecordKind,
    message_id: MessageId,
    payload: Vec<u8>,
}

async fn process_local_record(
    record: Record,
    session_id: u64,
    state: &Arc<RuntimeState>,
) -> ResponseRecord {
    match record.kind {
        RecordKind::Send | RecordKind::SendConfirmed => {
            let confirmed = record.kind == RecordKind::SendConfirmed;
            let (payload, expected_generation) = if confirmed {
                if record.payload.len() < REMOTE_GENERATION_BYTES {
                    return error_response(record.message_id, "invalid_generation");
                }
                let generation = u64::from_be_bytes(
                    record.payload[..REMOTE_GENERATION_BYTES]
                        .try_into()
                        .expect("generation prefix has fixed length"),
                );
                if generation == 0 {
                    return error_response(record.message_id, "invalid_generation");
                }
                (
                    record.payload[REMOTE_GENERATION_BYTES..].to_vec(),
                    Some(generation),
                )
            } else {
                (record.payload, None)
            };
            if validate_router_ingress(&payload).is_err() {
                return error_response(record.message_id, "invalid_frame");
            }
            match state
                .enqueue_outbound(record.message_id, payload, expected_generation)
                .await
            {
                EnqueueOutcome::Queued(control) | EnqueueOutcome::Duplicate(control) => {
                    if !confirmed {
                        ack_response(record.message_id)
                    } else {
                        match control.wait_for_terminal().await {
                            OutboundTerminal::Delivered => ack_response(record.message_id),
                            OutboundTerminal::PeerRotated => {
                                error_response(record.message_id, "peer_rotated")
                            }
                            OutboundTerminal::Cancelled => {
                                error_response(record.message_id, "cancelled")
                            }
                            OutboundTerminal::ReplayCollision => {
                                error_response(record.message_id, "replay_collision")
                            }
                        }
                    }
                }
                EnqueueOutcome::ReplayCollision => {
                    error_response(record.message_id, "replay_collision")
                }
                EnqueueOutcome::PeerRotated => error_response(record.message_id, "peer_rotated"),
                EnqueueOutcome::Full => error_response(record.message_id, "queue_full"),
            }
        }
        RecordKind::Receive => match state.receive(session_id, RECEIVE_POLL_WAIT).await {
            Some((message_id, generation, payload)) => ResponseRecord {
                kind: RecordKind::Delivery,
                message_id,
                payload: [generation.to_be_bytes().as_slice(), payload.as_slice()].concat(),
            },
            None => error_response(record.message_id, "empty"),
        },
        RecordKind::Ack => {
            if state.ack_inbound(session_id, record.message_id).await {
                ack_response(record.message_id)
            } else {
                error_response(record.message_id, "unknown_delivery")
            }
        }
        RecordKind::Cancel => {
            if state.cancel_outbound(record.message_id).await {
                ack_response(record.message_id)
            } else {
                error_response(record.message_id, "unknown_message")
            }
        }
        RecordKind::ConfigurePeer => {
            let Ok(configuration) = serde_json::from_slice::<ConfigurePeerPayload>(&record.payload)
            else {
                return error_response(record.message_id, "invalid_peer");
            };
            match state.configure_peer(configuration).await {
                Ok(()) => ack_response(record.message_id),
                Err(()) => error_response(record.message_id, "invalid_peer"),
            }
        }
        RecordKind::Ping => ack_response(record.message_id),
        RecordKind::Delivery | RecordKind::Error => {
            error_response(record.message_id, "invalid_kind")
        }
    }
}

fn ack_response(message_id: MessageId) -> ResponseRecord {
    ResponseRecord {
        kind: RecordKind::Ack,
        message_id,
        payload: Vec::new(),
    }
}

fn error_response(message_id: MessageId, code: &str) -> ResponseRecord {
    let payload = serde_json::to_vec(&ErrorPayload { code }).unwrap_or_default();
    ResponseRecord {
        kind: RecordKind::Error,
        message_id,
        payload,
    }
}

async fn write_authenticated_record(
    stream: &mut UnixStream,
    response: ResponseRecord,
    key: &[u8],
    sequence: &mut Option<u64>,
) -> Result<(), SessionError> {
    let current = sequence.ok_or(SessionError::Protocol)?;
    let encoded = encode_record(
        response.kind,
        current,
        response.message_id,
        &response.payload,
        key,
    )
    .map_err(|_| SessionError::Protocol)?;
    *sequence = current.checked_add(1);
    write_length_prefixed(stream, &encoded).await
}

async fn read_length_prefixed(
    stream: &mut UnixStream,
    maximum: usize,
) -> Result<Vec<u8>, SessionError> {
    let length = stream
        .read_u32()
        .await
        .map_err(|_| SessionError::Disconnected)? as usize;
    if length == 0 || length > maximum {
        return Err(SessionError::Protocol);
    }
    let mut bytes = vec![0_u8; length];
    stream
        .read_exact(&mut bytes)
        .await
        .map_err(|_| SessionError::Disconnected)?;
    Ok(bytes)
}

async fn write_length_prefixed(stream: &mut UnixStream, bytes: &[u8]) -> Result<(), SessionError> {
    let length = u32::try_from(bytes.len()).map_err(|_| SessionError::Protocol)?;
    stream
        .write_u32(length)
        .await
        .map_err(|_| SessionError::Disconnected)?;
    stream
        .write_all(bytes)
        .await
        .map_err(|_| SessionError::Disconnected)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SessionError {
    Authentication,
    Protocol,
    Disconnected,
    Timeout,
    Runtime,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct PeerBinding {
    address: EndpointAddr,
    generation: u64,
}

struct RuntimeState {
    peer: RwLock<Option<PeerBinding>>,
    peer_generation: watch::Sender<u64>,
    peer_changed: Notify,
    local_only: bool,
    inbound: Mutex<InboundState>,
    inbound_slots: Arc<Semaphore>,
    inbound_ready: Notify,
    seen_limit: usize,
    outbound: Mutex<VecDeque<OutboundItem>>,
    outbound_ready: Notify,
    outbound_slots: Arc<Semaphore>,
    outbound_tokens: Mutex<HashMap<MessageId, Arc<OutboundControl>>>,
}

struct InboundState {
    pending: VecDeque<InboundItem>,
    inflight: HashMap<u64, VecDeque<InboundItem>>,
    active: HashMap<MessageId, Arc<InboundControl>>,
    completed: HashMap<MessageId, FrameDigest>,
    completed_order: VecDeque<MessageId>,
    rotated: HashSet<MessageId>,
    rotated_order: VecDeque<MessageId>,
}

struct InboundItem {
    message_id: MessageId,
    generation: u64,
    payload: Vec<u8>,
    control: Arc<InboundControl>,
    _permit: OwnedSemaphorePermit,
}

struct InboundControl {
    digest: FrameDigest,
    terminal: std::sync::atomic::AtomicU8,
    completed: Notify,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
enum InboundTerminal {
    Acknowledged = 1,
    PeerRotated = 2,
}

impl InboundControl {
    fn terminal(&self) -> Option<InboundTerminal> {
        match self.terminal.load(Ordering::Acquire) {
            1 => Some(InboundTerminal::Acknowledged),
            2 => Some(InboundTerminal::PeerRotated),
            _ => None,
        }
    }

    fn finish(&self, terminal: InboundTerminal) {
        if self
            .terminal
            .compare_exchange(0, terminal as u8, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
        {
            self.completed.notify_waiters();
        }
    }

    async fn wait_for_terminal(&self) -> InboundTerminal {
        loop {
            let notified = self.completed.notified();
            if let Some(terminal) = self.terminal() {
                return terminal;
            }
            notified.await;
        }
    }
}

struct OutboundItem {
    message_id: MessageId,
    payload: Vec<u8>,
    target: Option<PeerBinding>,
    control: Arc<OutboundControl>,
}

struct OutboundControl {
    cancellation: CancellationToken,
    permit: Mutex<Option<OwnedSemaphorePermit>>,
    digest: FrameDigest,
    generation: AtomicU64,
    terminal: std::sync::atomic::AtomicU8,
    completed: Notify,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
enum OutboundTerminal {
    Delivered = 1,
    PeerRotated = 2,
    Cancelled = 3,
    ReplayCollision = 4,
}

impl OutboundControl {
    fn terminal(&self) -> Option<OutboundTerminal> {
        match self.terminal.load(Ordering::Acquire) {
            1 => Some(OutboundTerminal::Delivered),
            2 => Some(OutboundTerminal::PeerRotated),
            3 => Some(OutboundTerminal::Cancelled),
            4 => Some(OutboundTerminal::ReplayCollision),
            _ => None,
        }
    }

    fn finish(&self, terminal: OutboundTerminal) {
        if self
            .terminal
            .compare_exchange(0, terminal as u8, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
        {
            self.completed.notify_waiters();
        }
    }

    async fn wait_for_terminal(&self) -> OutboundTerminal {
        loop {
            let notified = self.completed.notified();
            if let Some(terminal) = self.terminal() {
                return terminal;
            }
            notified.await;
        }
    }
}

enum EnqueueOutcome {
    Queued(Arc<OutboundControl>),
    Duplicate(Arc<OutboundControl>),
    ReplayCollision,
    PeerRotated,
    Full,
}

enum AdmissionOutcome {
    Admitted(Arc<InboundControl>),
    PendingDuplicate(Arc<InboundControl>),
    CompletedDuplicate,
    ReplayCollision,
    PeerRotated,
    Full,
}

impl InboundState {
    fn remember_completed(&mut self, message_id: MessageId, digest: FrameDigest, limit: usize) {
        if self.completed.insert(message_id, digest).is_none() {
            self.completed_order.push_back(message_id);
        }
        while self.completed_order.len() > limit {
            if let Some(expired) = self.completed_order.pop_front() {
                self.completed.remove(&expired);
            }
        }
    }

    fn remember_rotated(&mut self, message_id: MessageId, limit: usize) {
        if self.rotated.insert(message_id) {
            self.rotated_order.push_back(message_id);
        }
        while self.rotated_order.len() > limit {
            if let Some(expired) = self.rotated_order.pop_front() {
                self.rotated.remove(&expired);
            }
        }
    }
}

impl RuntimeState {
    fn new(capacity: usize, local_only: bool) -> Arc<Self> {
        let seen_limit = capacity.saturating_mul(8).max(64);
        let (peer_generation, _) = watch::channel(0);
        Arc::new(Self {
            peer: RwLock::new(None),
            peer_generation,
            peer_changed: Notify::new(),
            local_only,
            inbound: Mutex::new(InboundState {
                pending: VecDeque::with_capacity(capacity),
                inflight: HashMap::new(),
                active: HashMap::with_capacity(capacity),
                completed: HashMap::with_capacity(seen_limit),
                completed_order: VecDeque::with_capacity(seen_limit),
                rotated: HashSet::with_capacity(seen_limit),
                rotated_order: VecDeque::with_capacity(seen_limit),
            }),
            inbound_slots: Arc::new(Semaphore::new(capacity)),
            inbound_ready: Notify::new(),
            seen_limit,
            outbound: Mutex::new(VecDeque::with_capacity(capacity)),
            outbound_ready: Notify::new(),
            outbound_slots: Arc::new(Semaphore::new(capacity)),
            outbound_tokens: Mutex::new(HashMap::with_capacity(capacity)),
        })
    }

    async fn configure_peer(&self, configuration: ConfigurePeerPayload) -> Result<(), ()> {
        let endpoint_id = configuration
            .endpoint_id
            .parse::<EndpointId>()
            .map_err(|_| ())?;
        if configuration.generation == 0 || endpoint_id != configuration.endpoint_addr.id {
            return Err(());
        }
        if configuration.endpoint_addr.id.to_string() != configuration.endpoint_id {
            return Err(());
        }
        if self.local_only
            && (configuration.endpoint_addr.addrs.is_empty()
                || !configuration.endpoint_addr.addrs.iter().all(
                    |address| matches!(address, TransportAddr::Ip(ip) if ip.ip().is_loopback()),
                ))
        {
            return Err(());
        }

        let replacement = PeerBinding {
            address: configuration.endpoint_addr,
            generation: configuration.generation,
        };
        let generation = replacement.generation;
        let mut peer = self.peer.write().await;
        match peer.as_ref() {
            Some(current) if current == &replacement => return Ok(()),
            Some(current) if replacement.generation <= current.generation => return Err(()),
            Some(_) => {
                *peer = Some(replacement);
                self.fence_outbound_for_rotation().await;
                self.fence_inbound_for_rotation().await;
            }
            None => *peer = Some(replacement),
        }
        drop(peer);
        self.peer_generation.send_replace(generation);
        self.peer_changed.notify_waiters();
        self.inbound_ready.notify_waiters();
        Ok(())
    }

    async fn fence_outbound_for_rotation(&self) {
        let mut tokens = self.outbound_tokens.lock().await;
        let stale_ids: HashSet<_> = tokens.keys().copied().collect();
        let controls: Vec<_> = stale_ids
            .iter()
            .filter_map(|message_id| tokens.remove(message_id))
            .collect();
        self.outbound
            .lock()
            .await
            .retain(|item| !stale_ids.contains(&item.message_id));
        drop(tokens);

        for control in controls {
            control.finish(OutboundTerminal::PeerRotated);
            control.cancellation.cancel();
            control.permit.lock().await.take();
        }
    }

    async fn fence_inbound_for_rotation(&self) {
        let mut inbound = self.inbound.lock().await;
        let active: Vec<_> = inbound.active.drain().collect();
        let completed: Vec<_> = inbound.completed.drain().map(|(id, _)| id).collect();
        inbound.completed_order.clear();
        inbound.pending.clear();
        inbound.inflight.clear();
        for (message_id, _) in &active {
            inbound.remember_rotated(*message_id, self.seen_limit);
        }
        for message_id in completed {
            inbound.remember_rotated(message_id, self.seen_limit);
        }
        drop(inbound);
        for (_, control) in active {
            control.finish(InboundTerminal::PeerRotated);
        }
    }

    async fn peer_binding(&self) -> Option<PeerBinding> {
        self.peer.read().await.clone()
    }

    async fn peer_matches(&self, endpoint_id: EndpointId, generation: u64) -> bool {
        self.peer.read().await.as_ref().is_some_and(|binding| {
            binding.address.id == endpoint_id && binding.generation == generation
        })
    }

    async fn bind_outbound(&self, control: &OutboundControl) -> Option<PeerBinding> {
        let peer = self.peer.read().await;
        let binding = peer.as_ref()?.clone();
        let generation = control.generation.load(Ordering::Acquire);
        if generation == 0 {
            control
                .generation
                .store(binding.generation, Ordering::Release);
            Some(binding)
        } else if generation == binding.generation {
            Some(binding)
        } else {
            None
        }
    }

    async fn enqueue_outbound(
        &self,
        message_id: MessageId,
        payload: Vec<u8>,
        expected_generation: Option<u64>,
    ) -> EnqueueOutcome {
        let digest = frame_digest(&payload);
        let peer = self.peer.read().await;
        if expected_generation.is_some_and(|generation| {
            peer.as_ref().map(|binding| binding.generation) != Some(generation)
        }) {
            return EnqueueOutcome::PeerRotated;
        }
        let mut tokens = self.outbound_tokens.lock().await;
        if let Some(control) = tokens.get(&message_id) {
            return if control.digest == digest {
                EnqueueOutcome::Duplicate(control.clone())
            } else {
                EnqueueOutcome::ReplayCollision
            };
        }
        let permit = match self.outbound_slots.clone().try_acquire_owned() {
            Ok(permit) => permit,
            Err(_) => return EnqueueOutcome::Full,
        };
        let target = peer.as_ref().cloned();
        let control = Arc::new(OutboundControl {
            cancellation: CancellationToken::new(),
            permit: Mutex::new(Some(permit)),
            digest,
            generation: AtomicU64::new(target.as_ref().map_or(0, |binding| binding.generation)),
            terminal: std::sync::atomic::AtomicU8::new(0),
            completed: Notify::new(),
        });
        let item = OutboundItem {
            message_id,
            payload,
            target,
            control: control.clone(),
        };
        tokens.insert(message_id, control.clone());
        self.outbound.lock().await.push_back(item);
        drop(tokens);
        drop(peer);
        self.outbound_ready.notify_one();
        EnqueueOutcome::Queued(control)
    }

    async fn cancel_outbound(&self, message_id: MessageId) -> bool {
        let mut tokens = self.outbound_tokens.lock().await;
        let Some(control) = tokens.remove(&message_id) else {
            return false;
        };
        let mut outbound = self.outbound.lock().await;
        if let Some(position) = outbound
            .iter()
            .position(|item| Arc::ptr_eq(&item.control, &control))
        {
            outbound.remove(position);
        }
        drop(outbound);
        drop(tokens);
        control.finish(OutboundTerminal::Cancelled);
        control.cancellation.cancel();
        control.permit.lock().await.take();
        true
    }

    async fn next_outbound(&self) -> OutboundItem {
        loop {
            let notified = self.outbound_ready.notified();
            if let Some(item) = self.outbound.lock().await.pop_front() {
                return item;
            }
            notified.await;
        }
    }

    async fn finish_outbound(
        &self,
        message_id: MessageId,
        control: &Arc<OutboundControl>,
        terminal: OutboundTerminal,
    ) {
        control.finish(terminal);
        let mut tokens = self.outbound_tokens.lock().await;
        if tokens
            .get(&message_id)
            .is_some_and(|current| Arc::ptr_eq(current, control))
        {
            tokens.remove(&message_id);
        }
        drop(tokens);
        control.permit.lock().await.take();
    }

    #[cfg(test)]
    async fn admit_inbound(&self, message_id: MessageId, payload: Vec<u8>) -> AdmissionOutcome {
        self.admit_inbound_inner(None, message_id, payload).await
    }

    async fn admit_remote_inbound(
        &self,
        endpoint_id: EndpointId,
        generation: u64,
        message_id: MessageId,
        payload: Vec<u8>,
    ) -> AdmissionOutcome {
        self.admit_inbound_inner(Some((endpoint_id, generation)), message_id, payload)
            .await
    }

    async fn admit_inbound_inner(
        &self,
        origin: Option<(EndpointId, u64)>,
        message_id: MessageId,
        payload: Vec<u8>,
    ) -> AdmissionOutcome {
        let digest = frame_digest(&payload);
        let generation = origin.as_ref().map_or(0, |(_, generation)| *generation);
        let peer = self.peer.read().await;
        if let Some((endpoint_id, generation)) = origin {
            let current = peer.as_ref().is_some_and(|binding| {
                binding.address.id == endpoint_id && binding.generation == generation
            });
            if !current {
                let mut inbound = self.inbound.lock().await;
                inbound.remember_rotated(message_id, self.seen_limit);
                return AdmissionOutcome::PeerRotated;
            }
        }
        let mut inbound = self.inbound.lock().await;
        if inbound.rotated.contains(&message_id) {
            return AdmissionOutcome::PeerRotated;
        }
        if let Some(control) = inbound.active.get(&message_id) {
            return if control.digest == digest {
                AdmissionOutcome::PendingDuplicate(control.clone())
            } else {
                AdmissionOutcome::ReplayCollision
            };
        }
        if let Some(completed_digest) = inbound.completed.get(&message_id) {
            return if *completed_digest == digest {
                AdmissionOutcome::CompletedDuplicate
            } else {
                AdmissionOutcome::ReplayCollision
            };
        }
        let permit = match self.inbound_slots.clone().try_acquire_owned() {
            Ok(permit) => permit,
            Err(_) => return AdmissionOutcome::Full,
        };
        let control = Arc::new(InboundControl {
            digest,
            terminal: std::sync::atomic::AtomicU8::new(0),
            completed: Notify::new(),
        });
        inbound.active.insert(message_id, control.clone());
        inbound.pending.push_back(InboundItem {
            message_id,
            generation,
            payload,
            control: control.clone(),
            _permit: permit,
        });
        drop(inbound);
        drop(peer);
        self.inbound_ready.notify_waiters();
        AdmissionOutcome::Admitted(control)
    }

    async fn receive(
        &self,
        session_id: u64,
        maximum_wait: Duration,
    ) -> Option<(MessageId, u64, Vec<u8>)> {
        let deadline = Instant::now() + maximum_wait;
        loop {
            let notified = self.inbound_ready.notified();
            {
                let _peer = self.peer.read().await;
                let mut inbound = self.inbound.lock().await;
                if let Some(item) = inbound.pending.pop_front() {
                    let result = (item.message_id, item.generation, item.payload.clone());
                    inbound
                        .inflight
                        .entry(session_id)
                        .or_default()
                        .push_back(item);
                    return Some(result);
                }
            }
            let now = Instant::now();
            if now >= deadline || timeout(deadline - now, notified).await.is_err() {
                return None;
            }
        }
    }

    async fn ack_inbound(&self, session_id: u64, message_id: MessageId) -> bool {
        let _peer = self.peer.read().await;
        let mut inbound = self.inbound.lock().await;
        let Some(items) = inbound.inflight.get_mut(&session_id) else {
            return false;
        };
        let Some(position) = items.iter().position(|item| item.message_id == message_id) else {
            return false;
        };
        let item = items.remove(position).expect("inflight position exists");
        if items.is_empty() {
            inbound.inflight.remove(&session_id);
        }
        if inbound
            .active
            .get(&message_id)
            .is_some_and(|control| Arc::ptr_eq(control, &item.control))
        {
            inbound.active.remove(&message_id);
        }
        inbound.remember_completed(message_id, item.control.digest, self.seen_limit);
        item.control.finish(InboundTerminal::Acknowledged);
        true
    }

    async fn redeliver_session(&self, session_id: u64) {
        let _peer = self.peer.read().await;
        let mut inbound = self.inbound.lock().await;
        let Some(mut inflight) = inbound.inflight.remove(&session_id) else {
            return;
        };
        while let Some(item) = inflight.pop_back() {
            inbound.pending.push_front(item);
        }
        drop(inbound);
        self.inbound_ready.notify_waiters();
    }
}

async fn outbound_worker(endpoint: Endpoint, state: Arc<RuntimeState>) {
    loop {
        let item = state.next_outbound().await;
        let mut retry_delay = INITIAL_RETRY_DELAY;
        let mut bound_peer = item.target.clone();
        let terminal = loop {
            if item.control.cancellation.is_cancelled() {
                break item
                    .control
                    .terminal()
                    .unwrap_or(OutboundTerminal::Cancelled);
            }
            let peer = match bound_peer.clone() {
                Some(peer) => peer,
                None => match state.bind_outbound(&item.control).await {
                    Some(peer) => {
                        bound_peer = Some(peer.clone());
                        peer
                    }
                    None => {
                        tokio::select! {
                            () = item.control.cancellation.cancelled() => {},
                            () = state.peer_changed.notified() => {},
                            () = sleep(INITIAL_RETRY_DELAY) => {},
                        }
                        continue;
                    }
                },
            };
            if !state.peer_matches(peer.address.id, peer.generation).await {
                break OutboundTerminal::PeerRotated;
            }
            match send_outbound_once(&endpoint, &peer, &item).await {
                SendAttempt::Delivered => break OutboundTerminal::Delivered,
                SendAttempt::Cancelled => {
                    break item
                        .control
                        .terminal()
                        .unwrap_or(OutboundTerminal::Cancelled);
                }
                SendAttempt::PeerRotated => break OutboundTerminal::PeerRotated,
                SendAttempt::ReplayCollision => break OutboundTerminal::ReplayCollision,
                SendAttempt::Retry => {
                    tokio::select! {
                        () = item.control.cancellation.cancelled() => {},
                        () = state.peer_changed.notified() => {},
                        () = sleep(retry_delay) => {},
                    }
                    retry_delay = retry_delay.saturating_mul(2).min(MAX_RETRY_DELAY);
                }
            }
        };
        state
            .finish_outbound(item.message_id, &item.control, terminal)
            .await;
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SendAttempt {
    Delivered,
    Cancelled,
    PeerRotated,
    ReplayCollision,
    Retry,
}

async fn send_outbound_once(
    endpoint: &Endpoint,
    peer: &PeerBinding,
    item: &OutboundItem,
) -> SendAttempt {
    let connection = tokio::select! {
        () = item.control.cancellation.cancelled() => return SendAttempt::Cancelled,
        result = timeout(STREAM_IO_TIMEOUT, endpoint.connect(peer.address.clone(), IROH_ALPN)) => {
            match result {
                Ok(Ok(connection)) => connection,
                _ => return SendAttempt::Retry,
            }
        }
    };
    if connection.remote_id() != peer.address.id {
        connection.close(REJECT_CODE, b"identity");
        return SendAttempt::Retry;
    }

    let (mut send, mut receive) = tokio::select! {
        () = item.control.cancellation.cancelled() => return SendAttempt::Cancelled,
        result = timeout(STREAM_IO_TIMEOUT, connection.open_bi()) => {
            match result {
                Ok(Ok(streams)) => streams,
                _ => return SendAttempt::Retry,
            }
        }
    };
    let mut transfer = Vec::with_capacity(REMOTE_GENERATION_BYTES + item.payload.len());
    transfer.extend_from_slice(&peer.generation.to_be_bytes());
    transfer.extend_from_slice(&item.payload);
    let encoded = match encode_remote_frame(RemoteKind::Transfer, item.message_id, &transfer) {
        Ok(encoded) => encoded,
        Err(_) => return SendAttempt::Cancelled,
    };

    let write_result = tokio::select! {
        () = item.control.cancellation.cancelled() => {
            cancel_streams(&mut send, &mut receive);
            return SendAttempt::Cancelled;
        }
        result = timeout(STREAM_IO_TIMEOUT, send.write_all(&encoded)) => result,
    };
    if !matches!(write_result, Ok(Ok(()))) || send.finish().is_err() {
        cancel_streams(&mut send, &mut receive);
        return SendAttempt::Retry;
    }

    let response = tokio::select! {
        () = item.control.cancellation.cancelled() => {
            cancel_streams(&mut send, &mut receive);
            return SendAttempt::Cancelled;
        }
        result = timeout(STREAM_IO_TIMEOUT, receive.read_to_end(REMOTE_MAX_FRAME_BYTES)) => {
            match result {
                Ok(Ok(response)) => response,
                _ => {
                    cancel_streams(&mut send, &mut receive);
                    return SendAttempt::Retry;
                }
            }
        }
    };
    let Ok(response) = decode_remote_frame(&response) else {
        return SendAttempt::Retry;
    };
    if response.message_id != item.message_id {
        return SendAttempt::Retry;
    }
    if response.kind == RemoteKind::Ack && response.payload.is_empty() {
        SendAttempt::Delivered
    } else if response.kind == RemoteKind::Error && response.payload == b"peer_rotated" {
        SendAttempt::PeerRotated
    } else if response.kind == RemoteKind::Error && response.payload == b"replay_collision" {
        SendAttempt::ReplayCollision
    } else {
        SendAttempt::Retry
    }
}

fn cancel_streams(send: &mut SendStream, receive: &mut RecvStream) {
    let _ = send.reset(CANCEL_CODE);
    let _ = receive.stop(CANCEL_CODE);
}

async fn incoming_worker(endpoint: Endpoint, state: Arc<RuntimeState>) {
    let connection_slots = Arc::new(Semaphore::new(
        state.inbound_slots.available_permits().max(1),
    ));
    loop {
        let Ok(permit) = connection_slots.clone().acquire_owned().await else {
            return;
        };
        let Some(incoming) = endpoint.accept().await else {
            return;
        };
        let connection_state = state.clone();
        tokio::spawn(async move {
            let _permit = permit;
            let Ok(connection) = incoming.await else {
                return;
            };
            let Some(expected) = connection_state.peer_binding().await else {
                connection.close(REJECT_CODE, b"unconfigured");
                return;
            };
            if connection.remote_id() != expected.address.id {
                connection.close(REJECT_CODE, b"identity");
                return;
            }
            let generation_changes = connection_state.peer_generation.subscribe();
            handle_remote_connection(
                connection,
                connection_state,
                expected.generation,
                generation_changes,
            )
            .await;
        });
    }
}

async fn handle_remote_connection(
    connection: iroh::endpoint::Connection,
    state: Arc<RuntimeState>,
    connection_generation: u64,
    mut generation_changes: watch::Receiver<u64>,
) {
    loop {
        let remote_id = connection.remote_id();
        if !state.peer_matches(remote_id, connection_generation).await {
            connection.close(REJECT_CODE, b"peer_rotated");
            return;
        }
        let streams = tokio::select! {
            streams = connection.accept_bi() => streams,
            changed = generation_changes.changed() => {
                let _ = changed;
                connection.close(REJECT_CODE, b"peer_rotated");
                return;
            }
        };
        let (mut send, mut receive) = match streams {
            Ok(streams) => streams,
            Err(_) => return,
        };
        let request = match timeout(
            STREAM_IO_TIMEOUT,
            receive.read_to_end(REMOTE_MAX_TRANSFER_BYTES),
        )
        .await
        {
            Ok(Ok(request)) => request,
            _ => {
                cancel_streams(&mut send, &mut receive);
                continue;
            }
        };
        let request = match decode_remote_frame(&request) {
            Ok(request) if request.kind == RemoteKind::Transfer => request,
            _ => {
                cancel_streams(&mut send, &mut receive);
                continue;
            }
        };
        if request.payload.len() < REMOTE_GENERATION_BYTES {
            cancel_streams(&mut send, &mut receive);
            continue;
        }
        let generation = u64::from_be_bytes(
            request.payload[..REMOTE_GENERATION_BYTES]
                .try_into()
                .expect("generation prefix has fixed length"),
        );
        let router_frame = request.payload[REMOTE_GENERATION_BYTES..].to_vec();

        let (response_kind, response_payload) = if generation != connection_generation
            || validate_router_ingress(&router_frame).is_err()
        {
            if generation != connection_generation {
                (RemoteKind::Error, b"peer_rotated".as_slice())
            } else {
                (RemoteKind::Error, b"invalid_frame".as_slice())
            }
        } else {
            match state
                .admit_remote_inbound(
                    remote_id,
                    connection_generation,
                    request.message_id,
                    router_frame,
                )
                .await
            {
                AdmissionOutcome::Admitted(control)
                | AdmissionOutcome::PendingDuplicate(control) => {
                    match timeout(STREAM_IO_TIMEOUT, control.wait_for_terminal()).await {
                        Ok(InboundTerminal::Acknowledged) => (RemoteKind::Ack, b"".as_slice()),
                        Ok(InboundTerminal::PeerRotated) => {
                            (RemoteKind::Error, b"peer_rotated".as_slice())
                        }
                        Err(_) => {
                            cancel_streams(&mut send, &mut receive);
                            continue;
                        }
                    }
                }
                AdmissionOutcome::CompletedDuplicate => (RemoteKind::Ack, b"".as_slice()),
                AdmissionOutcome::ReplayCollision => {
                    (RemoteKind::Error, b"replay_collision".as_slice())
                }
                AdmissionOutcome::PeerRotated => (RemoteKind::Error, b"peer_rotated".as_slice()),
                AdmissionOutcome::Full => (RemoteKind::Error, b"queue_full".as_slice()),
            }
        };
        let response =
            match encode_remote_frame(response_kind, request.message_id, response_payload) {
                Ok(response) => response,
                Err(_) => {
                    cancel_streams(&mut send, &mut receive);
                    continue;
                }
            };
        let write_result = timeout(STREAM_IO_TIMEOUT, send.write_all(&response)).await;
        if !matches!(write_result, Ok(Ok(()))) || send.finish().is_err() {
            cancel_streams(&mut send, &mut receive);
        }
    }
}

pub fn log_event(event: &'static str) {
    let encoded = serde_json::to_string(&serde_json::json!({ "event": event }))
        .unwrap_or_else(|_| "{\"event\":\"logging_failure\"}".to_owned());
    eprintln!("{encoded}");
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::{MessageType, encode_frame};
    use serde_json::json;

    fn valid_frame() -> Vec<u8> {
        let body = json!({ "accepted": true });
        encode_frame(
            &MessageType::ReservationResult,
            body.as_object().unwrap(),
            b"payload",
        )
        .unwrap()
    }

    #[tokio::test]
    async fn inbound_disconnect_redelivers_until_ack() {
        let state = RuntimeState::new(1, false);
        let payload = valid_frame();
        assert!(matches!(
            state.admit_inbound([1; 16], payload.clone()).await,
            AdmissionOutcome::Admitted(_)
        ));
        assert_eq!(
            state.receive(7, Duration::ZERO).await,
            Some(([1; 16], 0, payload.clone()))
        );
        state.redeliver_session(7).await;
        assert_eq!(
            state.receive(8, Duration::ZERO).await,
            Some(([1; 16], 0, payload))
        );
        assert!(state.ack_inbound(8, [1; 16]).await);
        assert!(state.receive(8, Duration::ZERO).await.is_none());
    }

    #[tokio::test]
    async fn queues_are_bounded_and_duplicates_do_not_consume_slots() {
        let state = RuntimeState::new(1, false);
        assert!(matches!(
            state.admit_inbound([1; 16], valid_frame()).await,
            AdmissionOutcome::Admitted(_)
        ));
        assert!(matches!(
            state.admit_inbound([1; 16], valid_frame()).await,
            AdmissionOutcome::PendingDuplicate(_)
        ));
        assert!(matches!(
            state.admit_inbound([2; 16], valid_frame()).await,
            AdmissionOutcome::Full
        ));
    }

    #[tokio::test]
    async fn cancellation_is_explicit_and_releases_capacity_before_worker_traversal() {
        let state = RuntimeState::new(1, false);
        assert!(!state.cancel_outbound([9; 16]).await);
        assert!(matches!(
            state.enqueue_outbound([1; 16], valid_frame(), None).await,
            EnqueueOutcome::Queued(_)
        ));
        assert!(state.cancel_outbound([1; 16]).await);
        assert!(!state.cancel_outbound([1; 16]).await);
        assert_eq!(state.outbound_slots.available_permits(), 1);
        assert!(matches!(
            state.enqueue_outbound([2; 16], valid_frame(), None).await,
            EnqueueOutcome::Queued(_)
        ));
    }

    #[tokio::test]
    async fn active_inbound_id_is_never_evicted_by_completed_history() {
        let state = RuntimeState::new(2, false);
        let active_id = [1; 16];
        assert!(matches!(
            state.admit_inbound(active_id, valid_frame()).await,
            AdmissionOutcome::Admitted(_)
        ));
        assert!(state.receive(7, Duration::ZERO).await.is_some());

        for counter in 2_u8..=70 {
            let completed_id = [counter; 16];
            assert!(matches!(
                state.admit_inbound(completed_id, valid_frame()).await,
                AdmissionOutcome::Admitted(_)
            ));
            assert!(state.receive(8, Duration::ZERO).await.is_some());
            assert!(state.ack_inbound(8, completed_id).await);
        }

        assert!(matches!(
            state.admit_inbound(active_id, valid_frame()).await,
            AdmissionOutcome::PendingDuplicate(_)
        ));
    }
}
