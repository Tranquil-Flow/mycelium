// SPDX-License-Identifier: AGPL-3.0-only
//! Composition seams; no listener, worker, model, relay or QUIC is launched here.
#[cfg(all(feature = "iroh-adapter", unix))]
pub mod artifact_journal;
pub mod composition;
pub mod discovery;
pub mod forwarding;
#[cfg(feature = "iroh-adapter")]
pub mod iroh;
#[cfg(feature = "iroh-adapter")]
pub mod iroh_artifact;
#[cfg(feature = "iroh-adapter")]
pub mod iroh_artifact_source;
#[cfg(feature = "iroh-adapter")]
pub mod iroh_cleanup;
#[cfg(feature = "iroh-adapter")]
pub mod iroh_control;
#[cfg(feature = "iroh-adapter")]
pub(crate) mod iroh_cross;
#[cfg(feature = "iroh-adapter")]
pub mod iroh_control_receive;
#[cfg(feature = "iroh-adapter")]
pub mod iroh_host;
#[cfg(feature = "iroh-adapter")]
pub(crate) mod iroh_lan_filter;
#[cfg(feature = "iroh-adapter")]
pub mod iroh_managed;
#[cfg(feature = "iroh-adapter")]
pub mod iroh_pool;
#[cfg(feature = "iroh-adapter")]
pub mod iroh_receive;
pub mod iroh_wire;
pub mod m16_capacity;
pub mod m16_coordinator;
pub mod m16_factory;
pub mod m16_hop_scheduler;
pub mod m16_ledger;
pub mod m16_path_builder;
pub mod m16_route_scorer;
pub mod m16_router_contracts;
#[cfg(unix)]
pub mod managed_owner;
pub mod managed_workflow;
pub mod mode;
pub mod observe;
pub mod observe_owner;
pub mod prefill_watch;
pub mod published;
pub mod relay;
pub mod repeats;
#[cfg(unix)]
pub mod relay_process;
pub mod rollover;
pub mod shutdown;
#[cfg(all(feature = "iroh-adapter", unix))]
pub mod sidecar_artifact;
#[cfg(all(feature = "iroh-adapter", unix))]
pub mod sidecar_listener;
pub mod sidecar_local;
#[cfg(all(feature = "iroh-adapter", unix))]
pub mod sidecar_main;
pub mod sidecar_pool;
#[cfg(all(feature = "iroh-adapter", unix))]
pub mod sidecar_process;
#[cfg(feature = "iroh-adapter")]
pub mod sidecar_session;
pub mod sidecar_telemetry;
pub mod simulated;
pub mod subject_cancel;
pub mod worker;
