// SPDX-License-Identifier: AGPL-3.0-only
//! `StandingAcceptor` (`standing_consent.py:375-470 @ f4e366e9`): a joined Mac accepts each pending
//! owner work offer once, automatically, within the standing caps its person granted at join, and
//! records why it did not (`status().last`, which the member's work report names). With the
//! `standing_limits` (`:230-261`) share check and `check_work_preview` (`:284-326`) it uses.
//!
//! Preview, accept and the pre-run report are the member's own collaborators ([`WorkAcceptance`]);
//! nothing here starts a worker or writes consent. A reason that can change on a later tick
//! (membership, an unbound/withdrawn/missing grant, any `resource_*` gate) leaves the offer to be
//! retried; every other outcome handles the offer once.
use crate::preferences::standing_consent::{self, StandingError};
use budgets::{BudgetId, Entries};
use serde_json::{Map, Value, json};
use std::{collections::BTreeSet, path::PathBuf};

/// `_TRANSIENT`: reasons that can change on a later tick.
const TRANSIENT: [&str; 4] = [
    "membership_required",
    "standing_consent_withdrawn",
    "standing_consent_unbound",
    "standing_consent_missing",
];
/// `_PREVIEW_LIMIT_CAPS`: each preview limit and the cap that bounds it.
const PREVIEW_LIMIT_CAPS: [(&str, &str); 4] = [
    ("max_rss_bytes", "max_memory_bytes"),
    ("max_disk_bytes", "storage_bytes"),
    ("max_wall_seconds", "max_wall_seconds"),
    ("max_commands", "max_commands"),
];

/// `_PUBLIC_CODE`: `[a-z][a-z0-9_]{0,95}`.
fn public_code(code: &str) -> bool {
    let mut chars = code.chars();
    chars.next().is_some_and(|c| c.is_ascii_lowercase())
        && code.len() <= 96
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}

/// A failed collaborator call: what the source reads off the exception.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct AcceptFailure {
    /// `exc.public_code` (a failed native launch names its closed launch step).
    pub public: Option<String>,
    /// `exc.code` or, for a bare exception, its identifier text.
    pub code: Option<String>,
    /// `_retryable_staging_error`: a transient managed-control read/ack failure before any runtime,
    /// on an offer with a finite expiry; the offer is tried again on a later tick.
    pub retryable: bool,
    /// `error_http(exc, reason)`: the refused HTTP step, when the failure was one.
    pub error_http: Option<Value>,
}
impl AcceptFailure {
    pub fn code(code: &str) -> Self {
        Self {
            code: Some(code.to_owned()),
            ..Self::default()
        }
    }
    /// The recorded reason: a valid public code, else a valid code, else `standing_accept_failed`.
    fn reason(&self) -> String {
        let candidate = self
            .public
            .as_deref()
            .filter(|c| public_code(c))
            .or(self.code.as_deref());
        candidate
            .filter(|c| public_code(c))
            .unwrap_or("standing_accept_failed")
            .to_owned()
    }
}
impl From<StandingError> for AcceptFailure {
    fn from(error: StandingError) -> Self {
        Self::code(error.0)
    }
}

/// The member's own work-acceptance collaborators.
pub trait WorkAcceptance {
    /// `actor.pending_stage()`.
    fn pending_offer(&mut self) -> Result<Option<Value>, AcceptFailure>;
    /// `report('pre_run', offer)`: the member's capability report before a run.
    fn report(&mut self, purpose: &str, offer: &Value) -> Result<(), AcceptFailure>;
    /// `preview_work(actor, offer, stage_acknowledged=True, limits=, resource_policy=)`.
    fn preview(
        &mut self,
        offer: &Value,
        limits: &Value,
        policy: &Value,
    ) -> Result<Value, AcceptFailure>;
    /// `accept_work(actor, preview, work_acknowledged=True, limits=, resource_policy=)`.
    fn accept(
        &mut self,
        preview: &Value,
        limits: &Value,
        policy: &Value,
    ) -> Result<(), AcceptFailure>;
    /// `self._clock()`.
    fn clock(&mut self) -> f64;
}

fn seconds(id: BudgetId) -> u64 {
    id.span(Entries::ZERO).map_or(0, |s| s.millis() / 1000)
}

