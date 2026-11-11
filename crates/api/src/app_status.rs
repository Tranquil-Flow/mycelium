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
use serde_json::Value;

/// What the app records between its passes and shows in its status: `DemoApp.renew_error`
/// (`_renew_loop`, app.py:917-927), `standing_error` / `standing_error_http` (`_standing_loop`,
/// app.py:1201-1212, and a failed `bind_standing_consent` at join, :1802-1806), the standing
/// acceptor's `last` offer (`StandingAcceptor.status()['last']`, standing_consent.py:393-397) and
/// `last_report` (`_auto_report`, app.py:1138-1163).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Recorded {
    pub renewal_error: Option<&'static str>,
    pub standing_error: Option<String>,
    pub standing_error_http: Option<Value>,
    pub standing_last: Option<Value>,
    pub last_report: Option<Value>,
}
