// SPDX-License-Identifier: AGPL-3.0-only
//! The sidecar entry point: `mycelium-iroh-sidecar`'s `main` and `run_sidecar` composed from
//! the ported pieces (`native/iroh_transport/src/bin/mycelium-iroh-sidecar.rs` and
//! `sidecar.rs:572-750 @ f4e366e9`): command line, bootstrap secret, endpoint secret,
//! artifact-source flags, owner-only socket, endpoint, artifact service, readiness gate, the
//! one `ready` line, the republisher and the listener loop.
//!
//! Exit codes and stderr events are the oracle's: a command-line error is exit 2 (clap), every
//! other refusal logs one `{"event":"..."}` line and exits 1 (`bootstrap_rejected`,
//! `endpoint_secret_rejected`, `artifact_source_rejected`, `fatal`), a clean stop exits 0.
//!
//! Differences, all fail-closed and recorded in `IROH-PARITY.md`:
//! - The production-relay and `--force-relay` modes wait the registry's
//!   `SidecarReadyAddressWindow` (3 s, `ready_address.rs:45`) for an address, name
//!   `ready_address_incomplete` when none comes and still publish `ready`, as the executed
//!   oracle does under a deny-outbound sandbox. The relay-ONLINE path and two physical hosts are
//!   NOT RUN (RU-022).
//! - A closed `--bootstrap-fd` ends `bootstrap_rejected`, exit 1: the same as the SHIPPED
//!   (release) oracle (executed; a debug build aborts, which v0.1.4 never ships).
//! - The artifact ALPN is served through the [`ArtifactBackend`]: the binary's
//!   [`crate::sidecar_artifact::DurableArtifactBackend`] (durable admission journal, equal to the
//!   oracle's on 26 recorded journal states and an 18-request scenario) with the registry's
//!   `ArtifactMaxFence` live-fence capacity.
use crate::{
    iroh_host::{LoopbackHost, NetworkMode, Reach},
    iroh_pool::{ConnectionService, DriverLimits, PooledRouter, Services},
    sidecar_listener::{LocalServer, serve_unix_listener},
    sidecar_local::ControlCallLimits,
    sidecar_pool::PoolLimits,
    sidecar_process::{
        AddressLineSink, BootstrapSecret, SidecarError, SocketCleanup, await_advertised_address,
        bind_secure_uds, event_line, posix_ready_record, read_bootstrap_descriptor,
        read_endpoint_secret, republish_address_changes,
    },
    sidecar_session::SessionLimits,
};
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant, Span};
use protocol::identity::Identity;
use std::{
    io::{self, Write},
    num::NonZeroUsize,
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::sync::watch;
use transport::{Channel, Clock};

/// Where the sidecar writes: stdout carries the ready line and `address_changed` lines,
/// stderr the events. Tests supply buffers; the binary supplies [`ProcessOutput`].
pub trait SidecarOutput: Send + Sync + 'static {
    fn stdout_line(&self, line: &str) -> io::Result<()>;
    fn stderr_line(&self, line: &str);
}
/// The real process streams. A failed stderr write has nowhere further to be reported.
pub struct ProcessOutput;
impl SidecarOutput for ProcessOutput {
    fn stdout_line(&self, line: &str) -> io::Result<()> {
        let mut stdout = io::stdout().lock();
        writeln!(stdout, "{line}")?;
        stdout.flush()
    }
    fn stderr_line(&self, line: &str) {
        let mut stderr = io::stderr().lock();
        if writeln!(stderr, "{line}").is_err() {
            // stderr is gone; the process keeps serving, as the oracle does.
        }
    }
}
struct StdoutSink(Arc<dyn SidecarOutput>);
impl AddressLineSink for StdoutSink {
    fn emit(&mut self, line: &str) {
        if self.0.stdout_line(line).is_err() {
            // The reader of stdout is gone: nothing further can be advertised.
        }
    }
}
fn log_event(output: &dyn SidecarOutput, event: &'static str) {
    output.stderr_line(&event_line(event));
}

