// SPDX-License-Identifier: AGPL-3.0-only
//! Authenticated iroh sidecar runtime.

use std::collections::{HashMap, HashSet, VecDeque};
use std::error::Error;
use std::fmt;
use std::fs;
use std::io::{self, Read};
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4};
use std::num::NonZeroUsize;
#[cfg(unix)]
use std::os::fd::{FromRawFd, RawFd};
#[cfg(unix)]
use std::os::unix::fs::{FileTypeExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use iroh::Watcher as _;
use iroh::endpoint::{ReadToEndError, RecvStream, SendStream, VarInt, presets};
use iroh::{Endpoint, EndpointAddr, EndpointId, RelayMode, SecretKey, TransportAddr};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
#[cfg(unix)]
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::{Mutex, Notify, OwnedSemaphorePermit, RwLock, Semaphore, watch};
use tokio::time::{Instant, sleep, sleep_until, timeout, timeout_at};
use tokio_util::sync::CancellationToken;
use zeroize::{Zeroize, Zeroizing};

use crate::local::{
    ClientHello, LOCAL_MAX_RECORD_BYTES, MAX_HELLO_BYTES, Record, RecordKind, SequenceGuard,
    ServerHello, SessionKeys, decode_record, derive_session_keys, encode_record,
};
use crate::local_ipc::{
    ConnectionOwnerTable, LoopbackListener, authorize_loopback_client, loopback_ready_record,
};
use crate::protocol::{WireError, decode_frame};
use crate::ready_address::{
    READY_ADDRESS_WINDOW, StdoutSink, await_advertised_address, republish_address_changes,
};
use crate::remote::{REMOTE_MAX_FRAME_BYTES, RemoteKind, decode_remote_frame, encode_remote_frame};
use mycelium_mobile_core::artifact_grant::ARTIFACT_CHUNK_ALPN;
use mycelium_mobile_core::artifact_net::ArtifactSource;
use mycelium_mobile_core::node_control::CONTROL_ALPN as NODE_CONTROL_ALPN;

pub const IROH_ALPN: &[u8] = b"mycelium.iroh.sidecar.v1";
pub const OPERATIONAL_MAX_FRAME_BYTES: usize = 16 * 1024 * 1024;
pub const DEFAULT_QUEUE_CAPACITY: usize = 128;
pub const RECEIVE_POLL_WAIT: Duration = Duration::from_millis(250);

const STREAM_IO_TIMEOUT: Duration = Duration::from_secs(60);
/// Bound on one outbound **dial** attempt (wp2-6 defect 2).
///
/// iroh 1.0.2 has no connect timeout of its own: the pinned `connect()` stays
/// pending for as long as the caller lets it (`research/research-iroh.md` §4.1
/// and §6 check 6).  Before this bound the dial inherited the generic 60 s
/// stream-IO budget, so one vanished peer could burn twelve times the hop's
/// whole 5 s Python delivery budget inside a single attempt and surface only as
/// `delivery_deadline_exceeded`.  Five seconds is exactly that hop budget, so a
/// dial can never outlive the caller's own deadline; the caller-visible,
/// registered bound is `mycelium_deadlines.peer_connect_deadline_seconds`
/// (1.0 s), which the Python client applies and names.
const PEER_CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
const LOCAL_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(2);
const INITIAL_RETRY_DELAY: Duration = Duration::from_millis(100);
const MAX_RETRY_DELAY: Duration = Duration::from_secs(5);
/// Hard lifetime of one outbound send inside the sidecar, from enqueue.
///
/// The outbound worker retries until the frame is delivered or Python cancels
/// it. Python cancels when its own delivery budget runs out. That budget is
/// `min(command_timeout, _MAX_IROH_DELIVERY_TIMEOUT_SECONDS)` =
/// `min(900, 240)` = 240 s on physical routes (`physical_inference_node.py`):
/// a confirmed delivery acknowledges only after the receiver's dispatch,
/// which for progressive prefill can run through slower downstream stages.
/// A cancellation can be lost (a wedged or disconnected client), and without
/// a bound of its own a queued frame could then be delivered arbitrarily
/// late, past the relay's 600 s tombstone retention horizon
/// (`relay.py` `_TOMBSTONE_RETENTION_SECONDS`), after which a retired
/// attempt's ManifestLocked could be admitted again. 300 s is that 240 s
/// budget plus a 60 s margin (the sidecar never ends a send Python is still
/// waiting for) and half of the horizon; past it the send ends as
/// `cancelled` whether or not Python's cancellation arrived.
/// `tests/path_cancellation_adversarial` pins both relations.
pub const OUTBOUND_DELIVERY_DEADLINE: Duration = Duration::from_secs(300);
/// M4-host control carrier: one `ControlCall` carries one phone control
/// command document (`mycelium.physical_node_control.v1`) to the phone's
/// control ALPN (`mycelium.node_control.v1`) and returns its exact response
/// bytes.  These bounds are the phone's own contract
/// (`node_control::MAX_COMMAND_BYTES` and the phone's 60 s
/// `CONTROL_COMMAND_TIMEOUT` ceiling), mirrored here so the host carrier can
/// never ask for more than the phone will serve.
pub const CONTROL_CALL_MAX_BYTES: usize = 4 * 1024 * 1024;
pub const CONTROL_CALL_TIMEOUT_CEILING: Duration = Duration::from_secs(60);
/// Control calls in flight at once.  Bounded separately from Router work:
/// the carrier has its own semaphore and never consumes Router queue slots.
/// A caller sharing one synchronous local session still serializes its calls.
pub const CONTROL_CALL_CONCURRENCY: usize = 4;
/// Wire prefix of one `ControlCall` payload: `u64` big-endian
/// per-call timeout in milliseconds, then the raw command bytes.
pub const CONTROL_CALL_PREFIX_BYTES: usize = 8;
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

/// How the local Router reaches the sidecar.
pub enum LocalChannel {
    /// Owner-only Unix-domain socket; peers must share the effective uid.
    #[cfg(unix)]
    UnixSocket(PathBuf),
    /// `127.0.0.1:0` TCP listener; the client end of every connection must be
    /// owned by `allowed_client_pid` according to `owners`.
    Loopback {
        allowed_client_pid: u32,
        owners: Arc<dyn ConnectionOwnerTable>,
    },
}

pub struct SidecarConfig {
    pub channel: LocalChannel,
    pub queue_capacity: NonZeroUsize,
    pub local_only: bool,
    pub force_relay: bool,
    pub endpoint_secret: Option<Zeroizing<[u8; 32]>>,
    /// Also serve phone stage objects on `mycelium.artifact_chunk.v1`.
    pub artifact: Option<ArtifactSourceConfig>,
}

/// The A2-over-iroh artifact source the sidecar serves beside the Router
/// ALPN (`mycelium_mobile_core::artifact_net`): content-addressed objects,
/// the seed key grants must verify under, the durable admission journal and
/// the source's receipt-signing key. Its grant identity is this sidecar's
/// iroh endpoint id.
pub struct ArtifactSourceConfig {
    pub objects: PathBuf,
    pub seed_key: serde_json::Value,
    pub state_dir: PathBuf,
    pub signing_secret: Zeroizing<[u8; 32]>,
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
    InvalidNetworkMode,
    ReadyOutput,
    Runtime,
    ArtifactSource,
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
#[cfg(unix)]
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
#[cfg(unix)]
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

/// Reads exactly 32 endpoint-secret bytes from an owner-only regular file.
///
/// The file is opened without following reparse points and must be owned by
/// the current user with a protected DACL whose only entries grant access to
/// that user and to SYSTEM: the Windows analogue of an owned mode-0600 file.
#[cfg(windows)]
pub fn read_endpoint_secret(path: &Path) -> Result<Zeroizing<[u8; 32]>, SidecarError> {
    use std::os::windows::fs::{MetadataExt as _, OpenOptionsExt as _};
    use std::os::windows::io::AsRawHandle as _;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ,
    };

    let mut file = fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .map_err(|_| SidecarError::EndpointSecretSecurity)?;
    let metadata = file
        .metadata()
        .map_err(|_| SidecarError::EndpointSecretSecurity)?;
    if !metadata.file_type().is_file()
        || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || metadata.len() != 32
        || !windows_security::handle_is_owner_only(file.as_raw_handle())
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

#[cfg(windows)]
mod windows_security {
    use std::ffi::c_void;
    use std::ptr::null_mut;

    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, LocalFree};
    use windows_sys::Win32::Security::Authorization::{GetSecurityInfo, SE_FILE_OBJECT};
    use windows_sys::Win32::Security::{
        ACCESS_ALLOWED_ACE, ACE_HEADER, ACL, ACL_SIZE_INFORMATION, AclSizeInformation,
        DACL_SECURITY_INFORMATION, EqualSid, GetAce, GetAclInformation,
        GetSecurityDescriptorControl, GetTokenInformation, IsWellKnownSid,
        OWNER_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID, SE_DACL_PROTECTED, TOKEN_QUERY,
        TOKEN_USER, TokenUser, WinLocalSystemSid,
    };
    use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

    /// `ACCESS_ALLOWED_ACE_TYPE` from winnt.h.
    const ACCESS_ALLOWED_ACE_TYPE: u8 = 0;

    struct LocalAllocation(*mut c_void);

    impl Drop for LocalAllocation {
        fn drop(&mut self) {
            if !self.0.is_null() {
                // SAFETY: the pointer was allocated by the API for LocalFree.
                unsafe { LocalFree(self.0) };
            }
        }
    }

    /// The current process token's `TOKEN_USER`, kept in aligned storage.
    struct TokenUserBuffer(Vec<u64>);

    impl TokenUserBuffer {
        fn current() -> Option<Self> {
            let mut token: HANDLE = null_mut();
            // SAFETY: GetCurrentProcess returns a pseudo handle; `token` is a
            // valid out pointer and is closed below on every path.
            if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
                return None;
            }
            let mut length = 0_u32;
            // SAFETY: a size query with a null buffer of length zero.
            unsafe { GetTokenInformation(token, TokenUser, null_mut(), 0, &mut length) };
            let mut storage = vec![0_u64; (length as usize).div_ceil(8).max(1)];
            let capacity = u32::try_from(storage.len() * 8).unwrap_or(0);
            // SAFETY: `storage` is writable for `capacity` bytes.
            let filled = unsafe {
                GetTokenInformation(
                    token,
                    TokenUser,
                    storage.as_mut_ptr().cast(),
                    capacity,
                    &mut length,
                )
            };
            // SAFETY: `token` was opened above and is not used afterwards.
            unsafe { CloseHandle(token) };
            (filled != 0 && length as usize >= size_of::<TOKEN_USER>()).then_some(Self(storage))
        }

        fn sid(&self) -> PSID {
            // SAFETY: the buffer was filled with a TOKEN_USER by the API and
            // is aligned for it; the SID it points at lives inside the buffer.
            unsafe { (*self.0.as_ptr().cast::<TOKEN_USER>()).User.Sid }
        }
    }

    /// `true` when the object behind `handle` is owned by the current user
    /// and its protected DACL allows only that user and SYSTEM.
    pub(super) fn handle_is_owner_only(handle: std::os::windows::io::RawHandle) -> bool {
        let Some(user) = TokenUserBuffer::current() else {
            return false;
        };
        let mut owner: PSID = null_mut();
        let mut dacl: *mut ACL = null_mut();
        let mut descriptor: PSECURITY_DESCRIPTOR = null_mut();
        // SAFETY: all out pointers are valid; the descriptor that owns the
        // returned owner and DACL pointers is released by `_descriptor`.
        let status = unsafe {
            GetSecurityInfo(
                handle,
                SE_FILE_OBJECT,
                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                &mut owner,
                null_mut(),
                &mut dacl,
                null_mut(),
                &mut descriptor,
            )
        };
        let _descriptor = LocalAllocation(descriptor);
        if status != 0 || owner.is_null() || dacl.is_null() || descriptor.is_null() {
            // A null DACL grants everyone full access.
            return false;
        }
        // SAFETY: `owner` and the token SID are valid SIDs for these calls.
        if unsafe { EqualSid(owner, user.sid()) } == 0 {
            return false;
        }
        let mut control = 0_u16;
        let mut revision = 0_u32;
        // SAFETY: `descriptor` is a valid self-relative descriptor.
        if unsafe { GetSecurityDescriptorControl(descriptor, &mut control, &mut revision) } == 0
            || control & SE_DACL_PROTECTED == 0
        {
            return false;
        }
        let mut information = ACL_SIZE_INFORMATION::default();
        // SAFETY: `information` is writable for the size passed.
        if unsafe {
            GetAclInformation(
                dacl,
                (&raw mut information).cast(),
                size_of::<ACL_SIZE_INFORMATION>() as u32,
                AclSizeInformation,
            )
        } == 0
        {
            return false;
        }
        let mut grants_user = false;
        for index in 0..information.AceCount {
            let mut ace: *mut c_void = null_mut();
            // SAFETY: `index` is below the ACE count reported for `dacl`.
            if unsafe { GetAce(dacl, index, &mut ace) } == 0 || ace.is_null() {
                return false;
            }
            // SAFETY: every ACE starts with an ACE_HEADER.
            let header = unsafe { *ace.cast::<ACE_HEADER>() };
            if header.AceType != ACCESS_ALLOWED_ACE_TYPE {
                return false;
            }
            let allowed = ace.cast::<ACCESS_ALLOWED_ACE>();
            // SAFETY: an access-allowed ACE stores its SID at SidStart.
            let sid: PSID = unsafe { (&raw mut (*allowed).SidStart).cast() };
            // SAFETY: `sid` points at a SID inside the live DACL.
            if unsafe { EqualSid(sid, user.sid()) } != 0 {
                grants_user = true;
            } else if unsafe { IsWellKnownSid(sid, WinLocalSystemSid) } == 0 {
                return false;
            }
        }
        grants_user
    }
}

