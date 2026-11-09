// SPDX-License-Identifier: AGPL-3.0-only
//! The seed HTTPS client of `RemoteMacMember.accept_handoff` (`mycelium_seed/http.py:821-1000` @
//! f4e366e9): `identity` (a pinned, signed, time-windowed seed statement) and `join` over one
//! no-redirect, frame-bounded, canonical-JSON HTTPS exchange.
//!
//! Trust: the system store (`rustls-native-certs`, which honours `SSL_CERT_FILE`/`SSL_CERT_DIR`
//! exactly like Python's default context), or - for an installation-local seed - one pinned CA
//! (`pinned_seed_client_from_invite(ca_certificate=...)`). `connect_to` redirects the TCP dial
//! (`transport_url`) while the TLS name and the request origin stay the public one.
use crate::schema::SeedWindow;
use budgets::{BudgetId, Entries};
use rustls::{ClientConfig, ClientConnection, RootCertStore, StreamOwned};
use rustls_pki_types::{CertificateDer, ServerName, pem::PemObject};
use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    net::{SocketAddr, TcpStream, ToSocketAddrs},
    sync::Arc,
    time::{Duration, Instant},
};

const SEED_IDENTITY_PROTOCOL: &str = "mycelium.seed.identity.v1";
const SEED_ENVELOPE_PROTOCOL: &str = "mycelium.seed.signed_envelope.v1";
const SEED_JOIN_HTTP_PROTOCOL: &str = "mycelium.seed.join_http.v1";
const SEED_RESUME_HTTP_PROTOCOL: &str = "mycelium.seed.resume_http.v1";
const SEED_MEMBER_HTTP_PROTOCOL: &str = "mycelium.seed.member_http.v1";
const HEARTBEAT_PROTOCOL: &str = "mycelium.membership.heartbeat.v1";
const LEASE_RENEWAL_PROTOCOL: &str = "mycelium.membership.lease_renewal.v1";
const SEED_HTTP_ERROR_PROTOCOL: &str = "mycelium.seed.http_error.v1";
const STATEMENT_FIELDS: [&str; 7] = [
    "protocol",
    "swarm_id",
    "seed_node_id",
    "seed_endpoint_id",
    "seed_url",
    "issued_at",
    "expires_at",
];
/// `JOIN_ROUTE_ERROR_STATUSES`: the codes a join refusal may expose, with their only valid status.
const JOIN_ROUTE_ERRORS: &[(&str, u16)] = &[
    ("invite_expired", 400),
    ("invite_malformed", 400),
    ("invite_protocol_invalid", 400),
    ("invite_replayed", 409),
    ("invite_signature_invalid", 401),
    ("join_request_protocol_required", 400),
    ("membership_endpoint_addr_invalid", 400),
    ("membership_endpoint_id_mismatch", 400),
    ("membership_envelope_invalid", 400),
    ("membership_field_unusable", 400),
    ("membership_fields_invalid", 400),
    ("membership_generation_invalid", 400),
    ("membership_identifier_invalid", 400),
    ("membership_join_generation_invalid", 400),
    ("membership_key_pin_invalid", 401),
    ("membership_message_expired", 400),
    ("membership_message_from_future", 400),
    ("membership_message_invalid", 400),
    ("membership_contract_upgrade_required", 426),
    ("membership_peer_class_invalid", 400),
    ("membership_peer_profile_binding_mismatch", 400),
    ("membership_peer_profile_invalid", 400),
    ("membership_v2_before_cutover", 400),
    ("membership_v2_peer_profile_invalid", 400),
    ("membership_v2_profile_generation_mismatch", 400),
    ("membership_v2_profile_has_no_consumer_projection", 400),
    ("membership_protocol_invalid", 400),
    ("membership_runtime_capability_invalid", 400),
    ("membership_runtime_capability_mismatch", 400),
    ("membership_signature_invalid", 401),
    ("membership_text_invalid", 400),
    ("membership_time_invalid", 400),
    ("membership_ttl_invalid", 400),
    ("membership_verifier_invalid", 400),
    ("seed_join_key_invalid", 400),
    ("seed_join_mismatch", 400),
    ("seed_join_retry_mismatch", 400),
    ("seed_member_identity_reused", 400),
    ("seed_node_endpoint_conflict", 400),
    ("seed_node_key_conflict", 409),
];

