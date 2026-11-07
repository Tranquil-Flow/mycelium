// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's in-memory registry of single-use pairing windows (`PairingWindows`,
//! `mycelium_a13_macos/pairing.py:608-830` @ f4e366e9): open / submit / approve / poll / revoke, the
//! 5-try code lockout, expiry, the 8-window capacity and the public HTTP surface the relay proxy
//! hands `/pairing-window/v1/*` to. Compared with the executed oracle in
//! `tests/pairing_windows.rs` (`fixtures/oracle_pairwin.json`, probe `r15-probe-pairwin.py`).
//! Windows are in-memory: an owner restart closes every open window (fail closed).
use crate::{
    capability,
    handoff::validate_quota,
    join_link::canonical_https_origin,
    pairing,
    persistent::identity_fingerprint,
    schema::{PublicError, Raised},
    wire::Json,
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use budgets::{BudgetId, Entries};
use protocol::canonical;
use serde_json::{Number, Value};
use std::sync::Mutex;

pub const PAIRING_WINDOW_PREFIX: &str = "/pairing-window/v1/";
pub const JOIN_LINK_SCHEME: &str = "mycelium-join:";
pub const JOIN_LINK_PROTOCOL: &str = "mycelium.a13_join_link.v1";
pub const SUBMISSION_PROTOCOL: &str = "mycelium.a13_pairing_submission.v1";
pub const POLL_PROTOCOL: &str = "mycelium.a13_pairing_poll.v1";
/// `_WINDOW_TTL_MIN` / `_WINDOW_TTL_MAX` (pairing.py:526-527), seconds, from the budget registry.
pub fn window_ttl_bounds() -> (i64, i64) {
    let seconds = |id: BudgetId| {
        id.span(Entries::new(0))
            .map_or(0, |s| i64::try_from(s.millis() / 1000).unwrap_or(i64::MAX))
    };
    (
        seconds(BudgetId::PyFinalMyceliumA13MacosPairingWindowTtlMinL452C0),
        seconds(BudgetId::PyFinalMyceliumA13MacosPairingWindowTtlMaxL453C0),
    )
}
/// `_MAX_WINDOWS` (pairing.py:528), count, from the budget registry.
pub fn max_windows() -> usize {
    BudgetId::PyFinalMyceliumA13MacosPairingMaxWindowsL454C0
        .count()
        .map_or(0, |c| usize::try_from(c.get()).unwrap_or(usize::MAX))
}
/// `_MAX_CODE_FAILURES` (pairing.py:529), count. SE-L10-PAIRING-BOUNDS (L1b): no registry id yet.
const MAX_CODE_FAILURES: u32 = 5;
const CODE_ALPHABET: &[u8] = b"23456789ABCDEFGHJKMNPQRSTVWXYZ";
const TUNNEL_SUFFIX: &str = ".trycloudflare.com";

fn fail(code: &'static str) -> PublicError {
    PublicError::new(409, code).raised_by(Raised::Pairing)
}
fn code_text(text: &str, length: usize) -> bool {
    text.len() == length && text.bytes().all(|b| CODE_ALPHABET.contains(&b))
}
fn digest_text(value: &Value) -> bool {
    value.as_str().is_some_and(|s| {
        s.strip_prefix("sha256:").is_some_and(|h| {
            h.len() == 64
                && h.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
    })
}
/// The HTTP status of each window refusal (`_WINDOW_STATUS`); every other code is a 400.
fn window_status(code: &str) -> u16 {
    match code {
        "pairing_window_unknown" => 404,
        "pairing_window_expired" | "pairing_window_revoked" | "pairing_window_locked" => 410,
        "pairing_window_code_invalid" | "pairing_window_request_mismatch" => 403,
        "pairing_window_claimed" => 409,
        _ => 400,
    }
}
/// `^[a-z][a-z0-9_]{0,95}$`.
pub fn error_code_shape(code: &str) -> bool {
    (1..=96).contains(&code.len())
        && code.as_bytes()[0].is_ascii_lowercase()
        && code
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum State {
    Open,
    Claimed,
    Approved,
    Delivered,
    Expired,
    Revoked,
    Locked,
}
impl State {
    fn name(self) -> &'static str {
        match self {
            Self::Open => "open",
            Self::Claimed => "claimed",
            Self::Approved => "approved",
            Self::Delivered => "delivered",
            Self::Expired => "expired",
            Self::Revoked => "revoked",
            Self::Locked => "locked",
        }
    }
    fn closed(self) -> bool {
        matches!(self, Self::Expired | Self::Revoked | Self::Locked)
    }
}
struct Window {
    id: String,
    code: String,
    origin: String,
    seed_key_digest: String,
    opened_at: f64,
    expires_at: f64,
    state: State,
    failures: u32,
    request: Option<Value>,
    report: Option<Value>,
    quota: Option<Value>,
    /// The claimant's `(device, link, quota)` in the key order it sent them (the owner view echoes them).
    parts: Option<(Json, Json, Json)>,
    handoff: Option<Json>,
}
impl Window {
    fn clear(&mut self) {
        self.request = None;
        self.report = None;
        self.quota = None;
        self.parts = None;
        self.handoff = None;
    }
    /// `_refresh`: a live window past `expires_at` expires and forgets its claimant.
    fn refresh(&mut self, now: f64) {
        if matches!(
            self.state,
            State::Open | State::Claimed | State::Approved | State::Delivered
        ) && now > self.expires_at
        {
            self.state = State::Expired;
            self.clear();
        }
    }
}

fn float(value: f64) -> Json {
    Number::from_f64(value).map_or(Json::Null, Json::Num)
}
/// `request_digest`: `sha256:` + the canonical digest of one exact pairing request.
pub fn request_digest(request: &Value) -> Result<String, PublicError> {
    canonical::digest(request).map_err(|_| fail("pairing_request_invalid"))
}
/// `encode_join_link` of the window's link document.
pub fn encode_join_link(document: &Value) -> Result<String, PublicError> {
    let bytes = canonical::to_bytes(document).map_err(|_| fail("join_code_invalid"))?;
    Ok(format!(
        "{JOIN_LINK_SCHEME}{}",
        URL_SAFE_NO_PAD.encode(bytes)
    ))
}
fn random_text_os(length: usize) -> String {
    use std::io::Read;
    let mut out = String::new();
    let mut file = std::fs::File::open("/dev/urandom").ok();
    let mut byte = [0u8; 1];
    // `secrets.choice`: rejection sampling, no modulo bias (240 = 8 * 30).
    while out.len() < length {
        let drew = file
            .as_mut()
            .is_some_and(|f| f.read_exact(&mut byte).is_ok());
        if drew && byte[0] < 240 {
            out.push(char::from(
                CODE_ALPHABET[usize::from(byte[0]) % CODE_ALPHABET.len()],
            ));
        } else if !drew {
            break;
        }
    }
    out
}

type Clock = Box<dyn Fn() -> f64 + Send + Sync>;
type Random = Box<dyn FnMut(usize) -> String + Send>;
struct Inner {
    windows: Vec<Window>,
    random: Random,
}
/// `PairingWindows`.
pub struct PairingWindows {
    clock: Clock,
    inner: Mutex<Inner>,
}

impl PairingWindows {
    pub fn new(clock: Clock, random: Random) -> Self {
        Self {
            clock,
            inner: Mutex::new(Inner {
                windows: Vec::new(),
                random,
            }),
        }
    }
    /// The wall clock and the operating system's randomness.
    pub fn system() -> Self {
        Self::new(
            Box::new(|| {
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map_or(0.0, |d| d.as_secs_f64())
            }),
            Box::new(random_text_os),
        )
    }
    fn with<R>(&self, f: impl FnOnce(&mut Inner, f64) -> R) -> R {
        let mut guard = self
            .inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let now = (self.clock)();
        f(&mut guard, now)
    }

    /// `open(origin=, seed_key_digest=, ttl_seconds=)`: the owner view with the secrets.
    pub fn open(&self, origin: &Value, pin: &Value, ttl: &Value) -> Result<Json, PublicError> {
        let canonical_origin = canonical_https_origin(origin)
            .ok()
            .filter(|c| origin.as_str() == Some(c.as_str()))
            .ok_or_else(|| fail("pairing_window_origin_invalid"))?;
        if !digest_text(pin) {
            return Err(fail("pairing_window_pin_invalid"));
        }
        let ttl = match ttl {
            Value::Number(n) => n.to_string().parse::<i64>().ok(),
            _ => None,
        }
        .filter(|t| {
            let (low, high) = window_ttl_bounds();
            (low..=high).contains(t)
        })
        .ok_or_else(|| fail("pairing_window_ttl_invalid"))?;
        self.with(|inner, now| {
            for window in &mut inner.windows {
                window.refresh(now);
            }
            let doomed: Vec<String> = inner
                .windows
                .iter()
                .filter(|w| w.state.closed())
                .map(|w| w.id.clone())
                .collect();
            for id in doomed {
                if inner.windows.len() >= max_windows() {
                    inner.windows.retain(|w| w.id != id);
                }
            }
            let live = inner
                .windows
                .iter()
                .filter(|w| matches!(w.state, State::Open | State::Claimed | State::Approved))
                .count();
            if live >= max_windows() {
                return Err(fail("pairing_window_capacity"));
            }
            let mut id = (inner.random)(4);
            while inner.windows.iter().any(|w| w.id == id) {
                id = (inner.random)(4);
            }
            let code = (inner.random)(8);
            let window = Window {
                id,
                code,
                origin: canonical_origin,
                seed_key_digest: pin.as_str().unwrap_or_default().to_owned(),
                opened_at: now,
                expires_at: now + ttl as f64,
                state: State::Open,
                failures: 0,
                request: None,
                report: None,
                quota: None,
                parts: None,
                handoff: None,
            };
            let view = owner_view(&window, true)?;
            inner.windows.push(window);
            Ok(view)
        })
    }

    /// `list()`: every window, oldest first.
    pub fn list(&self) -> Result<Json, PublicError> {
        self.with(|inner, now| {
            for window in &mut inner.windows {
                window.refresh(now);
            }
            let mut order: Vec<&Window> = inner.windows.iter().collect();
            order.sort_by(|a, b| a.opened_at.total_cmp(&b.opened_at));
            order
                .into_iter()
                .map(|w| owner_view(w, false))
                .collect::<Result<Vec<_>, _>>()
                .map(Json::Arr)
        })
    }
    /// How many windows are `open`, `claimed` or `approved` (status `join_flow`, app.py:813).
    pub fn live_count(&self) -> usize {
        self.with(|inner, now| {
            inner
                .windows
                .iter_mut()
                .map(|w| {
                    w.refresh(now);
                    matches!(w.state, State::Open | State::Claimed | State::Approved)
                })
                .filter(|live| *live)
                .count()
        })
    }
    /// `revoke(window_id)`: no refresh, so an expired window becomes `revoked`.
    pub fn revoke(&self, window_id: &Value) -> Result<Json, PublicError> {
        self.with(|inner, _| {
            let window = find_mut(&mut inner.windows, window_id)
                .ok_or_else(|| fail("pairing_window_unknown"))?;
            window.state = State::Revoked;
            window.clear();
            owner_view(window, false)
        })
    }
    /// `pending_request(window_id)`.
    pub fn pending_request(&self, window_id: &Value) -> Option<Value> {
        self.with(|inner, _| {
            find_mut(&mut inner.windows, window_id).and_then(|w| w.request.clone())
        })
    }

    /// `submit(window_id, document)`: the target posts its signed request and join report.
    pub fn submit(&self, window_id: &Value, document: &Value) -> Result<Json, PublicError> {
        self.submit_ordered(window_id, document, &Json::from(document))
    }
    /// `submit` for a document received as bytes: the claimant's key order is kept for the owner view.
    pub fn submit_bytes(&self, window_id: &Value, raw: &[u8]) -> Result<Json, PublicError> {
        let value =
            crate::schema::unique_value(raw).map_err(|_| fail("pairing_submission_invalid"))?;
        let ordered = ordered_json(raw).unwrap_or_else(|| Json::from(&value));
        self.submit_ordered(window_id, &value, &ordered)
    }
    fn submit_ordered(
        &self,
        window_id: &Value,
        document: &Value,
        ordered: &Json,
    ) -> Result<Json, PublicError> {
        self.with(|inner, now| {
            let shaped = document.as_object().filter(|d| {
                d.len() == 5
                    && ["protocol", "code", "request", "report", "quota"]
                        .iter()
                        .all(|k| d.contains_key(*k))
                    && d["protocol"] == SUBMISSION_PROTOCOL
            });
            let Some(document) = shaped else {
                // Unknown/closed windows answer first so a probe learns nothing more.
                live(
                    &mut inner.windows,
                    window_id,
                    document.as_object().and_then(|d| d.get("code")),
                    now,
                )?;
                return Err(fail("pairing_submission_invalid"));
            };
            let window = live(&mut inner.windows, window_id, Some(&document["code"]), now)?;
            pairing::preview(&document["request"], now)
                .map_err(|e| e.raised_by(Raised::Pairing))?;
            let request = document["request"].clone();
            let digest = request_digest(&request)?;
            if window.state != State::Open {
                if let Some(held) = &window.request
                    && request_digest(held)? == digest
                {
                    return waiting(&request);
                }
                let stale = window.state == State::Claimed
                    && window.request.as_ref().is_some_and(|held| {
                        held["identity_key_digest"] == request["identity_key_digest"]
                            && now > held["expires_at"].as_f64().unwrap_or(f64::NAN)
                    });
                if !stale {
                    return Err(fail("pairing_window_claimed"));
                }
            }
            let report = capability::verify_report(
                &document["report"],
                now,
                request["identity_key_digest"].as_str().unwrap_or_default(),
                request["node_id"].as_str().unwrap_or_default(),
                &["join"],
            )?;
            let quota = validate_quota(&document["quota"])?;
            if quota["background_allowed"] != Value::Bool(false) {
                return Err(fail("pairing_quota_invalid"));
            }
            let part = |outer: &str, key: &str| {
                ordered
                    .get(outer)
                    .and_then(|o| if key.is_empty() { Some(o) } else { o.get(key) })
                    .cloned()
                    .unwrap_or(Json::Null)
            };
            window.parts = Some((
                part("report", "device"),
                part("report", "link"),
                part("quota", ""),
            ));
            window.request = Some(request.clone());
            window.report = Some(report);
            window.quota = Some(quota);
            window.state = State::Claimed;
            waiting(&request)
        })
    }

    /// `approve(window_id, request_digest, issue)`: one owner click mints the handoff.
    pub fn approve(
        &self,
        window_id: &Value,
        digest: &Value,
        issue: &mut dyn FnMut(&Value, &Value) -> Result<Json, PublicError>,
    ) -> Result<Json, PublicError> {
        self.with(|inner, now| {
            let window = find_mut(&mut inner.windows, window_id)
                .ok_or_else(|| fail("pairing_window_unknown"))?;
            window.refresh(now);
            if window.state.closed() {
                return Err(window_closed(window.state));
            }
            let (Some(request), State::Claimed | State::Approved | State::Delivered) =
                (window.request.clone(), window.state)
            else {
                return Err(fail("pairing_window_not_claimed"));
            };
            if digest.as_str() != Some(request_digest(&request)?.as_str()) {
                return Err(fail("pairing_window_request_mismatch"));
            }
            if window.handoff.is_none() {
                let quota = window.quota.clone().unwrap_or(Value::Null);
                let handoff = issue(&request, &quota)?;
                if !matches!(handoff, Json::Obj(_)) {
                    return Err(fail("pairing_handoff_invalid"));
                }
                window.handoff = Some(handoff);
                window.state = State::Approved;
            }
            owner_view(window, false)
        })
    }

    /// `poll(window_id, document)`: the target collects the handoff.
    pub fn poll(&self, window_id: &Value, document: &Value) -> Result<Json, PublicError> {
        self.with(|inner, now| {
            let code = document.as_object().and_then(|d| d.get("code"));
            let window = live(&mut inner.windows, window_id, code, now)?;
            let shaped = document.as_object().filter(|d| {
                d.len() == 3
                    && ["protocol", "code", "request_digest"]
                        .iter()
                        .all(|k| d.contains_key(*k))
                    && d["protocol"] == POLL_PROTOCOL
            });
            let Some(document) = shaped else {
                return Err(fail("pairing_submission_invalid"));
            };
            let matches_request = match &window.request {
                Some(held) => {
                    document["request_digest"].as_str() == Some(request_digest(held)?.as_str())
                }
                None => false,
            };
            if !matches_request {
                return Err(fail("pairing_window_request_mismatch"));
            }
            match window.handoff.clone() {
                None => Ok(crate::jobj! {"state" => "waiting_for_owner"}),
                Some(handoff) => {
                    window.state = State::Delivered;
                    Ok(Json::object([
                        ("state", Json::str("approved")),
                        ("handoff", handoff),
                    ]))
                }
            }
        })
    }

    /// Every window's `(id, state)` in insertion order, with no expiry refresh (test and status aid).
    pub fn states(&self) -> Vec<(String, &'static str)> {
        self.with(|inner, _| {
            inner
                .windows
                .iter()
                .map(|w| (w.id.clone(), w.state.name()))
                .collect()
        })
    }
    /// `handle(method, path, body)`: the public HTTP surface behind the relay proxy.
    pub fn handle(&self, method: &str, path: &str, body: &[u8]) -> (u16, Vec<u8>) {
        let reply = |status: u16, value: Json| (status, value.dumps(crate::wire::Text::Ascii));
        let rest = path.strip_prefix(PAIRING_WINDOW_PREFIX).unwrap_or("");
        let (window_id, action) = rest.split_once('/').unwrap_or((rest, ""));
        if action != "request" && action != "poll" {
            return reply(404, crate::jobj! {"error" => "route_unknown"});
        }
        if !code_text(window_id, 4) {
            return reply(404, crate::jobj! {"error" => "pairing_window_unknown"});
        }
        if method != "POST" {
            return reply(405, crate::jobj! {"error" => "method_not_allowed"});
        }
        let document = std::str::from_utf8(body)
            .ok()
            .and_then(|_| crate::schema::unique_value(body).ok())
            .filter(Value::is_object);
        let Some(document) = document else {
            return reply(400, crate::jobj! {"error" => "pairing_submission_invalid"});
        };
        let id = Value::String(window_id.to_owned());
        let outcome = if action == "request" {
            self.submit_bytes(&id, body)
        } else {
            self.poll(&id, &document)
        };
        match outcome {
            Ok(value) => reply(200, value),
            Err(error) => {
                let code = if error_code_shape(error.code) {
                    error.code
                } else {
                    "pairing_submission_invalid"
                };
                reply(window_status(code), crate::jobj! {"error" => code})
            }
        }
    }
}

fn find_mut<'a>(windows: &'a mut [Window], id: &Value) -> Option<&'a mut Window> {
    let id = id.as_str()?;
    windows.iter_mut().find(|w| w.id == id)
}
fn window_closed(state: State) -> PublicError {
    fail(match state {
        State::Expired => "pairing_window_expired",
        State::Revoked => "pairing_window_revoked",
        _ => "pairing_window_locked",
    })
}
/// `_live`: the window must exist, be open to this code, and not be closed.
fn live<'a>(
    windows: &'a mut [Window],
    id: &Value,
    code: Option<&Value>,
    now: f64,
) -> Result<&'a mut Window, PublicError> {
    let window = find_mut(windows, id).ok_or_else(|| fail("pairing_window_unknown"))?;
    window.refresh(now);
    if window.state.closed() {
        return Err(window_closed(window.state));
    }
    // `secrets.compare_digest(code.encode(), window.code.encode())`
    let right = code
        .and_then(Value::as_str)
        .is_some_and(|c| constant_time_eq(c.as_bytes(), window.code.as_bytes()));
    if !right {
        window.failures += 1;
        if window.failures >= MAX_CODE_FAILURES {
            window.state = State::Locked;
            window.clear();
            return Err(fail("pairing_window_locked"));
        }
        return Err(fail("pairing_window_code_invalid"));
    }
    Ok(window)
}
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}
fn waiting(request: &Value) -> Result<Json, PublicError> {
    Ok(crate::jobj! {
        "state" => "waiting_for_owner",
        "fingerprint" => identity_fingerprint(&request["identity_key_digest"])?,
        "request_digest" => request_digest(request)?,
    })
}
/// `_owner_view`.
fn owner_view(window: &Window, with_secret: bool) -> Result<Json, PublicError> {
    let mut view = crate::jobj! {
        "window_id" => window.id.as_str(), "state" => window.state.name(),
    };
    if let Json::Obj(fields) = &mut view {
        fields.push(("opened_at".into(), float(window.opened_at)));
        fields.push(("expires_at".into(), float(window.expires_at)));
    }
    if with_secret || window.state == State::Open {
        let code = &window.code;
        let tunnel = window
            .origin
            .strip_prefix("https://")
            .unwrap_or(&window.origin);
        let tunnel = tunnel.strip_suffix(TUNNEL_SUFFIX).unwrap_or(tunnel);
        let link = serde_json::json!({
            "protocol": JOIN_LINK_PROTOCOL, "origin": window.origin, "window_id": window.id,
            "code": code, "seed_key_digest": window.seed_key_digest,
            "expires_at": Number::from_f64(window.expires_at),
        });
        view.set(
            "short_code",
            Json::str(format!("{}-{}-{}", window.id, &code[..4], &code[4..])),
        );
        view.set("tunnel_name", Json::str(tunnel));
        view.set("join_link", Json::str(encode_join_link(&link)?));
    }
    if let (
        Some(request),
        Some((device, link, quota)),
        State::Claimed | State::Approved | State::Delivered,
    ) = (&window.request, &window.parts, window.state)
    {
        view.set(
            "pending",
            crate::jobj! {
                "node_id" => &request["node_id"], "peer_class" => &request["peer_class"],
                "fingerprint" => identity_fingerprint(&request["identity_key_digest"])?,
                "request_digest" => request_digest(request)?,
                "request_expires_at" => &request["expires_at"],
                "device" => device.clone(), "link" => link.clone(), "quota" => quota.clone(),
            },
        );
    }
    Ok(view)
}

