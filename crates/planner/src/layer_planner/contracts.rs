// SPDX-License-Identifier: AGPL-3.0-only
//! Snapshot planner contracts (`mycelium_layer_planner/contracts.py`, `workload.py`,
//! `physical_graph.py`, `network_cost.py`) at f4e366e9.
//!
//! Dataclass construction in the oracle validates with `ValueError` texts; wrong-typed
//! or unknown/missing keyword arguments are `TypeError` (`Kind::Other`). Fields whose
//! original int/float spelling is echoed into plan output are kept as `py::Num`.
use crate::py::{Failure, Num, PyResult, float, num, py_repr, py_str, verr};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;

pub const NUMERIC_EPSILON: f64 = 1e-12;

pub fn other<T>(message: impl Into<String>) -> PyResult<T> {
    Err(Failure::other(message))
}

pub fn num_value(n: Num) -> Value {
    match n {
        Num::Int(i) => i64::try_from(i).map(|i| json!(i)).unwrap_or(Value::Null),
        Num::Float(f) => json!(f),
    }
}

pub fn num_f(n: Num) -> f64 {
    match n {
        Num::Int(i) => i as f64,
        Num::Float(f) => f,
    }
}

pub fn num_mul(a: Num, b: Num) -> Num {
    match (a, b) {
        (Num::Int(x), Num::Int(y)) => Num::Int(x * y),
        _ => Num::Float(num_f(a) * num_f(b)),
    }
}

pub fn num_add(a: Num, b: Num) -> Num {
    match (a, b) {
        (Num::Int(x), Num::Int(y)) => Num::Int(x + y),
        _ => Num::Float(num_f(a) + num_f(b)),
    }
}

/// Python `sum()` of floats (3.12+: Neumaier-compensated; the first add is exact).
pub fn py_sum(values: impl IntoIterator<Item = f64>) -> f64 {
    let mut f = 0.0_f64;
    let mut c = 0.0_f64;
    for x in values {
        let t = f + x;
        if f.abs() >= x.abs() {
            c += (f - t) + x;
        } else {
            c += (x - t) + f;
        }
        f = t;
    }
    if c != 0.0 && c.is_finite() {
        f += c;
    }
    f
}

/// Python `format(x, '.12g')`.
pub fn format_g12(x: f64) -> String {
    if x.is_nan() {
        return "nan".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf".into() } else { "-inf".into() };
    }
    if x == 0.0 {
        return if x.is_sign_negative() {
            "-0".into()
        } else {
            "0".into()
        };
    }
    let sci = format!("{:.11e}", x);
    let (mantissa, exp) = sci.split_once('e').unwrap_or((&sci, "0"));
    let exp: i32 = exp.parse().unwrap_or(0);
    if (-4..12).contains(&exp) {
        let decimals = (11 - exp).max(0) as usize;
        let fixed = format!("{x:.decimals$}");
        let trimmed = if fixed.contains('.') {
            fixed.trim_end_matches('0').trim_end_matches('.').to_owned()
        } else {
            fixed
        };
        return trimmed;
    }
    let mantissa = if mantissa.contains('.') {
        mantissa.trim_end_matches('0').trim_end_matches('.')
    } else {
        mantissa
    };
    format!(
        "{mantissa}e{}{:02}",
        if exp < 0 { '-' } else { '+' },
        exp.abs()
    )
}

/// Python iteration over a JSON-shaped value: lists by element, strings by character,
/// mappings by key. Anything else is a `TypeError`.
pub fn py_iter(v: &Value) -> PyResult<Vec<Value>> {
    match v {
        Value::Array(a) => Ok(a.clone()),
        Value::String(s) => Ok(s.chars().map(|c| json!(c.to_string())).collect()),
        Value::Object(m) => Ok(m.keys().map(|k| json!(k)).collect()),
        _ => other("object is not iterable"),
    }
}

// ---------------------------------------------------------------- keyword plumbing
fn kwargs<'a>(
    v: &'a Value,
    class: &str,
    required: &[&str],
    optional: &[&str],
) -> PyResult<&'a Map<String, Value>> {
    let Some(map) = v.as_object() else {
        return other(format!("{class}(**non-mapping)"));
    };
    for key in map.keys() {
        if !required.contains(&key.as_str()) && !optional.contains(&key.as_str()) {
            return other(format!(
                "{class}() got an unexpected keyword argument '{key}'"
            ));
        }
    }
    for key in required {
        if !map.contains_key(*key) {
            return other(format!("{class}() missing required argument '{key}'"));
        }
    }
    Ok(map)
}

fn text(map: &Map<String, Value>, key: &str) -> PyResult<String> {
    match map.get(key) {
        Some(Value::String(s)) => Ok(s.clone()),
        _ => other(format!("{key} must be a string")),
    }
}

