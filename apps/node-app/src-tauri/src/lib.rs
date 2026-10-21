// SPDX-License-Identifier: AGPL-3.0-only
//! Thin command layer over the shared Rust core. Commands never block the UI
//! thread: anything that touches the network runs on a blocking worker, and the
//! UI learns about progress from throttled `node://status` events.

mod node_service;

use std::sync::{Arc, OnceLock};
use std::time::Duration;

use mycelium_mobile_core::node::{NodeCore, NodeStatus};
use mycelium_mobile_core::transport::Reachability;
use node_service::{NodeService, ServiceStatus};
use tauri::{AppHandle, Emitter, Manager, State, Wry};

static CORE: OnceLock<Result<Arc<NodeCore>, String>> = OnceLock::new();

fn core() -> Result<Arc<NodeCore>, String> {
    match CORE.get() {
        Some(Ok(core)) => Ok(core.clone()),
        Some(Err(e)) => Err(e.clone()),
        None => Err("core_starting".into()),
    }
}

#[derive(serde::Serialize)]
struct CoreInfo {
    core_version: &'static str,
    target: &'static str,
    ready: bool,
}

#[tauri::command]
fn core_info() -> CoreInfo {
    CoreInfo {
        core_version: env!("CARGO_PKG_VERSION"),
        target: std::env::consts::OS,
        ready: matches!(CORE.get(), Some(Ok(_))),
    }
}

#[tauri::command]
fn node_status() -> Result<NodeStatus, String> {
    core().map(|c| c.status())
}

async fn blocking<T: Send + 'static>(
    f: impl FnOnce(Arc<NodeCore>) -> Result<T, String> + Send + 'static,
) -> Result<T, String> {
    let core = core()?;
    tauri::async_runtime::spawn_blocking(move || f(core))
        .await
        .map_err(|_| "worker_failed".to_string())?
}

#[tauri::command]
async fn node_pair(qr: String, device_ref: String) -> Result<NodeStatus, String> {
    blocking(move |c| c.pair_with_channel_qr(&qr, &device_ref).map_err(|e| e.0)).await
}

#[tauri::command]
async fn node_join() -> Result<NodeStatus, String> {
    blocking(|c| c.join().map_err(|e| e.0)).await
}

#[tauri::command]
async fn node_drain() -> Result<NodeStatus, String> {
    blocking(|c| Ok(c.drain())).await
}

const SERVING_DETAIL: &str = "Serving as a Mycelium node";

/// Start the OS serving contract and remember the choice, so a restarted app
/// (process death, force-stop) restores serving once it has resumed.
#[tauri::command]
fn serving_start(svc: State<'_, NodeService<Wry>>) -> Result<ServiceStatus, String> {
    let status = svc.start(SERVING_DETAIL)?;
    core()?.set_serving_intent(true).map_err(|e| e.0)?;
    Ok(status)
}

#[tauri::command]
fn serving_stop(svc: State<'_, NodeService<Wry>>) -> Result<ServiceStatus, String> {
    core()?.set_serving_intent(false).map_err(|e| e.0)?;
    svc.stop()
}

#[derive(serde::Serialize)]
struct EvidenceExport {
    shared: bool,
    observations: usize,
    downloads: Option<String>,
}

/// Seal the current observations into one export bundle under the app cache and
/// hand it to the share sheet (and, on Android 10+, a copy to Downloads); only
/// signed runtime observations leave the device.
#[tauri::command]
fn node_export_evidence(app: AppHandle, svc: State<'_, NodeService<Wry>>) -> Result<EvidenceExport, String> {
    let out = app.path().app_cache_dir().map_err(|e| e.to_string())?.join("exports");
    let (path, observations) = core()?.export_evidence(&out).map_err(|e| e.0)?;
    let result = svc.share(&path.to_string_lossy())?;
    Ok(EvidenceExport { shared: result.shared, observations, downloads: result.downloads })
}

#[tauri::command]
fn serving_status(svc: State<'_, NodeService<Wry>>) -> Result<ServiceStatus, String> {
    svc.status()
}

