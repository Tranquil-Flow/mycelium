// SPDX-License-Identifier: AGPL-3.0-only
//! Reviewed qualification path for recorded device evidence.
//!
//! The per-attempt V2 importer (`device::import_device`) never emits a p99 and is unchanged.
//! This path pools samples across attempts ONLY when a hash-bound trial declaration names
//! them as independent and comparable (same build, model, device pair, method), and applies
//! the unchanged rule of `crate::statistics`: n >= 100 valid, exact, load-known samples per
//! (phase, model, build, device) cell, nearest rank. Exactness is derived from the sample
//! kind here, never from the declaration, the sample count or a driver flag, except a
//! carrier-RTT probe that explicitly marks itself `relay_rtt_exact`. Polled boundaries are
//! never exact. Input is read-only and bounded; unknown fields are never copied to output.
use crate::ImportResult;
use crate::device::{Source, attempt_name, finite, read, sha256_hex, validate_digest};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::Path,
};

#[derive(Debug, Clone, Copy, Default)]
pub struct Policy {
    /// Treat a client-observed elapsed time that ends at a polled terminal observation as
    /// not exact (requires native event timestamps). Default off: chat_total is the
    /// client's own monotonic elapsed time, resolution bounded by the polling interval.
    pub event_level_only: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Declaration {
    schema_version: u32,
    trial_id: String,
    reviewed_by: String,
    review_note: String,
    independence_basis: String,
    comparable: Comparable,
    attempts: Vec<DeclaredAttempt>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Comparable {
    source_sha: String,
    exe_sha256: String,
    model: String,
    method: String,
    devices: BTreeMap<String, DeclaredDevice>,
}
#[derive(Deserialize, Clone)]
#[serde(deny_unknown_fields)]
struct DeclaredDevice {
    hardware: String,
    hardware_basis: String,
    macos: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DeclaredAttempt {
    name: String,
    result_sha256: String,
    events_sha256: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct DeviceIdentity {
    pub hardware: String,
    pub hardware_provenance: String,
    pub hardware_basis: String,
    pub macos: String,
}
#[derive(Debug, Serialize)]
pub struct DeclarationReceipt {
    pub sha256: String,
    pub trial_id: String,
    pub reviewed_by: String,
    pub review_note: String,
    pub independence_basis: String,
    pub method: String,
    pub independence: String,
}
#[derive(Debug, Serialize)]
pub struct Cell {
    pub phase: String,
    pub variant: Option<String>,
    pub model: String,
    pub build: String,
    pub device: BTreeMap<String, DeviceIdentity>,
    pub unit: String,
    pub required_phase: bool,
    pub exact: bool,
    pub exactness_basis: String,
    pub n_observed: usize,
    pub n_failed: usize,
    pub n_load_unknown: usize,
    pub n_valid_exact_load_known: usize,
    pub p50: Option<f64>,
    pub p99: Option<f64>,
    pub descriptive_p50: Option<f64>,
    pub descriptive_p99: Option<f64>,
    pub status: String,
    pub blocked_reasons: Vec<String>,
    pub attempts: Vec<String>,
}
#[derive(Debug, Serialize)]
pub struct Qualification {
    pub schema_version: u32,
    pub method_version: String,
    pub declaration: DeclarationReceipt,
    pub files: Vec<Source>,
    pub attempts_pooled: Vec<String>,
    pub attempts_unpooled: Vec<String>,
    pub cells: Vec<Cell>,
}

#[derive(Clone, Copy, PartialEq)]
enum Basis {
    ClientElapsed,
    ReportedDuration,
    Polled,
    Carrier,
    AuxHttps,
    NoProducer,
}
impl Basis {
    fn label(self) -> &'static str {
        match self {
            Self::ClientElapsed => "client_observed_elapsed",
            Self::ReportedDuration => "reported_compiler_duration",
            Self::Polled => "polled_boundary",
            Self::Carrier => "carrier_probe_self_marked_exact",
            Self::AuxHttps => "public_https_elapsed",
            Self::NoProducer => "no_ingestion_producer",
        }
    }
}
struct PhaseDef {
    name: &'static str,
    unit: &'static str,
    required: bool,
    basis: Basis,
}
const PHASES: [PhaseDef; 13] = [
    PhaseDef {
        name: "compile",
        unit: "ms",
        required: true,
        basis: Basis::ReportedDuration,
    },
    PhaseDef {
        name: "publish",
        unit: "ms",
        required: true,
        basis: Basis::Polled,
    },
    PhaseDef {
        name: "start",
        unit: "ms",
        required: true,
        basis: Basis::Polled,
    },
    PhaseDef {
        name: "stop",
        unit: "ms",
        required: true,
        basis: Basis::Polled,
    },
    PhaseDef {
        name: "close",
        unit: "ms",
        required: true,
        basis: Basis::NoProducer,
    },
    PhaseDef {
        name: "relay_rtt",
        unit: "ms",
        required: true,
        basis: Basis::Carrier,
    },
    PhaseDef {
        name: "ttft",
        unit: "ms",
        required: true,
        basis: Basis::Polled,
    },
    PhaseDef {
        name: "decode",
        unit: "tokens/s",
        required: true,
        basis: Basis::Polled,
    },
    PhaseDef {
        name: "chat_total",
        unit: "ms",
        required: true,
        basis: Basis::ClientElapsed,
    },
    PhaseDef {
        name: "rollover",
        unit: "ms",
        required: true,
        basis: Basis::NoProducer,
    },
    PhaseDef {
        name: "restart",
        unit: "ms",
        required: true,
        basis: Basis::NoProducer,
    },
    PhaseDef {
        name: "quit",
        unit: "ms",
        required: true,
        basis: Basis::Polled,
    },
    PhaseDef {
        name: "relay_https_probe",
        unit: "ms",
        required: false,
        basis: Basis::AuxHttps,
    },
];

struct Obs {
    phase: &'static str,
    variant: Option<&'static str>,
    value: f64,
    load_known: bool,
    attempt: String,
}
#[derive(Default)]
struct Failures(BTreeMap<&'static str, usize>);

fn num(v: &Value, key: &str) -> Option<f64> {
    v.get(key).and_then(Value::as_f64).filter(|x| x.is_finite())
}
fn text<'a>(v: &'a Value, key: &str) -> Option<&'a str> {
    v.get(key).and_then(Value::as_str)
}
fn loads(hosts: Option<&Value>) -> BTreeSet<&'static str> {
    let mut known = BTreeSet::new();
    let Some(map) = hosts.and_then(Value::as_object) else {
        return known;
    };
    for name in ["owner", "laptop"] {
        let ok = map
            .get(name)
            .and_then(|h| h.get("load"))
            .and_then(Value::as_array)
            .is_some_and(|l| {
                l.len() == 3
                    && l.iter()
                        .all(|x| x.as_f64().is_some_and(|y| y.is_finite() && y >= 0.0))
            });
        if ok {
            known.insert(name);
        }
    }
    known
}
fn both(set: &BTreeSet<&'static str>) -> bool {
    set.contains("owner") && set.contains("laptop")
}
fn interval_known(rows: &[&Value]) -> bool {
    let mut seen = BTreeSet::new();
    for r in rows {
        seen.extend(loads(r.get("hosts")));
        seen.extend(loads(r.get("hosts_after")));
    }
    both(&seen)
}

fn chats_and_probes(attempt: &str, rows: &[Value], out: &mut Vec<Obs>, fails: &mut Failures) {
    for r in rows {
        match text(r, "event") {
            Some("chat") => {
                let completed = r.get("done").and_then(Value::as_bool) == Some(true)
                    && text(r, "terminal") == Some("completed")
                    && r.get("error").is_none_or(Value::is_null);
                if !completed {
                    *fails.0.entry("chat_total").or_default() += 1;
                    continue;
                }
                let known = both(&loads(r.get("hosts"))) && both(&loads(r.get("hosts_after")));
                for (phase, key, scale) in [
                    ("chat_total", "total_s", 1000.0),
                    ("ttft", "ttft_s", 1000.0),
                    ("decode", "decode_tok_s", 1.0),
                ] {
                    if let Some(v) = num(r, key).filter(|v| *v >= 0.0) {
                        out.push(Obs {
                            phase,
                            variant: None,
                            value: v * scale,
                            load_known: known,
                            attempt: attempt.into(),
                        });
                    }
                }
            }
            Some("relay_http_probe") => {
                let carrier = r.get("relay_rtt_exact").and_then(Value::as_bool) == Some(true)
                    && !text(r, "method")
                        .unwrap_or_default()
                        .contains("not carrier RTT");
                let phase = if carrier {
                    "relay_rtt"
                } else {
                    "relay_https_probe"
                };
                match (r.get("ok").and_then(Value::as_bool), num(r, "total_s")) {
                    (Some(true), Some(v)) if v >= 0.0 => out.push(Obs {
                        phase,
                        variant: None,
                        value: v * 1000.0,
                        load_known: both(&loads(r.get("hosts"))),
                        attempt: attempt.into(),
                    }),
                    _ => *fails.0.entry(phase).or_default() += 1,
                }
            }
            Some("stop_observation") => {
                if let Some(v) = num(r, "seconds_after_stop").filter(|v| *v >= 0.0) {
                    out.push(Obs {
                        phase: "stop",
                        variant: None,
                        value: v * 1000.0,
                        load_known: both(&loads(r.get("hosts"))),
                        attempt: attempt.into(),
                    });
                }
            }
            Some("quit_observation") => {
                if let Some(v) = num(r, "seconds_to_exit").filter(|v| *v >= 0.0) {
                    out.push(Obs {
                        phase: "quit",
                        variant: None,
                        value: v * 1000.0,
                        load_known: both(&loads(r.get("hosts"))),
                        attempt: attempt.into(),
                    });
                }
            }
            _ => {}
        }
    }
}

fn run_phases(attempt: &str, rows: &[Value], out: &mut Vec<Obs>) {
    let first_at = rows.first().and_then(|r| num(r, "at_epoch"));
    let run_of = |r: &Value| -> Option<String> {
        r.get("owner_run")
            .and_then(|o| text(o, "run_id"))
            .map(str::to_owned)
    };
    let mut by_run: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    for (i, r) in rows.iter().enumerate() {
        if text(r, "event") == Some("run_progress")
            && let Some(id) = run_of(r)
        {
            by_run.entry(id).or_default().push(i);
        }
    }
    let posts: Vec<(usize, f64)> = rows
        .iter()
        .enumerate()
        .filter(|(_, r)| {
            text(r, "event") == Some("owner_action")
                && text(r, "path") == Some("/api/demo/start")
                && r.get("http_status").and_then(Value::as_u64) == Some(200)
        })
        .filter_map(|(i, r)| num(r, "at_epoch").map(|t| (i, t)))
        .collect();
    for items in by_run.values() {
        let win: Vec<&Value> = items.iter().map(|i| &rows[*i]).collect();
        let step = |r: &Value| {
            r.get("owner_run")
                .and_then(|o| text(o, "step"))
                .map(str::to_owned)
        };
        let state = |r: &Value| {
            r.get("owner_run")
                .and_then(|o| text(o, "state"))
                .map(str::to_owned)
        };
        // compile: reported build_seconds once per run that began inside this window
        if let Some(first_at) = first_at {
            for r in &win {
                let o = r.get("owner_run");
                let build = o.and_then(|o| num(o, "build_seconds")).filter(|v| *v > 0.0);
                let started = o.and_then(|o| num(o, "started_at"));
                if let (Some(build), Some(started)) = (build, started)
                    && started >= first_at - 1.0
                {
                    let rng: Vec<&Value> = win
                        .iter()
                        .filter(|x| num(x, "at_epoch").is_some_and(|t| t >= started))
                        .copied()
                        .collect();
                    let hit = o
                        .and_then(|o| o.get("compilation_cache_hit"))
                        .and_then(Value::as_bool)
                        == Some(true);
                    out.push(Obs {
                        phase: "compile",
                        variant: Some(if hit { "cache_hit" } else { "cold_or_unknown" }),
                        value: build * 1000.0,
                        load_known: interval_known(&rng),
                        attempt: attempt.into(),
                    });
                    break;
                }
            }
        }
        // publish: first observed publish step to first observed later step (poll censored)
        let mut pub_first: Option<f64> = None;
        for r in &win {
            let Some(t) = num(r, "at_epoch") else {
                continue;
            };
            match (step(r).as_deref() == Some("publish"), pub_first) {
                (true, None) => pub_first = Some(t),
                (false, Some(p)) => {
                    let rng: Vec<&Value> = win
                        .iter()
                        .filter(|x| num(x, "at_epoch").is_some_and(|y| y >= p && y <= t))
                        .copied()
                        .collect();
                    out.push(Obs {
                        phase: "publish",
                        variant: None,
                        value: (t - p) * 1000.0,
                        load_known: interval_known(&rng),
                        attempt: attempt.into(),
                    });
                    break;
                }
                _ => {}
            }
        }
        // start: Start POST to first observed serving (composite, polled)
        let serving = win.iter().find(|r| {
            state(r).as_deref() == Some("serving") && step(r).as_deref() == Some("serving")
        });
        let post = items
            .first()
            .and_then(|first| posts.iter().rev().find(|(i, _)| i < first));
        if let (Some((_, p_t)), Some(s)) = (post, serving)
            && let Some(s_t) = num(s, "at_epoch")
            && s_t >= *p_t
        {
            let rng: Vec<&Value> = win
                .iter()
                .filter(|x| num(x, "at_epoch").is_some_and(|y| y >= *p_t && y <= s_t))
                .copied()
                .collect();
            out.push(Obs {
                phase: "start",
                variant: None,
                value: (s_t - p_t) * 1000.0,
                load_known: interval_known(&rng),
                attempt: attempt.into(),
            });
        }
    }
}

struct Recorded {
    macos: BTreeMap<&'static str, BTreeSet<String>>,
    hardware: BTreeMap<&'static str, BTreeSet<String>>,
}
fn recorded(rows: &[Value]) -> Recorded {
    let mut rec = Recorded {
        macos: BTreeMap::new(),
        hardware: BTreeMap::new(),
    };
    for r in rows {
        if !["installed", "host_summary", "host_admission"]
            .contains(&text(r, "event").unwrap_or_default())
        {
            continue;
        }
        let machine = match text(r, "host") {
            Some("owner") => "owner",
            Some("laptop") => "laptop",
            _ => continue,
        };
        let summary = r.get("summary");
        if let Some(v) = summary.and_then(|s| text(s, "os_version")) {
            rec.macos.entry(machine).or_default().insert(v.to_owned());
        }
        if let Some(v) = summary.and_then(|s| text(s, "hardware_model")) {
            rec.hardware
                .entry(machine)
                .or_default()
                .insert(v.to_owned());
        }
    }
    rec
}

fn violated(attempt: &str, field: &str) -> Box<dyn std::error::Error> {
    format!("declared_comparability_violated:{attempt}:{field}").into()
}

fn load_declaration(
    path: &Path,
    expected: &str,
    files: &mut Vec<Source>,
) -> ImportResult<(Declaration, String)> {
    validate_digest(expected, 64)?;
    if fs::symlink_metadata(path)?.file_type().is_symlink() {
        return Err("non_regular_evidence_file".into());
    }
    let bytes = fs::read(path)?;
    let digest = sha256_hex(&bytes)?;
    if !digest.eq_ignore_ascii_case(expected) {
        return Err("declaration_digest_mismatch".into());
    }
    files.push(Source {
        path: "trial-declaration.json".into(),
        sha256: Some(digest.clone()),
        bytes: Some(bytes.len() as u64),
        disposition: "TRIAL_DECLARATION".into(),
    });
    let d: Declaration = serde_json::from_slice(&bytes).map_err(|_| "declaration_invalid")?;
    if d.schema_version != 1 {
        return Err("declaration_schema_unsupported".into());
    }
    for field in [
        &d.reviewed_by,
        &d.review_note,
        &d.independence_basis,
        &d.trial_id,
        &d.comparable.method,
    ] {
        if field.trim().is_empty() {
            return Err("declaration_review_missing".into());
        }
    }
    validate_digest(&d.comparable.source_sha, 40)?;
    validate_digest(&d.comparable.exe_sha256, 64)?;
    if d.comparable.model.trim().is_empty() {
        return Err("declaration_model_missing".into());
    }
    if d.comparable
        .devices
        .keys()
        .map(String::as_str)
        .collect::<Vec<_>>()
        != ["laptop", "owner"]
    {
        return Err("declaration_devices_invalid".into());
    }
    for (name, dev) in &d.comparable.devices {
        if dev.hardware.trim().is_empty() {
            return Err(format!("declaration_hardware_missing:{name}").into());
        }
        if dev.hardware_basis.trim().is_empty() || dev.macos.trim().is_empty() {
            return Err(format!("declaration_device_incomplete:{name}").into());
        }
    }
    if d.attempts.is_empty() {
        return Err("declaration_no_attempts".into());
    }
    let mut seen = BTreeSet::new();
    for a in &d.attempts {
        if !attempt_name(&a.name) {
            return Err("declaration_attempt_name_invalid".into());
        }
        if !seen.insert(a.name.clone()) {
            return Err("declaration_duplicate_attempt".into());
        }
        validate_digest(&a.result_sha256, 64)?;
        validate_digest(&a.events_sha256, 64)?;
    }
    Ok((d, digest))
}

pub fn qualify_device(
    root: &Path,
    declaration: &Path,
    expected_sha256: &str,
    policy: Policy,
) -> ImportResult<Qualification> {
    if fs::symlink_metadata(root)?.file_type().is_symlink() {
        return Err("symlink_evidence_root".into());
    }
    let mut files = Vec::new();
    let (decl, decl_digest) = load_declaration(declaration, expected_sha256, &mut files)?;
    let mut obs = Vec::new();
    let mut fails = Failures::default();
    let mut hw_recorded: BTreeMap<&'static str, usize> = BTreeMap::new();
    for a in &decl.attempts {
        let dir = root.join(&a.name);
        if !fs::symlink_metadata(&dir)?.is_dir() {
            return Err("non_directory_attempt".into());
        }
        let mut bytes = Vec::new();
        for (file, want, disposition) in [
            ("RESULT.json", &a.result_sha256, "PROVENANCE_DECLARED"),
            ("events.jsonl", &a.events_sha256, "PRIMARY_EVENTS_DECLARED"),
        ] {
            let relative = format!("{}/{file}", a.name);
            let data = read(root, &relative, &mut files, disposition)?
                .ok_or_else(|| format!("declared_attempt_file_missing:{}:{file}", a.name))?;
            if !sha256_hex(&data)?.eq_ignore_ascii_case(want) {
                return Err(format!("declared_attempt_digest_mismatch:{}:{file}", a.name).into());
            }
            bytes.push(data);
        }
        let result: Value =
            serde_json::from_slice(&bytes[0]).map_err(|_| "device_evidence_json_invalid")?;
        let c = &decl.comparable;
        if text(&result, "source_sha") != Some(c.source_sha.as_str()) {
            return Err(violated(&a.name, "source_sha"));
        }
        if text(&result, "exe_sha256") != Some(c.exe_sha256.as_str()) {
            return Err(violated(&a.name, "exe_sha256"));
        }
        let model = result
            .get("member_join")
            .and_then(|m| m.get("run_model"))
            .and_then(|m| text(m, "selected"));
        if model != Some(c.model.as_str()) {
            return Err(violated(&a.name, "model"));
        }
        let mut rows = Vec::new();
        for (n, line) in bytes[1].split(|b| *b == b'\n').enumerate() {
            if line.iter().all(u8::is_ascii_whitespace) {
                continue;
            }
            let v: Value = serde_json::from_slice(line).map_err(|_| {
                format!(
                    "device_evidence_json_invalid:{}/events.jsonl:{}",
                    a.name,
                    n + 1
                )
            })?;
            if let Some(t) = num(&v, "at_epoch") {
                finite(t)?;
            }
            rows.push(v);
        }
        let rec = recorded(&rows);
        for machine in ["owner", "laptop"] {
            let dev = &c.devices[machine];
            match rec.macos.get(machine) {
                None => return Err(violated(&a.name, &format!("{machine}_macos_unrecorded"))),
                Some(s) if s.len() != 1 || !s.contains(&dev.macos) => {
                    return Err(violated(&a.name, &format!("{machine}_macos")));
                }
                _ => {}
            }
            if let Some(h) = rec.hardware.get(machine) {
                if h.len() != 1 || !h.contains(&dev.hardware) {
                    return Err(violated(&a.name, &format!("{machine}_hardware")));
                }
                *hw_recorded
                    .entry(if machine == "owner" {
                        "owner"
                    } else {
                        "laptop"
                    })
                    .or_default() += 1;
            }
        }
        chats_and_probes(&a.name, &rows, &mut obs, &mut fails);
        run_phases(&a.name, &rows, &mut obs);
    }
    let pooled: Vec<String> = decl.attempts.iter().map(|a| a.name.clone()).collect();
    let mut unpooled = Vec::new();
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let name = entry
            .file_name()
            .into_string()
            .map_err(|_| "non_utf8_attempt")?;
        if attempt_name(&name) && entry.file_type()?.is_dir() && !pooled.contains(&name) {
            unpooled.push(name);
        }
    }
    unpooled.sort();

