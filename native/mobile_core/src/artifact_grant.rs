// SPDX-License-Identifier: AGPL-3.0-only
//! A2 artifact authority for phone stage objects, the Rust side of
//! `mycelium_mobile/artifact_grant.py`: the seed-signed grant (bound to the
//! recipient's node id, iroh endpoint id and enrolled key, not its membership
//! generation), the member-signed chunk request that embeds it, and the
//! source-signed receipt over the exact bytes served. All three are
//! `mycelium.membership.signed_message.v1` envelopes with integer millisecond
//! times and a 30 s clock-skew allowance. Pure (no I/O): the phone builds
//! requests and checks receipts, a source checks requests and signs receipts.

use std::collections::BTreeMap;

use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::identity::{self, Identity};
use crate::membership::SIGNED_MESSAGE_PROTOCOL;

pub const GRANT_PROTOCOL: &str = "mycelium.mobile_artifact_grant.v1";
pub const CHUNK_REQUEST_PROTOCOL: &str = "mycelium.mobile_artifact_chunk_request.v1";
pub const CHUNK_RECEIPT_PROTOCOL: &str = "mycelium.mobile_artifact_chunk_receipt.v1";
pub const ARTIFACT_CHUNK_ALPN: &[u8] = b"mycelium.artifact_chunk.v1";
pub const MAX_GRANT_TTL_MS: u64 = 6 * 3_600_000;
pub const MAX_REQUEST_TTL_MS: u64 = 120_000;
pub const REQUEST_TTL_MS: u64 = 60_000;
pub const CLOCK_SKEW_MS: u64 = 30_000;
pub const MAX_CHUNK_BYTES: u64 = 4 << 20;
const MAX_OBJECTS: usize = 4096;
const MAX_SOURCES: usize = 16;
const MAX_SAFE_INTEGER: u64 = (1 << 53) - 1;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct GrantError(pub &'static str);

fn fail<T>(code: &'static str) -> Result<T, GrantError> {
    Err(GrantError(code))
}

/// A verified grant.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Grant {
    pub grant_id: String,
    pub recipient_node_id: String,
    pub recipient_endpoint_id: String,
    pub recipient_key_digest: String,
    pub assignment_id: String,
    pub manifest_pack_digest: String,
    pub allowed_object_digests: Vec<String>,
    /// Source iroh endpoint id -> its signing key digest.
    pub sources: BTreeMap<String, String>,
    /// Source iroh endpoint id -> direct-address hints (`ip:port`); iroh still
    /// authenticates the endpoint, so a wrong hint only fails to connect.
    pub source_addrs: BTreeMap<String, Vec<std::net::SocketAddr>>,
    pub maximum_total_bytes: u64,
    pub maximum_bytes_per_second: u64,
    pub maximum_chunk_bytes: u64,
    pub not_before_unix_ms: u64,
    pub expires_at_unix_ms: u64,
    /// The signed envelope, embedded verbatim in every chunk request.
    pub envelope: Value,
}

/// A verified chunk request (source side) or one this node built.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChunkRequest {
    pub request_id: String,
    pub source_endpoint_id: String,
    pub object_digest: String,
    pub offset_bytes: u64,
    pub length_bytes: u64,
}

fn exact(v: &Value, fields: &[&str], code: &'static str) -> Result<(), GrantError> {
    let m = v.as_object().ok_or(GrantError(code))?;
    if m.len() != fields.len() || !fields.iter().all(|f| m.contains_key(*f)) {
        return fail(code);
    }
    Ok(())
}

fn s<'v>(v: &'v Value, code: &'static str) -> Result<&'v str, GrantError> {
    v.as_str().ok_or(GrantError(code))
}

fn ident<'v>(v: &'v Value, code: &'static str) -> Result<&'v str, GrantError> {
    let x = s(v, code)?;
    let b = x.as_bytes();
    let ok = !b.is_empty() && b.len() <= 128 && b[0].is_ascii_alphanumeric() && b.iter().all(|c| c.is_ascii_alphanumeric() || b"._:-".contains(c));
    if ok { Ok(x) } else { fail(code) }
}

