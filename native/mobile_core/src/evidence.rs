// SPDX-License-Identifier: AGPL-3.0-only
//! Native evidence records the A12 gate consumes
//! (`mycelium_mobile/a12_activation.py`): the closed
//! `mycelium.mobile_runtime_observation.v1` document, the undivided product path
//! and pseudonymous device-family references. Missing or denied platform data is
//! the string `unknown`, never a fabricated number.

use std::collections::BTreeMap;
use std::path::Path;

use serde_json::{Map, Value, json};

use crate::canonical;
use crate::identity::Identity;

pub const RUNTIME_OBSERVATION_PROTOCOL: &str = "mycelium.mobile_runtime_observation.v1";
pub const NATIVE_SOURCE_KIND: &str = "native_device_observation";
pub const PRODUCT_PATH_KINDS: [&str; 2] = ["signed_native_application", "embedded_iroh_transport"];
pub const EXCLUDED_PRODUCT_PATHS: [&str; 3] = ["termux", "adb", "ssh"];
pub const MOBILE_PLATFORM_FAMILIES: [&str; 3] = ["android", "ios", "ipados"];
pub const METRIC_COUNT_CEILING: usize = 256;
pub const APPLE_INITIAL_ELIGIBILITY: &str = "foreground_active_only";
/// The runtime authority a native node-app observation is attributed to.
pub const OBSERVATION_AUTHORITY: &str = "mycelium_node_app";
/// The member-signed envelope shape (`mycelium_membership/contracts.py`).
pub const SIGNED_MESSAGE_PROTOCOL: &str = "mycelium.membership.signed_message.v1";

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

/// Sign one closed runtime observation with the member's durable key.
///
/// The envelope is the A9 signed-message shape the A12 gate verifies
/// (`observe_mobile_runtime`): only a document this module would emit, carrying
/// native source kind and the node-app authority, can be signed.
pub fn sign_observation(identity: &Identity, observation: &Value) -> Result<Value, EvidenceError> {
    if observation["protocol"] != RUNTIME_OBSERVATION_PROTOCOL
        || observation["source_kind"] != NATIVE_SOURCE_KIND
        || observation["authority"] != OBSERVATION_AUTHORITY
        || observation.as_object().map(|o| o.len()) != Some(11)
    {
        return Err(EvidenceError("mobile_observation_unsignable"));
    }
    let signature = identity.sign(observation).map_err(|_| EvidenceError("mobile_observation_signature_failed"))?;
    Ok(json!({
        "protocol": SIGNED_MESSAGE_PROTOCOL,
        "message": observation,
        "signature": signature,
        "verification_key": identity.public_key_record(),
    }))
}

/// The bundle the owner shares off the device after an A12 gate: only the sealed,
/// member-signed runtime observations the driver consumes, never keys or state.
pub const EVIDENCE_EXPORT_PROTOCOL: &str = "mycelium.mobile_evidence_export.v1";
const OBSERVATION_FILE_PREFIX: &str = "runtime-observation-";

/// Collect every sealed `runtime-observation-*.json` in `evidence_dir` (sorted by
/// name) into one export bundle bound to the app's build. Anything that is not a
/// regular file holding a signed native observation fails the whole export.
pub fn export_bundle(evidence_dir: &Path, runtime_build_digest: &str) -> Result<Value, EvidenceError> {
    if runtime_build_digest.len() != 71 || !runtime_build_digest.starts_with("sha256:") {
        return Err(EvidenceError("mobile_evidence_export_build_invalid"));
    }
    let entries = std::fs::read_dir(evidence_dir).map_err(|_| EvidenceError("mobile_evidence_export_empty"))?;
    let mut names: Vec<String> = entries
        .filter_map(|e| e.ok())
        .filter_map(|e| e.file_name().into_string().ok())
        .filter(|n| n.starts_with(OBSERVATION_FILE_PREFIX) && n.ends_with(".json"))
        .collect();
    names.sort();
    if names.is_empty() {
        return Err(EvidenceError("mobile_evidence_export_empty"));
    }
    let mut observations = Vec::with_capacity(names.len());
    for name in names {
        let path = evidence_dir.join(&name);
        let meta = std::fs::symlink_metadata(&path).map_err(|_| EvidenceError("mobile_evidence_export_not_regular"))?;
        if !meta.file_type().is_file() {
            return Err(EvidenceError("mobile_evidence_export_not_regular"));
        }
        let bytes = std::fs::read(&path).map_err(|_| EvidenceError("mobile_evidence_export_not_regular"))?;
        let envelope: Value =
            serde_json::from_slice(&bytes).map_err(|_| EvidenceError("mobile_evidence_export_envelope_invalid"))?;
        if envelope["protocol"] != SIGNED_MESSAGE_PROTOCOL || envelope["message"]["protocol"] != RUNTIME_OBSERVATION_PROTOCOL {
            return Err(EvidenceError("mobile_evidence_export_envelope_invalid"));
        }
        observations.push(json!({"name": name, "envelope": envelope}));
    }
    Ok(json!({
        "protocol": EVIDENCE_EXPORT_PROTOCOL,
        "runtime_build_digest": runtime_build_digest,
        "observations": observations,
    }))
}

