// SPDX-License-Identifier: AGPL-3.0-only
//! Additive class-A shard prepare and signed local plan contracts. Legacy prepare,
//! proof, step and close bytes are unchanged. The new prepare carries the legacy
//! prepare fields plus `shard`: protocol, node_id, range, transfer (B2 row), recipe
//! (B2 node slice plan), and recipe_digest. Checkpoint names ONLY that stage file.
//! The v2 plan keeps v1 top-level fields, adds `shard` to each stage, and binds the
//! same source provenance plus shard transfer/recipe pins. It grants no serving.
pub const COMMAND: &str = "prepare_shard_v1";
pub const PROTOCOL: &str = "mycelium.native_stage_shard.v1";
pub const PLAN_PROTOCOL: &str = "mycelium.native_local_operator_plan.v2";
pub const HASH: &str = "native_shard_transfer_hash_mismatch";
pub const LENGTH: &str = "native_shard_length_mismatch";
pub const RECIPE: &str = "native_shard_recipe_mismatch";
pub const BINDING: &str = "native_shard_binding_mismatch";
pub const TENSORS: &str = "native_shard_tensor_mismatch";
pub const PATH: &str = "native_shard_path_invalid";
pub const READ: &str = "native_shard_read_failed";
