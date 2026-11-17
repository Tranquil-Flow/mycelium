// SPDX-License-Identifier: AGPL-3.0-only
//! `endpoint_recovery.py` @ f4e366e9 (lane B, B10): explicit signed relocation of a retained seed,
//! never a key or membership reset. An owner whose relay comes back on a new origin signs a short
//! card from the old origin to the new one with its seed key ([`endpoint_card`], :12-18); a joined
//! Mac given that card checks it against its own membership ([`verify_endpoint_card`], :21-36),
//! asks the same seed at the new address for its identity, and only then re-pins its membership
//! there and resumes it ([`reconnect_peer`], :89-114). [`refresh_endpoint_card`] reissues the card
//! for the current origin with a fresh short validity (:117-142).
//!
//! A `ValueError` of the source (every refusal of a card, `endpoint_unchanged`,
//! `endpoint_seed_identity_mismatch`, ...) is a builtin class: the handler answers its generic
//! `action_not_completed` (`public_failure_code`, app.py:344-351).
use crate::{
    app_host::{Cell, now},
    preferences::private_state,
    schema::{PublicError, Raised},
    seed::{SeedClient, SeedError, SeedOptions},
};
use protocol::{canonical, identity::Identity, origin::canonical_https_origin};
use serde_json::{Map, Value, json};
use std::path::Path;

/// `_PROTOCOL`.
pub const PROTOCOL: &str = "mycelium.a13.endpoint_recovery.v1";
/// The card as persisted beside the owner's network configuration and the member's membership.
pub const CARD_FILE: &str = "endpoint-recovery.json";
/// The evidence a failed identity check at the new address leaves (endpoint_recovery.py:98-107).
pub const DIAGNOSTIC_FILE: &str = "endpoint-recovery-diagnostic.json";
const DIAGNOSTIC_PROTOCOL: &str = "mycelium.a13.endpoint_recovery_diagnostic.v1";
/// `_FIELDS`.
const FIELDS: [&str; 8] = [
    "protocol",
    "swarm_id",
    "seed_node_id",
    "seed_key_digest",
    "previous_origin",
    "public_origin",
    "issued_at",
    "expires_at",
];
/// `'expires_at': now + 300`, and the most a card may live.
const LIFETIME: f64 = 300.0;
/// `issued_at - 30 <= now`.
const SKEW: f64 = 30.0;
/// `len(canonical_json_bytes(card)) > 8192`.
const MAX_BYTES: usize = 8192;

/// A builtin `ValueError` escaping to the handler: 409 `action_not_completed`.
pub fn value_error() -> PublicError {
    PublicError::new(409, "action_not_completed").raised_by(Raised::Builtin("ValueError"))
}

fn canonical_origin(origin: &Value) -> Result<String, PublicError> {
    canonical_https_origin(origin).map_err(|_| value_error())
}

/// `endpoint_card` (endpoint_recovery.py:12-18): the seed's signed statement that it moved from
/// `previous_origin` to `public_origin`, valid for 300 s from `now`.
pub fn endpoint_card(
    signer: &Identity,
    swarm_id: &str,
    seed_node_id: &str,
    previous_origin: &Value,
    public_origin: &Value,
    now: f64,
) -> Result<Value, PublicError> {
    let previous = canonical_origin(previous_origin)?;
    let new = canonical_origin(public_origin)?;
    if previous == new || !now.is_finite() {
        // `endpoint_unchanged`
        return Err(value_error());
    }
    let statement = json!({
        "protocol": PROTOCOL, "swarm_id": swarm_id, "seed_node_id": seed_node_id,
        "seed_key_digest": signer.verification_key_digest(), "previous_origin": previous,
        "public_origin": new, "issued_at": now, "expires_at": now + LIFETIME,
    });
    let signature = signer.sign(&statement).map_err(|_| value_error())?;
    Ok(json!({"statement": statement, "signature": signature}))
}

