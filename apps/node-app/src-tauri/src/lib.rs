mod node_service;

use node_service::{NodeService, ServiceStatus};
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::{State, Wry};

#[derive(serde::Serialize)]
struct CoreInfo {
    core_version: &'static str,
    target: &'static str,
    started_ms: u128,
}

#[tauri::command]
fn core_info() -> CoreInfo {
    CoreInfo {
        core_version: env!("CARGO_PKG_VERSION"),
        target: std::env::consts::OS,
        started_ms: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0),
    }
}

#[tauri::command]
fn serving_start(svc: State<'_, NodeService<Wry>>) -> Result<ServiceStatus, String> {
    svc.start("Serving as a Mycelium node")
}

#[tauri::command]
fn serving_stop(svc: State<'_, NodeService<Wry>>) -> Result<ServiceStatus, String> {
    svc.stop()
}

#[tauri::command]
fn serving_status(svc: State<'_, NodeService<Wry>>) -> Result<ServiceStatus, String> {
    svc.status()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(node_service::init())
        .invoke_handler(tauri::generate_handler![
            core_info,
            serving_start,
            serving_stop,
            serving_status
        ])
        .run(tauri::generate_context!())
        .expect("error while building tauri application");
}
