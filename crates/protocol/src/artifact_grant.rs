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
use crate::qwen_stage_manifest::{StageManifestError, verify_stage_manifest};

pub const GRANT_PROTOCOL: &str = "mycelium.mobile_artifact_grant.v1";
pub const CHUNK_REQUEST_PROTOCOL: &str = "mycelium.mobile_artifact_chunk_request.v1";
pub const CHUNK_RECEIPT_PROTOCOL: &str = "mycelium.mobile_artifact_chunk_receipt.v1";
pub const ARTIFACT_CHUNK_ALPN: &[u8] = b"mycelium.artifact_chunk.v1";
/// `DEFAULT_GRANT_TTL_MS` (`mycelium_mobile/artifact_grant.py:37`), A2's member-transport grant lifetime. The
/// authority value is BudgetId `PyMyceliumMobileArtifactGrantDefaultGrantTtlMsL37` (900 000 ms,
/// crates/budgets/src/table.rs:1003); protocol takes no budgets dependency, so the oracle literal is kept here.
pub const DEFAULT_GRANT_TTL_MS: i64 = 900_000;
/// `issue_grant`'s default `maximum_bytes_per_second` (`artifact_grant.py:210`, `32 << 20`).
pub const DEFAULT_MAXIMUM_BYTES_PER_SECOND: u64 = 32 << 20;
/// Time limits supplied by the authority budget registry (TODO L1 IDs).
#[derive(Clone, Copy, Debug)]
pub struct GrantLimits {
    pub max_grant_ttl_ms: u64,
    pub max_request_ttl_ms: u64,
    pub request_ttl_ms: u64,
    pub clock_skew_ms: u64,
}
pub const MAX_CHUNK_BYTES: u64 = 4 << 20;
const MAX_OBJECTS: usize = 4096;
const MAX_SOURCES: usize = 16;
/// `MAX_DIRECT_ADDRS` (`artifact_grant.py:72`): direct-address hints per source.
const MAX_DIRECT_ADDRS: usize = 8;
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
    /// Source iroh endpoint id -> its direct-address hints (`ip:port` / `[ipv6]:port`); iroh still
    /// authenticates the endpoint, so a wrong hint only fails to connect. Each is the signed text verbatim, already
    /// equal to its [`canonical_socket_addr`]. Only string hints are kept, as the oracle's consumer dials only those
    /// (`[str(a) for a in direct_addrs if isinstance(a, str)]`, `scripts/run_a12_phone_route.py:820`): a `null` hint,
    /// which the shape check admits (`artifact_grant.py:180-181, 269-271`), stays in `envelope` only. Turning a hint
    /// into a transport address is the dialler's step: `std::net::SocketAddr` cannot hold a named IPv6 zone such as
    /// `[fe80::1%en0]:4433`, which the issuer must still sign (`ipaddress` keeps the scope).
    pub source_addrs: BTreeMap<String, Vec<String>>,
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
    let ok = !b.is_empty()
        && b.len() <= 128
        && b[0].is_ascii_alphanumeric()
        && b.iter()
            .all(|c| c.is_ascii_alphanumeric() || b"._:-".contains(c));
    if ok { Ok(x) } else { fail(code) }
}