/// `SeedHTTPError`: a stable code, the HTTP status when there was one, and for a
/// `seed_http_seed_time_invalid` the window numbers (`_seed_time_invalid`).
#[derive(Clone, Debug, PartialEq)]
pub struct SeedError {
    pub code: String,
    pub status: Option<u16>,
    pub window: Option<SeedWindow>,
    /// The `http.client` exception class (`BadStatusLine`, `IncompleteRead`, ...) that escaped
    /// `SeedHTTPClient._request` untranslated (it is not an `OSError`); `code` is then a placeholder.
    pub raw: Option<&'static str>,
}
impl SeedError {
    fn new(code: &str) -> Self {
        Self {
            code: code.to_owned(),
            status: None,
            window: None,
            raw: None,
        }
    }
    fn with_status(code: &str, status: u16) -> Self {
        Self {
            status: Some(status),
            ..Self::new(code)
        }
    }
}

/// How one client reaches the seed.
#[derive(Clone, Debug, Default)]
pub struct SeedOptions {
    /// One installation-local CA (PEM); `None` = the system trust store.
    pub ca_pem: Option<Vec<u8>>,
    /// Dial this address instead of the origin's (`_OwnerLoopbackTransport`).
    pub connect_to: Option<SocketAddr>,
    /// Verify the certificate for this name instead of the origin's host (the owner's own loopback
    /// seed answers for `127.0.0.1` while its public identity is the relay origin).
    pub server_name: Option<String>,
}

#[derive(Debug)]
pub struct SeedClient {
    seed_url: String,
    host: String,
    port: u16,
    swarm_id: String,
    key_digest: String,
    timeout: Duration,
    frame_limit: u64,
    skew: f64,
    tls: Arc<ClientConfig>,
    connect_to: Option<SocketAddr>,
    server_name: Option<String>,
    /// `_rotation_transition`: the verified dual-signed key transition last fetched, if any.
    transition: Option<Transition>,
}

/// The fields of a verified `mycelium.seed_key_transition.v1` that govern which seed key signs.
#[derive(Clone, Debug)]
struct Transition {
    old_digest: Value,
    new_digest: Value,
    effective_at: f64,
    overlap_expires_at: f64,
}
/// `float(value)` of a verified transition time (`bool`, number or numeric text).
fn python_float(value: &Value) -> f64 {
    match value {
        Value::Bool(b) => f64::from(u8::from(*b)),
        Value::String(text) => text.trim().replace('_', "").parse().unwrap_or(f64::NAN),
        other => other.as_f64().unwrap_or(f64::NAN),
    }
}

/// The one `User-Agent` every Mycelium client sends toward a public origin
/// (`mycelium_http_identity.USER_AGENT`, PV18): Cloudflare answers 1010 to a bare client.
const USER_AGENT: &str = crate::join_client::USER_AGENT;
fn timeout() -> Duration {
    BudgetId::PyMyceliumA13MacosPeerRemotemacmemberInitTimeoutL117
        .span(Entries::new(0))
        .map_or(Duration::ZERO, |s| Duration::from_millis(s.millis()))
}
fn skew_seconds() -> f64 {
    BudgetId::PyMyceliumMembershipContractsMaxClockSkewSecondsL31
        .span(Entries::new(0))
        .map_or(0.0, |s| s.millis() as f64 / 1000.0)
}
fn frame_limit() -> u64 {
    BudgetId::PyMyceliumSeedHttpMaxHttpFrameBytesL55
        .bytes()
        .map_or(0, |b| b.get())
}

fn tls_config(options: &SeedOptions) -> Result<Arc<ClientConfig>, SeedError> {
    let mut roots = RootCertStore::empty();
    match &options.ca_pem {
        Some(pem) => {
            for cert in CertificateDer::pem_slice_iter(pem) {
                let cert = cert.map_err(|_| SeedError::new("local_tls_material_invalid"))?;
                roots
                    .add(cert)
                    .map_err(|_| SeedError::new("local_tls_material_invalid"))?;
            }
        }
        None => {
            // Roots that fail to parse are skipped, as the system store does.
            let loaded = rustls_native_certs::load_native_certs();
            roots.add_parsable_certificates(loaded.certs);
        }
    }
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let config = ClientConfig::builder_with_provider(provider)
        .with_protocol_versions(&[&rustls::version::TLS13, &rustls::version::TLS12])
        .map_err(|_| SeedError::new("seed_http_unreachable"))?
        .with_root_certificates(roots)
        .with_no_client_auth();
    Ok(Arc::new(config))
}

