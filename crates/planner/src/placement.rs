// SPDX-License-Identifier: AGPL-3.0-only
//! Pinned dense-Qwen placement, not a generalized model/compiler interface.
use crate::{Error, Result, add, mul, ratio::Ratio};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeSet;

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct ModelSpec {
    pub model_id: String,
    pub resolved_commit: String,
    pub shape: ModelShape,
    pub anchor: Option<MeasuredAnchor>,
}
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct ModelShape {
    /// `model_resources.ModelShape.label` (registered-models.json); only used in refusal messages.
    #[serde(default)]
    pub label: String,
    pub num_layers: usize,
    pub hidden_size: u64,
    pub intermediate_size: u64,
    pub num_attention_heads: u64,
    pub num_key_value_heads: u64,
    pub head_dim: u64,
    pub vocab_size: u64,
    pub tie_word_embeddings: bool,
    pub attention_bias: bool,
    pub qk_norm: bool,
    pub snapshot_bytes: u64,
    pub stores_tied_lm_head: bool,
    pub source_bytes_per_parameter: u64,
}
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct MeasuredAnchor {
    pub group_peak_bytes: u64,
    pub member_disk_bytes: u64,
}
/// TODO(L1): inject reviewed table bindings; no operational defaults here.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SplitPolicy {
    pub memory_fraction_numerator: u64,
    pub memory_fraction_denominator: u64,
    pub resident_factor_numerator: u64,
    pub resident_factor_denominator: u64,
    pub runtime_overhead_bytes: u64,
    pub speed_clamp_low_numerator: u64,
    pub speed_clamp_low_denominator: u64,
    pub speed_clamp_high: u64,
}
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ResourcePolicy {
    pub resident_factors: std::collections::BTreeMap<String, [u64; 2]>,
    pub disk_headroom: [u64; 2],
    pub guard_headroom: [u64; 2],
    pub load_peak_factor: u64,
    pub group_fixed_bytes: u64,
    pub context_tokens: u64,
    pub slice_header_bytes: u64,
    pub control_archive_bytes: u64,
    pub staged_copy_factor: u64,
    pub memory_fraction: [u64; 2],
}
#[derive(Clone, Debug, Deserialize, Serialize, Default)]
pub struct Caps {
    pub storage_bytes: Option<u64>,
    pub max_memory_bytes: Option<u64>,
    pub max_wall_seconds: Option<u64>,
}
/// Planning observation only. Actual signed provenance is supplied by the discovery seam.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Member {
    pub node_id: String,
    pub memory_bytes: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub decode_tokens_per_second: Option<serde_json::Number>,
    pub caps: Option<Caps>,
    pub available_storage_bytes: Option<u64>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Stage {
    pub node_id: String,
    pub start: usize,
    pub end: usize,
    pub stage_bytes: u64,
    pub cost_bytes: u64,
    pub budget_bytes: u64,
}
#[derive(Clone, Debug)]
pub struct LayerProfile {
    pub model_type: &'static str,
    pub layer_bytes: Vec<u64>,
    pub embedding_bytes: u64,
    pub output_bytes: u64,
    pub tie_word_embeddings: bool,
}
// Shared pre-existing v0.1.4 structural ceiling; L1 declaration requested.
pub(crate) fn valid_layer_count(layers: usize) -> bool {
    (1..=1024).contains(&layers)
}
impl ModelShape {
    pub fn profile(&self) -> Result<LayerProfile> {
        if !valid_layer_count(self.num_layers)
            || self.hidden_size == 0
            || self.head_dim == 0
            || self.num_key_value_heads == 0
            || self.num_attention_heads == 0
            || self.vocab_size == 0
            || self.intermediate_size == 0
            || self.source_bytes_per_parameter != 2
        {
            return Err(Error::new("model_profile_invalid"));
        }
        let h = self.hidden_size;
        let q = mul(self.num_attention_heads, self.head_dim)?;
        let kv = mul(self.num_key_value_heads, self.head_dim)?;
        let mut p = add(
            add(mul(h, q)?, mul(mul(2, h)?, kv)?)?,
            add(
                mul(q, h)?,
                add(mul(mul(3, h)?, self.intermediate_size)?, mul(2, h)?)?,
            )?,
        )?;
        if self.attention_bias {
            p = add(p, add(q, mul(2, kv)?)?)?;
        }
        if self.qk_norm {
            p = add(p, mul(2, self.head_dim)?)?;
        }
        let embed = mul(mul(self.vocab_size, h)?, self.source_bytes_per_parameter)?;
        let output = add(
            mul(h, self.source_bytes_per_parameter)?,
            if !self.tie_word_embeddings || self.stores_tied_lm_head {
                embed
            } else {
                0
            },
        )?;
        Ok(LayerProfile {
            model_type: if self.qk_norm { "qwen3" } else { "qwen2" },
            layer_bytes: vec![mul(p, self.source_bytes_per_parameter)?; self.num_layers],
            embedding_bytes: embed,
            output_bytes: output,
            tie_word_embeddings: self.tie_word_embeddings,
        })
    }
}
impl LayerProfile {
    pub fn document(&self) -> Value {
        json!({"model_type":self.model_type,"num_layers":self.layer_bytes.len(),"layer_bytes":self.layer_bytes,"embedding_bytes":self.embedding_bytes,"output_bytes":self.output_bytes,"tie_word_embeddings":self.tie_word_embeddings})
    }
    fn source_bytes(&self) -> Result<u64> {
        add(
            self.layer_bytes.iter().try_fold(0, |s, x| add(s, *x))?,
            add(self.embedding_bytes, self.output_bytes)?,
        )
    }
}
fn ratio(v: [u64; 2]) -> Result<Ratio> {
    Ratio::new(v[0].into(), v[1].into())
}
fn scaled(v: u64, r: [u64; 2]) -> Result<u64> {
    Ratio::int(v).mul(ratio(r)?)?.ceil()
}

