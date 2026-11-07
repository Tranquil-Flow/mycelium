// SPDX-License-Identifier: AGPL-3.0-only
pub mod bundle;
pub mod channel;
pub mod core;
pub mod docs;
pub mod entry;
pub mod install;
pub mod launch_location;
pub mod lifecycle;
#[cfg(feature = "tauri-shell")]
pub mod links;
#[cfg(feature = "tauri-shell")]
mod native_links;
#[cfg(all(feature = "dev-shell", debug_assertions))]
mod native_probe;
#[cfg(feature = "tauri-shell")]
mod native_quit;
pub mod package;
pub mod platform;
pub mod services;
#[cfg(feature = "tauri-shell")]
pub mod shell;
#[cfg(feature = "tauri-shell")]
mod webview_policy;
