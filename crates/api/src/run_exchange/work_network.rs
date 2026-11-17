// SPDX-License-Identifier: AGPL-3.0-only
//! Lane XB patch 2 (decision D1, REVIEW XB-03/04/06): the run's network reach, chosen by the
//! OWNER for each run and carried SIGNED to the joined Macs in the run announcement they already
//! verify before binding their carriers; a joined Mac binds in exactly the mode the owner signed.
//!
//! An ADDITIVE, versioned key [`FIELD`] of `mycelium.a13_demo_run_announcement.v1`, published
//! only for a run across networks ([`super::RunExchange::set_work_network`]); without it (every
//! same-network run) the announcement keeps its exact bytes and means `same_network`. Its value
//! is closed: `{"protocol": "mycelium.a13_work_network.v1", "reach": "relay", "relay_url":
//! "<the relay both Macs register at>"}`. It carries no address. A member never trusts the relay
//! URL merely because it is signed: it must be the member's own configured relay (D4), or the
//! member refuses by name ([`RELAY_MISMATCH`]) before binding anything.
//!
//! Member checks (each refusal names one code):
//! 1. key absent → [`RunReach::SameNetwork`];
//! 2. an object whose `protocol` is a string → else [`INVALID`]; that protocol is [`PROTOCOL`]
//!    → else [`VERSION_UNSUPPORTED`];
//! 3. exactly `{protocol, reach, relay_url}`, `reach` = `relay`, `relay_url` a relay URL that
//!    re-prints to the same text → else [`INVALID`].
use super::owner_pin::VerifiedAnnouncement;
use crate::wire::Json;
use serde_json::Value;

/// The announcement key.
pub const FIELD: &str = "work_network";
/// The field's version tag.
pub const PROTOCOL: &str = "mycelium.a13_work_network.v1";
/// A malformed field.
pub const INVALID: &str = "work_network_invalid";
/// A field of another version.
pub const VERSION_UNSUPPORTED: &str = "work_network_version_unsupported";
/// The signed relay is not this Mac's configured relay (D4: only our relay).
pub const RELAY_MISMATCH: &str = "work_network_relay_mismatch";

/// The owner's choice for one run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RunReach {
    /// "Macs on the same network": the direct-LAN carrier, no relay (the default).
    SameNetwork,
    /// "Macs on different networks": both Macs register at this relay.
    Relay(iroh::RelayUrl),
}
impl RunReach {
    /// `same_network` / `relay`, as status names it.
    pub fn name(&self) -> &'static str {
        match self {
            Self::SameNetwork => "same_network",
            Self::Relay(_) => "relay",
        }
    }
    /// The field's exact value, in its published key order; `None` for the same network (the
    /// key is not published at all).
    pub fn to_json(&self) -> Option<Json> {
        match self {
            Self::SameNetwork => None,
            Self::Relay(url) => Some(Json::Obj(vec![
                ("protocol".into(), PROTOCOL.into()),
                ("reach".into(), "relay".into()),
                ("relay_url".into(), url.to_string().as_str().into()),
            ])),
        }
    }
    /// The checks of the module contract on a verified announcement.
    pub fn from_announcement(verified: &VerifiedAnnouncement) -> Result<Self, &'static str> {
        Self::from_value(verified.announcement())
    }
    /// [`RunReach::from_announcement`] on the announcement document itself.
    pub fn from_value(announcement: &Value) -> Result<Self, &'static str> {
        let Some(field) = announcement.get(FIELD) else {
            return Ok(Self::SameNetwork);
        };
        let map = field.as_object().ok_or(INVALID)?;
        let protocol = map.get("protocol").and_then(Value::as_str).ok_or(INVALID)?;
        if protocol != PROTOCOL {
            return Err(VERSION_UNSUPPORTED);
        }
        if map.len() != 3 || map.get("reach").and_then(Value::as_str) != Some("relay") {
            return Err(INVALID);
        }
        let text = map
            .get("relay_url")
            .and_then(Value::as_str)
            .ok_or(INVALID)?;
        let url: iroh::RelayUrl = text.parse().map_err(|_| INVALID)?;
        // One encoding only: the text must be the URL's own display form.
        if url.to_string() != text {
            return Err(INVALID);
        }
        Ok(Self::Relay(url))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn relay() -> iroh::RelayUrl {
        "https://relay.mycelium.now".parse().unwrap()
    }

    /// The field round-trips, a same-network run publishes nothing, and every malformed or
    /// foreign-version field is refused by name.
    #[test]
    fn the_signed_reach_is_closed_and_versioned() {
        let doc = |field: Value| json!({"protocol": "x", FIELD: field});
        let published = RunReach::Relay(relay()).to_json().unwrap().to_value();
        assert_eq!(
            published,
            json!({"protocol": PROTOCOL, "reach": "relay", "relay_url": "https://relay.mycelium.now/"})
        );
        assert_eq!(
            RunReach::from_value(&doc(published.clone())),
            Ok(RunReach::Relay(relay()))
        );
        assert_eq!(RunReach::SameNetwork.to_json(), None);
        assert_eq!(
            RunReach::from_value(&json!({"protocol": "x"})),
            Ok(RunReach::SameNetwork)
        );
        for (bad, code) in [
            (json!("relay"), INVALID),
            (json!({"reach": "relay"}), INVALID),
            (json!({"protocol": "mycelium.a13_work_network.v2", "reach": "relay", "relay_url": "https://relay.mycelium.now/"}), VERSION_UNSUPPORTED),
            (json!({"protocol": PROTOCOL, "reach": "same_network", "relay_url": "https://relay.mycelium.now/"}), INVALID),
            (json!({"protocol": PROTOCOL, "reach": "relay"}), INVALID),
            (json!({"protocol": PROTOCOL, "reach": "relay", "relay_url": 7}), INVALID),
            (json!({"protocol": PROTOCOL, "reach": "relay", "relay_url": "not a url"}), INVALID),
            (json!({"protocol": PROTOCOL, "reach": "relay", "relay_url": "https://relay.mycelium.now"}), INVALID),
            (json!({"protocol": PROTOCOL, "reach": "relay", "relay_url": "https://relay.mycelium.now/", "extra": 1}), INVALID),
        ] {
            assert_eq!(RunReach::from_value(&doc(bad.clone())), Err(code), "{bad}");
        }
    }
}