/// `standing_limits(grant, required=)`: the worker limits under the standing caps; with `required`
/// (this member's share-derived limits) exactly its share, which must fit the caps.
pub fn standing_limits(
    grant: &Map<String, Value>,
    required: Option<&Value>,
) -> Result<Value, StandingError> {
    let grant = Value::Object(grant.clone());
    let caps = &grant["caps"];
    let mut rss = caps["max_memory_bytes"].clone();
    let mut disk = caps["storage_bytes"].clone();
    let mut wall = caps["max_wall_seconds"].clone();
    if let Some(required) = required.filter(|r| r.is_object()) {
        if let Some(want) = required.get("max_wall_seconds") {
            let reserve = seconds(BudgetId::WorkerCleanupReserve);
            let maximum =
                seconds(BudgetId::PyMyceliumA13MacosStandingConsentMaxWallSecondsMaximumL102);
            let Some(w) = want
                .as_u64()
                .filter(|w| (reserve + 1..=maximum).contains(w))
            else {
                return Err(StandingError("member_wall_limit_invalid"));
            };
            if caps["max_wall_seconds"].as_u64().is_some_and(|cap| w > cap) {
                return Err(StandingError("member_wall_cap_below_share"));
            }
            wall = want.clone();
        }
        let positive = |key: &str| required.get(key).and_then(Value::as_u64).filter(|v| *v > 0);
        let (Some(need_rss), Some(need_disk)) =
            (positive("max_rss_bytes"), positive("max_disk_bytes"))
        else {
            return Err(StandingError("member_resources_invalid"));
        };
        if caps["max_memory_bytes"]
            .as_u64()
            .is_some_and(|cap| need_rss > cap)
        {
            return Err(StandingError("member_memory_cap_below_share"));
        }
        if caps["storage_bytes"]
            .as_u64()
            .is_some_and(|cap| need_disk > cap)
        {
            return Err(StandingError("member_storage_cap_below_share"));
        }
        (rss, disk) = (json!(need_rss), json!(need_disk));
    }
    let limits = json!({"max_rss_bytes": rss, "max_disk_bytes": disk, "max_wall_seconds": wall,
        "max_commands": caps["max_commands"]});
    // `WorkerLimits.__post_init__`.
    let reserve = seconds(BudgetId::WorkerCleanupReserve);
    let positive = PREVIEW_LIMIT_CAPS
        .iter()
        .all(|(k, _)| limits[k].as_u64().is_some_and(|v| v > 0));
    let wall_ok = limits["max_wall_seconds"].as_u64().is_some_and(|w| {
        w > reserve
            && w <= seconds(BudgetId::PyMyceliumA13MacosStandingConsentMaxWallSecondsMaximumL102)
    });
    if !positive || !wall_ok || limits["max_commands"].as_u64().is_none_or(|c| c > 10_000) {
        return Err(StandingError("worker_limits_invalid"));
    }
    Ok(limits)
}

/// `check_work_preview(grant, preview, binding=, policy=, resource_violation=)`: may this exact
/// work preview be accepted without a click? `None` = allowed, else one closed reason.
pub fn check_work_preview(
    grant: Option<&Map<String, Value>>,
    preview: &Value,
    binding: Option<&Value>,
    policy: &Value,
    resource_violation: Option<&str>,
) -> Option<String> {
    let deny = |reason: &str| Some(reason.to_owned());
    let Some(grant) = grant.map(|g| Value::Object(g.clone())) else {
        return deny("standing_consent_missing");
    };
    if grant["state"] == "withdrawn" {
        return deny("standing_consent_withdrawn");
    }
    if grant["state"] != "granted" || grant["binding"].is_null() {
        return deny("standing_consent_unbound");
    }
    let Some(binding) = binding.filter(|b| !b.is_null()) else {
        return deny("membership_required");
    };
    if *binding != grant["binding"] {
        return deny("standing_consent_owner_mismatch");
    }
    let limits = preview.get("limits").and_then(Value::as_object);
    let valid = preview["protocol"] == "mycelium.a13.work_preview.v1"
        && preview["route_ready"] == Value::Bool(false)
        && limits.is_some_and(|l| {
            l.len() == PREVIEW_LIMIT_CAPS.len()
                && PREVIEW_LIMIT_CAPS
                    .iter()
                    .all(|(k, _)| l.get(*k).and_then(Value::as_i64).is_some_and(|v| v > 0))
        })
        && preview["consent_digest"].is_string();
    let Some(limits) = limits.filter(|_| valid) else {
        return deny("standing_preview_invalid");
    };
    if preview["node_id"] != binding["node_id"] {
        return deny("standing_offer_not_for_this_member");
    }
    let over = PREVIEW_LIMIT_CAPS.iter().any(|(k, cap)| {
        let cap = grant["caps"][*cap].as_i64().unwrap_or(i64::MAX);
        limits[*k].as_i64().is_some_and(|v| v > cap)
    });
    if over {
        return deny("standing_preview_exceeds_caps");
    }
    let offered = match preview.get("resource_policy") {
        Some(Value::Object(p)) => Value::Object(p.clone()),
        _ => json!({}),
    };
    if offered != *policy {
        return deny("standing_preview_policy_changed");
    }
    resource_violation
        .filter(|v| !v.is_empty())
        .map(str::to_owned)
}

/// `limits_for(offer)`: this member's share-derived worker limits for the offer's run, if any.
pub type LimitsFor = Box<dyn Fn(&Value) -> Option<Value> + Send>;