fn number(map: &Map<String, Value>, key: &str) -> PyResult<Num> {
    if let Some(Value::Bool(b)) = map.get(key) {
        return Ok(Num::Int(i128::from(*b)));
    }
    match map.get(key).and_then(num) {
        Some(n) => Ok(n),
        None => other(format!("{key} must be numeric")),
    }
}

fn real(map: &Map<String, Value>, key: &str) -> PyResult<f64> {
    Ok(num_f(number(map, key)?))
}

pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => {
            crate::py::number(n) != Num::Int(0) && crate::py::number(n) != Num::Float(0.0)
        }
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => !m.is_empty(),
    }
}

fn finite_nonneg(name: &str, v: f64) -> PyResult<()> {
    if !v.is_finite() || v < 0.0 {
        return verr(format!("{name} must be finite and non-negative"));
    }
    Ok(())
}

// ---------------------------------------------------------------- model identity
#[derive(Debug, Clone)]
pub struct ModelIdentity {
    pub model_id: Value,
    pub revision: Value,
    pub weight_digest: String,
    pub architecture: Value,
    pub num_layers: Num,
    pub hidden_size: Num,
    pub dtype_bytes: Num,
    pub kv_heads: Num,
    pub head_dim: Num,
    pub weight_bytes: Num,
    /// Original JSON spelling of the six numeric fields (the oracle echoes them).
    pub echo: Map<String, Value>,
}

impl ModelIdentity {
    pub fn parse(v: &Value) -> PyResult<Self> {
        const FIELDS: [&str; 10] = [
            "model_id",
            "revision",
            "weight_digest",
            "architecture",
            "num_layers",
            "hidden_size",
            "dtype_bytes",
            "kv_heads",
            "head_dim",
            "weight_bytes",
        ];
        let map = kwargs(v, "ModelIdentity", &FIELDS, &[])?;
        let model_id = map["model_id"].clone();
        let revision = map["revision"].clone();
        if !truthy(&model_id) || !truthy(&revision) || !truthy(&map["weight_digest"]) {
            return verr("model identity requires id, immutable revision, and digest");
        }
        let weight_digest = text(map, "weight_digest")?;
        if !weight_digest.starts_with("sha256:") || weight_digest.chars().count() != 71 {
            return verr("weight_digest must be sha256:<64 hex characters>");
        }
        if weight_digest
            .chars()
            .skip(7)
            .any(|c| !c.is_ascii_hexdigit())
        {
            return verr("weight_digest must contain hexadecimal characters");
        }
        let mut out = Self {
            model_id,
            revision,
            weight_digest,
            architecture: map["architecture"].clone(),
            num_layers: number(map, "num_layers")?,
            hidden_size: number(map, "hidden_size")?,
            dtype_bytes: number(map, "dtype_bytes")?,
            kv_heads: number(map, "kv_heads")?,
            head_dim: number(map, "head_dim")?,
            weight_bytes: number(map, "weight_bytes")?,
            echo: [
                "num_layers",
                "hidden_size",
                "dtype_bytes",
                "kv_heads",
                "head_dim",
                "weight_bytes",
            ]
            .iter()
            .map(|k| ((*k).to_owned(), map[*k].clone()))
            .collect(),
        };
        for (name, value) in [
            ("num_layers", out.num_layers),
            ("hidden_size", out.hidden_size),
            ("dtype_bytes", out.dtype_bytes),
            ("kv_heads", out.kv_heads),
            ("head_dim", out.head_dim),
            ("weight_bytes", out.weight_bytes),
        ] {
            if num_f(value) <= 0.0 {
                return verr(format!("{name} must be positive"));
            }
        }
        out.architecture = map["architecture"].clone();
        Ok(out)
    }

    pub fn layers(&self) -> i64 {
        num_f(self.num_layers) as i64
    }

    pub fn weight_bytes_per_layer(&self) -> f64 {
        num_f(self.weight_bytes) / num_f(self.num_layers)
    }

    pub fn kv_bytes_per_layer_token(&self) -> Num {
        num_mul(
            num_mul(num_mul(Num::Int(2), self.kv_heads), self.head_dim),
            self.dtype_bytes,
        )
    }

    pub fn activation_bytes(&self, tokens: Num) -> PyResult<Num> {
        if num_f(tokens) < 0.0 {
            return verr("tokens must be non-negative and batch positive");
        }
        Ok(num_mul(
            num_mul(num_mul(tokens, Num::Int(1)), self.hidden_size),
            self.dtype_bytes,
        ))
    }

