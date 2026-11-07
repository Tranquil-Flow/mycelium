// SPDX-License-Identifier: AGPL-3.0-only
//! Native-launch authority is separate from browser sessions. The host owns
//! private launch-file IO, closed-state and cryptographic entropy; HTTP never
//! returns a launch token or native credential. Oracle app.py:870-880,1951-1996
//! at f4e366e98a1f705dfaf182897044500d290dbd7f.
use crate::schema::PublicError;
use budgets::{BudgetId, Entries};
use std::time::{Duration, Instant};
pub trait SessionClock: Send + Sync {
    fn now(&self) -> Instant;
}
pub struct MonotonicClock;
impl SessionClock for MonotonicClock {
    fn now(&self) -> Instant {
        Instant::now()
    }
}
/// Serialized by the desktop session mutex, never a status/action mutex. Do not
/// launch a browser/process here. File failures must propagate; no success until
/// the owner-private launch file has been written.
pub trait NativeLauncher: Send {
    fn closed(&self) -> bool;
    fn fresh_token(&mut self) -> Result<String, PublicError>;
    fn publish_launch(&mut self, origin: &str, token: &str) -> Result<(), PublicError>;
    fn consume_launch(&mut self) -> Result<(), PublicError>;
}
pub fn launch_lifetime() -> Result<Duration, PublicError> {
    duration(BudgetId::PyClockMyceliumA13MacosAppL872C8)
}
pub fn session_lifetime() -> Result<Duration, PublicError> {
    duration(BudgetId::PyClockMyceliumA13MacosAppL1995C16)
}
fn duration(id: BudgetId) -> Result<Duration, PublicError> {
    id.span(Entries::new(0))
        .map(|v| Duration::from_millis(v.millis()))
        .map_err(|_| PublicError::new(503, "budget_unavailable"))
}
pub(crate) fn launch_form(raw: &[u8]) -> Result<Option<String>, PublicError> {
    if !raw.is_ascii() {
        return Err(PublicError::new(400, "invalid_request"));
    }
    if raw.is_empty() {
        return Ok(None);
    }
    let mut fields = Vec::new();
    for part in raw.split(|b| *b == b'&') {
        let Some(at) = part.iter().position(|b| *b == b'=') else {
            return Err(PublicError::new(400, "invalid_request"));
        };
        let value = form_decode(&part[at + 1..]);
        if !value.is_empty() {
            fields.push((form_decode(&part[..at]), value));
        }
    }
    if fields.len() == 1 && fields[0].0 == "launch" {
        Ok(fields.pop().map(|(_, value)| value))
    } else {
        Ok(None)
    }
}
fn form_decode(raw: &[u8]) -> String {
    let mut out = Vec::new();
    let mut at = 0;
    while at < raw.len() {
        if raw[at] == b'%'
            && at + 2 < raw.len()
            && let (Some(a), Some(b)) = (
                (raw[at + 1] as char).to_digit(16),
                (raw[at + 2] as char).to_digit(16),
            )
        {
            out.push((a * 16 + b) as u8);
            at += 3;
        } else {
            out.push(if raw[at] == b'+' { b' ' } else { raw[at] });
            at += 1;
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}
pub(crate) fn valid_token(s: &str) -> bool {
    !s.is_empty()
        && s.bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
}