/// `StandingAcceptor`.
pub struct StandingAcceptor {
    root: PathBuf,
    limits_for: Option<LimitsFor>,
    handled: BTreeSet<String>,
    last: Option<Value>,
}
impl StandingAcceptor {
    pub fn new(root: PathBuf, limits_for: Option<LimitsFor>) -> Self {
        Self {
            root,
            limits_for,
            handled: BTreeSet::new(),
            last: None,
        }
    }
    /// `status()`.
    pub fn status(&self) -> Result<Value, StandingError> {
        let grant = standing_consent::load(&self.root)?;
        Ok(json!({
            "state": grant.as_ref().map_or(json!("absent"), |g| g["state"].clone()),
            "caps": grant.as_ref().map_or(Value::Null, |g| g["caps"].clone()),
            "disclosure_digest": standing_consent::DISCLOSURE_DIGEST,
            "last": self.last.clone().unwrap_or(Value::Null),
        }))
    }
    fn record(&mut self, id: &str, state: &str, reason: Option<&str>, handled: bool) -> Value {
        if handled {
            self.handled.insert(id.to_owned());
        }
        let last = json!({"offer_id": id, "state": state, "reason": reason});
        self.last = Some(last.clone());
        last
    }
    /// A refusal is handled once unless its reason can change on a later tick.
    fn refuse(&mut self, id: &str, reason: &str) -> Value {
        let retry = TRANSIENT.contains(&reason) || reason.starts_with("resource_");
        self.record(id, "refused", Some(reason), !retry)
    }
    /// `_expired(offer)`: only a known, passed expiry expires an offer here.
    fn expired(offer: &Value, host: &mut dyn WorkAcceptance) -> bool {
        offer["expires_at"]
            .as_f64()
            .filter(|t| t.is_finite())
            .is_some_and(|t| host.clock() >= t)
    }
    /// `tick(actor, policy=, binding=, resource_check=)`: the outcome for the pending offer, if any.
    pub fn tick(
        &mut self,
        host: &mut dyn WorkAcceptance,
        policy: &Value,
        binding: Option<&Value>,
        resource_check: &mut dyn FnMut() -> Option<String>,
    ) -> Result<Option<Value>, AcceptFailure> {
        let Some(grant) = standing_consent::load(&self.root)? else {
            return Ok(None);
        };
        let Some(offer) = host.pending_offer()?.filter(Value::is_object) else {
            return Ok(None);
        };
        let Some(id) = offer["offer_id"]
            .as_str()
            .filter(|id| !self.handled.contains(*id))
            .map(str::to_owned)
        else {
            return Ok(None);
        };
        if Self::expired(&offer, host) {
            return Ok(Some(self.record(
                &id,
                "refused",
                Some("offer_expired"),
                true,
            )));
        }
        let violation = resource_check();
        let reason = protocol::consent::standing_admission(
            &Value::Object(grant.clone()),
            binding.unwrap_or(&Value::Null),
            &offer,
            violation.as_deref(),
        )
        .map_err(|e| AcceptFailure::code(e.0))?;
        if let Some(reason) = reason {
            return Ok(Some(self.refuse(&id, &reason)));
        }
        let required = self.limits_for.as_ref().and_then(|f| f(&offer));
        let limits = match standing_limits(&grant, required.as_ref()) {
            Ok(limits) => limits,
            Err(error) => return Ok(Some(self.record(&id, "refused", Some(error.0), true))),
        };
        let attempt = (|| -> Result<Option<Value>, AcceptFailure> {
            host.report("pre_run", &offer)?;
            if Self::expired(&offer, host) {
                return Ok(Some(self.record(
                    &id,
                    "refused",
                    Some("offer_expired"),
                    true,
                )));
            }
            let preview = host.preview(&offer, &limits, policy)?;
            let current = standing_consent::load(&self.root)?;
            let violation = resource_check();
            if let Some(reason) = check_work_preview(
                current.as_ref(),
                &preview,
                binding,
                policy,
                violation.as_deref(),
            ) {
                return Ok(Some(self.refuse(&id, &reason)));
            }
            if Self::expired(&offer, host) {
                return Ok(Some(self.record(
                    &id,
                    "refused",
                    Some("offer_expired"),
                    true,
                )));
            }
            host.accept(&preview, &limits, policy)?;
            Ok(None)
        })();
        match attempt {
            Ok(Some(outcome)) => Ok(Some(outcome)),
            Ok(None) => Ok(Some(self.record(&id, "accepted", None, true))),
            Err(failure) => {
                let reason = failure.reason();
                let mut last = self.record(&id, "failed", Some(&reason), !failure.retryable);
                if let Some(http) = failure.error_http {
                    last["error_http"] = http;
                    self.last = Some(last.clone());
                }
                Ok(Some(last))
            }
        }
    }
}
