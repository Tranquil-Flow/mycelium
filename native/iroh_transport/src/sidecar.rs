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
use std::os::unix::fs::{FileTypeExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use iroh::endpoint::{RecvStream, SendStream, VarInt, presets};
use iroh::{Endpoint, EndpointAddr, EndpointId, RelayMode, SecretKey, TransportAddr};
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

const STREAM_IO_TIMEOUT: Duration = Duration::from_secs(60);
const LOCAL_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(2);
const INITIAL_RETRY_DELAY: Duration = Duration::from_millis(100);
const MAX_RETRY_DELAY: Duration = Duration::from_secs(5);
const CANCEL_CODE: VarInt = VarInt::from_u32(7);
const REJECT_CODE: VarInt = VarInt::from_u32(8);
const MAX_LOCAL_SESSIONS: usize = 16;
const REMOTE_GENERATION_BYTES: usize = 8;
/// Raw ed25519 public key length, the on-wire size of a routed destination.
const ENDPOINT_ID_BYTES: usize = 32;
const LOCAL_CONFIRMED_GENERATION_BYTES: usize = REMOTE_GENERATION_BYTES * 2;
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

pub struct SidecarConfig {
    pub uds: PathBuf,
    pub queue_capacity: NonZeroUsize,
    pub local_only: bool,
    pub endpoint_secret: Option<Zeroizing<[u8; 32]>>,
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
    EndpointSecretSecurity,
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
            Self::EndpointSecretSecurity => "endpoint_secret_security_error",
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

/// Reads exactly 32 endpoint-secret bytes from an owned mode-0600 regular file.
///
/// O_NOFOLLOW prevents a symlink swap from redirecting the sidecar into reading
/// unrelated host secrets before descriptor metadata is validated.
pub fn read_endpoint_secret(path: &Path) -> Result<Zeroizing<[u8; 32]>, SidecarError> {
    let mut file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
        .map_err(|_| SidecarError::EndpointSecretSecurity)?;
    let metadata = file
        .metadata()
        .map_err(|_| SidecarError::EndpointSecretSecurity)?;
    // SAFETY: geteuid has no preconditions and dereferences no pointers.
    let effective_uid = unsafe { libc::geteuid() };
    if !metadata.file_type().is_file()
        || metadata.uid() != effective_uid
        || metadata.mode() & 0o7777 != 0o600
        || metadata.len() != 32
    {
        return Err(SidecarError::EndpointSecretSecurity);
    }
    let mut bytes = Vec::with_capacity(33);
    file.by_ref()
        .take(33)
        .read_to_end(&mut bytes)
        .map_err(|_| SidecarError::EndpointSecretSecurity)?;
    if bytes.len() != 32 {
        bytes.zeroize();
        return Err(SidecarError::EndpointSecretSecurity);
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
    let endpoint = bind_endpoint(config.local_only, config.endpoint_secret.as_deref()).await?;
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

async fn bind_endpoint(
    local_only: bool,
    endpoint_secret: Option<&[u8; 32]>,
) -> Result<Endpoint, SidecarError> {
    let mut builder = if local_only {
        Endpoint::builder(presets::Minimal)
            .relay_mode(RelayMode::Disabled)
            .clear_ip_transports()
            .bind_addr(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))
            .map_err(|_| SidecarError::EndpointBind)?
    } else {
        Endpoint::builder(presets::N0)
    };
    if let Some(secret) = endpoint_secret {
        builder = builder.secret_key(SecretKey::from_bytes(secret));
    }
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

/// Atomic multi-peer configuration for explicitly routed topologies.
///
/// The whole set is validated before any binding is installed, so a single
/// malformed or stale entry leaves the existing routing table untouched.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConfigurePeersPayload {
    peers: Vec<ConfigurePeerPayload>,
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
        RecordKind::Send | RecordKind::SendConfirmed | RecordKind::SendRouted => {
            let routed = record.kind == RecordKind::SendRouted;
            let confirmed = record.kind == RecordKind::SendConfirmed || routed;
            // A routed record prefixes the raw destination EndpointId, then
            // carries the same generation prefixes as a confirmed send.
            let (record, destination) = if routed {
                if record.payload.len() < ENDPOINT_ID_BYTES {
                    return error_response(record.message_id, "invalid_peer");
                }
                let raw: [u8; ENDPOINT_ID_BYTES] = record.payload[..ENDPOINT_ID_BYTES]
                    .try_into()
                    .expect("destination prefix has fixed length");
                let Ok(destination) = EndpointId::from_bytes(&raw) else {
                    return error_response(record.message_id, "invalid_peer");
                };
                let mut record = record;
                record.payload.drain(..ENDPOINT_ID_BYTES);
                (record, Some(destination))
            } else {
                (record, None)
            };
            let (payload, expected_generation, source_generation) = if confirmed {
                if record.payload.len() < LOCAL_CONFIRMED_GENERATION_BYTES {
                    return error_response(record.message_id, "invalid_generation");
                }
                let expected_generation = u64::from_be_bytes(
                    record.payload[..REMOTE_GENERATION_BYTES]
                        .try_into()
                        .expect("generation prefix has fixed length"),
                );
                let source_generation = u64::from_be_bytes(
                    record.payload[REMOTE_GENERATION_BYTES..LOCAL_CONFIRMED_GENERATION_BYTES]
                        .try_into()
                        .expect("source generation prefix has fixed length"),
                );
                if expected_generation == 0 || source_generation == 0 {
                    return error_response(record.message_id, "invalid_generation");
                }
                (
                    record.payload[LOCAL_CONFIRMED_GENERATION_BYTES..].to_vec(),
                    Some(expected_generation),
                    Some(source_generation),
                )
            } else {
                (record.payload, None, None)
            };
            if validate_router_ingress(&payload).is_err() {
                return error_response(record.message_id, "invalid_frame");
            }
            match state
                .enqueue_outbound(
                    record.message_id,
                    payload,
                    expected_generation,
                    source_generation,
                    destination,
                )
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
        RecordKind::ReceiveFrom => match state.receive_from(session_id, RECEIVE_POLL_WAIT).await {
            Some((message_id, source_endpoint, generation, payload)) => {
                let source = source_endpoint.map_or([0_u8; ENDPOINT_ID_BYTES], |endpoint_id| {
                    *endpoint_id.as_bytes()
                });
                ResponseRecord {
                    kind: RecordKind::DeliveryFrom,
                    message_id,
                    payload: [
                        source.as_slice(),
                        generation.to_be_bytes().as_slice(),
                        payload.as_slice(),
                    ]
                    .concat(),
                }
            }
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
        RecordKind::ConfigurePeers => {
            let Ok(configuration) =
                serde_json::from_slice::<ConfigurePeersPayload>(&record.payload)
            else {
                return error_response(record.message_id, "invalid_peer");
            };
            match state.configure_peers(configuration.peers).await {
                Ok(()) => ack_response(record.message_id),
                Err(()) => error_response(record.message_id, "invalid_peer"),
            }
        }
        RecordKind::Ping => ack_response(record.message_id),
        RecordKind::GetTransportObservations => {
            let payload = serde_json::to_vec(&state.transport_observation_documents().await)
                .unwrap_or_default();
            ResponseRecord {
                kind: RecordKind::TransportObservations,
                message_id: record.message_id,
                payload,
            }
        }
        RecordKind::Delivery
        | RecordKind::DeliveryFrom
        | RecordKind::TransportObservations
        | RecordKind::Error => error_response(record.message_id, "invalid_kind"),
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

/// Routing table of authenticated peers keyed by `EndpointId`.
///
/// `primary` names the binding used by operations that still carry v1
/// single-peer semantics (outbound dispatch, connection generation fencing).
/// Inbound admission accepts any binding in the table, which is what lets a
/// node sit in the middle of a routed topology.
#[derive(Debug, Default)]
struct PeerTable {
    bindings: HashMap<EndpointId, PeerBinding>,
    primary: Option<EndpointId>,
}

impl PeerTable {
    fn primary(&self) -> Option<&PeerBinding> {
        self.primary
            .as_ref()
            .and_then(|endpoint_id| self.bindings.get(endpoint_id))
    }

    fn get(&self, endpoint_id: EndpointId) -> Option<&PeerBinding> {
        self.bindings.get(&endpoint_id)
    }

    fn matches(&self, endpoint_id: EndpointId, generation: u64) -> bool {
        self.bindings
            .get(&endpoint_id)
            .is_some_and(|binding| binding.generation == generation)
    }

    /// Highest generation any binding holds, used as the staleness floor so a
    /// rotation can never move the table backwards on any endpoint.
    fn max_generation(&self) -> u64 {
        self.bindings
            .values()
            .map(|binding| binding.generation)
            .max()
            .unwrap_or(0)
    }

    fn install(&mut self, ordered: Vec<PeerBinding>) {
        self.primary = ordered.first().map(|binding| binding.address.id);
        self.bindings = ordered
            .into_iter()
            .map(|binding| (binding.address.id, binding))
            .collect();
    }
}

struct RuntimeState {
    peers: RwLock<PeerTable>,
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
    outbound_connections: Mutex<HashMap<(EndpointId, u64), iroh::endpoint::Connection>>,
    transport_observations: Mutex<HashMap<(EndpointId, u64), TransportObservationState>>,
}

#[derive(Debug, Default)]
struct TransportObservationState {
    connections_opened: u64,
    frames_sent: u64,
    attempts: u64,
    failures: u64,
    reconnect_count: u64,
    selected_path_changes: u64,
    path_class: String,
    relay_identity: Option<String>,
    cold_rtt_ms: Option<f64>,
    rtt_samples_ms: VecDeque<f64>,
    acknowledged_bytes: u64,
    acknowledged_elapsed_ms: f64,
    measured_at_unix_ms: u64,
}

#[derive(Serialize)]
struct TransportObservationDocument {
    protocol: &'static str,
    remote_endpoint_id: String,
    connection_generation: u64,
    path_class: String,
    relay_identity: Option<String>,
    relay_region: Option<String>,
    cold_rtt_ms: f64,
    warm_rtt_ms: f64,
    observed_goodput_bps: f64,
    jitter_ms: f64,
    loss_ratio: f64,
    sample_count: usize,
    connections_opened: u64,
    frames_sent: u64,
    reconnect_count: u64,
    selected_path_changes: u64,
    measured_at_unix_ms: u64,
}

#[derive(Serialize)]
struct TransportObservationEnvelope {
    protocol: &'static str,
    observations: Vec<TransportObservationDocument>,
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
    source_endpoint: Option<EndpointId>,
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
    source_generation: Option<u64>,
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
            peers: RwLock::new(PeerTable::default()),
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
            outbound_connections: Mutex::new(HashMap::new()),
            transport_observations: Mutex::new(HashMap::new()),
        })
    }

    /// Validate one peer configuration without touching any routing state.
    fn validate_peer(&self, configuration: ConfigurePeerPayload) -> Result<PeerBinding, ()> {
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

        Ok(PeerBinding {
            address: configuration.endpoint_addr,
            generation: configuration.generation,
        })
    }

    /// Install an exclusive single peer, preserving v1 semantics: the incoming
    /// binding becomes the whole table, and any other binding is displaced.
    async fn configure_peer(&self, configuration: ConfigurePeerPayload) -> Result<(), ()> {
        let replacement = self.validate_peer(configuration)?;
        let generation = replacement.generation;
        let mut peers = self.peers.write().await;
        let occupied = !peers.bindings.is_empty();
        if occupied {
            if peers.bindings.len() == 1 && peers.primary() == Some(&replacement) {
                return Ok(());
            }
            if replacement.generation <= peers.max_generation() {
                return Err(());
            }
        }
        peers.install(vec![replacement]);
        if occupied {
            self.fence_outbound_for_rotation().await;
            self.fence_inbound_for_rotation().await;
            self.close_outbound_connections().await;
        }
        drop(peers);
        self.publish_peer_generation(generation);
        Ok(())
    }

    /// Atomically install a routed peer set.
    ///
    /// Every entry is validated and generation-fenced before anything is
    /// installed, so a single bad entry leaves the previous table intact.
    async fn configure_peers(&self, configurations: Vec<ConfigurePeerPayload>) -> Result<(), ()> {
        if configurations.is_empty() {
            return Err(());
        }
        let mut ordered = Vec::with_capacity(configurations.len());
        let mut seen = HashSet::with_capacity(configurations.len());
        for configuration in configurations {
            let binding = self.validate_peer(configuration)?;
            if !seen.insert(binding.address.id) {
                return Err(());
            }
            ordered.push(binding);
        }

        let generation = ordered[0].generation;
        let mut peers = self.peers.write().await;
        let occupied = !peers.bindings.is_empty();
        if occupied {
            let unchanged = peers.bindings.len() == ordered.len()
                && peers.primary() == Some(&ordered[0])
                && ordered
                    .iter()
                    .all(|binding| peers.bindings.get(&binding.address.id) == Some(binding));
            if unchanged {
                return Ok(());
            }
            for binding in &ordered {
                if let Some(existing) = peers.bindings.get(&binding.address.id)
                    && binding.generation <= existing.generation
                {
                    return Err(());
                }
            }
        }
        peers.install(ordered);
        if occupied {
            self.fence_outbound_for_rotation().await;
            self.fence_inbound_for_rotation().await;
            self.close_outbound_connections().await;
        }
        drop(peers);
        self.publish_peer_generation(generation);
        Ok(())
    }

    fn publish_peer_generation(&self, generation: u64) {
        self.peer_generation.send_replace(generation);
        self.peer_changed.notify_waiters();
        self.inbound_ready.notify_waiters();
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

    async fn peer_matches(&self, endpoint_id: EndpointId, generation: u64) -> bool {
        self.peers.read().await.matches(endpoint_id, generation)
    }

    async fn reusable_connection(
        &self,
        endpoint: &Endpoint,
        peer: &PeerBinding,
    ) -> Result<iroh::endpoint::Connection, ()> {
        let key = (peer.address.id, peer.generation);
        if let Some(connection) = self.outbound_connections.lock().await.get(&key) {
            return Ok(connection.clone());
        }
        let connection = timeout(
            STREAM_IO_TIMEOUT,
            endpoint.connect(peer.address.clone(), IROH_ALPN),
        )
        .await
        .map_err(|_| ())?
        .map_err(|_| ())?;
        if connection.remote_id() != peer.address.id {
            connection.close(REJECT_CODE, b"identity");
            return Err(());
        }
        let mut connections = self.outbound_connections.lock().await;
        if let Some(existing) = connections.get(&key) {
            connection.close(REJECT_CODE, b"duplicate");
            return Ok(existing.clone());
        }
        connections.insert(key, connection.clone());
        drop(connections);
        let (path_class, relay_identity, rtt_ms) = selected_path_observation(&connection);
        let mut observations = self.transport_observations.lock().await;
        let observation = observations.entry(key).or_default();
        if observation.connections_opened > 0 {
            observation.reconnect_count = observation.reconnect_count.saturating_add(1);
        }
        observation.connections_opened = observation.connections_opened.saturating_add(1);
        observation.measured_at_unix_ms = unix_time_ms();
        update_selected_path(observation, path_class, relay_identity, rtt_ms);
        log_transport_event(
            "transport_connection_opened",
            peer.address.id,
            peer.generation,
        );
        Ok(connection)
    }

    async fn evict_connection(&self, peer: &PeerBinding) {
        let key = (peer.address.id, peer.generation);
        if let Some(connection) = self.outbound_connections.lock().await.remove(&key) {
            connection.close(REJECT_CODE, b"retry");
        }
        let mut observations = self.transport_observations.lock().await;
        let observation = observations.entry(key).or_default();
        observation.failures = observation.failures.saturating_add(1);
        observation.measured_at_unix_ms = unix_time_ms();
    }

    async fn note_attempt(&self, peer: &PeerBinding) {
        let key = (peer.address.id, peer.generation);
        let mut observations = self.transport_observations.lock().await;
        let observation = observations.entry(key).or_default();
        observation.attempts = observation.attempts.saturating_add(1);
        observation.measured_at_unix_ms = unix_time_ms();
    }

    async fn note_delivered(
        &self,
        peer: &PeerBinding,
        connection: &iroh::endpoint::Connection,
        payload_bytes: usize,
        elapsed: Duration,
    ) {
        let key = (peer.address.id, peer.generation);
        let (path_class, relay_identity, rtt_ms) = selected_path_observation(connection);
        let mut observations = self.transport_observations.lock().await;
        let observation = observations.entry(key).or_default();
        observation.frames_sent = observation.frames_sent.saturating_add(1);
        observation.acknowledged_bytes = observation
            .acknowledged_bytes
            .saturating_add(payload_bytes as u64);
        observation.acknowledged_elapsed_ms += elapsed.as_secs_f64() * 1_000.0;
        observation.measured_at_unix_ms = unix_time_ms();
        update_selected_path(observation, path_class, relay_identity, rtt_ms);
    }

    async fn transport_observation_documents(&self) -> TransportObservationEnvelope {
        let peers = self.peers.read().await;
        let observations = self.transport_observations.lock().await;
        let mut documents = peers
            .bindings
            .values()
            .map(|peer| {
                let key = (peer.address.id, peer.generation);
                let state = observations.get(&key);
                transport_observation_document(peer, state)
            })
            .collect::<Vec<_>>();
        documents.sort_by(|left, right| left.remote_endpoint_id.cmp(&right.remote_endpoint_id));
        TransportObservationEnvelope {
            protocol: "mycelium.iroh_sidecar.transport_observations.v1",
            observations: documents,
        }
    }

    async fn close_outbound_connections(&self) {
        let connections = std::mem::take(&mut *self.outbound_connections.lock().await);
        for (_, connection) in connections {
            connection.close(REJECT_CODE, b"peer_rotated");
        }
    }

    async fn bind_outbound(&self, control: &OutboundControl) -> Option<PeerBinding> {
        let peers = self.peers.read().await;
        let binding = peers.primary()?.clone();
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

    /// Queue a frame for dispatch.
    ///
    /// `destination` names an explicit peer for routed topologies; when it is
    /// absent the primary binding is used, preserving v1 point-to-point sends.
    /// An unknown destination fails closed rather than falling back.
    async fn enqueue_outbound(
        &self,
        message_id: MessageId,
        payload: Vec<u8>,
        expected_generation: Option<u64>,
        source_generation: Option<u64>,
        destination: Option<EndpointId>,
    ) -> EnqueueOutcome {
        let digest = frame_digest(&payload);
        let peers = self.peers.read().await;
        let routed = match destination {
            Some(endpoint_id) => match peers.get(endpoint_id) {
                Some(binding) => Some(binding.clone()),
                None => return EnqueueOutcome::PeerRotated,
            },
            None => peers.primary().cloned(),
        };
        if expected_generation
            .is_some_and(|generation| routed.as_ref().map(|b| b.generation) != Some(generation))
        {
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
        let target = routed;
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
            source_generation,
            target,
            control: control.clone(),
        };
        tokens.insert(message_id, control.clone());
        self.outbound.lock().await.push_back(item);
        drop(tokens);
        drop(peers);
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
        let source_endpoint = origin.as_ref().map(|(endpoint_id, _)| *endpoint_id);
        let generation = origin.as_ref().map_or(0, |(_, generation)| *generation);
        let peers = self.peers.read().await;
        if let Some((endpoint_id, generation)) = origin {
            // Any binding in the routing table is a legitimate origin, not just
            // the primary: a routed node receives from several upstream peers.
            if !peers.matches(endpoint_id, generation) {
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
            source_endpoint,
            generation,
            payload,
            control: control.clone(),
            _permit: permit,
        });
        drop(inbound);
        drop(peers);
        self.inbound_ready.notify_waiters();
        AdmissionOutcome::Admitted(control)
    }

    async fn receive(
        &self,
        session_id: u64,
        maximum_wait: Duration,
    ) -> Option<(MessageId, u64, Vec<u8>)> {
        self.receive_item(session_id, maximum_wait).await.map(
            |(message_id, _source_endpoint, generation, payload)| (message_id, generation, payload),
        )
    }

    async fn receive_from(
        &self,
        session_id: u64,
        maximum_wait: Duration,
    ) -> Option<(MessageId, Option<EndpointId>, u64, Vec<u8>)> {
        self.receive_item(session_id, maximum_wait).await
    }

    async fn receive_item(
        &self,
        session_id: u64,
        maximum_wait: Duration,
    ) -> Option<(MessageId, Option<EndpointId>, u64, Vec<u8>)> {
        let deadline = Instant::now() + maximum_wait;
        loop {
            let notified = self.inbound_ready.notified();
            {
                let _peers = self.peers.read().await;
                let mut inbound = self.inbound.lock().await;
                if let Some(item) = inbound.pending.pop_front() {
                    let result = (
                        item.message_id,
                        item.source_endpoint,
                        item.generation,
                        item.payload.clone(),
                    );
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
        let _peers = self.peers.read().await;
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
        let _peers = self.peers.read().await;
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

fn unix_time_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX)
}

fn selected_path_observation(
    connection: &iroh::endpoint::Connection,
) -> (String, Option<String>, Option<f64>) {
    let paths = connection.paths();
    let Some(path) = paths.iter().find(|path| path.is_selected()) else {
        return ("unknown".to_owned(), None, None);
    };
    let path_class = if path.is_ip() {
        "direct"
    } else if path.is_relay() {
        "relay"
    } else {
        "unknown"
    };
    let relay_identity = path.is_relay().then(|| path.remote_addr().to_string());
    (
        path_class.to_owned(),
        relay_identity,
        Some(path.rtt().as_secs_f64() * 1_000.0),
    )
}

fn update_selected_path(
    state: &mut TransportObservationState,
    path_class: String,
    relay_identity: Option<String>,
    rtt_ms: Option<f64>,
) {
    if !state.path_class.is_empty() && state.path_class != path_class {
        state.selected_path_changes = state.selected_path_changes.saturating_add(1);
    } else if state.path_class.is_empty() && path_class != "unknown" {
        state.selected_path_changes = state.selected_path_changes.saturating_add(1);
    }
    state.path_class = path_class;
    state.relay_identity = relay_identity;
    if let Some(rtt_ms) = rtt_ms.filter(|value| value.is_finite() && *value >= 0.0) {
        state.cold_rtt_ms.get_or_insert(rtt_ms);
        state.rtt_samples_ms.push_back(rtt_ms);
        while state.rtt_samples_ms.len() > 64 {
            state.rtt_samples_ms.pop_front();
        }
    }
}

fn transport_observation_document(
    peer: &PeerBinding,
    state: Option<&TransportObservationState>,
) -> TransportObservationDocument {
    let empty = TransportObservationState::default();
    let state = state.unwrap_or(&empty);
    let sample_count = state.rtt_samples_ms.len();
    let warm_rtt_ms = if sample_count == 0 {
        0.0
    } else {
        state.rtt_samples_ms.iter().sum::<f64>() / sample_count as f64
    };
    let jitter_ms = if sample_count == 0 {
        0.0
    } else {
        let variance = state
            .rtt_samples_ms
            .iter()
            .map(|value| (value - warm_rtt_ms).powi(2))
            .sum::<f64>()
            / sample_count as f64;
        variance.sqrt()
    };
    let observed_goodput_bps = if state.acknowledged_elapsed_ms <= 0.0 {
        0.0
    } else {
        state.acknowledged_bytes as f64 / (state.acknowledged_elapsed_ms / 1_000.0)
    };
    let loss_ratio = if state.attempts == 0 {
        0.0
    } else {
        state.failures as f64 / state.attempts as f64
    };
    TransportObservationDocument {
        protocol: "mycelium.iroh_sidecar.transport_observation.v1",
        remote_endpoint_id: peer.address.id.to_string(),
        connection_generation: peer.generation,
        path_class: if state.path_class.is_empty() {
            "unknown".to_owned()
        } else {
            state.path_class.clone()
        },
        relay_identity: state.relay_identity.clone(),
        relay_region: None,
        cold_rtt_ms: state.cold_rtt_ms.unwrap_or(0.0),
        warm_rtt_ms,
        observed_goodput_bps,
        jitter_ms,
        loss_ratio,
        sample_count,
        connections_opened: state.connections_opened,
        frames_sent: state.frames_sent,
        reconnect_count: state.reconnect_count,
        selected_path_changes: state.selected_path_changes,
        measured_at_unix_ms: state.measured_at_unix_ms,
    }
}

fn log_transport_event(event: &'static str, endpoint_id: EndpointId, generation: u64) {
    let encoded = serde_json::to_string(&serde_json::json!({
        "event": event,
        "remote_endpoint_id": endpoint_id.to_string(),
        "connection_generation": generation,
    }))
    .unwrap_or_else(|_| "{\"event\":\"logging_failure\"}".to_owned());
    eprintln!("{encoded}");
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
            match send_outbound_once(&endpoint, &state, &peer, &item).await {
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
    state: &Arc<RuntimeState>,
    peer: &PeerBinding,
    item: &OutboundItem,
) -> SendAttempt {
    state.note_attempt(peer).await;
    let connection = tokio::select! {
        () = item.control.cancellation.cancelled() => return SendAttempt::Cancelled,
        result = state.reusable_connection(endpoint, peer) => match result {
            Ok(connection) => connection,
            Err(()) => {
                state.evict_connection(peer).await;
                return SendAttempt::Retry;
            }
        }
    };
    let started = Instant::now();

    let (mut send, mut receive) = tokio::select! {
        () = item.control.cancellation.cancelled() => return SendAttempt::Cancelled,
        result = timeout(STREAM_IO_TIMEOUT, connection.open_bi()) => {
            match result {
                Ok(Ok(streams)) => streams,
                _ => {
                    state.evict_connection(peer).await;
                    return SendAttempt::Retry;
                },
            }
        }
    };
    let source_generation = item.source_generation.unwrap_or(peer.generation);
    let mut transfer = Vec::with_capacity(REMOTE_GENERATION_BYTES + item.payload.len());
    transfer.extend_from_slice(&source_generation.to_be_bytes());
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
        state.evict_connection(peer).await;
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
                    state.evict_connection(peer).await;
                    return SendAttempt::Retry;
                }
            }
        }
    };
    let Ok(response) = decode_remote_frame(&response) else {
        state.evict_connection(peer).await;
        return SendAttempt::Retry;
    };
    if response.message_id != item.message_id {
        state.evict_connection(peer).await;
        return SendAttempt::Retry;
    }
    if response.kind == RemoteKind::Ack && response.payload.is_empty() {
        state
            .note_delivered(peer, &connection, item.payload.len(), started.elapsed())
            .await;
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
            // Admit against the binding for this specific remote so every peer
            // in a routed set can open a connection, not just the primary.
            let expected = {
                let peers = connection_state.peers.read().await;
                if peers.bindings.is_empty() {
                    drop(peers);
                    connection.close(REJECT_CODE, b"unconfigured");
                    return;
                }
                match peers.get(connection.remote_id()) {
                    Some(binding) => binding.clone(),
                    None => {
                        drop(peers);
                        connection.close(REJECT_CODE, b"identity");
                        return;
                    }
                }
            };
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
    use std::fs::OpenOptions;
    use std::io::Write;
    use std::net::SocketAddr;
    use std::os::unix::fs::{OpenOptionsExt, PermissionsExt, symlink};
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temporary_path(label: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!(
            "mycelium-sidecar-{label}-{}-{nonce}",
            std::process::id()
        ))
    }

    #[test]
    fn endpoint_secret_file_requires_owned_regular_mode_0600_bytes() {
        let path = temporary_path("secret");
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)
            .unwrap();
        file.write_all(&[7_u8; 32]).unwrap();
        drop(file);

        let secret = read_endpoint_secret(&path).unwrap();
        assert_eq!(&*secret, &[7_u8; 32]);

        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        assert_eq!(
            read_endpoint_secret(&path),
            Err(SidecarError::EndpointSecretSecurity)
        );
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o4600)).unwrap();
        assert_eq!(
            read_endpoint_secret(&path),
            Err(SidecarError::EndpointSecretSecurity)
        );
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        let link = temporary_path("secret-link");
        symlink(&path, &link).unwrap();
        assert_eq!(
            read_endpoint_secret(&link),
            Err(SidecarError::EndpointSecretSecurity)
        );
        fs::remove_file(link).unwrap();
        fs::remove_file(path).unwrap();
    }

    #[tokio::test]
    async fn explicit_endpoint_secret_stabilizes_endpoint_identity() {
        let secret = Zeroizing::new([9_u8; 32]);
        let first = bind_endpoint(true, Some(&*secret)).await.unwrap();
        let first_id = first.id();
        first.close().await;
        let second = bind_endpoint(true, Some(&*secret)).await.unwrap();
        assert_eq!(second.id(), first_id);
        second.close().await;
    }

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
            state
                .enqueue_outbound([1; 16], valid_frame(), None, None, None)
                .await,
            EnqueueOutcome::Queued(_)
        ));
        assert!(state.cancel_outbound([1; 16]).await);
        assert!(!state.cancel_outbound([1; 16]).await);
        assert_eq!(state.outbound_slots.available_permits(), 1);
        assert!(matches!(
            state
                .enqueue_outbound([2; 16], valid_frame(), None, None, None)
                .await,
            EnqueueOutcome::Queued(_)
        ));
    }

    fn test_endpoint_id(seed: u8) -> EndpointId {
        SecretKey::from_bytes(&[seed; 32]).public()
    }

    fn peer_payload(seed: u8, generation: u64, address: Ipv4Addr) -> ConfigurePeerPayload {
        let endpoint_id = test_endpoint_id(seed);
        let mut endpoint_addr = EndpointAddr::new(endpoint_id);
        endpoint_addr
            .addrs
            .insert(TransportAddr::Ip(SocketAddr::V4(SocketAddrV4::new(
                address,
                20_000 + u16::from(seed),
            ))));
        ConfigurePeerPayload {
            endpoint_id: endpoint_id.to_string(),
            endpoint_addr,
            generation,
        }
    }

    fn loopback_peer(seed: u8, generation: u64) -> ConfigurePeerPayload {
        peer_payload(seed, generation, Ipv4Addr::LOCALHOST)
    }

    fn routable_peer(seed: u8, generation: u64) -> ConfigurePeerPayload {
        peer_payload(seed, generation, Ipv4Addr::new(203, 0, 113, seed))
    }

    /// The peer document Python hands back is the sidecar's own serialized
    /// `EndpointAddr`, so the wire shape must survive a JSON round trip.
    #[test]
    fn configure_peers_payload_round_trips_through_canonical_json() {
        let endpoint_id = test_endpoint_id(1);
        let peer = loopback_peer(1, 2);
        let encoded = serde_json::to_string(&json!({
            "peers": [{
                "endpoint_id": peer.endpoint_id,
                "endpoint_addr": peer.endpoint_addr,
                "generation": peer.generation,
            }],
        }))
        .expect("payload serializes");

        let decoded: ConfigurePeersPayload =
            serde_json::from_str(&encoded).expect("payload deserializes");
        assert_eq!(decoded.peers.len(), 1);
        assert_eq!(decoded.peers[0].endpoint_addr.id, endpoint_id);
        assert_eq!(decoded.peers[0].generation, 2);
    }

    #[tokio::test]
    async fn configure_peers_installs_every_binding_and_designates_the_first_as_primary() {
        let state = RuntimeState::new(4, true);
        state
            .configure_peers(vec![
                loopback_peer(1, 3),
                loopback_peer(2, 3),
                loopback_peer(3, 3),
            ])
            .await
            .expect("atomic multi-peer configuration is accepted");

        for seed in 1_u8..=3 {
            assert!(
                state.peer_matches(test_endpoint_id(seed), 3).await,
                "seed {seed} must be routable after ConfigurePeers"
            );
        }
        assert!(!state.peer_matches(test_endpoint_id(4), 3).await);
        assert!(!state.peer_matches(test_endpoint_id(1), 2).await);

        let primary = state
            .peers
            .read()
            .await
            .primary()
            .cloned()
            .expect("primary binding exists");
        assert_eq!(primary.address.id, test_endpoint_id(1));
        assert_eq!(primary.generation, 3);
        assert_eq!(*state.peer_generation.borrow(), 3);
    }

    #[tokio::test]
    async fn configure_peers_rejects_the_whole_set_when_one_entry_is_stale() {
        let state = RuntimeState::new(4, true);
        state
            .configure_peers(vec![loopback_peer(1, 5), loopback_peer(2, 5)])
            .await
            .expect("initial set is accepted");

        state
            .configure_peers(vec![
                loopback_peer(1, 6),
                loopback_peer(2, 5),
                loopback_peer(3, 6),
            ])
            .await
            .expect_err("a stale entry must reject the entire set");

        assert!(state.peer_matches(test_endpoint_id(1), 5).await);
        assert!(!state.peer_matches(test_endpoint_id(1), 6).await);
        assert!(!state.peer_matches(test_endpoint_id(3), 6).await);
        assert_eq!(*state.peer_generation.borrow(), 5);
    }

    #[tokio::test]
    async fn configure_peers_rejects_duplicates_zero_generations_and_mismatched_ids() {
        let state = RuntimeState::new(4, true);

        state
            .configure_peers(vec![loopback_peer(1, 2), loopback_peer(1, 3)])
            .await
            .expect_err("duplicate endpoint ids must fail closed");

        state
            .configure_peers(vec![loopback_peer(1, 0)])
            .await
            .expect_err("generation zero must fail closed");

        let mut mismatched = loopback_peer(1, 2);
        mismatched.endpoint_id = test_endpoint_id(9).to_string();
        state
            .configure_peers(vec![mismatched])
            .await
            .expect_err("endpoint_id must match endpoint_addr id");

        state
            .configure_peers(vec![routable_peer(1, 2)])
            .await
            .expect_err("local_only sidecars must reject non-loopback peers");

        assert!(state.peers.read().await.primary().is_none());
        assert_eq!(*state.peer_generation.borrow(), 0);
    }

    #[tokio::test]
    async fn configure_peers_fences_displaced_inflight_work() {
        let state = RuntimeState::new(4, true);
        state
            .configure_peers(vec![loopback_peer(1, 1)])
            .await
            .expect("initial set is accepted");
        let EnqueueOutcome::Queued(control) = state
            .enqueue_outbound([1; 16], valid_frame(), None, None, None)
            .await
        else {
            panic!("frame must queue against the configured peer");
        };

        state
            .configure_peers(vec![loopback_peer(1, 2), loopback_peer(2, 2)])
            .await
            .expect("rotation to a larger peer set is accepted");

        assert_eq!(control.terminal(), Some(OutboundTerminal::PeerRotated));
        assert!(state.peer_matches(test_endpoint_id(2), 2).await);
    }

    #[tokio::test]
    async fn single_peer_configuration_remains_exclusive() {
        let state = RuntimeState::new(4, true);
        state
            .configure_peers(vec![loopback_peer(1, 4), loopback_peer(2, 4)])
            .await
            .expect("multi-peer set is accepted");

        state
            .configure_peer(loopback_peer(3, 5))
            .await
            .expect("single-peer rotation is accepted");

        assert!(state.peer_matches(test_endpoint_id(3), 5).await);
        assert!(!state.peer_matches(test_endpoint_id(1), 4).await);
        assert!(!state.peer_matches(test_endpoint_id(2), 4).await);
    }

    #[tokio::test]
    async fn routed_send_targets_a_named_peer_instead_of_the_primary() {
        let state = RuntimeState::new(4, true);
        state
            .configure_peers(vec![loopback_peer(1, 1), loopback_peer(2, 1)])
            .await
            .expect("routed set is accepted");

        assert!(matches!(
            state
                .enqueue_outbound(
                    [1; 16],
                    valid_frame(),
                    None,
                    None,
                    Some(test_endpoint_id(2)),
                )
                .await,
            EnqueueOutcome::Queued(_)
        ));
        let item = state.next_outbound().await;
        let target = item.target.expect("routed send binds a target eagerly");
        assert_eq!(target.address.id, test_endpoint_id(2));
        assert_ne!(target.address.id, test_endpoint_id(1));
    }

    #[tokio::test]
    async fn routed_send_to_an_unconfigured_destination_fails_closed() {
        let state = RuntimeState::new(4, true);
        state
            .configure_peers(vec![loopback_peer(1, 1)])
            .await
            .expect("routed set is accepted");

        assert!(matches!(
            state
                .enqueue_outbound(
                    [3; 16],
                    valid_frame(),
                    None,
                    None,
                    Some(test_endpoint_id(9)),
                )
                .await,
            EnqueueOutcome::PeerRotated
        ));
        // A rejected route must not consume queue capacity.
        assert_eq!(state.outbound_slots.available_permits(), 4);
    }

    #[tokio::test]
    async fn routed_send_checks_the_generation_of_the_named_peer() {
        let state = RuntimeState::new(4, true);
        state
            .configure_peers(vec![loopback_peer(1, 7), loopback_peer(2, 7)])
            .await
            .expect("routed set is accepted");

        assert!(matches!(
            state
                .enqueue_outbound(
                    [4; 16],
                    valid_frame(),
                    Some(6),
                    None,
                    Some(test_endpoint_id(2)),
                )
                .await,
            EnqueueOutcome::PeerRotated
        ));
        assert!(matches!(
            state
                .enqueue_outbound(
                    [5; 16],
                    valid_frame(),
                    Some(7),
                    None,
                    Some(test_endpoint_id(2)),
                )
                .await,
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
