// SPDX-License-Identifier: AGPL-3.0-only
//! Signed demo-run route callbacks (`run_exchange.py:252-281 @ f4e366e9`).
//! The seed mount MUST hold its current-member authority guard while calling `dispatch_guarded`.
//! This adapter borrows the existing coordinator signer; it creates no identity, session or journal.
use crate::{installation_descriptor::is_segment, run_exchange::RunExchange};
use protocol::{ContractError, identity::Identity};
use serde_json::Value;

pub const READ: &str = "mycelium.a13_demo_run_read.v1";
pub const REPORT: &str = "mycelium.a13_demo_run_report.v1";
pub const RESPONSE: &str = "mycelium.a13_demo_run_state.v1";
pub const PATH_READ: &str = "/managed-control/demo-run/read";
pub const PATH_REPORT: &str = "/managed-control/demo-run/report";
pub const DISCOVERY: &str = "demo-run-discovery";

/// All authority is borrowed from an existing seed; member is a guarded current snapshot, not request data.
pub struct SignedRoutes<'a> {
    pub exchange: &'a RunExchange,
    pub signer: &'a Identity,
    pub swarm_id: &'a str,
    pub owner_node_id: &'a str,
    /// Existing PeerClockOffsets collaborator; called only after authentication/report validation.
    pub observe_clock: &'a dyn Fn(&str, f64, Option<f64>),
    /// Source response initializer (`managed_control.py:1006`); the host supplies the registered
    /// ManagedControlExchangeResponseTtl allowance, not a cap on arbitrary received envelopes.
    pub response_ttl: budgets::Span,
}
const BINDING: [&str; 7] = [
    "target_node_id",
    "target_endpoint_id",
    "target_key_digest",
    "membership_generation",
    "member_incarnation",
    "run_id",
    "deployment_id",
];
fn request_node<'a>(
    path: &str,
    envelope: &'a Value,
    swarm_id: &str,
) -> Result<&'a str, ContractError> {
    let protocol = match path {
        PATH_READ => READ,
        PATH_REPORT => REPORT,
        _ => return Err(ContractError("managed_control_route_unknown")),
    };
    let check = |ok: bool, code: &'static str| if ok { Ok(()) } else { Err(ContractError(code)) };
    let statement = envelope
        .get("statement")
        .and_then(Value::as_object)
        .ok_or(ContractError("managed_control_envelope_invalid"))?;
    let common = [
        "protocol",
        "request_id",
        "swarm_id",
        "sequence",
        "nonce",
        "issued_at",
        "expires_at",
    ];

    check(
        statement.len() == common.len() + BINDING.len() + usize::from(protocol == REPORT)
            && common
                .iter()
                .chain(BINDING.iter())
                .all(|k| statement.contains_key(*k))
            && (protocol != REPORT || statement.contains_key("report")),
        "managed_control_fields_invalid",
    )?;
    for field in ["request_id", "swarm_id", "nonce"] {
        check(
            statement[field].as_str().is_some_and(is_segment),
            "managed_control_identifier_invalid",
        )?;
    }
    check(
        statement["sequence"].as_u64().is_some_and(|n| n >= 1),
        "managed_control_sequence_invalid",
    )?;
    for field in [
        "target_node_id",
        "target_endpoint_id",
        "member_incarnation",
        "run_id",
        "deployment_id",
    ] {
        check(
            statement[field].as_str().is_some_and(is_segment),
            "managed_control_member_binding_invalid",
        )?;
    }
    check(
        statement["membership_generation"]
            .as_u64()
            .is_some_and(|n| n >= 1)
            && statement["target_key_digest"].as_str().is_some_and(|s| {
                s.strip_prefix("sha256:").is_some_and(|h| {
                    h.len() == 64
                        && h.bytes()
                            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                })
            }),
        "managed_control_member_binding_invalid",
    )?;
    check(
        statement["swarm_id"] == swarm_id,
        "managed_control_authority_binding_invalid",
    )?;
    statement["target_node_id"]
        .as_str()
        .ok_or(ContractError("managed_control_member_binding_invalid"))
}
fn check(ok: bool, code: &'static str) -> Result<(), ContractError> {
    if ok { Ok(()) } else { Err(ContractError(code)) }
}
impl SignedRoutes<'_> {
    fn verify_guarded(
        &self,
        path: &str,
        envelope: &Value,
        member: &Value,
        now: f64,
    ) -> Result<Value, ContractError> {
        eprintln!("l10_verify:{path}");
        use budgets::{BudgetId, Entries};
        request_node(path, envelope, self.swarm_id)?;
        let protocol = if path == PATH_READ { READ } else { REPORT };
        let statement = envelope["statement"]
            .as_object()
            .ok_or(ContractError("managed_control_envelope_invalid"))?;
        check(
            matches!(
                member["peer_class"].as_str(),
                Some("mac_mlx_iroh" | "windows_native_iroh" | "linux_native_iroh")
            ) && matches!(
                member["lifecycle_state"].as_str(),
                Some("NEW" | "CONFIGURED" | "RUNNING" | "DRAINING" | "STOPPING" | "STOPPED")
            ) && member["lease_expires_at"]
                .as_f64()
                .is_some_and(|expires| now < expires),
            "managed_control_member_stale",
        )?;
        for (field, request) in [
            ("node_id", "target_node_id"),
            ("endpoint_id", "target_endpoint_id"),
            ("verification_key_digest", "target_key_digest"),
            ("generation", "membership_generation"),
            ("incarnation", "member_incarnation"),
        ] {
            check(
                member.get(field) == statement.get(request),
                "managed_control_member_stale",
            )?;
        }
        let seconds = |id: BudgetId| {
            id.span(Entries::ZERO)
                .map(|v| v.millis() as f64 / 1000.0)
                .map_err(|_| ContractError("managed_control_configuration_invalid"))
        };
        let verified = protocol::managed::verify(
            envelope,
            protocol,
            &envelope["verification_key"],
            member["endpoint_id"]
                .as_str()
                .ok_or(ContractError("managed_control_member_stale"))?,
            now,
            protocol::TimeLimits {
                max_ttl_seconds: seconds(BudgetId::ControlTtl)?,
                clock_skew_seconds: seconds(BudgetId::ControlClockSkew)?,
            },
        )?;
        check(
            envelope["verification_key"]["verification_key_digest"]
                == member["verification_key_digest"],
            "managed_control_key_pin_mismatch",
        )?;
        check(
            verified["run_id"] == DISCOVERY && verified["deployment_id"] == DISCOVERY,
            "demo_request_invalid",
        )?;
        if path == PATH_REPORT {
            crate::run_exchange::validate_report(&verified["report"])
                .map_err(|_| ContractError("demo_report_invalid"))?;
        }
        Ok(verified)
    }
    /// Direct callback contract: providers/clocks supplied by the caller must be bounded and non-reentrant.
    /// The mounted SeedRoutes adapter prepares external hints outside authority and revalidates afterward.
    pub fn dispatch_guarded(
        &self,
        path: &str,
        envelope: &Value,
        member: &Value,
        now: f64,
    ) -> Result<Value, ContractError> {
        // Log-only: a member that never calls these routes and a member whose call is refused look
        // identical from the owner's side ("N of M Macs reported"), so name which one happened.
        let verified = match self.verify_guarded(path, envelope, member, now) {
            Ok(verified) => verified,
            Err(error) => {
                eprintln!("l10_route_reject:{path}:{}", error.0);
                return Err(error);
            }
        };
        let outcome = self.complete_guarded(path, verified, now, None, None);
        match &outcome {
            Ok(_) => eprintln!("l10_route_ok:{path}"),
            Err(error) => eprintln!("l10_route_reject:{path}:{}", error.0),
        }
        outcome
    }
    fn complete_guarded(
        &self,
        path: &str,
        verified: Value,
        now: f64,
        prepared: Option<Value>,
        received_at: Option<f64>,
    ) -> Result<Value, ContractError> {
        eprintln!("l10_complete:{path}");
        use budgets::{BudgetId, Entries};
        use serde_json::json;
        let seconds = |id: BudgetId| {
            id.span(Entries::ZERO)
                .map(|v| v.millis() as f64 / 1000.0)
                .map_err(|_| ContractError("managed_control_configuration_invalid"))
        };
        let node = verified["target_node_id"]
            .as_str()
            .ok_or(ContractError("managed_control_member_binding_invalid"))?;
        let signed_at = verified["issued_at"]
            .as_f64()
            .ok_or(ContractError("managed_control_time_invalid"))?;
        if path == PATH_REPORT {
            let report = crate::run_exchange::validate_report(&verified["report"])
                .map_err(|_| ContractError("demo_report_invalid"))?;
            (self.observe_clock)(node, signed_at, report["clock_rtt_seconds"].as_f64());
            self.exchange
                .submit_remote_verified_at(node, &report, received_at)
                .map_err(|_| ContractError("demo_report_invalid"))?;
        } else {
            (self.observe_clock)(node, signed_at, None);
        }
        let announcement =
            prepared.unwrap_or_else(|| self.exchange.announcement(Some(node)).to_value());
        // Frozen wire bound: run_exchange.py:63,261, not an operational resource budget.
        check(
            crate::preferences::dumps_sorted(&announcement).len() <= 768 * 1024,
            "demo_announcement_too_large",
        )?;
        let mut response = json!({"protocol":RESPONSE,"request_id":verified["request_id"],"swarm_id":self.swarm_id,
            "owner_node_id":self.owner_node_id,"owner_endpoint_id":self.signer.endpoint_id(),"issued_at":now,
            "expires_at":now+(self.response_ttl.millis() as f64/1000.0).min(seconds(BudgetId::ControlTtl)?),"announcement":announcement});
        for field in BINDING {
            response[field] = verified[field].clone();
        }
        let signature = self
            .signer
            .sign(&response)
            .map_err(|_| ContractError("managed_control_signer_invalid"))?;
        eprintln!("l10_complete_ok:{path}");
        Ok(
            json!({"protocol":protocol::managed::SIGNED_ENVELOPE_PROTOCOL,"statement":response,"signature":signature,"verification_key":self.signer.public_key_record()}),
        )
    }
}