/// `https://host[:port]` split (the origin is already canonical: `canonical_https_origin`).
fn split_origin(origin: &str) -> Option<(String, u16)> {
    let netloc = origin.strip_prefix("https://")?;
    match netloc.rsplit_once(':') {
        Some((host, port)) => Some((host.to_owned(), port.parse().ok()?)),
        None => Some((netloc.to_owned(), 443)),
    }
}

impl SeedClient {
    /// `SeedHTTPClient(seed_url, swarm_id, seed_key_digest, seed_key_records)` from a verified
    /// invite's payload.
    pub fn new(
        seed_url: &str,
        swarm_id: &str,
        key_digest: &str,
        key_records: &[Value],
        options: &SeedOptions,
    ) -> Result<Self, SeedError> {
        let invalid = |m: &str| SeedError::new(m);
        let (host, port) = split_origin(seed_url).ok_or_else(|| invalid("seed_url is invalid"))?;
        if key_records.len() != 1
            || key_records[0].get("verification_key_digest") != Some(&json!(key_digest))
        {
            return Err(invalid("seed key pin mismatch"));
        }
        Ok(Self {
            seed_url: seed_url.to_owned(),
            host,
            port,
            swarm_id: swarm_id.to_owned(),
            key_digest: key_digest.to_owned(),
            timeout: timeout(),
            frame_limit: frame_limit(),
            skew: skew_seconds(),
            tls: tls_config(options)?,
            connect_to: options.connect_to,
            server_name: options.server_name.clone(),
            transition: None,
        })
    }

    /// `ManagedControlClient._request(path, envelope)`: POST one signed envelope to an application
    /// route the seed mounts (demo-run exchange, managed control). The answer is unverified here:
    /// the caller verifies the owner's signature and binding.
    pub fn post(&self, path: &str, envelope: &Value) -> Result<Value, SeedError> {
        self.request("POST", path, Some(envelope))
    }

    /// One request; the canonical-JSON object it answers (`_request`).
    fn request(&self, method: &str, path: &str, body: Option<&Value>) -> Result<Value, SeedError> {
        let unreachable = || SeedError::new("seed_http_unreachable");
        let raw = match body {
            Some(body) => Some(protocol::canonical::to_bytes(body).map_err(|_| unreachable())?),
            None => None,
        };
        let deadline = Instant::now() + self.timeout;
        let address = match self.connect_to {
            Some(address) => address,
            None => (self.host.as_str(), self.port)
                .to_socket_addrs()
                .map_err(|_| unreachable())?
                .next()
                .ok_or_else(unreachable)?,
        };
        let tcp = TcpStream::connect_timeout(&address, self.timeout).map_err(|_| unreachable())?;
        tcp.set_read_timeout(Some(self.timeout))
            .and_then(|()| tcp.set_write_timeout(Some(self.timeout)))
            .map_err(|_| unreachable())?;
        let name = ServerName::try_from(
            self.server_name
                .clone()
                .unwrap_or_else(|| self.host.clone()),
        )
        .map_err(|_| unreachable())?;
        let connection =
            ClientConnection::new(self.tls.clone(), name).map_err(|_| unreachable())?;
        let mut stream = StreamOwned::new(connection, tcp);
        let authority = if self.port == 443 {
            self.host.clone()
        } else {
            format!("{}:{}", self.host, self.port)
        };
        let mut head = format!(
            "{method} {path} HTTP/1.1\r\nHost: {authority}\r\nAccept-Encoding: identity\r\nUser-Agent: {USER_AGENT}\r\nConnection: close\r\n"
        );
        if let Some(raw) = &raw {
            head.push_str(&format!(
                "Content-Type: application/json\r\nContent-Length: {}\r\n",
                raw.len()
            ));
        }
        head.push_str("\r\n");
        let mut wire = head.into_bytes();
        wire.extend_from_slice(raw.as_deref().unwrap_or_default());
        stream
            .write_all(&wire)
            .and_then(|()| stream.flush())
            .map_err(|_| unreachable())?;
        // Bounded by the frame limit plus the head; a peer that hangs up early is unreachable.
        let mut reply = Vec::new();
        let cap = self.frame_limit + 16 * 1024;
        let mut chunk = [0u8; 16 * 1024];
        loop {
            if Instant::now() > deadline {
                return Err(unreachable());
            }
            match stream.read(&mut chunk) {
                Ok(0) => break,
                Ok(n) => {
                    reply.extend_from_slice(&chunk[..n]);
                    // `http.client` returns on the framing, not on the hang-up.
                    if reply.len() as u64 > cap || crate::framing::response_complete(&reply) {
                        break;
                    }
                }
                // A TLS close without close_notify after a full body is still a full body.
                Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => break,
                Err(_) => return Err(unreachable()),
            }
        }
        self.interpret(path, &reply)
    }

