// SPDX-License-Identifier: AGPL-3.0-only
use crate::{Fault, FaultKind, Result, soak, stranger};
use serde::Deserialize;
use serde_json::{Value, json};
use std::path::PathBuf;
pub const HELP: &str = "device-harness (fake qualification is not device evidence)\n  evaluate <receipt.json>\n  stop --attempt <private-directory> --budgets <bounds.json>\nNo live stranger/soak launch command is enabled in this foundation-gated lane.\n";
#[derive(Debug, Eq, PartialEq)]
pub enum Action {
    Help,
    Evaluate(PathBuf),
    Stop { attempt: PathBuf, budgets: PathBuf },
}
pub fn parse(args: &[String]) -> Result<Action> {
    let a = args.iter().map(String::as_str).collect::<Vec<_>>();
    match a.as_slice() {
        [] | ["--help"] | ["help"] => Ok(Action::Help),
        ["evaluate", path] if !path.is_empty() => Ok(Action::Evaluate((*path).into())),
        ["stop", "--attempt", attempt, "--budgets", bounds]
            if !attempt.is_empty() && !bounds.is_empty() =>
        {
            Ok(Action::Stop {
                attempt: (*attempt).into(),
                budgets: (*bounds).into(),
            })
        }
        _ => Err(Fault::new("invalid_command_line", FaultKind::Input)),
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Bounds {
    pub command_ms: u64,
    pub grace_ms: u64,
    pub poll_ms: u64,
    pub max_output_bytes: u64,
}
/// JSON input decoded directly into typed payloads, without Serde's internally
/// tagged Content buffer (which treats arbitrary-precision decimals as maps).
pub enum Input {
    Soak {
        schema_version: u32,
        policy: soak::Policy,
        evidence: soak::Evidence,
    },
    Stranger {
        schema_version: u32,
        candidate: stranger::Candidate,
        evidence: stranger::Evidence,
    },
}
impl<'de> Deserialize<'de> for Input {
    fn deserialize<D: serde::Deserializer<'de>>(
        deserializer: D,
    ) -> std::result::Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(rename_all = "snake_case")]
        enum Scenario {
            Soak,
            Stranger,
        }
        #[derive(Deserialize)]
        struct Discriminator {
            scenario: Scenario,
        }
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Soak {
            #[serde(rename = "scenario")]
            _scenario: Scenario,
            schema_version: u32,
            policy: soak::Policy,
            evidence: soak::Evidence,
        }
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Stranger {
            #[serde(rename = "scenario")]
            _scenario: Scenario,
            schema_version: u32,
            candidate: stranger::Candidate,
            evidence: stranger::Evidence,
        }
        // Keep original JSON bytes: unlike a Value round trip this retains
        // duplicate keys, integer-vs-float syntax and strict typed refusals.
        let raw = Box::<serde_json::value::RawValue>::deserialize(deserializer)?;
        let discriminator: Discriminator =
            serde_json::from_str(raw.get()).map_err(serde::de::Error::custom)?;
        match discriminator.scenario {
            Scenario::Soak => {
                let p: Soak = serde_json::from_str(raw.get()).map_err(serde::de::Error::custom)?;
                Ok(Self::Soak {
                    schema_version: p.schema_version,
                    policy: p.policy,
                    evidence: p.evidence,
                })
            }
            Scenario::Stranger => {
                let p: Stranger =
                    serde_json::from_str(raw.get()).map_err(serde::de::Error::custom)?;
                Ok(Self::Stranger {
                    schema_version: p.schema_version,
                    candidate: p.candidate,
                    evidence: p.evidence,
                })
            }
        }
    }
}
pub fn evaluate(bytes: &[u8]) -> Result<Value> {
    let input: Input = serde_json::from_slice(bytes)
        .map_err(|_| Fault::new("invalid_evaluation_input", FaultKind::Input))?;
    let schema = match &input {
        Input::Soak { schema_version, .. } | Input::Stranger { schema_version, .. } => {
            *schema_version
        }
    };
    if schema != 1 {
        return Err(Fault::new("unsupported_evidence_schema", FaultKind::Input));
    }
    let mut value = match input {
        Input::Soak {
            policy,
            mut evidence,
            ..
        } => {
            // External receipt prose is not safe diagnostic text. Retain precise
            // local predicate codes, never echo arbitrary imported error strings.
            if evidence.product_failure.is_some() {
                evidence.product_failure = Some("product_failure_observed".into());
            }
            if evidence.incomplete_reason.is_some() {
                evidence.incomplete_reason = Some("observation_incomplete".into());
            }
            serde_json::to_value(soak::evaluate(&policy, &evidence))
                .map_err(|_| Fault::new("verdict_encoding_failed", FaultKind::Input))?
        }
        Input::Stranger {
            candidate,
            evidence,
            ..
        } => {
            let passed = stranger::evaluate(&candidate, &evidence);
            json!({"scenario_passed":passed,"outcome":if passed{"passed"}else if evidence.product_failed==Some(false){"incomplete"}else{"failed"},"release_qualified":false})
        }
    };
    value["evidence_boundary"] = json!("offline_replay_not_physical");
    Ok(value)
}
