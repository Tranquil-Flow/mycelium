// SPDX-License-Identifier: AGPL-3.0-only
//! Member side of the signed demo-run exchange (`RunExchangeClient`, `run_exchange.py:284-321 @
//! f4e366e9`): a joined Mac reads the owner's announcement and posts its own report, each as a
//! statement signed by its enrolled key, answered by a statement signed by the pinned seed key.
//! It also carries the member's other signed target-side requests (managed control, staging) to
//! the same seed (`ManagedControlClient._request` / `_verified_response`).
use crate::run_exchange_routes::{DISCOVERY, PATH_READ, PATH_REPORT, READ, REPORT, RESPONSE};
use crate::seed::SeedClient;
use budgets::{BudgetId, Entries};
use protocol::identity::Identity;
use serde_json::{Map, Value, json};
use std::sync::Arc;

/// A closed code (the oracle's `ManagedControlError.code` / `SeedHTTPError.code`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ClientError(pub String);
impl ClientError {
    fn new(code: &str) -> Self {
        Self(code.to_owned())
    }
}

/// The member binding fields every signed target statement carries.
pub const BINDING: [&str; 5] = [
    "target_node_id",
    "target_endpoint_id",
    "target_key_digest",
    "membership_generation",
    "member_incarnation",
];

/// `MAX_CLOCK_RTT_SECONDS` (`run_exchange.py:60`): a frozen wire bound of the report schema.
const MAX_CLOCK_RTT_SECONDS: f64 = 60.0;

/// This member as the seed knows it (`staging_control_snapshot()`).
#[derive(Clone, Debug)]
pub struct Membership {
    pub swarm_id: String,
    pub owner_node_id: String,
    /// The pinned seed (owner) verification key record.
    pub owner_key: Value,
    pub node_id: String,
    pub generation: u64,
    pub incarnation: String,
}

pub struct ExchangeClient {
    seed: SeedClient,
    signer: Arc<Identity>,
    member: Membership,
    clock: Box<dyn Fn() -> f64 + Send + Sync>,
    entropy: Box<dyn Fn() -> Option<String> + Send + Sync>,
    last_rtt: Option<f64>,
}

fn seconds(id: BudgetId) -> Result<f64, ClientError> {
    id.span(Entries::ZERO)
        .map(|s| s.millis() as f64 / 1000.0)
        .map_err(|_| ClientError::new("budget_unavailable"))
}

impl ExchangeClient {
    pub fn new(
        seed: SeedClient,
        signer: Arc<Identity>,
        member: Membership,
        clock: Box<dyn Fn() -> f64 + Send + Sync>,
        entropy: Box<dyn Fn() -> Option<String> + Send + Sync>,
    ) -> Self {
        Self {
            seed,
            signer,
            member,
            clock,
            entropy,
            last_rtt: None,
        }
    }
    pub fn member(&self) -> &Membership {
        &self.member
    }
    pub fn signer(&self) -> &Identity {
        &self.signer
    }
    pub fn now(&self) -> f64 {
        (self.clock)()
    }
    pub fn nonce(&self) -> Result<String, ClientError> {
        (self.entropy)().ok_or_else(|| ClientError::new("managed_control_entropy_unavailable"))
    }

    /// `_binding_from_member(member, run_id, deployment_id)`.
    pub fn binding(&self, run_id: &str, deployment_id: &str) -> Map<String, Value> {
        let m = &self.member;
        let mut b = Map::new();
        b.insert("target_node_id".into(), m.node_id.clone().into());
        b.insert(
            "target_endpoint_id".into(),
            self.signer.endpoint_id().to_owned().into(),
        );
        b.insert(
            "target_key_digest".into(),
            self.signer.verification_key_digest().into(),
        );
        b.insert("membership_generation".into(), m.generation.into());
        b.insert("member_incarnation".into(), m.incarnation.clone().into());
        b.insert("run_id".into(), run_id.into());
        b.insert("deployment_id".into(), deployment_id.into());
        b
    }

    /// `_signed_outbound(statement)`.
    pub fn sign(&self, statement: &Value) -> Result<Value, ClientError> {
        let signature = self
            .signer
            .sign(statement)
            .map_err(|_| ClientError::new("managed_control_sign_failed"))?;
        Ok(
            json!({"protocol": protocol::managed::SIGNED_ENVELOPE_PROTOCOL, "signature": signature,
            "verification_key": self.signer.public_key_record(), "statement": statement}),
        )
    }

    /// `_request(path, envelope)`: the raw answer (verification is the caller's).
    pub fn post(&self, path: &str, envelope: &Value) -> Result<Value, ClientError> {
        self.seed
            .post(path, envelope)
            .map_err(|e| ClientError(seed_code(&e.code, e.status)))
    }

