// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's side of the run exchange as the chain steps use it (SE-L10-REPORTS-PORT): the reports
//! the Macs post, the announcements the owner makes, the seed coordinator's member list and the
//! clock-offset guard (`demo_chain.py:_exchange/_reports/_remote_members/_refuse_skewed_peers/
//! _publish_participants/_announce @ f4e366e9`). A host implements [`ReportsPort`] over its real
//! `RunExchange` and coordinator; nothing about how a step uses them lives in the host.
use crate::chain::{StepEnv, StepError};
use crate::machine::OwnerRunError;
use crate::state::RunState;
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;

/// One row of `coordinator.members()`.
#[derive(Clone, Debug, PartialEq)]
pub struct MemberRow {
    pub node_id: String,
    /// `member.get('lifecycle_state')`.
    pub lifecycle_state: Option<String>,
    /// `member.get('lease_expires_at', 0)`; a missing lease is expired.
    pub lease_expires_at: Option<f64>,
}

/// `exchange.announce(phase=, run_id=, deployment_id=, model=, slice_mode=, manifest_digest=, **extra)`.
#[derive(Clone, Debug, PartialEq)]
pub struct Announcement {
    pub phase: String,
    pub run_id: String,
    pub deployment_id: String,
    pub model_id: String,
    pub resolved_commit: String,
    pub slice_mode: bool,
    pub manifest_digest: String,
    /// `per_node=` (observe: `{node: {'bootstrap_offer_id': ...}}`), in the observation context's order.
    pub per_node: Option<Vec<(String, Value)>>,
}

/// `exchange.clock_offsets.exceeded(nodes)` with the tolerance it was measured against.
#[derive(Clone, Debug, PartialEq)]
pub struct ClockSkew {
    pub tolerance: f64,
    /// `{node: offset_seconds}`, in the order the exchange reports them.
    pub exceeded: Vec<(String, f64)>,
}

/// What the descriptors, observe and compile steps read from and write to the run exchange.
pub trait ReportsPort {
    /// `exchange.forget_reports()`.
    fn forget_reports(&mut self);
    /// `exchange.announce(...)`; `owner_exchange_unavailable` when no exchange is mounted.
    fn announce(&mut self, announcement: &Announcement) -> Result<(), StepError>;
    /// Work announcements must retain the run-wide limits as well as `per_node`.
    /// Existing hosts fail closed until they implement this additive seam; never
    /// fall back to `announce` and silently discard authority/resource limits.
    fn announce_work(
        &mut self,
        _announcement: &Announcement,
        _work_limits: &Value,
    ) -> Result<(), StepError> {
        Err(crate::owner_chain::unwired(
            "slices",
            "NP-SLICE1-slices",
            "SE-L10-EXCHANGE",
        ))
    }
    /// `exchange.reports()`: every report, keyed by node (the steps filter by run and deployment).
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError>;
    /// `app.authority.coordinator.members()`.
    fn members(&mut self) -> Vec<MemberRow>;
    /// `app.authority.member.node_id`.
    fn own_node_id(&mut self) -> Option<String>;
    /// `tuple(coordinator.member(node)[k] for k in _IDENTITY)` as a JSON array
    /// (`generation, incarnation, verification_key_digest, endpoint_id`).
    fn member_identity(&mut self, node: &str) -> Value;
    /// `exchange.clock_offsets` (`None` when the exchange keeps none: no peer is skewed).
    fn clock_skew(&mut self, nodes: &[String]) -> Result<Option<ClockSkew>, StepError>;
    /// `self.agent.tick()` (the local agent answers the owner's announcement in-process).
    fn tick_local(&mut self);
}

/// Python truthiness of a JSON value.
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `_reports(ctx)`: the exchange's reports for this run and deployment only.
pub fn run_reports(
    port: &mut dyn ReportsPort,
    state: &RunState,
) -> Result<BTreeMap<String, Value>, StepError> {
    Ok(port
        .reports()?
        .into_iter()
        .filter(|(_, r)| {
            r.get("run_id").and_then(Value::as_str) == Some(state.run_id.as_str())
                && r.get("deployment_id").and_then(Value::as_str)
                    == Some(state.deployment_id.as_str())
        })
        .collect())
}

/// `_remote_members()`: live joined members, not the owner's authority node or this Mac's contributor.
pub fn remote_members(port: &mut dyn ReportsPort, local: &str, wall: f64) -> Vec<String> {
    let own = port.own_node_id();
    let mut nodes: Vec<String> = port
        .members()
        .into_iter()
        .filter(|m| Some(&m.node_id) != own.as_ref() && m.node_id != local)
        .filter(|m| !matches!(m.lifecycle_state.as_deref(), Some("STOPPING" | "STOPPED")))
        .filter(|m| m.lease_expires_at.unwrap_or(0.0) > wall)
        .map(|m| m.node_id)
        .collect();
    nodes.sort();
    nodes
}

