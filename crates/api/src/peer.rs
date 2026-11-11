// SPDX-License-Identifier: AGPL-3.0-only
//! Target-side retained acceptance: the first half of `RemoteMacMember.accept_handoff`
//! (`mycelium_a13_macos/peer.py:740-775` @ f4e366e9) that decides from the state files alone whether
//! a handoff was already accepted, replaced a lapsed membership, or must go on to the seed join; the
//! membership projection of `status()` that answers a retained acceptance; and
//! `durable_membership.load_membership_state` (the restart credential, validated field by field).
//!
//! Round 11 adds the seed half (`accept_handoff`): persist and decrypt the handoff, pin the seed
//! (`api::seed` HTTPS `identity`), checkpoint and send one `join` (`api::member::Session`), save the
//! restart credential and the acceptance, and keep the live session (`membership_binding()`, the live
//! `status()` membership projection). Not here: `_resume` on start.
use crate::{
    handoff,
    member::{Binding, MemberError, Session},
    preferences::private_state,
    schema::{PublicError, Raised},
    seed::{SeedClient, SeedError, SeedOptions},
    wire::Json,
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use protocol::identity::Identity;
use serde_json::{Map, Value, json};
use std::{
    fs,
    io::Read,
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::Path,
};

const ACCEPTED_FILE: &str = "accepted.json";
const REJOIN_FILE: &str = "rejoin.pending.json";
const JOIN_PENDING_FILE: &str = "join.pending.json";
const CONSENT_FILE: &str = "consent.json";
const DISPOSITION_FILE: &str = "membership.disposition.json";
const MEMBERSHIP_FILE: &str = "membership.state.json";
const ACCEPTED_PROTOCOL: &str = "mycelium.remote_mac_accepted.v1";
const CONSENT_PROTOCOL: &str = "mycelium.remote_mac_consent.v1";
const DISPOSITION_PROTOCOL: &str = "mycelium.remote_mac_membership_disposition.v1";
const RESULT_PROTOCOL: &str = "mycelium.remote_mac_membership_result.v1";
const MEMBERSHIP_PROTOCOL: &str = "mycelium.node.membership_state.v1";
const ACCEPTED_FIELDS: [&str; 4] = ["protocol", "handoff_digest", "quota", "accepted_at"];
const MEMBERSHIP_FIELDS: [&str; 11] = [
    "protocol",
    "node_id",
    "swarm_id",
    "seed_node_id",
    "seed_url",
    "seed_key_digest",
    "seed_key_records",
    "endpoint_id",
    "incarnation",
    "membership_generation",
    "restart_count",
];
const TERMINAL_STATES: [&str; 4] = ["expired", "revoked", "stale", "unknown"];

fn peer(code: &'static str) -> PublicError {
    PublicError::new(409, code).raised_by(Raised::Peer)
}
fn authority(error: private_state::StateError) -> PublicError {
    PublicError::new(409, error.0).raised_by(Raised::Authority)
}
fn durable(code: &'static str) -> PublicError {
    PublicError::new(409, code).raised_by(Raised::DurableMembership)
}

/// `_MAX_PRIVATE_JSON_BYTES` (authority.py:96), from the budget registry.
fn private_limit() -> Result<u64, PublicError> {
    private_state::private_json_limit().map_err(authority)
}
/// `durable_membership._MAX_BYTES`, from the budget registry.
fn membership_limit() -> Result<u64, PublicError> {
    budgets::BudgetId::PyMyceliumNodeDurableMembershipMaxBytesL41
        .bytes()
        .map(|b| b.get())
        .map_err(|_| durable("node_membership_state_unavailable"))
}

/// What the state files say about an incoming handoff.
#[derive(Debug, PartialEq)]
pub enum Retained {
    /// Already accepted: the `_membership_result()` the source answers.
    Accepted(Json),
    /// Nothing retained for this handoff: persist it, decrypt it and join the seed.
    Continue,
}

fn is_segment(text: &str) -> bool {
    // `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`
    let mut chars = text.chars();
    chars.next().is_some_and(|c| c.is_ascii_alphanumeric())
        && text.len() <= 128
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}
fn is_sha256_digest(text: &str) -> bool {
    // `^sha256:[0-9a-f]{64}$`
    text.strip_prefix("sha256:").is_some_and(|hex| {
        hex.len() == 64 && hex.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
    })
}
/// A Python `int` that is not a `bool` and at least `minimum`.
fn integer_at_least(value: &Value, minimum: i64) -> bool {
    match value {
        Value::Number(n) => n
            .as_i64()
            .map_or_else(|| n.as_u64().is_some(), |i| i >= minimum),
        _ => false,
    }
}

/// `durable_membership._validate` on an already parsed object.
fn validate_membership(value: &Map<String, Value>) -> Result<(), PublicError> {
    let invalid = || durable("node_membership_state_invalid");
    if value.len() != MEMBERSHIP_FIELDS.len()
        || !MEMBERSHIP_FIELDS.iter().all(|k| value.contains_key(*k))
        || value["protocol"] != MEMBERSHIP_PROTOCOL
    {
        return Err(invalid());
    }
    for field in [
        "node_id",
        "swarm_id",
        "seed_node_id",
        "endpoint_id",
        "incarnation",
    ] {
        if !value[field].as_str().is_some_and(is_segment) {
            return Err(invalid());
        }
    }
    let url_ok = value["seed_url"]
        .as_str()
        .is_some_and(|u| !u.is_empty() && u == u.trim());
    let digest = value["seed_key_digest"]
        .as_str()
        .filter(|d| is_sha256_digest(d));
    let (true, Some(digest)) = (url_ok, digest) else {
        return Err(invalid());
    };
    let records_ok = value["seed_key_records"].as_array().is_some_and(|records| {
        records.len() == 1
            && records[0].as_object().is_some_and(|record| {
                record
                    .get("verification_key_digest")
                    .and_then(Value::as_str)
                    == Some(digest)
            })
    });
    if !records_ok
        || !integer_at_least(&value["membership_generation"], 1)
        || !integer_at_least(&value["restart_count"], 0)
    {
        return Err(invalid());
    }
    Ok(())
}

/// `load_membership_state`: absent => `None`; anything but an owner-only (0600), single-link, regular,
/// canonical JSON object of at most 64 KiB with exactly the credential's fields is
/// `node_membership_state_invalid`; a file that cannot be read at all is `..._unavailable`.
pub fn load_membership_state(root: &Path) -> Result<Option<Map<String, Value>>, PublicError> {
    let path = root.join(MEMBERSHIP_FILE);
    let invalid = || durable("node_membership_state_invalid");
    let unavailable = || durable("node_membership_state_unavailable");
    let max = membership_limit()?;
    let meta = match fs::symlink_metadata(&path) {
        Ok(meta) => meta,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(unavailable()),
    };
    if !meta.file_type().is_file()
        || meta.nlink() != 1
        || meta.uid() != rustix::process::getuid().as_raw()
        || meta.mode() & 0o7777 != 0o600
        || meta.len() == 0
        || meta.len() > max
    {
        return Err(invalid());
    }
    let mut file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(0x0100) // O_NOFOLLOW
        .open(&path)
        .map_err(|_| unavailable())?;
    let opened = file.metadata().map_err(|_| unavailable())?;
    if (opened.dev(), opened.ino()) != (meta.dev(), meta.ino()) {
        return Err(invalid());
    }
    let mut raw = Vec::new();
    (&mut file)
        .take(max + 1)
        .read_to_end(&mut raw)
        .map_err(|_| unavailable())?;
    let Ok(Value::Object(value)) = serde_json::from_slice::<Value>(&raw) else {
        return Err(invalid());
    };
    if protocol::canonical::to_bytes(&Value::Object(value.clone()))
        .ok()
        .as_deref()
        != Some(raw.as_slice())
    {
        return Err(invalid());
    }
    validate_membership(&value)?;
    Ok(Some(value))
}

fn load_accepted(root: &Path) -> Result<Option<Map<String, Value>>, PublicError> {
    let Some(value) =
        private_state::load(&root.join(ACCEPTED_FILE), private_limit()?).map_err(authority)?
    else {
        return Ok(None);
    };
    if value.len() != ACCEPTED_FIELDS.len()
        || !ACCEPTED_FIELDS.iter().all(|k| value.contains_key(*k))
        || value["protocol"] != ACCEPTED_PROTOCOL
    {
        return Err(peer("peer_state_invalid"));
    }
    Ok(Some(value))
}

/// `_load_consent`: the stored consent must be well formed; only its validity matters here.
fn check_consent(root: &Path) -> Result<(), PublicError> {
    let Some(value) =
        private_state::load(&root.join(CONSENT_FILE), private_limit()?).map_err(authority)?
    else {
        return Ok(());
    };
    let state_invalid = || peer("peer_state_invalid");
    if value.len() != 2 || value.get("protocol") != Some(&json!(CONSENT_PROTOCOL)) {
        return Err(state_invalid());
    }
    let consent = value
        .get("consent")
        .and_then(Value::as_object)
        .ok_or_else(state_invalid)?;
    let fields = [
        "cooperative_peer_visibility_acknowledged",
        "resource_use_acknowledged",
        "contribution_enabled",
    ];
    let bools = consent.len() == fields.len()
        && fields
            .iter()
            .all(|f| matches!(consent.get(*f), Some(Value::Bool(_))));
    if !bools
        || consent["cooperative_peer_visibility_acknowledged"] != true
        || consent["resource_use_acknowledged"] != true
    {
        return Err(state_invalid());
    }
    Ok(())
}

/// `_load_disposition`: a recorded terminal membership state, if any.
fn load_disposition(root: &Path) -> Result<Option<String>, PublicError> {
    let Some(value) =
        private_state::load(&root.join(DISPOSITION_FILE), private_limit()?).map_err(authority)?
    else {
        return Ok(None);
    };
    let state = value.get("membership_state").and_then(Value::as_str);
    let observed_ok = matches!(value.get("observed_at"), Some(Value::Number(_)));
    if value.len() != 3
        || value.get("protocol") != Some(&json!(DISPOSITION_PROTOCOL))
        || !state.is_some_and(|s| TERMINAL_STATES.contains(&s))
        || !observed_ok
    {
        return Err(peer("peer_state_invalid"));
    }
    Ok(state.map(str::to_owned))
}

/// `_membership_result()` = the membership part of `status()` with no live member session.
fn membership_result(root: &Path) -> Result<Json, PublicError> {
    check_consent(root)?;
    let membership = load_membership_state(root)?;
    load_accepted(root)?;
    let disposition = load_disposition(root)?;
    let (mut state, generation, mut lease) = match &membership {
        Some(m) => (
            "retained".to_owned(),
            Json::from(m["membership_generation"].clone()),
            "unknown",
        ),
        None => ("absent".to_owned(), Json::Null, "none"),
    };
    if let Some(terminal) = disposition {
        if terminal == "expired" {
            lease = "expired";
        }
        state = terminal;
    }
    Ok(crate::jobj! {
        "protocol" => RESULT_PROTOCOL, "membership_state" => state.as_str(),
        "membership_generation" => generation, "lease_state" => lease,
        "serving_state" => "not_yet_serving", "route_ready" => false,
    })
}

/// The part of `accept_handoff` that precedes the seed: `started`, handoff shape, the retained
/// acceptance (same handoff => its membership result; another => `pairing_handoff_changed`, unless a
/// lapsed membership is being replaced), and the join checkpoint of another handoff.
pub fn accept_retained(
    root: &Path,
    started: bool,
    handoff: &Value,
) -> Result<Retained, PublicError> {
    if !started {
        return Err(peer("peer_not_started"));
    }
    if !handoff.is_object() {
        return Err(peer("pairing_handoff_invalid"));
    }
    let digest =
        protocol::canonical::digest(handoff).map_err(|_| peer("pairing_handoff_invalid"))?;
    let mut accepted = load_accepted(root)?;
    let rejoin = root.join(REJOIN_FILE);
    if accepted.is_some()
        && rejoin.exists()
        && accepted
            .as_ref()
            .is_some_and(|a| a["handoff_digest"] != digest)
    {
        accepted = None; // a new invite replaces the lapsed membership; the old one stays until it commits
    }
    if let Some(accepted) = accepted {
        if accepted["handoff_digest"] != digest {
            return Err(peer("pairing_handoff_changed"));
        }
        if load_membership_state(root)?.is_none() {
            return Err(peer("peer_membership_state_invalid"));
        }
        match fs::remove_file(&rejoin) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => {
                return Err(crate::preferences::os_error(&e));
            }
            _ => {}
        }
        return membership_result(root).map(Retained::Accepted);
    }
    let checkpoint =
        private_state::load(&root.join(JOIN_PENDING_FILE), private_limit()?).map_err(authority)?;
    if checkpoint.is_some_and(|c| c.get("handoff_digest") != Some(&Value::from(digest))) {
        return Err(peer("pairing_handoff_changed"));
    }
    Ok(Retained::Continue)
}

