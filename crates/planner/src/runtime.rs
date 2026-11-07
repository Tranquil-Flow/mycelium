// SPDX-License-Identifier: AGPL-3.0-only
//! Port of `runtime_contracts.py` (normalized runtime identity) at f4e366e9.
//!
//! Every architecture the pinned oracle admits is ported: GPT-2, Qwen2, Qwen3,
//! Qwen3.5 hybrid and Qwen3.5 MoE, for the `mlx`, `numpy` and `mobile-rust-qwen2`
//! backends. Messages are the oracle's `ValueError` texts.
use crate::py::{Failure, PyResult, float, get, has, int, is_bool, py_eq, verr};
use serde_json::{Map, Value, json};

pub const MLX_RUNTIME_BASE_FIELDS: [&str; 3] = ["backend", "dtype", "quantization"];
const NORMALIZED_FIELDS: [&str; 5] = [
    "backend",
    "dtype",
    "quantization",
    "architecture",
    "model_config",
];
const GPT2_FIELDS: [&str; 12] = [
    "n_layer",
    "n_embd",
    "n_head",
    "n_inner",
    "vocab_size",
    "n_positions",
    "layer_norm_epsilon",
    "activation_function",
    "scale_attn_weights",
    "scale_attn_by_inverse_layer_idx",
    "reorder_and_upcast_attn",
    "add_cross_attention",
];
const QWEN2_FIELDS: [&str; 12] = [
    "n_layer",
    "n_embd",
    "n_head",
    "n_kv_head",
    "n_inner",
    "vocab_size",
    "n_positions",
    "rms_norm_epsilon",
    "rope_theta",
    "head_dim",
    "activation_function",
    "tie_word_embeddings",
];
const QWEN35_EXTRA: [&str; 8] = [
    "layer_types",
    "rotary_dim",
    "attn_output_gate",
    "linear_conv_kernel_dim",
    "linear_num_key_heads",
    "linear_key_head_dim",
    "linear_num_value_heads",
    "linear_value_head_dim",
];
const QWEN35_MOE_FIELDS: [&str; 5] = [
    "num_experts",
    "num_experts_per_tok",
    "moe_intermediate_size",
    "shared_expert_intermediate_size",
    "norm_topk_prob",
];
const SUPPORTED_GPT2_FLAGS: [(&str, bool); 4] = [
    ("scale_attn_weights", true),
    ("scale_attn_by_inverse_layer_idx", false),
    ("reorder_and_upcast_attn", false),
    ("add_cross_attention", false),
];

fn key_set(v: &Value) -> Option<std::collections::BTreeSet<&str>> {
    v.as_object()
        .map(|m| m.keys().map(String::as_str).collect())
}

fn set_eq(v: &Value, groups: &[&[&str]]) -> bool {
    let expected: std::collections::BTreeSet<&str> =
        groups.iter().flat_map(|g| g.iter().copied()).collect();
    key_set(v).is_some_and(|k| k == expected)
}

fn positive_int(value: &Value, field: &str) -> PyResult<i128> {
    match int(value) {
        Some(n) if n > 0 => Ok(n),
        _ => verr(format!(
            "runtime model_config {field} must be a positive integer"
        )),
    }
}

fn finite_positive(value: &Value) -> Option<f64> {
    float(value).filter(|f| f.is_finite() && *f > 0.0)
}

/// `config.get(a, config.get(b))`.
fn get2<'a>(config: &'a Value, a: &str, b: &str) -> PyResult<&'a Value> {
    if has(config, a) {
        get(config, a)
    } else {
        get(config, b)
    }
}

fn config_object(config: &Value) -> PyResult<()> {
    if config.is_object() {
        Ok(())
    } else {
        verr("runtime model_config source must be an object")
    }
}

fn ensure_json(normalized: &Value) -> PyResult<()> {
    crate::canonical(normalized, false)
        .map(|_| ())
        .map_err(|_| Failure::value("Out of range float values are not JSON compliant"))
}

