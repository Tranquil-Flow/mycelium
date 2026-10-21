// SPDX-License-Identifier: AGPL-3.0-only
//! Shared Rust core of the Mycelium node app.
//!
//! Pure modules (no I/O, wasm32-compatible): [`canonical`], [`identity`],
//! [`onboarding`], [`membership`], [`wire`], [`runtime`], [`evidence`],
//! [`route_assignment`], and the Qwen2 stage runtime [`qwen2`] with its exact
//! weight plumbing [`bf16`], [`quant`] and [`safetensors`], the signed v2
//! stage manifest [`stage_manifest`] and the digest-verified per-tensor
//! artifact store [`artifact`] (std::fs; unused in the browser build).
//! The `net` feature adds the seed HTTP client, the embedded iroh endpoint and
//! the node supervisor used by the Android and iOS apps.

pub mod artifact;
pub mod artifact_grant;
#[cfg(feature = "artifact-source")]
pub mod artifact_net;
pub mod bf16;
pub mod canonical;
pub mod evidence;
pub mod identity;
pub mod membership;
#[cfg(feature = "net")]
pub mod node;
pub mod node_cancel;
pub mod node_control;
pub mod onboarding;
pub mod quant;
pub mod qwen2;
pub mod route_assignment;
pub mod runtime;
pub mod safetensors;
pub mod stage_manifest;
pub mod stage_node;
pub mod stage_wire;
#[cfg(feature = "artifact-source")]
pub mod transport;
pub mod wire;