    fn interpret(&self, path: &str, reply: &[u8]) -> Result<Value, SeedError> {
        let amt = usize::try_from(self.frame_limit)
            .unwrap_or(usize::MAX)
            .saturating_add(1);
        let Parsed {
            status,
            content_types,
            body,
        } = read_response(reply, amt).map_err(|wire| match wire {
            Wire::Unreachable => SeedError::new("seed_http_unreachable"),
            Wire::Raw(class) => SeedError {
                raw: Some(class),
                ..SeedError::new("seed_http_unreachable")
            },
        })?;
        let exact_json = content_types == ["application/json"];
        if !(200..300).contains(&status) {
            // urllib raises `HTTPError` for every status outside 2xx (the redirect handler refuses 3xx).
            let mut exposed = if exact_json {
                remote_error_code(&body, self.frame_limit)
            } else {
                None
            };
            if path == "/seed/join"
                && exposed.as_deref().is_none_or(|code| {
                    JOIN_ROUTE_ERRORS
                        .iter()
                        .find(|(c, _)| *c == code)
                        .map(|(_, s)| *s)
                        != Some(status)
                })
            {
                exposed = None;
            }
            return Err(SeedError::with_status(
                exposed.as_deref().unwrap_or("seed_http_remote_error"),
                status,
            ));
        }
        if body.len() as u64 > self.frame_limit {
            return Err(SeedError::with_status(
                "seed_http_response_too_large",
                status,
            ));
        }
        if !exact_json {
            return Err(SeedError::with_status("seed_http_remote_error", status));
        }
        match canonical_loads(&body)? {
            value @ Value::Object(_) => Ok(value),
            _ => Err(SeedError::with_status("seed_http_response_invalid", status)),
        }
    }

    /// `identity`: GET `/seed/identity`, then verify the statement at the clock sampled AFTER the
    /// response arrived (`now()` for a callable).
    pub fn identity(&self, now: &dyn Fn() -> f64) -> Result<Value, SeedError> {
        let envelope = self.request("GET", "/seed/identity", None)?;
        self.verify_envelope(&envelope, now())
    }

    /// `join`: POST the invite token and the signed join request.
    pub fn join(&self, invite_token: &str, join_envelope: &Value) -> Result<Value, SeedError> {
        self.request(
            "POST",
            "/seed/join",
            Some(&json!({
                "protocol": SEED_JOIN_HTTP_PROTOCOL,
                "invite_token": invite_token,
                "join_envelope": join_envelope,
            })),
        )
    }

    /// `resume`: POST the signed resume request.
    pub fn resume(&self, resume_envelope: &Value) -> Result<Value, SeedError> {
        self.request(
            "POST",
            "/seed/resume",
            Some(&json!({
                "protocol": SEED_RESUME_HTTP_PROTOCOL,
                "resume_envelope": resume_envelope,
            })),
        )
    }

