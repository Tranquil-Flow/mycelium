// SPDX-License-Identifier: AGPL-3.0-or-later
//! Native evidence records the A12 gate consumes
//! (`mycelium_mobile/a12_activation.py`): the closed
//! `mycelium.mobile_runtime_observation.v1` document, the undivided product path
//! and pseudonymous device-family references. Missing or denied platform data is
//! the string `unknown`, never a fabricated number.

use std::collections::BTreeMap;

use serde_json::{Map, Value, json};

use crate::canonical;

pub const RUNTIME_OBSERVATION_PROTOCOL: &str = "mycelium.mobile_runtime_observation.v1";
pub const NATIVE_SOURCE_KIND: &str = "native_device_observation";
pub const PRODUCT_PATH_KINDS: [&str; 2] = ["signed_native_application", "embedded_iroh_transport"];
pub const EXCLUDED_PRODUCT_PATHS: [&str; 3] = ["termux", "adb", "ssh"];
pub const MOBILE_PLATFORM_FAMILIES: [&str; 3] = ["android", "ios", "ipados"];
pub const METRIC_COUNT_CEILING: usize = 256;
pub const APPLE_INITIAL_ELIGIBILITY: &str = "foreground_active_only";

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct EvidenceError(pub &'static str);

/// `require_undivided_product_path`.
pub fn require_product_path(path_kind: &str) -> Result<&str, EvidenceError> {
    if EXCLUDED_PRODUCT_PATHS.contains(&path_kind) {
        return Err(EvidenceError("excluded_development_path_cannot_qualify"));
    }
    if !PRODUCT_PATH_KINDS.contains(&path_kind) {
        return Err(EvidenceError("product_path_kind_invalid"));
    }
    Ok(path_kind)
}

fn metric_name_ok(name: &str) -> bool {
    let b = name.as_bytes();
    !b.is_empty()
        && b.len() <= 64
        && b[0].is_ascii_lowercase()
        && b.iter().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'_')
}

/// One sampled metric: a finite number, or `None` when the platform API is
/// missing or consent was denied (serialized as `"unknown"`).
pub type Metrics = BTreeMap<String, Option<f64>>;

pub struct Observation<'a> {
    pub platform_family: &'a str,
    pub runtime_build_digest: &'a str,
    pub profile_generation: u64,
    pub evidence_generation: u64,
    pub observed_at_unix_ms: f64,
    pub measurement_duration_ms: f64,
    pub sample_count: u64,
    pub authority: &'a str,
    pub metrics: &'a Metrics,
}

/// Build one closed observation document; mirrors
/// `validate_mobile_runtime_observation` so the app never emits a record the gate
/// would reject.
pub fn runtime_observation(o: &Observation<'_>) -> Result<Value, EvidenceError> {
    if !MOBILE_PLATFORM_FAMILIES.contains(&o.platform_family) {
        return Err(EvidenceError("mobile_observation_platform_invalid"));
    }
    let d = o.runtime_build_digest;
    if !(d.len() == 71 && d.starts_with("sha256:") && d[7..].bytes().all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())) {
        return Err(EvidenceError("mobile_observation_build_invalid"));
    }
    if o.profile_generation < 1 || o.evidence_generation < 1 {
        return Err(EvidenceError("mobile_observation_generation_invalid"));
    }
    if !o.observed_at_unix_ms.is_finite() || o.observed_at_unix_ms < 0.0 {
        return Err(EvidenceError("mobile_observation_measurement_invalid"));
    }
    if !o.measurement_duration_ms.is_finite() || o.measurement_duration_ms <= 0.0 || o.sample_count < 1 {
        return Err(EvidenceError("mobile_observation_measurement_invalid"));
    }
    if o.authority.is_empty() || o.authority.len() > 128 {
        return Err(EvidenceError("mobile_observation_authority_invalid"));
    }
    if o.metrics.len() > METRIC_COUNT_CEILING {
        return Err(EvidenceError("mobile_observation_metric_invalid"));
    }
    let mut metrics = Map::new();
    for (name, value) in o.metrics {
        if !metric_name_ok(name) {
            return Err(EvidenceError("mobile_observation_metric_invalid"));
        }
        let v = match value {
            Some(x) if x.is_finite() => json!(x),
            Some(_) => return Err(EvidenceError("mobile_observation_metric_invalid")),
            None => json!("unknown"),
        };
        metrics.insert(name.clone(), v);
    }
    Ok(json!({
        "protocol": RUNTIME_OBSERVATION_PROTOCOL,
        "platform_family": o.platform_family,
        "runtime_build_digest": o.runtime_build_digest,
        "profile_generation": o.profile_generation,
        "evidence_generation": o.evidence_generation,
        "source_kind": NATIVE_SOURCE_KIND,
        "observed_at_unix_ms": o.observed_at_unix_ms,
        "measurement_duration_ms": o.measurement_duration_ms,
        "sample_count": o.sample_count,
        "authority": o.authority,
        "metrics": Value::Object(metrics),
    }))
}

