// SPDX-License-Identifier: AGPL-3.0-only
//! `_step_slices` (`demo_chain.py:852-888 @ f4e366e9`). This step announces the
//! compiled, source-bound plans and waits for verified member reports. It does
//! not fetch bytes or manufacture a plan, publication receipt or cleanup proof.
use super::exchange::{
    Announcement, ReportsPort, member_row, participant_rows, run_reports, truthy,
};
use crate::chain::{StepEnv, StepError, wait_with};
use crate::state::RunState;
use budgets::{BudgetId, Entries};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;

/// Reads the prepared workflow's plans and the current owner resource policy.
/// Plans are opaque source-bound documents, not digests reconstructed by the step.
pub trait SlicesPort {
    fn node_slice_plans(&mut self) -> Result<Option<BTreeMap<String, Value>>, StepError>;
    /// `read_resource_policy(root).get('storage_bytes')`. Read afresh on each call:
    /// the oracle reads policy twice. None denotes a missing key, not JSON null.
    fn storage_policy(&mut self) -> Result<Option<Value>, StepError>;
}

/// Additive borrow bundle, without adding fields to existing host Ports literals.
pub struct SlicesPorts<'a> {
    pub reports: &'a mut dyn ReportsPort,
    pub slices: &'a mut dyn SlicesPort,
}

fn exception(class: &str) -> StepError {
    StepError::Exception {
        class: class.into(),
    }
}
fn required<'a>(value: &'a Value, key: &str) -> Result<&'a Value, StepError> {
    value.get(key).ok_or_else(|| exception("KeyError"))
}
fn integer(value: &Value) -> Result<i128, StepError> {
    planner::layer_planner::contracts::py_int(value).map_err(|e| {
        exception(if e.kind == planner::py::Kind::Value {
            "ValueError"
        } else {
            "TypeError"
        })
    })
}
fn number(value: i128) -> Result<Value, StepError> {
    serde_json::to_value(value).map_err(|_| exception("OverflowError"))
}
fn seconds(id: BudgetId) -> f64 {
    id.span(Entries::ZERO)
        .map_or(f64::NAN, |s| s.millis() as f64 / 1000.0)
}
fn commands() -> Result<u64, StepError> {
    BudgetId::PyFinalMyceliumA13MacosDemoChainMaxCommandsL220C12
        .count()
        .map(|n| n.get())
        .map_err(|_| exception("ValueError"))
}
fn work_limits(proposal: &Value, storage: Option<&Value>) -> Result<Value, StepError> {
    let rss = integer(required(proposal, "max_combined_rss_bytes")?)?.div_euclid(2);
    let disk = integer(required(proposal, "max_additional_disk_bytes")?)?.div_euclid(2);
    let storage = integer(storage.unwrap_or(&json!(0)))?;
    let storage = if storage == 0 { 1 } else { storage };
    let wall = integer(required(proposal, "max_wall_seconds")?)?
        .min(seconds(BudgetId::PyUpperMyceliumA13MacosDemoChainL219C12) as i128);
    Ok(
        json!({"max_rss_bytes":number(rss)?,"max_disk_bytes":number(disk.min(storage))?,
        "max_wall_seconds":number(wall)?,"max_commands":commands()?}),
    )
}
fn member_limits(row: &Value, storage: Option<&Value>) -> Result<Value, StepError> {
    let mut disk = integer(required(row, "max_disk_bytes")?)?;
    if let Some(cap) = storage.and_then(planner::py::int).filter(|n| *n > 0) {
        disk = disk.min(cap);
    }
    Ok(
        json!({"max_rss_bytes":number(integer(required(row,"max_rss_bytes")?)?)?,
        "max_disk_bytes":number(disk)?,"max_commands":commands()?}),
    )
}
/// Public projection only; never forward other Macs' RAM/disk or private host facts.
fn public_resources(row: &Value) -> Result<Value, StepError> {
    const FIELDS: [&str; 11] = [
        "node_id",
        "start",
        "end",
        "stage_source_bytes",
        "resident_bytes",
        "kv_bytes",
        "load_peak_bytes",
        "max_rss_bytes",
        "max_disk_bytes",
        "disk_footprint_bytes",
        "basis",
    ];
    let mut out = Map::new();
    for k in FIELDS {
        out.insert(k.into(), required(row, k)?.clone());
    }
    Ok(Value::Object(out))
}

/// The no-cap wording of `model_resources.refusal_message`: compilation's known-cap
/// wording is a different callsite. Reuse the already compiled row, never re-plan.
pub(crate) fn resource_refusal(
    code: &str,
    row: &Value,
    state: &RunState,
) -> Result<String, StepError> {
    let who = row["node_id"].as_str().map_or_else(
        || "A Mac".into(),
        |n| format!("The Mac {}", n.chars().take(12).collect::<String>()),
    );
    let model = state
        .model
        .as_ref()
        .map_or("the selected model", |m| m.prepared.model.label.as_str());
    let first = integer(required(row, "start")?)? + 1;
    let last = required(row, "end")?;
    let memory = code == "member_memory_cap_below_share";
    let need = integer(required(
        row,
        if memory {
            "max_rss_bytes"
        } else {
            "max_disk_bytes"
        },
    )?)? as f64
        / 1e9;
    let what = if memory { "memory per run" } else { "storage" };
    let fix = if memory {
        "memory limit (rejoin with a larger limit)"
    } else {
        "storage limit"
    };
    Ok(format!(
        "{who} refused its share of {model} (layers {first}-{last}): it needs {need:.1} GB of {what}, more than that Mac agreed to. Raise that Mac's {fix}, add a Mac, or choose a smaller model."
    ))
}

