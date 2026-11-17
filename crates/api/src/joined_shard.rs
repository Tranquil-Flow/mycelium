// SPDX-License-Identifier: AGPL-3.0-only
//! Pairing-bound inert shard transfer. Existing artifact v1 wire bytes stay
//! unchanged; the versioned offer binds that grant to the actual joined member.
//! This supplies neither native-worker custody nor route readiness.
use crate::{
    compiler_inputs::{Error, member_shard::MemberShard},
    member::Session,
    preferences::standing_consent,
};
use protocol::{
    artifact_grant as ag,
    identity::{self, Identity},
};
use runtime::sidecar_artifact::grant_limits;
use serde_json::{Value, json};
use std::path::{Path, PathBuf};
pub mod carrier;
pub mod native;
pub mod route;
pub mod serve;
mod source;
pub use source::JoinedSource;
type Result<T> = std::result::Result<T, Error>;
const ENVELOPE: &str = "mycelium.joined_shard.signed.v1";
const REGISTER: &str = "mycelium.joined_shard.registration.v1";
const OFFER: &str = "mycelium.joined_shard.offer.v1";
const WITHDRAWAL: &str = "mycelium.joined_shard.withdrawal.v1";
fn fail(code: &'static str) -> Error {
    Error(code)
}
fn digest(v: &Value) -> Result<String> {
    protocol::canonical::digest(v).map_err(|_| fail("joined_shard_invalid"))
}
fn text<'a>(v: &'a Value, k: &str) -> Result<&'a str> {
    v[k].as_str().ok_or(fail("joined_shard_invalid"))
}
fn exact(v: &Value, fields: &[&str]) -> Result<()> {
    if !v
        .as_object()
        .is_some_and(|m| m.len() == fields.len() && fields.iter().all(|k| m.contains_key(*k)))
    {
        return Err(fail("joined_shard_invalid"));
    }
    Ok(())
}
fn signed(signer: &Identity, message: Value) -> Result<Value> {
    Ok(
        json!({"protocol":ENVELOPE,"signature":signer.sign(&message).map_err(|_| fail("joined_shard_signature_invalid"))?,"verification_key":signer.public_key_record(),"message":message}),
    )
}
fn open<'a>(envelope: &'a Value, protocol: &str, key_digest: &str) -> Result<&'a Value> {
    exact(
        envelope,
        &["protocol", "signature", "verification_key", "message"],
    )?;
    let message = &envelope["message"];
    if envelope["protocol"] != ENVELOPE
        || message["protocol"] != protocol
        || identity::key_record_digest(&envelope["verification_key"])
            .map_err(|_| fail("joined_shard_signature_invalid"))?
            != key_digest
    {
        return Err(fail("joined_shard_signature_invalid"));
    }
    identity::verify(
        message,
        &envelope["signature"],
        &envelope["verification_key"],
    )
    .map_err(|_| fail("joined_shard_signature_invalid"))?;
    Ok(message)
}
fn live(session: &Session, signer: &Identity, now: f64) -> Result<Value> {
    session.shard_identity(signer, now).map_err(|e| {
        fail(match e.code() {
            "joined_shard_identity_mismatch" => "joined_shard_identity_mismatch",
            "joined_shard_not_joined" => "joined_shard_not_joined",
            _ => "joined_shard_lease_expired",
        })
    })
}
/// A joined app identity signs the binding to its separately authenticated QUIC
/// endpoint. Carrier signatures use the SAME enrolled key, not an unrelated key.
pub fn register(
    session: &Session,
    signer: &Identity,
    carrier: &Identity,
    now: f64,
) -> Result<Value> {
    let member = live(session, signer, now)?;
    if carrier.verification_key_digest() != signer.verification_key_digest()
        || carrier.endpoint_id().parse::<iroh::EndpointId>().is_err()
    {
        return Err(fail("joined_shard_identity_mismatch"));
    }
    signed(
        signer,
        json!({"protocol":REGISTER,"member":member,"carrier_endpoint_id":carrier.endpoint_id()}),
    )
}
fn current(coordinator: &seed::Coordinator, member: &Value) -> Result<f64> {
    let node = text(member, "node_id")?;
    coordinator
        .with_current_member(node, |row, now| {
            for field in [
                "node_id",
                "endpoint_id",
                "verification_key_digest",
                "incarnation",
                "generation",
            ] {
                if row[field] != member[field] {
                    return Err(seed::SeedError::new("joined_shard_member_changed"));
                }
            }
            if member["swarm_id"] != coordinator.swarm_id()
                || member["seed_key_digest"] != coordinator.signer().verification_key_digest()
                || row["lifecycle_state"] != "RUNNING"
                || row["lease_expires_at"]
                    .as_f64()
                    .is_none_or(|v| !v.is_finite() || v <= now)
            {
                return Err(seed::SeedError::new("joined_shard_member_changed"));
            }
            Ok(row["lease_expires_at"].as_f64().unwrap())
        })
        .map_err(|_| fail("joined_shard_member_changed"))
}
/// A member's signed carrier registration, opened against the seed's CURRENT
/// row for the node it names: signed by that row's enrolled key and endpoint,
/// and every member field (key digest, endpoint, incarnation, generation)
/// equal to the row, RUNNING, leased. Returns the row and its lease expiry.
fn registered(coordinator: &seed::Coordinator, registration: &Value) -> Result<(Value, f64)> {
    let node = text(&registration["message"]["member"], "node_id")?;
    let row = coordinator
        .member(node)
        .map_err(|_| fail("joined_shard_member_changed"))?;
    let reg = open(
        registration,
        REGISTER,
        text(&row, "verification_key_digest")?,
    )?;
    exact(reg, &["protocol", "member", "carrier_endpoint_id"])?;
    if registration["signature"]["signer_endpoint_id"] != row["endpoint_id"] {
        return Err(fail("joined_shard_identity_mismatch"));
    }
    let lease = current(coordinator, &reg["member"])?;
    Ok((row, lease))
}
/// Verify a member's own registration claim (its enrolled key) against the
/// seed row before anything trusts it (REVIEW-laneB16 F2); the same checks
/// every offer repeats.
pub fn verify_registration(coordinator: &seed::Coordinator, registration: &Value) -> Result<()> {
    registered(coordinator, registration).map(|_| ())
}
/// Issue from the real seed's current durable member row. `recipe` and `transfer`
/// must come from the compiler; this API does not claim to have produced them.
pub fn issue(
    coordinator: &seed::Coordinator,
    registration: &Value,
    run: &str,
    transfer: &Value,
    recipe: &Value,
    source: &Identity,
) -> Result<Value> {
    issue_with_source(
        coordinator,
        registration,
        run,
        transfer,
        recipe,
        source.endpoint_id(),
        &source.public_key_record(),
    )
}
/// Public-key-only source binding; the issuing seed never needs a source secret.
pub fn issue_with_source(
    coordinator: &seed::Coordinator,
    registration: &Value,
    run: &str,
    transfer: &Value,
    recipe: &Value,
    source_endpoint: &str,
    source_key: &Value,
) -> Result<Value> {
    issue_with_source_addrs(
        coordinator,
        registration,
        run,
        transfer,
        recipe,
        source_endpoint,
        source_key,
        &[],
    )
}
/// As `issue_with_source`, also signing the source's direct addresses into the
/// grant (artifact grant v1 `direct_addrs`, unchanged wire shape), so a LAN
/// member dials the source only where the seed said it listens.
#[allow(clippy::too_many_arguments)]
pub fn issue_with_source_addrs(
    coordinator: &seed::Coordinator,
    registration: &Value,
    run: &str,
    transfer: &Value,
    recipe: &Value,
    source_endpoint: &str,
    source_key: &Value,
    source_addrs: &[std::net::SocketAddr],
) -> Result<Value> {
    let source_digest = identity::key_record_digest(source_key)
        .map_err(|_| fail("joined_shard_identity_mismatch"))?;
    let (row, _lease) = registered(coordinator, registration)?;
    let reg = &registration["message"];
    let node = text(&reg["member"], "node_id")?;
    if recipe["node_id"] != node {
        return Err(fail("joined_shard_member_changed"));
    }
    let now = crate::app_host::now();
    let limits = grant_limits().map_err(|_| fail("budget_unavailable"))?;
    let now_ms = (now * 1000.0).floor() as u64;
    // v0.1.4 sizes an artifact grant once, `min(DEFAULT_GRANT_TTL_MS, assignment expiry)`
    // (`mycelium_seed/route_assignment.py:826-827`; `mycelium_live/member_transport.py:1016-1022`
    // issues it after the slow source work so the recipient gets the whole window), and never
    // renews it mid-transfer. Liveness is not the grant's job: both ends re-check the member's
    // CURRENT row (generation, key, endpoint, RUNNING, unexpired lease) for every chunk, as the A13
    // peer source does (`peer_model_source.py:256-262`). Clamping the grant to the lease current at
    // issue failed every transfer that outlived it although the member kept renewing.
    let ttl = budgets::BudgetId::PyMyceliumMobileArtifactGrantDefaultGrantTtlMsL37
        .span(budgets::Entries::ZERO)
        .map_err(|_| fail("budget_unavailable"))?
        .millis()
        .min(limits.max_grant_ttl_ms);
    let expires = now_ms.saturating_add(ttl);
    let bytes = transfer["size_bytes"]
        .as_u64()
        .ok_or(fail("joined_shard_invalid"))?;
    let mut body = recipe.clone();
    body.as_object_mut()
        .ok_or(fail("joined_shard_invalid"))?
        .remove("node_plan_digest");
    if recipe["node_plan_digest"] != digest(&body)? {
        return Err(fail("joined_shard_recipe_invalid"));
    }
    let grant_message = json!({"protocol":ag::GRANT_PROTOCOL,"grant_id":run,
        "swarm_id":coordinator.swarm_id(),"seed_node_id":coordinator.seed_node_id(),
        "recipient_node_id":node,"recipient_endpoint_id":reg["carrier_endpoint_id"],
        "recipient_key_digest":row["verification_key_digest"],"assignment_id":run,
        "manifest_pack_digest":recipe["node_plan_digest"],"allowed_object_digests":[transfer["content_digest"]],
        "authorized_sources":[{"endpoint_id":source_endpoint,"verification_key_digest":source_digest,
            "direct_addrs":source_addrs.iter().map(|a| a.to_string()).collect::<Vec<_>>()}],
        "maximum_total_bytes":bytes.checked_mul(2).ok_or(fail("joined_shard_invalid"))?,
        "maximum_bytes_per_second":ag::DEFAULT_MAXIMUM_BYTES_PER_SECOND,"maximum_chunk_bytes":ag::MAX_CHUNK_BYTES,
        "issued_at_unix_ms":now_ms,"not_before_unix_ms":now_ms,"expires_at_unix_ms":expires});
    let signer = coordinator.signer();
    let grant = json!({"protocol":protocol::membership::SIGNED_MESSAGE_PROTOCOL,
        "signature":signer.sign(&grant_message).map_err(|_| fail("joined_shard_signature_invalid"))?,
        "verification_key":signer.public_key_record(),"message":grant_message});
    ag::verify_grant(&grant, &signer.public_key_record(), now_ms, limits)
        .map_err(|_| fail("joined_shard_grant_invalid"))?;
    let offer = signed(
        signer,
        json!({"protocol":OFFER,"member":reg["member"],"registration":registration,
        "run":run,"transfer":transfer,"recipe":recipe,"grant":grant}),
    )?;
    check_owner(coordinator, &offer)?;
    Ok(offer)
}
/// Repeat on the source before AND after carrier I/O, not just when issuing.
pub fn check_owner(coordinator: &seed::Coordinator, offer: &Value) -> Result<()> {
    let message = open(
        offer,
        OFFER,
        &coordinator.signer().verification_key_digest(),
    )?;
    current(coordinator, &message["member"])?;
    Ok(())
}
fn admit(
    root: &Path,
    session: &Session,
    signer: &Identity,
    carrier: &Identity,
    offer: &Value,
    now: f64,
) -> Result<ag::Grant> {
    let member = live(session, signer, now)?;
    let message = open(offer, OFFER, text(&member, "seed_key_digest")?)?;
    exact(
        message,
        &[
            "protocol",
            "member",
            "registration",
            "run",
            "transfer",
            "recipe",
            "grant",
        ],
    )?;
    if message["member"] != member {
        return Err(fail("joined_shard_member_changed"));
    }
    let reg = register(session, signer, carrier, now)?;
    if message["registration"] != reg {
        return Err(fail("joined_shard_identity_mismatch"));
    }
    let grant = ag::verify_grant(
        &message["grant"],
        &offer["verification_key"],
        (now * 1000.0).floor() as u64,
        grant_limits().map_err(|_| fail("budget_unavailable"))?,
    )
    .map_err(|_| fail("joined_shard_grant_invalid"))?;
    if grant.recipient_node_id != session.node_id()
        || grant.recipient_endpoint_id != carrier.endpoint_id()
        || grant.recipient_key_digest != signer.verification_key_digest()
        || grant.assignment_id != text(message, "run")?
        || grant.manifest_pack_digest != text(&message["recipe"], "node_plan_digest")?
        || grant.allowed_object_digests != [text(&message["transfer"], "content_digest")?]
        || now * 1000.0 >= grant.expires_at_unix_ms as f64
    {
        return Err(fail("joined_shard_grant_invalid"));
    }
    let consent = standing_consent::load(root)
        .map_err(|_| fail("joined_shard_consent_required"))?
        .ok_or(fail("joined_shard_consent_required"))?;
    let binding = json!({"swarm_id":member["swarm_id"],"node_id":member["node_id"],"seed_key_digest":member["seed_key_digest"]});
    if consent["state"] != "granted"
        || consent["binding"] != binding
        || consent["caps"]["storage_bytes"]
            .as_u64()
            .zip(message["transfer"]["size_bytes"].as_u64())
            .is_none_or(|(cap, size)| size == 0 || cap < size)
    {
        return Err(fail("joined_shard_consent_required"));
    }
    Ok(grant)
}
#[derive(Debug)]
pub struct JoinedShard {
    root: PathBuf,
    offer: Value,
    shard: MemberShard,
}
impl JoinedShard {
    /// Unbudgeted custody; the member's `receive` operation uses [`JoinedShard::begin_checked`].
    pub fn begin(
        root: &Path,
        session: &Session,
        signer: &Identity,
        carrier: &Identity,
        offer: &Value,
        now: f64,
    ) -> Result<Self> {
        Self::begin_checked(root, session, signer, carrier, offer, now, &|| Ok(()))
    }
    /// [`JoinedShard::begin`] under the receiving operation's own `checkpoint` (cancellation,
    /// deadline), which bounds the storage reservation (lane A patch 3, A2-01).
    pub fn begin_checked(
        root: &Path,
        session: &Session,
        signer: &Identity,
        carrier: &Identity,
        offer: &Value,
        now: f64,
        _checkpoint: &dyn Fn() -> Result<()>,
    ) -> Result<Self> {
        admit(root, session, signer, carrier, offer, now)?;
        let m = &offer["message"];
        let shard = MemberShard::begin(
            root,
            text(m, "run")?,
            session.node_id(),
            &m["transfer"],
            &m["recipe"],
            text(&m["recipe"], "node_plan_digest")?,
        )?;
        Ok(Self {
            root: root.into(),
            offer: offer.clone(),
            shard,
        })
    }
    pub fn path(&self) -> PathBuf {
        self.shard.path()
    }
    #[allow(clippy::too_many_arguments)]
    pub fn write_chunk(
        &mut self,
        offset: u64,
        bytes: &[u8],
        session: &Session,
        signer: &Identity,
        carrier: &Identity,
        now: f64,
    ) -> Result<()> {
        admit(&self.root, session, signer, carrier, &self.offer, now)?;
        self.shard.write_chunk(offset, bytes)
    }
    pub fn verify(
        &mut self,
        session: &Session,
        signer: &Identity,
        carrier: &Identity,
        now: &dyn Fn() -> f64,
    ) -> Result<()> {
        self.shard
            .verify(&|| admit(&self.root, session, signer, carrier, &self.offer, now()).map(drop))
    }
    pub fn verify_checked(
        &mut self,
        session: &Session,
        signer: &Identity,
        carrier: &Identity,
        now: &dyn Fn() -> f64,
        checkpoint: &dyn Fn() -> Result<()>,
    ) -> Result<()> {
        self.shard.verify(&|| {
            checkpoint()?;
            admit(&self.root, session, signer, carrier, &self.offer, now()).map(drop)
        })
    }
    /// As `verify_checked`, but every bounded read admits against the session
    /// `session()` returns NOW, so a renewal during a long verify is honoured.
    pub fn verify_live(
        &mut self,
        session: &dyn Fn() -> Result<Session>,
        signer: &Identity,
        carrier: &Identity,
        now: &dyn Fn() -> f64,
        checkpoint: &dyn Fn() -> Result<()>,
    ) -> Result<()> {
        self.shard.verify(&|| {
            checkpoint()?;
            admit(&self.root, &session()?, signer, carrier, &self.offer, now()).map(drop)
        })
    }
    /// Signed carrier fetch with live pairing/consent checks on both sides of I/O.
    #[allow(clippy::too_many_arguments)]
    pub fn fetch_chunk<C: transport::Clock>(
        &mut self,
        link: &mut runtime::iroh_artifact::ChunkSession<'_, C>,
        request: &ag::ChunkRequest,
        session: &Session,
        signer: &Identity,
        carrier: &Identity,
        now: &dyn Fn() -> f64,
        deadline: budgets::MonoInstant,
    ) -> std::result::Result<(), runtime::iroh_artifact::ArtifactError> {
        self.fetch_chunk_checked(
            link,
            request,
            session,
            signer,
            carrier,
            now,
            deadline,
            &|| Ok(()),
        )
    }
    /// Independent operation fence, serviced during transport and before writes.
    #[allow(clippy::too_many_arguments)]
    pub fn fetch_chunk_checked<C: transport::Clock>(
        &mut self,
        link: &mut runtime::iroh_artifact::ChunkSession<'_, C>,
        request: &ag::ChunkRequest,
        session: &Session,
        signer: &Identity,
        carrier: &Identity,
        now: &dyn Fn() -> f64,
        deadline: budgets::MonoInstant,
        checkpoint: &dyn Fn() -> Result<()>,
    ) -> std::result::Result<(), runtime::iroh_artifact::ArtifactError> {
        use runtime::iroh_artifact::ArtifactError;
        checkpoint().map_err(|e| ArtifactError(e.0.into()))?;
        let grant = admit(&self.root, session, signer, carrier, &self.offer, now())
            .map_err(|e| ArtifactError(e.0.into()))?;
        let envelope = ag::sign_chunk_request(
            carrier,
            &grant,
            &request.request_id,
            &request.request_id,
            &request.source_endpoint_id,
            &request.object_digest,
            request.offset_bytes,
            request.length_bytes,
            (now() * 1000.0).floor() as u64,
            grant_limits().map_err(|_| ArtifactError("budget_unavailable".into()))?,
        )
        .map_err(|e| ArtifactError(e.0.into()))?;
        self.shard
            .fetch_chunk_checked(link, &envelope, request, &grant, deadline, &|| {
                checkpoint()?;
                admit(&self.root, session, signer, carrier, &self.offer, now()).map(drop)
            })
    }
    /// Inert-byte cleanup only. A future compute owner must first prove reap.
    pub fn discard(&mut self) -> Result<()> {
        self.shard.discard()
    }
}
/// Member leave publication: the enrolled key signs that this held offer's
/// compute and custody are withdrawn, with the member's own cleanup evidence.
pub fn withdrawal(
    session: &Session,
    signer: &Identity,
    offer: &Value,
    evidence: &Value,
    now: f64,
) -> Result<Value> {
    let member = live(session, signer, now)?;
    if offer["message"]["member"] != member {
        return Err(fail("joined_shard_member_changed"));
    }
    signed(
        signer,
        json!({"protocol":WITHDRAWAL,"member":member,"offer_digest":digest(offer)?,
        "reason":"member_leave","native":evidence,"route_ready":false,"withdrawn_at":now}),
    )
}
/// Owner side: the enrolled key of the offer's member row must have signed it.
pub fn verify_withdrawal(
    coordinator: &seed::Coordinator,
    offer: &Value,
    envelope: &Value,
) -> Result<Value> {
    open(
        offer,
        OFFER,
        &coordinator.signer().verification_key_digest(),
    )?;
    let member = &offer["message"]["member"];
    let row = coordinator
        .member(text(member, "node_id")?)
        .map_err(|_| fail("joined_shard_member_changed"))?;
    let message = open(envelope, WITHDRAWAL, text(&row, "verification_key_digest")?)?;
    exact(
        message,
        &[
            "protocol",
            "member",
            "offer_digest",
            "reason",
            "native",
            "route_ready",
            "withdrawn_at",
        ],
    )?;
    if message["member"] != *member
        || message["offer_digest"] != digest(offer)?
        || envelope["signature"]["signer_endpoint_id"] != member["endpoint_id"]
        || message["route_ready"] != false
    {
        return Err(fail("joined_shard_identity_mismatch"));
    }
    Ok(message.clone())
}
