// SPDX-License-Identifier: AGPL-3.0-only
//! Browser-facing activation and swarm schemas, not lifecycle authority.
//! Source: mycelium_live/activation.py:98-178 and
//! mycelium_ui_gateway/validation.py:149-167,690-744 @ f4e366e9.
use crate::{ContractError, checks::*};
use serde_json::Value;
use std::{collections::BTreeSet, net::IpAddr};

/// Primary JSON Schema boundary only; no session/CSRF or endpoint authority.
/// ui/contracts/product/product-ui-bootstrap-v1.schema.json @ f4e366e9.
pub fn bootstrap(v: &Value) -> Result<(), ContractError> {
    let c = "bootstrap_schema_invalid";
    let has_features = v.get("features").is_some();
    fields(
        v,
        if has_features {
            "protocol source_mode session api limits qualification_authority features"
        } else {
            "protocol source_mode session api limits qualification_authority"
        },
        c,
    )?;
    require(
        v["protocol"] == "mycelium.product_ui.bootstrap.v1"
            && one_of(&v["source_mode"], &["fixture", "live", "replay"])
            && v["qualification_authority"]
                == "mycelium_qualification.qualifier:RouteQualificationV1",
        c,
    )?;
    let session = &v["session"];
    fields(session, "csrf_header csrf_token expires_at_unix_ms", c)?;
    require(
        session["csrf_header"] == "X-Mycelium-CSRF"
            && session["csrf_token"]
                .as_str()
                .is_some_and(|s| (16..=512).contains(&s.chars().count()))
            && session["expires_at_unix_ms"]
                .as_f64()
                .is_some_and(|n| n.fract() == 0.0 && (0.0..=9_007_199_254_740_991.0).contains(&n)),
        c,
    )?;
    let endpoints = [
        ("observatory_snapshot", "/api/v1/observatory/snapshot"),
        ("observatory_events", "/api/v1/observatory/events"),
        ("qualification_current", "/api/v1/qualification/current"),
        ("inference_submit", "/api/v1/inference"),
        ("swarm_status", "/api/v1/swarm/status"),
        ("swarm_invites", "/api/v1/swarm/invites"),
        ("swarm_join", "/api/v1/swarm/join"),
        ("swarm_leave", "/api/v1/swarm/leave"),
        ("product_snapshot", "/api/v1/product/snapshot"),
        ("product_events", "/api/v1/product/events"),
        ("product_export", "/api/v1/product/export"),
        ("recovery_status", "/api/v1/recovery/status"),
    ];
    require(
        v["api"].as_object().is_some_and(|o| {
            o.len() == endpoints.len()
                && endpoints
                    .iter()
                    .all(|(k, val)| o.get(*k) == Some(&Value::from(*val)))
        }),
        c,
    )?;
    fields(&v["limits"], "max_prompt_utf8_bytes max_new_tokens", c)?;
    require(
        python_equal(&v["limits"]["max_prompt_utf8_bytes"], &Value::from(131072))
            && python_equal(&v["limits"]["max_new_tokens"], &Value::from(4096)),
        c,
    )?;
    if has_features {
        fields(&v["features"], "speculation_preference", c)?;
        require(v["features"]["speculation_preference"] == true, c)?;
    }
    Ok(())
}