/// `_check_rollover_members(binding)` (`demo_chain.py:1117-1123`).
pub fn check_rollover_members(
    port: &mut dyn ReportsPort,
    local: &str,
    wall: f64,
    binding: &Value,
) -> Result<(), StepError> {
    let changed = || StepError::from(OwnerRunError::new("rollover_members_changed"));
    let members = binding
        .get("members")
        .and_then(Value::as_object)
        .ok_or_else(|| StepError::Exception {
            class: "KeyError".into(),
        })?;
    let mut want: Vec<String> = vec![local.to_owned()];
    want.extend(remote_members(port, local, wall));
    want.sort();
    want.dedup();
    let have: Vec<String> = members.keys().cloned().collect(); // object keys are sorted and unique
    if have != want {
        return Err(changed());
    }
    for (node, identity) in members {
        if &port.member_identity(node) != identity {
            return Err(changed());
        }
    }
    Ok(())
}

/// `_refuse_skewed_peers(ctx, nodes)`: a peer whose clock is beyond the tolerance would drop the owner's
/// short-TTL commands, so the route is refused here.
pub fn refuse_skewed_peers(port: &mut dyn ReportsPort, nodes: &[String]) -> Result<(), StepError> {
    let Some(skew) = port.clock_skew(nodes)? else {
        return Ok(());
    };
    let mut worst: Option<&(String, f64)> = None;
    for row in &skew.exceeded {
        if worst.is_none_or(|w| row.1.abs() > w.1.abs()) {
            worst = Some(row);
        }
    }
    let Some((node, seconds)) = worst else {
        return Ok(());
    };
    let tail: String = {
        let chars: Vec<char> = node.chars().collect();
        chars[chars.len().saturating_sub(8)..].iter().collect()
    };
    let message = format!(
        "The clock of the joined Mac …{tail} is {} s {} this Mac's, beyond the {} s the swarm tolerates. Turn on “Set time and date automatically” on both Macs (System Settings → General → Date & Time), then press Start again.",
        py_round(seconds.abs()),
        if *seconds > 0.0 { "ahead of" } else { "behind" },
        py_round(skew.tolerance)
    );
    Err(OwnerRunError::with("peer_clock_skew_exceeded", Some(&message), None).into())
}

/// Python 3 `round(x)` (banker's rounding to an int).
pub fn py_round(x: f64) -> i64 {
    let floor = x.floor();
    let diff = x - floor;
    let even = (floor as i64) % 2 == 0;
    if diff > 0.5 || (diff == 0.5 && !even) {
        floor as i64 + 1
    } else {
        floor as i64
    }
}

/// `_member_row(compile_state, node)`: the compiled resource plan's row for `node`.
pub fn member_row(compile_state: Option<&Value>, node: &str) -> Option<Value> {
    compile_state?
        .get("resource_plan")?
        .get("members")?
        .as_array()?
        .iter()
        .find(|m| m.get("node_id").and_then(Value::as_str) == Some(node))
        .cloned()
}

/// `_publish_participants(ctx)`: `ctx.result['participants']`.
pub fn publish_participants(
    env: &mut dyn StepEnv,
    port: &mut dyn ReportsPort,
    state: &RunState,
) -> Result<(), StepError> {
    let rows = participant_rows(port, state)?;
    env.set_result("participants", Value::Array(rows));
    Ok(())
}

/// The same publication snapshot, also used to total slice progress without an extra report read.
pub(crate) fn participant_rows(
    port: &mut dyn ReportsPort,
    state: &RunState,
) -> Result<Vec<Value>, StepError> {
    let reports = run_reports(port, state)?;
    let empty = Value::Object(Map::new());
    let rows: Vec<Value> = state
        .participants
        .iter()
        .map(|node| {
            let report = reports.get(node).unwrap_or(&empty);
            let get = |k: &str| report.get(k).cloned().unwrap_or(Value::Null);
            let is_local = state.local_node.as_deref() == Some(node.as_str());
            json!({
                "node_id": node,
                "label": if is_local { "This Mac (owner)" } else { "Joined Mac" },
                "state": get("state"),
                "detail": get("detail"),
                "progress": get("progress"),
                "layers": state.layers.get(node).map_or(Value::Null, |l| json!([l[0], l[1]])),
                "resources": member_row(state.compile_state.as_ref(), node).unwrap_or(Value::Null),
            })
        })
        .collect();
    Ok(rows)
}

/// `_announce(ctx, phase, **extra)`.
pub fn announce(
    port: &mut dyn ReportsPort,
    state: &RunState,
    phase: &str,
    per_node: Option<Vec<(String, Value)>>,
) -> Result<(), StepError> {
    let model = state.model.as_ref().ok_or_else(|| StepError::Exception {
        class: "KeyError".into(),
    })?;
    port.announce(&Announcement {
        phase: phase.to_owned(),
        run_id: state.run_id.clone(),
        deployment_id: state.deployment_id.clone(),
        model_id: model.model_id.clone(),
        resolved_commit: model.resolved_commit.clone(),
        slice_mode: model.slice_mode,
        manifest_digest: model.manifest_digest.clone(),
        per_node,
    })
}