// ---- command line (clap rules of the oracle's binary) ------------------------------------

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ArtifactArguments {
    pub objects: PathBuf,
    pub seed_key: PathBuf,
    pub state: PathBuf,
    pub signing_key_file: PathBuf,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Arguments {
    pub uds: PathBuf,
    pub bootstrap_fd: i32,
    pub endpoint_secret_file: Option<PathBuf>,
    pub local_only: bool,
    pub force_relay: bool,
    pub queue_capacity: NonZeroUsize,
    pub artifact: Option<ArtifactArguments>,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ArgError {
    /// `--help` / `--version`: printed to stdout, exit 0 (clap).
    Informational(String),
    /// Anything clap refuses: printed to stderr, exit 2.
    Usage(String),
}
impl ArgError {
    pub const fn exit_code(&self) -> i32 {
        match self {
            Self::Informational(_) => 0,
            Self::Usage(_) => 2,
        }
    }
}
fn usage(message: impl Into<String>) -> ArgError {
    ArgError::Usage(message.into())
}

const OPTIONS: [&str; 8] = [
    "--uds",
    "--bootstrap-fd",
    "--endpoint-secret-file",
    "--queue-capacity",
    "--artifact-objects",
    "--artifact-seed-key",
    "--artifact-state",
    "--artifact-signing-key-file",
];
const FLAGS: [&str; 2] = ["--local-only", "--force-relay"];

/// Parses `argv[1..]` by the oracle's clap definition: `--name value` or `--name=value`, each
/// option at most once, a value never starts with `-`, `--uds` and `--bootstrap-fd` required,
/// `--force-relay` conflicts with `--local-only`, `--artifact-objects` requires the other three
/// artifact options and each of them requires `--artifact-objects`.
pub fn parse_arguments(args: &[String]) -> Result<Arguments, ArgError> {
    let mut values: Vec<(&'static str, String)> = Vec::new();
    let mut flags: Vec<&'static str> = Vec::new();
    let mut index = 0;
    while index < args.len() {
        let argument = &args[index];
        index += 1;
        if argument == "--help" || argument == "-h" {
            return Err(ArgError::Informational(
                "Usage: mycelium-rs-sidecar --uds <PATH> --bootstrap-fd <FD> [OPTIONS]".to_owned(),
            ));
        }
        if argument == "--version" || argument == "-V" {
            return Err(ArgError::Informational(format!(
                "mycelium-rs-sidecar {}",
                env!("CARGO_PKG_VERSION")
            )));
        }
        let (name, inline) = match argument.split_once('=') {
            Some((name, value)) if name.starts_with("--") => (name, Some(value.to_owned())),
            _ => (argument.as_str(), None),
        };
        if let Some(flag) = FLAGS.iter().find(|flag| **flag == name) {
            if inline.is_some() || flags.contains(flag) {
                return Err(usage(format!("flag {flag} misused")));
            }
            flags.push(flag);
            continue;
        }
        let Some(option) = OPTIONS.iter().find(|option| **option == name) else {
            return Err(usage(format!("unexpected argument '{argument}'")));
        };
        if values.iter().any(|(seen, _)| seen == option) {
            return Err(usage(format!("{option} cannot be used multiple times")));
        }
        let value = match inline {
            Some(value) => value,
            None => {
                let next = args
                    .get(index)
                    .ok_or_else(|| usage(format!("{option} requires a value")))?;
                index += 1;
                next.clone()
            }
        };
        if value.starts_with('-') {
            return Err(usage(format!("unexpected value '{value}' for {option}")));
        }
        values.push((option, value));
    }
    let get = |name: &str| {
        values
            .iter()
            .find(|(option, _)| *option == name)
            .map(|(_, value)| value.clone())
    };
    let uds = get("--uds").ok_or_else(|| usage("--uds is required"))?;
    let bootstrap_fd = get("--bootstrap-fd").ok_or_else(|| usage("--bootstrap-fd is required"))?;
    let bootstrap_fd = bootstrap_fd
        .parse::<i32>()
        .map_err(|_| usage("invalid value for --bootstrap-fd"))?;
    let default_capacity = B::SidecarQueueCapacity
        .count()
        .ok()
        .and_then(|entries| usize::try_from(entries.get()).ok())
        .and_then(NonZeroUsize::new)
        .ok_or_else(|| usage("queue capacity budget unreadable"))?;
    let queue_capacity = match get("--queue-capacity") {
        Some(text) => text
            .parse::<NonZeroUsize>()
            .map_err(|_| usage("invalid value for --queue-capacity"))?,
        None => default_capacity,
    };
    let local_only = flags.contains(&"--local-only");
    let force_relay = flags.contains(&"--force-relay");
    if local_only && force_relay {
        return Err(usage("--force-relay conflicts with --local-only"));
    }
    let artifact_names = [
        "--artifact-objects",
        "--artifact-seed-key",
        "--artifact-state",
        "--artifact-signing-key-file",
    ];
    let artifact = match get("--artifact-objects") {
        Some(objects) => {
            let (Some(seed_key), Some(state), Some(signing_key_file)) = (
                get("--artifact-seed-key"),
                get("--artifact-state"),
                get("--artifact-signing-key-file"),
            ) else {
                return Err(usage(
                    "--artifact-objects requires the other artifact options",
                ));
            };
            Some(ArtifactArguments {
                objects: PathBuf::from(objects),
                seed_key: PathBuf::from(seed_key),
                state: PathBuf::from(state),
                signing_key_file: PathBuf::from(signing_key_file),
            })
        }
        None => {
            if artifact_names.iter().any(|name| get(name).is_some()) {
                return Err(usage("artifact options require --artifact-objects"));
            }
            None
        }
    };
    Ok(Arguments {
        uds: PathBuf::from(uds),
        bootstrap_fd,
        endpoint_secret_file: get("--endpoint-secret-file").map(PathBuf::from),
        local_only,
        force_relay,
        queue_capacity,
        artifact,
    })
}

// ---- artifact source configuration -------------------------------------------------------

/// The validated artifact flags (`artifact_config`, the binary's lines 196-206): the seed key
/// record parsed as JSON and the receipt-signing key read with the endpoint key's checks.
pub struct ArtifactSourceConfig {
    pub objects: PathBuf,
    pub seed_key: serde_json::Value,
    pub state_dir: PathBuf,
    pub signing_secret: BootstrapSecret,
}
pub fn artifact_config(
    arguments: &ArtifactArguments,
) -> Result<ArtifactSourceConfig, SidecarError> {
    let seed = std::fs::read(&arguments.seed_key).map_err(|_| SidecarError::ArtifactSource)?;
    let seed_key = serde_json::from_slice(&seed).map_err(|_| SidecarError::ArtifactSource)?;
    let signing_secret = read_endpoint_secret(&arguments.signing_key_file)
        .map_err(|_| SidecarError::ArtifactSource)?;
    Ok(ArtifactSourceConfig {
        objects: arguments.objects.clone(),
        seed_key,
        state_dir: arguments.state.clone(),
        signing_secret,
    })
}
/// What the backend builds an artifact service from: the signer is bound to this sidecar's
/// endpoint id, exactly as `ArtifactSource::with_endpoint` binds it.
pub struct ArtifactBuild {
    pub signer: Identity,
    pub seed_key: serde_json::Value,
    pub objects: PathBuf,
    pub state_dir: PathBuf,
}
/// Builds the service for `mycelium.artifact_chunk.v1` (content-verified object store and the
/// durable admission journal). The bare binary has none ([`NoArtifactBackend`]).
pub trait ArtifactBackend {
    fn serve(&self, build: ArtifactBuild) -> Result<Arc<dyn ConnectionService>, SidecarError>;
}
pub struct NoArtifactBackend;
impl ArtifactBackend for NoArtifactBackend {
    fn serve(&self, _build: ArtifactBuild) -> Result<Arc<dyn ConnectionService>, SidecarError> {
        Err(SidecarError::ArtifactSource)
    }
}

// ---- limits and clock --------------------------------------------------------------------

struct Mono(Instant);
impl Clock for Mono {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(
            ClockDomain(1),
            u64::try_from(self.0.elapsed().as_millis()).unwrap_or(u64::MAX),
        )
    }
    fn wall_seconds(&self) -> f64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0.0, |elapsed| elapsed.as_secs_f64())
    }
}
fn span(id: B) -> Result<Span, SidecarError> {
    id.span(Entries::ZERO).map_err(|_| SidecarError::Runtime)
}
fn count(id: B) -> Result<usize, SidecarError> {
    id.count()
        .ok()
        .and_then(|entries| usize::try_from(entries.get()).ok())
        .ok_or(SidecarError::Runtime)
}
/// The shipped bounds, every one from the budget registry.
pub fn driver_limits(queue_capacity: NonZeroUsize) -> Result<DriverLimits, SidecarError> {
    Ok(DriverLimits {
        pool: PoolLimits {
            capacity: queue_capacity.get(),
            seen_factor: count(B::SidecarSeenFactor)?,
            seen_floor: count(B::SidecarSeenFloor)?,
            initial_retry: span(B::SidecarRetryInitial)?,
            max_retry: span(B::SidecarRetryMax)?,
            lifetime: span(B::SidecarOutboundLifetime)?,
        },
        connect: span(B::SidecarPeerConnect)?,
        stream_io: span(B::SidecarStreamIo)?,
    })
}
pub fn session_limits() -> Result<SessionLimits, SidecarError> {
    Ok(SessionLimits {
        handshake: span(B::SidecarLocalHandshake)?,
        receive_poll: span(B::SidecarReceivePollWait)?,
        control: ControlCallLimits {
            ceiling_millis: span(B::NodeControlCallTimeoutCeiling)?.millis(),
            max_command_bytes: B::SidecarControlCallMaxBytes
                .bytes()
                .ok()
                .and_then(|bytes| usize::try_from(bytes.get()).ok())
                .ok_or(SidecarError::Runtime)?,
        },
    })
}

