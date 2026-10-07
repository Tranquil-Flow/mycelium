// SPDX-License-Identifier: AGPL-3.0-or-later
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

pub mod canonical;
pub mod identity;
pub mod onboarding;
pub mod runtime;
pub mod bf16;
pub mod quant;
pub mod safetensors;
pub mod qwen2;
pub mod stage_manifest;
pub mod artifact;
pub mod wire;
pub mod membership;
pub mod evidence;
pub mod route_assignment;
#[cfg(feature = "net")]
pub mod transport;
#[cfg(feature = "net")]
pub mod node;