/// `json.loads` keeping each object's key order (a `Value` sorts them); `None` when it is not JSON.
fn ordered_json(raw: &[u8]) -> Option<Json> {
    serde_json::from_slice::<Ordered>(raw).ok().map(|o| o.0)
}
struct Ordered(Json);
impl<'de> serde::Deserialize<'de> for Ordered {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Visit;
        impl<'de> serde::de::Visitor<'de> for Visit {
            type Value = Ordered;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("any JSON value")
            }
            fn visit_unit<E>(self) -> Result<Ordered, E> {
                Ok(Ordered(Json::Null))
            }
            fn visit_i64<E>(self, v: i64) -> Result<Ordered, E> {
                Ok(Ordered(Json::Num(v.into())))
            }
            fn visit_u64<E>(self, v: u64) -> Result<Ordered, E> {
                Ok(Ordered(Json::Num(v.into())))
            }
            fn visit_f64<E>(self, v: f64) -> Result<Ordered, E> {
                Ok(Ordered(Number::from_f64(v).map_or(Json::Null, Json::Num)))
            }
            fn visit_bool<E>(self, v: bool) -> Result<Ordered, E> {
                Ok(Ordered(Json::Bool(v)))
            }
            fn visit_str<E>(self, v: &str) -> Result<Ordered, E> {
                Ok(Ordered(Json::Str(v.to_owned())))
            }
            fn visit_seq<A: serde::de::SeqAccess<'de>>(
                self,
                mut seq: A,
            ) -> Result<Ordered, A::Error> {
                let mut items = Vec::new();
                while let Some(item) = seq.next_element::<Ordered>()? {
                    items.push(item.0);
                }
                Ok(Ordered(Json::Arr(items)))
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(
                self,
                mut map: A,
            ) -> Result<Ordered, A::Error> {
                let mut fields: Vec<(String, Json)> = Vec::new();
                while let Some(key) = map.next_key::<String>()? {
                    // `arbitrary_precision` hands every number over as a one-key map.
                    if fields.is_empty() && key == "$serde_json::private::Number" {
                        let text: String = map.next_value()?;
                        let number = text.parse::<Number>().map_err(serde::de::Error::custom)?;
                        return Ok(Ordered(Json::Num(number)));
                    }
                    fields.push((key, map.next_value::<Ordered>()?.0));
                }
                Ok(Ordered(Json::Obj(fields)))
            }
        }
        deserializer.deserialize_any(Visit)
    }
}
