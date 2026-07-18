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
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::{Mutex, Notify, OwnedSemaphorePermit, RwLock, Semaphore};
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

type MessageId = [u8; 16];

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
        let response = process_local_record(record, session_id, &state).await;
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
        RecordKind::Send => {
            if validate_router_ingress(&record.payload).is_err() {
                return error_response(record.message_id, "invalid_frame");
            }
            match state
                .enqueue_outbound(record.message_id, record.payload)
                .await
            {
                EnqueueOutcome::Queued | EnqueueOutcome::Duplicate => {
                    ack_response(record.message_id)
                }
                EnqueueOutcome::Full => error_response(record.message_id, "queue_full"),
            }
        }
        RecordKind::Receive => match state.receive(session_id, RECEIVE_POLL_WAIT).await {
            Some((message_id, payload)) => ResponseRecord {
                kind: RecordKind::Delivery,
                message_id,
                payload,
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

struct RuntimeState {
    peer: RwLock<Option<EndpointAddr>>,
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
    seen: HashSet<MessageId>,
    seen_order: VecDeque<MessageId>,
}

struct InboundItem {
    message_id: MessageId,
    payload: Vec<u8>,
    _permit: OwnedSemaphorePermit,
}

struct OutboundItem {
    message_id: MessageId,
    payload: Vec<u8>,
    target: Option<EndpointAddr>,
    control: Arc<OutboundControl>,
}

struct OutboundControl {
    cancellation: CancellationToken,
    permit: Mutex<Option<OwnedSemaphorePermit>>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum EnqueueOutcome {
    Queued,
    Duplicate,
    Full,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum AdmissionOutcome {
    Admitted,
    Duplicate,
    Full,
}

impl RuntimeState {
    fn new(capacity: usize, local_only: bool) -> Arc<Self> {
        let seen_limit = capacity.saturating_mul(8).max(64);
        Arc::new(Self {
            peer: RwLock::new(None),
            peer_changed: Notify::new(),
            local_only,
            inbound: Mutex::new(InboundState {
                pending: VecDeque::with_capacity(capacity),
                inflight: HashMap::new(),
                seen: HashSet::with_capacity(seen_limit),
                seen_order: VecDeque::with_capacity(seen_limit),
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
        if endpoint_id != configuration.endpoint_addr.id {
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
        *self.peer.write().await = Some(configuration.endpoint_addr);
        self.peer_changed.notify_waiters();
        Ok(())
    }

    async fn peer_addr(&self) -> Option<EndpointAddr> {
        self.peer.read().await.clone()
    }

    async fn peer_id(&self) -> Option<EndpointId> {
        self.peer.read().await.as_ref().map(|address| address.id)
    }

    async fn enqueue_outbound(&self, message_id: MessageId, payload: Vec<u8>) -> EnqueueOutcome {
        let target = self.peer_addr().await;
        let permit = match self.outbound_slots.clone().try_acquire_owned() {
            Ok(permit) => permit,
            Err(_) => return EnqueueOutcome::Full,
        };
        let control = Arc::new(OutboundControl {
            cancellation: CancellationToken::new(),
            permit: Mutex::new(Some(permit)),
        });
        {
            let mut tokens = self.outbound_tokens.lock().await;
            if tokens.contains_key(&message_id) {
                return EnqueueOutcome::Duplicate;
            }
            tokens.insert(message_id, control.clone());
        }
        let item = OutboundItem {
            message_id,
            payload,
            target,
            control: control.clone(),
        };
        self.outbound.lock().await.push_back(item);
        self.outbound_ready.notify_one();
        EnqueueOutcome::Queued
    }

    async fn cancel_outbound(&self, message_id: MessageId) -> bool {
        let control = self.outbound_tokens.lock().await.remove(&message_id);
        if let Some(control) = control {
            control.cancellation.cancel();
            let mut outbound = self.outbound.lock().await;
            if let Some(position) = outbound
                .iter()
                .position(|item| item.message_id == message_id)
            {
                outbound.remove(position);
            }
            drop(outbound);
            control.permit.lock().await.take();
            true
        } else {
            false
        }
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

    async fn finish_outbound(&self, message_id: MessageId) {
        if let Some(control) = self.outbound_tokens.lock().await.remove(&message_id) {
            control.permit.lock().await.take();
        }
    }

    async fn admit_inbound(&self, message_id: MessageId, payload: Vec<u8>) -> AdmissionOutcome {
        let permit = match self.inbound_slots.clone().try_acquire_owned() {
            Ok(permit) => permit,
            Err(_) => {
                if self.inbound.lock().await.seen.contains(&message_id) {
                    return AdmissionOutcome::Duplicate;
                }
                return AdmissionOutcome::Full;
            }
        };
        let mut inbound = self.inbound.lock().await;
        if inbound.seen.contains(&message_id) {
            return AdmissionOutcome::Duplicate;
        }
        inbound.seen.insert(message_id);
        inbound.seen_order.push_back(message_id);
        while inbound.seen_order.len() > self.seen_limit {
            let candidates = inbound.seen_order.len();
            let mut evicted = false;
            for _ in 0..candidates {
                let Some(expired) = inbound.seen_order.pop_front() else {
                    break;
                };
                let active = expired == message_id
                    || inbound
                        .pending
                        .iter()
                        .any(|item| item.message_id == expired)
                    || inbound
                        .inflight
                        .values()
                        .any(|items| items.iter().any(|item| item.message_id == expired));
                if active {
                    inbound.seen_order.push_back(expired);
                } else {
                    inbound.seen.remove(&expired);
                    evicted = true;
                    break;
                }
            }
            if !evicted {
                break;
            }
        }
        inbound.pending.push_back(InboundItem {
            message_id,
            payload,
            _permit: permit,
        });
        drop(inbound);
        self.inbound_ready.notify_waiters();
        AdmissionOutcome::Admitted
    }

    async fn receive(
        &self,
        session_id: u64,
        maximum_wait: Duration,
    ) -> Option<(MessageId, Vec<u8>)> {
        let deadline = Instant::now() + maximum_wait;
        loop {
            let notified = self.inbound_ready.notified();
            {
                let mut inbound = self.inbound.lock().await;
                if let Some(item) = inbound.pending.pop_front() {
                    let result = (item.message_id, item.payload.clone());
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
        let mut inbound = self.inbound.lock().await;
        let Some(items) = inbound.inflight.get_mut(&session_id) else {
            return false;
        };
        let Some(position) = items.iter().position(|item| item.message_id == message_id) else {
            return false;
        };
        items.remove(position);
        if items.is_empty() {
            inbound.inflight.remove(&session_id);
        }
        true
    }

    async fn redeliver_session(&self, session_id: u64) {
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
        loop {
            if item.control.cancellation.is_cancelled() {
                break;
            }
            let peer = match bound_peer.clone() {
                Some(peer) => peer,
                None => match state.peer_addr().await {
                    Some(peer) => {
                        bound_peer = Some(peer.clone());
                        peer
                    }
                    None => {
                        tokio::select! {
                            () = item.control.cancellation.cancelled() => break,
                            () = state.peer_changed.notified() => {},
                            () = sleep(INITIAL_RETRY_DELAY) => {},
                        }
                        continue;
                    }
                },
            };
            match send_outbound_once(&endpoint, &peer, &item).await {
                SendAttempt::Delivered | SendAttempt::Cancelled => break,
                SendAttempt::Retry => {
                    tokio::select! {
                        () = item.control.cancellation.cancelled() => break,
                        () = state.peer_changed.notified() => {},
                        () = sleep(retry_delay) => {},
                    }
                    retry_delay = retry_delay.saturating_mul(2).min(MAX_RETRY_DELAY);
                }
            }
        }
        state.finish_outbound(item.message_id).await;
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SendAttempt {
    Delivered,
    Cancelled,
    Retry,
}

async fn send_outbound_once(
    endpoint: &Endpoint,
    peer: &EndpointAddr,
    item: &OutboundItem,
) -> SendAttempt {
    let connection = tokio::select! {
        () = item.control.cancellation.cancelled() => return SendAttempt::Cancelled,
        result = timeout(STREAM_IO_TIMEOUT, endpoint.connect(peer.clone(), IROH_ALPN)) => {
            match result {
                Ok(Ok(connection)) => connection,
                _ => return SendAttempt::Retry,
            }
        }
    };
    if connection.remote_id() != peer.id {
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
    let encoded = match encode_remote_frame(RemoteKind::Transfer, item.message_id, &item.payload) {
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
    if response.kind == RemoteKind::Ack
        && response.message_id == item.message_id
        && response.payload.is_empty()
    {
        SendAttempt::Delivered
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
            let Some(expected) = connection_state.peer_id().await else {
                connection.close(REJECT_CODE, b"unconfigured");
                return;
            };
            if connection.remote_id() != expected {
                connection.close(REJECT_CODE, b"identity");
                return;
            }
            handle_remote_connection(connection, connection_state).await;
        });
    }
}

async fn handle_remote_connection(
    connection: iroh::endpoint::Connection,
    state: Arc<RuntimeState>,
) {
    loop {
        if state.peer_id().await != Some(connection.remote_id()) {
            connection.close(REJECT_CODE, b"identity");
            return;
        }
        let (mut send, mut receive) = match connection.accept_bi().await {
            Ok(streams) => streams,
            Err(_) => return,
        };
        let request = match timeout(
            STREAM_IO_TIMEOUT,
            receive.read_to_end(REMOTE_MAX_FRAME_BYTES),
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

        let (response_kind, response_payload) =
            if validate_router_ingress(&request.payload).is_err() {
                (RemoteKind::Error, b"invalid_frame".as_slice())
            } else {
                match state
                    .admit_inbound(request.message_id, request.payload)
                    .await
                {
                    AdmissionOutcome::Admitted | AdmissionOutcome::Duplicate => {
                        (RemoteKind::Ack, b"".as_slice())
                    }
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
        assert_eq!(
            state.admit_inbound([1; 16], payload.clone()).await,
            AdmissionOutcome::Admitted
        );
        assert_eq!(
            state.receive(7, Duration::ZERO).await,
            Some(([1; 16], payload.clone()))
        );
        state.redeliver_session(7).await;
        assert_eq!(
            state.receive(8, Duration::ZERO).await,
            Some(([1; 16], payload))
        );
        assert!(state.ack_inbound(8, [1; 16]).await);
        assert!(state.receive(8, Duration::ZERO).await.is_none());
    }

    #[tokio::test]
    async fn queues_are_bounded_and_duplicates_do_not_consume_slots() {
        let state = RuntimeState::new(1, false);
        assert_eq!(
            state.admit_inbound([1; 16], valid_frame()).await,
            AdmissionOutcome::Admitted
        );
        assert_eq!(
            state.admit_inbound([1; 16], valid_frame()).await,
            AdmissionOutcome::Duplicate
        );
        assert_eq!(
            state.admit_inbound([2; 16], valid_frame()).await,
            AdmissionOutcome::Full
        );
    }

    #[tokio::test]
    async fn cancellation_is_explicit_and_releases_capacity_before_worker_traversal() {
        let state = RuntimeState::new(1, false);
        assert!(!state.cancel_outbound([9; 16]).await);
        assert_eq!(
            state.enqueue_outbound([1; 16], valid_frame()).await,
            EnqueueOutcome::Queued
        );
        assert!(state.cancel_outbound([1; 16]).await);
        assert!(!state.cancel_outbound([1; 16]).await);
        assert_eq!(state.outbound_slots.available_permits(), 1);
        assert_eq!(
            state.enqueue_outbound([2; 16], valid_frame()).await,
            EnqueueOutcome::Queued
        );
    }

    #[tokio::test]
    async fn active_inbound_id_is_never_evicted_by_completed_history() {
        let state = RuntimeState::new(2, false);
        let active_id = [1; 16];
        assert_eq!(
            state.admit_inbound(active_id, valid_frame()).await,
            AdmissionOutcome::Admitted
        );
        assert!(state.receive(7, Duration::ZERO).await.is_some());

        for counter in 2_u8..=70 {
            let completed_id = [counter; 16];
            assert_eq!(
                state.admit_inbound(completed_id, valid_frame()).await,
                AdmissionOutcome::Admitted
            );
            assert!(state.receive(8, Duration::ZERO).await.is_some());
            assert!(state.ack_inbound(8, completed_id).await);
        }

        assert_eq!(
            state.admit_inbound(active_id, valid_frame()).await,
            AdmissionOutcome::Duplicate
        );
    }
}