// ---- run_sidecar -------------------------------------------------------------------------

pub struct SidecarConfig {
    /// Relay selection of the network modes (ignored with `local_only`).
    pub reach: Reach,
    /// How long the address gate waits for a relay address before `ready` (the oracle's
    /// `READY_ADDRESS_WINDOW`, `ready_address.rs:45`). The registry id is
    /// (`SidecarReadyAddressWindow`): `None` refuses the relay modes.
    pub ready_window: Option<Span>,
    pub socket: PathBuf,
    pub local_only: bool,
    pub force_relay: bool,
    pub queue_capacity: NonZeroUsize,
    pub endpoint_secret: Option<BootstrapSecret>,
    pub artifact: Option<ArtifactSourceConfig>,
}

/// The shipped ready-address window: `READY_ADDRESS_WINDOW` (`ready_address.rs:45 @ f4e366e9`,
/// 3 s), registered by L1b as `SidecarReadyAddressWindow` (seam SE-L9-3). A registry that
/// refused the id would leave `None`, which refuses the relay modes before any socket exists.
fn shipped_ready_window() -> Option<Span> {
    B::SidecarReadyAddressWindow.span(Entries::ZERO).ok()
}

/// Runs until `stop` resolves (the binary passes SIGINT, as the oracle's `ctrl_c`) or the
/// listener fails. Order as the oracle: socket, endpoint, artifact source, address gate, queue
/// workers, `artifact_source_ready` event, the ready line, republisher, listener loop.
pub fn run_sidecar(
    runtime: &tokio::runtime::Runtime,
    config: SidecarConfig,
    secret: BootstrapSecret,
    backend: &dyn ArtifactBackend,
    output: Arc<dyn SidecarOutput>,
    stop: impl std::future::Future<Output = ()>,
) -> Result<(), SidecarError> {
    if config.local_only && config.force_relay {
        return Err(SidecarError::InvalidNetworkMode);
    }
    if !config.local_only && config.ready_window.is_none() {
        // No ready-address window: fail closed before any socket or endpoint exists.
        return Err(SidecarError::NetworkModeUnsupported);
    }
    let mode = if config.local_only {
        NetworkMode::LocalOnly
    } else if config.force_relay {
        NetworkMode::ForceRelay
    } else {
        NetworkMode::Production
    };
    let guard = runtime.enter();
    let listener = bind_secure_uds(&config.socket)?;
    let _socket_cleanup = SocketCleanup(config.socket.clone());
    drop(guard);
    let clock = Arc::new(Mono(Instant::now()));
    let endpoint_key = match &config.endpoint_secret {
        Some(secret) => iroh::SecretKey::from_bytes(secret.as_bytes()),
        None => iroh::SecretKey::from_bytes(
            &crate::sidecar_process::os_random_nonce().map_err(|_| SidecarError::EndpointBind)?,
        ),
    };
    let mut channels = vec![Channel::Router];
    if config.artifact.is_some() {
        channels.push(Channel::ArtifactChunk);
    }
    let bind_deadline = clock
        .monotonic()
        .checked_add(span(B::WorkerStartup)?)
        .map_err(|_| SidecarError::Runtime)?;
    let mut host = LoopbackHost::bind_network(
        runtime,
        &*clock,
        bind_deadline,
        endpoint_key,
        &channels,
        mode,
        &config.reach,
    )
    .map_err(|_| SidecarError::EndpointBind)?;
    let outcome = serve(
        runtime, &config, &secret, backend, &output, &clock, &mut host, &listener, stop,
    );
    let close_deadline = clock
        .monotonic()
        .checked_add(span(B::RouterClose)?)
        .map_err(|_| SidecarError::Runtime)?;
    let closed = host.close(&*clock, close_deadline);
    outcome?;
    closed.map_err(|_| SidecarError::Runtime)
}