    pub fn to_value(&self) -> Value {
        json!({
            "model_id": self.model_id,
            "revision": self.revision,
            "weight_digest": self.weight_digest,
            "architecture": self.architecture,
            "num_layers": self.echo["num_layers"],
            "hidden_size": self.echo["hidden_size"],
            "dtype_bytes": self.echo["dtype_bytes"],
            "kv_heads": self.echo["kv_heads"],
            "head_dim": self.echo["head_dim"],
            "weight_bytes": self.echo["weight_bytes"],
        })
    }
}

// ---------------------------------------------------------------- nodes and links
#[derive(Debug, Clone)]
pub struct NodeCapability {
    pub node_id: String,
    pub prefill_ms_per_layer_token: f64,
    pub decode_ms_per_layer_token: f64,
    pub fast_memory_bytes: f64,
    pub total_memory_bytes: f64,
    pub memory_bandwidth_bps: f64,
    pub spill_bandwidth_bps: f64,
    pub eligible: bool,
    pub exclusion_reason: Value,
    pub region: Value,
    pub workspace_bytes: f64,
    pub calibration_confidence: f64,
    /// The JSON spelling was an integer (Python `min()` echoes the original object).
    pub calibration_confidence_int: bool,
}

impl NodeCapability {
    pub fn parse(v: &Value) -> PyResult<Self> {
        let map = kwargs(
            v,
            "NodeCapability",
            &[
                "node_id",
                "prefill_ms_per_layer_token",
                "decode_ms_per_layer_token",
                "fast_memory_bytes",
                "total_memory_bytes",
                "memory_bandwidth_Bps",
                "spill_bandwidth_Bps",
            ],
            &[
                "eligible",
                "exclusion_reason",
                "region",
                "latitude",
                "longitude",
                "workspace_bytes",
                "calibration_confidence",
            ],
        )?;
        // Python validates in `__post_init__` order: identity first, then each
        // numeric field in turn (a wrong type raises where the comparison happens).
        if !truthy(&map["node_id"]) {
            return verr("node_id is required");
        }
        let node_id = text(map, "node_id")?;
        let mut numbers = Vec::new();
        for (name, default) in [
            ("prefill_ms_per_layer_token", None),
            ("decode_ms_per_layer_token", None),
            ("fast_memory_bytes", None),
            ("total_memory_bytes", None),
            ("memory_bandwidth_Bps", None),
            ("spill_bandwidth_Bps", None),
            ("workspace_bytes", Some(0.0)),
        ] {
            let value = if map.contains_key(name) {
                real(map, name)?
            } else {
                default.unwrap_or(0.0)
            };
            finite_nonneg(name, value)?;
            numbers.push(value);
        }
        let confidence = if map.contains_key("calibration_confidence") {
            real(map, "calibration_confidence")?
        } else {
            1.0
        };
        if numbers[3] < numbers[2] {
            return verr("total memory cannot be smaller than fast memory");
        }
        if !(0.0..=1.0).contains(&confidence) {
            return verr("calibration_confidence must be in [0, 1]");
        }
        Ok(Self {
            node_id,
            prefill_ms_per_layer_token: numbers[0],
            decode_ms_per_layer_token: numbers[1],
            fast_memory_bytes: numbers[2],
            total_memory_bytes: numbers[3],
            memory_bandwidth_bps: numbers[4],
            spill_bandwidth_bps: numbers[5],
            eligible: map.get("eligible").is_none_or(truthy),
            exclusion_reason: map.get("exclusion_reason").cloned().unwrap_or(Value::Null),
            region: map
                .get("region")
                .cloned()
                .unwrap_or_else(|| json!("unknown")),
            workspace_bytes: numbers[6],
            calibration_confidence: confidence,
            calibration_confidence_int: matches!(
                map.get("calibration_confidence").and_then(num),
                Some(Num::Int(_))
            ),
        })
    }
}

#[derive(Debug, Clone)]
pub struct Link {
    pub src: String,
    pub dst: String,
    pub rtt_ms: f64,
    pub jitter_ms: f64,
    pub bandwidth_bps: Option<f64>,
    pub loss_ratio: f64,
    pub geolocation_floor_ms: f64,
    pub inferred: bool,
    pub stale: bool,
    pub confidence: f64,
    pub confidence_int: bool,
}