    /// `send_member_message` for a HEARTBEAT (http.py:1109-1162): POST the signed heartbeat, sample the
    /// clock AFTER the response arrived, and verify the seed's lease renewal against the pinned (or
    /// transition-held) key, the swarm, the two node ids and the heartbeat it answers. The receipt branch
    /// (capability and other member messages) is not ported.
    pub fn send_heartbeat(
        &self,
        envelope: &Value,
        now: &dyn Fn() -> f64,
    ) -> Result<Value, SeedError> {
        let invalid = || SeedError::new("seed_http_member_envelope_invalid");
        let message = envelope
            .get("message")
            .filter(|m| m.is_object())
            .ok_or_else(invalid)?;
        let expected_id = match message.get("message_id") {
            Some(Value::String(id)) if !id.is_empty() => id.clone(),
            _ => return Err(invalid()),
        };
        debug_assert_eq!(message["protocol"], HEARTBEAT_PROTOCOL);
        let response = self.request(
            "POST",
            "/seed/message",
            Some(&json!({"protocol": SEED_MEMBER_HTTP_PROTOCOL, "envelope": envelope})),
        )?;
        let now = now();
        if !now.is_finite() {
            return Err(SeedError::new("seed_clock_invalid")); // the caller's clock raised in the source
        }
        let digest = response
            .get("verification_key")
            .filter(|record| record.is_object())
            .and_then(|record| record.get("verification_key_digest"));
        // A pin that is not accepted is a `MembershipContractError` the source re-raises as a `SeedHTTPError`.
        if !self.digest_accepted(digest, now) {
            return Err(SeedError::new("membership_key_pin_invalid"));
        }
        let Some(Value::String(response_digest)) = digest else {
            return Err(SeedError::new("membership_key_pin_invalid"));
        };
        let renewal = protocol::membership::verify(
            &response,
            protocol::membership::Expected {
                protocol: LEASE_RENEWAL_PROTOCOL,
                key_digest: response_digest,
                swarm_id: Some(self.swarm_id.as_str()),
                sender_node_id: message["recipient_node_id"].as_str(),
                sender_endpoint_id: None,
                recipient_node_id: message["sender_node_id"].as_str(),
            },
            now,
            crate::member::limits(),
        )
        .map_err(|e| SeedError::new(e.0))?;
        if renewal["heartbeat_message_id"] != expected_id.as_str()
            || renewal["member_incarnation"] != message["incarnation"]
            || renewal["membership_generation"] != message["generation"]
        {
            return Err(SeedError::new("seed_http_lease_renewal_mismatch"));
        }
        Ok(response)
    }

    /// `rotation`: fetch and verify a dual-signed rotation under the current pin; `None` when the
    /// seed answers `seed_rotation_absent` with 404. A verified transition then governs which seed
    /// key `identity` and `accepted_seed_key_digest` accept (`_seed_digest_accepted`).
    pub fn rotation(&mut self, now: f64) -> Result<Option<Value>, SeedError> {
        let envelope = match self.request("GET", "/seed/rotation", None) {
            Err(e) if e.code == "seed_rotation_absent" && e.status == Some(404) => {
                return Ok(None);
            }
            other => other?,
        };
        let transition = protocol::seed_transition::verify(&envelope, &json!(now), None)
            .map_err(|e| SeedError::new(e.0))?;
        let (old, new) = (
            transition["old_seed_key_digest"].clone(),
            transition["new_seed_key_digest"].clone(),
        );
        if old != self.key_digest.as_str() && new != self.key_digest.as_str() {
            return Err(SeedError::new("seed_operator_rotation_record_invalid"));
        }
        self.transition = Some(Transition {
            old_digest: old,
            new_digest: new,
            effective_at: python_float(&transition["effective_at"]),
            overlap_expires_at: python_float(&transition["overlap_expires_at"]),
        });
        Ok(Some(envelope))
    }

    /// `_seed_digest_accepted`: the pinned digest, or - once a transition is held - the old key
    /// before `effective_at`, either key through the overlap, the new key after it.
    fn digest_accepted(&self, digest: Option<&Value>, now: f64) -> bool {
        let Some(transition) = &self.transition else {
            return digest == Some(&json!(self.key_digest));
        };
        if now < transition.effective_at {
            digest == Some(&transition.old_digest)
        } else if now <= transition.overlap_expires_at {
            digest == Some(&transition.old_digest) || digest == Some(&transition.new_digest)
        } else {
            digest == Some(&transition.new_digest)
        }
    }

    /// `accepted_seed_key_digest`: the trusted signer digest a seed message carries.
    pub fn accepted_seed_key_digest(
        &self,
        envelope: &Value,
        now: f64,
    ) -> Result<String, SeedError> {
        let digest = envelope
            .get("verification_key")
            .filter(|record| record.is_object())
            .and_then(|record| record.get("verification_key_digest"));
        match digest {
            Some(Value::String(text)) if self.digest_accepted(digest, now) => Ok(text.clone()),
            _ => Err(SeedError::new("seed_http_seed_pin_mismatch")),
        }
    }