/// `verify_endpoint_card` (endpoint_recovery.py:21-36): the card's new origin when it is this
/// membership's seed's own, unexpired card from or to the address the membership has; anything
/// else is `endpoint_recovery_invalid` (a `ValueError`).
pub fn verify_endpoint_card(
    card: &Value,
    membership: &Map<String, Value>,
    now: f64,
) -> Result<String, PublicError> {
    checked(card, membership, now).ok_or_else(value_error)
}
fn checked(card: &Value, membership: &Map<String, Value>, now: f64) -> Option<String> {
    let outer = card.as_object()?;
    if outer.len() != 2 || !outer.contains_key("statement") || !outer.contains_key("signature") {
        return None;
    }
    if canonical::to_bytes(card).ok()?.len() > MAX_BYTES {
        return None;
    }
    let s = outer["statement"].as_object()?;
    if s.len() != FIELDS.len() || !FIELDS.iter().all(|f| s.contains_key(*f)) {
        return None;
    }
    if s["protocol"] != PROTOCOL {
        return None;
    }
    for key in ["swarm_id", "seed_node_id", "seed_key_digest"] {
        if &s[key] != membership.get(key)? {
            return None;
        }
    }
    let old = canonical_https_origin(&s["previous_origin"]).ok()?;
    let new = canonical_https_origin(&s["public_origin"]).ok()?;
    let seed_url = membership.get("seed_url")?;
    if old == new || (seed_url != old.as_str() && seed_url != new.as_str()) {
        return None;
    }
    // `type(t) in (int, float)`: a JSON number (never a bool), and finite.
    let time = |v: &Value| v.as_f64().filter(|t| v.is_number() && t.is_finite());
    let (issued, expires) = (time(&s["issued_at"])?, time(&s["expires_at"])?);
    let fresh = now.is_finite() && issued - SKEW <= now && now < expires;
    let bounded = 0.0 < expires - issued && expires - issued <= LIFETIME;
    if !(fresh && bounded) {
        return None;
    }
    let signature = outer["signature"].as_object()?;
    let digest = membership.get("seed_key_digest")?;
    if signature.get("verification_key_digest") != Some(digest) {
        return None;
    }
    // `build_ed25519_verifier(membership['seed_key_records'])`: every record must load; the
    // signature is checked with the one its digest names.
    let records = membership.get("seed_key_records")?.as_array()?;
    let mut key = None;
    for record in records {
        let loaded = protocol::identity::key_record_digest(record).ok()?;
        if Some(&Value::from(loaded)) == Some(digest) {
            key = Some(record);
        }
    }
    let statement = Value::Object(s.clone());
    protocol::identity::verify(&statement, &outer["signature"], key?).ok()?;
    Some(new)
}

/// `refresh_endpoint_card` (endpoint_recovery.py:117-142): the owner's persisted card, checked at
/// its own issue time against this seed, still naming the origin the seed answers on now, is
/// reissued with a fresh validity (from `previous_origin` when the owner names another old one).
pub fn refresh_endpoint_card(
    signer: &Identity,
    swarm_id: &str,
    seed_node_id: &str,
    configured_origin: &str,
    old: &Value,
    previous_origin: Option<&Value>,
    now: f64,
) -> Result<Value, PublicError> {
    let membership = json!({
        "swarm_id": swarm_id, "seed_node_id": seed_node_id, "seed_url": configured_origin,
        "seed_key_digest": signer.verification_key_digest(),
        "seed_key_records": [signer.public_key_record()],
    });
    let issued = old["statement"]["issued_at"]
        .as_f64()
        .ok_or_else(value_error)?;
    verify_endpoint_card(old, membership.as_object().ok_or_else(value_error)?, issued)?;
    if old["statement"]["public_origin"] != configured_origin {
        // `endpoint_recovery_origin_changed`
        return Err(value_error());
    }
    let previous = match previous_origin {
        None => old["statement"]["previous_origin"].clone(),
        Some(origin) => Value::from(canonical_origin(origin)?),
    };
    endpoint_card(
        signer,
        swarm_id,
        seed_node_id,
        &previous,
        &Value::from(configured_origin),
        now,
    )
}

/// The private-state cap the app's own JSON files use.
fn store(path: &Path, document: &Value) -> Result<(), PublicError> {
    let limit = private_state::private_json_limit().map_err(|_| value_error())?;
    private_state::store(path, document, limit)
        .map_err(|e| PublicError::new(409, e.0).raised_by(Raised::Authority))
}
/// Persist the card the owner just signed or reissued (`_atomic_private_json`).
pub fn store_card(root: &Path, card: &Value) -> Result<(), PublicError> {
    store(&root.join(CARD_FILE), card)
}
/// The owner's persisted card, if any (`_load_private_json`).
pub fn load_card(root: &Path) -> Result<Option<Value>, PublicError> {
    let limit = private_state::private_json_limit().map_err(|_| value_error())?;
    private_state::load(&root.join(CARD_FILE), limit)
        .map(|card| card.map(Value::Object))
        .map_err(|e| PublicError::new(409, e.0).raised_by(Raised::Authority))
}