impl Link {
    pub fn parse(v: &Value) -> PyResult<Self> {
        let map = kwargs(
            v,
            "DirectedLinkObservation",
            &["src", "dst", "rtt_ms", "jitter_ms", "bandwidth_Bps"],
            &[
                "loss_ratio",
                "geolocation_floor_ms",
                "measured_at",
                "inferred",
                "stale",
                "confidence",
            ],
        )?;
        if !truthy(&map["src"]) || !truthy(&map["dst"]) || map["src"] == map["dst"] {
            return verr("directed link requires distinct source and destination");
        }
        let get_or = |key: &str, default: f64| -> PyResult<f64> {
            if map.contains_key(key) {
                real(map, key)
            } else {
                Ok(default)
            }
        };
        let mut numbers = Vec::new();
        for (name, required) in [
            ("rtt_ms", true),
            ("jitter_ms", true),
            ("loss_ratio", false),
            ("geolocation_floor_ms", false),
        ] {
            let value = if required {
                real(map, name)?
            } else {
                get_or(name, 0.0)?
            };
            finite_nonneg(name, value)?;
            numbers.push(value);
        }
        let bandwidth_bps = match &map["bandwidth_Bps"] {
            Value::Null => None,
            Value::Bool(b) => Some(f64::from(u8::from(*b))),
            other => {
                Some(float(other).ok_or_else(|| Failure::other("bandwidth_Bps must be numeric"))?)
            }
        };
        if bandwidth_bps.is_some_and(|b| b <= 0.0) {
            return verr("bandwidth must be positive when present");
        }
        let confidence = get_or("confidence", 1.0)?;
        if numbers[2] > 1.0 || !(0.0..=1.0).contains(&confidence) {
            return verr("loss_ratio and confidence must be in [0, 1]");
        }
        Ok(Self {
            src: text(map, "src")?,
            dst: text(map, "dst")?,
            rtt_ms: numbers[0],
            jitter_ms: numbers[1],
            bandwidth_bps,
            loss_ratio: numbers[2],
            geolocation_floor_ms: numbers[3],
            inferred: map.get("inferred").is_some_and(truthy),
            stale: map.get("stale").is_some_and(truthy),
            confidence,
            confidence_int: matches!(map.get("confidence").and_then(num), Some(Num::Int(_))),
        })
    }
}

// ---------------------------------------------------------------- workload
#[derive(Debug, Clone)]
pub struct Scenario {
    pub name: String,
    pub prompt_tokens: Num,
    pub output_tokens: Num,
    pub concurrency: Num,
    pub probability: Option<f64>,
    pub user_scale: Num,
    pub user_scale_raw: Value,
    pub system_prefix_tokens: Num,
    pub history_tokens: Num,
}

impl Scenario {
    pub fn parse(v: &Value) -> PyResult<Self> {
        let map = kwargs(
            v,
            "WorkloadScenario",
            &["name", "prompt_tokens", "output_tokens", "concurrency"],
            &[
                "probability",
                "user_scale",
                "arrival_rate_rps",
                "system_prefix_tokens",
                "history_tokens",
                "prompt_p95_tokens",
                "output_p95_tokens",
                "batch_size",
                "qos_class",
            ],
        )?;
        let opt_num = |key: &str| -> PyResult<Option<Num>> {
            match map.get(key) {
                None | Some(Value::Null) => Ok(None),
                Some(_) => Ok(Some(number(map, key)?)),
            }
        };
        let or_int = |key: &str, default: i128| -> PyResult<Num> {
            if map.contains_key(key) {
                number(map, key)
            } else {
                Ok(Num::Int(default))
            }
        };
        let out = Self {
            name: text(map, "name")?,
            prompt_tokens: number(map, "prompt_tokens")?,
            output_tokens: number(map, "output_tokens")?,
            concurrency: number(map, "concurrency")?,
            probability: opt_num("probability")?.map(num_f),
            user_scale: if map.contains_key("user_scale") {
                number(map, "user_scale")?
            } else {
                Num::Float(1.0)
            },
            user_scale_raw: map.get("user_scale").cloned().unwrap_or_else(|| json!(1.0)),
            system_prefix_tokens: or_int("system_prefix_tokens", 0)?,
            history_tokens: or_int("history_tokens", 0)?,
        };
        let arrival = opt_num("arrival_rate_rps")?;
        let p95 = opt_num("prompt_p95_tokens")?;
        let o95 = opt_num("output_p95_tokens")?;
        let batch = or_int("batch_size", 1)?;
        let qos = match map.get("qos_class") {
            None => "interactive".to_owned(),
            Some(Value::String(s)) => s.clone(),
            Some(_) => return other("qos_class must be a string"),
        };
        if out.name.is_empty()
            || num_f(out.prompt_tokens) < 0.0
            || num_f(out.output_tokens) <= 0.0
            || num_f(out.concurrency) <= 0.0
        {
            return verr("invalid workload scenario");
        }
        if out.probability.is_some_and(|p| !(0.0..=1.0).contains(&p)) {
            return verr("probability must be in [0, 1]");
        }
        if num_f(out.user_scale) <= 0.0
            || num_f(out.system_prefix_tokens) < 0.0
            || num_f(out.history_tokens) < 0.0
        {
            return verr("invalid workload scaling");
        }
        if arrival.is_some_and(|a| num_f(a) < 0.0) {
            return verr("arrival rate must be non-negative");
        }
        if p95.is_some_and(|p| num_f(p) < num_f(out.prompt_tokens)) {
            return verr("prompt p95 must not be smaller than prompt p50");
        }
        if o95.is_some_and(|p| num_f(p) < num_f(out.output_tokens)) {
            return verr("output p95 must not be smaller than output p50");
        }
        if num_f(batch) <= 0.0 || !["interactive", "batch"].contains(&qos.as_str()) {
            return verr("invalid workload batch or QoS class");
        }
        Ok(out)
    }