    /// `_verify_seed_envelope` for the identity statement (no rotation transition held).
    fn verify_envelope(&self, envelope: &Value, now: f64) -> Result<Value, SeedError> {
        let bad = || SeedError::new("seed_http_seed_envelope_invalid");
        let object = envelope.as_object().ok_or_else(bad)?;
        let fields = ["protocol", "statement", "signature", "verification_key"];
        if object.len() != fields.len()
            || !fields.iter().all(|f| object.contains_key(*f))
            || object["protocol"] != SEED_ENVELOPE_PROTOCOL
        {
            return Err(bad());
        }
        let (statement, signature, record) = (
            &object["statement"],
            &object["signature"],
            &object["verification_key"],
        );
        let statement_map = statement.as_object().ok_or_else(bad)?;
        if !signature.is_object() || !record.is_object() {
            return Err(bad());
        }
        if statement_map.len() != STATEMENT_FIELDS.len()
            || !STATEMENT_FIELDS
                .iter()
                .all(|f| statement_map.contains_key(*f))
            || statement_map["protocol"] != SEED_IDENTITY_PROTOCOL
        {
            return Err(bad());
        }
        if !self.digest_accepted(record.get("verification_key_digest"), now)
            || statement["swarm_id"] != self.swarm_id
            || statement["seed_url"] != self.seed_url
            || signature.get("signer_endpoint_id") != statement.get("seed_endpoint_id")
        {
            return Err(SeedError::new("seed_http_seed_pin_mismatch"));
        }
        // `build_ed25519_verifier` failing is a bad key; a verifier that refuses is a bad signature.
        protocol::identity::verify(statement, signature, record).map_err(|e| {
            use protocol::identity::IdentityError as E;
            match e {
                E::KeyRecord | E::KeyDigest => SeedError::new("seed_http_seed_key_invalid"),
                _ => SeedError::new("seed_http_seed_signature_invalid"),
            }
        })?;
        let number = |v: &Value| v.as_f64().filter(|n| n.is_finite() && !v.is_boolean());
        let (issued, expires) = (
            number(&statement["issued_at"]),
            number(&statement["expires_at"]),
        );
        let (Some(issued), Some(expires)) = (issued, expires) else {
            return Err(self.time_invalid(now, &statement["issued_at"], &statement["expires_at"]));
        };
        if !now.is_finite() || now + self.skew < issued || now > expires {
            return Err(self.time_invalid(now, &statement["issued_at"], &statement["expires_at"]));
        }
        Ok(statement.clone())
    }

    /// `_seed_time_invalid`: the rejection with the numbers that say WHICH bound failed.
    fn time_invalid(&self, now: f64, issued: &Value, expires: &Value) -> SeedError {
        let finite = |v: &Value| match v {
            Value::Number(n) => n.as_f64().filter(|f| f.is_finite()),
            _ => None,
        };
        let now_value = Some(now).filter(|n| n.is_finite());
        let (issued, expires) = (finite(issued), finite(expires));
        let window = SeedWindow {
            now: now_value,
            issued_at: issued,
            expires_at: expires,
        };
        SeedError {
            window: Some(window),
            ..SeedError::new("seed_http_seed_time_invalid")
        }
    }
}

// ---- HTTP/1.x response parsing as CPython's `http.client` + `email.feedparser` do it -----------------
// (the oracle's `urllib.request` opener). Three kinds of outcome: a parsed response, the transport
// family of `OSError` (`seed_http_unreachable`) and the `http.client` exceptions that are NOT
// `OSError` and so escape `SeedHTTPClient._request` untranslated.

/// `http.client._MAXLINE` / `_MAXHEADERS` (CPython stdlib constants, not registry budgets).
const MAX_LINE: usize = 65_536;
const MAX_HEADERS: usize = 100;

/// How an exchange failed below the seed protocol.
enum Wire {
    /// `OSError`/`URLError`: the peer was not usable.
    Unreachable,
    /// An `http.client` exception (`BadStatusLine`, `IncompleteRead`, ...) that escapes as is.
    Raw(&'static str),
}

struct Reader<'a> {
    data: &'a [u8],
    at: usize,
}
impl<'a> Reader<'a> {
    /// `fp.readline(_MAXLINE + 1)`: through the newline, at most `MAX_LINE + 1` bytes, else the rest.
    fn line(&mut self) -> &'a [u8] {
        let rest = &self.data[self.at..];
        let end = rest
            .iter()
            .take(MAX_LINE + 1)
            .position(|b| *b == b'\n')
            .map_or(rest.len().min(MAX_LINE + 1), |p| p + 1);
        self.at += end;
        &rest[..end]
    }
    /// `fp.read(n)`: up to `n` bytes.
    fn take(&mut self, n: usize) -> &'a [u8] {
        let rest = &self.data[self.at..];
        let end = rest.len().min(n);
        self.at += end;
        &rest[..end]
    }
}

struct Parsed {
    status: u16,
    content_types: Vec<String>,
    body: Vec<u8>,
}

fn latin1(bytes: &[u8]) -> String {
    bytes.iter().map(|b| char::from(*b)).collect()
}