pub fn normalize_gpt2_model_config(config: &Value, expected_layers: i128) -> PyResult<Value> {
    config_object(config)?;
    let n_layer = positive_int(get(config, "n_layer")?, "n_layer")?;
    if n_layer != expected_layers {
        return verr("runtime model_config n_layer does not match manifest layer count");
    }
    let n_embd = positive_int(get(config, "n_embd")?, "n_embd")?;
    let n_head = positive_int(get(config, "n_head")?, "n_head")?;
    if n_embd % n_head != 0 {
        return verr("runtime model_config requires n_embd divisible by n_head");
    }
    let raw_inner = get(config, "n_inner")?;
    let n_inner = if raw_inner.is_null() {
        4 * n_embd
    } else {
        positive_int(raw_inner, "n_inner")?
    };
    let vocab_size = positive_int(get(config, "vocab_size")?, "vocab_size")?;
    let n_positions = positive_int(get(config, "n_positions")?, "n_positions")?;
    if vocab_size < 3 || n_positions < 3 {
        return verr("runtime model_config vocab_size and n_positions must be at least 3");
    }
    let epsilon = finite_positive(get(config, "layer_norm_epsilon")?);
    let Some(epsilon) = epsilon else {
        return verr("runtime model_config layer_norm_epsilon must be positive and finite");
    };
    if get(config, "activation_function")? != &json!("gelu_new") {
        return verr("runtime model_config activation_function must be gelu_new");
    }
    for (field, expected) in SUPPORTED_GPT2_FLAGS {
        let actual = if has(config, field) {
            get(config, field)?
        } else {
            &Value::Bool(expected)
        };
        if !is_bool(actual, expected) {
            return verr(format!(
                "unsupported runtime model_config {field}={}",
                crate::py_repr(actual)
            ));
        }
    }
    let normalized = json!({
        "n_layer": to_json_int(n_layer),
        "n_embd": to_json_int(n_embd),
        "n_head": to_json_int(n_head),
        "n_inner": to_json_int(n_inner),
        "vocab_size": to_json_int(vocab_size),
        "n_positions": to_json_int(n_positions),
        "layer_norm_epsilon": epsilon,
        "activation_function": "gelu_new",
        "scale_attn_weights": true,
        "scale_attn_by_inverse_layer_idx": false,
        "reorder_and_upcast_attn": false,
        "add_cross_attention": false,
    });
    ensure_json(&normalized)?;
    Ok(normalized)
}

fn to_json_int(n: i128) -> Value {
    if let Ok(i) = i64::try_from(n) {
        json!(i)
    } else if let Ok(u) = u64::try_from(n) {
        json!(u)
    } else {
        Value::Null
    }
}

