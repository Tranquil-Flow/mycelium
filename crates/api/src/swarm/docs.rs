// SPDX-License-Identifier: AGPL-3.0-only
//! Closed set of signed swarm documents, mycelium_swarm_directory/docs.py @ f4e366e9.
//! Validators check exact key set, bounds and signature in the source's order and
//! return the document unchanged. Freshness/state belong to the directory core.
use super::{
    json::J,
    wire::{
        Verifier, WireError, canonical, exact_keys, integer, key_bytes, text, unb64url,
        verify_document,
    },
};
use budgets::BudgetId;

pub const RECORD: &str = "mycelium.swarm.record.v1";
pub const DELETE: &str = "mycelium.swarm.delete.v1";
pub const HEARTBEAT: &str = "mycelium.swarm.heartbeat.v1";
pub const ACCESS_REQUEST: &str = "mycelium.swarm.access_request.v1";
pub const DENY: &str = "mycelium.swarm.deny.v1";
pub const GRANT: &str = "mycelium.swarm.grant.v1";
pub const OWNER_QUERY: &str = "mycelium.swarm.owner_query.v1";
pub const GRANTEE_QUERY: &str = "mycelium.swarm.grantee_query.v1";
pub const ENTRY_OFFER: &str = "mycelium.swarm.entry_offer.v1";
pub const ENTRY_SEND: &str = "mycelium.swarm.entry_send.v1";
pub const OPEN_GRANT_REQUEST: &str = "mycelium.swarm.open_grant_request.v1";
pub const KILL_SWITCH: &str = "mycelium.swarm.kill_switch.v1";
pub const ENTRY_CANCEL: &str = "mycelium.swarm.entry_cancel.v1";
pub const LINK: &str = "mycelium.swarm.link.v1";
pub const LINK_SCHEME: &str = "mycelium-swarm:";
pub const MAX_PROMPT_CHARS: i64 = 2000;
pub const MAX_NEW_TOKENS: i64 = 256;
const MAX_GRANT_TOKENS: i64 = 1_000_000;
const MAX_MEMBERS: i64 = 64;
const TIME_MAX: i64 = 1 << 40;
const STATUSES: [&str; 3] = ["serving", "warming", "offline"];

