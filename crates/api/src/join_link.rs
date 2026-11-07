// SPDX-License-Identifier: AGPL-3.0-only
//! Byte-faithful ports of `pairing.parse_join_text` (pairing.py:543-589 @ f4e366e9),
//! `mycelium_internet.bootstrap.canonical_https_origin` (bootstrap.py:70-122),
//! `public_relay.validate_external_origin` / `validate_proxy_port` (public_relay.py:262-282)
//! and the option checks of `TemporaryHttpsRelay.__init__` (public_relay.py:315-330).
//! Every outcome is compared with the executed oracle in `tests/join_link.rs`.
use crate::{
    persistent::JoinLink,
    schema::{PublicError, Raised},
    wire,
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use budgets::{BudgetId, Entries};
use serde_json::{Map, Value};

const SCHEME: &str = "mycelium-join:";
const LINK_PROTOCOL: &str = "mycelium.a13_join_link.v1";
const LINK_FIELDS: [&str; 6] = [
    "protocol",
    "origin",
    "window_id",
    "code",
    "seed_key_digest",
    "expires_at",
];
const CODE_ALPHABET: &str = "23456789ABCDEFGHJKMNPQRSTVWXYZ";
const TUNNEL_SUFFIX: &str = ".trycloudflare.com";
/// `_NON_PUBLIC_SUFFIXES` (public_relay.py:255).
const NON_PUBLIC_SUFFIXES: [&str; 10] = [
    "local",
    "localhost",
    "internal",
    "lan",
    "home",
    "corp",
    "invalid",
    "test",
    "onion",
    "arpa",
];

// Shared with seed; dependency direction remains seed -> protocol, never seed -> API.
pub use protocol::origin::{Invalid, canonical_https_origin, py_isspace, py_strip};

fn pairing(code: &'static str) -> PublicError {
    PublicError::new(409, code).raised_by(Raised::Pairing)
}
fn invalid_code() -> PublicError {
    pairing("join_code_invalid")
}

/// `_EXTERNAL_ORIGIN`: `https://` + dotted lowercase DNS labels + an alphabetic TLD (no port).
fn external_shape(candidate: &str) -> bool {
    let Some(rest) = candidate.strip_prefix("https://") else {
        return false;
    };
    let label = |l: &str| {
        let bytes = l.as_bytes();
        (1..=63).contains(&bytes.len())
            && bytes
                .iter()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'-')
            && bytes[0] != b'-'
            && bytes[bytes.len() - 1] != b'-'
    };
    let Some((labels, tld)) = rest.rsplit_once('.') else {
        return false;
    };
    (1..=253).contains(&rest.chars().count())
        && !labels.is_empty()
        && labels.split('.').all(label)
        && (2..=63).contains(&tld.len())
        && tld.bytes().all(|b| b.is_ascii_lowercase())
}
/// `validate_external_origin`: `https://<dns-name>` only (no credentials, port, path, IP literal).
pub fn validate_external_origin(value: &Value) -> Result<String, Invalid> {
    let text = value.as_str().ok_or(Invalid)?;
    if text != py_strip(text) {
        return Err(Invalid);
    }
    let candidate = text.to_lowercase();
    let canonical = canonical_https_origin(&Value::String(candidate.clone()))?;
    let tld = candidate.rsplit('.').next().unwrap_or_default();
    if canonical != candidate || !external_shape(&candidate) || NON_PUBLIC_SUFFIXES.contains(&tld) {
        return Err(Invalid);
    }
    Ok(candidate)
}
/// `validate_proxy_port`: an `int` (never a bool or float) in 1024..=65535.
pub fn validate_proxy_port(value: &Value) -> Result<u16, Invalid> {
    value
        .as_u64()
        .filter(|port| (1024..=65_535).contains(port))
        .and_then(|port| u16::try_from(port).ok())
        .ok_or(Invalid)
}

fn seconds(id: BudgetId) -> f64 {
    id.span(Entries::new(0))
        .map_or(f64::NAN, |span| span.millis() as f64 / 1000.0)
}
/// What `TemporaryHttpsRelay.__init__` keeps of an external relay request.
#[derive(Clone, Debug, PartialEq)]
pub struct RelayOptions {
    pub cloudflare_front: bool,
    pub auto_renew: bool,
    pub long_lived: bool,
    pub renewal_cap_seconds: f64,
    pub public_origin: String,
    pub proxy_port: u16,
}
impl RelayOptions {
    /// `app._new_relay` (app.py:757-766) for an `external` provider: the bounded `ValueError`
    /// code becomes the 400 `DemoError` text. The checks run in `__init__`'s order.
    pub fn external(spec: &Map<String, Value>) -> Result<Self, &'static str> {
        let auto_renew = spec.get("auto_renew").unwrap_or(&Value::Bool(true));
        let long_lived = spec.get("long_lived").unwrap_or(&Value::Bool(false));
        let default_cap = seconds(BudgetId::RelayGrantedWindowCap);
        let maximum = seconds(BudgetId::PyMyceliumA13MacosPublicRelayMaxRenewalCapSecondsL286);
        let cap_seconds = match spec.get("max_renewed_seconds") {
            None => Some(default_cap),
            Some(Value::Number(n)) => n.to_string().parse::<f64>().ok(),
            Some(_) => None,
        };
        let (Value::Bool(auto_renew), Value::Bool(long_lived)) = (auto_renew, long_lived) else {
            return Err("relay_renewal_invalid");
        };
        let Some(cap_seconds) = cap_seconds.filter(|c| *c > 0.0 && *c <= maximum) else {
            return Err("relay_renewal_invalid");
        };
        let origin = spec.get("public_origin").unwrap_or(&Value::Null);
        let public_origin = validate_external_origin(origin).map_err(|_| "relay_origin_invalid")?;
        let port = spec.get("proxy_port").unwrap_or(&Value::Null);
        let proxy_port = validate_proxy_port(port).map_err(|_| "relay_proxy_port_invalid")?;
        Ok(Self {
            cloudflare_front: spec.get("cloudflare_front") == Some(&Value::Bool(true)),
            auto_renew: *auto_renew,
            long_lived: *long_lived,
            renewal_cap_seconds: cap_seconds,
            public_origin,
            proxy_port,
        })
    }
}