fn start_core(app: &AppHandle) {
    let dir = app.path().app_data_dir().map(|d| d.join("node"));
    let handle = app.clone();
    #[cfg(all(debug_assertions, target_os = "android"))]
    if let Ok(d) = &dir {
        // Debug-only transport diagnostics into app-private storage.
        let _ = std::fs::create_dir_all(d);
        if let Ok(file) = std::fs::File::create(d.join("dev-trace.log")) {
            let _ = tracing_subscriber::fmt()
                .with_env_filter(tracing_subscriber::EnvFilter::new("warn,iroh=info,iroh_relay=info,n0_dns_resolver=debug"))
                .with_ansi(false)
                .with_writer(std::sync::Mutex::new(file))
                .try_init();
        }
    }
    std::thread::Builder::new()
        .name("mycelium-core-start".into())
        .spawn(move || {
            let opened = dir
                .map_err(|e| e.to_string())
                .and_then(|d| NodeCore::open(&d, Reachability::Production).map_err(|e| e.0))
                .map(Arc::new);
            #[cfg(debug_assertions)]
            if let (Ok(core), Ok(dir)) = (&opened, handle.path().app_data_dir()) {
                load_dev_route(core, &dir.join("node").join("dev-route.json"));
            }
            let _ = CORE.set(opened);
            // A restarted, already enrolled member resumes without a new invite
            // (after the core is visible to the UI, so the UI never waits on it).
            if let Ok(c) = core()
                && c.is_enrolled()
            {
                let _ = c.resume();
                // The user had chosen to serve before the process died: bring
                // the serving service back (the app is in the foreground now).
                if c.serving_intent()
                    && let Some(svc) = handle.try_state::<NodeService<Wry>>()
                {
                    let _ = svc.start(SERVING_DETAIL);
                }
            }
            #[cfg(debug_assertions)]
            let dev_dir = handle.path().app_data_dir().ok().map(|d| d.join("node"));
            #[cfg(debug_assertions)]
            let mut last_dump = String::new();
            // Throttled status stream (<= 2 Hz): the UI never polls in a loop.
            let mut tick: u64 = 0;
            let mut last_service: Option<ServiceStatus> = None;
            loop {
                // The OS service can stop or come back without a UI action
                // (notification Stop, process restart): stream it too (~2 s).
                if tick.is_multiple_of(4)
                    && let Some(svc) = handle.try_state::<NodeService<Wry>>()
                    && let Ok(service) = svc.status()
                    && last_service.as_ref().map(|l| (l.running, &l.mode)) != Some((service.running, &service.mode))
                {
                    // Stopped from outside the app (the notification's Stop):
                    // that is the user's choice too, so do not restore it later.
                    if last_service.as_ref().is_some_and(|l| l.running)
                        && !service.running
                        && let Ok(c) = core()
                    {
                        let _ = c.set_serving_intent(false);
                    }
                    let _ = handle.emit("node://service", service.clone());
                    last_service = Some(service);
                }
                if let Ok(c) = core() {
                    // OS power/thermal readings for the sealed observations, every ~10 s.
                    if tick.is_multiple_of(20)
                        && let Some(svc) = handle.try_state::<NodeService<Wry>>()
                        && let Ok(sample) = svc.platform_metrics()
                    {
                        c.set_platform_metrics(sample);
                    }
                    let status = c.status();
                    #[cfg(debug_assertions)]
                    if let Some(dir) = &dev_dir {
                        dev_hooks(&c, dir, &status, &mut last_dump);
                    }
                    let _ = handle.emit("node://status", status);
                }
                tick = tick.wrapping_add(1);
                std::thread::sleep(Duration::from_millis(500));
            }
        })
        .expect("spawn core thread");
}

/// DEBUG BUILDS ONLY: a synthetic test assignment (stage pack + one route peer)
/// placed in app-private storage by the developer. It stands in for the A2
/// artifact client and planner placement, which this build does not implement;
/// release builds never read it.
#[cfg(debug_assertions)]
fn load_dev_route(core: &NodeCore, path: &std::path::Path) {
    let Ok(raw) = std::fs::read(path) else { return };
    let Ok(route) = serde_json::from_slice::<serde_json::Value>(&raw) else { return };
    let _ = core.load_stage(&route["pack"]);
    if let (Some(peer), Some(generation)) = (route["peer_endpoint_id"].as_str(), route["generation"].as_u64()) {
        let _ = core.configure_route_peer(peer, generation, true);
    }
}

/// DEBUG BUILDS ONLY, for unattended device checkpoints on a locked phone:
/// mirrors status changes to `node/dev-status.jsonl` and consumes a one-shot
/// `node/dev-invite.txt` through the same pair-and-join path the scan uses.
#[cfg(debug_assertions)]
fn dev_hooks(core: &NodeCore, dir: &std::path::Path, status: &NodeStatus, last: &mut String) {
    use std::io::Write;
    let mut snapshot = serde_json::to_value(status).unwrap_or_default();
    snapshot["last_heartbeat_at"] = serde_json::Value::Null;
    snapshot["lease_expires_at"] = serde_json::Value::Null;
    let key = snapshot.to_string();
    if key != *last {
        *last = key;
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(dir.join("dev-status.jsonl")) {
            let _ = writeln!(f, "{}", serde_json::json!({"t": mycelium_mobile_core::node::now_s(), "status": status}));
        }
    }
    let invite = dir.join("dev-invite.txt");
    if let Ok(qr) = std::fs::read_to_string(&invite) {
        let _ = std::fs::remove_file(&invite);
        let ref_ = format!("device-{}", &status.endpoint_id[..12]);
        if core.pair_with_channel_qr(qr.trim(), &ref_).is_ok() {
            let _ = core.join();
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default().plugin(tauri_plugin_deep_link::init());
    #[cfg(mobile)]
    let builder = builder.plugin(tauri_plugin_barcode_scanner::init());
    builder
        .plugin(node_service::init())
        .setup(|app| {
            start_core(app.handle());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            core_info,
            node_status,
            node_pair,
            node_join,
            node_drain,
            serving_start,
            serving_stop,
            serving_status,
            node_export_evidence
        ])
        .run(tauri::generate_context!())
        .expect("error while building tauri application");
}
