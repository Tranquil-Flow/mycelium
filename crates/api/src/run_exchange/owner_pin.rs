// SPDX-License-Identifier: AGPL-3.0-only
//! The owner pin of the owner-signed run announcement (PH3B U2; contract: plans/S4B-PLAN.md §0).
//!
//! An ADDITIVE, versioned key [`FIELD`] of `mycelium.a13_demo_run_announcement.v1`, present only
//! when the owner sets it ([`super::RunExchange::set_owner_control`]); without it the announcement
//! keeps its exact bytes. Its value is closed:
//! `{"protocol": "mycelium.a13_demo_owner_control.v1", "endpoint_id": "<owner control endpoint id>"}`
//! and carries exactly what `shard_member --owner-control` carries: the Ed25519 key of the owner's
//! member-control (NodeControl) endpoint. It carries NO address; a member never trusts an address,
//! only this key, and only from an announcement that reached it inside the seed-signed read
//! response it verified against the seed key pinned at join ([`VerifiedAnnouncement`]).
//!
//! Member checks, in order (each refusal names one code of [`REFUSALS`]):
//! 1. the read response verifies (pinned seed key, request, swarm, owner node, this member's
//!    binding, window) → else `owner_pin_announcement_unverified`;
//! 2. a closed response with a known announcement protocol and phase → else
//!    `demo_announcement_invalid`;
//! 3. the key is present → else `owner_pin_missing`;
//! 4. an object whose `protocol` is a string → else `owner_pin_invalid`; that protocol is
//!    [`PROTOCOL`] → else `owner_pin_version_unsupported`;
//! 5. exactly `{protocol, endpoint_id}` → else `owner_pin_invalid`;
//! 6. `endpoint_id` parses as an endpoint id and re-prints to the same text → else
//!    `owner_pin_endpoint_invalid`.
use crate::run_exchange_client::{ClientError, ExchangeClient};
use serde_json::{Value, json};

/// The announcement key.
pub const FIELD: &str = "owner_control";
/// The field's version tag.
pub const PROTOCOL: &str = "mycelium.a13_demo_owner_control.v1";
/// Every refusal of the member-side check, in check order.
pub const REFUSALS: [&str; 6] = [
    "owner_pin_announcement_unverified",
    "demo_announcement_invalid",
    "owner_pin_missing",
    "owner_pin_invalid",
    "owner_pin_version_unsupported",
    "owner_pin_endpoint_invalid",
];
const UNIMPLEMENTED: &str = "owner_pin_unimplemented";

/// What the owner publishes: its member-control endpoint key.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OwnerControl {
    endpoint: iroh::EndpointId,
}
impl OwnerControl {
    pub fn new(endpoint: iroh::EndpointId) -> Self {
        Self { endpoint }
    }
    pub fn endpoint(&self) -> iroh::EndpointId {
        self.endpoint
    }
    /// The field's exact value.
    pub fn to_value(&self) -> Value {
        json!({"protocol": PROTOCOL, "endpoint_id": self.endpoint.to_string()})
    }
}

/// A closed refusal code of the member-side check.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PinRefusal(pub &'static str);

/// A run announcement that reached this member inside a seed-signed read response it verified
/// against the seed key pinned at join. No other constructor exists.
#[derive(Clone, Debug)]
pub struct VerifiedAnnouncement {
    announcement: Value,
    seed_key_digest: String,
}
impl VerifiedAnnouncement {
    /// One signed read (`ExchangeClient::read`, which verifies before it answers).
    pub fn read(client: &mut ExchangeClient) -> Result<Self, ClientError> {
        let _ = client;
        Err(ClientError(UNIMPLEMENTED.into()))
    }
    /// A read response this member already received for `request_id`.
    pub fn from_response(
        client: &ExchangeClient,
        response: &Value,
        request_id: &str,
    ) -> Result<Self, PinRefusal> {
        let _ = (client, response, request_id);
        Err(PinRefusal(UNIMPLEMENTED))
    }
    pub fn announcement(&self) -> &Value {
        &self.announcement
    }
    /// Digest of the seed key the response verified against.
    pub fn seed_key_digest(&self) -> &str {
        &self.seed_key_digest
    }
}

/// The owner control key a member obeys, taken from a verified announcement.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OwnerPin {
    control: OwnerControl,
    seed_key_digest: String,
}
impl OwnerPin {
    /// Checks 3-6 of the module contract on a verified announcement.
    pub fn from_announcement(verified: &VerifiedAnnouncement) -> Result<Self, PinRefusal> {
        let _ = verified;
        Err(PinRefusal(UNIMPLEMENTED))
    }
    pub fn endpoint(&self) -> iroh::EndpointId {
        self.control.endpoint
    }
    /// The seed key the pin's announcement was verified against.
    pub fn seed_key_digest(&self) -> &str {
        &self.seed_key_digest
    }
}