fn hex64(x: &str) -> bool {
    x.len() == 64 && x.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn endpoint<'v>(v: &'v Value, code: &'static str) -> Result<&'v str, GrantError> {
    let x = s(v, code)?;
    if hex64(x) { Ok(x) } else { fail(code) }
}

fn digest<'v>(v: &'v Value, code: &'static str) -> Result<&'v str, GrantError> {
    let x = s(v, code)?;
    if x.len() == 71 && x.starts_with("sha256:") && hex64(&x[7..]) { Ok(x) } else { fail(code) }
}

fn int(v: &Value, code: &'static str, min: u64, max: u64) -> Result<u64, GrantError> {
    v.as_u64().filter(|n| (min..=max).contains(n)).ok_or(GrantError(code))
}

pub fn range_digest(data: &[u8]) -> String {
    format!("sha256:{}", hex::encode(Sha256::digest(data)))
}

/// Open a signed envelope of `protocol`: (message, signature, key record).
fn open<'v>(env: &'v Value, protocol: &str, code: &'static str) -> Result<(&'v Value, &'v Value, &'v Value), GrantError> {
    exact(env, &["protocol", "message", "signature", "verification_key"], code)?;
    if env["protocol"] != SIGNED_MESSAGE_PROTOCOL || env["message"]["protocol"] != protocol {
        return fail(code);
    }
    Ok((&env["message"], &env["signature"], &env["verification_key"]))
}

fn envelope(signer: &Identity, message: Value) -> Result<Value, GrantError> {
    Ok(json!({
        "protocol": SIGNED_MESSAGE_PROTOCOL,
        "signature": signer.sign(&message).map_err(|_| GrantError("artifact_signing_failed"))?,
        "verification_key": signer.public_key_record(),
        "message": message,
    }))
}

const GRANT_FIELDS: [&str; 17] = [
    "protocol",
    "grant_id",
    "swarm_id",
    "seed_node_id",
    "recipient_node_id",
    "recipient_endpoint_id",
    "recipient_key_digest",
    "assignment_id",
    "manifest_pack_digest",
    "allowed_object_digests",
    "authorized_sources",
    "maximum_total_bytes",
    "maximum_bytes_per_second",
    "maximum_chunk_bytes",
    "issued_at_unix_ms",
    "not_before_unix_ms",
    "expires_at_unix_ms",
];

fn grant_shape(g: &Value, env: &Value) -> Result<Grant, GrantError> {
    let c = "artifact_grant_invalid";
    exact(g, &GRANT_FIELDS, c)?;
    for f in ["swarm_id", "seed_node_id"] {
        ident(&g[f], c)?;
    }
    let objects: Vec<String> = g["allowed_object_digests"]
        .as_array()
        .filter(|a| !a.is_empty() && a.len() <= MAX_OBJECTS)
        .ok_or(GrantError(c))?
        .iter()
        .map(|d| digest(d, c).map(str::to_owned))
        .collect::<Result<_, _>>()?;
    if objects.windows(2).any(|w| w[0] >= w[1]) {
        return fail(c);
    }
    let list = g["authorized_sources"].as_array().filter(|a| !a.is_empty() && a.len() <= MAX_SOURCES).ok_or(GrantError(c))?;
    let mut sources = BTreeMap::new();
    let mut source_addrs = BTreeMap::new();
    let mut previous = String::new();
    for src in list {
        exact(src, &["endpoint_id", "verification_key_digest", "direct_addrs"], c)?;
        let id = endpoint(&src["endpoint_id"], c)?;
        if id <= previous.as_str() {
            return fail(c);
        }
        previous = id.to_owned();
        sources.insert(id.to_owned(), digest(&src["verification_key_digest"], c)?.to_owned());
        let hints = src["direct_addrs"].as_array().filter(|a| a.len() <= 8).ok_or(GrantError(c))?;
        let mut addrs = Vec::with_capacity(hints.len());
        for hint in hints {
            let text = s(hint, c)?;
            let addr: std::net::SocketAddr = text.parse().map_err(|_| GrantError(c))?;
            if addr.to_string() != text {
                return fail(c);
            }
            addrs.push(addr);
        }
        source_addrs.insert(id.to_owned(), addrs);
    }
    // The member's enrolled endpoint id (an iroh id on a phone; any seed
    // segment in general); sources are iroh endpoints this node dials.
    let recipient_endpoint_id = ident(&g["recipient_endpoint_id"], c)?.to_owned();
    if sources.contains_key(&recipient_endpoint_id) {
        return fail(c);
    }
    let issued = int(&g["issued_at_unix_ms"], c, 1, MAX_SAFE_INTEGER)?;
    let not_before = int(&g["not_before_unix_ms"], c, 1, MAX_SAFE_INTEGER)?;
    let expires = int(&g["expires_at_unix_ms"], c, 1, MAX_SAFE_INTEGER)?;
    if issued > not_before || !(not_before < expires && expires <= not_before + MAX_GRANT_TTL_MS) {
        return fail(c);
    }
    Ok(Grant {
        grant_id: ident(&g["grant_id"], c)?.to_owned(),
        recipient_node_id: ident(&g["recipient_node_id"], c)?.to_owned(),
        recipient_endpoint_id,
        recipient_key_digest: digest(&g["recipient_key_digest"], c)?.to_owned(),
        assignment_id: ident(&g["assignment_id"], c)?.to_owned(),
        manifest_pack_digest: digest(&g["manifest_pack_digest"], c)?.to_owned(),
        allowed_object_digests: objects,
        sources,
        source_addrs,
        maximum_total_bytes: int(&g["maximum_total_bytes"], c, 1, MAX_SAFE_INTEGER)?,
        maximum_bytes_per_second: int(&g["maximum_bytes_per_second"], c, 1, MAX_SAFE_INTEGER)?,
        maximum_chunk_bytes: int(&g["maximum_chunk_bytes"], c, 1, MAX_CHUNK_BYTES)?,
        not_before_unix_ms: not_before,
        expires_at_unix_ms: expires,
        envelope: env.clone(),
    })
}

