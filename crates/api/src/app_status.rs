// SPDX-License-Identifier: AGPL-3.0-only
//! The status document the unchanged v0.1.4 page reads (`DemoApp.status`, app.py:1244-1283 @
//! f4e366e9), as far as both hosts (the headless launcher's `status_json` and the packaged host's
//! status overlay) build it from the same facts, so the two cannot drift: the run model with this
//! Mac's memory choices, the model contract, fast mode, the role, the resource policy, the
//! authority (owner member rows of four keys), `renewal_error`, the member's leave and the join flow.
//!
//! Page readers (crates/desktop/assets, byte-identical to v0.1.4 web/): `model_contract`
//! app.js:173; `run_model` run.js:53-66 and app.js:466-484 (`resources.memory_cap_options` for
//! `#standing-memory`); `fast_mode` run.js:69-75; `installation_role` app.js:175, swarm.js:27;
//! `resource_policy` app.js:231, :474; `authority` app.js:172-260 (members :252-260); `leave`
//! app.js:373; `join_flow` app.js:499-535, :578, :592. `renewal_error` is read by no page script:
//! it is part of the published document only (API readers).
use crate::{
    app_host::{Authority, ManagedSlot},
    jobj, joined_member,
    preferences::{fast_mode, resource_policy, run_model},
    windows::PendingWindows,
    wire::Json,
};
use serde_json::Value;

/// What the app records between its passes and shows in its status: `DemoApp.renew_error`
/// (`_renew_loop`, app.py:917-927), `standing_error` / `standing_error_http` (`_standing_loop`,
/// app.py:1201-1212, and a failed `bind_standing_consent` at join, :1802-1806), the standing
/// acceptor's `last` offer (`StandingAcceptor.status()['last']`, standing_consent.py:393-397) and
/// `last_report` (`_auto_report`, app.py:1138-1163).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Recorded {
    pub renewal_error: Option<&'static str>,
    /// `DemoApp.recovery_error` (app.py:557, :625): a joined Mac whose start found a dead relay
    /// or a lapsed membership keeps the app started but not running and records the code here; a
    /// later successful start or reconnect clears it (app.py:1434, :1636).
    pub recovery_error: Option<&'static str>,
    pub standing_error: Option<String>,
    pub standing_error_http: Option<Value>,
    pub standing_last: Option<Value>,
    pub last_report: Option<Value>,
    /// `DemoApp._next_periodic_report` (app.py:532, :1162): `None` = `0.0`, due at the first tick.
    pub next_periodic_report: Option<std::time::Instant>,
}
/// The 300 s between two periodic capability reports (`MemberPeriodicReport`, app.py:1162).
pub fn periodic_report_period() -> std::time::Duration {
    budgets::BudgetId::MemberPeriodicReport
        .span(budgets::Entries::ZERO)
        .map_or(std::time::Duration::ZERO, |s| {
            std::time::Duration::from_millis(s.millis())
        })
}
/// `_renew_loop`'s code for a pass that failed while the app was running (app.py:925-927).
pub const RENEWAL_UNAVAILABLE: &str = "renewal_unavailable";
/// One pass of `_renew_loop` as it ended.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RenewalPass {
    /// `continue`: closed, not running, left, or no authority; nothing changes.
    Skipped,
    /// `authority.renew()` returned: `renew_error = None`.
    Renewed,
    /// It raised: `renew_error = 'renewal_unavailable'` if the app is still running and open.
    Failed,
}
impl Recorded {
    /// `_auto_report`'s end (app.py:1161-1162): `self.last_report = record` and
    /// `self._next_periodic_report = time.monotonic() + 300` ([`periodic_report_period`]).
    pub fn reported(&mut self, record: Value, now: std::time::Instant) {
        self.last_report = Some(record);
        self.next_periodic_report = Some(now + periodic_report_period());
    }
    /// `_standing_tick`'s `due = time.monotonic() >= self._next_periodic_report` (app.py:1197).
    pub fn periodic_report_due(&self, now: std::time::Instant) -> bool {
        self.next_periodic_report.is_none_or(|next| now >= next)
    }
    /// Record a pass of `_renew_loop`; `running_open` is `self.running and not
    /// self.closed.is_set()` read after the pass (app.py:926).
    pub fn renewal(&mut self, pass: RenewalPass, running_open: bool) {
        match pass {
            RenewalPass::Skipped => {}
            RenewalPass::Renewed => self.renewal_error = None,
            RenewalPass::Failed if running_open => self.renewal_error = Some(RENEWAL_UNAVAILABLE),
            RenewalPass::Failed => {}
        }
    }
}