#[cfg(any(unix, test))]
#[derive(Serialize)]
struct ReadyEvent<'a> {
    event: &'static str,
    endpoint_id: String,
    endpoint_addr: &'a EndpointAddr,
    alpn: &'static str,
}

#[cfg(any(unix, test))]
/// The POSIX ready record: exactly `{event, endpoint_id, endpoint_addr, alpn}`.
pub(crate) fn posix_ready_record(
    endpoint_id: &str,
    endpoint_addr: &EndpointAddr,
    alpn: &'static str,
) -> Result<String, SidecarError> {
    let ready = ReadyEvent {
        event: "ready",
        endpoint_id: endpoint_id.to_owned(),
        endpoint_addr,
        alpn,
    };
    serde_json::to_string(&ready).map_err(|_| SidecarError::ReadyOutput)
}

/// The listener selected by [`LocalChannel`], bound before the endpoint.
enum BoundChannel {
    #[cfg(unix)]
    UnixSocket {
        listener: UnixListener,
        /// Removes the socket file when the channel is dropped.
        _cleanup: SocketCleanup,
    },
    Loopback {
        listener: LoopbackListener,
        allowed_client_pid: u32,
        owners: Arc<dyn ConnectionOwnerTable>,
    },
}

impl BoundChannel {
    fn bind(channel: LocalChannel) -> Result<Self, SidecarError> {
        match channel {
            #[cfg(unix)]
            LocalChannel::UnixSocket(path) => {
                let listener = bind_secure_uds(&path)?;
                Ok(Self::UnixSocket {
                    listener,
                    _cleanup: SocketCleanup(path),
                })
            }
            LocalChannel::Loopback {
                allowed_client_pid,
                owners,
            } => Ok(Self::Loopback {
                listener: LoopbackListener::bind()?,
                allowed_client_pid,
                owners,
            }),
        }
    }

    fn ready_record(
        &self,
        endpoint_id: &str,
        endpoint_addr: &EndpointAddr,
        alpn: &'static str,
    ) -> Result<String, SidecarError> {
        match self {
            #[cfg(unix)]
            Self::UnixSocket { .. } => posix_ready_record(endpoint_id, endpoint_addr, alpn),
            Self::Loopback { listener, .. } => {
                loopback_ready_record(endpoint_id, endpoint_addr, alpn, listener.local_port())
            }
        }
    }
}