fn normalize_qwen_decoder(
    config: &Value,
    expected_layers: i128,
    wide_attention: bool,
) -> PyResult<Map<String, Value>> {
    config_object(config)?;
    let n_layer = positive_int(get2(config, "num_hidden_layers", "n_layer")?, "n_layer")?;
    if n_layer != expected_layers {
        return verr("runtime model_config n_layer does not match manifest layer count");
    }
    let n_embd = positive_int(get2(config, "hidden_size", "n_embd")?, "n_embd")?;
    let n_head = positive_int(get2(config, "num_attention_heads", "n_head")?, "n_head")?;
    let n_kv_head = positive_int(
        get2(config, "num_key_value_heads", "n_kv_head")?,
        "n_kv_head",
    )?;
    let default_head_dim = json!(n_embd / n_head);
    let head_dim_raw = if has(config, "head_dim") {
        get(config, "head_dim")?
    } else {
        &default_head_dim
    };
    let head_dim = positive_int(head_dim_raw, "head_dim")?;
    if (!wide_attention && n_embd != n_head * head_dim) || n_head % n_kv_head != 0 {
        return verr("runtime model_config has incompatible attention heads");
    }
    let n_inner = positive_int(get2(config, "intermediate_size", "n_inner")?, "n_inner")?;
    let vocab_size = positive_int(get(config, "vocab_size")?, "vocab_size")?;
    let n_positions = positive_int(
        get2(config, "max_position_embeddings", "n_positions")?,
        "n_positions",
    )?;
    let epsilon = get2(config, "rms_norm_eps", "rms_norm_epsilon")?;
    let rope_theta = get(config, "rope_theta")?;
    let mut floats = Vec::new();
    for (value, field) in [(epsilon, "rms_norm_epsilon"), (rope_theta, "rope_theta")] {
        match finite_positive(value) {
            Some(f) => floats.push(f),
            None => {
                return verr(format!(
                    "runtime model_config {field} must be positive and finite"
                ));
            }
        }
    }
    let activation = get2(config, "hidden_act", "activation_function")?;
    if activation != &json!("silu") {
        return verr("runtime model_config activation_function must be silu");
    }
    let tied = get(config, "tie_word_embeddings")?;
    if !tied.is_boolean() {
        return verr("runtime model_config tie_word_embeddings must be boolean");
    }
    let normalized = json!({
        "n_layer": to_json_int(n_layer),
        "n_embd": to_json_int(n_embd),
        "n_head": to_json_int(n_head),
        "n_kv_head": to_json_int(n_kv_head),
        "n_inner": to_json_int(n_inner),
        "vocab_size": to_json_int(vocab_size),
        "n_positions": to_json_int(n_positions),
        "rms_norm_epsilon": floats[0],
        "rope_theta": floats[1],
        "head_dim": to_json_int(head_dim),
        "activation_function": "silu",
        "tie_word_embeddings": tied,
    });
    ensure_json(&normalized)?;
    Ok(normalized.as_object().cloned().unwrap_or_default())
}

pub fn normalize_qwen2_model_config(config: &Value, expected_layers: i128) -> PyResult<Value> {
    Ok(Value::Object(normalize_qwen_decoder(
        config,
        expected_layers,
        false,
    )?))
}

/// `value is not False` for `config.get(field, False)`.
fn not_false(config: &Value, field: &str) -> PyResult<bool> {
    Ok(has(config, field) && !is_bool(get(config, field)?, false))
}

fn zero_like(v: &Value) -> bool {
    // `v == 0` (bool False, 0, 0.0)
    py_eq(v, &json!(0))
}

pub fn normalize_qwen3_model_config(config: &Value, expected_layers: i128) -> PyResult<Value> {
    let normalized = normalize_qwen_decoder(config, expected_layers, true)?;
    if not_false(config, "attention_bias")? {
        return verr("runtime qwen3 attention_bias must be false");
    }
    if has(config, "attention_dropout") && !zero_like(get(config, "attention_dropout")?) {
        return verr("runtime qwen3 attention_dropout must be zero");
    }
    if !get(config, "rope_scaling")?.is_null() {
        return verr("runtime qwen3 rope_scaling is unsupported");
    }
    if not_false(config, "use_sliding_window")? {
        return verr("runtime qwen3 sliding-window attention is unsupported");
    }
    if !get(config, "sliding_window")?.is_null() {
        return verr("runtime qwen3 sliding_window must be null");
    }
    Ok(Value::Object(normalized))
}

fn empty_list_or_absent(text: &Value, field: &str) -> PyResult<bool> {
    if !has(text, field) {
        return Ok(true);
    }
    Ok(get(text, field)?.as_array().is_some_and(Vec::is_empty))
}