    let c = &decl.comparable;
    let device: BTreeMap<String, DeviceIdentity> = c
        .devices
        .iter()
        .map(|(name, d)| {
            let all = hw_recorded.get(name.as_str()).copied().unwrap_or(0) == pooled.len();
            (
                name.clone(),
                DeviceIdentity {
                    hardware: d.hardware.clone(),
                    hardware_provenance: if all {
                        "recorded_and_declared"
                    } else {
                        "declared_only"
                    }
                    .into(),
                    hardware_basis: d.hardware_basis.clone(),
                    macos: d.macos.clone(),
                },
            )
        })
        .collect();
    let mut cells = Vec::new();
    for def in &PHASES {
        let variants: Vec<Option<&'static str>> = if def.name == "compile" {
            vec![Some("cache_hit"), Some("cold_or_unknown")]
        } else {
            vec![None]
        };
        for variant in variants {
            let mine: Vec<&Obs> = obs
                .iter()
                .filter(|o| o.phase == def.name && o.variant == variant)
                .collect();
            let exact = match def.basis {
                Basis::ClientElapsed => !policy.event_level_only,
                Basis::ReportedDuration | Basis::Carrier | Basis::AuxHttps => true,
                Basis::Polled | Basis::NoProducer => false,
            };
            let load_unknown = mine.iter().filter(|o| !o.load_known).count();
            let valid: Vec<f64> = if exact {
                mine.iter()
                    .filter(|o| o.load_known)
                    .map(|o| o.value)
                    .collect()
            } else {
                Vec::new()
            };
            let stats = crate::statistics(&valid, exact, true);
            let all: Vec<f64> = mine.iter().map(|o| o.value).collect();
            let mut sorted = all.clone();
            sorted.sort_by(f64::total_cmp);
            let d_p99 = (!sorted.is_empty()).then(|| sorted[(sorted.len() * 99).div_ceil(100) - 1]);
            let d_p50 = (!sorted.is_empty()).then(|| sorted[sorted.len().div_ceil(2) - 1]);
            let mut reasons: Vec<String> = Vec::new();
            if mine.is_empty() {
                reasons.push(
                    match def.name {
                        "close" => "no_close_samples",
                        "relay_rtt" => "no_carrier_rtt_samples",
                        _ if def.basis == Basis::NoProducer => "no_ingestion_producer",
                        _ => "no_samples",
                    }
                    .into(),
                );
            }
            if def.basis == Basis::NoProducer && !mine.is_empty() {
                reasons.push("no_ingestion_producer".into());
            }
            if !exact && def.basis == Basis::Polled {
                reasons.push("polled_boundary_not_exact".into());
            }
            if !exact && def.basis == Basis::ClientElapsed {
                reasons.push("client_observed_elapsed_not_event_level".into());
            }
            if !def.required {
                reasons.push("not_a_required_phase".into());
            }
            if variant == Some("cache_hit") {
                reasons.push("cache_hit_not_cold_compile".into());
            }
            if exact && valid.len() < 100 {
                reasons.push("fewer_than_100_valid_exact_load_known_samples".into());
            }
            let qualified = reasons.is_empty() && stats.p99_ms.is_some();
            let mut attempts: Vec<String> = mine.iter().map(|o| o.attempt.clone()).collect();
            attempts.sort();
            attempts.dedup();
            cells.push(Cell {
                phase: def.name.into(),
                variant: variant.map(str::to_owned),
                model: c.model.clone(),
                build: c.source_sha[..8].to_owned(),
                device: device.clone(),
                unit: def.unit.into(),
                required_phase: def.required,
                exact,
                exactness_basis: def.basis.label().into(),
                n_observed: mine.len(),
                n_failed: fails.0.get(def.name).copied().unwrap_or(0),
                n_load_unknown: load_unknown,
                n_valid_exact_load_known: valid.len(),
                p50: if qualified { stats.p50_ms } else { None },
                p99: if qualified { stats.p99_ms } else { None },
                descriptive_p50: d_p50,
                descriptive_p99: d_p99,
                status: if qualified {
                    "EMPIRICAL_P99"
                } else {
                    "BLOCKED"
                }
                .into(),
                blocked_reasons: reasons,
                attempts,
            });
        }
    }
    Ok(Qualification {
        schema_version: 3,
        method_version: "qualified-device-v1".into(),
        declaration: DeclarationReceipt {
            sha256: decl_digest,
            trial_id: decl.trial_id,
            reviewed_by: decl.reviewed_by,
            review_note: decl.review_note,
            independence_basis: decl.independence_basis,
            method: decl.comparable.method,
            independence: "declared_by_reviewer_not_statistically_tested".into(),
        },
        files,
        attempts_pooled: pooled,
        attempts_unpooled: unpooled,
        cells,
    })
}