/// Runs until SIGINT, SIGTERM/process termination, or a fatal listener error.
pub async fn run_sidecar(
    config: SidecarConfig,
    secret: Zeroizing<[u8; 32]>,
) -> Result<(), SidecarError> {
    if config.local_only && config.force_relay {
        return Err(SidecarError::InvalidNetworkMode);
    }
    let channel = BoundChannel::bind(config.channel)?;
    let endpoint = bind_endpoint(
        config.local_only,
        config.force_relay,
        config.endpoint_secret.as_deref(),
        config.artifact.is_some(),
    )
    .await?;
    let artifact_source = match config.artifact {
        Some(artifact) => Some(
            ArtifactSource::with_endpoint(
                endpoint.clone(),
                &artifact.signing_secret,
                artifact.seed_key,
                artifact.objects,
                &artifact.state_dir,
            )
            .map_err(|_| SidecarError::ArtifactSource)?,
        ),
        None => None,
    };
    // The advertised address is a live value, not the bind-time snapshot the
    // WP2 probe caught with `relay_addrs=0` (wp2-6 defect 1): it is read from a
    // watcher, waited for until it is usable, and re-advertised on change.
    let (address_sender, mut addresses) = watch::channel(endpoint.addr());
    tokio::spawn(publish_address_updates(endpoint.clone(), address_sender));
    let (endpoint_addr, address_readiness) = await_advertised_address(
        &mut addresses,
        !config.local_only,
        endpoint.online(),
        READY_ADDRESS_WINDOW,
    )
    .await;
    log_event(address_readiness.event());
    // What the ready event is about to advertise; everything that differs from
    // it afterwards is re-advertised as it happens.
    let advertised_at_ready = endpoint_addr.clone();
    let endpoint_id = endpoint.id().to_string();

    let state = RuntimeState::with_control_carrier(
        config.queue_capacity.get(),
        config.local_only,
        &endpoint,
    );
    let outbound_task = tokio::spawn(outbound_worker(endpoint.clone(), state.clone()));
    if let Some(source) = &artifact_source {
        // What the owner names when issuing a v2 assignment
        // (`--artifact-source ENDPOINT_ID:KEY_DIGEST`); the ready record itself
        // keeps its fixed shape.
        eprintln!(
            "{}",
            serde_json::json!({"event": "artifact_source_ready", "endpoint_id": source.endpoint_id(), "verification_key_digest": source.key_digest()})
        );
    }
    let incoming_task = tokio::spawn(incoming_worker(
        endpoint.clone(),
        state.clone(),
        artifact_source,
    ));

    let ready_json = channel.ready_record(
        &endpoint_id,
        &endpoint_addr,
        std::str::from_utf8(IROH_ALPN).expect("ALPN is ASCII"),
    )?;
    {
        use std::io::Write as _;
        let mut stdout = io::stdout().lock();
        writeln!(stdout, "{ready_json}").map_err(|_| SidecarError::ReadyOutput)?;
        stdout.flush().map_err(|_| SidecarError::ReadyOutput)?;
    }
    tokio::spawn(republish_address_changes(
        endpoint_id.clone(),
        addresses,
        advertised_at_ready,
        StdoutSink,
    ));

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
            result = channel.accept() => result,
            signal = tokio::signal::ctrl_c() => {
                let _ = signal;
                break Ok(());
            }
        };
        let stream = match accepted {
            Ok(stream) => stream,
            Err(_) => break Err(SidecarError::Runtime),
        };
        match stream {
            #[cfg(unix)]
            AcceptedStream::UnixSocket(stream) => {
                if !same_effective_uid(&stream) {
                    drop(permit);
                    log_event("local_peer_rejected");
                    continue;
                }

                let session_id = next_session.fetch_add(1, Ordering::Relaxed);
                tokio::spawn(serve_local_session(
                    stream,
                    permit,
                    session_id,
                    secret.clone(),
                    endpoint_id.clone(),
                    state.clone(),
                ));
            }
            AcceptedStream::Loopback {
                stream,
                client,
                server,
                allowed_client_pid,
                owners,
            } => {
                let session_id = next_session.fetch_add(1, Ordering::Relaxed);
                let session_secret = secret.clone();
                let public_id = endpoint_id.clone();
                let session_state = state.clone();
                tokio::spawn(async move {
                    // The owner lookup can block (it walks the host TCP table),
                    // so it runs off the reactor and before any handshake byte.
                    let authorized = tokio::task::spawn_blocking(move || {
                        authorize_loopback_client(
                            owners.as_ref(),
                            client,
                            server,
                            allowed_client_pid,
                        )
                    })
                    .await;
                    if !matches!(authorized, Ok(Ok(()))) {
                        drop(permit);
                        log_event("local_peer_rejected");
                        return;
                    }
                    serve_local_session(
                        stream,
                        permit,
                        session_id,
                        session_secret,
                        public_id,
                        session_state,
                    )
                    .await;
                });
            }
        }
    };

    endpoint.close().await;
    outbound_task.abort();
    incoming_task.abort();
    listener_result
}

/// One accepted local connection, still to be authorized for its channel.
enum AcceptedStream {
    #[cfg(unix)]
    UnixSocket(UnixStream),
    Loopback {
        stream: tokio::net::TcpStream,
        client: SocketAddr,
        server: SocketAddr,
        allowed_client_pid: u32,
        owners: Arc<dyn ConnectionOwnerTable>,
    },
}

impl BoundChannel {
    async fn accept(&self) -> io::Result<AcceptedStream> {
        match self {
            #[cfg(unix)]
            Self::UnixSocket { listener, .. } => {
                let (stream, _) = listener.accept().await?;
                Ok(AcceptedStream::UnixSocket(stream))
            }
            Self::Loopback {
                listener,
                allowed_client_pid,
                owners,
            } => {
                let (stream, client) = listener.accept().await?;
                Ok(AcceptedStream::Loopback {
                    stream,
                    client,
                    server: SocketAddr::V4(listener.local_addr()),
                    allowed_client_pid: *allowed_client_pid,
                    owners: owners.clone(),
                })
            }
        }
    }
}

/// Runs one authenticated session and hands its undelivered messages back.
async fn serve_local_session<S>(
    stream: S,
    permit: OwnedSemaphorePermit,
    session_id: u64,
    secret: Arc<Zeroizing<[u8; 32]>>,
    endpoint_id: String,
    state: Arc<RuntimeState>,
) where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let _permit = permit;
    let _ = handle_local_session(
        stream,
        session_id,
        secret.as_ref().as_ref(),
        &endpoint_id,
        state.clone(),
    )
    .await;
    state.redeliver_session(session_id).await;
}

/// Publish the endpoint's own address changes into a watch channel.
///
/// `Endpoint::addr()` is a bind-time snapshot; `Endpoint::watch_addr()` is the
/// live value.  Bridging it into a channel is what lets the readiness gate and
/// the republisher be driven deterministically in tests (packet wp2-6).
async fn publish_address_updates(endpoint: Endpoint, addresses: watch::Sender<EndpointAddr>) {
    let mut watcher = endpoint.watch_addr();
    loop {
        let current = watcher.get();
        if *addresses.borrow() != current {
            if addresses.send(current).is_err() {
                return;
            }
        }
        if watcher.updated().await.is_err() {
            return;
        }
    }
}

async fn bind_endpoint(
    local_only: bool,
    force_relay: bool,
    endpoint_secret: Option<&[u8; 32]>,
    serve_artifacts: bool,
) -> Result<Endpoint, SidecarError> {
    let mut builder = if local_only {
        Endpoint::builder(presets::Minimal)
            .relay_mode(RelayMode::Disabled)
            .clear_ip_transports()
            .bind_addr(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))
            .map_err(|_| SidecarError::EndpointBind)?
    } else if force_relay {
        // Keep the production relay transport while removing every direct IP
        // transport. The selected path still comes from Iroh's live
        // connection observation; this setting never authors the outcome.
        Endpoint::builder(presets::N0).clear_ip_transports()
    } else {
        Endpoint::builder(presets::N0)
    };
    if let Some(secret) = endpoint_secret {
        builder = builder.secret_key(SecretKey::from_bytes(secret));
    }
    let mut alpns = vec![IROH_ALPN.to_vec()];
    if serve_artifacts {
        alpns.push(ARTIFACT_CHUNK_ALPN.to_vec());
    }
    builder
        .alpns(alpns)
        .bind()
        .await
        .map_err(|_| SidecarError::EndpointBind)
}

#[cfg(unix)]
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

#[cfg(unix)]
fn same_effective_uid(stream: &UnixStream) -> bool {
    let Ok(credentials) = stream.peer_cred() else {
        return false;
    };
    // SAFETY: geteuid has no preconditions and does not dereference pointers.
    credentials.uid() == unsafe { libc::geteuid() }
}

#[cfg(unix)]
struct SocketCleanup(PathBuf);

#[cfg(unix)]
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

async fn handle_local_session<S>(
    mut stream: S,
    session_id: u64,
    secret: &[u8],
    endpoint_id: &str,
    state: Arc<RuntimeState>,
) -> Result<(), SessionError>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
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