pub fn normalize_qwen35_model_config(config: &Value, expected_layers: i128) -> PyResult<Value> {
    config_object(config)?;
    let text = if has(config, "text_config") {
        get(config, "text_config")?
    } else {
        config
    };
    if !text.is_object() {
        return verr("runtime qwen3_5 text_config must be an object");
    }
    for field in ["num_experts", "num_experts_per_tok"] {
        if has(text, field) {
            let experts = get(text, field)?;
            if !(experts.is_null() || zero_like(experts)) {
                return verr("runtime qwen3_5 dense contract refuses moe fields");
            }
        }
    }
    let mut decoder_source = text.as_object().cloned().unwrap_or_default();
    if !decoder_source.contains_key("tie_word_embeddings") && has(config, "tie_word_embeddings") {
        decoder_source.insert(
            "tie_word_embeddings".into(),
            get(config, "tie_word_embeddings")?.clone(),
        );
    }
    if !has(text, "rotary_dim") {
        let rope = get(text, "rope_parameters")?;
        if rope.is_object() && !decoder_source.contains_key("rope_theta") {
            decoder_source.insert("rope_theta".into(), get(rope, "rope_theta")?.clone());
        }
    }
    let decoder_source = Value::Object(decoder_source);
    let mut normalized = normalize_qwen_decoder(&decoder_source, expected_layers, true)?;
    if not_false(text, "attention_bias")? {
        return verr("runtime qwen3_5 attention_bias must be false");
    }
    if has(text, "attention_dropout") && !zero_like(get(text, "attention_dropout")?) {
        return verr("runtime qwen3_5 attention_dropout must be zero");
    }
    if has(text, "attn_output_gate") && !is_bool(get(text, "attn_output_gate")?, true) {
        return verr("runtime qwen3_5 attn_output_gate must be true");
    }
    if !empty_list_or_absent(text, "mlp_only_layers")? {
        return verr("runtime qwen3_5 mlp_only_layers must be empty");
    }
    if has(text, "mamba_ssm_dtype") && get(text, "mamba_ssm_dtype")? != &json!("float32") {
        return verr("runtime qwen3_5 linear state must be float32");
    }
    let head_dim = normalized
        .get("head_dim")
        .and_then(int)
        .ok_or_else(|| Failure::other("head_dim"))?;
    let rotary_dim;
    let rope_theta;
    if has(text, "rotary_dim") {
        rotary_dim = positive_int(get(text, "rotary_dim")?, "rotary_dim")?;
        rope_theta = normalized
            .get("rope_theta")
            .and_then(float)
            .ok_or_else(|| Failure::other("rope_theta"))?;
    } else {
        let rope = get(text, "rope_parameters")?;
        if !rope.is_object() {
            return verr("runtime qwen3_5 rope_parameters must be an object");
        }
        let rope_type = if has(rope, "rope_type") {
            get(rope, "rope_type")?
        } else {
            get(rope, "type")?
        };
        if rope_type != &json!("default") {
            return verr("runtime qwen3_5 rope_type must be default (no rope scaling)");
        }
        let factor = float(get(rope, "partial_rotary_factor")?).filter(|f| 0.0 < *f && *f <= 1.0);
        let Some(factor) = factor else {
            return verr("runtime qwen3_5 partial_rotary_factor is invalid");
        };
        let product = head_dim as f64 * factor;
        rotary_dim = product as i128;
        if rotary_dim as f64 * 1.0 != product {
            return verr("runtime qwen3_5 rotary dimension is not integral");
        }
        let section = get(rope, "mrope_section")?;
        let ok = section.as_array().is_some_and(|s| {
            s.len() == 3
                && s.iter().all(|v| int(v).is_some_and(|n| n >= 0))
                && s.iter().filter_map(int).sum::<i128>() == rotary_dim.div_euclid(2)
        });
        if !ok {
            return verr("runtime qwen3_5 mrope_section does not cover the rotary dimension");
        }
        let theta = finite_positive(get(rope, "rope_theta")?);
        let Some(theta) = theta else {
            return verr("runtime qwen3_5 rope_theta must be positive and finite");
        };
        rope_theta = theta;
    }
    if rotary_dim % 2 != 0 || rotary_dim > head_dim {
        return verr("runtime qwen3_5 rotary dimension is invalid");
    }
    let mut linear = Map::new();
    for field in [
        "linear_conv_kernel_dim",
        "linear_num_key_heads",
        "linear_key_head_dim",
        "linear_num_value_heads",
        "linear_value_head_dim",
    ] {
        linear.insert(
            field.into(),
            to_json_int(positive_int(get(text, field)?, field)?),
        );
    }
    let values = linear["linear_num_value_heads"].as_i64().unwrap_or(0);
    let keys = linear["linear_num_key_heads"].as_i64().unwrap_or(1);
    if values % keys != 0 {
        return verr("runtime qwen3_5 linear value heads must be a multiple of key heads");
    }
    let n_layer = normalized
        .get("n_layer")
        .and_then(int)
        .ok_or_else(|| Failure::other("n_layer"))?;
    let layer_types = get(text, "layer_types")?;
    let types_ok = layer_types.as_array().is_some_and(|t| {
        t.len() as i128 == n_layer
            && t.iter()
                .all(|k| k == &json!("linear_attention") || k == &json!("full_attention"))
    });
    if !types_ok {
        return verr("runtime qwen3_5 layer_types are invalid");
    }
    let interval = get(text, "full_attention_interval")?;
    if !interval.is_null() {
        let interval = positive_int(interval, "full_attention_interval")?;
        let expected: Vec<Value> = (0..n_layer)
            .map(|index| {
                if (index + 1) % interval == 0 {
                    json!("full_attention")
                } else {
                    json!("linear_attention")
                }
            })
            .collect();
        if layer_types.as_array() != Some(&expected) {
            return verr("runtime qwen3_5 layer_types disagree with full_attention_interval");
        }
    }
    normalized.insert("rope_theta".into(), json!(rope_theta));
    normalized.insert("layer_types".into(), layer_types.clone());
    normalized.insert("rotary_dim".into(), to_json_int(rotary_dim));
    normalized.insert("attn_output_gate".into(), json!(true));
    normalized.extend(linear);
    let normalized = Value::Object(normalized);
    ensure_json(&normalized)?;
    Ok(normalized)
}