pub fn member_resource(
    model: &ModelSpec,
    r: &ResourcePolicy,
    stage: &Stage,
    member: &Member,
    owner: Option<&str>,
    slice: bool,
    quant: &str,
) -> Result<Value> {
    let registered = || Error::new("planner_registered_policy_mismatch");
    // model_resources.py:77-79 explicitly derives this allowance from
    // slice_acquisition.MAX_INLINE_BYTES_PER_NODE, not a same-valued frame cap.
    if r.slice_header_bytes
        != budgets::BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxInlineBytesPerNodeL64
            .bytes()
            .map_err(|_| registered())?
            .get()
        || r.control_archive_bytes
            != budgets::BudgetId::ControlArchiveAllowance
                .bytes()
                .map_err(|_| registered())?
                .get()
        || r.staged_copy_factor
            != budgets::BudgetId::StagedCopyFactor
                .count()
                .map_err(|_| registered())?
                .get()
    {
        return Err(registered());
    }
    if !matches!(quant, "int8-weight-only" | "none") {
        return Err(Error::new("model_resources_quantization_unsupported"));
    }
    let factor = ratio(
        *r.resident_factors
            .get(quant)
            .ok_or_else(|| Error::new("model_resources_quantization_unsupported"))?,
    )?;
    let resident = Ratio::int(stage.stage_bytes).mul(factor)?.ceil()?;
    let kv = mul(
        mul(
            mul(
                mul(2, model.shape.num_key_value_heads)?,
                model.shape.head_dim,
            )?,
            4,
        )?,
        mul((stage.end - stage.start) as u64, r.context_tokens)?,
    )?;
    let (peak, disk, basis) = if let Some(a) = &model.anchor {
        (a.group_peak_bytes, a.member_disk_bytes, "measured")
    } else {
        (
            add(
                add(mul(r.load_peak_factor, resident)?, r.group_fixed_bytes)?,
                kv,
            )?,
            add(
                if slice {
                    add(stage.stage_bytes, r.slice_header_bytes)?
                } else {
                    add(model.shape.snapshot_bytes, stage.stage_bytes)?
                },
                r.control_archive_bytes,
            )?,
            "modelled",
        )
    };
    let mut footprint = mul(r.staged_copy_factor, disk)?;
    if owner == Some(member.node_id.as_str()) && slice && model.anchor.is_none() {
        footprint = add(footprint, model.shape.profile()?.source_bytes()?)?;
    }
    let ceiling = Ratio::int(member.memory_bytes)
        .mul(ratio(r.memory_fraction)?)?
        .floor()?;
    Ok(
        json!({"node_id":member.node_id,"start":stage.start,"end":stage.end,"layers":stage.end-stage.start,"stage_source_bytes":stage.stage_bytes,"resident_bytes":resident,"kv_bytes":kv,"load_peak_bytes":peak,"max_rss_bytes":scaled(peak,r.guard_headroom)?,"disk_need_bytes":disk,"max_disk_bytes":scaled(disk,r.disk_headroom)?,"disk_footprint_bytes":footprint,"basis":basis,"memory_bytes":member.memory_bytes,"memory_ceiling_bytes":ceiling}),
    )
}
pub(crate) fn require_member(row: &Value, m: &Member) -> Result<()> {
    let n = |k: &str| {
        row[k]
            .as_u64()
            .ok_or_else(|| Error::new("model_resources_split_invalid"))
    };
    let code = if m
        .caps
        .as_ref()
        .and_then(|c| c.max_memory_bytes)
        .is_some_and(|c| row["max_rss_bytes"].as_u64().is_some_and(|v| v > c))
    {
        Some("member_memory_cap_below_share")
    } else if m
        .caps
        .as_ref()
        .and_then(|c| c.storage_bytes)
        .is_some_and(|c| row["max_disk_bytes"].as_u64().is_some_and(|v| v > c))
    {
        Some("member_storage_cap_below_share")
    } else if n("max_rss_bytes")? > n("memory_ceiling_bytes")? {
        Some("member_memory_insufficient_for_share")
    } else if row["basis"] == "modelled"
        && m.available_storage_bytes
            .is_some_and(|c| row["disk_footprint_bytes"].as_u64().is_some_and(|v| v > c))
    {
        Some("member_disk_insufficient_for_share")
    } else {
        None
    };
    match code {
        Some(c) => Err(Error::node(c, &m.node_id)),
        None => Ok(()),
    }
}
struct Search<'a, F> {
    members: &'a [Member],
    profile: &'a LayerProfile,
    policy: &'a SplitPolicy,
    cumulative: Vec<u64>,
    budgets: Vec<u64>,
    targets: Vec<Ratio>,
    check: F,
    failed: BTreeSet<(usize, usize)>,
}
impl<F: Fn(&Stage, &Member) -> Result<()>> Search<'_, F> {
    fn stage(&self, i: usize, start: usize, end: usize) -> Result<Stage> {
        let mut bytes = self.cumulative[end] - self.cumulative[start];
        if i == 0 {
            bytes = add(bytes, self.profile.embedding_bytes)?;
        }
        if i == self.members.len() - 1 {
            bytes = add(bytes, self.profile.output_bytes)?;
            if self.profile.tie_word_embeddings {
                bytes = add(bytes, self.profile.embedding_bytes)?;
            }
        }
        Ok(Stage {
            node_id: self.members[i].node_id.clone(),
            start,
            end,
            stage_bytes: bytes,
            cost_bytes: add(
                scaled(
                    bytes,
                    [
                        self.policy.resident_factor_numerator,
                        self.policy.resident_factor_denominator,
                    ],
                )?,
                self.policy.runtime_overhead_bytes,
            )?,
            budget_bytes: self.budgets[i],
        })
    }
    fn check_stage(&self, i: usize, s: &Stage) -> Result<()> {
        (self.check)(s, &self.members[i])?;
        if s.cost_bytes > s.budget_bytes {
            return Err(Error::node("member_cannot_hold_share", &s.node_id));
        }
        Ok(())
    }
    fn ends(&self, i: usize, start: usize) -> Result<Vec<usize>> {
        if i == self.members.len() - 1 {
            return Ok(vec![self.profile.layer_bytes.len()]);
        }
        let mut ends = Vec::new();
        for end in start + 1..=self.profile.layer_bytes.len() - (self.members.len() - i - 1) {
            let score = Ratio::int(self.cumulative[end]).distance(self.targets[i])?;
            let mut pos = ends.len();
            for (j, (_, other)) in ends.iter().enumerate() {
                if score.cmp(*other)?.is_lt() {
                    pos = j;
                    break;
                }
            }
            ends.insert(pos, (end, score));
        }
        Ok(ends.into_iter().map(|(e, _)| e).collect())
    }
    fn suffix(&mut self, i: usize, start: usize) -> Result<Option<Vec<Stage>>> {
        if self.failed.contains(&(i, start)) {
            return Ok(None);
        }
        for end in self.ends(i, start)? {
            let stage = self.stage(i, start, end)?;
            if let Err(e) = self.check_stage(i, &stage) {
                if !e.is_resource() {
                    return Err(e);
                }
                continue;
            }
            if i == self.members.len() - 1 {
                return Ok(Some(vec![stage]));
            }
            if let Some(mut rest) = self.suffix(i + 1, end)? {
                rest.insert(0, stage);
                return Ok(Some(rest));
            }
        }
        self.failed.insert((i, start));
        Ok(None)
    }
}
pub fn split<F: Fn(&Stage, &Member) -> Result<()>>(
    members: &[Member],
    p: &LayerProfile,
    policy: &SplitPolicy,
    check: F,
) -> Result<Value> {
    if !(2..=8).contains(&members.len()) {
        return Err(Error::new("host_count_invalid"));
    }
    let mut seen = BTreeSet::new();
    for m in members {
        let n = &m.node_id;
        if n.is_empty()
            || n.len() > 128
            || !n.as_bytes()[0].is_ascii_alphanumeric()
            || !n
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"_.:-".contains(&c))
        {
            return Err(Error::new("member_capability_invalid"));
        }
        if !seen.insert(n) {
            return Err(Error::node("member_duplicate", n));
        }
        if m.memory_bytes == 0 {
            return Err(Error::node("member_capability_invalid", n));
        }
        if let Some(s) = &m.decode_tokens_per_second
            && s.as_f64().is_none_or(|v| !v.is_finite() || v <= 0.0)
        {
            return Err(Error::node("member_capability_invalid", n));
        }
    }
    if p.layer_bytes.len() < members.len() {
        return Err(Error::new("model_too_small_for_members"));
    }
    // The pure Python splitter accepts zero static bytes (its synthetic test
    // profiles use them). Header ingestion independently validates real models.
    let microbench = members.iter().all(|m| m.decode_tokens_per_second.is_some());
    let mut speeds = Vec::new();
    if microbench {
        for m in members {
            let v = m
                .decode_tokens_per_second
                .as_ref()
                .and_then(|v| v.as_f64())
                .ok_or_else(|| Error::new("member_capability_invalid"))?;
            speeds.push(Ratio::float(v)?);
        }
        let mut ordered = Vec::new();
        for speed in &speeds {
            let mut pos = ordered.len();
            for (i, v) in ordered.iter().enumerate() {
                if speed.cmp(*v)?.is_lt() {
                    pos = i;
                    break;
                }
            }
            ordered.insert(pos, *speed);
        }
        let median = ordered[(ordered.len() - 1) / 2];
        let low = ratio([
            policy.speed_clamp_low_numerator,
            policy.speed_clamp_low_denominator,
        ])?;
        let high = Ratio::int(policy.speed_clamp_high);
        if low.cmp(high)?.is_gt() || low.n == 0 {
            return Err(Error::new("invalid_policy"));
        }
        for speed in &mut speeds {
            *speed = speed.div(median)?;
            if speed.cmp(low)?.is_lt() {
                *speed = low;
            }
            if speed.cmp(high)?.is_gt() {
                *speed = high;
            }
        }
    } else {
        speeds = vec![Ratio::int(1); members.len()];
    }
    let mut budgets = Vec::new();
    let mut weights = Vec::new();
    for (m, speed) in members.iter().zip(speeds) {
        let budget = Ratio::int(m.memory_bytes)
            .mul(ratio([
                policy.memory_fraction_numerator,
                policy.memory_fraction_denominator,
            ])?)?
            .floor()?;
        if budget <= policy.runtime_overhead_bytes {
            return Err(Error::node("member_cannot_hold_share", &m.node_id));
        }
        budgets.push(budget);
        weights.push(Ratio::int(budget - policy.runtime_overhead_bytes).mul(speed)?);
    }
    let total = add(
        p.source_bytes()?,
        if p.tie_word_embeddings {
            p.embedding_bytes
        } else {
            0
        },
    )?;
    let mut cumulative = vec![p.embedding_bytes];
    let mut sum = p.embedding_bytes;
    for bytes in &p.layer_bytes {
        sum = add(sum, *bytes)?;
        cumulative.push(sum);
    }
    let all = weights.iter().try_fold(Ratio::int(0), |a, b| a.add(*b))?;
    let mut running = Ratio::int(0);
    let mut targets = Vec::new();
    for w in weights.iter().take(weights.len() - 1) {
        running = running.add(*w)?;
        targets.push(Ratio::int(total).mul(running)?.div(all)?);
    }
    let mut search = Search {
        members,
        profile: p,
        policy,
        cumulative,
        budgets,
        targets,
        check,
        failed: BTreeSet::new(),
    };
    let mut stages = Vec::new();
    let mut start = 0;
    for i in 0..members.len() {
        let end = *search
            .ends(i, start)?
            .first()
            .ok_or_else(|| Error::new("model_profile_invalid"))?;
        stages.push(search.stage(i, start, end)?);
        start = end;
    }
    let first_error = stages
        .iter()
        .enumerate()
        .find_map(|(i, s)| search.check_stage(i, s).err());
    if let Some(e) = first_error {
        if !e.is_resource() {
            return Err(e);
        }
        stages = search.suffix(0, 0)?.ok_or(e)?;
    }
    let rows: Vec<Value> = members
        .iter()
        .map(|m| {
            let mut row = json!({"node_id":m.node_id,"memory_bytes":m.memory_bytes});
            if let Some(s) = &m.decode_tokens_per_second {
                row["decode_tokens_per_second"] = json!(s);
            }
            row
        })
        .collect();
    let mut doc = json!({"protocol":"mycelium.a13_layer_split.v1","model_type":p.model_type,"num_layers":p.layer_bytes.len(),"profile_digest":crate::digest(&p.document(),true)?,"policy":policy,"members":rows,"weighting":if microbench{"memory_and_decode_microbench"}else{"memory"},"stages":stages});
    doc["split_digest"] = json!(crate::digest(&doc, true)?);
    Ok(doc)
}
/// Decode the oracle's member boundary without losing its specific refusal/node.
/// Consent is authenticated separately; this only parses planning observations.
pub fn members_from_document(value: &Value) -> Result<Vec<Member>> {
    let rows = value
        .as_array()
        .ok_or_else(|| Error::new("host_count_invalid"))?;
    if !(2..=8).contains(&rows.len()) {
        return Err(Error::new("host_count_invalid"));
    }
    let mut seen = BTreeSet::new();
    rows.iter()
        .map(|row| {
            let node = row["node_id"]
                .as_str()
                .filter(|n| {
                    !n.is_empty()
                        && n.len() <= 128
                        && n.as_bytes()[0].is_ascii_alphanumeric()
                        && n.bytes()
                            .all(|b| b.is_ascii_alphanumeric() || b"_.:-".contains(&b))
                })
                .ok_or_else(|| Error::new("member_capability_invalid"))?;
            if !seen.insert(node) {
                return Err(Error::node("member_duplicate", node));
            }
            if row["memory_bytes"].as_u64().is_none_or(|v| v == 0)
                || row
                    .get("decode_tokens_per_second")
                    .is_some_and(|v| v.as_f64().is_none_or(|v| !v.is_finite() || v <= 0.0))
            {
                return Err(Error::node("member_capability_invalid", node));
            }
            serde_json::from_value(python_typed_optionals(row))
                .map_err(|_| Error::node("member_capability_invalid", node))
        })
        .collect()
}