#[allow(clippy::too_many_arguments)]
fn serve(
    runtime: &tokio::runtime::Runtime,
    config: &SidecarConfig,
    secret: &BootstrapSecret,
    backend: &dyn ArtifactBackend,
    output: &Arc<dyn SidecarOutput>,
    clock: &Arc<Mono>,
    host: &mut LoopbackHost<'_>,
    listener: &tokio::net::UnixListener,
    stop: impl std::future::Future<Output = ()>,
) -> Result<(), SidecarError> {
    let address = host.address().map_err(|_| SidecarError::EndpointBind)?;
    let endpoint_id = address.id.to_string();
    let mut services = Services::default();
    let mut artifact_ready = None;
    if let Some(artifact) = &config.artifact {
        let signer = Identity::from_secret(&endpoint_id, artifact.signing_secret.as_bytes())
            .map_err(|_| SidecarError::ArtifactSource)?;
        artifact_ready = Some(serde_json::json!({
            "event": "artifact_source_ready",
            "endpoint_id": signer.endpoint_id(),
            "verification_key_digest": signer.verification_key_digest(),
        }));
        services.artifact = Some(backend.serve(ArtifactBuild {
            signer,
            seed_key: artifact.seed_key.clone(),
            objects: artifact.objects.clone(),
            state_dir: artifact.state_dir.clone(),
        })?);
    }
    let events = output.clone();
    services.events = Some(Arc::new(move |event, key, generation| {
        // A key that is not a valid endpoint id was never dialled or accepted, so it has no line.
        if let Ok(remote) = iroh::EndpointId::from_bytes(&key) {
            events.stderr_line(&event.line(&remote.to_string(), generation));
        }
    }));
    // The advertised address is read through the gate, as the oracle's live watcher. In
    // local-only mode a usable address exists at bind, so no wait window is needed; the relay
    // modes wait (bounded) for a relay address and name the outcome when the window expires.
    let (address_sender, addresses, advertised, readiness) = if config.local_only {
        let (address_sender, mut addresses) = watch::channel(address.clone());
        let (advertised, readiness) = runtime.block_on(await_advertised_address(
            &mut addresses,
            false,
            std::future::ready(()),
            Duration::ZERO,
        ));
        (Some(address_sender), addresses, advertised, readiness)
    } else {
        let window = config
            .ready_window
            .ok_or(SidecarError::NetworkModeUnsupported)?;
        let mut addresses = host
            .address_watch()
            .map_err(|_| SidecarError::EndpointBind)?;
        let online = host.online().map_err(|_| SidecarError::EndpointBind)?;
        let (advertised, readiness) = runtime.block_on(await_advertised_address(
            &mut addresses,
            true,
            online,
            Duration::from_millis(window.millis()),
        ));
        (None, addresses, advertised, readiness)
    };
    log_event(&**output, readiness.event());
    let router = Arc::new(
        PooledRouter::start_with(
            host,
            clock.clone(),
            driver_limits(config.queue_capacity)?,
            services,
        )
        .map_err(|_| SidecarError::Runtime)?,
    );
    if let Some(event) = artifact_ready {
        output.stderr_line(&event.to_string());
    }
    let ready = posix_ready_record(
        &endpoint_id,
        &advertised,
        std::str::from_utf8(Channel::Router.alpn()).map_err(|_| SidecarError::ReadyOutput)?,
    )?;
    output
        .stdout_line(&ready)
        .map_err(|_| SidecarError::ReadyOutput)?;
    let republisher = runtime.spawn(republish_address_changes(
        endpoint_id.clone(),
        addresses,
        advertised,
        StdoutSink(output.clone()),
    ));
    let server = LocalServer {
        router: router.clone(),
        secret: copy_secret(secret),
        endpoint_id,
        limits: session_limits()?,
        max_sessions: count(B::SidecarLocalSessions)?,
        events: {
            let output = output.clone();
            Arc::new(move |event| log_event(&*output, event))
        },
    };
    let result = runtime.block_on(serve_unix_listener(listener, &server, stop));
    runtime.block_on(router.shutdown());
    republisher.abort();
    drop(address_sender);
    drop(server);
    result
}
fn copy_secret(secret: &BootstrapSecret) -> BootstrapSecret {
    BootstrapSecret::from_bytes(*secret.as_bytes())
}