pub fn normalize_qwen35_moe_model_config(config: &Value, expected_layers: i128) -> PyResult<Value> {
    config_object(config)?;
    let text = if has(config, "text_config") {
        get(config, "text_config")?
    } else {
        config
    };
    if !text.is_object() {
        return verr("runtime qwen3_5_moe text_config must be an object");
    }
    let mut moe = Map::new();
    for field in [
        "num_experts",
        "num_experts_per_tok",
        "moe_intermediate_size",
        "shared_expert_intermediate_size",
    ] {
        match positive_int(get(text, field)?, field) {
            Ok(n) => {
                moe.insert(field.into(), to_json_int(n));
            }
            Err(_) => {
                return verr(format!(
                    "runtime qwen3_5_moe {field} must be a positive integer"
                ));
            }
        }
    }
    let experts = moe["num_experts"].as_i64().unwrap_or(0);
    let per_tok = moe["num_experts_per_tok"].as_i64().unwrap_or(0);
    if per_tok > experts {
        return verr("runtime qwen3_5_moe num_experts_per_tok exceeds num_experts");
    }
    if has(text, "norm_topk_prob") && !is_bool(get(text, "norm_topk_prob")?, true) {
        return verr("runtime qwen3_5_moe norm_topk_prob must be true");
    }
    if has(text, "decoder_sparse_step") && !py_eq(get(text, "decoder_sparse_step")?, &json!(1)) {
        return verr("runtime qwen3_5_moe decoder_sparse_step must be 1 (every layer sparse)");
    }
    if !empty_list_or_absent(text, "mlp_only_layers")? {
        return verr("runtime qwen3_5_moe mlp_only_layers must be empty");
    }
    if has(text, "n_inner") && !py_eq(get(text, "n_inner")?, &moe["moe_intermediate_size"]) {
        return verr("runtime qwen3_5_moe n_inner must equal moe_intermediate_size");
    }
    let mut dense: Map<String, Value> = text
        .as_object()
        .map(|m| {
            m.iter()
                .filter(|(k, _)| {
                    !QWEN35_MOE_FIELDS.contains(&k.as_str())
                        && !["decoder_sparse_step", "intermediate_size", "n_inner"]
                            .contains(&k.as_str())
                })
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect()
        })
        .unwrap_or_default();
    dense.insert(
        "intermediate_size".into(),
        moe["moe_intermediate_size"].clone(),
    );
    if !dense.contains_key("tie_word_embeddings") && has(config, "tie_word_embeddings") {
        dense.insert(
            "tie_word_embeddings".into(),
            get(config, "tie_word_embeddings")?.clone(),
        );
    }
    let normalized = normalize_qwen35_model_config(&Value::Object(dense), expected_layers)?;
    let mut normalized = normalized.as_object().cloned().unwrap_or_default();
    normalized.extend(moe);
    normalized.insert("norm_topk_prob".into(), json!(true));
    let normalized = Value::Object(normalized);
    ensure_json(&normalized)?;
    Ok(normalized)
}