/// Verify a grant under the pinned seed key, inside its window (± skew).
pub fn verify_grant(env: &Value, pinned_seed_key: &Value, now_ms: u64) -> Result<Grant, GrantError> {
    let (message, signature, key) = open(env, GRANT_PROTOCOL, "artifact_grant_invalid")?;
    let grant = grant_shape(message, env)?;
    if key != pinned_seed_key {
        return fail("artifact_grant_signer_mismatch");
    }
    identity::verify(message, signature, key).map_err(|_| GrantError("artifact_grant_signature_invalid"))?;
    if now_ms + CLOCK_SKEW_MS < grant.not_before_unix_ms || now_ms > grant.expires_at_unix_ms + CLOCK_SKEW_MS {
        return fail("artifact_grant_ineligible");
    }
    Ok(grant)
}

impl Grant {
    /// The grant was issued for this node and this manifest.
    pub fn check_binding(&self, node_id: &str, endpoint_id: &str, key_digest: &str, pack_digest: &str) -> Result<(), GrantError> {
        if self.recipient_node_id != node_id || self.recipient_endpoint_id != endpoint_id || self.recipient_key_digest != key_digest {
            return fail("artifact_grant_recipient_mismatch");
        }
        if self.manifest_pack_digest != pack_digest {
            return fail("artifact_grant_manifest_mismatch");
        }
        Ok(())
    }
}

/// The phone's signed request for one range of one object from one source.
#[allow(clippy::too_many_arguments)]
pub fn sign_chunk_request(
    member: &Identity,
    grant: &Grant,
    request_id: &str,
    request_nonce: &str,
    source_endpoint_id: &str,
    object_digest: &str,
    offset_bytes: u64,
    length_bytes: u64,
    now_ms: u64,
) -> Result<Value, GrantError> {
    envelope(
        member,
        json!({
            "protocol": CHUNK_REQUEST_PROTOCOL,
            "request_id": request_id,
            "request_nonce": request_nonce,
            "grant": grant.envelope,
            "source_endpoint_id": source_endpoint_id,
            "object_digest": object_digest,
            "offset_bytes": offset_bytes,
            "length_bytes": length_bytes,
            "issued_at_unix_ms": now_ms,
            "expires_at_unix_ms": now_ms + REQUEST_TTL_MS,
        }),
    )
}