/// `pairing._finite`: an int or float (never a bool), finite once converted to a float. A huge
/// int is an uncaught `OverflowError` (409, no closed code); anything else a `PairingError`.
pub(crate) fn finite(value: &Value) -> Result<f64, PublicError> {
    let Value::Number(number) = value else {
        return Err(pairing("pairing_time_invalid"));
    };
    let text = number.to_string();
    let as_float: f64 = text.parse().map_err(|_| pairing("pairing_time_invalid"))?;
    if as_float.is_finite() {
        return Ok(as_float);
    }
    if text.contains(['.', 'e', 'E']) {
        Err(pairing("pairing_time_invalid"))
    } else {
        Err(PublicError::new(409, "action_not_completed")
            .raised_by(Raised::Builtin("OverflowError")))
    }
}
fn code_chars(text: &str, len: usize) -> bool {
    text.chars().count() == len && text.chars().all(|c| CODE_ALPHABET.contains(c))
}
/// `_normalized_code`: whitespace and hyphens dropped, upper-cased, 4 + 8 code characters.
fn normalized_code(text: &str) -> Result<(String, String), PublicError> {
    let compact: String = text
        .chars()
        .filter(|c| !py_isspace(*c) && *c != '-')
        .collect::<String>()
        .to_uppercase();
    let chars: Vec<char> = compact.chars().collect();
    if chars.len() != 12 {
        return Err(invalid_code());
    }
    let window_id: String = chars[..4].iter().collect();
    let code: String = chars[4..].iter().collect();
    if code_chars(&window_id, 4) && code_chars(&code, 8) {
        Ok((window_id, code))
    } else {
        Err(invalid_code())
    }
}
fn digest_shape(text: &str) -> bool {
    text.strip_prefix("sha256:").is_some_and(|hex| {
        hex.len() == 64
            && hex
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}
/// Python's `json.loads(bytes)`; `None` when it raises a `ValueError`.
fn loads(raw: &[u8]) -> Option<Value> {
    let text = wire::decode_json_bytes(raw).ok()?;
    let text = wire::neutralise_surrogate_escapes(&text);
    wire::parse_python_json(&text)
}

/// `parse_join_text(text, now=...)`: the parsed link and (full link only) its `expires_at`.
pub fn parse_join_text(text: &Value, now: &Value) -> Result<(JoinLink, Option<f64>), PublicError> {
    let text = text
        .as_str()
        .filter(|t| !py_strip(t).is_empty() && t.chars().count() <= 4096)
        .ok_or_else(invalid_code)?;
    let value = py_strip(text);
    let instant = finite(now)?;
    if let Some(encoded) = value.strip_prefix(SCHEME) {
        let document = URL_SAFE_NO_PAD
            .decode(encoded)
            .ok()
            .filter(|raw| !raw.is_empty())
            .and_then(|raw| loads(&raw))
            .ok_or_else(invalid_code)?;
        let fields = document.as_object().ok_or_else(invalid_code)?;
        let text_of = |key: &str| fields.get(key).and_then(Value::as_str);
        let well_formed = fields.len() == LINK_FIELDS.len()
            && LINK_FIELDS.iter().all(|k| fields.contains_key(*k))
            && text_of("protocol") == Some(LINK_PROTOCOL)
            && text_of("window_id").is_some_and(|w| code_chars(w, 4))
            && text_of("code").is_some_and(|c| code_chars(c, 8))
            && text_of("seed_key_digest").is_some_and(digest_shape);
        if !well_formed {
            return Err(invalid_code());
        }
        let origin = canonical_https_origin(&fields["origin"]).map_err(|_| invalid_code())?;
        let expires_at = finite(&fields["expires_at"])?;
        if instant > expires_at {
            return Err(pairing("join_link_expired"));
        }
        let link = JoinLink {
            origin,
            window_id: text_of("window_id").unwrap_or_default().to_owned(),
            code: text_of("code").unwrap_or_default().to_owned(),
            seed_key_digest: text_of("seed_key_digest").map(str::to_owned),
        };
        return Ok((link, Some(expires_at)));
    }
    let (host, code_text) = value.split_once(' ').unwrap_or((value, ""));
    let mut host = py_strip(host).to_lowercase();
    if let Some(rest) = host.strip_prefix("https://") {
        host = rest.to_owned();
    }
    let host = host.strip_suffix('/').unwrap_or(&host);
    let host = host.strip_suffix(TUNNEL_SUFFIX).unwrap_or(host);
    let tunnel = !host.is_empty()
        && host.split('-').all(|part| {
            !part.is_empty()
                && part
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
        });
    if !tunnel || py_strip(code_text).is_empty() {
        return Err(invalid_code());
    }
    let (window_id, code) = normalized_code(code_text)?;
    let link = JoinLink {
        origin: format!("https://{host}{TUNNEL_SUFFIX}"),
        window_id,
        code,
        seed_key_digest: None,
    };
    Ok((link, None))
}