type Normalizer = fn(&Value, i128) -> PyResult<Value>;

fn architecture_runtime(runtime: &Value) -> PyResult<(String, Value)> {
    let architecture = get(runtime, "architecture")?;
    let model_config = get(runtime, "model_config")?;
    if !model_config.is_object() {
        return verr(format!(
            "{} runtime requires model_config",
            crate::py::py_str(architecture)
        ));
    }
    let layers = |config: &Value| positive_int(get(config, "n_layer")?, "n_layer");
    let name = architecture.as_str().unwrap_or("");
    let (fields, normalizer): (Vec<&[&str]>, Normalizer) = match name {
        "gpt2" => (vec![&GPT2_FIELDS], normalize_gpt2_model_config),
        "qwen2" => (vec![&QWEN2_FIELDS], normalize_qwen2_model_config),
        "qwen3" => (vec![&QWEN2_FIELDS], normalize_qwen3_model_config),
        "qwen3_5" => (
            vec![&QWEN2_FIELDS, &QWEN35_EXTRA],
            normalize_qwen35_model_config,
        ),
        "qwen3_5_moe" => (
            vec![&QWEN2_FIELDS, &QWEN35_EXTRA, &QWEN35_MOE_FIELDS],
            normalize_qwen35_moe_model_config,
        ),
        _ => {
            return verr(
                "unsupported runtime architecture; expected gpt2, qwen2, qwen3, qwen3_5, or qwen3_5_moe",
            );
        }
    };
    if !set_eq(model_config, &fields) {
        return verr(match name {
            "gpt2" => {
                "gpt2 model_config fields do not match the normalized runtime contract".to_owned()
            }
            other => format!(
                "{other} architecture model_config fields do not match the normalized runtime contract"
            ),
        });
    }
    let normalized = normalizer(model_config, layers(model_config)?)?;
    Ok((name.to_owned(), normalized))
}

const SUPPORTED_QUANTIZATIONS: [&str; 2] = ["none", "int8-weight-only"];

fn canonical_form(runtime: &Value, normalized: &Value) -> PyResult<()> {
    let round_trip = crate::canonical(runtime, false)
        .map_err(|_| Failure::value("Out of range float values are not JSON compliant"))?;
    let reparsed: Value = serde_json::from_str(&round_trip)
        .map_err(|_| Failure::value("runtime identity is not in normalized canonical form"))?;
    if !py_eq(&reparsed, normalized) {
        return verr("runtime identity is not in normalized canonical form");
    }
    Ok(())
}

fn str_field<'a>(v: &'a Value, key: &str) -> PyResult<Option<&'a str>> {
    Ok(get(v, key)?.as_str())
}

