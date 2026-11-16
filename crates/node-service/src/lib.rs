// SPDX-License-Identifier: AGPL-3.0-only
//! Explicit composition adapters. The native slice carries control, provisioning and
//! activation over Iroh; loopback TCP is only the qualification HTTP gateway.
pub mod activation;
#[cfg(any(feature = "desktop-shell", feature = "packaged"))]
pub mod app_host;
#[cfg(feature = "iroh-loopback")]
pub mod artifact;
pub mod chat_edge;
#[cfg(feature = "iroh-loopback")]
pub mod control;
#[cfg(any(feature = "desktop-shell", feature = "packaged"))]
pub mod desktop_host;
pub mod detok;
pub mod enrollment;
#[cfg(feature = "metal")]
pub mod hello_bootstrap;
#[cfg(feature = "iroh-loopback")]
pub mod iroh_frames;
pub mod lifecycle;
pub mod liveness;
pub mod loopback;
#[cfg(feature = "metal")]
pub mod native;
pub mod native_selection;
pub mod owner_chain;
#[cfg(any(feature = "desktop-shell", feature = "packaged"))]
pub mod packaged;
#[cfg(any(feature = "desktop-shell", feature = "packaged"))]
pub mod packaged_network;
#[cfg(any(feature = "desktop-shell", feature = "packaged"))]
pub mod packaged_product;
#[cfg(any(feature = "desktop-shell", feature = "packaged"))]
pub mod packaged_run;
#[cfg(feature = "iroh-loopback")]
pub mod packaged_work;
pub mod phase;
pub mod planning;
pub mod private_state;
pub mod reap;
pub mod receipts;
pub mod router_port;
pub mod service;
pub mod spawn;
pub mod status;
#[cfg(feature = "metal")]
pub mod tokenize;
pub mod worker;
pub mod worker_link;