    pub fn effective_prompt_tokens(&self) -> Num {
        num_add(
            num_add(self.prompt_tokens, self.system_prefix_tokens),
            self.history_tokens,
        )
    }

    pub fn total_context_tokens(&self) -> Num {
        num_add(self.effective_prompt_tokens(), self.output_tokens)
    }

    fn preset_scenario(
        name: String,
        prompt: i128,
        output: i128,
        concurrency: i128,
        user_scale: f64,
    ) -> Self {
        Self {
            name,
            prompt_tokens: Num::Int(prompt),
            output_tokens: Num::Int(output),
            concurrency: Num::Int(concurrency),
            probability: None,
            user_scale: Num::Float(user_scale),
            user_scale_raw: json!(user_scale),
            system_prefix_tokens: Num::Int(0),
            history_tokens: Num::Int(0),
        }
    }
}

#[derive(Debug, Clone)]
pub struct Profile {
    pub name: String,
    pub scenarios: Vec<Scenario>,
    pub mode: String,
    pub source: String,
    pub arrival_process: String,
}

/// `float(value)` of the oracle for JSON-shaped input.
pub fn py_float(v: &Value) -> PyResult<f64> {
    match v {
        Value::Number(_) => Ok(float(v).unwrap_or(f64::NAN)),
        Value::Bool(b) => Ok(f64::from(u8::from(*b))),
        Value::String(s) => s.trim().parse::<f64>().map_err(|_| {
            Failure::value(format!("could not convert string to float: {}", py_repr(v)))
        }),
        _ => other("float() argument must be a string or a real number"),
    }
}

/// `int(value)` of the oracle for JSON-shaped input.
pub fn py_int(v: &Value) -> PyResult<i128> {
    match v {
        Value::Number(_) => match num(v) {
            Some(Num::Int(i)) => Ok(i),
            Some(Num::Float(f)) if f.is_finite() => Ok(f.trunc() as i128),
            _ => verr("cannot convert float NaN or infinity to integer"),
        },
        Value::Bool(b) => Ok(i128::from(*b)),
        Value::String(s) => s.trim().parse::<i128>().map_err(|_| {
            Failure::value(format!(
                "invalid literal for int() with base 10: {}",
                py_repr(v)
            ))
        }),
        _ => other("int() argument must be a string or a real number"),
    }
}

impl Profile {
    pub fn parse(data: &Value) -> PyResult<Self> {
        if !data.is_object() {
            return other("workload.get on non-mapping");
        }
        let get = |key: &str| data.get(key);
        let preset = get("preset").unwrap_or(&Value::Null);
        let profile = if preset == &json!("interactive_chat_v1") {
            let user_scale = py_float(get("user_scale").unwrap_or(&json!(1.0)))?;
            let prefix = py_int(get("system_prefix_tokens").unwrap_or(&json!(0)))?;
            let history = py_int(get("history_tokens").unwrap_or(&json!(0)))?;
            let points: Vec<i128> = match get("concurrency_points") {
                None => vec![1, 2, 4, 8, 16, 32],
                Some(v) => py_iter(v)?.iter().map(py_int).collect::<PyResult<_>>()?,
            };
            let mut scenarios = Vec::new();
            for c in points {
                let mut s =
                    Scenario::preset_scenario(format!("interactive-c{c}"), 70, 215, c, user_scale);
                s.system_prefix_tokens = Num::Int(prefix);
                s.history_tokens = Num::Int(history);
                // The preset also fixes p95 70→512 / 215→512; validate as the dataclass does.
                if c <= 0 {
                    return verr("invalid workload scenario");
                }
                if user_scale <= 0.0 || prefix < 0 || history < 0 {
                    return verr("invalid workload scaling");
                }
                scenarios.push(s);
            }
            Self {
                name: "interactive_chat_v1".into(),
                scenarios,
                mode: "sensitivity_grid".into(),
                source: "LMSYS-Chat-1M".into(),
                arrival_process: "poisson_capacity_sweep".into(),
            }
        } else if preset == &json!("qa_benchmark_v1") {
            let user_scale = py_float(get("user_scale").unwrap_or(&json!(1.0)))?;
            if user_scale <= 0.0 {
                return verr("invalid workload scaling");
            }
            Self {
                name: "qa_benchmark_v1".into(),
                scenarios: [("qa-c1", 1), ("qa-c4", 4), ("qa-c16", 16)]
                    .iter()
                    .map(|(n, c)| Scenario::preset_scenario((*n).into(), 1024, 294, *c, user_scale))
                    .collect(),
                mode: "sensitivity_grid".into(),
                source: "MLPerf-Inference-OpenOrca".into(),
                arrival_process: "poisson_capacity_sweep".into(),
            }
        } else if !preset.is_null() {
            return verr(format!("unknown workload preset: {}", py_str(preset)));
        } else {
            let raw = match get("scenarios") {
                None => Vec::new(),
                Some(v) => py_iter(v)?,
            };
            let scenarios = raw
                .iter()
                .map(Scenario::parse)
                .collect::<PyResult<Vec<_>>>()?;
            let string = |key: &str, default: &str| -> String {
                get(key).map_or_else(|| default.to_owned(), py_str)
            };
            Self {
                name: string("name", "custom"),
                scenarios,
                mode: string("mode", "sensitivity_grid"),
                source: string("source", "user_configured"),
                arrival_process: string("arrival_process", "poisson_capacity_sweep"),
            }
        };
        profile.validate()?;
        Ok(profile)
    }