/// `SeedHTTPError` escaping `reconnect_peer` (a `mycelium_seed` class): its own closed code is
/// the public one; an `http.client` class escapes as `action_not_completed`.
fn seed_failure(error: &SeedError) -> PublicError {
    if let Some(class) = error.raw {
        return PublicError::new(409, "action_not_completed").raised_by(Raised::HttpClient(class));
    }
    let failure = PublicError::new(409, intern(&error.code));
    match error.window {
        Some(window) => failure.with_window(window),
        None => failure,
    }
}
fn intern(code: &str) -> &'static str {
    use std::{collections::HashSet, sync::Mutex};
    static CODES: Mutex<Option<HashSet<&'static str>>> = Mutex::new(None);
    let mut guard = CODES
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let set = guard.get_or_insert_with(HashSet::new);
    if let Some(known) = set.get(code) {
        return known;
    }
    let leaked: &'static str = Box::leak(code.to_owned().into_boxed_str());
    set.insert(leaked);
    leaked
}

/// `reconnect_peer` (endpoint_recovery.py:89-114) with the app's `/api/network/reconnect` around
/// it (app.py:1427-1435): the card is checked against this Mac's membership, the same seed must
/// answer at the new address with its own identity, then the membership stops, is re-pinned to the
/// new address with the card kept beside it, and starts again (a resume, generation + 1). Only a
/// resumed membership makes the Mac running again and clears its `recovery_error`
/// ([`crate::joined_member::start_membership`]).
pub fn reconnect_peer(
    cell: &impl Cell,
    options: &SeedOptions,
    card: &Value,
) -> Result<Value, PublicError> {
    let root = cell.with(|c| c.root.clone());
    let Some(membership) = crate::peer::load_membership_state(&root)? else {
        // `endpoint_recovery_requires_membership`
        return Err(value_error());
    };
    let new = verify_endpoint_card(card, &membership, now())?;
    let text = |key: &str| membership[key].as_str().unwrap_or_default().to_owned();
    let records: Vec<Value> = membership["seed_key_records"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let client = SeedClient::new(
        &new,
        &text("swarm_id"),
        &text("seed_key_digest"),
        &records,
        options,
    )
    .map_err(|e| seed_failure(&e))?;
    let answered = match client.identity(&now) {
        Ok(identity) => identity,
        Err(error) => {
            let window = error.window.as_ref().map_or(
                Value::Null,
                |w| json!({"now": w.now, "issued_at": w.issued_at, "expires_at": w.expires_at}),
            );
            store(
                &root.join(DIAGNOSTIC_FILE),
                &json!({"protocol": DIAGNOSTIC_PROTOCOL, "code": error.code,
                        "public_origin": new, "peer_now": now(), "window": window}),
            )
            .unwrap_or(());
            return Err(seed_failure(&error));
        }
    };
    if answered["seed_url"] != new.as_str()
        || answered["swarm_id"] != membership["swarm_id"]
        || answered["seed_node_id"] != membership["seed_node_id"]
    {
        // `endpoint_seed_identity_mismatch`
        return Err(value_error());
    }
    // `peer.stop()`: no live member; a renewal still on the wire never puts its Session back.
    cell.with(|c| {
        c.session = None;
        c.session_view = None;
        c.member_flight = None;
        c.running = false;
    });
    crate::peer::repin_owner_seed_url(&root, &new)?;
    store(&root.join(CARD_FILE), card)?;
    crate::joined_member::start_membership(cell, options)?;
    let live = cell.with(|c| {
        c.session
            .as_ref()
            .map(|s| (s.generation(), s.lease_expires_at()))
    });
    let (state, generation, lease) = crate::peer::status_membership(&root, live, now());
    Ok(
        json!({"membership_state": state, "membership_generation": generation,
              "lease_state": lease, "seed_url": new}),
    )
}
