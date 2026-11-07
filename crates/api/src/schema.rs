// SPDX-License-Identifier: AGPL-3.0-only
//! Frozen desktop envelopes at f4e366e9, app.py:1263-1283,1873-2033.
//! Nested component projections remain L6/L9-owned; this is NOT their validator.
use serde::{
    Deserialize, Deserializer,
    de::{self, MapAccess, SeqAccess, Visitor},
};
use serde_json::{Map, Value, json, value::RawValue};
use std::fmt;

/// Which v0.1.4 exception raised a failure: the identity `last-action-error.private.json`
/// persists (`exception_type` / `exception_module`, app.py:354-380). It never reaches the wire
/// and never takes part in equality.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Raised {
    /// A producer that has not named its oracle exception yet.
    Unspecified,
    /// `mycelium_a13_macos.authority.DemoAuthorityError`.
    Authority,
    /// `mycelium_a13_macos.pairing.PairingError`.
    Pairing,
    /// `mycelium_a13_macos.peer.RemoteMacMemberError`.
    Peer,
    /// `mycelium_invite.token.InviteError`.
    Invite,
    /// `mycelium_node.durable_membership.DurableMembershipError`.
    DurableMembership,
    /// `mycelium_a13_macos.standing_consent.StandingConsentError`.
    Standing,
    /// `mycelium_a13_macos.member_leave.LeaveError`.
    Leave,
    /// `mycelium_a13_macos.acquisition.AcquisitionError`.
    Acquisition,
    /// `mycelium_membership.contracts.MembershipContractError` (a `ValueError`): its closed code is
    /// the public one, so it surfaces unchanged where no `except` clause translates it.
    MembershipContract,
    /// `json.decoder.JSONDecodeError` (a builtin `ValueError` subclass outside `builtins`).
    JsonDecode,
    /// A builtin exception class (`code` is then `action_not_completed`).
    Builtin(&'static str),
    /// An `http.client` exception (`IncompleteRead`, `BadStatusLine`, ...): not an `OSError`, so it escapes
    /// the seed client and every `except` clause above it (`code` is then `action_not_completed`).
    HttpClient(&'static str),
}
impl Raised {
    /// The recorded `(exception_type, exception_module)`.
    pub fn identity(self) -> (&'static str, &'static str) {
        match self {
            Self::Unspecified => ("PublicError", "api::schema"),
            Self::Authority => ("DemoAuthorityError", "mycelium_a13_macos.authority"),
            Self::Pairing => ("PairingError", "mycelium_a13_macos.pairing"),
            Self::Peer => ("RemoteMacMemberError", "mycelium_a13_macos.peer"),
            Self::Invite => ("InviteError", "mycelium_invite.token"),
            Self::DurableMembership => {
                ("DurableMembershipError", "mycelium_node.durable_membership")
            }
            Self::Leave => ("LeaveError", "mycelium_a13_macos.member_leave"),
            Self::Acquisition => ("AcquisitionError", "mycelium_a13_macos.acquisition"),
            Self::MembershipContract => {
                ("MembershipContractError", "mycelium_membership.contracts")
            }
            Self::Standing => (
                "StandingConsentError",
                "mycelium_a13_macos.standing_consent",
            ),
            Self::JsonDecode => ("JSONDecodeError", "json.decoder"),
            Self::Builtin(class) => (class, "builtins"),
            Self::HttpClient(class) => (class, "http.client"),
        }
    }
    /// The class a library error code belongs to when the producer did not name one: closed
    /// codes are owned by one oracle module each (`authority_*` by the authority, ...).
    pub fn for_code(code: &str) -> Self {
        if code.starts_with("authority_") {
            Self::Authority
        } else if code.starts_with("standing_consent_") || code.starts_with("member_") {
            Self::Standing
        } else if code.starts_with("pairing_") || code.starts_with("join_") {
            Self::Pairing
        } else if code.starts_with("peer_") {
            Self::Peer
        } else if code.starts_with("invite_") {
            Self::Invite
        } else if code.starts_with("node_membership_state_") {
            Self::DurableMembership
        } else {
            Self::Unspecified
        }
    }
}
/// The numbers of a `seed_http_seed_time_invalid` (`_seed_time_invalid`, `app.seed_time_window`): each a
/// finite float or `None`. The three derived fields are computed from these (kept small: a
/// `PublicError` travels in every `Result`).
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct SeedWindow {
    pub now: Option<f64>,
    pub issued_at: Option<f64>,
    pub expires_at: Option<f64>,
}
impl SeedWindow {
    fn all(&self) -> Option<(f64, f64, f64)> {
        Some((self.now?, self.issued_at?, self.expires_at?))
    }
    /// `issued_at - now`, when all three numbers are finite.
    pub fn seconds_before_issued(&self) -> Option<f64> {
        self.all().map(|(n, i, _)| i - n)
    }
    /// `now - expires_at`, when all three numbers are finite.
    pub fn seconds_after_expires(&self) -> Option<f64> {
        self.all().map(|(n, _, e)| n - e)
    }
}
#[derive(Clone, Copy, Debug)]
pub struct PublicError {
    pub status: u16,
    pub code: &'static str,
    pub raised: Raised,
    /// Evidence a refusal carries beyond its code: the ticket of its `SeedWindow` (the source's
    /// `error.window` on the cause chain), 0 for none. See [`PublicError::window`].
    window_ticket: u32,
    /// The advice of a `DemoActionError(code, message=, fix=)` (`action_error_body`, app.py:76-81), 0 for none.
    detail_ticket: u32,
}
impl PartialEq for PublicError {
    fn eq(&self, other: &Self) -> bool {
        self.status == other.status && self.code == other.code
    }
}
impl Eq for PublicError {}
/// The most recent seed windows (a bounded ring, so a `PublicError` stays small and `Copy`).
const WINDOW_RING: usize = 16;
static WINDOWS: std::sync::Mutex<(u32, [Option<SeedWindow>; WINDOW_RING])> =
    std::sync::Mutex::new((0, [None; WINDOW_RING]));
/// The most recent refusal advice (`message`, `fix`), bounded like the seed windows.
type DetailRing = (u32, [Option<(String, Value)>; WINDOW_RING]);
static DETAILS: std::sync::Mutex<DetailRing> =
    std::sync::Mutex::new((0, [const { None }; WINDOW_RING]));
impl PublicError {
    /// The same failure with the oracle's `message` and `fix` in its public body (`DemoActionError`).
    #[must_use]
    pub fn with_detail(self, message: String, fix: Value) -> Self {
        let mut ring = DETAILS
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        ring.0 = ring.0.wrapping_add(1).max(1);
        let ticket = ring.0;
        let slot = ticket as usize % WINDOW_RING;
        ring.1[slot] = Some((message, fix));
        Self {
            detail_ticket: ticket,
            ..self
        }
    }
    /// The advice while it is still among the most recent ones.
    pub fn detail(&self) -> Option<(String, Value)> {
        if self.detail_ticket == 0 {
            return None;
        }
        let ring = DETAILS
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let newer = ring.0.wrapping_sub(self.detail_ticket) as usize;
        (newer < WINDOW_RING)
            .then(|| ring.1[self.detail_ticket as usize % WINDOW_RING].clone())
            .flatten()
    }
    /// The same failure carrying the window numbers of a seed time refusal.
    #[must_use]
    pub fn with_window(self, window: SeedWindow) -> Self {
        let mut ring = WINDOWS
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        ring.0 = ring.0.wrapping_add(1).max(1);
        let ticket = ring.0;
        let slot = ticket as usize % WINDOW_RING;
        ring.1[slot] = Some(window);
        Self {
            window_ticket: ticket,
            ..self
        }
    }
    /// The window of a seed time refusal while it is still among the most recent ones.
    pub fn window(&self) -> Option<SeedWindow> {
        if self.window_ticket == 0 {
            return None;
        }
        let ring = WINDOWS
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        // A ticket whose slot was reused by a newer refusal is no longer evidence for this error.
        let newer = ring.0.wrapping_sub(self.window_ticket) as usize;
        (newer < WINDOW_RING)
            .then(|| ring.1[self.window_ticket as usize % WINDOW_RING])
            .flatten()
    }
    pub const fn new(status: u16, code: &'static str) -> Self {
        Self {
            status,
            code,
            raised: Raised::Unspecified,
            window_ticket: 0,
            detail_ticket: 0,
        }
    }
    /// The same failure, raised by `raised` in the oracle.
    pub const fn raised_by(self, raised: Raised) -> Self {
        Self { raised, ..self }
    }
    pub fn document(self) -> Value {
        let mut body = json!({"error":self.code});
        if let Some((message, fix)) = self.detail() {
            body["message"] = Value::String(message);
            body["fix"] = fix;
        }
        body
    }
    /// An uncaught server-side exception in the source: the handler thread dies, `handle_error`
    /// is a no-op and the socket closes with no bytes (server.py:97-98). Status 0 marks it; the
    /// transport must write nothing.
    pub const fn dropped(code: &'static str) -> Self {
        Self::new(0, code)
    }
    pub const fn is_dropped(&self) -> bool {
        self.status == 0
    }
}
impl fmt::Display for PublicError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}:{}", self.status, self.code)
    }
}
impl std::error::Error for PublicError {}