fn read_trimmed(path: &Path) -> Option<String> {
    std::fs::read_to_string(path).ok().map(|s| s.trim().to_owned())
}

/// Best-effort native platform metrics from the Linux/Android procfs and sysfs
/// under `root` (`/` on the device). A source the OS does not expose to the app,
/// or that does not parse, is `None` and serializes as `unknown` — never a
/// fabricated number.
pub fn platform_metrics(root: &Path) -> Metrics {
    let mut metrics = Metrics::new();
    let memory = read_trimmed(&root.join("proc/meminfo")).and_then(|text| {
        text.lines()
            .find(|line| line.starts_with("MemAvailable:"))
            .and_then(|line| line.split_whitespace().nth(1))
            .and_then(|kb| kb.parse::<f64>().ok())
            .map(|kb| kb * 1024.0)
    });
    metrics.insert("memory_available_bytes".into(), memory);
    let battery = root.join("sys/class/power_supply/battery");
    metrics.insert(
        "battery_level_percent".into(),
        read_trimmed(&battery.join("capacity")).and_then(|v| v.parse::<f64>().ok()),
    );
    metrics.insert(
        "charging".into(),
        read_trimmed(&battery.join("status")).map(|s| if s == "Charging" || s == "Full" { 1.0 } else { 0.0 }),
    );
    let thermal = std::fs::read_dir(root.join("sys/class/thermal")).ok().and_then(|entries| {
        entries
            .filter_map(Result::ok)
            .filter(|e| e.file_name().to_string_lossy().starts_with("thermal_zone"))
            .filter_map(|e| read_trimmed(&e.path().join("temp")).and_then(|v| v.parse::<f64>().ok()))
            .map(|milli| milli / 1000.0)
            .fold(None, |acc: Option<f64>, v| Some(acc.map_or(v, |a| a.max(v))))
    });
    metrics.insert("thermal_zone_max_celsius".into(), thermal);
    metrics.insert("thermal_max_celsius".into(), thermal);
    metrics
}

/// A reading the app shell sampled from OS APIs is used for at most this long.
pub const SHELL_SAMPLE_MAX_AGE_S: f64 = 180.0;

/// Readings the app shell may supply, with the range a real reading falls in:
/// * `battery_level_percent` — sticky `ACTION_BATTERY_CHANGED` level/scale × 100;
/// * `charging` — 1 when the battery status is charging or full, else 0;
/// * `battery_temperature_celsius` — `BatteryManager.EXTRA_TEMPERATURE` / 10;
/// * `thermal_status` — `PowerManager.getCurrentThermalStatus()` (0 none … 6 shutdown);
/// * `thermal_headroom` — `PowerManager.getThermalHeadroom(0)` (1.0 = severe throttling).
pub const SHELL_METRICS: [(&str, f64, f64); 5] = [
    ("battery_level_percent", 0.0, 100.0),
    ("charging", 0.0, 1.0),
    ("battery_temperature_celsius", -40.0, 150.0),
    ("thermal_status", 0.0, 6.0),
    ("thermal_headroom", 0.0, 100.0),
];

