// SPDX-License-Identifier: AGPL-3.0-only
//! Local explicit consent, not a signed remote execution permit.
use crate::{Error, Result};
use budgets::{Bytes, Entries, Span};
use serde::{Deserialize, Serialize};
pub const DISCLOSURE: &str = "While this Mac is a member of the owner's swarm and this app is open, it will automatically: accept work the owner assigns to it; download and store only the model slice assigned to it; load that slice with MLX and compute it for requests routed through the swarm; and send the owner a capability report (chip, memory, free disk, MLX availability, power and thermal state) and a link measurement over the relay on join and before each run. Limits: at most the disk, memory, run time and command budget you choose below, only on the power and network policy you chose, and it stops on serious thermal pressure. Cooperative workers observe the activations they compute, so this is not confidential inference. You can withdraw this consent at any time from this app; automatic acceptance stops immediately.";
pub fn disclosure_digest() -> String {
    protocol::canonical::sha256_prefixed(DISCLOSURE.as_bytes())
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Caps {
    pub storage_bytes: u64,
    pub memory_bytes: u64,
    pub wall_ms: u64,
    pub commands: u64,
}
impl Caps {
    pub fn positive(&self) -> bool {
        self.storage_bytes > 0 && self.memory_bytes > 0 && self.wall_ms > 0 && self.commands > 0
    }
    pub fn contains(&self, other: &Self) -> bool {
        other.positive()
            && other.storage_bytes <= self.storage_bytes
            && other.memory_bytes <= self.memory_bytes
            && other.wall_ms <= self.wall_ms
            && other.commands <= self.commands
    }
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Binding {
    pub swarm_id: String,
    pub node_id: String,
    pub seed_key_digest: String,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Consent {
    pub disclosure_digest: String,
    pub caps: Caps,
    pub binding: Binding,
    pub granted_at: u64,
    pub withdrawn_at: Option<u64>,
}
/// Caller supplies L1's standing-consent range policy. No invented defaults while
/// those registry IDs are unmerged. Device memory ceiling is already derived by host.
pub struct ConsentBounds {
    pub minimum_storage: Bytes,
    pub policy_storage: Bytes,
    pub minimum_memory: Bytes,
    pub maximum_memory: Bytes,
    pub device_memory_ceiling: Bytes,
    pub minimum_wall: Span,
    pub maximum_wall: Span,
    pub maximum_commands: Entries,
}
impl ConsentBounds {
    pub fn validate(&self, caps: &Caps) -> Result<()> {
        if !caps.positive()
            || caps.storage_bytes < self.minimum_storage.get()
            || caps.storage_bytes > self.policy_storage.get()
            || caps.memory_bytes < self.minimum_memory.get()
            || caps.memory_bytes > self.maximum_memory.get()
            || caps.wall_ms < self.minimum_wall.millis()
            || caps.wall_ms > self.maximum_wall.millis()
            || caps.commands > self.maximum_commands.get()
        {
            return Err(Error::Refused("standing_consent_caps_invalid"));
        }
        if caps.memory_bytes > self.device_memory_ceiling.get() {
            return Err(Error::Refused("standing_consent_memory_exceeds_device"));
        }
        Ok(())
    }
}
impl Consent {
    pub fn record(
        acknowledged: bool,
        digest: &str,
        caps: Caps,
        binding: Binding,
        bounds: &ConsentBounds,
        now: u64,
    ) -> Result<Self> {
        if !acknowledged || digest != disclosure_digest() {
            return Err(Error::Refused("standing_consent_required"));
        }
        if [
            &binding.swarm_id,
            &binding.node_id,
            &binding.seed_key_digest,
        ]
        .iter()
        .any(|s| s.is_empty())
        {
            return Err(Error::Refused("standing_consent_binding_invalid"));
        }
        bounds.validate(&caps)?;
        Ok(Self {
            disclosure_digest: disclosure_digest(),
            caps,
            binding,
            granted_at: now,
            withdrawn_at: None,
        })
    }
}