    fn validate(&self) -> PyResult<()> {
        if self.name.is_empty() || self.scenarios.is_empty() || self.source.is_empty() {
            return verr("workload profile requires name, scenarios, and source");
        }
        if !["probability", "sensitivity_grid"].contains(&self.mode.as_str()) {
            return verr("unknown workload mode");
        }
        if self.mode == "probability" {
            if self.scenarios.iter().any(|s| s.probability.is_none()) {
                return verr("probability profile requires every scenario probability");
            }
            let total = py_sum(self.scenarios.iter().map(|s| s.probability.unwrap_or(0.0)));
            if (total - 1.0).abs() > NUMERIC_EPSILON {
                return verr("scenario probabilities must sum to one");
            }
        } else if self.scenarios.iter().any(|s| s.probability.is_some()) {
            return verr("sensitivity grids must not imply probabilities");
        }
        Ok(())
    }

    /// The oracle's `max(..., key=(total_context_tokens * concurrency, name))`.
    pub fn representative(&self) -> &Scenario {
        let key = |s: &Scenario| {
            (
                num_f(num_mul(s.total_context_tokens(), s.concurrency)),
                s.name.clone(),
            )
        };
        let mut best = &self.scenarios[0];
        for s in &self.scenarios[1..] {
            let (a, b) = (key(s), key(best));
            if a.0 > b.0 || (a.0 == b.0 && a.1 > b.1) {
                best = s;
            }
        }
        best
    }

    /// `primary_plan._representative`: adds prompt and output tokens to the key.
    pub fn primary_representative(&self) -> &Scenario {
        let key = |s: &Scenario| {
            (
                num_f(num_mul(s.total_context_tokens(), s.concurrency)),
                num_f(s.effective_prompt_tokens()),
                num_f(s.output_tokens),
                s.name.clone(),
            )
        };
        let mut best = &self.scenarios[0];
        for s in &self.scenarios[1..] {
            let (a, b) = (key(s), key(best));
            let greater = a.0 > b.0
                || (a.0 == b.0
                    && (a.1 > b.1 || (a.1 == b.1 && (a.2 > b.2 || (a.2 == b.2 && a.3 > b.3)))));
            if greater {
                best = s;
            }
        }
        best
    }
}

// ---------------------------------------------------------------- policy
#[derive(Debug, Clone)]
pub struct Policy {
    pub exact_cycle_max_nodes: f64,
    pub held_karp_max_nodes: f64,
    pub local_search_max_nodes: f64,
    pub clustered_max_nodes: f64,
    pub search_candidate_budget: Num,
    pub replica_budget: f64,
    pub minimum_replica_gain_fraction: Num,
    pub replica_uncertainty_fraction: Num,
    pub conservative_bandwidth_bps: Option<f64>,
    pub exclude_missing_bandwidth: bool,
    pub jitter_guard_sigma: f64,
    pub loss_penalty_ms: f64,
    pub memory_reserve_fraction: f64,
    pub objective: String,
    pub ttft_slo_ms: f64,
    pub tpot_slo_ms: f64,
    pub echo_budget: Value,
    pub echo_gain: Value,
    pub echo_uncertainty: Value,
}

const POLICY_FIELDS: [&str; 16] = [
    "exact_cycle_max_nodes",
    "held_karp_max_nodes",
    "local_search_max_nodes",
    "clustered_max_nodes",
    "search_candidate_budget",
    "replica_budget",
    "minimum_replica_gain_fraction",
    "replica_uncertainty_fraction",
    "conservative_bandwidth_Bps",
    "exclude_missing_bandwidth",
    "jitter_guard_sigma",
    "loss_penalty_ms",
    "memory_reserve_fraction",
    "objective",
    "ttft_slo_ms",
    "tpot_slo_ms",
];