pub const STATUS_FIELDS: &[&str] = &[
    "label",
    "role",
    "model_contract",
    "run_model",
    "fast_mode",
    "network",
    "acquisition",
    "resource_observation",
    "acquisition_policy_error",
    "installation_role",
    "resource_policy",
    "running",
    "authority",
    "renewal_error",
    "recovery_error",
    "distribution",
    "leave",
    "join_flow",
    "adaptive_host",
    "adaptive_host_publication",
    "sharing",
    "relay_wait",
    "owner_run",
    "demo_target",
    "chat",
    "swarm",
    "serving",
    "peer_liveness",
    "test_seams_active",
    "test_seams",
];
#[derive(Clone)]
pub struct Snapshot(crate::wire::Json);
impl Snapshot {
    /// A sorted-key snapshot (objects of a `Value` iterate sorted).
    pub fn parse(value: Value) -> Result<Self, PublicError> {
        Self::parse_json(crate::wire::Json::from(value))
    }
    /// A snapshot whose objects keep the host's insertion order, as v0.1.4's `status()` dict does.
    pub fn parse_json(value: crate::wire::Json) -> Result<Self, PublicError> {
        let valid = matches!(&value, crate::wire::Json::Obj(o)
            if o.len() == STATUS_FIELDS.len() && STATUS_FIELDS.iter().all(|key| value.get(key).is_some()))
            && matches!(value.get("installation_role"),
                Some(crate::wire::Json::Str(s)) if s == "owner" || s == "target")
            && matches!(value.get("running"), Some(crate::wire::Json::Bool(_)))
            && matches!(
                value.get("test_seams_active"),
                Some(crate::wire::Json::Bool(_))
            );
        if !valid {
            return Err(PublicError::new(503, "status_unavailable"));
        }
        Ok(Self(value))
    }
    pub(crate) fn document(&self) -> crate::wire::Json {
        self.0.clone()
    }
}