async fn authenticated_session_loop<S>(
    stream: &mut S,
    session_id: u64,
    keys: &SessionKeys,
    state: Arc<RuntimeState>,
) -> Result<(), SessionError>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let mut receive_sequence = SequenceGuard::new();
    let mut send_sequence = Some(0_u64);
    loop {
        let encoded = read_length_prefixed(stream, LOCAL_MAX_RECORD_BYTES).await?;
        let record = decode_record(&encoded, &keys.client_to_sidecar, &mut receive_sequence)
            .map_err(|_| SessionError::Authentication)?;
        let confirmed = matches!(
            record.kind,
            RecordKind::SendConfirmed | RecordKind::ControlCall
        );
        let response = process_local_record(record, session_id, &state);
        tokio::pin!(response);
        let response = if confirmed {
            // A confirmed send waits for its terminal outcome; meanwhile the
            // client may only hang up. `read` is cancel-safe, so losing the
            // race to the response consumes no byte.
            let mut unexpected = [0_u8; 1];
            tokio::select! {
                response = &mut response => response,
                read = stream.read(&mut unexpected) => match read {
                    Ok(0) => return Ok(()),
                    Ok(_) => return Err(SessionError::Protocol),
                    Err(_) => return Err(SessionError::Disconnected),
                },
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
        RecordKind::GetAdmissionCounters => {
            let Ok(request) = serde_json::from_slice::<AdmissionCounterRequest>(&record.payload)
            else {
                return error_response(record.message_id, "invalid_candidate_endpoint");
            };
            let Ok(candidate_endpoint_id) = request.candidate_endpoint_id.parse::<EndpointId>()
            else {
                return error_response(record.message_id, "invalid_candidate_endpoint");
            };
            let payload =
                serde_json::to_vec(&state.admission_counters(candidate_endpoint_id).await)
                    .unwrap_or_default();
            ResponseRecord {
                kind: RecordKind::AdmissionCounters,
                message_id: record.message_id,
                payload,
            }
        }
        RecordKind::ControlCall => {
            let call = match parse_control_call(&record.payload) {
                Ok(call) => call,
                Err(code) => return error_response(record.message_id, code),
            };
            let Some(endpoint) = state.control_carrier.clone() else {
                return error_response(record.message_id, "carrier_unavailable");
            };
            let changed = state.peer_generation.subscribe();
            let peer = {
                let peers = state.peers.read().await;
                match peers.get(call.destination) {
                    Some(binding) => binding.clone(),
                    None => return error_response(record.message_id, "unknown_peer"),
                }
            };
            match state.control_call(endpoint, peer, call, changed).await {
                Ok(response) => ResponseRecord {
                    kind: RecordKind::ControlCallResult,
                    message_id: record.message_id,
                    payload: response,
                },
                Err(code) => error_response(record.message_id, code),
            }
        }
        RecordKind::Delivery
        | RecordKind::DeliveryFrom
        | RecordKind::TransportObservations
        | RecordKind::AdmissionCounters
        | RecordKind::ControlCallResult
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

async fn write_authenticated_record<S: AsyncWrite + Unpin>(
    stream: &mut S,
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

async fn read_length_prefixed<S: AsyncRead + Unpin>(
    stream: &mut S,
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

async fn write_length_prefixed<S: AsyncWrite + Unpin>(
    stream: &mut S,
    bytes: &[u8],
) -> Result<(), SessionError> {
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
    /// The endpoint the M4-host control carrier dials from.  `None` in
    /// unit-test states and any runtime without a bound endpoint: a
    /// `ControlCall` then answers `carrier_unavailable` instead of panicking.
    control_carrier: Option<Endpoint>,
    /// Bounded control-call concurrency, separate from Router work: a control
    /// call never occupies an outbound or inbound Router slot.
    control_slots: Arc<Semaphore>,
    peer_generation: watch::Sender<u64>,
    peer_changed: Notify,
    local_only: bool,
    inbound: Mutex<InboundState>,
    inbound_slots: Arc<Semaphore>,
    inbound_ready: Notify,
    seen_limit: usize,
    outbound_deadline: Duration,
    outbound: Mutex<VecDeque<OutboundItem>>,
    outbound_ready: Notify,
    outbound_slots: Arc<Semaphore>,
    outbound_tokens: Mutex<HashMap<MessageId, Arc<OutboundControl>>>,
    outbound_connections: Mutex<HashMap<(EndpointId, u64), iroh::endpoint::Connection>>,
    transport_observations: Mutex<HashMap<(EndpointId, u64), TransportObservationState>>,
    inbound_identity_rejections: AtomicU64,
    inbound_frames_admitted: AtomicU64,
    inbound_identity_rejections_by_endpoint: Mutex<HashMap<EndpointId, u64>>,
}

#[derive(Deserialize)]
struct AdmissionCounterRequest {
    candidate_endpoint_id: String,
}

struct ControlConnection(iroh::endpoint::Connection);

impl Drop for ControlConnection {
    fn drop(&mut self) {
        self.0.close(CANCEL_CODE, b"control call ended");
    }
}

/// One parsed `ControlCall` record payload.
#[derive(Debug, PartialEq)]
struct ControlCall {
    destination: EndpointId,
    /// Per-call absolute timeout; the handler rejects anything above
    /// [`CONTROL_CALL_TIMEOUT_CEILING`] before dialing.
    timeout: Duration,
    command: Vec<u8>,
}

/// Decode a `ControlCall` payload: 32 raw destination `EndpointId` bytes,
/// a `u64` big-endian per-call timeout in milliseconds, then the command
/// bytes.  The whole payload (prefix included) is bounded by the record
/// layer's `LOCAL_MAX_PAYLOAD_BYTES`; the command alone is bounded by
/// [`CONTROL_CALL_MAX_BYTES`].
fn parse_control_call(payload: &[u8]) -> Result<ControlCall, &'static str> {
    let prefix = ENDPOINT_ID_BYTES + CONTROL_CALL_PREFIX_BYTES;
    if payload.len() < prefix {
        return Err("invalid_control_call");
    }
    let raw: [u8; ENDPOINT_ID_BYTES] = payload[..ENDPOINT_ID_BYTES]
        .try_into()
        .expect("destination prefix has fixed length");
    let destination = EndpointId::from_bytes(&raw).map_err(|_| "invalid_control_destination")?;
    let millis = u64::from_be_bytes(
        payload[ENDPOINT_ID_BYTES..prefix]
            .try_into()
            .expect("timeout prefix has fixed length"),
    );
    if Duration::from_millis(millis) > CONTROL_CALL_TIMEOUT_CEILING {
        return Err("control_timeout_exceeded");
    }
    let command = &payload[prefix..];
    if millis == 0 || command.is_empty() || command.len() > CONTROL_CALL_MAX_BYTES {
        return Err("invalid_control_call");
    }
    Ok(ControlCall {
        destination,
        timeout: Duration::from_millis(millis),
        command: command.to_vec(),
    })
}

#[derive(Serialize)]
struct AdmissionCounterEnvelope {
    protocol: &'static str,
    inbound_identity_rejections: u64,
    inbound_frames_admitted: u64,
    candidate_identity_rejections: u64,
    measured_at_unix_ms: u64,
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
    /// Enqueue time plus `OUTBOUND_DELIVERY_DEADLINE`: never sent after it.
    deadline: Instant,
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
    #[cfg(test)]
    fn new(capacity: usize, local_only: bool) -> Arc<Self> {
        Self::with_outbound_deadline(capacity, local_only, OUTBOUND_DELIVERY_DEADLINE)
    }

    fn with_control_carrier(capacity: usize, local_only: bool, endpoint: &Endpoint) -> Arc<Self> {
        let state = Self::with_outbound_deadline(capacity, local_only, OUTBOUND_DELIVERY_DEADLINE);
        // Safe: a freshly built runtime state has exactly one owner.
        let mut state = Arc::try_unwrap(state)
            .unwrap_or_else(|_| unreachable!("a freshly built runtime state has one owner"));
        state.control_carrier = Some(endpoint.clone());
        Arc::new(state)
    }

    fn with_outbound_deadline(
        capacity: usize,
        local_only: bool,
        outbound_deadline: Duration,
    ) -> Arc<Self> {
        let seen_limit = capacity.saturating_mul(8).max(64);
        let (peer_generation, _) = watch::channel(0);
        Arc::new(Self {
            peers: RwLock::new(PeerTable::default()),
            control_carrier: None,
            control_slots: Arc::new(Semaphore::new(CONTROL_CALL_CONCURRENCY)),
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
            outbound_deadline,
            outbound: Mutex::new(VecDeque::with_capacity(capacity)),
            outbound_ready: Notify::new(),
            outbound_slots: Arc::new(Semaphore::new(capacity)),
            outbound_tokens: Mutex::new(HashMap::with_capacity(capacity)),
            outbound_connections: Mutex::new(HashMap::new()),
            transport_observations: Mutex::new(HashMap::new()),
            inbound_identity_rejections: AtomicU64::new(0),
            inbound_frames_admitted: AtomicU64::new(0),
            inbound_identity_rejections_by_endpoint: Mutex::new(HashMap::new()),
        })
    }

    /// One attempt, using the existing endpoint; every wait shares one deadline.
    async fn control_call(
        &self,
        endpoint: Endpoint,
        peer: PeerBinding,
        call: ControlCall,
        mut changed: watch::Receiver<u64>,
    ) -> Result<Vec<u8>, &'static str> {
        if call.timeout > CONTROL_CALL_TIMEOUT_CEILING {
            return Err("control_timeout_exceeded");
        }
        let deadline = Instant::now()
            .checked_add(call.timeout)
            .ok_or("control_timeout_invalid")?;
        let exchange = async {
            let _permit = timeout_at(deadline, self.control_slots.clone().acquire_owned())
                .await
                .map_err(|_| "control_carrier_busy")?
                .map_err(|_| "carrier_unavailable")?;
            if self.peers.read().await.get(call.destination) != Some(&peer) {
                return Err("peer_rotated");
            }
            let connection = timeout_at(
                deadline,
                endpoint.connect(peer.address.clone(), NODE_CONTROL_ALPN),
            )
            .await
            .map_err(|_| "control_connect_timeout")?
            .map_err(|_| "control_connect_failed")?;
            // Own this call's connection until completion, cancellation, caller
            // hangup or revocation. Drop closes it even when select cancels IO.
            let connection = ControlConnection(connection);
            if connection.0.remote_id() != call.destination {
                return Err("control_peer_mismatch");
            }
            if self.peers.read().await.get(call.destination) != Some(&peer) {
                return Err("peer_rotated");
            }
            let (mut send, mut recv) = timeout_at(deadline, connection.0.open_bi())
                .await
                .map_err(|_| "control_open_timeout")?
                .map_err(|_| "control_open_failed")?;
            timeout_at(deadline, send.write_all(&call.command))
                .await
                .map_err(|_| "control_write_timeout")?
                .map_err(|_| "control_write_failed")?;
            send.finish().map_err(|_| "control_write_failed")?;
            let response =
                match timeout_at(deadline, recv.read_to_end(CONTROL_CALL_MAX_BYTES)).await {
                    Ok(Ok(response)) if response.is_empty() => Err("empty_control_call_result"),
                    Ok(Ok(response)) => Ok(response),
                    Ok(Err(ReadToEndError::TooLong)) => Err("control_response_too_large"),
                    Ok(Err(_)) => Err("control_read_failed"),
                    Err(_) => Err("control_read_timeout"),
                }?;
            // Do not admit a result under replaced authority, even if response
            // and configuration update become ready in the same scheduler turn.
            if self.peers.read().await.get(call.destination) != Some(&peer) {
                return Err("peer_rotated");
            }
            Ok(response)
        };
        tokio::select! {
            biased;
            _ = changed.changed() => Err("peer_rotated"),
            result = exchange => {
                if changed.has_changed().unwrap_or(true) { Err("peer_rotated") } else { result }
            }
        }
    }

    async fn admission_counters(
        &self,
        candidate_endpoint_id: EndpointId,
    ) -> AdmissionCounterEnvelope {
        let candidate_identity_rejections = self
            .inbound_identity_rejections_by_endpoint
            .lock()
            .await
            .get(&candidate_endpoint_id)
            .copied()
            .unwrap_or(0);
        AdmissionCounterEnvelope {
            protocol: "mycelium.iroh_sidecar.inbound_admission.v1",
            inbound_identity_rejections: self.inbound_identity_rejections.load(Ordering::Acquire),
            inbound_frames_admitted: self.inbound_frames_admitted.load(Ordering::Acquire),
            candidate_identity_rejections,
            measured_at_unix_ms: unix_time_ms(),
        }
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
        let connection = match timeout(
            PEER_CONNECT_TIMEOUT,
            endpoint.connect(peer.address.clone(), IROH_ALPN),
        )
        .await
        {
            Ok(Ok(connection)) => connection,
            Ok(Err(_)) => return Err(()),
            Err(_elapsed) => {
                // iroh 1.0.2 has no connect timeout of its own, so the cause of
                // a dial that never completes has to be named here; otherwise
                // the only visible symptom is the caller's own deadline
                // (wp2-6 defect 2).  The outbound item stays pending and keeps
                // retrying with bounded backoff, exactly as the sidecar
                // contract requires.
                log_transport_event(
                    "peer_connect_deadline_exceeded",
                    peer.address.id,
                    peer.generation,
                );
                return Err(());
            }
        };
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
            deadline: Instant::now() + self.outbound_deadline,
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
    if state.path_class != path_class && (!state.path_class.is_empty() || path_class != "unknown") {
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
        let deadline = item.control.deadline;
        let terminal = loop {
            if item.control.cancellation.is_cancelled() {
                break item
                    .control
                    .terminal()
                    .unwrap_or(OutboundTerminal::Cancelled);
            }
            if Instant::now() >= deadline {
                // The sidecar's own bound: a lost Python cancellation never
                // lets a frame go out past OUTBOUND_DELIVERY_DEADLINE.
                break OutboundTerminal::Cancelled;
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
                            () = sleep_until(deadline) => {},
                        }
                        continue;
                    }
                },
            };
            if !state.peer_matches(peer.address.id, peer.generation).await {
                break OutboundTerminal::PeerRotated;
            }
            let Ok(attempt) = timeout_at(
                deadline,
                send_outbound_once(&endpoint, &state, &peer, &item),
            )
            .await
            else {
                break OutboundTerminal::Cancelled;
            };
            match attempt {
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
                        () = sleep_until(deadline) => {},
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

async fn incoming_worker(
    endpoint: Endpoint,
    state: Arc<RuntimeState>,
    artifacts: Option<ArtifactSource>,
) {
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
        let artifacts = artifacts.clone();
        tokio::spawn(async move {
            let permit = permit;
            let Ok(connection) = incoming.await else {
                return;
            };
            // Artifact chunks are authorized per request by the seed grant and
            // the recipient's endpoint, never by the Router peer allow-list;
            // Router frames are never accepted on the artifact ALPN. An
            // artifact connection gives its Router connection slot back at
            // once and is bounded by the artifact source's own limits, so
            // artifact load can never hold the slots Router peers need.
            if connection.alpn() == ARTIFACT_CHUNK_ALPN {
                drop(permit);
                match artifacts {
                    Some(source) => source.serve_connection(connection).await,
                    None => connection.close(REJECT_CODE, b"alpn"),
                }
                return;
            }
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
                        connection_state
                            .inbound_identity_rejections
                            .fetch_add(1, Ordering::AcqRel);
                        let mut by_endpoint = connection_state
                            .inbound_identity_rejections_by_endpoint
                            .lock()
                            .await;
                        let count = by_endpoint.entry(connection.remote_id()).or_insert(0);
                        *count = count.saturating_add(1);
                        drop(by_endpoint);
                        connection.close(REJECT_CODE, b"identity");
                        return;
                    }
                }
            };
            let _permit = permit;
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
                AdmissionOutcome::Admitted(control) => {
                    state.inbound_frames_admitted.fetch_add(1, Ordering::AcqRel);
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
                AdmissionOutcome::PendingDuplicate(control) => {
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
    #[cfg(unix)]
    use std::fs::OpenOptions;
    #[cfg(unix)]
    use std::io::Write;
    #[cfg(unix)]
    use std::os::unix::fs::{OpenOptionsExt, PermissionsExt, symlink};
    #[cfg(unix)]
    use std::time::{SystemTime, UNIX_EPOCH};

    #[cfg(unix)]
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
    fn selected_path_change_counter_preserves_unknown_and_transition_semantics() {
        let mut state = TransportObservationState::default();

        update_selected_path(&mut state, "unknown".to_owned(), None, None);
        assert_eq!(state.selected_path_changes, 0);

        update_selected_path(&mut state, "direct".to_owned(), None, None);
        assert_eq!(state.selected_path_changes, 1);

        update_selected_path(&mut state, "direct".to_owned(), None, None);
        assert_eq!(state.selected_path_changes, 1);

        update_selected_path(&mut state, "unknown".to_owned(), None, None);
        assert_eq!(state.selected_path_changes, 2);

        update_selected_path(&mut state, "relay".to_owned(), None, None);
        assert_eq!(state.selected_path_changes, 3);
    }

    #[cfg(unix)]
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
        let first = bind_endpoint(true, false, Some(&*secret), false)
            .await
            .unwrap();
        let first_id = first.id();
        first.close().await;
        let second = bind_endpoint(true, false, Some(&*secret), false)
            .await
            .unwrap();
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

    #[test]
    fn outbound_deadline_is_bounded_below_the_relay_retention_horizon() {
        // relay.py _TOMBSTONE_RETENTION_SECONDS; the Python side pins the same
        // relation from the source of this constant.
        // physical_inference_node.py _MAX_IROH_DELIVERY_TIMEOUT_SECONDS is
        // 240 s: never end a send Python is still waiting for.
        assert!(OUTBOUND_DELIVERY_DEADLINE > Duration::from_secs(240));
        assert!(OUTBOUND_DELIVERY_DEADLINE * 2 <= Duration::from_secs(600));
    }

    #[tokio::test]
    async fn a_send_whose_cancellation_is_lost_still_ends_at_the_deadline() {
        // An unreachable peer (TEST-NET-3) keeps the worker retrying; nobody
        // ever calls cancel_outbound. The sidecar ends the send by itself.
        let state = RuntimeState::with_outbound_deadline(1, false, Duration::from_millis(400));
        state
            .configure_peers(vec![routable_peer(1, 1)])
            .await
            .expect("peer set is accepted");
        let endpoint = bind_endpoint(true, false, None, false).await.unwrap();
        let worker = tokio::spawn(outbound_worker(endpoint.clone(), state.clone()));
        let EnqueueOutcome::Queued(control) = state
            .enqueue_outbound([5; 16], valid_frame(), None, None, None)
            .await
        else {
            panic!("send is queued");
        };
        let started = Instant::now();
        let terminal = timeout(Duration::from_secs(10), control.wait_for_terminal())
            .await
            .expect("the send ends without any cancellation");
        assert_eq!(terminal, OutboundTerminal::Cancelled);
        assert!(
            started.elapsed() < Duration::from_secs(5),
            "ended at the deadline, not a dial timeout"
        );
        // Its queue slot is released and its id is forgotten.
        timeout(Duration::from_secs(2), async {
            while state.outbound_slots.available_permits() != 1 {
                sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("slot released");
        assert!(!state.cancel_outbound([5; 16]).await);
        worker.abort();
        endpoint.close().await;
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

    const SESSION_SECRET: [u8; 32] = [0x5a; 32];
    const SESSION_ENDPOINT: &str = "duplex-endpoint";

    /// Runs the client half of the bootstrap handshake over any stream.
    async fn client_handshake<S>(stream: &mut S, secret: &[u8; 32]) -> Option<SessionKeys>
    where
        S: AsyncRead + AsyncWrite + Unpin,
    {
        let client_nonce = [0x21_u8; 32];
        let hello = ClientHello::new(secret, &client_nonce).unwrap();
        write_length_prefixed(stream, &serde_json::to_vec(&hello).unwrap())
            .await
            .ok()?;
        let reply = read_length_prefixed(stream, MAX_HELLO_BYTES).await.ok()?;
        let server: ServerHello = serde_json::from_slice(&reply).unwrap();
        assert_eq!(server.endpoint_id, SESSION_ENDPOINT);
        let server_nonce = server.verify(secret, &client_nonce).unwrap();
        Some(derive_session_keys(secret, &client_nonce, &server_nonce).unwrap())
    }

    fn spawn_duplex_session(
        state: Arc<RuntimeState>,
    ) -> (
        tokio::io::DuplexStream,
        tokio::task::JoinHandle<Result<(), SessionError>>,
    ) {
        let (client, server) = tokio::io::duplex(64 * 1024);
        let session = tokio::spawn(async move {
            handle_local_session(server, 1, &SESSION_SECRET, SESSION_ENDPOINT, state).await
        });
        (client, session)
    }

    #[tokio::test]
    async fn session_runs_over_any_async_stream_after_the_hmac_handshake() {
        let (mut client, session) = spawn_duplex_session(RuntimeState::new(1, true));
        let keys = client_handshake(&mut client, &SESSION_SECRET)
            .await
            .expect("the sidecar answers a correct client proof");

        let ping =
            encode_record(RecordKind::Ping, 0, [4; 16], b"", &keys.client_to_sidecar).unwrap();
        write_length_prefixed(&mut client, &ping).await.unwrap();
        let reply = read_length_prefixed(&mut client, LOCAL_MAX_RECORD_BYTES)
            .await
            .unwrap();
        let mut guard = SequenceGuard::new();
        let record = decode_record(&reply, &keys.sidecar_to_client, &mut guard).unwrap();
        assert_eq!(record.kind, RecordKind::Ack);
        assert_eq!(record.message_id, [4; 16]);

        drop(client);
        assert_eq!(session.await.unwrap(), Err(SessionError::Disconnected));
    }

    #[tokio::test]
    async fn session_refuses_a_wrong_client_proof_without_answering() {
        let (mut client, session) = spawn_duplex_session(RuntimeState::new(1, true));
        assert!(
            client_handshake(&mut client, &[0x5b; 32]).await.is_none(),
            "a wrong HMAC must not receive a server hello"
        );
        assert_eq!(session.await.unwrap(), Err(SessionError::Authentication));
    }

    #[tokio::test]
    async fn session_refuses_records_sealed_with_the_wrong_direction_key() {
        let (mut client, session) = spawn_duplex_session(RuntimeState::new(1, true));
        let keys = client_handshake(&mut client, &SESSION_SECRET)
            .await
            .unwrap();
        let reflected =
            encode_record(RecordKind::Ping, 0, [4; 16], b"", &keys.sidecar_to_client).unwrap();
        write_length_prefixed(&mut client, &reflected)
            .await
            .unwrap();
        assert_eq!(session.await.unwrap(), Err(SessionError::Authentication));
    }

    fn confirmed_send(keys: &SessionKeys) -> Vec<u8> {
        let payload = [
            3_u64.to_be_bytes().as_slice(),
            1_u64.to_be_bytes().as_slice(),
            valid_frame().as_slice(),
        ]
        .concat();
        encode_record(
            RecordKind::SendConfirmed,
            0,
            [6; 16],
            &payload,
            &keys.client_to_sidecar,
        )
        .unwrap()
    }

    fn control_call_payload(destination: &EndpointId, millis: u64, command: &[u8]) -> Vec<u8> {
        [
            destination.as_bytes().as_slice(),
            &millis.to_be_bytes(),
            command,
        ]
        .concat()
    }

    fn control_record(destination: EndpointId, millis: u64) -> Record {
        Record {
            kind: RecordKind::ControlCall,
            sequence: 0,
            message_id: [9; 16],
            payload: control_call_payload(&destination, millis, b"{\"command\":\"health\"}"),
        }
    }

    fn control_error(response: &ResponseRecord) -> String {
        assert_eq!(response.kind, RecordKind::Error);
        serde_json::from_slice::<serde_json::Value>(&response.payload).unwrap()["code"]
            .as_str()
            .unwrap()
            .to_owned()
    }

    #[test]
    fn control_call_payload_round_trips_and_rejects_malformed_input() {
        let id = SecretKey::from_bytes(&[0x33; 32]).public();
        let payload = control_call_payload(&id, 500, b"{}");
        let call = parse_control_call(&payload).unwrap();
        assert_eq!(call.destination, id);
        assert_eq!(call.timeout, Duration::from_millis(500));
        assert_eq!(call.command, b"{}");
        for payload in [
            payload[..39].to_vec(),
            control_call_payload(&id, 0, b"{}"),
            control_call_payload(&id, 500, b""),
            control_call_payload(&id, 500, &vec![b'x'; CONTROL_CALL_MAX_BYTES + 1]),
        ] {
            assert_eq!(
                parse_control_call(&payload).unwrap_err(),
                "invalid_control_call"
            );
        }
        assert_eq!(
            parse_control_call(&control_call_payload(&id, 60_001, b"{}")).unwrap_err(),
            "control_timeout_exceeded"
        );
    }

    async fn control_test_host() -> Endpoint {
        bind_endpoint(true, false, Some(&[0x71; 32]), false)
            .await
            .unwrap()
    }

    // A real loopback QUIC peer: observes the authenticated caller and exact
    // command, then holds the reply behind an explicit test barrier.
    async fn control_test_phone(
        response: Vec<u8>,
    ) -> (
        Endpoint,
        tokio::sync::oneshot::Receiver<(EndpointId, Vec<u8>)>,
        tokio::sync::oneshot::Sender<()>,
        tokio::task::JoinHandle<()>,
    ) {
        let phone = Endpoint::builder(presets::Minimal)
            .relay_mode(RelayMode::Disabled)
            .clear_ip_transports()
            .bind_addr(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))
            .unwrap()
            .secret_key(SecretKey::from_bytes(&[0x55; 32]))
            .alpns(vec![NODE_CONTROL_ALPN.to_vec()])
            .bind()
            .await
            .unwrap();
        let server = phone.clone();
        let (observed_tx, observed_rx) = tokio::sync::oneshot::channel();
        let (release_tx, release_rx) = tokio::sync::oneshot::channel();
        let task = tokio::spawn(async move {
            let connection = server.accept().await.unwrap().await.unwrap();
            assert_eq!(connection.alpn(), NODE_CONTROL_ALPN);
            let (mut send, mut recv) = connection.accept_bi().await.unwrap();
            let command = recv.read_to_end(CONTROL_CALL_MAX_BYTES).await.unwrap();
            let _ = observed_tx.send((connection.remote_id(), command));
            if release_rx.await.is_ok() {
                let _ = send.write_all(&response).await;
                let _ = send.finish();
                let _ = send.stopped().await;
            } else {
                let _ = connection.closed().await;
            }
        });
        (phone, observed_rx, release_tx, task)
    }

    async fn control_test_state(host: &Endpoint, phone: &Endpoint) -> Arc<RuntimeState> {
        let state = RuntimeState::with_control_carrier(2, true, host);
        state
            .configure_peer(ConfigurePeerPayload {
                endpoint_id: phone.id().to_string(),
                endpoint_addr: phone.addr(),
                generation: 1,
            })
            .await
            .unwrap();
        state
    }

    #[tokio::test]
    async fn control_call_without_endpoint_or_configured_peer_fails_closed() {
        let id = SecretKey::from_bytes(&[0x33; 32]).public();
        let response =
            process_local_record(control_record(id, 500), 1, &RuntimeState::new(2, true)).await;
        assert_eq!(control_error(&response), "carrier_unavailable");
        let host = control_test_host().await;
        let state = RuntimeState::with_control_carrier(2, true, &host);
        let response = process_local_record(control_record(id, 500), 1, &state).await;
        assert_eq!(control_error(&response), "unknown_peer");
        let response = process_local_record(control_record(id, 60_001), 1, &state).await;
        assert_eq!(control_error(&response), "control_timeout_exceeded");
        host.close().await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn control_call_authenticated_ipc_to_real_loopback_uses_existing_identity() {
        let host = control_test_host().await;
        let (phone, observed, release, server) =
            control_test_phone(b"{\"ok\":true}".to_vec()).await;
        let state = control_test_state(&host, &phone).await;
        let (mut client, session) = spawn_duplex_session(state);
        let keys = client_handshake(&mut client, &SESSION_SECRET)
            .await
            .unwrap();
        let record = control_record(phone.id(), 2000);
        let encoded = encode_record(
            record.kind,
            0,
            record.message_id,
            &record.payload,
            &keys.client_to_sidecar,
        )
        .unwrap();
        write_length_prefixed(&mut client, &encoded).await.unwrap();
        let (identity, command) = timeout(Duration::from_secs(3), observed)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(identity, host.id());
        assert_eq!(command, b"{\"command\":\"health\"}");
        release.send(()).unwrap();
        let bytes = read_length_prefixed(&mut client, LOCAL_MAX_RECORD_BYTES)
            .await
            .unwrap();
        let reply =
            decode_record(&bytes, &keys.sidecar_to_client, &mut SequenceGuard::new()).unwrap();
        assert_eq!(reply.kind, RecordKind::ControlCallResult);
        assert_eq!(reply.message_id, record.message_id);
        assert_eq!(reply.payload, b"{\"ok\":true}");
        drop(client);
        assert_eq!(
            timeout(Duration::from_secs(1), session)
                .await
                .unwrap()
                .unwrap(),
            Err(SessionError::Disconnected)
        );
        server.abort();
        phone.close().await;
        host.close().await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn control_call_revocation_aborts_pending_response_and_next_stream() {
        let host = control_test_host().await;
        let (phone, observed, release, server) = control_test_phone(b"{}".to_vec()).await;
        let state = control_test_state(&host, &phone).await;
        let worker_state = state.clone();
        let id = phone.id();
        let worker = tokio::spawn(async move {
            process_local_record(control_record(id, 2000), 1, &worker_state).await
        });
        timeout(Duration::from_secs(3), observed)
            .await
            .unwrap()
            .unwrap();
        state.configure_peer(loopback_peer(7, 2)).await.unwrap();
        let response = timeout(Duration::from_millis(400), worker)
            .await
            .expect("revocation must abort an outstanding control stream")
            .unwrap();
        assert_eq!(control_error(&response), "peer_rotated");
        let next = process_local_record(control_record(phone.id(), 500), 1, &state).await;
        assert_eq!(control_error(&next), "unknown_peer");
        drop(release);
        timeout(Duration::from_millis(400), server).await.expect("revocation must close the remote connection").unwrap();
        phone.close().await;
        host.close().await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn control_call_rotation_while_queued_prevents_stream_admission() {
        use std::future::Future;
        use std::task::Poll;
        let host = control_test_host().await;
        let (phone, mut observed, release, server) = control_test_phone(b"{}".to_vec()).await;
        let state = control_test_state(&host, &phone).await;
        let slots = state.control_slots.clone()
            .acquire_many_owned(CONTROL_CALL_CONCURRENCY as u32).await.unwrap();
        let call = process_local_record(control_record(phone.id(), 1000), 1, &state);
        tokio::pin!(call);
        // Poll to the admission wait before rotating; no scheduler sleep is
        // used as a proxy for reaching the guarded boundary.
        std::future::poll_fn(|cx| {
            assert!(call.as_mut().poll(cx).is_pending());
            Poll::Ready(())
        }).await;
        state.configure_peer(ConfigurePeerPayload {
            endpoint_id: phone.id().to_string(), endpoint_addr: phone.addr(), generation: 2,
        }).await.unwrap();
        let reply = timeout(Duration::from_millis(400), call).await.unwrap();
        assert_eq!(control_error(&reply), "peer_rotated");
        assert_eq!(observed.try_recv(), Err(tokio::sync::oneshot::error::TryRecvError::Empty));
        assert_eq!(state.outbound_slots.available_permits(), 2);
        drop(slots); drop(release); server.abort(); phone.close().await; host.close().await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn control_call_queue_wait_does_not_reset_absolute_deadline() {
        let host = control_test_host().await;
        let (phone, _observed, release, server) = control_test_phone(b"{}".to_vec()).await;
        let state = control_test_state(&host, &phone).await;
        let slots = state
            .control_slots
            .clone()
            .acquire_many_owned(CONTROL_CALL_CONCURRENCY as u32)
            .await
            .unwrap();
        let free = tokio::spawn(async move {
            sleep(Duration::from_millis(350)).await;
            drop(slots);
        });
        let start = Instant::now();
        let reply = process_local_record(control_record(phone.id(), 500), 1, &state).await;
        assert_eq!(control_error(&reply), "control_read_timeout");
        assert!(
            start.elapsed() < Duration::from_millis(700),
            "queue time was added to budget: {:?}",
            start.elapsed()
        );
        free.await.unwrap();
        drop(release);
        server.abort();
        phone.close().await;
        host.close().await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn control_call_client_hangup_releases_slot_and_session() {
        let host = control_test_host().await;
        let (phone, observed, release, server) = control_test_phone(b"{}".to_vec()).await;
        let state = control_test_state(&host, &phone).await;
        let (mut client, session) = spawn_duplex_session(state.clone());
        let keys = client_handshake(&mut client, &SESSION_SECRET)
            .await
            .unwrap();
        let record = control_record(phone.id(), 2000);
        let encoded = encode_record(
            record.kind,
            0,
            record.message_id,
            &record.payload,
            &keys.client_to_sidecar,
        )
        .unwrap();
        write_length_prefixed(&mut client, &encoded).await.unwrap();
        timeout(Duration::from_secs(3), observed)
            .await
            .unwrap()
            .unwrap();
        drop(client);
        timeout(Duration::from_millis(400), session)
            .await
            .expect("hangup must cancel carrier work")
            .unwrap()
            .unwrap();
        assert_eq!(
            state.control_slots.available_permits(),
            CONTROL_CALL_CONCURRENCY
        );
        drop(release);
        server.abort();
        phone.close().await;
        host.close().await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn control_call_bounds_remote_response_and_releases_permit() {
        for (body, expected) in [
            (Vec::new(), "empty_control_call_result"),
            (
                vec![b'x'; CONTROL_CALL_MAX_BYTES + 1],
                "control_response_too_large",
            ),
        ] {
            let host = control_test_host().await;
            let (phone, observed, release, server) = control_test_phone(body).await;
            let state = control_test_state(&host, &phone).await;
            let worker_state = state.clone();
            let id = phone.id();
            let worker = tokio::spawn(async move {
                process_local_record(control_record(id, 2000), 1, &worker_state).await
            });
            timeout(Duration::from_secs(3), observed)
                .await
                .unwrap()
                .unwrap();
            release.send(()).unwrap();
            let reply = worker.await.unwrap();
            assert_eq!(control_error(&reply), expected);
            assert_eq!(
                state.control_slots.available_permits(),
                CONTROL_CALL_CONCURRENCY
            );
            server.abort();
            phone.close().await;
            host.close().await;
        }
    }

    /// A confirmed send stays pending (no outbound worker runs here) until
    /// the client hangs up, which ends the session cleanly.
    #[tokio::test]
    async fn pending_confirmed_send_ends_cleanly_when_the_client_hangs_up() {
        let state = RuntimeState::new(1, true);
        state.configure_peer(loopback_peer(1, 3)).await.unwrap();
        let (mut client, session) = spawn_duplex_session(state);
        let keys = client_handshake(&mut client, &SESSION_SECRET)
            .await
            .unwrap();
        write_length_prefixed(&mut client, &confirmed_send(&keys))
            .await
            .unwrap();
        tokio::task::yield_now().await;
        assert!(!session.is_finished());

        drop(client);
        assert_eq!(
            timeout(Duration::from_secs(5), session)
                .await
                .expect("hang-up is observed while the send is pending")
                .unwrap(),
            Ok(())
        );
    }

    /// Any byte the client sends while a confirmed send is pending is a
    /// protocol violation.
    #[tokio::test]
    async fn pending_confirmed_send_refuses_unexpected_client_bytes() {
        let state = RuntimeState::new(1, true);
        state.configure_peer(loopback_peer(1, 3)).await.unwrap();
        let (mut client, session) = spawn_duplex_session(state);
        let keys = client_handshake(&mut client, &SESSION_SECRET)
            .await
            .unwrap();
        write_length_prefixed(&mut client, &confirmed_send(&keys))
            .await
            .unwrap();
        client.write_all(&[0]).await.unwrap();
        assert_eq!(
            timeout(Duration::from_secs(5), session)
                .await
                .expect("the stray byte is observed while the send is pending")
                .unwrap(),
            Err(SessionError::Protocol)
        );
    }

    /// The sidecar serves phone stage objects on the artifact ALPN under a
    /// seed grant: a phone acquires the synthetic v2 stage end to end and the
    /// store-loaded stage passes its golden. Without the artifact
    /// configuration the ALPN is not offered at all.
    #[tokio::test(flavor = "multi_thread")]
    async fn the_sidecar_serves_artifact_chunks_only_when_configured() {
        use base64::Engine as _;
        use mycelium_mobile_core::artifact::ArtifactStore;
        use mycelium_mobile_core::artifact_grant as ag;
        use mycelium_mobile_core::artifact_net::IrohChunkSource;
        use mycelium_mobile_core::identity::Identity;
        use mycelium_mobile_core::stage_manifest::StageManifest;
        use mycelium_mobile_core::transport::Reachability;

        let fixtures = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../mobile_core/tests/fixtures");
        let read = |name: &str| -> serde_json::Value {
            serde_json::from_str(&fs::read_to_string(fixtures.join(name)).unwrap()).unwrap()
        };
        let scratch = std::env::temp_dir().join(format!("mycelium-sidecar-artifacts-{}", std::process::id()));
        let _ = fs::remove_dir_all(&scratch);
        let objects = scratch.join("objects");
        fs::create_dir_all(&objects).unwrap();
        let file = base64::engine::general_purpose::STANDARD
            .decode(read("qwen2_synthetic.json")["safetensors_b64"].as_str().unwrap())
            .unwrap();
        let header = mycelium_mobile_core::safetensors::parse_file(&file).unwrap();
        for name in header.tensors.keys() {
            let (start, end) = header.range(name).unwrap();
            let bytes = &file[start as usize..end as usize];
            fs::write(objects.join(hex::encode(Sha256::digest(bytes))), bytes).unwrap();
        }
        let manifest = StageManifest::from_document(&read("qwen2_stage_manifest_v2.json")["manifest"]).unwrap();
        let seed = Identity::from_secret("seed-endpoint", &[0x22; 32]).unwrap();

        let endpoint = bind_endpoint(true, false, Some(&[0x71; 32]), true).await.unwrap();
        let source = ArtifactSource::with_endpoint(endpoint.clone(), &[0x72; 32], seed.public_key_record(), objects.clone(), &scratch.join("state")).unwrap();
        let (source_id, source_digest) = (source.endpoint_id(), source.key_digest());
        assert_eq!(source_id, endpoint.id().to_string(), "the grant names the sidecar's own endpoint");
        tokio::spawn(incoming_worker(endpoint.clone(), RuntimeState::new(4, true), Some(source)));

        let phone_secret = [0x73_u8; 32];
        let phone_id = SecretKey::from_bytes(&phone_secret).public().to_string();
        let member = Arc::new(Identity::from_secret(&phone_id, &[0x11; 32]).unwrap());
        let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64;
        let mut objects_list: Vec<String> = manifest.tensors.iter().map(|t| t.sha256.clone()).collect();
        objects_list.sort();
        objects_list.dedup();
        let message = json!({
            "protocol": ag::GRANT_PROTOCOL, "grant_id": "grant-sidecar-1", "swarm_id": "swarm-a",
            "seed_node_id": "seed-node", "recipient_node_id": "node-phone", "recipient_endpoint_id": phone_id,
            "recipient_key_digest": member.verification_key_digest(), "assignment_id": "route-1",
            "manifest_pack_digest": manifest.pack_digest, "allowed_object_digests": objects_list,
            "authorized_sources": [{"endpoint_id": source_id, "verification_key_digest": source_digest, "direct_addrs": []}],
            "maximum_total_bytes": manifest.total_bytes(), "maximum_bytes_per_second": 64_u64 << 20,
            "maximum_chunk_bytes": 4_u64 << 20, "issued_at_unix_ms": now, "not_before_unix_ms": now,
            "expires_at_unix_ms": now + 900_000,
        });
        let envelope = json!({"protocol": "mycelium.membership.signed_message.v1", "signature": seed.sign(&message).unwrap(),
                              "verification_key": seed.public_key_record(), "message": message});
        let grant = ag::verify_grant(&envelope, &seed.public_key_record(), now).unwrap();

        let acquire = |sources: Vec<EndpointAddr>, store_dir: PathBuf, grant: ag::Grant, member: Arc<Identity>, manifest: StageManifest| async move {
            let client = IrohChunkSource::bind(&phone_secret, member, grant, sources, Reachability::LocalOnly).await.unwrap();
            tokio::task::spawn_blocking(move || {
                let mut client = client;
                let store = ArtifactStore::open(&store_dir, 1 << 30).map_err(|e| e.0)?;
                store.acquire(&manifest, &mut client, 1024, None).map_err(|e| e.0)?;
                store.load_stage_for_serving(&manifest).map(|_| ()).map_err(|e| e.0)
            })
            .await
            .unwrap()
        };
        acquire(vec![endpoint.addr()], scratch.join("store"), grant.clone(), member.clone(), manifest.clone()).await.unwrap();

        // A sidecar started without the artifact configuration does not offer the ALPN.
        endpoint.close().await;
        let plain = bind_endpoint(true, false, Some(&[0x71; 32]), false).await.unwrap();
        tokio::spawn(incoming_worker(plain.clone(), RuntimeState::new(4, true), None));
        let refused = acquire(vec![plain.addr()], scratch.join("store2"), grant, member, manifest).await.unwrap_err();
        assert_eq!(refused, "artifact_source_unreachable");
        plain.close().await;
        let _ = fs::remove_dir_all(&scratch);
    }


    /// DoS: artifact clients holding many connections (more than the
    /// sidecar's connection slots, from several peers), each with a half-open
    /// request stream, must not delay Router traffic: a configured Router
    /// peer's frame is still admitted promptly.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn artifact_load_never_starves_router_connections() {
        use mycelium_mobile_core::artifact_grant::ARTIFACT_CHUNK_ALPN;
        let scratch = std::env::temp_dir().join(format!("mycelium-sidecar-dos-{}", std::process::id()));
        let _ = fs::remove_dir_all(&scratch);
        fs::create_dir_all(scratch.join("objects")).unwrap();
        let seed = mycelium_mobile_core::identity::Identity::from_secret("seed-endpoint", &[0x22; 32]).unwrap();
        let endpoint = bind_endpoint(true, false, Some(&[0x81; 32]), true).await.unwrap();
        let source = ArtifactSource::with_endpoint(endpoint.clone(), &[0x82; 32], seed.public_key_record(), scratch.join("objects"), &scratch.join("state")).unwrap();
        // Two connection slots in total.
        let state = RuntimeState::new(2, true);
        tokio::spawn(incoming_worker(endpoint.clone(), state.clone(), Some(source)));
        let client = |byte: u8| async move {
            Endpoint::builder(presets::Minimal)
                .relay_mode(RelayMode::Disabled)
                .clear_ip_transports()
                .bind_addr(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))
                .unwrap()
                .secret_key(SecretKey::from_bytes(&[byte; 32]))
                .bind()
                .await
                .unwrap()
        };
        // Six peers each hold two idle artifact connections, and each keeps
        // one half-open stream (a request that never finishes).
        let mut held = Vec::new();
        for peer in 0..6u8 {
            let c = client(0x90 + peer).await;
            for _ in 0..2 {
                if let Ok(Ok(conn)) = timeout(Duration::from_secs(1), c.connect(endpoint.addr(), ARTIFACT_CHUNK_ALPN)).await {
                    if let Ok((mut send, recv)) = conn.open_bi().await {
                        let _ = send.write_all(b"{\"partial\":").await;
                        held.push((conn, send, recv));
                    }
                }
            }
        }
        // A configured Router peer sends one frame: it must be admitted fast.
        let router = client(0xA0).await;
        state
            .configure_peer(ConfigurePeerPayload { endpoint_id: router.id().to_string(), endpoint_addr: router.addr(), generation: 3 })
            .await
            .unwrap();
        let mut payload = 3u64.to_be_bytes().to_vec();
        payload.extend_from_slice(&valid_frame());
        let transfer = encode_remote_frame(RemoteKind::Transfer, [7; 16], &payload).unwrap();
        let started = Instant::now();
        let sent = timeout(Duration::from_secs(3), async {
            let conn = router.connect(endpoint.addr(), IROH_ALPN).await.unwrap();
            let (mut send, _recv) = conn.open_bi().await.unwrap();
            send.write_all(&transfer).await.unwrap();
            send.finish().unwrap();
            while state.inbound_frames_admitted.load(Ordering::Acquire) == 0 {
                sleep(Duration::from_millis(5)).await;
            }
            conn
        })
        .await;
        let waited = started.elapsed();
        assert!(sent.is_ok(), "the Router frame was not admitted within 3 s under artifact load");
        assert!(waited < Duration::from_secs(2), "Router frame admission took {waited:?}");
        drop(held);
        endpoint.close().await;
        let _ = fs::remove_dir_all(&scratch);
    }

}
