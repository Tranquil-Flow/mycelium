// SPDX-License-Identifier: AGPL-3.0-only
//! Exact Python dataclass repr used by the pinned eligibility-generation digest.
use super::C;
use crate::{ContractError, canonical};
use serde_json::Value;
use std::sync::OnceLock;
pub fn string(s: &str) -> String {
    static NONPRINT: OnceLock<Vec<[u32; 2]>> = OnceLock::new();
    let ranges = NONPRINT.get_or_init(|| {
        serde_json::from_str(include_str!(
            "../../fixtures/gossip-nonprintable-ranges.json"
        ))
        .expect("sealed Unicode oracle table")
    });
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c => {
                let cp = c as u32;
                let i = ranges.partition_point(|r| r[1] < cp);
                if ranges.get(i).is_some_and(|r| r[0] <= cp) {
                    out.push_str(&if cp <= 255 {
                        format!("\\x{cp:02x}")
                    } else if cp <= 65535 {
                        format!("\\u{cp:04x}")
                    } else {
                        format!("\\U{cp:08x}")
                    });
                } else {
                    out.push(c);
                }
            }
        }
    }
    out.push(quote);
    out
}
fn atom(v: &Value) -> Result<String, ContractError> {
    Ok(match v {
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        Value::String(s) => string(s),
        Value::Number(_) => {
            String::from_utf8(canonical::to_bytes(v).map_err(|_| ContractError(C))?)
                .map_err(|_| ContractError(C))?
        }
        Value::Array(a) => tuple(a.iter().map(atom).collect::<Result<Vec<_>, _>>()?),
        _ => return Err(ContractError(C)),
    })
}
pub(super) fn tuple(parts: Vec<String>) -> String {
    format!(
        "({}{})",
        parts.join(", "),
        if parts.len() == 1 { "," } else { "" }
    )
}
pub(super) fn class(name: &str, v: &Value) -> Result<String, ContractError> {
    let fields = match name {
        "EvidenceVersion" => "incarnation sequence payload_hash",
        "EndpointEvidence" => "endpoint_id transport host port scope inbound",
        "OfferingEvidence" => {
            "deployment_id deployment_epoch assignment_id manifest_digest resolved_commit model_id start_layer end_layer_exclusive runtime_instance_id load_generation proof_digest inference_endpoint_id version"
        }
        "RouterNodeEvidence" => {
            "node_id incarnation boot_id peer_state eligible exclusion_reasons endpoints offerings profile_version status_version queue_depth in_flight concurrency_limit"
        }
        "RouterEdgeEvidence" => {
            "src_node_id src_endpoint_id dst_node_id dst_endpoint_id reachable eligible exclusion_reasons connect_rtt_ema_ms rtt_p95_ms jitter_ms loss_ratio goodput_mbps sample_count measurement_method version"
        }
        "MemoryDomainEvidence" => {
            "memory_domain_id kind total_bytes allocatable_after_reservations_bytes committed_bytes reclaimable_bytes reservation_generation"
        }
        "AllocatorNodeEvidence" => {
            "node_id incarnation boot_id peer_state eligible exclusion_reasons memory_domains fast_allocatable_bytes total_allocatable_bytes queue_depth in_flight concurrency_limit profile_version status_version"
        }
        _ => return Err(ContractError(C)),
    };
    let mut parts = Vec::new();
    for f in fields.split_whitespace() {
        let x = &v[f];
        let repr = match f {
            "version" | "profile_version" | "status_version" if !x.is_null() => {
                class("EvidenceVersion", x)?
            }
            "endpoints" | "offerings" | "memory_domains" => {
                let child = match f {
                    "endpoints" => "EndpointEvidence",
                    "offerings" => "OfferingEvidence",
                    _ => "MemoryDomainEvidence",
                };
                sequence(child, x)?
            }
            "peer_state" if !x.is_null() => {
                let s = x.as_str().ok_or(ContractError(C))?;
                format!("<PeerHealthState.{}: {}>", s.to_uppercase(), string(s))
            }
            _ => atom(x)?,
        };
        parts.push(format!("{f}={repr}"));
    }
    Ok(format!("{name}({})", parts.join(", ")))
}
pub(super) fn sequence(name: &str, v: &Value) -> Result<String, ContractError> {
    Ok(tuple(
        v.as_array()
            .ok_or(ContractError(C))?
            .iter()
            .map(|x| class(name, x))
            .collect::<Result<Vec<_>, _>>()?,
    ))
}
pub(super) fn digest(repr: &str) -> String {
    canonical::sha256_prefixed(repr.as_bytes())[7..].to_owned()
}