/// Existing peer-clock diagnostic collaborator; always invoked after releasing seed authority.
pub type ClockObserver = std::sync::Arc<dyn Fn(&str, f64, Option<f64>) + Send + Sync>;
/// API-owned mount, borrowing seed identity and current-member authority, never a parallel journal.
pub struct SeedRoutes {
    exchange: std::sync::Arc<RunExchange>,
    observe_clock: ClockObserver,
    response_ttl: budgets::Span,
}
impl SeedRoutes {
    pub fn new(
        exchange: std::sync::Arc<RunExchange>,
        observe_clock: ClockObserver,
        response_ttl: budgets::Span,
    ) -> Self {
        Self {
            exchange,
            observe_clock,
            response_ttl,
        }
    }
}
impl seed::http::RouteExtension for SeedRoutes {
    fn post(
        &self,
        coordinator: &seed::Coordinator,
        path: &str,
        envelope: &Value,
    ) -> Option<Result<Value, seed::SeedError>> {
        if !matches!(path, PATH_READ | PATH_REPORT) {
            return None;
        }
        let observed = std::cell::RefCell::new(None);
        let collect = |node: &str, signed: f64, rtt: Option<f64>| {
            *observed.borrow_mut() = Some((node.to_owned(), signed, rtt));
        };
        let routes = SignedRoutes {
            exchange: &self.exchange,
            signer: coordinator.signer(),
            swarm_id: coordinator.swarm_id(),
            owner_node_id: coordinator.seed_node_id(),
            observe_clock: &collect,
            response_ttl: self.response_ttl,
        };
        let result = (|| {
            let node = request_node(path, envelope, coordinator.swarm_id())
                .map_err(seed::SeedError::from)?;
            // Authenticate before invoking an external hint provider, but retain no authority snapshot.
            coordinator
                .with_current_member(node, |member, now| {
                    Ok(routes.verify_guarded(path, envelope, member, now))
                })
                .map_err(|_| seed::SeedError::new("managed_control_member_stale"))??;
            let prepared = self.exchange.announcement(Some(node)).to_value();
            // Recheck time and durable current membership after preparation, holding authority through
            // report commit and response signing. No observer, provider or exchange clock runs here.
            coordinator
                .with_current_member(node, |member, now| {
                    Ok(routes
                        .verify_guarded(path, envelope, member, now)
                        .and_then(|verified| {
                            routes.complete_guarded(path, verified, now, Some(prepared), Some(now))
                        }))
                })
                .map_err(|_| seed::SeedError::new("managed_control_member_stale"))?
                .map_err(seed::SeedError::from)
        })();
        if let Some((node, signed, rtt)) = observed.into_inner() {
            (self.observe_clock)(&node, signed, rtt);
        }
        Some(result)
    }
}