impl Policy {
    pub fn parse(data: &Value) -> PyResult<Self> {
        let map = match data {
            Value::Object(m) => m,
            // `set(data) - allowed` iterates strings by character and lists by element.
            Value::String(s) if !s.is_empty() => {
                let mut names: Vec<String> = s.chars().map(String::from).collect();
                names.sort();
                names.dedup();
                return verr(format!(
                    "unknown planning policy fields: {}",
                    py_repr(&json!(names))
                ));
            }
            Value::Array(items) if !items.is_empty() => {
                let mut names: Vec<&str> = Vec::new();
                for item in items {
                    match item.as_str() {
                        Some(s) => names.push(s),
                        None => return other("unhashable or non-string policy field"),
                    }
                }
                names.sort();
                names.dedup();
                if names.iter().any(|n| !POLICY_FIELDS.contains(n)) {
                    let unknown: Vec<&&str> = names
                        .iter()
                        .filter(|n| !POLICY_FIELDS.contains(n))
                        .collect();
                    return verr(format!(
                        "unknown planning policy fields: {}",
                        py_repr(&json!(unknown))
                    ));
                }
                return other("PlanningPolicy(**non-mapping)");
            }
            Value::String(_) | Value::Array(_) => return other("PlanningPolicy(**non-mapping)"),
            _ => return other("policy is not iterable"),
        };
        let mut unknown: Vec<&String> = map
            .keys()
            .filter(|k| !POLICY_FIELDS.contains(&k.as_str()))
            .collect();
        unknown.sort();
        if !unknown.is_empty() {
            let names: Vec<Value> = unknown.iter().map(|k| json!(k)).collect();
            return verr(format!(
                "unknown planning policy fields: {}",
                py_repr(&Value::Array(names))
            ));
        }
        let n = |key: &str, default: Num| -> PyResult<Num> {
            if map.contains_key(key) {
                number(map, key)
            } else {
                Ok(default)
            }
        };
        let f =
            |key: &str, default: f64| -> PyResult<f64> { Ok(num_f(n(key, Num::Float(default))?)) };
        let conservative = match map.get("conservative_bandwidth_Bps") {
            None => Some(1_000_000.0),
            Some(Value::Null) => None,
            Some(_) => Some(real(map, "conservative_bandwidth_Bps")?),
        };
        let objective = match map.get("objective") {
            None => "slo_goodput".to_owned(),
            Some(Value::String(s)) => s.clone(),
            Some(_) => String::from("\u{0}non-string"),
        };
        let out = Self {
            exact_cycle_max_nodes: num_f(n("exact_cycle_max_nodes", Num::Int(7))?),
            held_karp_max_nodes: num_f(n("held_karp_max_nodes", Num::Int(12))?),
            local_search_max_nodes: num_f(n("local_search_max_nodes", Num::Int(32))?),
            clustered_max_nodes: num_f(n("clustered_max_nodes", Num::Int(128))?),
            search_candidate_budget: n("search_candidate_budget", Num::Int(100_000))?,
            replica_budget: num_f(n("replica_budget", Num::Int(32))?),
            minimum_replica_gain_fraction: n("minimum_replica_gain_fraction", Num::Float(0.05))?,
            replica_uncertainty_fraction: n("replica_uncertainty_fraction", Num::Float(0.1))?,
            conservative_bandwidth_bps: conservative,
            exclude_missing_bandwidth: map.get("exclude_missing_bandwidth").is_some_and(truthy),
            jitter_guard_sigma: f("jitter_guard_sigma", 2.0)?,
            loss_penalty_ms: f("loss_penalty_ms", 10.0)?,
            memory_reserve_fraction: f("memory_reserve_fraction", 0.1)?,
            objective,
            ttft_slo_ms: f("ttft_slo_ms", 2_000.0)?,
            tpot_slo_ms: f("tpot_slo_ms", 200.0)?,
            echo_budget: map
                .get("search_candidate_budget")
                .cloned()
                .unwrap_or_else(|| json!(100_000)),
            echo_gain: map
                .get("minimum_replica_gain_fraction")
                .cloned()
                .unwrap_or_else(|| json!(0.05)),
            echo_uncertainty: map
                .get("replica_uncertainty_fraction")
                .cloned()
                .unwrap_or_else(|| json!(0.1)),
        };
        let thresholds = [
            out.exact_cycle_max_nodes,
            out.held_karp_max_nodes,
            out.local_search_max_nodes,
            out.clustered_max_nodes,
        ];
        if thresholds.iter().any(|x| *x <= 0.0) || thresholds.windows(2).any(|w| w[0] > w[1]) {
            return verr("search thresholds must be positive and nondecreasing");
        }
        if num_f(out.search_candidate_budget) <= 0.0 || out.replica_budget < 0.0 {
            return verr("invalid planning budget");
        }
        if !(0.0..=1.0).contains(&num_f(out.minimum_replica_gain_fraction)) {
            return verr("minimum replica gain fraction must be in [0, 1]");
        }
        let uncertainty = num_f(out.replica_uncertainty_fraction);
        if !(0.0..1.0).contains(&uncertainty) {
            return verr("replica uncertainty fraction must be in [0, 1)");
        }
        if out.conservative_bandwidth_bps.is_some_and(|b| b <= 0.0) {
            return verr("fallback bandwidth must be positive");
        }
        if !(0.0..1.0).contains(&out.memory_reserve_fraction) {
            return verr("memory reserve must be in [0, 1)");
        }
        if !["slo_goodput", "balanced", "prefill_ttft", "decode_tpot"]
            .contains(&out.objective.as_str())
        {
            return verr("unknown planning objective");
        }
        Ok(out)
    }
}