/// What `accept_handoff` needs from the host: the target's signer, its clock, the source's two
/// random draws (`secrets.token_hex(n)` for the incarnation, `uuid4().hex` for the join message
/// id) and how to reach the seed.
pub struct JoinEnv<'a> {
    pub signer: &'a Identity,
    pub clock: &'a dyn Fn() -> f64,
    pub random_hex: &'a dyn Fn(usize) -> String,
    pub seed: &'a SeedOptions,
}

/// The answer of `accept_handoff` and the live session a first acceptance leaves behind
/// (`self._member`); `None` for a retained acceptance.
#[derive(Debug)]
pub struct Accepted {
    pub answer: Json,
    pub session: Option<Session>,
}

const HANDOFF_FILE: &str = "pairing.handoff.json";
const PENDING_FILE: &str = "pairing.pending.json";
const PENDING_PROTOCOL: &str = "mycelium.remote_mac_pairing_pending.v1";
const JOIN_PENDING_PROTOCOL: &str = "mycelium.remote_mac_join_pending.v1";

/// Failure codes leave the process as `&'static str`; each distinct remote code is interned once.
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

/// `RemoteMacMember._translate`: the public code, and a terminal membership disposition is recorded.
fn translate(root: &Path, code: &str, now: f64) -> PublicError {
    translate_with(root, code, &|| now)
}
/// `_translate` with the source's clock discipline: the clock is read only for a terminal disposition.
fn translate_with(root: &Path, code: &str, now: &dyn Fn() -> f64) -> PublicError {
    let translated = handoff::peer_translate(Some(code));
    let disposition = match translated.as_str() {
        "peer_member_revoked" => Some("revoked"),
        "peer_generation_stale" => Some("stale"),
        "peer_member_unknown" => Some("unknown"),
        "peer_member_expired" => Some("expired"),
        _ => None,
    };
    if let Some(state) = disposition {
        let record = json!({"protocol": DISPOSITION_PROTOCOL, "membership_state": state, "observed_at": now()});
        if let Ok(limit) = private_limit() {
            private_state::store(&root.join(DISPOSITION_FILE), &record, limit).unwrap_or(());
        }
    }
    peer(intern(&translated))
}
fn seed_failure(root: &Path, error: SeedError, now: f64) -> PublicError {
    if let Some(class) = error.raw {
        return PublicError::new(409, "action_not_completed").raised_by(Raised::HttpClient(class));
    }
    let failure = translate(root, &error.code, now);
    match error.window {
        Some(window) => failure.with_window(window),
        None => failure,
    }
}
fn member_failure(root: &Path, error: MemberError, now: f64) -> PublicError {
    match error {
        MemberError::Membership(code) => translate(root, &code, now),
        // `MembershipContractError` is not among the source's `except` classes: it escapes as is, and
        // the handler's `public_failure_code` publishes its closed code (`mycelium_*` module).
        MemberError::Contract(code) => {
            PublicError::new(409, intern(&code)).raised_by(Raised::MembershipContract)
        }
        MemberError::Value => {
            PublicError::new(409, "action_not_completed").raised_by(Raised::Builtin("ValueError"))
        }
    }
}

