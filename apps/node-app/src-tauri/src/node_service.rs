// SPDX-License-Identifier: AGPL-3.0-only
//! The OS serving contract. On Android this is a foreground service (Kotlin,
//! `NodeServicePlugin`); on iOS and desktop there is no background service: serving
//! is foreground-active only, so `start` reports `foreground_only`.

use serde::{Deserialize, Serialize};
use tauri::{
    Manager, Runtime,
    plugin::{Builder, TauriPlugin},
};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ServiceStatus {
    pub running: bool,
    #[serde(default)]
    pub uptime_ms: u64,
    /// `android_foreground_service` or `foreground_only`.
    #[serde(default)]
    pub mode: String,
}

#[derive(Serialize)]
struct StartArgs<'a> {
    detail: &'a str,
}

#[derive(Serialize)]
struct ShareArgs<'a> {
    path: &'a str,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ShareResult {
    pub shared: bool,
    /// File name of the copy saved to the public Downloads collection, if any.
    #[serde(default)]
    pub downloads: Option<String>,
}

pub struct NodeService<R: Runtime> {
    #[cfg(target_os = "android")]
    handle: tauri::plugin::PluginHandle<R>,
    #[cfg(not(target_os = "android"))]
    _marker: std::marker::PhantomData<fn() -> R>,
    #[cfg(not(target_os = "android"))]
    running: std::sync::atomic::AtomicBool,
}

impl<R: Runtime> NodeService<R> {
    pub fn start(&self, detail: &str) -> Result<ServiceStatus, String> {
        #[cfg(target_os = "android")]
        {
            let mut s: ServiceStatus = self
                .handle
                .run_mobile_plugin("start", StartArgs { detail })
                .map_err(|e| e.to_string())?;
            s.mode = "android_foreground_service".into();
            Ok(s)
        }
        #[cfg(not(target_os = "android"))]
        {
            let _ = StartArgs { detail };
            self.running
                .store(true, std::sync::atomic::Ordering::SeqCst);
            Ok(ServiceStatus {
                running: true,
                uptime_ms: 0,
                mode: "foreground_only".into(),
            })
        }
    }

    pub fn stop(&self) -> Result<ServiceStatus, String> {
        #[cfg(target_os = "android")]
        {
            let mut s: ServiceStatus = self
                .handle
                .run_mobile_plugin("stop", ())
                .map_err(|e| e.to_string())?;
            s.mode = "android_foreground_service".into();
            Ok(s)
        }
        #[cfg(not(target_os = "android"))]
        {
            self.running
                .store(false, std::sync::atomic::Ordering::SeqCst);
            Ok(ServiceStatus {
                running: false,
                uptime_ms: 0,
                mode: "foreground_only".into(),
            })
        }
    }

    /// Offer one exported evidence bundle (under the app cache) to the system share
    /// sheet. Android only; elsewhere the bundle stays where it was written.
    pub fn share(&self, path: &str) -> Result<ShareResult, String> {
        #[cfg(target_os = "android")]
        {
            self.handle
                .run_mobile_plugin("share", ShareArgs { path })
                .map_err(|e| e.to_string())
        }
        #[cfg(not(target_os = "android"))]
        {
            let _ = ShareArgs { path };
            Ok(ShareResult { shared: false, downloads: None })
        }
    }

    /// Battery, charging and thermal readings from the OS (Android
    /// `BatteryManager` / `PowerManager`); empty where the shell has none.
    pub fn platform_metrics(&self) -> Result<mycelium_mobile_core::evidence::Metrics, String> {
        #[cfg(target_os = "android")]
        {
            let raw: std::collections::BTreeMap<String, Option<f64>> = self
                .handle
                .run_mobile_plugin("platformMetrics", ())
                .map_err(|e| e.to_string())?;
            Ok(raw.into_iter().collect())
        }
        #[cfg(not(target_os = "android"))]
        {
            Ok(Default::default())
        }
    }

    pub fn status(&self) -> Result<ServiceStatus, String> {
        #[cfg(target_os = "android")]
        {
            let mut s: ServiceStatus = self
                .handle
                .run_mobile_plugin("status", ())
                .map_err(|e| e.to_string())?;
            s.mode = "android_foreground_service".into();
            Ok(s)
        }
        #[cfg(not(target_os = "android"))]
        {
            let running = self.running.load(std::sync::atomic::Ordering::SeqCst);
            Ok(ServiceStatus {
                running,
                uptime_ms: 0,
                mode: "foreground_only".into(),
            })
        }
    }
}

pub fn init<R: Runtime>() -> TauriPlugin<R> {
    Builder::new("nodeservice")
        .setup(|app, _api| {
            #[cfg(target_os = "android")]
            let svc = NodeService {
                handle: _api.register_android_plugin("now.mycelium.node", "NodeServicePlugin")?,
            };
            #[cfg(not(target_os = "android"))]
            let svc = NodeService::<R> {
                _marker: std::marker::PhantomData,
                running: std::sync::atomic::AtomicBool::new(false),
            };
            app.manage(svc);
            Ok(())
        })
        .build()
}