/// Pseudonymous device-family reference from registered capability fields only
/// (ABI, OS API level, accelerator capability class) — never from brand or model
/// text, which the function does not accept.
pub fn family_ref(platform_family: &str, abi: &str, api_level: u32, capability_class: &str) -> String {
    let doc = json!({
        "protocol": "mycelium.mobile_family_ref.v1",
        "platform_family": platform_family,
        "abi": abi,
        "api_level": api_level,
        "capability_class": capability_class,
    });
    let digest = canonical::digest(&doc).expect("fixed-shape document is canonical");
    format!("family:{}", &digest[7..39])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn product_path_guard_mirrors_a12() {
        assert!(require_product_path("signed_native_application").is_ok());
        assert!(require_product_path("embedded_iroh_transport").is_ok());
        for p in EXCLUDED_PRODUCT_PATHS {
            assert_eq!(require_product_path(p).unwrap_err().0, "excluded_development_path_cannot_qualify");
        }
        assert_eq!(require_product_path("sideload").unwrap_err().0, "product_path_kind_invalid");
    }

    #[test]
    fn observation_is_closed_native_and_unknown_is_never_a_number() {
        let mut metrics = Metrics::new();
        metrics.insert("thermal_status".into(), Some(1.0));
        metrics.insert("battery_temperature_c".into(), None);
        let o = Observation {
            platform_family: "android",
            runtime_build_digest: &format!("sha256:{}", "a".repeat(64)),
            profile_generation: 1,
            evidence_generation: 1,
            observed_at_unix_ms: 1.0,
            measurement_duration_ms: 10.0,
            sample_count: 3,
            authority: "mycelium_node_app",
            metrics: &metrics,
        };
        let doc = runtime_observation(&o).unwrap();
        assert_eq!(doc.as_object().unwrap().len(), 11);
        assert_eq!(doc["source_kind"], NATIVE_SOURCE_KIND);
        assert_eq!(doc["metrics"]["battery_temperature_c"], "unknown");

        let mut bad = metrics.clone();
        bad.insert("Thermal".into(), Some(1.0));
        assert!(runtime_observation(&Observation { metrics: &bad, ..o }).is_err());
        let mut nan = metrics.clone();
        nan.insert("x".into(), Some(f64::NAN));
        assert!(runtime_observation(&Observation { metrics: &nan, ..o }).is_err());
        assert!(runtime_observation(&Observation { platform_family: "termux", ..o }).is_err());
    }

    #[test]
    fn family_ref_matches_the_a12_pattern_and_is_stable() {
        let r = family_ref("android", "arm64-v8a", 36, "gpu_vulkan_1_3");
        assert!(r.starts_with("family:") && r.len() == 7 + 32);
        assert_eq!(r, family_ref("android", "arm64-v8a", 36, "gpu_vulkan_1_3"));
        assert_ne!(r, family_ref("android", "arm64-v8a", 35, "gpu_vulkan_1_3"));
    }
}