fn keys(protocol: &str) -> &'static [&'static str] {
    match protocol {
        RECORD => &[
            "protocol",
            "swarm_id",
            "owner_key",
            "seq",
            "name",
            "model",
            "description",
            "access_mode",
            "open_quota",
            "entry",
            "sig",
        ],
        DELETE => &["protocol", "swarm_id", "owner_key", "seq", "sig"],
        HEARTBEAT => &[
            "protocol",
            "swarm_id",
            "signer_key",
            "status",
            "members",
            "served_model",
            "at",
            "sig",
        ],
        ACCESS_REQUEST => &["protocol", "swarm_id", "requester_key", "note", "at", "sig"],
        DENY => &[
            "protocol",
            "swarm_id",
            "owner_key",
            "requester_key",
            "at",
            "sig",
        ],
        GRANT => &[
            "protocol",
            "grant_id",
            "swarm_id",
            "grantee_key",
            "issuer_key",
            "issued_at",
            "expires_at",
            "max_requests",
            "max_tokens",
            "sig",
        ],
        OWNER_QUERY => &["protocol", "swarm_id", "owner_key", "at", "nonce", "sig"],
        GRANTEE_QUERY => &["protocol", "grantee_key", "at", "nonce", "sig"],
        ENTRY_OFFER => &["protocol", "swarm_id", "entry_key", "origin", "at", "sig"],
        ENTRY_SEND => &[
            "protocol",
            "swarm_id",
            "key",
            "grant",
            "prompt",
            "max_new_tokens",
            "at",
            "nonce",
            "sig",
        ],
        OPEN_GRANT_REQUEST => &["protocol", "swarm_id", "key", "at", "nonce", "sig"],
        KILL_SWITCH => &[
            "protocol",
            "swarm_id",
            "owner_key",
            "engaged",
            "seq",
            "at",
            "sig",
        ],
        ENTRY_CANCEL => &[
            "protocol",
            "swarm_id",
            "key",
            "request_id",
            "at",
            "nonce",
            "sig",
        ],
        _ => &[],
    }
}
fn all(v: &str, ok: impl Fn(u8) -> bool) -> bool {
    v.bytes().all(ok)
}
fn tail(v: &J, prefix: &str, n: std::ops::RangeInclusive<usize>, ok: fn(u8) -> bool) -> bool {
    v.as_str().is_some_and(|s| {
        s.strip_prefix(prefix)
            .is_some_and(|t| n.contains(&t.len()) && all(t, ok))
    })
}
fn lower_digit(c: u8) -> bool {
    c.is_ascii_lowercase() || c.is_ascii_digit()
}
fn url_char(c: u8) -> bool {
    c.is_ascii_alphanumeric() || c == b'_' || c == b'-'
}
/// `sw-[a-z0-9]{20}`
pub fn is_swarm_id(v: &J) -> bool {
    tail(v, "sw-", 20..=20, lower_digit)
}
fn ident(ok: bool, code: &'static str) -> Result<(), WireError> {
    if ok {
        Ok(())
    } else {
        Err(WireError::bad(code))
    }
}
fn model_ok(v: &J) -> bool {
    v.as_str().is_some_and(|s| {
        (1..=96).contains(&s.len())
            && all(s, |c| c.is_ascii_alphanumeric() || b"._/:+-".contains(&c))
    })
}
fn rfc1918(octets: [u8; 4]) -> bool {
    octets[0] == 10
        || (octets[0] == 172 && (16..=31).contains(&octets[1]))
        || (octets[0] == 192 && octets[1] == 168)
}
fn ipv4(text: &str) -> Option<[u8; 4]> {
    let parts: Vec<&str> = text.split('.').collect();
    if parts.len() != 4 {
        return None;
    }
    let mut out = [0u8; 4];
    for (slot, p) in out.iter_mut().zip(parts) {
        if p.is_empty()
            || p.len() > 3
            || !all(p, |c| c.is_ascii_digit())
            || (p.len() > 1 && p.starts_with('0'))
        {
            return None;
        }
        *slot = p.parse().ok()?;
    }
    Some(out)
}
fn port_ok(p: &str) -> bool {
    (2..=5).contains(&p.len()) && !p.starts_with('0') && all(p, |c| c.is_ascii_digit())
}
fn public_origin(v: &str) -> bool {
    let Some(host) = v.strip_prefix("https://") else {
        return false;
    };
    if host.is_empty() || host.len() > 253 || host.contains('\n') {
        return false;
    }
    let labels: Vec<&str> = host.split('.').collect();
    let (top, front) = match labels.split_last() {
        Some((t, f)) if !f.is_empty() => (*t, f),
        _ => return false,
    };
    (2..=63).contains(&top.len())
        && all(top, |c| c.is_ascii_lowercase())
        && front.iter().all(|l| {
            let b = l.as_bytes();
            (1..=63).contains(&b.len())
                && lower_digit(b[0])
                && lower_digit(b[b.len() - 1])
                && all(l, |c| lower_digit(c) || c == b'-')
        })
}
fn loopback_origin(v: &str) -> bool {
    v.strip_prefix("http://127.0.0.1:").is_some_and(port_ok)
}
fn private_origin(v: &str) -> bool {
    let Some(rest) = v
        .strip_prefix("https://")
        .or_else(|| v.strip_prefix("http://"))
    else {
        return false;
    };
    let (host, port) = match rest.split_once(':') {
        Some((h, p)) => (h, Some(p)),
        None => (rest, None),
    };
    if !(7..=15).contains(&host.len()) || !all(host, |c| c.is_ascii_digit() || c == b'.') {
        return false;
    }
    if let Some(p) = port
        && (!port_ok(p) || p.parse::<u32>().is_ok_and(|p| p > 65_535))
    {
        return false;
    }
    ipv4(host).is_some_and(rfc1918)
}
/// docs.py:202-207 `origin`.
pub fn origin(
    v: &J,
    allow_loopback: bool,
    allow_private: bool,
    code: &'static str,
) -> Result<(), WireError> {
    match v.as_str() {
        Some(s)
            if public_origin(s)
                || (allow_loopback && loopback_origin(s))
                || (allow_private && private_origin(s)) =>
        {
            Ok(())
        }
        _ => Err(WireError::bad(code)),
    }
}
fn key(v: &J, code: &'static str) -> Result<(), WireError> {
    key_bytes(v).map(|_| ()).map_err(|_| WireError::bad(code))
}
fn time(v: &J) -> Result<i64, WireError> {
    integer(v, 0, TIME_MAX, "time_invalid")
}
fn max_record_bytes() -> Result<usize, WireError> {
    BudgetId::PyMyceliumSwarmDirectoryDocsMaxRecordBytesL41
        .bytes()
        .ok()
        .and_then(|b| usize::try_from(b.get()).ok())
        .ok_or(WireError::new("budget_unavailable", 503))
}
pub fn max_grant_seconds() -> Result<i64, WireError> {
    BudgetId::PyMyceliumSwarmDirectoryDocsMaxGrantSecondsL42
        .span(budgets::Entries::ZERO)
        .ok()
        .and_then(|s| i64::try_from(s.millis() / 1000).ok())
        .ok_or(WireError::new("budget_unavailable", 503))
}
fn max_grant_requests() -> Result<i64, WireError> {
    BudgetId::PyMyceliumSwarmDirectoryDocsMaxGrantRequestsL43
        .count()
        .ok()
        .and_then(|c| i64::try_from(c.get()).ok())
        .ok_or(WireError::new("budget_unavailable", 503))
}
pub fn check_record(
    d: &J,
    verifier: &Verifier,
    allow_loopback: bool,
    allow_private: bool,
) -> Result<(), WireError> {
    exact_keys(d, RECORD, keys(RECORD))?;
    if canonical(d)?.len() > max_record_bytes()? {
        return Err(WireError::new("record_too_large", 413));
    }
    let f = |k: &str| d.get(k).unwrap_or(&J::Null);
    ident(is_swarm_id(f("swarm_id")), "swarm_id_invalid")?;
    key(f("owner_key"), "owner_key_invalid")?;
    integer(f("seq"), 1, 1 << 40, "seq_invalid")?;
    text(f("name"), 1, 48, "name_invalid")?;
    ident(model_ok(f("model")), "model_invalid")?;
    listing_tail(d)?;
    let entry = f("entry");
    if !entry.is_null() {
        let J::Obj(p) = entry else {
            return Err(WireError::bad("entry_invalid"));
        };
        if p.len() != 2 || entry.get("key").is_none() || entry.get("origin").is_none() {
            return Err(WireError::bad("entry_invalid"));
        }
        key(entry.get("key").unwrap_or(&J::Null), "entry_invalid")?;
        origin(
            entry.get("origin").unwrap_or(&J::Null),
            allow_loopback,
            allow_private,
            "entry_origin_invalid",
        )?;
    }
    verify_document(d, f("owner_key"), verifier)
}
/// The owner-chosen public fields of a record (`name`, `description`, `access_mode`,
/// `open_quota`) with `check_record`'s rules and codes, before any record exists.
pub fn check_listing(d: &J) -> Result<(), WireError> {
    text(d.get("name").unwrap_or(&J::Null), 1, 48, "name_invalid")?;
    listing_tail(d)
}
fn listing_tail(d: &J) -> Result<(), WireError> {
    let f = |k: &str| d.get(k).unwrap_or(&J::Null);
    text(f("description"), 0, 280, "description_invalid")?;
    if !matches!(
        f("access_mode").as_str(),
        Some("open" | "request" | "invite")
    ) {
        return Err(WireError::bad("access_mode_invalid"));
    }
    let quota = f("open_quota");
    if f("access_mode").as_str() == Some("open") || !quota.is_null() {
        let J::Obj(p) = quota else {
            return Err(WireError::bad("open_quota_invalid"));
        };
        let q = |k: &str| quota.get(k).unwrap_or(&J::Null);
        if p.len() != 3
            || ["max_requests", "max_tokens", "ttl_seconds"]
                .iter()
                .any(|k| quota.get(k).is_none())
        {
            return Err(WireError::bad("open_quota_invalid"));
        }
        integer(q("max_requests"), 1, 1000, "open_quota_invalid")?;
        integer(q("max_tokens"), 1, 200_000, "open_quota_invalid")?;
        integer(
            q("ttl_seconds"),
            60,
            max_grant_seconds()?,
            "open_quota_invalid",
        )?;
    }
    Ok(())
}
/// docs.py:252-316 `check` for every non-record document type.
pub fn check(
    d: &J,
    protocol: &str,
    verifier: &Verifier,
    allow_loopback: bool,
    allow_private: bool,
) -> Result<(), WireError> {
    exact_keys(d, protocol, keys(protocol))?;
    let f = |k: &str| d.get(k).unwrap_or(&J::Null);
    if let Some(id) = d.get("swarm_id") {
        ident(is_swarm_id(id), "swarm_id_invalid")?;
    }
    if let Some(at) = d.get("at") {
        time(at)?;
    }
    if let Some(n) = d.get("nonce") {
        ident(tail(n, "", 16..=43, url_char), "nonce_invalid")?;
    }
    let signer = match protocol {
        DELETE => {
            integer(f("seq"), 1, 1 << 40, "seq_invalid")?;
            key(f("owner_key"), "owner_key_invalid")?;
            "owner_key"
        }
        HEARTBEAT => {
            if !f("status").as_str().is_some_and(|s| STATUSES.contains(&s)) {
                return Err(WireError::bad("status_invalid"));
            }
            integer(f("members"), 0, MAX_MEMBERS, "members_invalid")?;
            if !f("served_model").is_null() {
                ident(model_ok(f("served_model")), "model_invalid")?;
            }
            key(f("signer_key"), "signer_key_invalid")?;
            "signer_key"
        }
        ACCESS_REQUEST => {
            text(f("note"), 0, 140, "note_invalid")?;
            key(f("requester_key"), "requester_key_invalid")?;
            "requester_key"
        }
        DENY => {
            key(f("requester_key"), "requester_key_invalid")?;
            key(f("owner_key"), "owner_key_invalid")?;
            "owner_key"
        }
        GRANT => {
            ident(
                tail(f("grant_id"), "gr-", 22..=22, url_char),
                "grant_invalid",
            )?;
            key(f("grantee_key"), "grant_invalid")?;
            key(f("issuer_key"), "grant_invalid")?;
            let issued = time(f("issued_at"))?;
            let expires = time(f("expires_at"))?;
            if !(issued < expires && expires <= issued + max_grant_seconds()?) {
                return Err(WireError::bad("grant_lifetime_invalid"));
            }
            integer(
                f("max_requests"),
                1,
                max_grant_requests()?,
                "grant_quota_invalid",
            )?;
            integer(f("max_tokens"), 1, MAX_GRANT_TOKENS, "grant_quota_invalid")?;
            "issuer_key"
        }
        OWNER_QUERY => {
            key(f("owner_key"), "owner_key_invalid")?;
            "owner_key"
        }
        GRANTEE_QUERY => {
            key(f("grantee_key"), "grantee_key_invalid")?;
            "grantee_key"
        }
        ENTRY_OFFER => {
            origin(
                f("origin"),
                allow_loopback,
                allow_private,
                "entry_origin_invalid",
            )?;
            key(f("entry_key"), "entry_key_invalid")?;
            "entry_key"
        }
        ENTRY_SEND => {
            match f("prompt") {
                J::Str(s) if !s.trim().is_empty() => {
                    if s.chars().count() > MAX_PROMPT_CHARS as usize {
                        return Err(WireError::new("entry_prompt_too_long", 413));
                    }
                }
                _ => return Err(WireError::bad("entry_prompt_required")),
            }
            integer(
                f("max_new_tokens"),
                1,
                1 << 31,
                "entry_max_new_tokens_invalid",
            )?;
            if !matches!(f("grant"), J::Obj(_)) {
                return Err(WireError::new("grant_invalid", 403));
            }
            key(f("key"), "key_invalid")?;
            "key"
        }
        OPEN_GRANT_REQUEST => {
            key(f("key"), "key_invalid")?;
            "key"
        }
        ENTRY_CANCEL => {
            let id = f("request_id");
            ident(tail(id, "", 16..=64, url_char), "entry_request_invalid")?;
            key(f("key"), "key_invalid")?;
            "key"
        }
        KILL_SWITCH => {
            if !matches!(f("engaged"), J::Bool(_)) {
                return Err(WireError::bad("kill_engaged_invalid"));
            }
            integer(f("seq"), 1, 1 << 40, "seq_invalid")?;
            key(f("owner_key"), "owner_key_invalid")?;
            "owner_key"
        }
        _ => return Err(WireError::bad("protocol_invalid")),
    };
    verify_document(d, f(signer), verifier)
}
pub fn fresh(at: i64, now: i64, skew: i64) -> Result<(), WireError> {
    if (at - now).abs() > skew {
        Err(WireError::bad("clock_skew"))
    } else {
        Ok(())
    }
}
/// docs.py:164-167 `swarm_link`.
pub fn swarm_link(
    directory_origin: &str,
    swarm_id: &str,
    owner_key: &str,
) -> Result<String, WireError> {
    let doc = J::obj(vec![
        ("protocol", J::str(LINK)),
        ("directory", J::str(directory_origin)),
        ("swarm_id", J::str(swarm_id)),
        ("owner_key", J::str(owner_key)),
    ]);
    Ok(format!(
        "{LINK_SCHEME}{}",
        super::wire::b64url(&canonical(&doc)?)
    ))
}
/// Base64url helper kept public for tests that build link/ signature fixtures.
pub fn decode_b64url(v: &str) -> Result<Vec<u8>, WireError> {
    unb64url(&J::str(v), None, "encoding_invalid")
}