pub fn validate_normalized_mlx_runtime(runtime: &Value) -> PyResult<Value> {
    if !runtime.is_object() {
        return verr("runtime identity must be an object");
    }
    if !set_eq(runtime, &[&NORMALIZED_FIELDS]) {
        return verr("runtime identity fields do not match the normalized MLX contract");
    }
    if str_field(runtime, "backend")? != Some("mlx") {
        return verr("unsupported runtime backend; expected mlx");
    }
    let quantization = str_field(runtime, "quantization")?;
    if !quantization.is_some_and(|q| SUPPORTED_QUANTIZATIONS.contains(&q)) {
        return verr("unsupported runtime quantization");
    }
    let dtype = str_field(runtime, "dtype")?;
    if !dtype.is_some_and(|d| ["float16", "bfloat16", "float32"].contains(&d)) {
        return verr("unsupported runtime dtype; expected float16, bfloat16, or float32");
    }
    let (architecture, config) = architecture_runtime(runtime)?;
    if architecture == "gpt2" && quantization != Some("none") {
        return verr("gpt2 runtime quantization is unsupported");
    }
    let normalized = json!({
        "backend": "mlx",
        "dtype": dtype,
        "quantization": quantization,
        "architecture": architecture,
        "model_config": config,
    });
    canonical_form(runtime, &normalized)?;
    Ok(normalized)
}

pub fn validate_normalized_numpy_runtime(runtime: &Value) -> PyResult<Value> {
    if !runtime.is_object() {
        return verr("runtime identity must be an object");
    }
    if !set_eq(runtime, &[&NORMALIZED_FIELDS]) {
        return verr("runtime identity fields do not match the normalized NumPy contract");
    }
    if str_field(runtime, "backend")? != Some("numpy") {
        return verr("unsupported runtime backend; expected numpy");
    }
    let quantization = str_field(runtime, "quantization")?;
    if !quantization.is_some_and(|q| SUPPORTED_QUANTIZATIONS.contains(&q)) {
        return verr("unsupported runtime quantization");
    }
    let dtype = str_field(runtime, "dtype")?;
    if dtype != Some("float32") {
        return verr("unsupported numpy runtime dtype; expected float32");
    }
    let (architecture, config) = architecture_runtime(runtime)?;
    if architecture == "gpt2" && quantization != Some("none") {
        return verr("gpt2 runtime quantization is unsupported");
    }
    if architecture == "qwen3_5" || architecture == "qwen3_5_moe" {
        return verr(format!("{architecture} numpy stage runtime is unsupported"));
    }
    let normalized = json!({
        "backend": "numpy",
        "dtype": dtype,
        "quantization": quantization,
        "architecture": architecture,
        "model_config": config,
    });
    canonical_form(runtime, &normalized)?;
    Ok(normalized)
}

/// Strict dispatch (`validate_normalized_runtime`).
pub fn validate_normalized_runtime(
    runtime: &Value,
    expected_backend: Option<&str>,
) -> PyResult<Value> {
    if !runtime.is_object() {
        return verr("runtime identity must be an object");
    }
    let backend = get(runtime, "backend")?;
    if let Some(expected) = expected_backend
        && backend != &json!(expected)
    {
        return verr(format!("runtime backend mismatch; expected {expected}"));
    }
    match backend.as_str() {
        Some("mlx") => validate_normalized_mlx_runtime(runtime),
        Some("mobile-rust-qwen2") => {
            if get(runtime, "architecture")? != &json!("qwen2")
                || get(runtime, "quantization")? != &json!("int8-weight-only")
            {
                return verr("phone runtime requires qwen2 int8-weight-only");
            }
            let mut as_numpy = runtime.as_object().cloned().unwrap_or_default();
            as_numpy.insert("backend".into(), json!("numpy"));
            let normalized = validate_normalized_numpy_runtime(&Value::Object(as_numpy))?;
            let mut out = normalized.as_object().cloned().unwrap_or_default();
            out.insert("backend".into(), json!("mobile-rust-qwen2"));
            Ok(Value::Object(out))
        }
        Some("numpy") => validate_normalized_numpy_runtime(runtime),
        _ => verr(format!(
            "unsupported runtime backend: {}",
            crate::py_repr(backend)
        )),
    }
}