// ---- main --------------------------------------------------------------------------------

/// The process: parse, bootstrap, endpoint secret, artifact flags, run. Returns the exit code.
pub fn sidecar_main(
    args: &[String],
    output: Arc<dyn SidecarOutput>,
    backend: &dyn ArtifactBackend,
) -> i32 {
    let arguments = match parse_arguments(args) {
        Ok(arguments) => arguments,
        Err(ArgError::Informational(text)) => {
            if output.stdout_line(&text).is_err() {
                return 1;
            }
            return 0;
        }
        Err(error @ ArgError::Usage(_)) => {
            if let ArgError::Usage(text) = &error {
                output.stderr_line(text);
            }
            return error.exit_code();
        }
    };
    let Ok(secret) = read_bootstrap_descriptor(arguments.bootstrap_fd) else {
        log_event(&*output, "bootstrap_rejected");
        return 1;
    };
    let endpoint_secret = match arguments.endpoint_secret_file.as_deref() {
        Some(path) => match read_endpoint_secret(path) {
            Ok(secret) => Some(secret),
            Err(_) => {
                log_event(&*output, "endpoint_secret_rejected");
                return 1;
            }
        },
        None => None,
    };
    let artifact = match arguments.artifact.as_ref().map(artifact_config) {
        Some(Ok(config)) => Some(config),
        Some(Err(_)) => {
            log_event(&*output, "artifact_source_rejected");
            return 1;
        }
        None => None,
    };
    let Ok(runtime) = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    else {
        log_event(&*output, "fatal");
        return 1;
    };
    let config = SidecarConfig {
        reach: Reach::Shipped,
        ready_window: shipped_ready_window(),
        socket: arguments.uds.clone(),
        local_only: arguments.local_only,
        force_relay: arguments.force_relay,
        queue_capacity: arguments.queue_capacity,
        endpoint_secret,
        artifact,
    };
    // The SIGINT handler exists before the socket, endpoint and ready line do: tokio installs
    // `ctrl_c()` only when first polled, which is after `ready`, and a SIGINT in between would
    // kill the process by signal instead of stopping it cleanly (exit 0).
    let interrupt_signal = {
        let _guard = runtime.enter();
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())
    };
    let interrupt = async move {
        match interrupt_signal {
            Ok(mut signal) => {
                signal.recv().await;
            }
            // No handler could be installed: serve until the listener fails.
            Err(_) => std::future::pending::<()>().await,
        }
    };
    match run_sidecar(&runtime, config, secret, backend, output.clone(), interrupt) {
        Ok(()) => 0,
        Err(_) => {
            log_event(&*output, "fatal");
            1
        }
    }
}

/// The socket path an entry binds, for callers that clean up after it.
pub fn socket_of(arguments: &Arguments) -> &Path {
    &arguments.uds
}