    /// `_verified_response(...)`: the owner's signed answer to `request_id`, bound to this member.
    pub fn verified(
        &self,
        response: &Value,
        protocol: &str,
        request_id: &str,
        run_id: &str,
        deployment_id: &str,
    ) -> Result<Value, ClientError> {
        let invalid = || ClientError::new("managed_control_response_invalid");
        let endpoint = response["statement"]["owner_endpoint_id"]
            .as_str()
            .ok_or_else(invalid)?;
        let statement = protocol::managed::verify(
            response,
            protocol,
            &self.member.owner_key,
            endpoint,
            self.now(),
            protocol::TimeLimits {
                max_ttl_seconds: seconds(BudgetId::ControlTtl)?,
                clock_skew_seconds: seconds(BudgetId::ControlClockSkew)?,
            },
        )
        .map_err(|e| ClientError::new(e.0))?;
        if statement["request_id"] != request_id
            || statement["swarm_id"] != self.member.swarm_id.as_str()
            || statement["owner_node_id"] != self.member.owner_node_id.as_str()
            || self
                .binding(run_id, deployment_id)
                .iter()
                .any(|(k, v)| statement[k] != *v)
        {
            return Err(invalid());
        }
        Ok(statement)
    }

    fn call(&mut self, path: &str, protocol: &str, extra: Value) -> Result<Value, ClientError> {
        let now = self.now();
        let request_id = self.nonce()?;
        let mut statement = Value::Object(self.binding(DISCOVERY, DISCOVERY));
        let ttl = seconds(BudgetId::ControlTtl)?;
        for (k, v) in json!({"protocol": protocol, "swarm_id": self.member.swarm_id,
            "request_id": request_id, "sequence": 1, "nonce": self.nonce()?, "issued_at": now,
            "expires_at": now + ttl})
        .as_object()
        .into_iter()
        .flatten()
        .chain(extra.as_object().into_iter().flatten())
        {
            statement[k] = v.clone();
        }
        let response = self.post(path, &self.sign(&statement)?)?;
        self.last_rtt = Some((self.now() - now).max(0.0));
        let result = self.verified(&response, RESPONSE, &request_id, DISCOVERY, DISCOVERY)?;
        let fields = 8 + BINDING.len() + 2;
        let announcement = &result["announcement"];
        if result.as_object().is_none_or(|o| o.len() != fields)
            || announcement["protocol"] != crate::run_exchange::ANNOUNCEMENT
            || !announcement["phase"]
                .as_str()
                .is_some_and(|p| crate::run_exchange::PHASES.contains(&p))
        {
            return Err(ClientError::new("demo_announcement_invalid"));
        }
        Ok(announcement.clone())
    }

    /// `read()`.
    pub fn read(&mut self) -> Result<Value, ClientError> {
        self.call(PATH_READ, READ, json!({}))
    }

    /// `report(report)`: the report, with this Mac's last measured round trip when it is bounded.
    pub fn report(&mut self, report: &Value) -> Result<Value, ClientError> {
        let mut report = report.clone();
        if let Some(rtt) = self.last_rtt
            && report.get("clock_rtt_seconds").is_none()
            && rtt <= MAX_CLOCK_RTT_SECONDS
        {
            report["clock_rtt_seconds"] = json!((rtt * 10_000.0).round() / 10_000.0);
        }
        let report = crate::run_exchange::validate_report(&report)
            .map_err(|_| ClientError::new("demo_report_invalid"))?;
        eprintln!("l10_member_report_sending");
        let outcome = self.call(PATH_REPORT, REPORT, json!({"report": report}));
        match &outcome {
            Ok(_) => eprintln!("l10_member_report_ok"),
            Err(error) => eprintln!("l10_member_report_failed:{error:?}"),
        }
        outcome
    }
}

/// `ManagedControlClient._request`'s failure mapping: a transport failure is unreachable, a
/// closed remote code is kept.
fn seed_code(code: &str, status: Option<u16>) -> String {
    match (code, status) {
        ("seed_http_unreachable", None) => "managed_control_unreachable".into(),
        ("seed_http_remote_error" | "seed_http_internal_error", _) => {
            // The wire code a caller sees is deliberately generic; the owner's log is not. Without
            // this, a step-7 failure read only `managed_control_internal_error` and the seed's own
            // account of the fault was lost between two processes.
            eprintln!("l11_seed_refusal_masked:{code}:status={status:?}");
            "managed_control_internal_error".into()
        }
        _ => code.to_owned(),
    }
}