/// Source side: the request comes from exactly the grant's recipient, asks
/// this source for an allowed object range it holds, inside both windows.
pub fn verify_chunk_request(
    env: &Value,
    pinned_seed_key: &Value,
    source_endpoint_id: &str,
    source_key_digest: &str,
    object_size: &dyn Fn(&str) -> Option<u64>,
    now_ms: u64,
) -> Result<(ChunkRequest, Grant), GrantError> {
    let c = "artifact_chunk_request_invalid";
    let (m, signature, key) = open(env, CHUNK_REQUEST_PROTOCOL, c)?;
    exact(
        m,
        &[
            "protocol",
            "request_id",
            "request_nonce",
            "grant",
            "source_endpoint_id",
            "object_digest",
            "offset_bytes",
            "length_bytes",
            "issued_at_unix_ms",
            "expires_at_unix_ms",
        ],
        c,
    )?;
    let request = ChunkRequest {
        request_id: ident(&m["request_id"], c)?.to_owned(),
        source_endpoint_id: endpoint(&m["source_endpoint_id"], c)?.to_owned(),
        object_digest: digest(&m["object_digest"], c)?.to_owned(),
        offset_bytes: int(&m["offset_bytes"], c, 0, MAX_SAFE_INTEGER)?,
        length_bytes: int(&m["length_bytes"], c, 1, MAX_CHUNK_BYTES)?,
    };
    ident(&m["request_nonce"], c)?;
    let issued = int(&m["issued_at_unix_ms"], c, 1, MAX_SAFE_INTEGER)?;
    let expires = int(&m["expires_at_unix_ms"], c, 1, MAX_SAFE_INTEGER)?;
    let grant = verify_grant(&m["grant"], pinned_seed_key, now_ms)?;
    if key["verification_key_digest"] != grant.recipient_key_digest.as_str() || signature["signer_endpoint_id"] != grant.recipient_endpoint_id.as_str() {
        return fail("artifact_chunk_request_unauthorized");
    }
    identity::verify(m, signature, key).map_err(|_| GrantError("artifact_chunk_request_signature_invalid"))?;
    if expires <= issued || expires - issued > MAX_REQUEST_TTL_MS || issued > now_ms + CLOCK_SKEW_MS || expires + CLOCK_SKEW_MS < now_ms {
        return fail("artifact_chunk_request_ineligible");
    }
    if request.source_endpoint_id != source_endpoint_id
        || grant.sources.get(source_endpoint_id).map(String::as_str) != Some(source_key_digest)
        || grant.allowed_object_digests.binary_search(&request.object_digest).is_err()
        || request.length_bytes > grant.maximum_chunk_bytes
    {
        return fail("artifact_chunk_request_unauthorized");
    }
    let size = object_size(&request.object_digest).ok_or(GrantError("artifact_object_unavailable"))?;
    if request.offset_bytes + request.length_bytes > size {
        return fail("artifact_chunk_range_invalid");
    }
    Ok((request, grant))
}

/// Source side: sign what was served.
pub fn sign_chunk_receipt(source: &Identity, request: &ChunkRequest, grant: &Grant, data: &[u8], now_ms: u64) -> Result<Value, GrantError> {
    envelope(
        source,
        json!({
            "protocol": CHUNK_RECEIPT_PROTOCOL,
            "request_id": request.request_id,
            "grant_id": grant.grant_id,
            "source_endpoint_id": request.source_endpoint_id,
            "recipient_node_id": grant.recipient_node_id,
            "object_digest": request.object_digest,
            "offset_bytes": request.offset_bytes,
            "length_bytes": data.len(),
            "range_content_digest": range_digest(data),
            "responded_at_unix_ms": now_ms,
        }),
    )
}

/// Phone side: signed by the source key the grant authorizes, bound to this
/// request, digesting exactly the bytes received.
pub fn verify_chunk_receipt(env: &Value, request: &ChunkRequest, grant: &Grant, data: &[u8]) -> Result<(), GrantError> {
    let c = "artifact_chunk_receipt_invalid";
    let (m, signature, key) = open(env, CHUNK_RECEIPT_PROTOCOL, c)?;
    exact(
        m,
        &[
            "protocol",
            "request_id",
            "grant_id",
            "source_endpoint_id",
            "recipient_node_id",
            "object_digest",
            "offset_bytes",
            "length_bytes",
            "range_content_digest",
            "responded_at_unix_ms",
        ],
        c,
    )?;
    if grant.sources.get(&request.source_endpoint_id).map(String::as_str) != key["verification_key_digest"].as_str() {
        return fail("artifact_chunk_receipt_signer_mismatch");
    }
    identity::verify(m, signature, key).map_err(|_| GrantError("artifact_chunk_receipt_signature_invalid"))?;
    let ok = m["request_id"] == request.request_id.as_str()
        && m["grant_id"] == grant.grant_id.as_str()
        && m["source_endpoint_id"] == request.source_endpoint_id.as_str()
        && m["recipient_node_id"] == grant.recipient_node_id.as_str()
        && m["object_digest"] == request.object_digest.as_str()
        && m["offset_bytes"].as_u64() == Some(request.offset_bytes)
        && m["length_bytes"].as_u64() == Some(data.len() as u64)
        && !data.is_empty()
        && data.len() as u64 <= request.length_bytes
        && m["range_content_digest"] == range_digest(data).as_str();
    if !ok {
        return fail("artifact_chunk_receipt_mismatch");
    }
    Ok(())
}
