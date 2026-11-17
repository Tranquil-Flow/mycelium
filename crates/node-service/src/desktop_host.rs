// SPDX-License-Identifier: AGPL-3.0-only
//! Development-only L11 host over the actual native service. No public relay,
//! fixture inference, or browser-asserted consent. Callbacks run off the GUI thread.
//! `/chat/api/send`, its events and Stop go through the serving root
//! ([`crate::router_port::Serving`]: the oracle-checked request gateway over the node service).
mod chat_boundary;
mod relay_chat;
use crate::{
    app_host::{FastModeDefault, FastModeProcess, HostLoop, PackagedHost, Publishing},
    router_port::{self, Serving, ServingActions},
    service::NodeService,
};
use api::{
    actions::Control,
    gateway::QualificationSource,
    persistent::PersistentBackend,
    registry::{AdmissionError, Pull, Subscription},
    requests::Service as _,
    schema::PublicError,
};
use desktop::{
    core::{Completion, Reply},
    lifecycle::{CleanupError, Poll},
    services::{ServiceRequest, Services},
};
use serde_json::{Value, json};
use std::{
    num::NonZeroUsize,
    path::Path,
    sync::{Arc, Mutex},
    thread::JoinHandle,
};

#[cfg(feature = "desktop-shell")]
pub fn run(
    checkpoint: &std::path::Path,
    tokenizer: &std::path::Path,
    evidence: &std::path::Path,
    private: &std::path::Path,
    capacity: NonZeroUsize,
) -> crate::loopback::Result<()> {
    run_host(checkpoint, tokenizer, evidence, private, capacity, false)
}
#[cfg(all(feature = "desktop-shell", debug_assertions))]
pub fn run_probe(
    checkpoint: &std::path::Path,
    tokenizer: &std::path::Path,
    evidence: &std::path::Path,
    private: &std::path::Path,
    capacity: NonZeroUsize,
) -> crate::loopback::Result<()> {
    run_host(checkpoint, tokenizer, evidence, private, capacity, true)
}
/// The composed adapter pair the window needs: the real node service behind the bounded
/// `desktop::services` queue, and the cleanup that proves its owned resources are gone.
pub type Composed = (
    Arc<desktop::services::ServiceCore<DesktopServices>>,
    desktop::services::ServiceCleanup<DesktopServices>,
);
/// Build the native driver (fresh `evidence` directory, persisted `private` state) and the
/// node service over it. Starts nothing until the first request. No window, no refusal of
/// release builds: the development shell (`run`, `run_probe`) calls this and adds its own. The
/// packaged executable does not: its chat is the owner run's route (`crate::packaged`, lane CHAT).
pub fn compose(
    checkpoint: &std::path::Path,
    tokenizer: &std::path::Path,
    evidence: &std::path::Path,
    private: &std::path::Path,
    capacity: NonZeroUsize,
) -> crate::loopback::Result<Composed> {
    std::fs::create_dir(evidence)?;
    let driver = crate::native::configured_driver(checkpoint, tokenizer, evidence, private)?;
    let node = NodeService::new(
        driver,
        crate::service::random_id().map_err(|e| e.code)?,
        crate::native::development_qualification()?,
    );
    let services = Arc::new(DesktopServices::new(node, capacity, private));
    Ok(desktop::services::adapt(services, capacity))
}
/// `status.app_version` (lane VL; the owner: "it should show the version being used somewhere"):
/// the running build's version, `desktop::bundle::VERSION` (what the bundle's Info.plist and
/// finalize carry; the workspace's Cargo version is a `-dev` placeholder and is not used), and the
/// source revision the install record holds when this executable is that version's installed copy
/// ([`desktop::install::running_source_revision`], read-only), else `null`. Read once per process.
fn app_version() -> Value {
    static VERSION: std::sync::OnceLock<Value> = std::sync::OnceLock::new();
    VERSION
        .get_or_init(|| {
            let revision = desktop::install::Roots::for_current_user()
                .ok()
                .zip(std::env::current_exe().ok())
                .and_then(|(roots, exe)| desktop::install::running_source_revision(&roots, &exe));
            json!({"version": desktop::bundle::VERSION, "source_revision": revision})
        })
        .clone()
}
#[cfg(feature = "desktop-shell")]
fn run_host(
    checkpoint: &std::path::Path,
    tokenizer: &std::path::Path,
    evidence: &std::path::Path,
    private: &std::path::Path,
    capacity: NonZeroUsize,
    probe: bool,
) -> crate::loopback::Result<()> {
    if !cfg!(debug_assertions) {
        return Err("development_shell_refused_in_release".into());
    }
    let (core, cleanup) = compose(checkpoint, tokenizer, evidence, private, capacity)?;
    #[cfg(debug_assertions)]
    if probe {
        let fixture: Value = serde_json::from_str(include_str!("../data/qwen2-oracle.json"))?;
        // The user's plain question, as typed into v0.1.4's chat; the chat edge wraps it.
        let script = probe_script(
            &serde_json::to_string(api::prompt_codec::ORACLE_USER_PROMPT)?,
            &serde_json::to_string(
                &fixture["tokens"]
                    .as_array()
                    .ok_or("oracle_tokens")?
                    .len()
                    .to_string(),
            )?,
            page_receipts_enabled(),
        );
        return desktop::shell::run_development_with_probe(
            core,
            cleanup,
            desktop::lifecycle::quit_allowance().map_err(|e| format!("cleanup budget: {e:?}"))?,
            script,
        )
        .map_err(Into::into);
    }
    #[cfg(not(debug_assertions))]
    if probe {
        return Err("development_probe_refused_in_release".into());
    }
    desktop::shell::run_development(
        core,
        cleanup,
        desktop::lifecycle::quit_allowance().map_err(|e| format!("cleanup budget: {e:?}"))?,
        false,
    )
    .map_err(Into::into)
}
/// Test-owned probe script with its placeholders substituted. `page_receipts` switches only
/// the per-fetch title receipts; the scripted chat flow is identical either way.
#[cfg(debug_assertions)]
pub fn probe_script(prompt_json: &str, tokens_json: &str, page_receipts: bool) -> String {
    include_str!("desktop_host/probe.js")
        .replace("__PROMPT_JSON__", prompt_json)
        .replace("__TOKENS_JSON__", tokens_json)
        .replace("__PAGE_RECEIPTS__", &page_receipts.to_string())
}
/// Page-fetch receipts are on unless the qualifier explicitly sets the variable to `0`.
#[cfg(all(feature = "desktop-shell", debug_assertions))]
fn page_receipts_enabled() -> bool {
    std::env::var("MYCELIUM_SLICE_PAGE_RECEIPTS").as_deref() != Ok("0")
}
fn err(code: &'static str) -> PublicError {
    PublicError::new(503, code)
}
struct Gate {
    closing: bool,
    /// Admitted callbacks (chat send and events, actions): at most `capacity` at once.
    tasks: Vec<JoinHandle<()>>,
    /// Stop, status and the chat state: never refused for the admitted ones, bounded apart.
    servable: Vec<JoinHandle<()>>,
    /// Status, the Stops and Quit: never refused for the chat state either, bounded apart.
    control: Vec<JoinHandle<()>>,
    failed: bool,
}
/// The callbacks the page can always reach (v0.1.4's gateway serves Stop, status and health
/// whatever its admitted requests hold, laneB18 FU1): the page's and the owner's status, the chat
/// state, and every Stop (`/api/serving/stop`, `/api/demo/stop`, `/api/stop`) and the web Quit.
fn always_servable(request: &ServiceRequest) -> bool {
    matches!(
        (request.method.as_str(), request.target.as_str()),
        ("GET", "/api/status")
            | (
                "POST",
                "/chat/api/state"
                    | "/api/serving/stop"
                    | "/api/demo/stop"
                    | "/api/stop"
                    | "/api/quit"
            )
    )
}
/// The always-servable callbacks the chat state cannot crowd out: its handler first reads the
/// owner run's route (`RunRoute::status`, which may wait on the run), and v0.1.4's page server has
/// no admitted cap at all (a `ThreadingHTTPServer`, app.py:1940-2025 @ f4e366e9).
fn control(request: &ServiceRequest) -> bool {
    always_servable(request) && request.target != "/chat/api/state"
}
struct Chat {
    id: String,
    /// The page's gateway subscription. None: the gateway's credential-theft policy answered it
    /// and no request exists.
    sub: Option<Subscription>,
    /// The node service may hold this (or an earlier) request's run: the next send rolls it over.
    holds_run: bool,
    tokens: Vec<String>,
    done: bool,
    /// The gateway terminal the page saw (v0.1.4's `terminal`, chat.py:363-369).
    terminal: Option<&'static str>,
    error: Option<String>,
}
/// The packaged action root: the desktop action backend over the packaged host, with the node
/// service (Stop routed through the serving root first) as the delegate for everything the
/// backend does not own.
type Actions = Control<PersistentBackend<PackagedHost, crate::packaged_network::PackagedActions>>;
type Source = Result<Arc<dyn QualificationSource>, PublicError>;
/// The node service's own development record as the gateway's qualification source.
fn development_source(node: &NodeService) -> Source {
    router_port::DevelopmentRecord::of(node).map(|r| Arc::new(r) as Arc<dyn QualificationSource>)
}
#[derive(Clone)]
pub struct DesktopServices {
    node: NodeService,
    /// The request gateway over the node service (its construction failure answers every send).
    serving: Result<Arc<Serving>, PublicError>,
    /// Every `POST /api/*`, in one action transaction at a time (v0.1.4's `app.guard`).
    actions: Arc<Actions>,
    fast_mode: FastModeProcess,
    /// The packaged host's own loops (its `_policy_loop`): Quit stops them as it fences
    /// admission and proves them ended and joined before the services verify.
    loops: Vec<Arc<HostLoop>>,
    /// The packaged host's listing custody, closed by the packaged Quit (not by these services).
    publishing: Option<Publishing>,
    /// The product evidence routes over the packaged host's owner seed (`crate::packaged_product`).
    product: Arc<dyn api::desktop::ProductRoutes>,
    /// The packaged host's network and authority, for the status (`crate::packaged_network`).
    network: Arc<crate::packaged_network::Network>,
    authority: Option<Arc<Mutex<crate::packaged_run::Authority>>>,
    /// The packaged host's owner run, for the status's `owner_run` (`crate::packaged_run`).
    run: crate::packaged_run::RunConfig,
    gate: Arc<Mutex<Gate>>,
    chat: Arc<Mutex<Option<Chat>>>,
    /// v0.1.4's `ChatService._rate` windows (`chat_edge`), on this host's own monotonic clock.
    rate: Arc<Mutex<crate::chat_edge::RateWindow>>,
    /// The shipped `ChatService` over the owner run's route gateway (lane PH3b S5): once a run is
    /// composed it answers every `/chat/*` route (lane S5F). The packaged executable always
    /// composes one (`packaged::compose`, lane CHAT); without one, only in the development shell
    /// (`mycelium-node desktop`) and the tests that build these services bare, the SLICE-1 native
    /// path above answers.
    chat_run: Option<api::chat_service::ChatService>,
    /// The handle the owner run's ports hold to cancel the route chats (lane S5F), over `chat_run`.
    run_chats: Option<api::chat_service::RunChats>,
    /// The node's state root these services were built with: `private`'s parent (`<state
    /// root>/v02-node` in the packaged composition, packaged.rs:76-89). The chat timing file must
    /// lie under it (lane S5F).
    node_root: Option<std::path::PathBuf>,
    started: std::time::Instant,
    capacity: NonZeroUsize,
}
/// The model the development shell's one-Mac native chat serves (its state document's
/// `Qwen2.5-0.5B-Instruct`): its prompts take that model's codec from the seam.
const NATIVE_CHAT_MODEL: &str = "Qwen/Qwen2.5-0.5B-Instruct";
/// `/chat/api/send` over the run's route (chat.py:300-340) for `client` (the page's `local`, or
/// the relay's `relay:<address>` key, `chat.client_key`): the body checks (`chat_edge`), Quit's
/// fence, then the service (rate limits per client and for the swarm, one chat at a time under
/// its own lock, chat.py:285-298, 319-321).
fn send_routed(
    gate: &Mutex<Gate>,
    chat: &api::chat_service::ChatService,
    client: &str,
    body: &Value,
) -> Result<Value, api::chat::ChatError> {
    let refused = |error: PublicError| api::chat::ChatError {
        error,
        retry_after: None,
    };
    let plan = crate::chat_edge::plan(body).map_err(refused)?;
    if gate
        .lock()
        .map_err(|_| refused(err("desktop_poisoned")))?
        .closing
    {
        return Err(refused(err("application_closed")));
    }
    chat.send(
        client,
        api::chat_service::Prompt {
            text: plan.prompt,
            max_new_tokens: plan.max_new_tokens,
            clamped: plan.clamped,
        },
    )
}
impl DesktopServices {
    /// `private` is the packaged private state root: the preference files, the recorded action
    /// failure and every later persisted action state live there.
    pub fn new(node: NodeService, capacity: NonZeroUsize, private: &Path) -> Self {
        let source = development_source(&node);
        Self::with_host(
            node.clone(),
            capacity,
            private,
            PackagedHost::new(node, private.to_owned()),
            source,
        )
    }
    /// [`DesktopServices::new`] with the gateway's qualification source given (a test scripts it).
    pub fn with_qualification(
        node: NodeService,
        capacity: NonZeroUsize,
        private: &Path,
        source: Arc<dyn QualificationSource>,
    ) -> Self {
        Self::with_host(
            node.clone(),
            capacity,
            private,
            PackagedHost::new(node, private.to_owned()),
            Ok(source),
        )
    }
    /// [`DesktopServices::new`] with the owner's lease renewal period read from `period`
    /// (`PackagedHost::with_renewal_period`): a test runs the same composition on a shorter budget.
    pub fn with_renewal_period(
        node: NodeService,
        capacity: NonZeroUsize,
        private: &Path,
        period: budgets::BudgetId,
    ) -> Self {
        let host = PackagedHost::with_renewal_period(node.clone(), private.to_owned(), period);
        let source = development_source(&node);
        Self::with_host(node, capacity, private, host, source)
    }
    /// [`DesktopServices::new`] with the packaged host's run-model acquisition over `source`
    /// (`PackagedHost::with_model_source`): `/api/model/download` and `/api/model/cancel`.
    pub fn with_model_source(
        node: NodeService,
        capacity: NonZeroUsize,
        private: &Path,
        source: crate::app_host::ModelSource,
    ) -> Self {
        let host = PackagedHost::with_model_source(node.clone(), private.to_owned(), source);
        let qualification = development_source(&node);
        Self::with_host(node, capacity, private, host, qualification)
    }
    /// The packaged executable's services: [`DesktopServices::with_model_source`] with the
    /// owner's listing custody over `publisher` (`PackagedHost::with_publisher`).
    pub fn packaged(
        node: NodeService,
        capacity: NonZeroUsize,
        private: &Path,
        source: crate::app_host::ModelSource,
        publisher: Arc<api::swarm::publisher::app::AppPublisher>,
    ) -> Self {
        Self::packaged_with_network(
            node,
            capacity,
            private,
            source,
            publisher,
            crate::packaged_network::NetworkConfig::default(),
        )
    }
    /// [`DesktopServices::packaged`] with the host's network over `network` (the relay's hooks and
    /// the join seams, `PackagedHost::with_network`).
    pub fn packaged_with_network(
        node: NodeService,
        capacity: NonZeroUsize,
        private: &Path,
        source: crate::app_host::ModelSource,
        publisher: Arc<api::swarm::publisher::app::AppPublisher>,
        network: crate::packaged_network::NetworkConfig,
    ) -> Self {
        Self::packaged_with_run(
            node,
            capacity,
            private,
            source,
            publisher,
            network,
            crate::packaged_run::RunConfig::default(),
        )
    }
    /// [`DesktopServices::packaged_with_network`] with the owner's run composed over `run`'s seams
    /// (`PackagedHost::with_run`, lane PH3b S3); `run` is also the status's handle to it.
    pub fn packaged_with_run(
        node: NodeService,
        capacity: NonZeroUsize,
        private: &Path,
        source: crate::app_host::ModelSource,
        publisher: Arc<api::swarm::publisher::app::AppPublisher>,
        network: crate::packaged_network::NetworkConfig,
        run: crate::packaged_run::RunConfig,
    ) -> Self {
        // Bundle admission's program is the only stage-worker program of this host's work
        // services (lane BND, REPORT-laneTEN D2): the owner's compiler, its own contributor and its
        // member work all launch the same admitted executable.
        let network = crate::packaged_network::NetworkConfig {
            native_program: run.native_program.clone(),
            ..network
        };
        let host = PackagedHost::with_model_source(node.clone(), private.to_owned(), source)
            .with_network(network)
            .with_publisher(publisher)
            .with_run(run.clone());
        let qualification = development_source(&node);
        // `PublishedStatus.start()` (published_status.py:26-30, app.py:630-632 @ f4e366e9): the
        // packaged app publishes a complete initial status before the window's first request, so
        // the page shows the join-consent document at once. A failure keeps its code on stderr;
        // the next action publishes again.
        if let Err(error) = api::actions::Backend::publish(&mut node.clone()) {
            eprintln!("status_publish_failed: {}", error.code);
        }
        let mut services = Self::with_host(node, capacity, private, host, qualification);
        services.run = run;
        services
    }
    /// The packaged host's network (its relay, closed by the packaged Quit).
    pub fn network(&self) -> Arc<crate::packaged_network::Network> {
        self.network.clone()
    }
    /// The listing custody these services' host keeps (none outside the packaged executable).
    pub fn publishing(&self) -> Option<Publishing> {
        self.publishing.clone()
    }
    /// Chat over the owner run's route gateway (lane PH3b S5, chat.py:211-500 @ f4e366e9): from now
    /// on `/chat/api/{state,send,events}` always go through the shipped `ChatService`, which answers
    /// `chat_not_ready` / `chat_warming_up` / `chat_switching_runs` itself while `run` does not serve
    /// (lane S5F); `timing` is the owner's per-chat timing file (v0.2's own evidence folder, never
    /// v0.1.4's).
    pub fn with_chat_run(
        mut self,
        run: Arc<dyn api::chat_service::RunRoute>,
        timing: Option<std::path::PathBuf>,
    ) -> Result<Self, PublicError> {
        let refused = PublicError::new(500, "chat_timing_path_refused");
        let timing = match (timing, &self.node_root) {
            (None, _) => None,
            (Some(path), Some(root)) => Some(api::chat_service::TimingFile::under(root, &path)?),
            (Some(_), None) => return Err(refused),
        };
        let chat = api::chat_service::ChatService::new(
            run,
            Arc::new(api::chat_service::MonotonicClock::default()),
            timing,
        )?;
        self.run_chats = Some(api::chat_service::RunChats::new(Arc::new(chat.clone())));
        // The relay's `/chat` reaches this same service (`_bind_public_routes`, app.py:1073-1077).
        self.network.bind_relay_chat(Some(relay_chat::handler(
            chat.clone(),
            self.gate.clone(),
            self.authority.clone(),
        )));
        self.chat_run = Some(chat);
        Ok(self)
    }
    /// The handle the owner run's ports hold (lane S5F): its teardown, ceiling, rollover and
    /// dead-member facts cancel the page's route chats with the shipped codes. `None` without
    /// [`DesktopServices::with_chat_run`].
    pub fn run_chats(&self) -> Option<api::chat_service::RunChats> {
        self.run_chats.clone()
    }
    /// The route chat service once a run is composed: then it answers EVERY `/chat/*` route, as the
    /// shipped app's only chat path does (`DemoApp.chat_route` app.py:1926 -> `chat.handle`
    /// chat.py:553-584 -> `ChatService`; not serving -> its own 503 codes, chat.py:311-318). `None`:
    /// no run is composed and the SLICE-1 native path answers. A host never has both paths, so a
    /// native chat and a route chat can never run at once (lane S5F, REVIEW-lanePH3B-S5 F1/F3).
    fn routed_chat(&self) -> Option<&api::chat_service::ChatService> {
        self.chat_run.as_ref()
    }
    /// `chat.chat_status(app)` (chat.py:521-529 @ f4e366e9, wired at app.py:1280 through
    /// `_demo_status(self,'chat')`): owner only (`None` -> `null` for any other role); `available`
    /// and `busy` from the route chat service's own `state()`, the one the chat page reads;
    /// `local_url` = the app's origin + `/chat` while the app serves its pages (`app.server is not
    /// None`: the window's custom protocol here, so until Quit begins); `share_url` = the relay's
    /// origin + `/chat` when that origin is https. run.js:128-137 shows "Open the chat on this Mac"
    /// and "Share with the room" only from this block. `None`: no route chat is composed (the
    /// development shell), and the node's document stays as it is.
    fn chat_status(&self, status: &Value) -> Option<Value> {
        let chat = self.routed_chat()?;
        if status["installation_role"] != "owner" {
            return Some(Value::Null);
        }
        let state = chat.state();
        let serves_pages = self.gate.lock().map(|gate| !gate.closing).unwrap_or(false);
        let share_url = self
            .network
            .origin()
            .filter(|origin| origin.starts_with("https://"))
            .map(|origin| format!("{}/chat", origin.trim_end_matches('/')));
        Some(json!({
            "available": state["available"],
            "busy": state["busy"],
            "local_url": serves_pages.then(|| format!("{}/chat", desktop::channel::ORIGIN)),
            "share_url": share_url,
        }))
    }
    /// `/chat/api/send` over the run's route (chat.py:300-340) for the owner's own page, the one
    /// local client (`chat.client_key(..., via_relay=False)` is `local`): [`send_routed`].
    fn route_send(
        &self,
        chat: &api::chat_service::ChatService,
        body: &Value,
    ) -> Result<Value, api::chat::ChatError> {
        send_routed(&self.gate, chat, "local", body)
    }
    fn with_host(
        node: NodeService,
        capacity: NonZeroUsize,
        private: &Path,
        host: PackagedHost,
        source: Source,
    ) -> Self {
        let serving = source
            .and_then(|source| Serving::new(node.clone(), source))
            .map(Arc::new);
        let fast_mode = host.fast_mode_process();
        let loops = host.loops();
        let publishing = host.publishing();
        let product =
            crate::packaged_product::routes(host.authority_handle(), node.clone(), serving.clone());
        let (network, authority) = (host.network(), host.authority_handle());
        let delegate = crate::packaged_network::PackagedActions::new(
            ServingActions::new(node.clone(), serving.clone()),
            network.clone(),
            host.authority_result(),
        );
        let backend = PersistentBackend::new(private.to_owned(), host, delegate);
        // `DemoApp._pending_windows`: the relay's Start materializes the backend's own list.
        network.share_pending(backend.pending_windows());
        Self {
            actions: Arc::new(Control::new(backend)),
            network,
            authority,
            run: crate::packaged_run::RunConfig::default(),
            serving,
            fast_mode,
            loops,
            publishing,
            product,
            node,
            gate: Arc::new(Mutex::new(Gate {
                closing: false,
                tasks: Vec::new(),
                servable: Vec::new(),
                control: Vec::new(),
                failed: false,
            })),
            chat: Arc::new(Mutex::new(None)),
            rate: Arc::default(),
            chat_run: None,
            run_chats: None,
            node_root: private.parent().map(Path::to_path_buf),
            started: std::time::Instant::now(),
            capacity,
        }
    }
    /// What the serving root's router port handed to the driver (observation only).
    pub fn ledger(&self) -> router_port::Ledger {
        self.serving
            .as_ref()
            .map(|s| s.ledger())
            .unwrap_or_default()
    }
    /// The next run's fast-mode default as the packaged host last refreshed it.
    pub fn fast_mode_default(&self) -> FastModeDefault {
        self.fast_mode.read()
    }
    fn json(value: Value) -> Result<Reply, PublicError> {
        Ok(Reply {
            status: 200,
            content_type: "application/json",
            body: serde_json::to_vec(&value).map_err(|_| err("native_json_failed"))?,
        })
    }
    fn deliver(complete: Completion, result: Result<Reply, PublicError>) {
        complete(result.unwrap_or_else(|e| Reply::error(e.status, e.code)));
    }
    fn complete_error(complete: Completion, error: PublicError) {
        Self::deliver(complete, Err(error));
    }
    fn work(&self, request: ServiceRequest, complete: Completion) {
        crate::phase::mark(
            "page_request",
            &format!("{}:{}", request.method, request.target),
        );
        if request.target.starts_with("/chat/") {
            chat_boundary::handle(self, request, complete);
            return;
        }
        Self::deliver(complete, self.dispatch(request));
    }
    fn dispatch(&self, request: ServiceRequest) -> Result<Reply, PublicError> {
        let body: Value = if request.body.is_empty() {
            json!({})
        } else {
            serde_json::from_slice(&request.body)
                .map_err(|_| PublicError::new(400, "invalid_json"))?
        };
        if let Some(reply) = crate::packaged_product::dispatch(&*self.product, &request, &body) {
            return reply;
        }
        match (request.method.as_str(), request.target.as_str()) {
            // The invite card's `<img src="/api/pair/window/qr/{window_id}.svg">` (run.js `renderQr`).
            // Nothing served this path before -- the only source hit was the auth allowlist in
            // `api::desktop` -- so every invite QR in the app was a broken image.
            ("GET", target) if target.starts_with("/api/pair/window/qr/") => {
                let id = target
                    .trim_start_matches("/api/pair/window/qr/")
                    .trim_end_matches(".svg");
                let windows = self
                    .authority
                    .as_ref()
                    .ok_or_else(|| err("authority_state_invalid"))?
                    .lock()
                    .map_err(|_| err("desktop_poisoned"))?
                    .windows
                    .clone();
                let svg = windows
                    .link_svg(&serde_json::json!(id))
                    .ok_or_else(|| err("pairing_window_unknown"))?;
                Ok(Reply {
                    status: 200,
                    content_type: "image/svg+xml",
                    body: svg,
                })
            }
            ("GET", "/api/status") => {
                // The node's published document with the owner's listing custody (v0.1.4 `swarm`).
                let mut status = self.node.published_status()?;
                // Only into a published object, never indexed into a panic (REVIEW-lanePH2 F8).
                status
                    .as_object_mut()
                    .ok_or_else(|| err("status_unavailable"))?
                    .insert(
                        "swarm".into(),
                        crate::app_host::swarm_status(self.publishing.as_ref()),
                    );
                // v0.1.4's network window, role, consent and join flow (`crate::packaged_network`).
                crate::packaged_network::status_overlay(
                    &mut status,
                    self.authority.as_deref(),
                    &self.network,
                );
                // v0.1.4's `owner_run` (`demo_app.owner_status`, `crate::packaged_run`).
                crate::packaged_run::status_overlay(&mut status, &self.run);
                // v0.1.4's `demo_target` member card (PH3b S2).
                crate::packaged_network::member_status(
                    &mut status,
                    self.authority.as_deref(),
                    &self.network,
                );
                // v0.1.4's `chat` block (`chat.chat_status`, lane CHATS item 1).
                if let Some(chat) = self.chat_status(&status) {
                    status["chat"] = chat;
                }
                // Lane VL: the running app's version for the page's footer (additive, v0.2 only).
                status["app_version"] = app_version();
                Self::json(status)
            }
            ("POST", "/chat/api/state") if body == json!({}) => {
                let closed = self
                    .gate
                    .lock()
                    .map_err(|_| err("desktop_poisoned"))?
                    .closing;
                let busy = self
                    .chat
                    .try_lock()
                    .map(|c| c.as_ref().is_some_and(|c| !c.done))
                    .unwrap_or(true);
                Self::json(
                    json!({"available":!closed,"busy":busy,"model_id":"Qwen2.5-0.5B-Instruct","served_by":[],"switching_runs":false,"rejoining":false,"warming_up":false,"limits":crate::chat_edge::limits_json()}),
                )
            }
            ("POST", "/chat/api/send") => {
                // v0.1.4's `ChatService.send` decides body, prompt and token limits first.
                let plan = crate::chat_edge::plan(&body)?;
                let tokens = plan.max_new_tokens;
                let policy = crate::chat_edge::policy_response(&plan.prompt);
                // Use the native request contract's validation, not an alternate
                // browser-only generation policy.
                // The one prompt-codec seam (lane CHATFIX): this path serves Qwen2.5-0.5B.
                let prompt = api::prompt_codec::PromptCodec::for_model(NATIVE_CHAT_MODEL)?
                    .chat(&plan.prompt)
                    .into_string();
                let document = json!({"protocol":"mycelium.request_gateway.v1","prompt":prompt,"max_new_tokens":tokens,"qualification":self.node.qualification()?});
                protocol::request::submission(&document).map_err(|e| PublicError::new(400, e.0))?;
                // The gate is only Quit's fence here, never held across the send's admission
                // (rollover, `Serving::submit`'s wait for the start, up to WorkerStartup): every
                // other callback (status, Stop, Quit's begin) takes it on the window's thread.
                // `chat` stays the single-admission fence: one send at a time, the rest 429.
                let closing = || -> Result<(), PublicError> {
                    if self
                        .gate
                        .lock()
                        .map_err(|_| err("desktop_poisoned"))?
                        .closing
                    {
                        return Err(err("application_closed"));
                    }
                    Ok(())
                };
                closing()?;
                let mut chat = self
                    .chat
                    .try_lock()
                    .map_err(|_| PublicError::new(429, "chat_busy"))?;
                if chat.as_ref().is_some_and(|c| !c.done) {
                    return Err(PublicError::new(429, "chat_busy"));
                }
                // Quit may have begun meanwhile: nothing is rolled over or admitted after it (an
                // admission racing it is refused by the node service's own shutdown fence).
                closing()?;
                // The owner's own page is the one local client (`chat.client_key`).
                self.rate
                    .lock()
                    .map_err(|_| err("desktop_poisoned"))?
                    .admit("local", self.started.elapsed().as_secs_f64())?;
                let reply = |request_id: &str| {
                    Self::json(
                        json!({"request_id":request_id,"max_new_tokens":tokens,"clamped":plan.clamped,"served_by":[]}),
                    )
                };
                if let Some(refusal) = policy {
                    // The gateway emits token 0 = the refusal and completes, before any model work.
                    let id = crate::service::random_id()?;
                    let holds_run = chat.as_mut().is_some_and(|previous| {
                        self.release(previous);
                        previous.holds_run
                    });
                    *chat = Some(Chat {
                        id: id.clone(),
                        sub: None,
                        holds_run,
                        tokens: vec![refusal.to_owned()],
                        done: true,
                        terminal: Some("completed"),
                        error: None,
                    });
                    return reply(&id);
                }
                let serving = self.serving.as_ref().map_err(|e| *e)?;
                if let Some(previous) = chat.as_mut() {
                    if previous.holds_run {
                        self.node.rollover()?;
                        previous.holds_run = false;
                    }
                    self.release(previous);
                }
                let admitted = serving.submit(&prompt, tokens)?;
                *chat = Some(Chat {
                    id: admitted.request_id.clone(),
                    sub: Some(admitted.subscription),
                    holds_run: admitted.may_hold_run,
                    tokens: Vec::new(),
                    done: false,
                    terminal: None,
                    error: None,
                });
                reply(&admitted.request_id)
            }
            // Lane XB patch 2 (XB-05, D1/D2): the window's network choices (this Mac's
            // portmapping preference; the owner's same/different-networks choice for its next run).
            ("POST", crate::packaged_work::reach::ACTION) => {
                let body = body
                    .as_object()
                    .ok_or(PublicError::new(400, "invalid_request"))?;
                // Lane XB patch 3 (REVIEW XB2-06): one action transaction, like every other
                // control action: the role and run checks, the load and the save happen as one
                // piece; no other change (nor Start) interleaves.
                let value = self
                    .actions
                    .exclusive(|| {
                        let owner = self.authority.as_ref().is_some_and(|authority| {
                            authority
                                .lock()
                                .is_ok_and(|a| !a.target && a.owner_authority().is_some())
                        });
                        self.network.reach_action(body, owner)
                    })?
                    .map_err(|code| PublicError::new(400, code))?;
                Self::json(value)
            }
            ("POST", path) if path.starts_with("/api/") => {
                use api::desktop::Service as _;
                let value = self.actions.action(
                    path,
                    body.as_object()
                        .ok_or(PublicError::new(400, "invalid_request"))?,
                )?;
                // The run's Stop tears it down with `chat.cancel_all('chat_cancelled')`
                // (demo_chain.py:1322-1335 @ f4e366e9); repeated by the run's own hook, a no-op.
                if path == "/api/demo/stop"
                    && let Some(chat) = &self.chat_run
                {
                    chat.cancel_all(api::chat_service::Teardown::Stopped.code());
                }
                Self::json(value)
            }
            _ => Err(PublicError::new(404, "not_found")),
        }
    }
    fn events(&self, request: ServiceRequest, complete: Completion) {
        let body: Value = match serde_json::from_slice(&request.body) {
            Ok(v) => v,
            Err(_) => {
                Self::complete_error(complete, PublicError::new(400, "invalid_json"));
                return;
            }
        };
        if request.method != "POST"
            || body.as_object().is_none_or(|b| b.len() != 2)
            || !body["request_id"].is_string()
            || !body["after"].is_u64()
        {
            Self::complete_error(complete, PublicError::new(400, "invalid_request"));
            return;
        }
        let Ok(mut chat) = self.chat.lock() else {
            Self::complete_error(complete, err("desktop_chat_poisoned"));
            return;
        };
        let Some(chat) = chat
            .as_mut()
            .filter(|c| Some(c.id.as_str()) == body["request_id"].as_str())
        else {
            Self::complete_error(complete, PublicError::new(404, "chat_request_unknown"));
            return;
        };
        let Some(after) = body["after"]
            .as_u64()
            .and_then(|v| usize::try_from(v).ok())
            .filter(|v| *v <= chat.tokens.len())
        else {
            Self::complete_error(complete, PublicError::new(400, "invalid_request"));
            return;
        };
        crate::phase::mark(
            "events_wait",
            &format!(
                "after={after}:tokens={}:done={}",
                chat.tokens.len(),
                chat.done
            ),
        );
        let mut sequence = None;
        if after == chat.tokens.len() && !chat.done {
            sequence = self.pull(chat);
        }
        let reply = Self::json(
            json!({"tokens":chat.tokens[after..],"next":chat.tokens.len(),"done":chat.done,"error":chat.error,"terminal":chat.terminal,"seconds":Value::Null}),
        );
        let delivered = reply.is_ok();
        crate::phase::mark(
            "events_reply",
            &format!("sequence={sequence:?}:delivered={delivered}"),
        );
        // Completion hands the entire frame to the native channel. Keep the
        // subscription lock through ACK so another page cannot race delivery.
        Self::deliver(complete, reply);
        crate::phase::mark("events_delivered", "");
        if delivered
            && let Some(sequence) = sequence
            && let (Ok(serving), Some(sub)) = (&self.serving, chat.sub.as_mut())
            && let Err(AdmissionError(code)) = serving.ack(sub, sequence)
        {
            chat.done = true;
            chat.error = Some(code.to_owned());
        }
    }
    /// The next page-visible gateway event of `chat`, waiting at most one gateway SSE poll step
    /// (v0.1.4's events answer at once with what the request has, chat.py:460-474). `accepted`
    /// is acknowledged here; a token or the terminal is acknowledged after its reply is
    /// delivered (its sequence is returned).
    fn pull(&self, chat: &mut Chat) -> Option<i64> {
        let Some(sub) = chat.sub.as_mut() else {
            // A policy answer carries all of its tokens and is done from the start.
            chat.done = true;
            return None;
        };
        let serving = match &self.serving {
            Ok(serving) => serving,
            Err(e) => {
                chat.done = true;
                chat.error = Some(e.code.to_owned());
                return None;
            }
        };
        let deadline = std::time::Instant::now()
            + crate::service::duration(
                budgets::BudgetId::PyMyceliumDeadlinesGatewaySseDisconnectPollSecondsL240,
            );
        loop {
            let wait = deadline.saturating_duration_since(std::time::Instant::now());
            let event = match serving.next(sub, wait) {
                Ok(Pull::Event(event)) => event,
                Ok(Pull::Pending) => return None,
                Ok(Pull::End) => {
                    chat.done = true;
                    return None;
                }
                Err(AdmissionError(code)) => {
                    chat.done = true;
                    chat.error = Some(code.to_owned());
                    return None;
                }
            };
            let terminal = match event.kind.as_str() {
                "token" => {
                    chat.tokens.push(
                        event.payload["text"]
                            .as_str()
                            .unwrap_or_default()
                            .to_owned(),
                    );
                    return Some(event.sequence);
                }
                "completed" => "completed",
                "cancelled" => "cancelled",
                // `/api/demo/stop` cancels the node service's job directly, which ends the
                // request `failed` with `request_cancelled`; v0.1.4's Stop cancels through the
                // gateway (`cancel_all`, demo_chain.py:1322-1335), a `cancelled` terminal.
                "failed" if event.payload["code"] == "request_cancelled" => "cancelled",
                "failed" => "failed",
                // `accepted` (and any lifecycle event) carries nothing for the page.
                _ => {
                    if let Err(AdmissionError(code)) = serving.ack(sub, event.sequence) {
                        chat.done = true;
                        chat.error = Some(code.to_owned());
                        return None;
                    }
                    continue;
                }
            };
            chat.done = true;
            chat.terminal = Some(terminal);
            if terminal == "cancelled" {
                // v0.1.4: `error = cancel_reason or 'chat_cancelled'` (chat.py:368-369); every
                // cancel on this path is a Stop, whose reason is `chat_cancelled`.
                chat.error = Some(api::chat_service::Teardown::Stopped.code().to_owned());
            }
            if terminal == "failed" {
                // v0.1.4: a failed terminal shows its code (chat.py:366-367).
                chat.error = Some(
                    event.payload["code"]
                        .as_str()
                        .unwrap_or("chat_generation_failed")
                        .to_owned(),
                );
            }
            return Some(event.sequence);
        }
    }
    /// Release a replaced chat's gateway subscription (closing never cancels inference).
    fn release(&self, chat: &mut Chat) {
        if let (Ok(serving), Some(mut sub)) = (&self.serving, chat.sub.take()) {
            serving.close(&mut sub);
        }
    }
}
impl Services for DesktopServices {
    fn submit(&self, request: ServiceRequest, complete: Completion) {
        let Ok(mut gate) = self.gate.lock() else {
            Self::complete_error(complete, err("desktop_poisoned"));
            return;
        };
        if gate.closing {
            Self::complete_error(complete, err("application_closed"));
            return;
        }
        let Gate {
            tasks,
            servable,
            control: controls,
            failed,
            ..
        } = &mut *gate;
        for slots in [&mut *tasks, &mut *servable, &mut *controls] {
            for task in std::mem::take(slots) {
                if task.is_finished() {
                    if task.join().is_err() {
                        *failed = true;
                    }
                } else {
                    slots.push(task);
                }
            }
        }
        if *failed {
            Self::complete_error(complete, err("desktop_callback_panicked"));
            return;
        }
        // Stop and status never wait for a slot the admitted requests hold; they have their own.
        let slots = if control(&request) {
            controls
        } else if always_servable(&request) {
            servable
        } else {
            tasks
        };
        if slots.len() >= self.capacity.get() {
            crate::phase::mark("callback_capacity", "refused");
            Self::complete_error(complete, err("desktop_callback_capacity"));
            return;
        }
        let callback = Arc::new(Mutex::new(Some(complete)));
        let owned = callback.clone();
        let this = self.clone();
        match std::thread::Builder::new()
            .name("slice-desktop-request".into())
            .spawn(move || {
                if let Ok(mut callback) = owned.lock()
                    && let Some(callback) = callback.take()
                {
                    this.work(request, callback);
                }
            }) {
            Ok(task) => slots.push(task),
            Err(_) => {
                if let Ok(mut callback) = callback.lock()
                    && let Some(callback) = callback.take()
                {
                    Self::complete_error(callback, err("desktop_thread_start_failed"));
                }
            }
        }
    }
    fn begin_shutdown(&self, _: std::time::Instant) -> Result<(), CleanupError> {
        self.gate.lock().map_err(|_| CleanupError::Begin)?.closing = true;
        for host_loop in &self.loops {
            host_loop.stop();
        }
        if let Ok(serving) = &self.serving {
            serving.begin_shutdown();
        }
        self.node
            .request_shutdown()
            .map_err(|_| CleanupError::Begin)
    }
    fn poll_shutdown(&self) -> Result<Poll, CleanupError> {
        let mut gate = self.gate.lock().map_err(|_| CleanupError::Poll)?;
        if !gate.closing {
            return Err(CleanupError::Poll);
        }
        if gate
            .tasks
            .iter()
            .chain(&gate.servable)
            .chain(&gate.control)
            .any(|t| !t.is_finished())
        {
            return Ok(Poll::Pending);
        }
        let ended: Vec<_> = std::mem::take(&mut gate.tasks)
            .into_iter()
            .chain(std::mem::take(&mut gate.servable))
            .chain(std::mem::take(&mut gate.control))
            .collect();
        for task in ended {
            if task.join().is_err() {
                gate.failed = true;
            }
        }
        if gate.failed {
            return Err(CleanupError::Poll);
        }
        // The host's loops end after their pass in progress (one observation at most).
        for host_loop in &self.loops {
            match host_loop.poll_join() {
                Ok(true) => {}
                Ok(false) => return Ok(Poll::Pending),
                Err(code) => {
                    eprintln!("desktop shutdown: {code}");
                    gate.failed = true;
                    return Err(CleanupError::Poll);
                }
            }
        }
        // The live request's gateway worker ends after its driver job (or its refusal).
        if self.serving.as_ref().is_ok_and(|s| !s.settled()) {
            return Ok(Poll::Pending);
        }
        Ok(
            if self.node.poll_shutdown().map_err(|_| CleanupError::Poll)? {
                Poll::Verified
            } else {
                Poll::Pending
            },
        )
    }
}
