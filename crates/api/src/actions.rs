// SPDX-License-Identifier: AGPL-3.0-only
//! Typed desktop action tranche: f4e366e9 app.py:676-705,855-868,1306-1477,
//! demo_app.py:109-133 and fast_mode.py:53-69. This is dispatch/shape, NOT a
//! backend, authority, preference store or relay launcher.
use crate::{desktop, schema::PublicError, wire::Json};
use budgets::{BudgetId, Entries};
use serde_json::{Map, Value};
use std::sync::Mutex;
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Action {
    PairPreview,
    PairRequest,
    PairApprove,
    PairAccept,
    PairWindowOpen,
    PairWindowList,
    PairWindowApprove,
    PairWindowRevoke,
    Revoke,
    RevokeSelf,
    Quit,
    Start,
    Stop,
    Leave,
    JoinRequest,
    JoinWindow,
    JoinWindowPoll,
    JoinWindowCancel,
    RunModel,
    FastMode,
    Create,
    ConsentStandingWithdraw,
    DemoStart,
    DemoRebuild,
    DemoStop,
    NetworkStart,
    NetworkRestart,
    NetworkReconnect,
    NetworkRecoveryCard,
    ServingCompile,
    ServingStart,
    ServingActivate,
    ServingStatus,
    ServingStop,
    ModelDownload,
    ModelCancel,
    LocalContributorStart,
    LocalContributorStop,
    /// `swarm_publish.ROUTES` app mode only; a host without listing custody answers as before.
    SwarmPublish,
    SwarmUnpublish,
}
impl Action {
    pub fn from_path(path: &str) -> Result<Self, PublicError> {
        Ok(match path {
            "/api/pair/preview" => Self::PairPreview,
            "/api/pair/request" => Self::PairRequest,
            "/api/pair/approve" => Self::PairApprove,
            "/api/pair/accept" => Self::PairAccept,
            "/api/pair/window/open" => Self::PairWindowOpen,
            "/api/pair/window/list" => Self::PairWindowList,
            "/api/pair/window/approve" => Self::PairWindowApprove,
            "/api/pair/window/revoke" => Self::PairWindowRevoke,
            "/api/quit" => Self::Quit,
            "/api/start" => Self::Start,
            "/api/stop" => Self::Stop,
            "/api/leave" => Self::Leave,
            "/api/revoke" => Self::Revoke,
            "/api/revoke-self" => Self::RevokeSelf,
            "/api/join/request" => Self::JoinRequest,
            "/api/join/window" => Self::JoinWindow,
            "/api/join/window/poll" => Self::JoinWindowPoll,
            "/api/join/window/cancel" => Self::JoinWindowCancel,
            "/api/run-model" => Self::RunModel,
            "/api/fast-mode" => Self::FastMode,
            "/api/create" => Self::Create,
            "/api/consent/standing/withdraw" => Self::ConsentStandingWithdraw,
            "/api/demo/start" => Self::DemoStart,
            "/api/demo/rebuild" => Self::DemoRebuild,
            "/api/demo/stop" => Self::DemoStop,
            "/api/network/start" => Self::NetworkStart,
            "/api/network/restart" => Self::NetworkRestart,
            "/api/network/reconnect" => Self::NetworkReconnect,
            "/api/network/recovery-card" => Self::NetworkRecoveryCard,
            "/api/serving/compile" => Self::ServingCompile,
            "/api/serving/start" => Self::ServingStart,
            "/api/serving/activate" => Self::ServingActivate,
            "/api/serving/status" => Self::ServingStatus,
            "/api/serving/stop" => Self::ServingStop,
            "/api/model/download" => Self::ModelDownload,
            "/api/model/cancel" => Self::ModelCancel,
            "/api/local-contributor/start" => Self::LocalContributorStart,
            "/api/local-contributor/stop" => Self::LocalContributorStop,
            "/api/swarm/publish" => Self::SwarmPublish,
            "/api/swarm/unpublish" => Self::SwarmUnpublish,
            _ => return Err(refuse("unknown_action")),
        })
    }
    fn validate(self, body: &Map<String, Value>) -> Result<(), PublicError> {
        use Action::*;
        let (valid, code) = match self {
            PairPreview => (exact(body, &["request"]), "invalid_request"),
            // app.py:1580-1583,1661-1680,1689-1740: bodies are closed shapes.
            PairRequest | PairWindowList | JoinWindowPoll | JoinWindowCancel => {
                (body.is_empty(), "invalid_request")
            }
            PairApprove => (
                exact(body, &["request", "ttl_seconds", "quota"]),
                "invalid_request",
            ),
            PairAccept => (exact(body, &["handoff"]), "invalid_request"),
            // app.py:1618-1640: `if body: raise DemoError('invalid_request')`.
            // app.py:1369-1374: `if body: raise DemoError('invalid_request')`.
            Quit | Start | Stop | LocalContributorStart | LocalContributorStop => {
                (body.is_empty(), "invalid_request")
            }
            // member_leave.leave_action: role, shape, confirmation and mode are checked in that order
            // by the persistent backend (a single code here could not keep the order).
            Leave => (true, "invalid_request"),
            // swarm_publish.action: the role is checked before the shape, by the listing
            // custody itself, so hosts without one keep answering `unknown_action`.
            SwarmPublish | SwarmUnpublish => (true, "invalid_request"),
            Revoke => (exact(body, &["node_id", "generation"]), "invalid_request"),
            RevokeSelf => (
                exact(body, &["confirm"]) && body["confirm"] == true,
                "revoke_confirmation_required",
            ),
            PairWindowOpen => (exact(body, &["ttl_seconds"]), "invalid_request"),
            PairWindowRevoke => (exact(body, &["window_id"]), "invalid_request"),
            PairWindowApprove => (
                exact(body, &["window_id", "request_digest"]),
                "invalid_request",
            ),
            JoinRequest => (
                exact(body, &["consent"])
                    && crate::preferences::resource_policy::consent_valid(&body["consent"]),
                "consent_required",
            ),
            JoinWindow => (
                exact(body, &["consent", "standing_consent", "join"])
                    && crate::preferences::resource_policy::consent_valid(&body["consent"]),
                "consent_required",
            ),
            RunModel => (exact(body, &["choice"]), "invalid_request"),
            FastMode => (
                exact(body, &["enabled"]) && body["enabled"].is_boolean(),
                "invalid_request",
            ),
            Create => (
                (exact(body, &["consent"])
                    || exact(body, &["consent", "relay_acknowledged", "max_seconds"]))
                    && crate::preferences::resource_policy::consent_valid(&body["consent"]),
                "consent_required",
            ),
            ConsentStandingWithdraw => (
                exact(body, &["confirm"]) && body["confirm"] == true,
                "revoke_confirmation_required",
            ),
            DemoStart => (
                body.is_empty() || exact(body, &["execution_ceiling"]),
                "invalid_request",
            ),
            DemoRebuild | DemoStop | ServingStatus | ServingStop | ModelCancel => {
                (body.is_empty(), "invalid_request")
            }
            // app.py:1551-1553: `set(body) != {'consent'} or not isinstance(body['consent'], dict)`.
            ModelDownload => (
                exact(body, &["consent"]) && body["consent"].is_object(),
                "download_consent_required",
            ),
            ServingCompile => (
                exact(body, &["model_preparation_acknowledged"]),
                "model_preparation_consent_required",
            ),
            ServingStart => (
                exact(
                    body,
                    &["execution_proposal", "model_execution_acknowledged"],
                ),
                "model_execution_consent_required",
            ),
            ServingActivate => (
                exact(body, &["execution_proposal", "activation_acknowledged"]),
                "model_execution_consent_required",
            ),
            NetworkReconnect => (
                exact(body, &["card", "relay_acknowledged"]) && body["relay_acknowledged"] == true,
                "network_consent_required",
            ),
            NetworkRecoveryCard => (
                (exact(body, &["relay_acknowledged"])
                    || exact(body, &["relay_acknowledged", "previous_origin"]))
                    && body["relay_acknowledged"] == true,
                "network_consent_required",
            ),
            NetworkStart | NetworkRestart => {
                (network_shape(self, body)?, "network_consent_required")
            }
        };
        if valid { Ok(()) } else { Err(refuse(code)) }
    }
}
fn refuse(code: &'static str) -> PublicError {
    PublicError::new(400, code)
}
fn exact(body: &Map<String, Value>, keys: &[&str]) -> bool {
    body.len() == keys.len() && keys.iter().all(|key| body.contains_key(*key))
}
fn network_shape(action: Action, b: &Map<String, Value>) -> Result<bool, PublicError> {
    let base = ["relay_acknowledged", "max_seconds"];
    let external = b.get("provider").is_some_and(|v| v == "external");
    let shape = if external {
        let required: &[&str] = if action == Action::NetworkStart {
            &[
                "relay_acknowledged",
                "max_seconds",
                "provider",
                "public_origin",
                "proxy_port",
            ]
        } else {
            &["relay_acknowledged", "max_seconds", "provider"]
        };
        let allowed = [
            "relay_acknowledged",
            "max_seconds",
            "provider",
            "public_origin",
            "proxy_port",
            "cloudflare_front",
            "auto_renew",
            "long_lived",
            "max_renewed_seconds",
        ];
        required.iter().all(|k| b.contains_key(*k))
            && b.keys().all(|k| allowed.contains(&k.as_str()))
    } else if action == Action::NetworkStart {
        (exact(b, &base) || exact(b, &["relay_acknowledged", "max_seconds", "provider"]))
            && b.get("provider").is_none_or(|v| v == "cloudflare")
    } else {
        exact(b, &base)
    };
    let seconds = |id: BudgetId| {
        id.span(Entries::new(0))
            .map(|v| v.millis() / 1000)
            .map_err(|_| PublicError::new(503, "budget_unavailable"))
    };
    let lo = seconds(BudgetId::RelayMinimumWindow)?;
    let hi = seconds(BudgetId::QuickRelayMaximumWindow)?;
    Ok(shape
        && b.get("relay_acknowledged") == Some(&Value::Bool(true))
        && b.get("max_seconds")
            .and_then(Value::as_u64)
            .is_some_and(|v| (lo..=hi).contains(&v)))
}
/// Hooks run in one serialized action transaction, never under PublishedStatus's
/// mutex. All state/authority policy remains mandatory backend work, not inferred
/// from a display snapshot. Other entrypoints must share the backend's authority
/// fencing; this adapter's mutex alone is NOT a global authority lock.
pub trait Backend: Send {
    /// Check application_closed first, then route-specific role/currentness.
    /// Preserve source pre-shape order: demo execution_ceiling validation;
    /// serving start/activate relay gate; network-start stopped-relay cleanup.
    /// Do not start work/consume authority merely because a route is recognized.
    fn prepare(&mut self, action: Action, body: &Map<String, Value>) -> Result<(), PublicError>;
    /// Perform remaining nested validation, consent/currentness, effects and
    /// exact public response. Preserve post-shape active/locked preference checks,
    /// demo swarm/relay gates, network origin validation and rollback semantics.
    fn execute(&mut self, action: Action, body: &Map<String, Value>) -> Result<Value, PublicError>;
    /// Same as `execute` for backends that build the response in the source's insertion order
    /// (integrator ruling (a)); the default converts the sorted `Value`.
    fn execute_json(
        &mut self,
        action: Action,
        body: &Map<String, Value>,
    ) -> Result<Json, PublicError> {
        self.execute(action, body).map(Json::from)
    }
    /// Publish completed display state before the HTTP success can be returned.
    /// A failure must remain observable; never turn it into a success response.
    fn publish(&mut self) -> Result<(), PublicError>;
    /// `adaptive_owner.apply_first_consent_choice` for the optional `adaptive_sharing` bool of a
    /// consent call, applied only after the consent step succeeded and before publication.
    fn apply_first_consent_choice(&mut self, _enabled: bool) -> Result<(), PublicError> {
        Ok(())
    }
    /// A non-`DemoError` failure (status 409): the handler records WHY before answering
    /// (app.py:2008-2031, `last-action-error.private.json`). Best effort; never fails the call.
    fn record_failure(&mut self, _path: &str, _error: &PublicError) {}
    /// A failed action may already have changed state the status shows (a retained handoff, a
    /// consumed pairing request): v0.1.4 serves live status, so a backend that owns such effects
    /// republishes. Best effort - a publication failure never replaces the action's own error.
    fn republish_after_failure(&mut self, _action: Action) {}
}
pub struct Control<S: Backend> {
    backend: Mutex<S>,
}
impl<S: Backend> Control<S> {
    pub fn new(backend: S) -> Self {
        Self {
            backend: Mutex::new(backend),
        }
    }
}
impl<S: Backend> desktop::Service for Control<S> {
    fn action(&self, path: &str, body: &Map<String, Value>) -> Result<Value, PublicError> {
        self.action_json(path, body).map(|json| json.to_value())
    }
    fn action_json(&self, path: &str, body: &Map<String, Value>) -> Result<Json, PublicError> {
        let action = Action::from_path(path)?;
        // app.py:502,1306-1327: `_CONSENT_ROUTES` (create, join/request, join/window) take the optional top-level `adaptive_sharing` bool of the consent form, which is
        // split off before every strict key check (and before the closed check).
        let mut choice = None;
        let mut stripped;
        let mut body = body;
        if matches!(
            action,
            Action::Create | Action::JoinRequest | Action::JoinWindow
        ) && body.contains_key("adaptive_sharing")
        {
            choice = Some(
                body["adaptive_sharing"]
                    .as_bool()
                    .ok_or_else(|| refuse("invalid_request"))?,
            );
            stripped = body.clone();
            stripped.remove("adaptive_sharing");
            body = &stripped;
        }
        let mut backend = self
            .backend
            .lock()
            .map_err(|_| PublicError::new(503, "action_unavailable"))?;
        let outcome = (|| -> Result<Json, PublicError> {
            backend.prepare(action, body)?;
            action.validate(body)?;
            let result = backend.execute_json(action, body)?;
            if let Some(enabled) = choice {
                backend.apply_first_consent_choice(enabled)?;
            }
            backend.publish()?;
            Ok(result)
        })();
        // The handler records WHY a non-DemoError failure (409) happened before answering.
        if let Err(error) = &outcome {
            if error.status == 409 {
                backend.record_failure(path, error);
            }
            backend.republish_after_failure(action);
        }
        outcome
    }
}
