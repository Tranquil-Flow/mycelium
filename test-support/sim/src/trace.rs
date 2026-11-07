// SPDX-License-Identifier: AGPL-3.0-only
use crate::trace_budgets::{self, Snapshot};
use crate::{
    SimResult,
    model::{self, Command, Config, Input, Mode, Outcome},
};
use serde::{Deserialize, Serialize};
pub const SIMULATOR_VERSION: &str = "mycelium-sim-v2";
pub const PRNG_VERSION: &str = "splitmix64-keyed-v1";
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Trace {
    pub schema_version: u32,
    pub build_id: String,
    pub cargo_lock_sha256: String,
    pub simulator_version: String,
    pub prng_version: String,
    pub seed: u64,
    pub config: Config,
    pub profile_id: String,
    pub profile_snapshot: String,
    pub budget_snapshot: Snapshot,
    pub corrected_budget_snapshot: Snapshot,
    pub corrected_outcome: Outcome,
    /// Canonical serialized execution inputs, independently retained at recording.
    pub execution_pin: String,
    pub corrected_execution_pin: String,
    pub commands: Vec<Input>,
    pub expected_failure: Vec<String>,
    pub expected_outcome: Outcome,
}

impl Trace {
    fn input_pin(&self) -> SimResult<String> {
        serde_json::to_string(&(
            &self.config,
            &self.commands,
            &self.profile_id,
            &self.profile_snapshot,
        ))
        .map_err(|e| format!("execution_pin_encode:{e}"))
    }
    /// Recording/minimization only. Replay NEVER refreshes a pin.
    fn record_runs(&mut self) -> SimResult<()> {
        self.profile_snapshot = if self.config.incident.is_extended() {
            self.profile_id.clone()
        } else {
            let bundle: latency_profiles::Bundle =
                serde_json::from_str(include_str!("../../profiles/data/v014-v1.json"))
                    .map_err(|e| format!("profile_decode:{e}"))?;
            let profile = bundle
                .profiles
                .iter()
                .find(|p| p.id == self.profile_id)
                .ok_or("missing_profile")?;
            serde_json::to_string(profile).map_err(|e| format!("profile_encode:{e}"))?
        };
        let (outcome, reads) = trace_budgets::capture(|| model::run(&self.config, &self.commands))?;
        self.budget_snapshot = reads;
        self.expected_failure = model::violations(&self.config, &outcome);
        self.expected_outcome = outcome;
        let mut corrected = self.config.clone();
        corrected.mode = Mode::Corrected;
        let (outcome, reads) = trace_budgets::capture(|| model::run(&corrected, &self.commands))?;
        self.corrected_budget_snapshot = reads;
        self.corrected_outcome = outcome;
        self.corrected_execution_pin = serde_json::to_string(&(
            &corrected,
            &self.commands,
            &self.profile_id,
            &self.profile_snapshot,
        ))
        .map_err(|e| e.to_string())?;
        self.execution_pin = self.input_pin()?;
        Ok(())
    }
    /// Explicit legacy migration; callers must compare both outcomes to the
    /// independently exported baseline before publishing these new producer pins.
    pub fn migrate_legacy(raw: serde_json::Value, build: String, lock: String) -> SimResult<Self> {
        if raw["schema_version"] != 1
            || raw["simulator_version"] != "mycelium-sim-v1"
            || raw["prng_version"] != PRNG_VERSION
        {
            return Err("unsupported_legacy_trace".into());
        }
        let mut v = raw.clone();
        v["schema_version"] = 2.into();
        v["simulator_version"] = SIMULATOR_VERSION.into();
        v["build_id"] = build.into();
        v["cargo_lock_sha256"] = lock.into();
        v["budget_snapshot"] = serde_json::json!({});
        v["corrected_budget_snapshot"] = serde_json::json!({});
        v["corrected_outcome"] =
            serde_json::to_value(Outcome::default()).map_err(|e| e.to_string())?;
        v["profile_snapshot"] = "".into();
        v["execution_pin"] = "".into();
        v["corrected_execution_pin"] = "".into();
        let mut t: Self = serde_json::from_value(v).map_err(|e| format!("legacy_decode:{e}"))?;
        let recorded = t.expected_outcome.clone();
        let failures = t.expected_failure.clone();
        t.record_runs()?;
        // v1 only retained values. Require every actually read old value,
        // including reads on the corrected branch, before enriching declarations.
        for (id, declaration) in t.budget_snapshot.iter().chain(&t.corrected_budget_snapshot) {
            if raw["budget_snapshot"][id].as_str() != Some(declaration.value.as_str()) {
                return Err(format!("legacy_used_budget_mismatch:{id}"));
            }
        }
        if t.expected_outcome != recorded || t.expected_failure != failures {
            return Err("legacy_outcome_mismatch".into());
        }
        t.verify_recorded()?;
        Ok(t)
    }