/// `HTTPResponse.begin()` + the email header parser + `read(amt)`.
fn read_response(reply: &[u8], amt: usize) -> Result<Parsed, Wire> {
    let mut reader = Reader { data: reply, at: 0 };
    let (version, status) = loop {
        let line = reader.line();
        if line.len() > MAX_LINE {
            return Err(Wire::Raw("LineTooLong"));
        }
        if line.is_empty() {
            return Err(Wire::Unreachable); // RemoteDisconnected
        }
        let text = latin1(line);
        let mut parts = text.split_ascii_whitespace();
        let version = parts.next().unwrap_or_default();
        let status = parts.next();
        if !version.starts_with("HTTP/") {
            return Err(Wire::Raw("BadStatusLine"));
        }
        let status = status
            .and_then(|s| s.parse::<i64>().ok())
            .filter(|s| (100..=999).contains(s))
            .ok_or(Wire::Raw("BadStatusLine"))?;
        if status != 100 {
            break (version.to_owned(), status as u16);
        }
        // `100 Continue`: skip its headers, then read the real status line.
        loop {
            let skipped = reader.line();
            if skipped.len() > MAX_LINE {
                return Err(Wire::Raw("LineTooLong"));
            }
            if skipped.trim_ascii().is_empty() {
                break;
            }
        }
    };
    if version != "HTTP/1.0" && version != "HTTP/0.9" && !version.starts_with("HTTP/1.") {
        return Err(Wire::Raw("UnknownProtocol"));
    }
    // `parse_headers`: lines to the blank line (or EOF), at most `_MAXHEADERS`.
    let mut lines: Vec<&[u8]> = Vec::new();
    loop {
        let line = reader.line();
        if line.len() > MAX_LINE {
            return Err(Wire::Raw("LineTooLong"));
        }
        lines.push(line);
        if lines.len() > MAX_HEADERS {
            return Err(Wire::Raw("HTTPException"));
        }
        if line.is_empty() || line == b"\r\n" || line == b"\n" {
            break;
        }
    }
    // `email.feedparser`: `Name: value` (value left-stripped of blanks, right-stripped of CR/LF), a
    // leading blank continues the previous header, any other line ends the header block.
    let mut headers: Vec<(String, String)> = Vec::new();
    for line in lines {
        if line.is_empty() || line == b"\r\n" || line == b"\n" {
            break;
        }
        let text = latin1(line);
        if text.starts_with([' ', '\t']) {
            if let Some(last) = headers.last_mut() {
                last.1.push('\n');
                last.1.push_str(text.trim_end_matches(['\r', '\n']));
            }
            continue;
        }
        let Some((name, value)) = text.split_once(':') else {
            break;
        };
        if !name
            .bytes()
            .all(|b| (0x21..=0x7e).contains(&b) && b != b':')
        {
            break;
        }
        headers.push((
            name.to_ascii_lowercase(),
            value
                .trim_start_matches([' ', '\t'])
                .trim_end_matches(['\r', '\n'])
                .to_owned(),
        ));
    }
    let first = |name: &str| headers.iter().find(|(n, _)| n == name).map(|(_, v)| v);
    let chunked = first("transfer-encoding").is_some_and(|v| v.to_lowercase() == "chunked");
    let mut length = first("content-length")
        .filter(|v| !v.is_empty() && !chunked)
        .and_then(|v| v.trim().parse::<i64>().ok())
        .filter(|n| *n >= 0)
        .map(|n| n as usize);
    if status == 204 || status == 304 || (100..200).contains(&status) {
        length = Some(0);
    }
    let body = if chunked {
        read_chunked(&mut reader, amt)?
    } else if let Some(length) = length {
        reader.take(length.min(amt)).to_vec()
    } else {
        reader.take(amt).to_vec()
    };
    Ok(Parsed {
        status,
        content_types: headers
            .iter()
            .filter(|(n, _)| n == "content-type")
            .map(|(_, v)| v.clone())
            .collect(),
        body,
    })
}