/// v0.1.4 reads a member's standing caps and free disk with `type(x) is int` guards (`check_member_caps`,
/// `check_members`): a value of any other type (bool, float, string, list, null, a falsy `caps`) is
/// ignored, and a negative integer is a cap/free disk below every share. This drops the ignored values
/// and clamps a negative integer to 0 (no stage has zero bytes, so it refuses exactly like the negative
/// value), before the typed decode. A truthy non-mapping `caps` stays an error (Python `AttributeError`).
/// Executed-oracle sweep: `tests/stage_sweep.rs` (FX-L7-stage-sweep-r10).
fn python_typed_optionals(row: &Value) -> Value {
    fn int(v: &Value) -> Option<Value> {
        v.as_i64()
            .map(|i| json!(u64::try_from(i).unwrap_or(0)))
            .or_else(|| v.as_u64().map(|u| json!(u)))
    }
    let mut out = row.clone();
    let Some(map) = out.as_object_mut() else {
        return out;
    };
    match map.get("available_storage_bytes").map(int) {
        Some(Some(v)) => {
            map.insert("available_storage_bytes".into(), v);
        }
        Some(None) => {
            map.remove("available_storage_bytes");
        }
        None => {}
    }
    let falsy = |v: &Value| match v {
        Value::Null => true,
        Value::Bool(b) => !b,
        Value::Number(n) => n.as_f64() == Some(0.0),
        Value::String(s) => s.is_empty(),
        Value::Array(a) => a.is_empty(),
        Value::Object(o) => o.is_empty(),
    };
    if let Some(caps) = map.get("caps").cloned() {
        match caps {
            Value::Object(c) => {
                let mut clean = serde_json::Map::new();
                for key in ["storage_bytes", "max_memory_bytes"] {
                    if let Some(v) = c.get(key).and_then(int) {
                        clean.insert(key.into(), v);
                    }
                }
                // Not consulted by the placement check: kept only when it is a valid value.
                if let Some(v) = c.get("max_wall_seconds").filter(|v| v.is_u64()) {
                    clean.insert("max_wall_seconds".into(), v.clone());
                }
                map.insert("caps".into(), Value::Object(clean));
            }
            other if falsy(&other) => {
                map.remove("caps");
            }
            _ => {}
        }
    }
    out
}