pub(crate) fn fields(v: &Value, names: &str, code: &'static str) -> Result<(), ContractError> {
    exact(v, &names.split_whitespace().collect::<Vec<_>>(), code)
}
pub(crate) fn nonempty(v: &Value) -> bool {
    v.as_str().is_some_and(|s| !s.is_empty())
}
pub(crate) fn hex_string(v: &Value, length: usize) -> bool {
    v.as_str().is_some_and(|s| {
        s.len() == length
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}
pub(crate) fn safe_integer(v: &Value, min: u64) -> bool {
    v.as_u64()
        .is_some_and(|n| n >= min && n <= 9_007_199_254_740_991)
}
// The identifier grammar is ASCII, so regex word boundaries reduce to ASCII
// alphanumeric/underscore here. Keep credential detection separate from grammar.
pub(crate) fn credential_identifier(s: &str) -> bool {
    let lower = s.to_ascii_lowercase();
    for (i, _) in lower.char_indices() {
        if i > 0
            && (lower.as_bytes()[i - 1].is_ascii_alphanumeric() || lower.as_bytes()[i - 1] == b'_')
        {
            continue;
        }
        let tail = &lower[i..];
        for (prefix, minimum, underscore) in [
            ("sk-", 12, true),
            ("github_pat_", 20, true),
            ("ghp_", 20, false),
            ("gho_", 20, false),
            ("ghu_", 20, false),
            ("ghs_", 20, false),
            ("ghr_", 20, false),
        ] {
            if let Some(rest) = tail.strip_prefix(prefix) {
                let count = rest
                    .bytes()
                    .take_while(|b| {
                        b.is_ascii_alphanumeric()
                            || (underscore && (*b == b'_' || (prefix == "sk-" && *b == b'-')))
                    })
                    .count();
                if count >= minimum {
                    return true;
                }
            }
        }
    }
    false
}
pub(crate) fn host_port(s: &str) -> bool {
    s.rsplit_once(':').is_some_and(|(host, port)| {
        !host.is_empty()
            && host
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b".-".contains(&b))
            && !port.is_empty()
            && port.len() <= 5
            && port.bytes().all(|b| b.is_ascii_digit())
    })
}
pub(crate) fn public_identifier(v: &Value) -> bool {
    let Some(s) = v.as_str() else {
        return false;
    };
    let lower = s.to_ascii_lowercase();
    !s.is_empty()
        && s.len() <= 128
        && s.as_bytes()[0].is_ascii_alphanumeric()
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:~-".contains(&b))
        && !host_port(s)
        && !credential_identifier(s)
        && lower != "localhost"
        && ![".localhost", ".local", ".internal", ".lan"]
            .iter()
            .any(|suffix| lower.ends_with(suffix))
        && s.parse::<IpAddr>().is_err()
}

pub fn activation(v: &Value) -> Result<(), ContractError> {
    let c = "deployment_activation_status_invalid";
    fields(
        v,
        "protocol generation busy_candidate_id invalid_candidate_count candidates",
        c,
    )?;
    require(
        v["protocol"] == "mycelium.deployment_activation.v1"
            && integer(&v["generation"], 0)
            && integer(&v["invalid_candidate_count"], 0)
            && (v["busy_candidate_id"].is_null() || nonempty(&v["busy_candidate_id"])),
        c,
    )?;
    let candidates = v["candidates"].as_array().ok_or(ContractError(c))?;
    let mut seen = BTreeSet::new();
    let mut activating = Vec::new();
    for candidate in candidates {
        let c = "deployment_activation_candidate_invalid";
        fields(
            candidate,
            "candidate_id deployment_id model_id model_revision quantization topology_size plan_digest state phase completed_steps total_steps reason_code",
            c,
        )?;
        for f in [
            "candidate_id",
            "deployment_id",
            "model_id",
            "model_revision",
            "quantization",
            "plan_digest",
        ] {
            require(nonempty(&candidate[f]), c)?;
        }
        require(
            seen.insert(candidate["candidate_id"].as_str())
                && candidate["candidate_id"] == candidate["deployment_id"]
                && hex_string(&candidate["model_revision"], 40)
                && digest(&candidate["plan_digest"])
                && integer(&candidate["topology_size"], 1)
                && candidate["completed_steps"]
                    .as_u64()
                    .is_some_and(|n| n <= 4)
                && python_equal(&candidate["total_steps"], &Value::from(4)),
            c,
        )?;
        let phase = &candidate["phase"];
        let reason = &candidate["reason_code"];
        let completed = &candidate["completed_steps"];
        match candidate["state"].as_str() {
            Some("activating") => {
                let step = match phase.as_str() {
                    Some("validating_plan") => 1,
                    Some("opening_route") => 2,
                    Some("qualifying_route") => 3,
                    Some("registering") => 4,
                    _ => return Err(ContractError(c)),
                };
                require(reason.is_null() && completed == step, c)?;
                activating.push(&candidate["candidate_id"]);
            }
            Some("qualified" | "active") => {
                require(phase.is_null() && reason.is_null() && completed == 4, c)?
            }
            Some("failed" | "unavailable") => require(phase.is_null() && nonempty(reason), c)?,
            Some("prepared") => require(phase.is_null() && reason.is_null() && completed == 0, c)?,
            _ => return Err(ContractError(c)),
        }
    }
    require(
        if v["busy_candidate_id"].is_null() {
            activating.is_empty()
        } else {
            activating == [&v["busy_candidate_id"]]
        },
        "deployment_activation_busy_state_invalid",
    )
}

pub fn swarm(v: &Value) -> Result<(), ContractError> {
    let c = "invalid_coordinator_response";
    fields(v, "protocol native_nodes browser_workers", c)?;
    require(v["protocol"] == "mycelium.product_ui.swarm.v1", c)?;
    let nodes = v["native_nodes"]
        .as_array()
        .filter(|a| a.len() <= 4096)
        .ok_or(ContractError(c))?;
    let workers = v["browser_workers"]
        .as_array()
        .filter(|a| a.len() <= 4096)
        .ok_or(ContractError(c))?;
    for n in nodes {
        fields(
            n,
            "member_id capability membership_state connectivity endpoint_id",
            c,
        )?;
        require(
            public_identifier(&n["member_id"])
                && n["capability"] == "native_inference_node"
                && one_of(
                    &n["membership_state"],
                    &[
                        "invited",
                        "trusted",
                        "reachable",
                        "assigned",
                        "qualified",
                        "revoked",
                    ],
                )
                && one_of(
                    &n["connectivity"],
                    &[
                        "direct",
                        "relayed",
                        "local",
                        "disconnected",
                        "stale",
                        "unknown",
                    ],
                )
                && (n["endpoint_id"].is_null()
                    || (public_identifier(&n["endpoint_id"])
                        && n["endpoint_id"]
                            .as_str()
                            .is_some_and(|s| !s.to_ascii_lowercase().starts_with("localhost")))),
            c,
        )?;
    }
    for w in workers {
        fields(w, "peer_id capability state expires_at_unix_ms", c)?;
        require(
            public_identifier(&w["peer_id"])
                && w["capability"] == "synthetic_browser_probe"
                && one_of(
                    &w["state"],
                    &[
                        "invited",
                        "connecting",
                        "ready",
                        "busy",
                        "disconnected",
                        "stale",
                        "revoked",
                    ],
                )
                && safe_integer(&w["expires_at_unix_ms"], 0),
            c,
        )?;
    }
    Ok(())
}
