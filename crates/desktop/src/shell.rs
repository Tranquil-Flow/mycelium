// SPDX-License-Identifier: AGPL-3.0-only
//! Native custom-protocol adapter. No HTTP listener or invoke API.
use crate::{
    channel::{self, Channel, Request},
    lifecycle::{Cleanup, CleanupError, QuitSource, Readiness, Shutdown, State},
};
use budgets::{BudgetId, Entries, Span};
use std::{
    sync::{Arc, Condvar, Mutex},
    time::{Duration, Instant},
};
use tauri::{
    Manager, RunEvent, WebviewUrl, WebviewWindowBuilder,
    menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem, SubmenuBuilder},
};

pub use crate::core::{Core, Reply};
/// Size the packaged window opens at, in logical points.
///
/// The page is one long single-column form (the create form alone runs past 1 600 pt, and the
/// consent/role sections take the document to roughly 8 800 pt at 400 pt wide). Without an
/// explicit size the window fell back to the framework default of ~400×300, so a fresh install
/// opened showing only the masthead and the first heading, with no hint that the form continued
/// below. 1040×760 shows the masthead plus the first two fields on a 1512×982 display and still
/// fits a 13-inch MacBook screen.
pub const WINDOW_WIDTH: f64 = 1_040.0;
pub const WINDOW_HEIGHT: f64 = 760.0;
/// Below this the form is unusable (a 400 pt column puts the create form past 8 800 pt of scroll),
/// so the user may not drag the window smaller than this.
pub const MIN_WINDOW_WIDTH: f64 = 720.0;
pub const MIN_WINDOW_HEIGHT: f64 = 560.0;
#[cfg(all(feature = "dev-shell", debug_assertions))]
const DEV_TITLE: &str = "Mycelium L11 development — core not integrated";
fn asset(path: &str) -> Option<Reply> {
    let asset = crate::page::window(path)?;
    Some(Reply {
        status: 200,
        content_type: asset.content_type,
        body: asset.bytes.to_vec(),
    })
}
fn response(reply: Reply) -> tauri::http::Response<Vec<u8>> {
    use tauri::http::{
        Response, StatusCode,
        header::{CACHE_CONTROL, CONTENT_SECURITY_POLICY, CONTENT_TYPE, HeaderValue},
    };
    let mut response = Response::new(reply.body);
    *response.status_mut() =
        StatusCode::from_u16(reply.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    let headers = response.headers_mut();
    headers.insert(CONTENT_TYPE, HeaderValue::from_static(reply.content_type));
    headers.insert(
        CONTENT_SECURITY_POLICY,
        HeaderValue::from_static(channel::CSP),
    );
    headers.insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
    headers.insert("referrer-policy", HeaderValue::from_static("no-referrer"));
    headers.insert(
        "x-content-type-options",
        HeaderValue::from_static("nosniff"),
    );
    response
}
fn elapsed(start: Instant) -> Result<Span, &'static str> {
    Ok(Span::from_millis(
        u64::try_from(start.elapsed().as_millis()).map_err(|_| "monotonic_overflow")?,
    ))
}
/// Must run on the process main thread. Core and native resources must already
/// be admitted by the production composition root. This lane supplies only an
/// explicitly named dev executable; it cannot be mistaken for a release binary.
/// Production entry seam: verify OS and the complete trusted payload manifest
/// BEFORE constructing a core that can load native code. The factory is L6/L8/L10
/// owned. No factory or release executable is provided by this preparatory lane.
pub fn run_packaged<C, F>(
    manifest: &crate::package::Manifest,
    factory: F,
    cleanup_budget: Span,
) -> Result<(), String>
where
    C: Cleanup + Send + 'static,
    F: FnOnce(crate::package::VerifiedBundle) -> Result<(Arc<dyn Core>, C), String>,
{
    if cleanup_budget == Span::ZERO {
        return Err("cleanup:InvalidBudget".into());
    }
    crate::platform::probe().map_err(|e| format!("platform:{e:?}"))?;
    let executable = std::env::current_exe().map_err(|e| format!("executable:{:?}", e.kind()))?;

    // Platform was independently probed above; no synthetic OS facts are passed
    // into production admission. The verified token is built by the package seam.
    let bundle = crate::package::admit_native_payload(&executable, manifest)
        .map_err(|e| format!("payload:{e:?}"))?;

    let (core, cleanup) = factory(bundle)?;
    run(core, cleanup, cleanup_budget, Probe::Disabled, "Mycelium")
}
/// Test-only twin of `run_packaged` (non-default `qualification-probe` feature, compiled into
/// no shipped bundle): the same OS and payload admission, then the caller's trusted
/// initialization script in the same unchanged window. A scratch qualification bundle built
/// with the feature can drive the frozen chat page; the default bundle has neither this
/// function nor any probe hook (`probe_hooks` is a no-op module without it).
#[cfg(feature = "qualification-probe")]
pub fn run_packaged_with_probe<C, F>(
    manifest: &crate::package::Manifest,
    factory: F,
    cleanup_budget: Span,
    script: String,
) -> Result<(), String>
where
    C: Cleanup + Send + 'static,
    F: FnOnce(crate::package::VerifiedBundle) -> Result<(Arc<dyn Core>, C), String>,
{
    if cleanup_budget == Span::ZERO {
        return Err("cleanup:InvalidBudget".into());
    }
    crate::platform::probe().map_err(|e| format!("platform:{e:?}"))?;
    let executable = std::env::current_exe().map_err(|e| format!("executable:{:?}", e.kind()))?;
    let bundle = crate::package::admit_native_payload(&executable, manifest)
        .map_err(|e| format!("payload:{e:?}"))?;
    let (core, cleanup) = factory(bundle)?;
    run(
        core,
        cleanup,
        cleanup_budget,
        Probe::Script(script),
        "Mycelium",
    )
}
// Script storage and construction do not exist in release builds, even if the
// developer accidentally enables dev-shell. Packaged entry always disables it; only the
// non-default `qualification-probe` feature adds a packaged probe entry.
enum Probe {
    Disabled,
    #[cfg(any(
        feature = "qualification-probe",
        all(feature = "dev-shell", debug_assertions)
    ))]
    Script(String),
}
impl Probe {
    fn enabled(&self) -> bool {
        !matches!(self, Self::Disabled)
    }
    fn script(&self) -> &str {
        match self {
            Self::Disabled => "",
            #[cfg(any(
                feature = "qualification-probe",
                all(feature = "dev-shell", debug_assertions)
            ))]
            Self::Script(script) => script,
        }
    }
}
/// Trusted development-composition script, never a webview-supplied value.
/// The caller owns reading its script; no environment or HTTP override exists.
/// Security, native URL/label, CSP, navigation and cleanup are unchanged.
/// This API and script storage are compiled out of release builds.
#[cfg(all(feature = "dev-shell", debug_assertions))]
pub fn run_development_with_probe<C: Cleanup + Send + 'static>(
    core: Arc<dyn Core>,
    cleanup: C,
    cleanup_budget: Span,
    script: String,
) -> Result<(), String> {
    run(
        core,
        cleanup,
        cleanup_budget,
        Probe::Script(script),
        DEV_TITLE,
    )
}
#[cfg(feature = "dev-shell")]
pub fn run_development<C: Cleanup + Send + 'static>(
    core: Arc<dyn Core>,
    cleanup: C,
    cleanup_budget: Span,
    probe: bool,
) -> Result<(), String> {
    #[cfg(debug_assertions)]
    {
        let probe = if probe {
            Probe::Script(include_str!("probe.js").into())
        } else {
            Probe::Disabled
        };
        run(core, cleanup, cleanup_budget, probe, DEV_TITLE)
    }
    #[cfg(not(debug_assertions))]
    {
        drop((core, cleanup, cleanup_budget, probe));
        Err("development_shell_refused_in_release".into())
    }
}
fn run<C: Cleanup + Send + 'static>(
    core: Arc<dyn Core>,
    mut cleanup: C,
    cleanup_budget: Span,
    probe: Probe,
    title: &str,
) -> Result<(), String> {
    let window_title = title.to_owned();
    let dev_probe = probe.enabled();
    crate::platform::probe().map_err(|e| format!("platform:{e:?}"))?;
    let start = Instant::now();
    cleanup
        .bind_clock(start)
        .map_err(|error| format!("cleanup_clock:{error:?}"))?;
    let native_quit = crate::native_quit::NativeQuit::new().map_err(String::from)?;
    let shutdown = Arc::new(Mutex::new(
        Shutdown::new(cleanup, cleanup_budget).map_err(|e| format!("cleanup:{e:?}"))?,
    ));
    let channel = Arc::new(Mutex::new(Channel::default()));
    let protocol_channel = channel.clone();
    let quit_state = shutdown.clone();
    let quit_channel = channel.clone();
    let app = tauri::Builder::default()
        .register_asynchronous_uri_scheme_protocol("mycelium", move |context, request, responder| {
            let native_document = if request.method() == "POST" {
                context.app_handle().get_webview_window("main").and_then(|window| match window.url() {
                    Ok(url) => Some(url),
                    Err(_) => { eprintln!("native_document_url_unavailable"); None },
                })
            } else { None };
            let request = Request { window: context.webview_label().into(), method: request.method().to_string(), uri: request.uri().to_string(),
                headers: request.headers().iter().map(|(key,value)| (key.to_string(), value.to_str().unwrap_or("invalid_header").to_owned())).collect(), body: request.into_body() };
            let accepted = match protocol_channel.lock() {
                Ok(channel) => match native_document.as_ref().map_or_else(|| channel.authorize(&request), |url| channel.authorize_native_document(&request, url.as_str())) {
                    Ok(()) => true,
                    Err(error) => { probe_hooks::channel_refusal(dev_probe, &request, &error); false }
                },
                Err(_) => { eprintln!("channel_lock_poisoned"); false },
            };
            if !accepted {
                responder.respond(response(Reply { status: 403, content_type: "application/json", body: b"{\"error\":\"window_channel_rejected\"}".to_vec() }));
                return;
            }
            let path = request.uri.strip_prefix(channel::ORIGIN).unwrap_or("");
            if let Some(reply) = probe_hooks::attack_target(dev_probe, path) {
                responder.respond(reply);
                return;
            }
            if request.method == "GET"
                && let Some(reply) = probe_hooks::native_action(dev_probe, context.app_handle(), path)
            {
                responder.respond(reply);
                return;
            }
            if request.method == "GET" && let Some(reply) = asset(path) { responder.respond(response(reply)); return; }
            if path == "/api/quit" && request.method == "POST" {
                let empty = serde_json::from_slice::<serde_json::Value>(&request.body).is_ok_and(|value| value.as_object().is_some_and(|object| object.is_empty()));
                if !empty {
                    responder.respond(response(Reply { status: 400, content_type: "application/json", body: b"{\"error\":\"invalid_request\"}".to_vec() }));
                    return;
                }
                request_quit(&quit_state, &quit_channel, QuitSource::WebUi, start);
                // app.py:1681-1684 @ the frozen oracle: acknowledgment of stop,
                // never a claim that asynchronous cleanup has already succeeded.
                responder.respond(response(Reply { status: 200, content_type: "application/json", body: b"{\"stopping\":true,\"identity_retained\":true,\"evidence_retained\":true}".to_vec() }));
                return;
            }
            core.submit(request, Box::new(move |reply| responder.respond(response(reply))));
        })
        .setup(move |app| {
            let quit = MenuItemBuilder::with_id("quit", "Quit Mycelium").accelerator("CmdOrCtrl+Q").build(app)?;
            // The standard Edit menu is what makes Cmd+C/V/X/A/Z reach the web view at all: macOS
            // routes those key equivalents through the menu's selectors, so a menu without them
            // leaves text fields unable to copy or paste.
            let edit = SubmenuBuilder::new(app, "Edit")
                .item(&PredefinedMenuItem::undo(app, None)?)
                .item(&PredefinedMenuItem::redo(app, None)?)
                .separator()
                .item(&PredefinedMenuItem::cut(app, None)?)
                .item(&PredefinedMenuItem::copy(app, None)?)
                .item(&PredefinedMenuItem::paste(app, None)?)
                .item(&PredefinedMenuItem::select_all(app, None)?)
                .build()?;
            let menu = MenuBuilder::new(app)
                .item(&SubmenuBuilder::new(app, "Mycelium").item(&quit).build()?)
                .item(&edit)
                .build()?;
            app.set_menu(menu)?;
            let navigation_app = app.handle().clone();
            let popup_app = app.handle().clone();
            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::External("mycelium://localhost/".parse()?))
                .title(window_title.as_str())
                .inner_size(WINDOW_WIDTH, WINDOW_HEIGHT)
                .min_inner_size(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
                .visible(false).focused(!dev_probe).incognito(true).devtools(false)
                .initialization_script(probe.script())
                .on_navigation(move |url| {
                    let allowed = handle_link(url, dev_probe);
                    if !allowed {
                        probe_hooks::navigation_denied(dev_probe, &navigation_app, url);
                    }
                    allowed
                })
                .on_new_window(move |url, _| {
                    if handle_link(&url, dev_probe) {
                        // The unchanged /chat link requests _blank. Reuse the
                        // pinned native window, never create a second capability.
                        if let Some(window) = popup_app.get_webview_window("main")
                            && let Err(error) = window.navigate(url) {
                            eprintln!("pinned_link_navigation_failed:{error}");
                        }
                    }
                    probe_hooks::popup_blocked(dev_probe, &popup_app);
                    tauri::webview::NewWindowResponse::Deny
                })
                .on_page_load(|_, payload| {
                    if matches!(payload.event(), tauri::webview::PageLoadEvent::Finished) {
                        println!("L11_PAGE_LOADED");
                    }
                })
                .on_document_title_changed(move |window, title| {
                    probe_hooks::title_changed(dev_probe, window.app_handle(), &title);
                })
                .build()?;
            crate::webview_policy::keep_page_scheduled(&window, dev_probe)?;

            Ok(())
        })
        .build(tauri::generate_context!()).map_err(|_| "tauri_build_failed".to_string())?;
    // Wake the native loop even while idle so a pending close reaches its named
    // monotonic bound. An owned, joinable timer; no backend cleanup on this thread.
    let stop = Arc::new((Mutex::new(false), Condvar::new()));
    let timer_stop = stop.clone();
    let handle = app.handle().clone();
    let poll = BudgetId::RegistryPoll
        .span(Entries::ZERO)
        .map_err(|e| format!("budget:{e:?}"))?;
    let timer = std::thread::Builder::new()
        .name("desktop-wake".into())
        .spawn(move || -> Result<(), &'static str> {
            loop {
                let guard = timer_stop.0.lock().map_err(|_| "wake_lock_poisoned")?;
                let (guard, _) = timer_stop
                    .1
                    .wait_timeout_while(guard, Duration::from_millis(poll.millis()), |stopped| {
                        !*stopped
                    })
                    .map_err(|_| "wake_wait_poisoned")?;
                if *guard {
                    return Ok(());
                }
                drop(guard);
                handle
                    .run_on_main_thread(|| {})
                    .map_err(|_| "wake_dispatch_failed")?;
            }
        })
        .map_err(|e| format!("wake_spawn:{:?}", e.kind()))?;
    let mut readiness = Readiness::default();
    let mut advertised = false;
    let mut occlusion: Option<bool> = None;
    let mut last = State::Running;
    app.run(move |app, event| {
        match event {
            RunEvent::Ready => {
                native_quit.arm();
                readiness.native_ready();
            }
            RunEvent::MainEventsCleared => {
                readiness.event_barrier();
                probe_hooks::occlusion(dev_probe, app, &mut occlusion);
                if readiness.is_ready() && !advertised {
                    advertised = true;
                    println!("L11_NATIVE_READY");
                    if let Some(window) = app.get_webview_window("main")
                        && let Err(e) = window.show()
                    {
                        eprintln!("window_show_failed:{e}");
                    } else if let Some(window) = app.get_webview_window("main") {
                        // Launch-to-window receipt for the packaged smoke: the native
                        // window exists, was shown, and reports its own visibility.
                        match window.is_visible() {
                            Ok(visible) => println!("L11_WINDOW_VISIBLE:{visible}"),
                            Err(e) => eprintln!("window_visible_failed:{e}"),
                        }
                        // Report the size the window actually opened at, in logical points, so a
                        // smoke test can hold the "the form is usable without resizing" line:
                        // the default fallback was ~400×300 and clipped the whole create form.
                        match (window.inner_size(), window.scale_factor()) {
                            (Ok(size), Ok(scale)) if scale > 0.0 => println!(
                                "L11_WINDOW_SIZE:{}x{}",
                                (f64::from(size.width) / scale).round() as i64,
                                (f64::from(size.height) / scale).round() as i64
                            ),
                            (Err(e), _) => eprintln!("window_size_failed:{e}"),
                            (_, Err(e)) => eprintln!("window_scale_failed:{e}"),
                            _ => eprintln!("window_size_failed:zero_scale"),
                        }
                    }
                }
            }
            RunEvent::MenuEvent(event) if event.id().as_ref() == "quit" => {
                request_quit(&shutdown, &channel, QuitSource::Menu, start)
            }
            RunEvent::ExitRequested { api, .. } => {
                // A Quit with a result (verified or allowance spent: 0, a raised close step: 3 as v0.1.4) may leave; anything
                // earlier is turned into the one shared cleanup request.
                let has_result = match shutdown.lock() {
                    Ok(state) => state.exit_status().is_some(),
                    Err(_) => {
                        eprintln!("cleanup_lock_poisoned");
                        false
                    }
                };
                if !has_result {
                    api.prevent_exit();
                    request_quit(&shutdown, &channel, QuitSource::AppleEvent, start);
                }
            }
            RunEvent::WindowEvent {
                event: tauri::WindowEvent::CloseRequested { api, .. },
                ..
            } => {
                api.prevent_close();
                request_quit(&shutdown, &channel, QuitSource::Menu, start);
            }
            RunEvent::Reopen {
                has_visible_windows,
                ..
            } => {
                // Dock click / `open` of the running app (Finder double-click on it again):
                // bring the pinned window back; receipt for the qualifier.
                let shown = app
                    .get_webview_window("main")
                    .map(|window| window.show().and_then(|()| window.is_visible()));
                match shown {
                    Some(Ok(visible)) => println!(
                        "L11_REOPEN:had_visible_windows={has_visible_windows}:visible={visible}"
                    ),
                    Some(Err(e)) => eprintln!("reopen_failed:{e}"),
                    None => eprintln!("reopen_failed:no_window"),
                }
            }
            _ => {}
        }
        if native_quit.take_request() {
            request_quit(&shutdown, &channel, QuitSource::AppleEvent, start);
        }
        let state = match (shutdown.lock(), elapsed(start)) {
            (Ok(mut state), Ok(now)) => state.tick(now),
            _ => {
                eprintln!("cleanup_state_unavailable");
                return;
            }
        };
        if state != last {
            println!("L11_CLEANUP:{state:?}");
            last = state;
            match state {
                State::Verified => app.exit(0),
                // v0.1.4 `demo_app.py:202-203`: the join allowance running out with the teardown
                // still going is not an error - it exits 0, silently (RU-032, executed q4).
                State::Failed(CleanupError::Deadline) => app.exit(0),
                // `__main__.py:219-223`: a close step that raised retains the state, says so on
                // stderr and exits 3; it does not stay up.
                State::Failed(_) => {
                    eprintln!("{}", crate::lifecycle::CLEANUP_INCOMPLETE_EVENT);
                    app.exit(crate::lifecycle::EXIT_CLEANUP_INCOMPLETE);
                }
                State::Running | State::Stopping => {}
            }
        }
    });
    *stop.0.lock().map_err(|_| "wake_stop_poisoned")? = true;
    stop.1.notify_one();
    timer
        .join()
        .map_err(|_| "wake_thread_panicked")?
        .map_err(String::from)
}
struct WindowBrowser {
    probe: bool,
}
impl crate::links::Browser for WindowBrowser {
    fn open(&self, url: &tauri::Url) -> Result<(), crate::links::LinkError> {
        if probe_hooks::capture_browser_open(self.probe) {
            Ok(())
        } else {
            crate::links::Browser::open(&crate::native_links::SystemBrowser, url)
        }
    }
}
fn handle_link(url: &tauri::Url, probe: bool) -> bool {
    match crate::links::navigate(url, &WindowBrowser { probe }) {
        Ok(allowed) => allowed,
        Err(error) => {
            eprintln!("external_link_failed:{error:?}");
            false
        }
    }
}
/// Development/qualification hooks. Without a build that names them (debug `dev-shell` or the
/// non-default `qualification-probe` feature) every function is an empty inline stub: no
/// attack-target stubs, receipts, page events or capture strings exist in the shipped executable
/// (`tests/probe_absence.rs` scans the built bundle).
#[cfg(any(
    feature = "qualification-probe",
    all(feature = "dev-shell", debug_assertions)
))]
mod probe_hooks {
    use super::{Reply, channel, response};
    pub fn channel_refusal(on: bool, request: &channel::Request, error: &dyn std::fmt::Debug) {
        if on {
            let origin = request
                .headers
                .iter()
                .find(|(k, _)| k.eq_ignore_ascii_case("origin"))
                .map(|(_, v)| v.as_str());
            eprintln!(
                "L11_CHANNEL_REFUSAL:{error:?}:origin_absent={}:origin_null={}:origin_exact={}",
                origin.is_none(),
                origin == Some("null"),
                origin == Some(channel::ORIGIN)
            );
        }
    }
    /// These inert attack targets must never be reached: denial is at CSP/navigation, before a
    /// document can borrow the native origin.
    pub fn attack_target(on: bool, path: &str) -> Option<tauri::http::Response<Vec<u8>>> {
        if !(on
            && matches!(
                path,
                "/l11-attack" | "/l11-attack.js" | "/l11-redirect" | "/api/l11-forbidden-post"
            ))
        {
            return None;
        }
        println!("L11_ATTACK_TARGET_REACHED_FAILED");
        Some(if path == "/l11-redirect" {
            let mut reply = response(Reply {
                status: 302,
                content_type: "text/html",
                body: Vec::new(),
            });
            reply.headers_mut().insert(
                "location",
                tauri::http::HeaderValue::from_static("mycelium://localhost/l11-attack"),
            );
            reply
        } else {
            response(Reply {
                status: 200,
                content_type: "text/html",
                body: b"<p>inert rejected target</p>".to_vec(),
            })
        })
    }
    /// Development shell only: `GET /l11-quit/{menu,command-q}` from the trusted probe script makes
    /// AppKit perform the real menu item / Command-Q key equivalent of our own window, so the
    /// qualifier can prove those paths reach the one shared cleanup. Absent in every other build.
    #[cfg(all(feature = "dev-shell", debug_assertions))]
    pub fn native_action(
        on: bool,
        app: &tauri::AppHandle,
        path: &str,
    ) -> Option<tauri::http::Response<Vec<u8>>> {
        if on && path == "/l11-window/hide" {
            // Reopen qualification: a window that is not visible when LaunchServices reopens us.
            let handle = app.clone();
            let queued = app.run_on_main_thread(move || {
                use tauri::Manager as _;
                if let Some(window) = handle.get_webview_window("main") {
                    match window.hide().and_then(|()| window.is_visible()) {
                        Ok(visible) => println!("L11_WINDOW_HIDDEN:visible={visible}"),
                        Err(e) => println!("L11_WINDOW_HIDDEN:FAILED:{e}"),
                    }
                }
            });
            return Some(response(Reply {
                status: if queued.is_ok() { 200 } else { 500 },
                content_type: "application/json",
                body: b"{}".to_vec(),
            }));
        }
        let variants = crate::native_probe::Variant::from_path(path).filter(|_| on)?;
        let queued = app.run_on_main_thread(move || {
            for variant in variants {
                let label = variant.label();
                match crate::native_probe::dispatch(*variant) {
                    Ok(answer) => println!("L11_QUIT_VARIANT:{label}:{answer}"),
                    Err(error) => println!("L11_QUIT_VARIANT:{label}:FAILED:{error}"),
                }
            }
        });
        Some(response(Reply {
            status: if queued.is_ok() { 200 } else { 500 },
            content_type: "application/json",
            body: b"{}".to_vec(),
        }))
    }
    #[cfg(not(all(feature = "dev-shell", debug_assertions)))]
    pub fn native_action(
        _: bool,
        _: &tauri::AppHandle,
        _: &str,
    ) -> Option<tauri::http::Response<Vec<u8>>> {
        None
    }
    pub fn navigation_denied(on: bool, app: &tauri::AppHandle, url: &tauri::Url) {
        if on {
            println!("L11_NAVIGATION_DENIED:{}", url.scheme());
            super::probe_event(app, "l11-navigation-blocked");
        }
    }
    pub fn popup_blocked(on: bool, app: &tauri::AppHandle) {
        if on {
            super::probe_event(app, "l11-popup-blocked");
        }
    }
    pub fn title_changed(on: bool, app: &tauri::AppHandle, title: &str) {
        if on && title.starts_with("L11_") {
            println!("{title}");
            super::probe_event(app, "l11-title-recorded");
        }
    }
    /// Observation for the qualifier: every change of the AppKit occlusion state.
    pub fn occlusion(on: bool, app: &tauri::AppHandle, last: &mut Option<bool>) {
        use tauri::Manager as _;
        if on
            && let Some(window) = app.get_webview_window("main")
            && let Some(visible) = crate::webview_policy::window_visible(&window)
            && *last != Some(visible)
        {
            *last = Some(visible);
            println!(
                "L11_WINDOW_OCCLUSION:{}",
                if visible { "visible" } else { "occluded" }
            );
        }
    }
    /// Dev smoke records the OS seam, never launches the user's browser.
    pub fn capture_browser_open(on: bool) -> bool {
        if on {
            println!("L11_SYSTEM_BROWSER_CAPTURE");
        }
        on
    }
}
#[cfg(not(any(
    feature = "qualification-probe",
    all(feature = "dev-shell", debug_assertions)
)))]
mod probe_hooks {
    use super::channel;
    #[inline(always)]
    pub fn channel_refusal(_: bool, _: &channel::Request, _: &dyn std::fmt::Debug) {}
    #[inline(always)]
    pub fn attack_target(_: bool, _: &str) -> Option<tauri::http::Response<Vec<u8>>> {
        None
    }
    #[inline(always)]
    pub fn native_action(
        _: bool,
        _: &tauri::AppHandle,
        _: &str,
    ) -> Option<tauri::http::Response<Vec<u8>>> {
        None
    }
    #[inline(always)]
    pub fn navigation_denied(_: bool, _: &tauri::AppHandle, _: &tauri::Url) {}
    #[inline(always)]
    pub fn popup_blocked(_: bool, _: &tauri::AppHandle) {}
    #[inline(always)]
    pub fn title_changed(_: bool, _: &tauri::AppHandle, _: &str) {}
    #[inline(always)]
    pub fn occlusion(_: bool, _: &tauri::AppHandle, _: &mut Option<bool>) {}
    #[inline(always)]
    pub fn capture_browser_open(_: bool) -> bool {
        false
    }
}
#[cfg(any(
    feature = "qualification-probe",
    all(feature = "dev-shell", debug_assertions)
))]
fn probe_event(app: &tauri::AppHandle, event: &'static str) {
    let handle = app.clone();
    if let Err(error) = app.run_on_main_thread(move || {
        if let Some(window) = handle.get_webview_window("main")
            && let Err(error) = window.eval(format!("window.dispatchEvent(new Event('{event}'))"))
        {
            eprintln!("probe_event_failed:{error}");
        }
    }) {
        eprintln!("probe_dispatch_failed:{error}");
    }
}
fn request_quit<C: Cleanup>(
    shutdown: &Mutex<Shutdown<C>>,
    channel: &Mutex<Channel>,
    source: QuitSource,
    start: Instant,
) {
    match channel.lock() {
        Ok(mut channel) => channel.revoke(),
        Err(_) => eprintln!("channel_revoke_failed"),
    }
    match (shutdown.lock(), elapsed(start)) {
        (Ok(mut state), Ok(now)) => state.request(source, now),
        _ => eprintln!("cleanup_request_failed"),
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn both_pinned_documents_emit_the_enforced_csp_without_redirects() {
        for path in ["/", "/chat"] {
            let reply = response(asset(path).expect("pinned asset"));
            assert_eq!(reply.status(), 200);
            assert_eq!(reply.headers()["content-security-policy"], channel::CSP);
            assert!(reply.headers().get("location").is_none());
            assert_eq!(reply.headers()["referrer-policy"], "no-referrer");
        }
    }
}