/// Combine the procfs/sysfs readings with the OS readings the app shell sampled
/// (Android `BatteryManager` / `PowerManager`; an unprivileged app usually cannot
/// read the sysfs battery and thermal nodes). A shell reading outside its range
/// is dropped, never clamped. `thermal_max_celsius` is the hottest temperature in
/// degrees Celsius among the sensors the app can read: the battery temperature
/// and any readable `/sys/class/thermal` zone. It is not a skin or SoC
/// temperature when no zone is readable, and the unitless thermal status and
/// headroom never feed it. Anything with no reading stays unknown.
pub fn merge_platform_metrics(mut base: Metrics, shell: Option<&Metrics>) -> Metrics {
    if let Some(shell) = shell {
        for (name, low, high) in SHELL_METRICS {
            if let Some(Some(v)) = shell.get(name)
                && v.is_finite()
                && (low..=high).contains(v)
            {
                base.insert(name.into(), Some(*v));
            } else {
                base.entry(name.into()).or_insert(None);
            }
        }
    }
    let thermal = ["thermal_zone_max_celsius", "battery_temperature_celsius"]
        .iter()
        .filter_map(|name| base.get(*name).copied().flatten())
        .fold(None, |acc: Option<f64>, v| Some(acc.map_or(v, |a| a.max(v))));
    base.insert("thermal_max_celsius".into(), thermal);
    base
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

    fn observation_doc(metrics: &Metrics) -> Value {
        runtime_observation(&Observation {
            platform_family: "android",
            runtime_build_digest: &format!("sha256:{}", "b".repeat(64)),
            profile_generation: 2,
            evidence_generation: 5,
            observed_at_unix_ms: 1_000.0,
            measurement_duration_ms: 60_000.0,
            sample_count: 12,
            authority: OBSERVATION_AUTHORITY,
            metrics,
        })
        .unwrap()
    }

    #[test]
    fn signed_observation_verifies_with_the_member_key_only() {
        let identity = Identity::generate("member-endpoint").unwrap();
        let doc = observation_doc(&Metrics::new());
        let envelope = sign_observation(&identity, &doc).unwrap();
        assert_eq!(envelope["protocol"], SIGNED_MESSAGE_PROTOCOL);
        assert_eq!(envelope["message"], doc);
        crate::identity::verify(&envelope["message"], &envelope["signature"], &envelope["verification_key"]).unwrap();
        let mut tampered = envelope["message"].clone();
        tampered["sample_count"] = json!(13);
        assert!(crate::identity::verify(&tampered, &envelope["signature"], &envelope["verification_key"]).is_err());
        let mut foreign = doc.clone();
        foreign["authority"] = json!("operator");
        assert_eq!(sign_observation(&identity, &foreign).unwrap_err().0, "mobile_observation_unsignable");
        let mut synthetic = doc.clone();
        synthetic["source_kind"] = json!("synthetic_conformance");
        assert!(sign_observation(&identity, &synthetic).is_err());
    }

    #[test]
    fn platform_metrics_read_procfs_and_sysfs_and_leave_the_rest_unknown() {
        let root = std::env::temp_dir().join(format!("mycelium-evidence-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(root.join("proc")).unwrap();
        std::fs::write(root.join("proc/meminfo"), "MemTotal: 100 kB\nMemAvailable:   2048 kB\n").unwrap();
        std::fs::create_dir_all(root.join("sys/class/thermal/thermal_zone0")).unwrap();
        std::fs::create_dir_all(root.join("sys/class/thermal/thermal_zone3")).unwrap();
        std::fs::write(root.join("sys/class/thermal/thermal_zone0/temp"), "41000\n").unwrap();
        std::fs::write(root.join("sys/class/thermal/thermal_zone3/temp"), "47500\n").unwrap();
        let metrics = platform_metrics(&root);
        assert_eq!(metrics["memory_available_bytes"], Some(2048.0 * 1024.0));
        assert_eq!(metrics["thermal_max_celsius"], Some(47.5));
        assert_eq!(metrics["battery_level_percent"], None);
        assert_eq!(metrics["charging"], None);
        let doc = observation_doc(&metrics);
        assert_eq!(doc["metrics"]["battery_level_percent"], "unknown");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn shell_readings_fill_power_and_thermal_and_out_of_range_stays_unknown() {
        // No readable sysfs (an unprivileged Android app): everything unknown.
        let empty = std::env::temp_dir().join(format!("mycelium-evidence-none-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&empty);
        std::fs::create_dir_all(&empty).unwrap();
        let base = platform_metrics(&empty);
        assert_eq!(merge_platform_metrics(base.clone(), None)["thermal_max_celsius"], None);

        let mut shell = Metrics::new();
        shell.insert("battery_level_percent".into(), Some(87.0));
        shell.insert("charging".into(), Some(1.0));
        shell.insert("battery_temperature_celsius".into(), Some(31.4));
        shell.insert("thermal_status".into(), Some(0.0));
        shell.insert("thermal_headroom".into(), None);
        shell.insert("memory_available_bytes".into(), Some(1.0)); // not a shell metric
        let merged = merge_platform_metrics(base.clone(), Some(&shell));
        assert_eq!(merged["battery_level_percent"], Some(87.0));
        assert_eq!(merged["charging"], Some(1.0));
        assert_eq!(merged["battery_temperature_celsius"], Some(31.4));
        assert_eq!(merged["thermal_max_celsius"], Some(31.4));
        assert_eq!(merged["thermal_status"], Some(0.0));
        assert_eq!(merged["thermal_headroom"], None);
        assert_eq!(merged["memory_available_bytes"], None);
        let doc = observation_doc(&merged);
        assert_eq!(doc["metrics"]["thermal_max_celsius"], 31.4);
        assert_eq!(doc["metrics"]["charging"], 1.0);
        assert_eq!(doc["metrics"]["thermal_headroom"], "unknown");

        // Out-of-range readings are dropped, not clamped.
        let mut bad = shell.clone();
        bad.insert("battery_level_percent".into(), Some(140.0));
        bad.insert("battery_temperature_celsius".into(), Some(f64::NAN));
        let merged = merge_platform_metrics(base, Some(&bad));
        assert_eq!(merged["battery_level_percent"], None);
        assert_eq!(merged["battery_temperature_celsius"], None);
        assert_eq!(merged["thermal_max_celsius"], None);

        // A readable thermal zone hotter than the battery wins.
        std::fs::create_dir_all(empty.join("sys/class/thermal/thermal_zone1")).unwrap();
        std::fs::write(empty.join("sys/class/thermal/thermal_zone1/temp"), "52000\n").unwrap();
        let merged = merge_platform_metrics(platform_metrics(&empty), Some(&shell));
        assert_eq!(merged["thermal_zone_max_celsius"], Some(52.0));
        assert_eq!(merged["thermal_max_celsius"], Some(52.0));
        let _ = std::fs::remove_dir_all(&empty);
    }

    #[test]
    fn family_ref_matches_the_a12_pattern_and_is_stable() {
        let r = family_ref("android", "arm64-v8a", 36, "gpu_vulkan_1_3");
        assert!(r.starts_with("family:") && r.len() == 7 + 32);
        assert_eq!(r, family_ref("android", "arm64-v8a", 36, "gpu_vulkan_1_3"));
        assert_ne!(r, family_ref("android", "arm64-v8a", 35, "gpu_vulkan_1_3"));
    }

    fn export_dir(tag: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("mycelium-export-{}-{}", tag, std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn export_bundle_carries_only_signed_runtime_observations_in_order() {
        let dir = export_dir("only");
        let identity = Identity::generate("member-endpoint").unwrap();
        let envelope = sign_observation(&identity, &observation_doc(&Metrics::new())).unwrap();
        let bytes = serde_json::to_vec(&envelope).unwrap();
        std::fs::write(dir.join("runtime-observation-000002.json"), &bytes).unwrap();
        std::fs::write(dir.join("runtime-observation-000001.json"), &bytes).unwrap();
        // Private and unrelated state next to the evidence never leaves the device.
        std::fs::write(dir.join("member.key"), b"secret").unwrap();
        std::fs::write(dir.join("notes.json"), b"{}").unwrap();
        let build = format!("sha256:{}", "b".repeat(64));
        let bundle = export_bundle(&dir, &build).unwrap();
        assert_eq!(bundle["protocol"], EVIDENCE_EXPORT_PROTOCOL);
        assert_eq!(bundle["runtime_build_digest"], build);
        let items = bundle["observations"].as_array().unwrap();
        assert_eq!(items.len(), 2);
        assert_eq!(items[0]["name"], "runtime-observation-000001.json");
        assert_eq!(items[1]["name"], "runtime-observation-000002.json");
        assert_eq!(items[0]["envelope"], envelope);
        assert_eq!(bundle.as_object().unwrap().len(), 3);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn export_bundle_fails_closed_on_foreign_or_unsigned_documents() {
        let build = format!("sha256:{}", "b".repeat(64));
        let dir = export_dir("foreign");
        std::fs::write(dir.join("runtime-observation-000001.json"), b"{\"protocol\":\"x\"}").unwrap();
        assert_eq!(export_bundle(&dir, &build).unwrap_err().0, "mobile_evidence_export_envelope_invalid");
        let _ = std::fs::remove_dir_all(&dir);

        let dir = export_dir("empty");
        assert_eq!(export_bundle(&dir, &build).unwrap_err().0, "mobile_evidence_export_empty");
        let _ = std::fs::remove_dir_all(&dir);

        let dir = export_dir("build");
        assert_eq!(export_bundle(&dir, "sha256:short").unwrap_err().0, "mobile_evidence_export_build_invalid");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn export_bundle_refuses_symlinked_observations() {
        let build = format!("sha256:{}", "b".repeat(64));
        let dir = export_dir("link");
        let identity = Identity::generate("member-endpoint").unwrap();
        let envelope = sign_observation(&identity, &observation_doc(&Metrics::new())).unwrap();
        std::fs::write(dir.join("elsewhere.json"), serde_json::to_vec(&envelope).unwrap()).unwrap();
        std::os::unix::fs::symlink(dir.join("elsewhere.json"), dir.join("runtime-observation-000001.json")).unwrap();
        assert_eq!(export_bundle(&dir, &build).unwrap_err().0, "mobile_evidence_export_not_regular");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