/// Pinned v0.1.4 resource registry; a dense shape is not permission to plan MoE.
/// The general backend support matrix is separate from A13 resource registration.
pub fn registered_model(model_id: &str, resolved_commit: &str) -> Result<ModelSpec> {
    #[derive(Deserialize)]
    struct Catalog {
        models: Vec<ModelSpec>,
    }
    let catalog: Catalog = serde_json::from_str(include_str!("../data/registered-models.json"))
        .map_err(|_| Error::new("model_registry_invalid"))?;
    catalog
        .models
        .into_iter()
        .find(|m| m.model_id == model_id && m.resolved_commit == resolved_commit)
        .ok_or_else(|| Error::new("model_resources_unregistered"))
}

/// Recompute a compiled split's source-bound rows without requiring live member
/// observations (the Python whole-model proposal deliberately has none).
pub fn member_resources(
    model: &ModelSpec,
    r: &ResourcePolicy,
    split: &Value,
    owner: Option<&str>,
    slice: bool,
    quant: &str,
) -> Result<Value> {
    if registered_model(&model.model_id, &model.resolved_commit)? != *model {
        return Err(Error::new("model_registry_mismatch"));
    }
    let invalid = || Error::new("model_resources_split_invalid");
    let stages = split["stages"]
        .as_array()
        .filter(|s| !s.is_empty())
        .ok_or_else(invalid)?;
    let mut previous = 0;
    let mut validated = Vec::new();
    for row in stages {
        let node = row["node_id"].as_str().ok_or_else(invalid)?;
        let start = row["start"]
            .as_u64()
            .and_then(|v| usize::try_from(v).ok())
            .ok_or_else(invalid)?;
        let end = row["end"]
            .as_u64()
            .and_then(|v| usize::try_from(v).ok())
            .ok_or_else(invalid)?;
        let size = row["stage_bytes"]
            .as_u64()
            .filter(|s| *s > 0)
            .ok_or_else(invalid)?;
        if start != previous || end <= start || end > model.shape.num_layers {
            return Err(invalid());
        }
        previous = end;
        let memory = split["members"]
            .as_array()
            .and_then(|m| m.iter().find(|m| m["node_id"].as_str() == Some(node)))
            .map(|m| m["memory_bytes"].clone())
            .unwrap_or(Value::Null);
        validated.push((
            Stage {
                node_id: node.into(),
                start,
                end,
                stage_bytes: size,
                cost_bytes: 0,
                budget_bytes: 0,
            },
            memory,
        ));
    }
    if previous != model.shape.num_layers {
        return Err(invalid());
    }
    let mut rows = Vec::new();
    for (stage, memory) in validated {
        if (memory.is_i64() || memory.is_u64()) && memory.as_u64().is_none_or(|v| v == 0) {
            return Err(Error::new("member_capability_invalid"));
        }
        let member = Member {
            node_id: stage.node_id.clone(),
            memory_bytes: memory.as_u64().unwrap_or(0),
            decode_tokens_per_second: None,
            caps: None,
            available_storage_bytes: None,
        };
        let mut row = member_resource(model, r, &stage, &member, owner, slice, quant)?;
        if !memory.is_i64() && !memory.is_u64() {
            row["memory_ceiling_bytes"] = Value::Null;
        }
        row["memory_bytes"] = memory;
        rows.push(row);
    }
    Ok(json!(rows))
}