/// JSON ingress refuses duplicate keys at EVERY nesting level. serde Value alone
/// is last-key-wins and would silently change the signed/consent input contract.
pub fn strict_object(raw: &[u8]) -> Result<Map<String, Value>, PublicError> {
    let value = unique_value(raw).map_err(|_| PublicError::new(400, "invalid_request"))?;
    match value {
        Value::Object(map) => Ok(map),
        _ => Err(PublicError::new(400, "invalid_request")),
    }
}
pub(crate) fn unique_value(raw: &[u8]) -> Result<Value, serde_json::Error> {
    // Preserve the existing serde recursion limit and duplicate-key guard.
    // Its Value is NOT the result: arbitrary_precision represents numbers as
    // synthetic maps to a generic Visitor, indistinguishable from user maps.
    // Decode original raw tokens after structural admission instead.
    serde_json::from_slice::<Unique>(raw)?;
    let raw: Box<RawValue> = serde_json::from_slice(raw)?;
    decode_unique(raw.get())
}
fn decode_unique(raw: &str) -> Result<Value, serde_json::Error> {
    match raw.as_bytes().first() {
        Some(b'{') => {
            struct Object;
            impl<'de> Visitor<'de> for Object {
                type Value = Value;
                fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                    f.write_str("unique-key object")
                }
                fn visit_map<A: MapAccess<'de>>(self, mut input: A) -> Result<Value, A::Error> {
                    let mut values = Map::new();
                    while let Some(key) = input.next_key::<String>()? {
                        if values.contains_key(&key) {
                            return Err(de::Error::custom("duplicate_key"));
                        }
                        let raw = input.next_value::<Box<RawValue>>()?;
                        values.insert(key, decode_unique(raw.get()).map_err(de::Error::custom)?);
                    }
                    Ok(Value::Object(values))
                }
            }
            let mut parser = serde_json::Deserializer::from_str(raw);
            let value = parser.deserialize_map(Object)?;
            parser.end()?;
            Ok(value)
        }
        Some(b'[') => {
            let items: Vec<Box<RawValue>> = serde_json::from_str(raw)?;
            items
                .iter()
                .map(|item| decode_unique(item.get()))
                .collect::<Result<Vec<_>, _>>()
                .map(Value::Array)
        }
        Some(b'-' | b'0'..=b'9') if raw.contains(['.', 'e', 'E']) => {
            // Python json.loads materializes floats, not their original lexeme.
            let value: f64 = serde_json::from_str(raw)?;
            serde_json::Number::from_f64(value)
                .map(Value::Number)
                .ok_or_else(|| de::Error::custom("nonfinite"))
        }
        _ => serde_json::from_str(raw),
    }
}
struct Unique(Value);
impl<'de> Deserialize<'de> for Unique {
    fn deserialize<D: de::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Unique;
            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("unique-key JSON")
            }
            fn visit_bool<E: de::Error>(self, v: bool) -> Result<Unique, E> {
                Ok(Unique(Value::Bool(v)))
            }
            fn visit_i64<E: de::Error>(self, v: i64) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_u64<E: de::Error>(self, v: u64) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_f64<E: de::Error>(self, v: f64) -> Result<Unique, E> {
                serde_json::Number::from_f64(v)
                    .map(|n| Unique(Value::Number(n)))
                    .ok_or_else(|| E::custom("nonfinite"))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_string<E: de::Error>(self, v: String) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_unit<E: de::Error>(self) -> Result<Unique, E> {
                Ok(Unique(Value::Null))
            }
            fn visit_none<E: de::Error>(self) -> Result<Unique, E> {
                Ok(Unique(Value::Null))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut a: A) -> Result<Unique, A::Error> {
                let mut v = Vec::new();
                while let Some(item) = a.next_element::<Unique>()? {
                    v.push(item.0);
                }
                Ok(Unique(Value::Array(v)))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut a: A) -> Result<Unique, A::Error> {
                let mut v = Map::new();
                while let Some(key) = a.next_key::<String>()? {
                    if v.contains_key(&key) {
                        return Err(de::Error::custom("duplicate_key"));
                    }
                    v.insert(key, a.next_value::<Unique>()?.0);
                }
                Ok(Unique(Value::Object(v)))
            }
        }
        deserializer.deserialize_any(V)
    }
}