// ---------------------------------------------------------------- network cost
pub struct EdgeCost {
    pub total_ms: f64,
    pub confidence: f64,
    pub confidence_int: bool,
}

pub fn transfer_time_ms(link: &Link, payload_bytes: f64, policy: &Policy) -> PyResult<EdgeCost> {
    if payload_bytes < 0.0 {
        return verr("payload must be non-negative");
    }
    let bandwidth = match link.bandwidth_bps {
        Some(b) => b,
        None => {
            if policy.exclude_missing_bandwidth || policy.conservative_bandwidth_bps.is_none() {
                return verr(format!(
                    "link {}->{} lacks measured bandwidth",
                    link.src, link.dst
                ));
            }
            policy.conservative_bandwidth_bps.unwrap_or(0.0)
        }
    };
    let base = (link.rtt_ms / 2.0).max(link.geolocation_floor_ms);
    let serialization = payload_bytes / bandwidth * 1000.0;
    let jitter = policy.jitter_guard_sigma * link.jitter_ms;
    let loss = policy.loss_penalty_ms * link.loss_ratio;
    let mut confidence = link.confidence;
    if link.inferred {
        confidence *= 0.7;
    }
    if link.stale {
        confidence *= 0.7;
    }
    Ok(EdgeCost {
        total_ms: base + serialization + jitter + loss,
        confidence,
        confidence_int: link.confidence_int && !link.inferred && !link.stale,
    })
}

// ---------------------------------------------------------------- physical graph
pub struct Graph {
    pub nodes: BTreeMap<String, NodeCapability>,
    pub edges: BTreeMap<(String, String), Link>,
    pub exclusions: BTreeMap<String, Value>,
}

impl Graph {
    pub fn build(nodes: &[NodeCapability], links: &[Link], policy: &Policy) -> PyResult<Self> {
        let mut sorted: Vec<&NodeCapability> = nodes.iter().collect();
        sorted.sort_by(|a, b| a.node_id.cmp(&b.node_id));
        let mut all = BTreeMap::new();
        let mut exclusions: BTreeMap<String, Value> = BTreeMap::new();
        for node in sorted {
            if all.contains_key(&node.node_id) || exclusions.contains_key(&node.node_id) {
                return verr(format!("duplicate node id: {}", node.node_id));
            }
            if node.eligible {
                all.insert(node.node_id.clone(), node.clone());
            } else {
                let reason = match &node.exclusion_reason {
                    v if truthy(v) => v.clone(),
                    _ => json!("ineligible"),
                };
                exclusions.insert(node.node_id.clone(), reason);
            }
        }
        let mut sorted_links: Vec<&Link> = links.iter().collect();
        sorted_links.sort_by(|a, b| (&a.src, &a.dst).cmp(&(&b.src, &b.dst)));
        let mut edges = BTreeMap::new();
        for link in sorted_links {
            if !all.contains_key(&link.src) || !all.contains_key(&link.dst) {
                continue;
            }
            let key = (link.src.clone(), link.dst.clone());
            if edges.contains_key(&key) {
                return verr(format!(
                    "duplicate directed link: {}->{}",
                    link.src, link.dst
                ));
            }
            if transfer_time_ms(link, 0.0, policy).is_err() {
                continue;
            }
            edges.insert(key, link.clone());
        }
        Ok(Self {
            nodes: all,
            edges,
            exclusions,
        })
    }

    pub fn candidates(&self) -> Vec<String> {
        self.nodes.keys().cloned().collect()
    }

    pub fn link(&self, src: &str, dst: &str) -> Option<&Link> {
        self.edges.get(&(src.to_owned(), dst.to_owned()))
    }
}