/// `HTTPResponse._read_chunked(amt)`.
fn read_chunked(reader: &mut Reader, mut amt: usize) -> Result<Vec<u8>, Wire> {
    let incomplete = || Wire::Raw("IncompleteRead");
    let mut out = Vec::new();
    let mut first = true;
    loop {
        if !first && reader.take(2).len() < 2 {
            return Err(incomplete()); // the CRLF that closes the previous chunk
        }
        first = false;
        let line = reader.line();
        if line.len() > MAX_LINE {
            return Err(Wire::Raw("LineTooLong"));
        }
        let size_text = line.split(|b| *b == b';').next().unwrap_or_default();
        let size = std::str::from_utf8(size_text.trim_ascii())
            .ok()
            .and_then(|t| usize::from_str_radix(t, 16).ok())
            .ok_or_else(incomplete)?;
        if size == 0 {
            // `_read_and_discard_trailer`.
            loop {
                let trailer = reader.line();
                if trailer.len() > MAX_LINE {
                    return Err(Wire::Raw("LineTooLong"));
                }
                if trailer.is_empty() || trailer == b"\r\n" || trailer == b"\n" {
                    return Ok(out);
                }
            }
        }
        let wanted = amt.min(size);
        let data = reader.take(wanted);
        out.extend_from_slice(data);
        if data.len() < wanted {
            return Err(incomplete());
        }
        if amt <= size {
            return Ok(out);
        }
        amt -= size;
    }
}

/// `_canonical_json_loads`: strict JSON that re-encodes to exactly the bytes received.
fn canonical_loads(raw: &[u8]) -> Result<Value, SeedError> {
    let invalid = || SeedError::new("seed_http_json_invalid");
    let text = std::str::from_utf8(raw).map_err(|_| invalid())?;
    let value: Value = serde_json::from_str(text).map_err(|_| invalid())?;
    // `_pairs`: an object with a repeated key is refused, at any depth.
    if serde_json::from_str::<DuplicateFree>(text).is_err() {
        return Err(invalid());
    }
    let canonical = protocol::canonical::to_bytes(&value).map_err(|_| invalid())?;
    if canonical != raw {
        return Err(SeedError::new("seed_http_noncanonical"));
    }
    Ok(value)
}

/// `_authoritative_remote_error_code`.
fn remote_error_code(raw: &[u8], limit: u64) -> Option<String> {
    if raw.is_empty() || raw.len() as u64 > limit {
        return None;
    }
    let value = canonical_loads(raw).ok()?;
    let object = value.as_object()?;
    if object.len() != 2 || object.get("protocol") != Some(&json!(SEED_HTTP_ERROR_PROTOCOL)) {
        return None;
    }
    let error = object.get("error")?.as_object()?;
    if error.len() != 1 {
        return None;
    }
    let code = error.get("code")?.as_str()?;
    let mut chars = code.chars();
    let valid = chars.next().is_some_and(|c| c.is_ascii_lowercase())
        && code.len() <= 128
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_');
    valid.then(|| code.to_owned())
}

/// Accepts exactly the documents in which no object repeats a key (`_pairs` in the source).
struct DuplicateFree;
impl<'de> serde::Deserialize<'de> for DuplicateFree {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Visit;
        impl<'de> serde::de::Visitor<'de> for Visit {
            type Value = DuplicateFree;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("a JSON value without repeated object keys")
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(
                self,
                mut map: A,
            ) -> Result<DuplicateFree, A::Error> {
                let mut seen = std::collections::HashSet::new();
                while let Some(key) = map.next_key::<String>()? {
                    if !seen.insert(key) {
                        return Err(serde::de::Error::custom("duplicate key"));
                    }
                    map.next_value::<DuplicateFree>()?;
                }
                Ok(DuplicateFree)
            }
            fn visit_seq<A: serde::de::SeqAccess<'de>>(
                self,
                mut seq: A,
            ) -> Result<DuplicateFree, A::Error> {
                while seq.next_element::<DuplicateFree>()?.is_some() {}
                Ok(DuplicateFree)
            }
            fn visit_bool<E>(self, _: bool) -> Result<DuplicateFree, E> {
                Ok(DuplicateFree)
            }
            fn visit_i64<E>(self, _: i64) -> Result<DuplicateFree, E> {
                Ok(DuplicateFree)
            }
            fn visit_u64<E>(self, _: u64) -> Result<DuplicateFree, E> {
                Ok(DuplicateFree)
            }
            fn visit_f64<E>(self, _: f64) -> Result<DuplicateFree, E> {
                Ok(DuplicateFree)
            }
            fn visit_str<E>(self, _: &str) -> Result<DuplicateFree, E> {
                Ok(DuplicateFree)
            }
            fn visit_unit<E>(self) -> Result<DuplicateFree, E> {
                Ok(DuplicateFree)
            }
        }
        deserializer.deserialize_any(Visit)
    }
}
