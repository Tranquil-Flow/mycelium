// SPDX-License-Identifier: AGPL-3.0-only
//! Public site observation. No implicit Cloudflare, owner or session credentials.
use crate::{Fault, FaultKind, Result};
use budgets::Span;
use serde_json::Value;
use std::collections::BTreeMap;
pub struct Request {
    pub url: String,
    pub headers: BTreeMap<String, String>,
    pub follow_redirects: bool,
}
pub trait Transport {
    fn get(&mut self, request: Request, bound: Span) -> Result<(u16, Value)>;
}
pub fn verify(
    io: &mut impl Transport,
    url: &str,
    headers: BTreeMap<String, String>,
    bound: Span,
) -> Result<Value> {
    if bound == Span::ZERO {
        return Err(Fault::new("site_budget_invalid", FaultKind::Input));
    }
    let authority = url
        .strip_prefix("https://")
        .and_then(|s| s.split('/').next());
    if url
        .bytes()
        .any(|b| b.is_ascii_control() || b.is_ascii_whitespace())
        || authority.is_none_or(|s| {
            s.is_empty()
                || !s
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b".-:".contains(&b))
        })
    {
        return Err(Fault::new("site_origin_invalid", FaultKind::Input));
    }
    for (name, value) in &headers {
        // Explicit allowlist: neither reserved CF-* nor credentials can reach
        // the adapter, including through casing, whitespace or CRLF injection.
        if !matches!(name.to_ascii_lowercase().as_str(), "accept" | "user-agent")
            || value.bytes().any(|b| b.is_ascii_control())
        {
            return Err(Fault::new("site_header_forbidden", FaultKind::Input));
        }
    }
    let (http, value) = io.get(
        Request {
            url: url.into(),
            headers,
            follow_redirects: false,
        },
        bound,
    )?;
    if http != 200 {
        return Err(Fault::new("site_http_refused", FaultKind::Observation));
    }
    Ok(value)
}
