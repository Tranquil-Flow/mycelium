// SPDX-License-Identifier: AGPL-3.0-only
//! Signed owner<->member control channel (`mycelium.member_control.v1`, class A,
//! new and versioned) over the existing NodeControl Iroh carrier.
//!
//! Authentication is layered: (1) QUIC/TLS pins both endpoint keys (the member
//! accepts only the operator-pinned owner control endpoint; the owner dials the
//! member's control endpoint id); (2) every command is signed by the owner
//! control key itself and bound to the member endpoint, a strictly increasing
//! sequence and a short validity window; (3) every reply is signed by the
//! member's app (enrolled) key and echoes the command digest, so after pairing
//! the owner verifies replies against the seed row. The operator pin is the
//! out-of-band trust root, exactly like a join link; nothing is trusted on first
//! use. This channel carries the member operator commands (pairing, consent,
//! receive, prepare, serve, status, leave); it does not replace seed-signed
//! membership, offers or routes, which each side still verifies independently.
use crate::schema::PublicError;
use budgets::{BudgetId as B, Entries};
use protocol::identity::{self, Identity};
use serde_json::{Value, json};

pub const COMMAND: &str = "mycelium.member_control.v1";
pub const REPLY: &str = "mycelium.member_control.reply.v1";
const ENVELOPE: &str = "mycelium.member_control.signed.v1";
const COMMAND_FIELDS: &[&str] = &[
    "protocol",
    "owner_endpoint_id",
    "member_endpoint_id",
    "sequence",
    "issued_at",
    "expires_at",
    "request",
];
const REPLY_FIELDS: &[&str] = &[
    "protocol",
    "owner_endpoint_id",
    "member_endpoint_id",
    "sequence",
    "command_digest",
    "reply",
];
type Result<T> = std::result::Result<T, PublicError>;
fn fail(code: &'static str) -> PublicError {
    PublicError::new(409, code)
}
fn exact(v: &Value, fields: &[&str]) -> Result<()> {
    if v.as_object()
        .is_some_and(|m| m.len() == fields.len() && fields.iter().all(|k| m.contains_key(*k)))
    {
        Ok(())
    } else {
        Err(fail("member_control_invalid"))
    }
}
fn envelope(signer: &Identity, message: Value) -> Result<Value> {
    Ok(json!({"protocol":ENVELOPE,
        "signature":signer.sign(&message).map_err(|_| fail("member_control_signature_invalid"))?,
        "verification_key":signer.public_key_record(),"message":message}))
}
fn open(envelope: &Value) -> Result<&Value> {
    exact(
        envelope,
        &["protocol", "signature", "verification_key", "message"],
    )?;
    if envelope["protocol"] != ENVELOPE {
        return Err(fail("member_control_invalid"));
    }
    identity::verify(
        &envelope["message"],
        &envelope["signature"],
        &envelope["verification_key"],
    )
    .map_err(|_| fail("member_control_signature_invalid"))?;
    Ok(&envelope["message"])
}
fn key_bytes(envelope: &Value) -> Option<Vec<u8>> {
    use base64::Engine;
    envelope["verification_key"]["verification_key"]
        .as_str()
        .and_then(|k| base64::engine::general_purpose::STANDARD.decode(k).ok())
}
fn window() -> Result<f64> {
    Ok(B::NodeControlCallTimeoutCeiling
        .span(Entries::ZERO)
        .map_err(|_| fail("budget_unavailable"))?
        .millis() as f64
        / 1000.0)
}
fn skew() -> Result<f64> {
    Ok(runtime::sidecar_artifact::grant_limits()
        .map_err(|_| fail("budget_unavailable"))?
        .clock_skew_ms as f64
        / 1000.0)
}
/// Owner side: the control key IS the owner's control endpoint key.
pub struct Owner {
    signer: Identity,
    endpoint: iroh::EndpointId,
    next: std::sync::atomic::AtomicU64,
}
impl Owner {
    pub fn new(secret: &iroh::SecretKey) -> Result<Self> {
        let endpoint = secret.public();
        Ok(Self {
            signer: Identity::from_secret(&endpoint.to_string(), &secret.to_bytes())
                .map_err(|_| fail("member_control_signature_invalid"))?,
            endpoint,
            next: std::sync::atomic::AtomicU64::new(0),
        })
    }
    /// Strictly increasing and restart-safe: never below wall-clock microseconds,
    /// so a restarted owner is not refused as a replay by a long-lived member.
    fn sequence(&self, now: f64) -> u64 {
        use std::sync::atomic::Ordering::SeqCst;
        let floor = (now.max(0.0) * 1e6) as u64;
        let previous = self
            .next
            .fetch_update(SeqCst, SeqCst, |n| Some(n.saturating_add(1).max(floor)))
            .unwrap_or_default();
        previous.saturating_add(1).max(floor)
    }
    pub fn endpoint(&self) -> iroh::EndpointId {
        self.endpoint
    }
    /// Sign one command for exactly this member control endpoint.
    pub fn command(&self, member: &iroh::EndpointId, request: Value, now: f64) -> Result<Value> {
        let sequence = self.sequence(now);
        envelope(
            &self.signer,
            json!({"protocol":COMMAND,"owner_endpoint_id":self.endpoint.to_string(),
            "member_endpoint_id":member.to_string(),"sequence":sequence,"issued_at":now,
            "expires_at":now + window()?,"request":request}),
        )
    }
    /// Verify a member reply to `command`. `enrolled_key_digest` (from the seed
    /// row) is required once the member is paired; before pairing the reply is
    /// bound to the TLS-pinned member endpoint and the command digest only.
    pub fn open_reply(
        &self,
        command: &Value,
        reply: &Value,
        enrolled_key_digest: Option<&str>,
    ) -> Result<Value> {
        let message = open(reply)?;
        exact(message, REPLY_FIELDS)?;
        let sent = &command["message"];
        if message["protocol"] != REPLY
            || message["owner_endpoint_id"] != sent["owner_endpoint_id"]
            || message["member_endpoint_id"] != sent["member_endpoint_id"]
            || message["sequence"] != sent["sequence"]
            || message["command_digest"]
                != protocol::canonical::digest(command)
                    .map_err(|_| fail("member_control_invalid"))?
        {
            return Err(fail("member_control_reply_mismatch"));
        }
        if let Some(digest) = enrolled_key_digest
            && reply["verification_key"]["verification_key_digest"] != digest
        {
            return Err(fail("member_control_reply_unenrolled"));
        }
        Ok(message["reply"].clone())
    }
}
/// Member side: verify one command against the operator-pinned owner endpoint,
/// this member's control endpoint, the replay sequence and the validity window.
pub fn verify_command(
    envelope: &Value,
    owner: &iroh::EndpointId,
    member: &iroh::EndpointId,
    last_sequence: u64,
    now: f64,
) -> Result<(u64, Value)> {
    let message = open(envelope)?;
    exact(message, COMMAND_FIELDS)?;
    // The signing key must be the pinned owner endpoint key itself.
    if key_bytes(envelope).as_deref() != Some(owner.as_bytes().as_slice())
        || envelope["signature"]["signer_endpoint_id"] != owner.to_string()
        || message["protocol"] != COMMAND
        || message["owner_endpoint_id"] != owner.to_string()
    {
        return Err(fail("member_control_owner_unpinned"));
    }
    if message["member_endpoint_id"] != member.to_string() {
        return Err(fail("member_control_wrong_member"));
    }
    let sequence = message["sequence"]
        .as_u64()
        .ok_or(fail("member_control_invalid"))?;
    if sequence <= last_sequence {
        return Err(fail("member_control_replayed"));
    }
    let (issued, expires) = (
        message["issued_at"].as_f64().unwrap_or(f64::NAN),
        message["expires_at"].as_f64().unwrap_or(f64::NAN),
    );
    if !issued.is_finite()
        || !expires.is_finite()
        || expires - issued > window()?
        || issued > now + skew()?
        || expires < now - skew()?
    {
        return Err(fail("member_control_expired"));
    }
    Ok((sequence, message["request"].clone()))
}
/// Member side: sign the reply with the member's app (enrolled) key.
pub fn sign_reply(signer: &Identity, command: &Value, reply: Value) -> Result<Value> {
    let sent = &command["message"];
    envelope(
        signer,
        json!({"protocol":REPLY,"owner_endpoint_id":sent["owner_endpoint_id"],
        "member_endpoint_id":sent["member_endpoint_id"],"sequence":sent["sequence"],
        "command_digest":protocol::canonical::digest(command).map_err(|_| fail("member_control_invalid"))?,
        "reply":reply}),
    )
}
/// Owner-side handle on one remote member control endpoint.
pub struct Remote {
    pub addr: iroh::EndpointAddr,
    /// The member's enrolled key digest once paired (from the seed row).
    pub enrolled: Option<String>,
}
impl Remote {
    /// One signed request/response over NodeControl, bounded by `deadline`.
    pub fn call<C: transport::Clock>(
        &self,
        owner: &Owner,
        client: &runtime::iroh_control::NodeControlClient<'_, '_, C>,
        request: Value,
        deadline: budgets::MonoInstant,
        now: f64,
    ) -> Result<Value> {
        // Concurrent owner calls may reach the member out of order; a signed
        // `member_control_replayed` refusal is retried with a fresh sequence.
        let mut last = Err(fail("member_control_replayed"));
        for _ in 0..3 {
            let reply = self.call_once(owner, client, request.clone(), deadline, now)?;
            if reply["ok"] == false && reply["code"] == "member_control_replayed" {
                last = Ok(reply);
                continue;
            }
            return Ok(reply);
        }
        last
    }
    fn call_once<C: transport::Clock>(
        &self,
        owner: &Owner,
        client: &runtime::iroh_control::NodeControlClient<'_, '_, C>,
        request: Value,
        deadline: budgets::MonoInstant,
        now: f64,
    ) -> Result<Value> {
        use runtime::iroh_control::ControlRequest;
        use transport::ConnectionFence;
        let command = owner.command(&self.addr.id, request, now)?;
        let fence = ConnectionFence {
            endpoint_id: *self.addr.id.as_bytes(),
            local_generation: 0,
            remote_generation: 0,
        };
        let (_keep, mut authority) = tokio::sync::watch::channel(Some(fence));
        let bytes = serde_json::to_vec(&command).map_err(|_| fail("member_control_invalid"))?;
        let response = client
            .call(
                ControlRequest {
                    peer: &self.addr,
                    fence,
                    command: &bytes,
                    deadline,
                },
                &mut authority,
            )
            .map_err(|e| PublicError::new(503, e.0))?;
        let reply: Value =
            serde_json::from_slice(&response).map_err(|_| fail("member_control_invalid"))?;
        owner.open_reply(&command, &reply, self.enrolled.as_deref())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn keys() -> (iroh::SecretKey, iroh::EndpointId, Identity) {
        let owner = iroh::SecretKey::from_bytes(&[3; 32]);
        let member = iroh::SecretKey::from_bytes(&[4; 32]).public();
        let app = Identity::from_secret("member-app", &[5; 32]).unwrap();
        (owner, member, app)
    }
    const NOW: f64 = 1_800_000_000.0;
    #[test]
    fn commands_are_pinned_bound_fresh_and_single_use() {
        let (secret, member, _) = keys();
        let owner = Owner::new(&secret).unwrap();
        let pin = owner.endpoint();
        let c = owner
            .command(&member, json!({"command":"status"}), NOW)
            .unwrap();
        let (seq, request) = verify_command(&c, &pin, &member, 0, NOW).unwrap();
        assert_eq!(request["command"], "status");
        let code = |r: Result<(u64, Value)>| r.unwrap_err().code;
        assert_eq!(
            code(verify_command(&c, &pin, &member, seq, NOW)),
            "member_control_replayed"
        );
        let other = iroh::SecretKey::from_bytes(&[6; 32]).public();
        assert_eq!(
            code(verify_command(&c, &pin, &other, 0, NOW)),
            "member_control_wrong_member"
        );
        assert_eq!(
            code(verify_command(&c, &other, &member, 0, NOW)),
            "member_control_owner_unpinned"
        );
        let late = NOW + 2.0 * window().unwrap() + skew().unwrap();
        assert_eq!(
            code(verify_command(&c, &pin, &member, 0, late)),
            "member_control_expired"
        );
        // A stranger re-signing a command claiming the pinned owner id.
        let stranger = Identity::from_secret(&pin.to_string(), &[8; 32]).unwrap();
        let forged = envelope(&stranger, c["message"].clone()).unwrap();
        assert_eq!(
            code(verify_command(&forged, &pin, &member, 0, NOW)),
            "member_control_owner_unpinned"
        );
        let mut tampered = c.clone();
        tampered["message"]["request"] = json!({"command":"leave"});
        assert_eq!(
            code(verify_command(&tampered, &pin, &member, 0, NOW)),
            "member_control_signature_invalid"
        );
        // Restart-safe ordering: a fresh owner never reuses a sequence.
        let again = Owner::new(&secret)
            .unwrap()
            .command(&member, json!({}), NOW + 1.0)
            .unwrap();
        assert!(verify_command(&again, &pin, &member, seq, NOW + 1.0).is_ok());
    }
    #[test]
    fn replies_are_bound_to_the_command_and_the_enrolled_key() {
        let (secret, member, app) = keys();
        let owner = Owner::new(&secret).unwrap();
        let c = owner
            .command(&member, json!({"command":"status"}), NOW)
            .unwrap();
        let r = sign_reply(&app, &c, json!({"ok":true})).unwrap();
        let enrolled = app.public_key_record()["verification_key_digest"]
            .as_str()
            .unwrap()
            .to_owned();
        assert_eq!(
            owner.open_reply(&c, &r, Some(&enrolled)).unwrap()["ok"],
            true
        );
        let code = |r: Result<Value>| r.unwrap_err().code;
        let other = owner
            .command(&member, json!({"command":"status"}), NOW)
            .unwrap();
        assert_eq!(
            code(owner.open_reply(&other, &r, None)),
            "member_control_reply_mismatch"
        );
        let impostor = Identity::from_secret("member-app", &[9; 32]).unwrap();
        let r2 = sign_reply(&impostor, &c, json!({"ok":true})).unwrap();
        assert_eq!(
            code(owner.open_reply(&c, &r2, Some(&enrolled))),
            "member_control_reply_unenrolled"
        );
        let mut tampered = r.clone();
        tampered["message"]["reply"] = json!({"ok":false});
        assert_eq!(
            code(owner.open_reply(&c, &tampered, None)),
            "member_control_signature_invalid"
        );
    }
}
