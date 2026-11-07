// SPDX-License-Identifier: AGPL-3.0-only
//! `OwnerRun.status_snapshot()` (`owner_run.py:188-214 @ f4e366e9`) as a typed projection with the
//! oracle's key order. Nested chain-owned values (`participants`, `cleanup_unproven`, ...) are
//! `serde_json::Value`s as the chain set them.
use crate::machine::{CleanupRow, PROTOCOL, Rollover, STEPS, StepRow};
use serde_json::{Map, Value};

#[derive(Clone, Debug, PartialEq)]
pub struct Status {
    pub state: &'static str,
    pub reason: Option<&'static str>,
    pub step: Option<&'static str>,
    pub failed_step: Option<&'static str>,
    pub error_code: Option<String>,
    pub error_message: Option<String>,
    pub error_fix: Option<Map<String, Value>>,
    pub run_id: Option<String>,
    pub deployment_id: Option<String>,
    pub steps: Vec<StepRow>,
    pub registry_origin: Option<Value>,
    pub participants: Option<Value>,
    pub started_at: Option<f64>,
    pub route_ready: bool,
    pub cleanup_unproven: Option<Value>,
    pub cleanup_errors: Vec<CleanupRow>,
    pub rollover: Rollover,
    pub compilation_cache_hit: Option<Value>,
    pub build_seconds: Option<Value>,
}

fn opt<T: Into<Value>>(v: Option<T>) -> Value {
    v.map_or(Value::Null, Into::into)
}

impl Status {
    /// The document's entries in source order.
    pub fn entries(&self) -> Vec<(&'static str, Value)> {
        let steps = self
            .steps
            .iter()
            .zip(STEPS)
            .map(|(row, (name, label))| {
                let mut m = Map::new();
                for (k, v) in [
                    ("name", Value::from(name)),
                    ("label", Value::from(label)),
                    ("state", Value::from(row.state.as_str())),
                    ("detail", opt(row.detail.clone())),
                ] {
                    m.insert(k.into(), v);
                }
                Value::Object(m)
            })
            .collect::<Vec<_>>();
        let errors = self
            .cleanup_errors
            .iter()
            .map(|r| {
                let mut m = Map::new();
                m.insert("code".into(), r.code.clone().into());
                m.insert("exception_class".into(), r.exception_class.clone().into());
                m.insert("count".into(), r.count.into());
                Value::Object(m)
            })
            .collect::<Vec<_>>();
        let mut rollover = Map::new();
        rollover.insert("state".into(), self.rollover.state.into());
        rollover.insert("completed".into(), self.rollover.completed.into());
        rollover.insert(
            "consecutive_failures".into(),
            self.rollover.consecutive_failures.into(),
        );
        rollover.insert(
            "retry_at".into(),
            self.rollover
                .retry_at
                .and_then(serde_json::Number::from_f64)
                .map_or(Value::Null, Value::Number),
        );
        rollover.insert("last_error".into(), opt(self.rollover.last_error.clone()));
        vec![
            ("protocol", PROTOCOL.into()),
            ("state", self.state.into()),
            ("reason", opt(self.reason)),
            ("step", opt(self.step)),
            ("failed_step", opt(self.failed_step)),
            ("error_code", opt(self.error_code.clone())),
            ("error_message", opt(self.error_message.clone())),
            (
                "error_fix",
                self.error_fix.clone().map_or(Value::Null, Value::Object),
            ),
            ("run_id", opt(self.run_id.clone())),
            ("deployment_id", opt(self.deployment_id.clone())),
            ("steps", Value::Array(steps)),
            (
                "registry_origin",
                self.registry_origin.clone().unwrap_or(Value::Null),
            ),
            (
                "participants",
                self.participants.clone().unwrap_or(Value::Null),
            ),
            (
                "started_at",
                self.started_at
                    .and_then(serde_json::Number::from_f64)
                    .map_or(Value::Null, Value::Number),
            ),
            ("route_ready", self.route_ready.into()),
            (
                "cleanup_unproven",
                self.cleanup_unproven.clone().unwrap_or(Value::Null),
            ),
            ("cleanup_errors", Value::Array(errors)),
            ("rollover", Value::Object(rollover)),
            (
                "compilation_cache_hit",
                self.compilation_cache_hit.clone().unwrap_or(Value::Null),
            ),
            (
                "build_seconds",
                self.build_seconds.clone().unwrap_or(Value::Null),
            ),
        ]
    }

    pub fn to_value(&self) -> Value {
        Value::Object(
            self.entries()
                .into_iter()
                .map(|(k, v)| (k.to_owned(), v))
                .collect(),
        )
    }
}