fn hex64(x: &str) -> bool {
    x.len() == 64
        && x.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn endpoint<'v>(v: &'v Value, code: &'static str) -> Result<&'v str, GrantError> {
    let x = s(v, code)?;
    if hex64(x) { Ok(x) } else { fail(code) }
}

fn digest<'v>(v: &'v Value, code: &'static str) -> Result<&'v str, GrantError> {
    let x = s(v, code)?;
    if x.len() == 71 && x.starts_with("sha256:") && hex64(&x[7..]) {
        Ok(x)
    } else {
        fail(code)
    }
}

fn int(v: &Value, code: &'static str, min: u64, max: u64) -> Result<u64, GrantError> {
    v.as_u64()
        .filter(|n| (min..=max).contains(n))
        .ok_or(GrantError(code))
}

/// `canonical_socket_addr` (`artifact_grant.py:175-189`): `ip:port` / `[ipv6]:port` in the text form Python's
/// `ipaddress` gives, or `None`. Only text can have one (`isinstance(value, str)` and a `:`); the port is the text
/// after the last `:` (`rpartition`), all decimal digits, `0 < int(port) < 65536` and `str(int(port)) == port` (no
/// leading zero); the host loses one pair of brackets only when it starts with `[` and ends with `]`, then must be
/// an `ipaddress.ip_address`. An IPv6 scope id (`%en0`, `%2`) is kept as text, which `std::net::SocketAddr` cannot
/// do for a named zone, so this works on strings and never through `SocketAddr`.
///
/// `str.isdigit` also admits non-ASCII digits, but no such port survives: `int` either raises or yields a value whose
/// `str` (ASCII) differs from the port, so only ASCII digits can be canonical here.
pub fn canonical_socket_addr(value: &Value) -> Option<String> {
    let text = value.as_str()?;
    let (host, port) = text.rsplit_once(':')?;
    // `port.isdigit()`, `0 < int(port) < 65536`, `str(int(port)) == port`; past five digits without a leading zero
    // the value is above 65535 anyway.
    let port_ok = !port.is_empty()
        && port.bytes().all(|b| b.is_ascii_digit())
        && port.len() <= 5
        && port.parse::<u32>().is_ok_and(|n| (1..=65_535).contains(&n))
        && !port.starts_with('0');
    if !port_ok {
        return None;
    }
    let host = match host.strip_prefix('[').and_then(|h| h.strip_suffix(']')) {
        Some(inner) => inner,
        None => host,
    };
    // `ipaddress.ip_address` (ipaddress.py:28-52): IPv4 first, then IPv6.
    if let Some(v4) = ipv4_from_text(host) {
        return Some(format!("{}:{port}", ipv4_text(v4)));
    }
    let (v6, scope) = ipv6_from_text(host)?;
    let mut ip = ipv6_text(v6);
    if let Some(scope) = scope {
        ip.push('%');
        ip.push_str(scope);
    }
    Some(format!("[{ip}]:{port}"))
}

/// `canonical_socket_addr(addr) == addr` (`artifact_grant.py:269-271`): `None` maps to `None`, so a JSON `null`
/// hint passes; any other non-text value has no canonical form and fails.
fn canonical_hint(hint: &Value) -> bool {
    match canonical_socket_addr(hint) {
        Some(canonical) => hint.as_str() == Some(canonical.as_str()),
        None => hint.is_null(),
    }
}

/// `IPv4Address(text)` (`_ip_int_from_string`, `_parse_octet`): no `/`, exactly four dot-separated octets of one to
/// three ASCII digits, no leading zero, each at most 255.
fn ipv4_from_text(text: &str) -> Option<u32> {
    if text.is_empty() || text.contains('/') {
        return None;
    }
    let octets: Vec<&str> = text.split('.').collect();
    if octets.len() != 4 {
        return None;
    }
    let mut value = 0u32;
    for octet in octets {
        if octet.is_empty()
            || !octet.bytes().all(|b| b.is_ascii_digit())
            || octet.len() > 3
            || (octet != "0" && octet.starts_with('0'))
        {
            return None;
        }
        let byte: u8 = octet.parse().ok()?;
        value = (value << 8) | u32::from(byte);
    }
    Some(value)
}

/// `IPv4Address.__str__`: dotted decimal.
fn ipv4_text(value: u32) -> String {
    let [a, b, c, d] = value.to_be_bytes();
    format!("{a}.{b}.{c}.{d}")
}

/// `IPv6Address(text)` (`__init__`, `_split_scope_id`, `_ip_int_from_string`, `_parse_hextet`): the address and its
/// scope id, if any.
fn ipv6_from_text(text: &str) -> Option<(u128, Option<&str>)> {
    if text.contains('/') {
        return None;
    }
    let (address, scope) = match text.split_once('%') {
        None => (text, None),
        Some((_, scope)) if scope.is_empty() || scope.contains('%') => return None,
        Some((address, scope)) => (address, Some(scope)),
    };
    if address.is_empty() || address.chars().count() > 45 {
        return None;
    }
    const HEXTETS: usize = 8;
    let mut parts: Vec<String> = address
        .splitn(HEXTETS + 2, ':')
        .map(str::to_owned)
        .collect();
    if parts.len() < 3 {
        return None;
    }
    // An IPv4-style suffix becomes two hextets.
    if parts.last().is_some_and(|last| last.contains('.')) {
        let v4 = ipv4_from_text(&parts.pop()?)?;
        parts.push(format!("{:x}", (v4 >> 16) & 0xffff));
        parts.push(format!("{:x}", v4 & 0xffff));
    }
    if parts.len() > HEXTETS + 1 {
        return None;
    }
    let mut skip = None;
    for (i, part) in parts.iter().enumerate().take(parts.len() - 1).skip(1) {
        if part.is_empty() {
            if skip.is_some() {
                return None;
            }
            skip = Some(i);
        }
    }
    let (first_empty, last_empty) = (parts[0].is_empty(), parts[parts.len() - 1].is_empty());
    let (hi, lo, skipped) = match skip {
        Some(skip) => {
            let (mut hi, mut lo) = (skip, parts.len() - skip - 1);
            if first_empty {
                hi -= 1;
                if hi != 0 {
                    return None;
                }
            }
            if last_empty {
                lo -= 1;
                if lo != 0 {
                    return None;
                }
            }
            if hi + lo >= HEXTETS {
                return None;
            }
            (hi, lo, HEXTETS - (hi + lo))
        }
        None => {
            if parts.len() != HEXTETS || first_empty || last_empty {
                return None;
            }
            (HEXTETS, 0, 0)
        }
    };
    let mut value = 0u128;
    for part in &parts[..hi] {
        value = (value << 16) | u128::from(hextet(part)?);
    }
    for _ in 0..skipped {
        value <<= 16;
    }
    for part in &parts[parts.len() - lo..] {
        value = (value << 16) | u128::from(hextet(part)?);
    }
    Some((value, scope))
}

/// `_parse_hextet`: one to four ASCII hex digits.
fn hextet(text: &str) -> Option<u16> {
    if text.is_empty() || text.len() > 4 || !text.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    u16::from_str_radix(text, 16).ok()
}

/// `IPv6Address.__str__` without the scope, as the oracle's Python 3.13 `ipaddress` gives it (the IPv4-mapped form
/// `fixtures/grant-address-cases.json` pins): an IPv4-mapped address as `_ipv4_mapped_ipv6_to_str` (the high 96 bits
/// in hextet form, then the dotted IPv4), otherwise `_string_from_ip_int`: lower-case hextets without
/// leading zeros, the first longest run of two or more zero hextets as `::` (`_compress_hextets`).
fn ipv6_text(value: u128) -> String {
    if value >> 32 == 0xffff {
        // `ipv4_mapped`: the low 32 bits.
        return format!("{}:{}", ipv6_text(value >> 32), ipv4_text(value as u32));
    }
    let mut hextets: Vec<String> = (0..8)
        .rev()
        .map(|i| format!("{:x}", (value >> (16 * i)) & 0xffff))
        .collect();
    let (mut best_start, mut best_len, mut start, mut len) = (0usize, 0usize, 0usize, 0usize);
    for (index, hextet) in hextets.iter().enumerate() {
        if hextet == "0" {
            if len == 0 {
                start = index;
            }
            len += 1;
            if len > best_len {
                (best_start, best_len) = (start, len);
            }
        } else {
            len = 0;
        }
    }
    if best_len > 1 {
        let end = best_start + best_len;
        if end == hextets.len() {
            hextets.push(String::new());
        }
        hextets.splice(best_start..end, [String::new()]);
        if best_start == 0 {
            hextets.insert(0, String::new());
        }
    }
    hextets.join(":")
}

pub fn range_digest(data: &[u8]) -> String {
    format!("sha256:{}", hex::encode(Sha256::digest(data)))
}

/// Open a signed envelope of `protocol`: (message, signature, key record).
fn open<'v>(
    env: &'v Value,
    protocol: &str,
    code: &'static str,
) -> Result<(&'v Value, &'v Value, &'v Value), GrantError> {
    exact(
        env,
        &["protocol", "message", "signature", "verification_key"],
        code,
    )?;
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

fn grant_shape(g: &Value, env: &Value, limits: GrantLimits) -> Result<Grant, GrantError> {
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
    let list = g["authorized_sources"]
        .as_array()
        .filter(|a| !a.is_empty() && a.len() <= MAX_SOURCES)
        .ok_or(GrantError(c))?;
    let mut sources = BTreeMap::new();
    let mut source_addrs = BTreeMap::new();
    let mut previous = String::new();
    for src in list {
        exact(
            src,
            &["endpoint_id", "verification_key_digest", "direct_addrs"],
            c,
        )?;
        let id = endpoint(&src["endpoint_id"], c)?;
        if id <= previous.as_str() {
            return fail(c);
        }
        previous = id.to_owned();
        sources.insert(
            id.to_owned(),
            digest(&src["verification_key_digest"], c)?.to_owned(),
        );
        let hints = src["direct_addrs"]
            .as_array()
            .filter(|a| a.len() <= MAX_DIRECT_ADDRS)
            .ok_or(GrantError(c))?;
        // :269-271 `if canonical_socket_addr(addr) != addr: _fail(code)`, on the JSON value itself.
        if !hints.iter().all(canonical_hint) {
            return fail(c);
        }
        source_addrs.insert(
            id.to_owned(),
            hints
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect(),
        );
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
    if issued > not_before
        || !(not_before < expires
            && u128::from(expires) <= u128::from(not_before) + u128::from(limits.max_grant_ttl_ms))
    {
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

/// Who and what one grant binds: `issue_grant`'s identity arguments (`artifact_grant.py:199-205`).
#[derive(Debug, Clone, Copy)]
pub struct GrantBinding<'a> {
    pub grant_id: &'a str,
    pub swarm_id: &'a str,
    pub seed_node_id: &'a str,
    pub recipient_node_id: &'a str,
    pub recipient_endpoint_id: &'a str,
    pub recipient_key_digest: &'a str,
    pub assignment_id: &'a str,
}

/// Why [`issue_grant`] refused: the manifest's own `StageManifestError` (`verify_stage_manifest` raises it
/// through `issue_grant` unchanged, `artifact_grant.py:217`) or an `ArtifactGrantError`.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum IssueGrantError {
    #[error(transparent)]
    Manifest(#[from] StageManifestError),
    #[error(transparent)]
    Grant(#[from] GrantError),
}

/// `issue_grant` (`artifact_grant.py:196-245`): seed-sign a grant for exactly the objects one verified v2 manifest
/// pins. `now_unix_ms` and `ttl_ms` are the oracle's Python integers; the rate and chunk caps are its defaults
/// (:210-211). `limits.max_grant_ttl_ms` is `_check_grant_shape`'s `MAX_GRANT_TTL_MS`.
pub fn issue_grant(
    seed_signer: &Identity,
    binding: &GrantBinding<'_>,
    manifest: &Value,
    sources: &Value,
    now_unix_ms: i64,
    ttl_ms: i64,
    limits: GrantLimits,
) -> Result<Value, IssueGrantError> {
    let c = "artifact_grant_invalid";
    // :217: the manifest's own refusal (the architecture maxima :235-237 included) propagates unchanged.
    let verified = verify_stage_manifest(manifest)?;
    // :218, :235: one size per digest, the last occurrence winning (`{sha256: size_bytes}`); the digests sorted
    // and unique, the budget their sum.
    let mut sizes: BTreeMap<&str, u64> = BTreeMap::new();
    for tensor in verified["tensors"].as_array().ok_or(GrantError(c))? {
        let digest = tensor["sha256"].as_str().ok_or(GrantError(c))?;
        sizes.insert(digest, tensor["size_bytes"].as_u64().ok_or(GrantError(c))?);
    }
    let total = sizes
        .values()
        .try_fold(0u64, |sum, size| sum.checked_add(*size))
        .ok_or(GrantError(c))?;
    let authorized = authorized_sources(sources)?;
    // :240: an `expires` past i64 is one `_check_grant_shape` refuses either way.
    let expires = now_unix_ms.checked_add(ttl_ms).ok_or(GrantError(c))?;
    let message = json!({
        "protocol": GRANT_PROTOCOL,
        "grant_id": binding.grant_id,
        "swarm_id": binding.swarm_id,
        "seed_node_id": binding.seed_node_id,
        "recipient_node_id": binding.recipient_node_id,
        "recipient_endpoint_id": binding.recipient_endpoint_id,
        "recipient_key_digest": binding.recipient_key_digest,
        "assignment_id": binding.assignment_id,
        "manifest_pack_digest": verified["pack_digest"],
        "allowed_object_digests": sizes.keys().collect::<Vec<_>>(),
        "authorized_sources": authorized,
        "maximum_total_bytes": total,
        "maximum_bytes_per_second": DEFAULT_MAXIMUM_BYTES_PER_SECOND,
        "maximum_chunk_bytes": MAX_CHUNK_BYTES,
        "issued_at_unix_ms": now_unix_ms,
        "not_before_unix_ms": now_unix_ms,
        "expires_at_unix_ms": expires,
    });
    // :242-243
    grant_shape(&message, &Value::Null, limits)?;
    Ok(envelope(seed_signer, message)?)
}

/// `sorted(({"direct_addrs": [], **dict(s)} for s in sources), key=lambda s: s["endpoint_id"])`
/// (`artifact_grant.py:230-232`). Python's `dict(s)` also accepts a source given as a list of key/value pairs, but
/// the only product writer, `issue_route_assignment`, normalises every source to an object (`dict(v)` in
/// `_artifact_sources`, `mycelium_seed/route_assignment.py:452-475`) before it persists them
/// (`canonical_json_bytes(sources)`, :436), so a stored pair list never reaches `issue_grant`; the port therefore
/// refuses a non-object source (fail closed). Where the oracle raises (sources that are not a list, a source that
/// `dict` cannot convert, a missing or non-text `endpoint_id`) or `_check_grant_shape` would refuse the result
/// anyway (non-text ids), the grant is refused as malformed.
fn authorized_sources(sources: &Value) -> Result<Vec<Value>, GrantError> {
    let c = "artifact_grant_invalid";
    let mut keyed: Vec<(String, Value)> = Vec::new();
    for source in sources.as_array().ok_or(GrantError(c))? {
        let mut merged = serde_json::Map::new();
        merged.insert("direct_addrs".to_owned(), Value::Array(Vec::new()));
        merged.extend(source.as_object().ok_or(GrantError(c))?.clone());
        let id = merged
            .get("endpoint_id")
            .and_then(Value::as_str)
            .ok_or(GrantError(c))?
            .to_owned();
        keyed.push((id, Value::Object(merged)));
    }
    // Stable, as `sorted` is; equal ids stay in order and `_check_grant_shape` refuses them.
    keyed.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(keyed.into_iter().map(|(_, source)| source).collect())
}

/// Verify a grant under the pinned seed key, inside its window (± skew).
pub fn verify_grant(
    env: &Value,
    pinned_seed_key: &Value,
    now_ms: u64,
    limits: GrantLimits,
) -> Result<Grant, GrantError> {
    let (message, signature, key) = open(env, GRANT_PROTOCOL, "artifact_grant_invalid")?;
    let grant = grant_shape(message, env, limits)?;
    if key != pinned_seed_key {
        return fail("artifact_grant_signer_mismatch");
    }
    identity::verify(message, signature, key)
        .map_err(|_| GrantError("artifact_grant_signature_invalid"))?;
    if u128::from(now_ms) + u128::from(limits.clock_skew_ms) < u128::from(grant.not_before_unix_ms)
        || u128::from(now_ms)
            > u128::from(grant.expires_at_unix_ms) + u128::from(limits.clock_skew_ms)
    {
        return fail("artifact_grant_ineligible");
    }
    Ok(grant)
}

impl Grant {
    /// The grant was issued for this node and this manifest.
    pub fn check_binding(
        &self,
        node_id: &str,
        endpoint_id: &str,
        key_digest: &str,
        pack_digest: &str,
    ) -> Result<(), GrantError> {
        if self.recipient_node_id != node_id
            || self.recipient_endpoint_id != endpoint_id
            || self.recipient_key_digest != key_digest
        {
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
    limits: GrantLimits,
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
            "expires_at_unix_ms": now_ms.checked_add(limits.request_ttl_ms).ok_or(GrantError("artifact_chunk_request_invalid"))?,
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
    limits: GrantLimits,
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
    let grant = verify_grant(&m["grant"], pinned_seed_key, now_ms, limits)?;
    if key["verification_key_digest"] != grant.recipient_key_digest.as_str()
        || signature["signer_endpoint_id"] != grant.recipient_endpoint_id.as_str()
    {
        return fail("artifact_chunk_request_unauthorized");
    }
    identity::verify(m, signature, key)
        .map_err(|_| GrantError("artifact_chunk_request_signature_invalid"))?;
    if expires <= issued
        || expires - issued > limits.max_request_ttl_ms
        || u128::from(issued) > u128::from(now_ms) + u128::from(limits.clock_skew_ms)
        || u128::from(expires) + u128::from(limits.clock_skew_ms) < u128::from(now_ms)
    {
        return fail("artifact_chunk_request_ineligible");
    }
    if request.source_endpoint_id != source_endpoint_id
        || grant.sources.get(source_endpoint_id).map(String::as_str) != Some(source_key_digest)
        || grant
            .allowed_object_digests
            .binary_search(&request.object_digest)
            .is_err()
        || request.length_bytes > grant.maximum_chunk_bytes
    {
        return fail("artifact_chunk_request_unauthorized");
    }
    let size =
        object_size(&request.object_digest).ok_or(GrantError("artifact_object_unavailable"))?;
    if request.offset_bytes + request.length_bytes > size {
        return fail("artifact_chunk_range_invalid");
    }
    Ok((request, grant))
}

/// Source side: sign what was served.
pub fn sign_chunk_receipt(
    source: &Identity,
    request: &ChunkRequest,
    grant: &Grant,
    data: &[u8],
    now_ms: u64,
) -> Result<Value, GrantError> {
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
pub fn verify_chunk_receipt(
    env: &Value,
    request: &ChunkRequest,
    grant: &Grant,
    data: &[u8],
) -> Result<(), GrantError> {
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
    if grant
        .sources
        .get(&request.source_endpoint_id)
        .map(String::as_str)
        != key["verification_key_digest"].as_str()
    {
        return fail("artifact_chunk_receipt_signer_mismatch");
    }
    identity::verify(m, signature, key)
        .map_err(|_| GrantError("artifact_chunk_receipt_signature_invalid"))?;
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