/// Step 6's progress once a force-sliced run's owner pushed every Mac its own stage shard (B11 b).
pub const SHARDS_PUSHED: &str = "stage shards pushed: every Mac holds only its own layers";

pub fn run_slices_step(
    env: &mut dyn StepEnv,
    reports: &mut dyn ReportsPort,
    slices: &mut dyn SlicesPort,
    state: &mut RunState,
    interval: f64,
) -> Result<(), StepError> {
    let plans = slices.node_slice_plans()?;
    let storage = slices.storage_policy()?;
    let proposal = state
        .proposal
        .as_ref()
        .ok_or_else(|| exception("KeyError"))?;
    let mut limits = work_limits(proposal, storage.as_ref())?;
    state.work_limits = Some(limits.clone());
    let reuse = state
        .compile_state
        .as_ref()
        .and_then(|c| c.get("artifact_reuse_key"))
        .filter(|v| truthy(v));
    let mut per_node = Map::new();
    if let Some(plans) = &plans {
        for (node, plan) in plans {
            let mut entry = json!({"node_slice_plan":plan});
            if let Some(reuse) = reuse {
                entry["artifact_reuse_key"] = reuse.clone();
            }
            per_node.insert(node.clone(), entry);
        }
    }
    let storage = slices.storage_policy()?;
    for node in &state.participants {
        let row = member_row(state.compile_state.as_ref(), node);
        let entry = per_node.entry(node.clone()).or_insert_with(|| json!({}));
        let mut own = match &row {
            Some(row) => member_limits(
                row,
                if state.local_node.as_deref() == Some(node) {
                    storage.as_ref()
                } else {
                    None
                },
            )?,
            None => limits.clone(),
        };
        own["max_wall_seconds"] = json!(
            state
                .worker_wall_limits
                .get(node)
                .ok_or_else(|| exception("KeyError"))?
        );
        entry["work_limits"] = own;
        if let Some(row) = row {
            entry["resources"] = public_resources(&row)?;
        }
    }
    limits["max_wall_seconds"] = json!(
        state
            .worker_wall_limits
            .values()
            .min()
            .ok_or_else(|| exception("ValueError"))?
    );
    state.work_limits = Some(limits.clone());
    state.work_nodes = Some(Value::Object(per_node.clone()));
    let model = state.model.as_ref().ok_or_else(|| exception("KeyError"))?;
    reports.announce_work(
        &Announcement {
            phase: "work".into(),
            run_id: state.run_id.clone(),
            deployment_id: state.deployment_id.clone(),
            model_id: model.model_id.clone(),
            resolved_commit: model.resolved_commit.clone(),
            slice_mode: model.slice_mode,
            manifest_digest: model.manifest_digest.clone(),
            per_node: Some(per_node.into_iter().collect()),
        },
        &limits,
    )?;
    if plans.as_ref().is_none_or(BTreeMap::is_empty) {
        // B11(b), recorded divergence (owner ruling 16 Nov (4 a)): a host that forces stage
        // slicing pushed each Mac its own stage shard inside `node_slice_plans` before it answered
        // "no plan" (`OwnerWork::node_slice_plans`), so the oracle's whole-snapshot text would say
        // nothing moved while the stage shards did. Any other run keeps the oracle's text.
        let forced = state.model.as_ref().is_some_and(|m| m.forced_slice);
        env.progress(if forced {
            SHARDS_PUSHED
        } else {
            "whole snapshots: no slice to fetch"
        });
        return Ok(());
    }
    wait_with(
        env,
        |env| {
            reports.tick_local();
            let published = participant_rows(reports, state)?;
            env.set_result("participants", json!(published));
            let sum = |key: &str| -> Result<f64, StepError> {
                published.iter().try_fold(0.0, |n, p| {
                    let progress = &p["progress"];
                    if truthy(progress) && !progress.is_object() {
                        return Err(exception("AttributeError"));
                    }
                    let Some(value) = progress.get(key) else {
                        return Ok(n);
                    };
                    let v = match value {
                        Value::Bool(b) => f64::from(*b),
                        _ => value.as_f64().ok_or_else(|| exception("TypeError"))?,
                    };
                    Ok(n + v)
                })
            };
            let done = sum("done_bytes")?;
            let total = sum("total_bytes")?;
            env.progress(&format!(
                "slices verified {:.1} / {:.1} GB",
                done / 1e9,
                total / 1e9
            ));
            let current = run_reports(reports, state)?;
            super::observe::raise_member_refusal(&current, state)?;
            Ok((!state.participants.is_empty()
                && state.participants.iter().all(|n| {
                    matches!(
                        current
                            .get(n)
                            .and_then(|r| r.get("state"))
                            .and_then(Value::as_str),
                        Some("slice_ready" | "work_running" | "serving")
                    )
                }))
            .then_some(()))
        },
        seconds(BudgetId::Slices),
        "member_slice_timeout",
        interval,
    )
}