pub fn plan(model: &Value, policy: &Value, resources: &Value, request: &Value) -> Result<Value> {
    let parse = || Error::new("planner_input_invalid");
    let model: ModelSpec = serde_json::from_value(model.clone()).map_err(|_| parse())?;
    if registered_model(&model.model_id, &model.resolved_commit)? != model {
        return Err(Error::new("model_registry_mismatch"));
    }
    if request["model_id"] != model.model_id {
        return Err(Error::new("model_identity_mismatch"));
    }
    let policy: SplitPolicy = serde_json::from_value(policy.clone()).map_err(|_| parse())?;
    let resources: ResourcePolicy =
        serde_json::from_value(resources.clone()).map_err(|_| parse())?;
    let members = members_from_document(&request["members"])?;
    let quant = request["quantization"].as_str().ok_or_else(parse)?;
    let slice = request["slice_mode"].as_bool().ok_or_else(parse)?;
    let owner = request["owner_node_id"].as_str();
    plan_members(&model, &policy, &resources, &members, quant, slice, owner)
}

/// `plan` over already-decoded inputs (the chain compile entry, `chain_compile::prepare`). A stage refused for a
/// resource reason carries the oracle's human message in `Error::detail` (`messages`).
pub fn plan_members(
    model: &ModelSpec,
    policy: &SplitPolicy,
    resources: &ResourcePolicy,
    members: &[Member],
    quant: &str,
    slice: bool,
    owner: Option<&str>,
) -> Result<Value> {
    let parse = || Error::new("planner_input_invalid");
    let profile = model.shape.profile()?;
    let split = split(members, &profile, policy, |s, m| {
        let row = member_resource(model, resources, s, m, owner, slice, quant)?;
        match require_member(&row, m) {
            Err(e)
                if e.code == "member_memory_insufficient_for_share"
                    && s.cost_bytes > s.budget_bytes =>
            {
                Err(Error::node("member_cannot_hold_share", &m.node_id))
            }
            Err(e) => Err(crate::messages::annotate(e, &model.shape.label, &row, m)),
            other => other,
        }
    })?;
    let stages: Vec<Stage> =
        serde_json::from_value(split["stages"].clone()).map_err(|_| parse())?;
    let rows: Result<Vec<_>> = stages
        .iter()
        .zip(members)
        .map(|(s, m)| member_resource(model, resources, s, m, owner, slice, quant))
        .collect();
    Ok(json!({"split":split,"members":rows?}))
}