/// `_load_consent() or {}` (already validated by `check_consent`).
fn consent_document(root: &Path) -> Result<Value, PublicError> {
    check_consent(root)?;
    let loaded =
        private_state::load(&root.join(CONSENT_FILE), private_limit()?).map_err(authority)?;
    Ok(loaded.map_or_else(
        || json!({}),
        |mut doc| doc.remove("consent").unwrap_or_else(|| json!({})),
    ))
}

/// `_load_pending`: the target's retained pairing request and recipient key.
fn load_pending(root: &Path, now: f64) -> Result<(Value, Vec<u8>), PublicError> {
    let invalid = || peer("peer_pairing_state_invalid");
    let value = private_state::load(&root.join(PENDING_FILE), private_limit()?)
        .map_err(authority)?
        .ok_or_else(invalid)?;
    let fields = [
        "protocol",
        "request",
        "recipient_private_key",
        "consent_digest",
    ];
    if value.len() != fields.len()
        || !fields.iter().all(|f| value.contains_key(*f))
        || value["protocol"] != PENDING_PROTOCOL
    {
        return Err(invalid());
    }
    let request = value["request"].clone();
    if crate::pairing::preview(&request, now).is_err_and(|e| e.code != "pairing_request_expired") {
        return Err(invalid());
    }
    let key = value["recipient_private_key"]
        .as_str()
        .and_then(|k| URL_SAFE_NO_PAD.decode(k).ok())
        .ok_or_else(invalid)?;
    let digest = protocol::canonical::digest(&consent_document(root)?).map_err(|_| invalid())?;
    if value["consent_digest"] != digest {
        return Err(invalid());
    }
    Ok((request, key))
}