/// The facts a host reads beside its authority: the run-model and token-path environment
/// overrides and this Mac's RAM (`host_memory_bytes()`, app.py:267-273, or v0.1.4's test seam).
#[derive(Clone, Copy, Debug, Default)]
pub struct Facts<'a> {
    pub run_model_env: Option<&'a str>,
    pub fast_mode_env: Option<&'a str>,
    pub device_memory_bytes: Option<u64>,
}

/// `run_model.document(root)` (run_model.py:116-131) with `consent_document(model,
/// device_memory_bytes=...)` (model_resources.py:605-631): the picker and the consent choices.
/// A choice that cannot be read leaves the picker out (`null`).
pub fn run_model(root: &std::path::Path, facts: &Facts<'_>) -> Json {
    run_model::document_json(root, facts.run_model_env, facts.device_memory_bytes)
        .unwrap_or(Json::Null)
}
/// `asdict(self.run_model_contract())` (app.py:1264, serving.py:150-160): every registered model is
/// a bf16 safetensors source served by MLX in float32 with int8 weights, greedy, seed 0
/// (serving.py:189-265); `null` when the run model cannot be read.
pub fn model_contract(run: &Json) -> Json {
    match (run.get("model_id"), run.get("resolved_commit")) {
        (Some(Json::Str(id)), Some(Json::Str(commit))) => jobj! {
            "model_id" => id.as_str(), "resolved_commit" => commit.as_str(),
            "source_representation" => "bfloat16-safetensors", "runtime_backend" => "mlx",
            "runtime_dtype" => "float32", "runtime_quantization" => "int8-weight-only",
            "decoding" => "quantized_greedy_token_id", "sampling_seed" => 0u64,
        },
        _ => Json::Null,
    }
}
/// `fast_mode.document(self.root)` (fast_mode.py:22-40); `null` when it cannot be read.
pub fn fast_mode(root: &std::path::Path, facts: &Facts<'_>) -> Json {
    fast_mode::document_json(root, facts.fast_mode_env).unwrap_or(Json::Null)
}
/// `self.renew_error` (app.py:1272).
pub fn renewal_error(recorded: &Recorded) -> Json {
    recorded.renewal_error.map_or(Json::Null, Json::from)
}
/// `self.recovery_error` (app.py:1273).
pub fn recovery_error(recorded: &Recorded) -> Json {
    recorded.recovery_error.map_or(Json::Null, Json::from)
}
/// `self._leave_status()`: a joined Mac's leave record (member_leave.py), `null` for an owner.
pub fn leave<M: ManagedSlot>(c: &Authority<M>) -> Json {
    if !c.target {
        return Json::Null;
    }
    let leave = crate::leave::MemberLeave::new(&c.root, crate::leave::Limits::default());
    crate::leave::status_view(&leave).map_or(Json::Null, Json::from)
}
/// The fields of `DemoApp.status()` both hosts take from the authority and `facts`, in the source's
/// key order (`STATUS_FIELDS`): `role`, `model_contract`, `run_model`, `fast_mode`, `installation_role`,
/// `resource_policy`, `running`, `authority` (owner member rows: node_id / generation / lifecycle_state /
/// clock, app.py:1253-1258), `renewal_error`, `recovery_error`, `leave`, `join_flow` (`_join_flow_status`,
/// app.py:1214-1237, with what the app recorded).
pub fn shared<M: ManagedSlot>(
    c: &Authority<M>,
    pending: &PendingWindows,
    facts: &Facts<'_>,
) -> Vec<(&'static str, Json)> {
    let run = run_model(&c.root, facts);
    vec![
        // `'role':'not_yet_serving'` (app.py:1263): a constant of the shipped status.
        ("role", "not_yet_serving".into()),
        ("model_contract", model_contract(&run)),
        ("run_model", run),
        ("fast_mode", fast_mode(&c.root, facts)),
        (
            "installation_role",
            if c.target { "target" } else { "owner" }.into(),
        ),
        (
            "resource_policy",
            resource_policy::read(&c.root)
                .ok()
                .flatten()
                .map_or(Json::Null, Json::from),
        ),
        // `'running': self.running` (app.py:1272): the APPLICATION's flag (authority started and not
        // stopped), never a native worker's; the page enables its member buttons on it
        // (app.js:181-188, 207-209, 231-235).
        ("running", c.running.into()),
        ("authority", joined_member::authority_status(c)),
        ("renewal_error", renewal_error(&c.recorded)),
        // B10: v0.1.4's next status key (app.py:1273); the joined Mac's relay line reads it.
        ("recovery_error", recovery_error(&c.recorded)),
        ("leave", leave(c)),
        (
            "join_flow",
            joined_member::join_flow_status(&c.root, c.target, &c.windows, pending, &c.recorded),
        ),
    ]
}