    pub fn generate(
        build_id: String,
        cargo_lock_sha256: String,
        seed: u64,
        config: Config,
    ) -> SimResult<Self> {
        let profile_id = if config.incident.is_extended() {
            "section7-policy-model-v1-NOT-MEASURED".into()
        } else if config.close_multiplier == 1 {
            "v014-L13-reported-stop-v1".into()
        } else {
            format!(
                "v014-L13-reported-stop-v1-synthetic-{}x",
                config.close_multiplier
            )
        };
        let mut t = Self {
            schema_version: 2,
            build_id,
            cargo_lock_sha256,
            simulator_version: SIMULATOR_VERSION.into(),
            prng_version: PRNG_VERSION.into(),
            seed,
            config,
            profile_id,
            profile_snapshot: String::new(),
            budget_snapshot: Snapshot::new(),
            corrected_budget_snapshot: Snapshot::new(),
            corrected_outcome: Outcome::default(),
            execution_pin: String::new(),
            corrected_execution_pin: String::new(),
            commands: vec![
                Input {
                    at_ms: 0,
                    task: 0,
                    command: Command::Observe,
                },
                Input {
                    at_ms: 0,
                    task: 1,
                    command: Command::Begin,
                },
                Input {
                    at_ms: 0,
                    task: 2,
                    command: Command::WallJump {
                        delta_ms: if crate::random(seed, 0, 0, 0).is_multiple_of(2) {
                            1
                        } else {
                            -1
                        } * i64::try_from(
                            crate::span(budgets::BudgetId::ControlClockSkew)?.millis(),
                        )
                        .map_err(|_| "clock_overflow")?,
                    },
                },
            ],
            expected_failure: Vec::new(),
            expected_outcome: Outcome::default(),
        };
        t.record_runs()?;
        Ok(t)
    }
    pub fn replay(&self) -> SimResult<Outcome> {
        if self.schema_version != 2
            || self.simulator_version != SIMULATOR_VERSION
            || self.prng_version != PRNG_VERSION
        {
            return Err("unsupported_trace_version".into());
        }
        if self.build_id.is_empty()
            || self.cargo_lock_sha256.len() != 64
            || !self
                .cargo_lock_sha256
                .bytes()
                .all(|c| c.is_ascii_hexdigit())
        {
            return Err("invalid_build_identity".into());
        }
        trace_budgets::validate(&self.budget_snapshot, &trace_budgets::registry())?;
        if self.execution_pin != self.input_pin()? {
            return Err("execution_input_mismatch".into());
        }
        if self.commands.len() > 1000 {
            return Err("command_limit".into());
        }
        // Replay executes concrete inputs; it never calls generate/random.
        let (outcome, reads) = trace_budgets::capture(|| model::run(&self.config, &self.commands))?;
        trace_budgets::enforce_reads(&self.budget_snapshot, &reads)?;
        if outcome != self.expected_outcome {
            return Err("recorded_outcome_mismatch".into());
        }
        if model::violations(&self.config, &outcome) != self.expected_failure {
            return Err("recorded_failure_mismatch".into());
        }
        Ok(outcome)
    }
    /// Verify the saved event order and semantic failure before interpreting a
    /// replay as evidence. Generation deliberately uses replay(), not this check.
    pub fn verify_recorded(&self) -> SimResult<Outcome> {
        let outcome = self.replay()?;
        if outcome != self.expected_outcome {
            return Err("recorded_outcome_mismatch".into());
        }
        if model::violations(&self.config, &outcome) != self.expected_failure {
            return Err("recorded_failure_mismatch".into());
        }
        Ok(outcome)
    }
    pub fn require_identity(&self, build: &str, lock: &str) -> SimResult<()> {
        if self.build_id != build || self.cargo_lock_sha256 != lock {
            return Err("replay_identity_mismatch".into());
        }
        Ok(())
    }
    pub fn validate_profile(&self, bundle: &latency_profiles::Bundle) -> SimResult<()> {
        if self.config.incident.is_extended() {
            return if self.profile_id == "section7-policy-model-v1-NOT-MEASURED"
                && self.profile_snapshot == self.profile_id
            {
                Ok(())
            } else {
                Err("model_profile_mismatch".into())
            };
        }
        if bundle.schema_version != 1 {
            return Err("profile_schema_mismatch".into());
        }
        let p = bundle
            .profiles
            .iter()
            .find(|p| p.id == self.profile_id)
            .ok_or("missing_profile")?;
        let expected = crate::span(budgets::BudgetId::ObservedStop)?
            .checked_mul(self.config.close_multiplier)
            .map_err(|_| "profile_overflow")?
            .millis() as f64;
        let origin = if self.config.close_multiplier == 1 {
            "REPORTED_APPROXIMATE"
        } else {
            "SYNTHETIC"
        };
        if p.phase != "stop"
            || p.unit != "ms"
            || p.origin != origin
            || p.samples.len() != 1
            || p.samples[0].value != expected
        {
            return Err("profile_budget_mismatch".into());
        }
        if serde_json::to_string(p).map_err(|e| format!("profile_encode:{e}"))?
            != self.profile_snapshot
        {
            return Err("profile_content_mismatch".into());
        }
        Ok(())
    }
    /// Deterministic deletion minimizer: preserve exact semantic failure set AND
    /// reached start/stop. Setup errors and vacuous no-op traces cannot qualify.
    pub fn minimize(&self) -> SimResult<Self> {
        let mut best = self.clone();
        let original = self.replay()?;
        let fingerprint = model::violations(&self.config, &original);
        if fingerprint.is_empty() {
            return Err("not_a_failing_trace".into());
        }
        let mut i = 0;
        while i < best.commands.len() {
            let mut candidate = best.clone();
            candidate.commands.remove(i);
            match candidate
                .record_runs()
                .map(|()| candidate.expected_outcome.clone())
            {
                Ok(o)
                    if model::violations(&candidate.config, &o) == fingerprint
                        && o.starts > 0
                        && candidate
                            .config
                            .incident
                            .required_transitions()
                            .iter()
                            .all(|t| o.transitions.contains_key(*t)) =>
                {
                    best = candidate
                }
                Ok(_) | Err(_) => i += 1, // candidate rejected; original evidence retained
            }
        }
        best.expected_failure = fingerprint;
        best.expected_outcome = best.replay()?;
        Ok(best)
    }
    pub fn corrected(&self) -> Self {
        let mut t = self.clone();
        t.config.mode = Mode::Corrected;
        t.budget_snapshot = t.corrected_budget_snapshot.clone();
        t.expected_outcome = t.corrected_outcome.clone();
        t.execution_pin = t.corrected_execution_pin.clone();
        t.expected_failure.clear();
        t
    }
}