fn remove_quietly(path: &Path) -> Result<(), PublicError> {
    match fs::remove_file(path) {
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(crate::preferences::os_error(&e)),
        _ => Ok(()),
    }
}

/// `RemoteMacMember.accept_handoff` (peer.py:740-832): the retained branches, then decrypt locally,
/// pin the seed and submit one checkpointed HTTPS join.
pub fn accept_handoff(
    root: &Path,
    started: bool,
    handoff_value: &Value,
    env: &JoinEnv,
) -> Result<Accepted, PublicError> {
    if let Retained::Accepted(answer) = accept_retained(root, started, handoff_value)? {
        return Ok(Accepted {
            answer,
            session: None,
        });
    }
    let handoff_digest =
        protocol::canonical::digest(handoff_value).map_err(|_| peer("pairing_handoff_invalid"))?;
    let limit = private_limit()?;
    let checkpoint =
        private_state::load(&root.join(JOIN_PENDING_FILE), limit).map_err(authority)?;
    private_state::store(&root.join(HANDOFF_FILE), handoff_value, limit).map_err(authority)?;
    let (request, recipient_key) = load_pending(root, (env.clock)())?;
    let clock = json!((env.clock)());
    let recovered =
        handoff::decrypt_invitation_handoff(&request, handoff_value, Some(&recipient_key), &clock)
            .map_err(|e| translate(root, e.code, (env.clock)()))?;
    let bundle = recovered["invite_bundle"].clone();
    let verified = handoff::verify_invite_bundle(&bundle, &json!((env.clock)()))
        .map_err(|e| translate(root, e.code, (env.clock)()))?;
    if recovered["identity_key_digest"] != env.signer.verification_key_digest().as_str() {
        return Err(peer("peer_identity_mismatch"));
    }
    // `_make_client`: the canonical origin check, then the pinned client.
    let seed_url = verified.payload["seed_url"].as_str().unwrap_or_default();
    if crate::join_link::canonical_https_origin(&verified.payload["seed_url"]).is_err() {
        return Err(translate(root, "peer_operation_failed", (env.clock)()));
    }
    let key_records: Vec<Value> = bundle["seed_key_records"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let swarm_id = verified.payload["swarm_id"].as_str().unwrap_or_default();
    let now_failure = |e: SeedError| seed_failure(root, e, (env.clock)());
    let client = SeedClient::new(
        seed_url,
        swarm_id,
        &verified.seed_key_digest,
        &key_records,
        env.seed,
    )
    .map_err(now_failure)?;
    let identity = client.identity(env.clock).map_err(now_failure)?;
    if identity["seed_url"] != verified.payload["seed_url"]
        || identity["swarm_id"] != verified.payload["swarm_id"]
    {
        return Err(peer("peer_seed_identity_mismatch"));
    }
    let node_id = recovered["node_id"].as_str().unwrap_or_default().to_owned();
    let seed_node_id = identity["seed_node_id"].as_str().unwrap_or_default();
    let address = vec![format!(
        "https://{}.invalid/control",
        env.signer.endpoint_id()
    )];
    let nonce = recovered["pairing_nonce"].as_str().unwrap_or_default();
    let (incarnation, message_id, join_envelope, mut session) = match checkpoint {
        None => {
            let incarnation = format!("remote-incarnation-{}", (env.random_hex)(8));
            let message_id = format!("join-message-{}", (env.random_hex)(16));
            let mut session =
                Session::new(&node_id, swarm_id, seed_node_id, env.signer, &incarnation);
            let envelope = session
                .join_request(env.signer, (env.clock)(), &message_id, nonce, &address)
                .map_err(|e| member_failure(root, e, (env.clock)()))?;
            let record = json!({
                "protocol": JOIN_PENDING_PROTOCOL, "handoff_digest": handoff_digest,
                "incarnation": incarnation, "join_message_id": message_id,
                "join_envelope": envelope,
            });
            private_state::store(&root.join(JOIN_PENDING_FILE), &record, limit)
                .map_err(authority)?;
            (incarnation, message_id, envelope, session)
        }
        Some(checkpoint) => {
            let recovery = || peer("peer_recovery_invalid");
            let (Some(Value::String(incarnation)), Some(Value::String(message_id))) = (
                checkpoint.get("incarnation"),
                checkpoint.get("join_message_id"),
            ) else {
                return Err(recovery());
            };
            if checkpoint.get("protocol") != Some(&json!(JOIN_PENDING_PROTOCOL))
                || !checkpoint
                    .get("join_envelope")
                    .is_some_and(Value::is_object)
            {
                return Err(recovery());
            }
            // The request is re-signed at the checkpoint's own `issued_at`, and must reproduce it.
            let issued = checkpoint["join_envelope"]["message"]["issued_at"]
                .as_f64()
                .ok_or_else(recovery)?;
            let mut session =
                Session::new(&node_id, swarm_id, seed_node_id, env.signer, incarnation);
            let reproduced = session
                .join_request(env.signer, issued, message_id, nonce, &address)
                .map_err(|e| member_failure(root, e, (env.clock)()))?;
            if protocol::canonical::to_bytes(&reproduced).ok()
                != protocol::canonical::to_bytes(&checkpoint["join_envelope"]).ok()
            {
                return Err(recovery());
            }
            (incarnation.clone(), message_id.clone(), reproduced, session)
        }
    };
    drop(message_id);
    let token = bundle["token"].as_str().unwrap_or_default();
    let acceptance = client.join(token, &join_envelope).map_err(now_failure)?;
    let accepted_message = session
        .accept_join(&acceptance, (env.clock)(), &verified.seed_key_digest)
        .map_err(|e| member_failure(root, e, (env.clock)()))?;
    let state = json!({
        "protocol": MEMBERSHIP_PROTOCOL, "node_id": node_id, "swarm_id": swarm_id,
        "seed_node_id": identity["seed_node_id"], "seed_url": verified.payload["seed_url"],
        "seed_key_digest": verified.seed_key_digest, "seed_key_records": key_records,
        "endpoint_id": env.signer.endpoint_id(), "incarnation": incarnation,
        "membership_generation": accepted_message["membership_generation"], "restart_count": 0,
    });
    let membership_limit = membership_limit()?;
    private_state::store(&root.join(MEMBERSHIP_FILE), &state, membership_limit)
        .map_err(|_| durable("node_membership_state_write_failed"))?;
    private_state::store(
        &root.join(ACCEPTED_FILE),
        &json!({
            "protocol": ACCEPTED_PROTOCOL, "handoff_digest": handoff_digest,
            "quota": recovered["quota"], "accepted_at": (env.clock)(),
        }),
        limit,
    )
    .map_err(authority)?;
    for name in [PENDING_FILE, HANDOFF_FILE, JOIN_PENDING_FILE, REJOIN_FILE] {
        remove_quietly(&root.join(name))?;
    }
    remove_quietly(&root.join(DISPOSITION_FILE))?; // `_clear_disposition`
    session.attach_client(client);
    let answer = membership_result_live(root, Some(&session), (env.clock)())?;
    Ok(Accepted {
        answer,
        session: Some(session),
    })
}

/// `status()`'s membership projection with a live session (`self._member`): the generation and lease
/// come from the session; without one it is the retained projection.
pub fn membership_result_live(
    root: &Path,
    session: Option<&Session>,
    now: f64,
) -> Result<Json, PublicError> {
    membership_result_live_lazy(root, session, &|| now)
}

/// The same with the source's clock discipline: `status()` reads the clock once, and only after the
/// state files loaded (an unreadable file raises before any read).
pub fn membership_result_live_lazy(
    root: &Path,
    session: Option<&Session>,
    now: &dyn Fn() -> f64,
) -> Result<Json, PublicError> {
    let Some(session) = session else {
        return membership_result(root);
    };
    check_consent(root)?;
    load_membership_state(root)?;
    load_accepted(root)?;
    let disposition = load_disposition(root)?;
    let (mut state, mut lease) = match session.lease_expires_at() {
        Some(lease) => {
            let at = now();
            if !at.is_finite() {
                return Err(peer("peer_clock_invalid")); // `RemoteMacMember._clock`
            }
            if at >= lease {
                ("expired".to_owned(), "expired")
            } else {
                ("current".to_owned(), "fresh")
            }
        }
        None => ("current".to_owned(), "fresh"),
    };
    if let Some(terminal) = disposition {
        if terminal == "expired" {
            lease = "expired";
        }
        state = terminal;
    }
    Ok(crate::jobj! {
        "protocol" => RESULT_PROTOCOL, "membership_state" => state.as_str(),
        "membership_generation" => session.generation().map_or(Json::Null, Json::from),
        "lease_state" => lease, "serving_state" => "not_yet_serving", "route_ready" => false,
    })
}

/// `_require_current`: the peer is started and holds a live member with its seed client; otherwise the
/// retained terminal disposition says why not.
fn require_current(
    root: &Path,
    started: bool,
    session: Option<&Session>,
) -> Result<std::sync::Arc<SeedClient>, PublicError> {
    if !started {
        return Err(peer("peer_not_started"));
    }
    if let Some(client) = session.and_then(Session::client) {
        return Ok(client);
    }
    Err(match load_disposition(root)?.as_deref() {
        Some("expired") => peer("peer_member_expired"),
        Some("revoked") => peer("peer_member_revoked"),
        Some("stale") => peer("peer_generation_stale"),
        Some("unknown") => peer("peer_member_unknown"),
        _ => peer("peer_membership_missing"),
    })
}

/// `RemoteMacMember.renew()` (peer.py:1037-1060): sign a forced RUNNING heartbeat, send it to the seed,
/// install the lease renewal it answers with, and clear a retained disposition. Failures of the member
/// session and of the seed client are translated (and may retain a terminal disposition); a
/// `MembershipContractError` out of `accept_lease_renewal` and an `http.client` exception escape as they are.
pub fn renew(
    root: &Path,
    started: bool,
    session: Option<&mut Session>,
    env: &JoinEnv,
) -> Result<Json, PublicError> {
    let client = require_current(root, started, session.as_deref())?;
    let Some(session) = session else {
        return Err(peer("peer_membership_missing"));
    };
    // `RemoteMacMember._clock` raises `peer_clock_invalid` at the read of a non-finite value; the session
    // calls then fail on that value, and the failure is replaced by the source's error.
    let bad_clock = std::cell::Cell::new(false);
    let clock = || {
        let at = (env.clock)();
        if !at.is_finite() {
            bad_clock.set(true);
        }
        at
    };
    let member_error = |error: MemberError| match error {
        _ if bad_clock.get() => peer("peer_clock_invalid"),
        MemberError::Membership(code) => translate_with(root, &code, &clock),
        other => member_failure(root, other, f64::NAN), // never a terminal disposition: no clock read
    };
    let seed_error = |error: SeedError| {
        if bad_clock.get() {
            return peer("peer_clock_invalid");
        }
        if let Some(class) = error.raw {
            return PublicError::new(409, "action_not_completed")
                .raised_by(Raised::HttpClient(class));
        }
        translate_with(root, &error.code, &clock)
    };
    let id_source = || format!("member-message-{}", (env.random_hex)(16));
    let heartbeat = session
        .heartbeat(env.signer, &clock, &id_source, "RUNNING", 0)
        .map_err(member_error)?;
    let renewal = client
        .send_heartbeat(&heartbeat, &clock)
        .map_err(seed_error)?;
    session
        .accept_lease_renewal(
            &renewal,
            &clock,
            heartbeat["message"]["message_id"]
                .as_str()
                .unwrap_or_default(),
        )
        .map_err(member_error)?;
    remove_quietly(&root.join(DISPOSITION_FILE))?; // `_clear_disposition`
    membership_result_live_lazy(root, Some(session), &clock)
}

/// `RemoteMacMember.membership_binding()` of a live session.
pub fn membership_binding(session: Option<&Session>) -> Option<Binding> {
    session.and_then(Session::binding)
}

/// What `RemoteMacMember.start()` leaves behind on success: the live session of a resumed membership.
#[derive(Debug)]
pub struct Started {
    pub session: Option<Session>,
}
/// A failed `start()`: the raised error and whether the peer stays started (a lapsed membership
/// keeps its identity loaded and may only ask for a new pairing).
#[derive(Debug)]
pub struct StartFailure {
    pub error: PublicError,
    pub started: bool,
}

const RESUME_PENDING_FILE: &str = "resume.pending.json";
const RESUME_PENDING_PROTOCOL: &str = "mycelium.remote_mac_resume_pending.v1";
/// `RemoteMacMember.start()`'s `_has_post_identity_state` paths.
const POST_IDENTITY_FILES: [&str; 10] = [
    CONSENT_FILE,
    PENDING_FILE,
    HANDOFF_FILE,
    JOIN_PENDING_FILE,
    RESUME_PENDING_FILE,
    ACCEPTED_FILE,
    DISPOSITION_FILE,
    "capability.evidence.json",
    REJOIN_FILE,
    MEMBERSHIP_FILE,
];

/// An error inside `start()`: either already a `RemoteMacMemberError` (the first `except` clause,
/// which keeps a lapsed membership started) or one that escapes to the second clause (the peer is
/// stopped, whatever the code).
enum Fail {
    Direct(PublicError),
    Escaped(PublicError),
}
type Step<T> = Result<T, Fail>;

/// A private-state failure of the source's `_load_private_json` is a `DemoAuthorityError` or a
/// `DurableMembershipError`: both are translated by the second clause.
fn escaped(root: &Path, error: PublicError, now: f64) -> Fail {
    Fail::Escaped(translate(root, error.code, now))
}
/// A failure `peer()` raised directly stays direct; library errors are translated.
fn classify(root: &Path, error: PublicError, now: f64) -> Fail {
    match error.raised {
        Raised::Peer => Fail::Direct(error),
        _ => escaped(root, error, now),
    }
}

/// `durable_membership.next_incarnation`.
fn next_incarnation(base: &str, restart_count: u64) -> String {
    let candidate = format!("{base}.r{restart_count}");
    if candidate.len() <= 128 {
        return candidate;
    }
    let digest = protocol::canonical::sha256_prefixed(candidate.as_bytes());
    let hex = digest.strip_prefix("sha256:").unwrap_or(&digest);
    format!("{}.{}", &base[..110.min(base.len())], &hex[..16])
}

/// `_load_resume_checkpoint`: the unfinished resume this restart must repeat (`None` after a commit
/// that only lacked its cleanup).
fn load_resume_checkpoint(
    root: &Path,
    membership: &Map<String, Value>,
    now: f64,
) -> Step<Option<Map<String, Value>>> {
    let path = root.join(RESUME_PENDING_FILE);
    let limit = private_limit().map_err(|e| classify(root, e, now))?;
    let Some(checkpoint) =
        private_state::load(&path, limit).map_err(|e| escaped(root, authority(e), now))?
    else {
        return Ok(None);
    };
    let invalid = || Fail::Direct(peer("peer_recovery_invalid"));
    let integer = |key: &str| checkpoint.get(key).and_then(Value::as_u64);
    let text = |key: &str| checkpoint.get(key).and_then(Value::as_str);
    if checkpoint.get("protocol") != Some(&json!(RESUME_PENDING_PROTOCOL)) {
        return Err(invalid());
    }
    let (Some(previous), Some(previous_incarnation), Some(incarnation), Some(restarts)) = (
        integer("previous_generation"),
        text("previous_incarnation"),
        text("incarnation"),
        integer("restart_count"),
    ) else {
        return Err(invalid());
    };
    if text("resume_message_id").is_none()
        || !checkpoint
            .get("resume_envelope")
            .is_some_and(Value::is_object)
    {
        return Err(invalid());
    }
    let generation = membership["membership_generation"].as_u64();
    let restart_count = membership["restart_count"].as_u64();
    if generation == Some(previous + 1)
        && membership["incarnation"] == incarnation
        && restart_count == Some(restarts)
    {
        remove_quietly(&path).map_err(|e| escaped(root, e, now))?;
        return Ok(None);
    }
    if generation != Some(previous)
        || membership["incarnation"] != previous_incarnation
        || restart_count.map(|n| n + 1) != Some(restarts)
    {
        return Err(invalid());
    }
    Ok(Some(checkpoint))
}

/// `_resume`: repeat the restart handshake with the seed that accepted this membership.
fn resume(
    root: &Path,
    signer: &Identity,
    membership: &Map<String, Value>,
    env: &JoinEnv,
) -> Step<Session> {
    let now = || (env.clock)();
    let digest = signer.verification_key_digest();
    let node_id = format!(
        "remote-mac-{}",
        &digest.strip_prefix("sha256:").unwrap_or(&digest)[..24]
    );
    if membership["node_id"] != node_id.as_str()
        || membership["endpoint_id"] != signer.endpoint_id()
    {
        return Err(Fail::Direct(peer("peer_identity_mismatch")));
    }
    // `_make_client(membership=...)`.
    let Ok(origin) = crate::join_link::canonical_https_origin(&membership["seed_url"]) else {
        return Err(Fail::Direct(peer("peer_seed_origin_invalid")));
    };
    let key_records: Vec<Value> = membership["seed_key_records"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let swarm_id = membership["swarm_id"].as_str().unwrap_or_default();
    let key_digest = membership["seed_key_digest"].as_str().unwrap_or_default();
    // A constructor `ValueError` carries no code: the second clause turns it into `peer_operation_failed`.
    let client = SeedClient::new(&origin, swarm_id, key_digest, &key_records, env.seed)
        .map_err(|_| Fail::Escaped(translate(root, "peer_operation_failed", now())))?;
    let seed_failure = |e: SeedError| Fail::Direct(seed_failure(root, e, now()));
    let identity = client.identity(env.clock).map_err(seed_failure)?;
    if identity["seed_url"] != membership["seed_url"]
        || identity["swarm_id"] != membership["swarm_id"]
        || identity["seed_node_id"] != membership["seed_node_id"]
    {
        return Err(Fail::Direct(peer("peer_seed_identity_mismatch")));
    }
    let checkpoint = load_resume_checkpoint(root, membership, now())?;
    let address = vec![format!("https://{}.invalid/control", signer.endpoint_id())];
    let node = membership["node_id"].as_str().unwrap_or_default();
    let seed_node = membership["seed_node_id"].as_str().unwrap_or_default();
    let previous_generation = membership["membership_generation"]
        .as_u64()
        .unwrap_or_default();
    let previous_incarnation = membership["incarnation"].as_str().unwrap_or_default();
    let limit = private_limit().map_err(|e| classify(root, e, now()))?;
    let contract_or_membership = |e: MemberError| match e {
        MemberError::Membership(code) => Fail::Direct(translate(root, &code, now())),
        MemberError::Contract(code) => Fail::Escaped(translate(root, &code, now())),
        MemberError::Value => Fail::Escaped(translate(root, "peer_operation_failed", now())),
    };
    let (checkpoint, mut session, envelope) = match checkpoint {
        None => {
            let restart_count = membership["restart_count"].as_u64().unwrap_or_default() + 1;
            let incarnation = next_incarnation(previous_incarnation, restart_count);
            let message_id = format!("resume-message-{}", (env.random_hex)(16));
            let mut session = Session::new(node, swarm_id, seed_node, signer, &incarnation);
            let envelope = session
                .resume_request(
                    signer,
                    now(),
                    &message_id,
                    previous_generation,
                    previous_incarnation,
                    &address,
                )
                .map_err(contract_or_membership)?;
            let record = json!({
                "protocol": RESUME_PENDING_PROTOCOL,
                "previous_generation": previous_generation,
                "previous_incarnation": previous_incarnation,
                "incarnation": incarnation, "restart_count": restart_count,
                "resume_message_id": message_id, "resume_envelope": envelope,
            });
            private_state::store(&root.join(RESUME_PENDING_FILE), &record, limit)
                .map_err(|e| escaped(root, authority(e), now()))?;
            (record, session, envelope)
        }
        Some(checkpoint) => {
            let text = |key: &str| checkpoint[key].as_str().unwrap_or_default().to_owned();
            let incarnation = text("incarnation");
            let message_id = text("resume_message_id");
            let issued = checkpoint["resume_envelope"]["message"]["issued_at"]
                .as_f64()
                .ok_or_else(|| Fail::Direct(peer("peer_recovery_invalid")))?;
            let mut session = Session::new(node, swarm_id, seed_node, signer, &incarnation);
            let reproduced = session
                .resume_request(
                    signer,
                    issued,
                    &message_id,
                    checkpoint["previous_generation"]
                        .as_u64()
                        .unwrap_or_default(),
                    checkpoint["previous_incarnation"]
                        .as_str()
                        .unwrap_or_default(),
                    &address,
                )
                .map_err(contract_or_membership)?;
            if protocol::canonical::to_bytes(&reproduced).ok()
                != protocol::canonical::to_bytes(&checkpoint["resume_envelope"]).ok()
            {
                return Err(Fail::Direct(peer("peer_recovery_invalid")));
            }
            (Value::Object(checkpoint), session, reproduced)
        }
    };
    let acceptance = client.resume(&envelope).map_err(seed_failure)?;
    let accepted = session
        .accept_resume(&acceptance, now(), key_digest)
        .map_err(contract_or_membership)?;
    let mut state = membership.clone();
    state.insert("incarnation".into(), checkpoint["incarnation"].clone());
    state.insert(
        "membership_generation".into(),
        accepted["membership_generation"].clone(),
    );
    state.insert("restart_count".into(), checkpoint["restart_count"].clone());
    let membership_limit = membership_limit().map_err(|e| classify(root, e, now()))?;
    private_state::store(
        &root.join(MEMBERSHIP_FILE),
        &Value::Object(state),
        membership_limit,
    )
    .map_err(|_| escaped(root, durable("node_membership_state_write_failed"), now()))?;
    remove_quietly(&root.join(RESUME_PENDING_FILE)).map_err(|e| escaped(root, e, now()))?;
    remove_quietly(&root.join(DISPOSITION_FILE)).map_err(|e| escaped(root, e, now()))?; // `_clear_disposition`
    session.attach_client(client);
    Ok(session)
}

/// `RemoteMacMember.status()`'s membership projection (peer.py:395-455 @ f4e366e9): a retained
/// credential is `retained` (lease `unknown`), a live session `current` (`fresh`) or `expired`, and a
/// recorded terminal disposition wins (`expired` also expires the lease). `live` is the session's
/// `(generation, lease_expires_at)`; a credential or disposition that cannot be read counts as absent
/// here (the status never fails on it). Returns `(membership_state, generation, lease_state)`.
pub fn status_membership(
    root: &Path,
    live: Option<(Option<u64>, Option<f64>)>,
    now: f64,
) -> (String, Option<u64>, &'static str) {
    let mut state = "absent".to_owned();
    let mut generation = None;
    let mut lease_state = "none";
    if let Ok(Some(membership)) = load_membership_state(root) {
        generation = membership["membership_generation"].as_u64();
        state = "retained".into();
        lease_state = "unknown";
    }
    if let Some((live_generation, lease)) = live {
        generation = live_generation;
        if lease.is_some_and(|lease| now >= lease) {
            state = "expired".into();
            lease_state = "expired";
        } else {
            state = "current".into();
            lease_state = "fresh";
        }
    }
    if let Ok(Some(disposition)) = load_disposition(root) {
        if disposition == "expired" {
            lease_state = "expired";
        }
        state = disposition;
    }
    (state, generation, lease_state)
}

/// The owner's own member re-pinned to the seed address its authority answers on now
/// (`DemoAuthority._resume` pins its client to `self._coordinator.seed_url` and saves the
/// membership with it, authority.py:578-604 @ f4e366e9): after `attach_public_endpoint` /
/// `relocate_authority` reopened the seed on the relay's origin (endpoint_recovery.py:61-85, "the
/// resume at restart re-pins its membership to the new seed address"). Nothing retained, or the
/// same address: nothing is written.
pub fn repin_owner_seed_url(root: &Path, seed_url: &str) -> Result<(), PublicError> {
    let Some(mut membership) = load_membership_state(root)? else {
        return Ok(());
    };
    if membership["seed_url"] == seed_url {
        return Ok(());
    }
    membership.insert("seed_url".into(), Value::from(seed_url));
    private_state::store(
        &root.join(MEMBERSHIP_FILE),
        &Value::Object(membership),
        membership_limit()?,
    )
    .map_err(|_| durable("node_membership_state_write_failed"))
}

/// `RemoteMacMember.start()` (peer.py:327-380): load the identity and the retained state, refuse an
/// inconsistent combination, finish the cleanup a crash left behind, and resume an accepted
/// membership. `identity_present` is whether the durable key file exists (the host owns that file).
pub fn start(
    root: &Path,
    signer: &Identity,
    identity_present: bool,
    env: &JoinEnv,
) -> Result<Started, StartFailure> {
    let now = || (env.clock)();
    let result: Step<Option<Session>> = (|| {
        let has_post_identity_state = POST_IDENTITY_FILES
            .iter()
            .any(|name| fs::symlink_metadata(root.join(name)).is_ok());
        if has_post_identity_state && !identity_present {
            return Err(Fail::Escaped(peer("peer_identity_missing")));
        }
        let membership = load_membership_state(root).map_err(|e| escaped(root, e, now()))?;
        let accepted = load_accepted(root).map_err(|e| classify(root, e, now()))?;
        let join_pending = private_state::load(
            &root.join(JOIN_PENDING_FILE),
            private_limit().map_err(|e| classify(root, e, now()))?,
        )
        .map_err(|e| escaped(root, authority(e), now()))?;
        if accepted.is_some() && membership.is_none() {
            return Err(Fail::Direct(peer("peer_membership_state_invalid")));
        }
        if membership.is_some() && accepted.is_none() && join_pending.is_none() {
            return Err(Fail::Direct(peer("peer_membership_state_invalid")));
        }
        if let (Some(membership), Some(_)) = (membership, accepted) {
            // Durable acceptance makes pairing recovery unnecessary; a re-join in flight keeps its files.
            if !root.join(REJOIN_FILE).exists() {
                for name in [PENDING_FILE, HANDOFF_FILE, JOIN_PENDING_FILE] {
                    remove_quietly(&root.join(name)).map_err(|e| escaped(root, e, now()))?;
                }
            }
            return resume(root, signer, &membership, env).map(Some);
        }
        Ok(None)
    })();
    match result {
        Ok(session) => Ok(Started { session }),
        Err(Fail::Direct(error)) => Err(StartFailure {
            // A lapsed membership keeps the peer started (identity loaded, no member): the only
            // thing it may do is ask for a new pairing, which the owner still has to approve.
            started: matches!(
                error.code,
                "peer_member_expired" | "peer_generation_stale" | "peer_member_unknown"
            ) || matches!(error.raised, Raised::HttpClient(_)),
            error,
        }),
        Err(Fail::Escaped(error)) => Err(StartFailure {
            error,
            started: false,
        }),
    }
}
