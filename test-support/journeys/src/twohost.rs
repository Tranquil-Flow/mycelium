// SPDX-License-Identifier: AGPL-3.0-only
//! Data-driven two-host joined-member journeys (L3C).
//!
//! The scenarios are DATA (`fixtures/twohost-scenarios.json`); this module validates them statically and
//! executes them over loopback HTTP against instances that an orchestrator (`window-run.sh`) launched
//! from fresh test HOMEs. A remote instance is reached through an `ssh -L` forward on the same loopback
//! port, so this code never speaks to a non-loopback address. Expected values come from executed oracle
//! output (probes, goldens recorded in the window), never from this file (RU-003).
use crate::{
    Result, compare, duration, http::Client, http::Response, io, process::digest_file, require,
};
use budgets::BudgetId;
use serde::{Deserialize, Deserializer};
use serde_json::{Map, Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::Write,
    path::Path,
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

pub const SCHEMA: &str = "mycelium.journeys.twohost.v1";
pub const GOLDEN_SCHEMA: &str = "mycelium.journeys.twohost.golden.v1";
pub const ENDPOINTS_SCHEMA: &str = "mycelium.journeys.twohost.endpoints.v1";
const OPS: [&str; 7] = [
    "http",
    "poll",
    "sse",
    "control",
    "sleep",
    "assert_latency",
    "note",
];
const WHO: [&str; 7] = [
    "owner",
    "member",
    "registry",
    "directory",
    "fresh",
    "legacy",
    "relay_proxy",
];
const METHODS: [&str; 3] = ["GET", "POST", "DELETE"];
const PROVENANCE: [&str; 3] = ["EXECUTED", "SOURCE", "RECORDED"];
/// Variables the orchestrator may supply in `endpoints.vars` in addition to the scenario defaults.
const BUILTIN_VARS: [&str; 7] = [
    "member_power",
    "owner_power",
    "relay_public_origin",
    "relay_proxy_port",
    "directory_origin",
    "relay_mode",
    "disclosure_digest_override",
];

fn some<'de, D: Deserializer<'de>>(d: D) -> std::result::Result<Option<Value>, D::Error> {
    Value::deserialize(d).map(Some)
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Expect {
    #[serde(default)]
    pub ptr: Option<String>,
    #[serde(default, deserialize_with = "some")]
    pub eq: Option<Value>,
    #[serde(default, deserialize_with = "some")]
    pub ne: Option<Value>,
    #[serde(default, rename = "in")]
    pub one_of: Option<Vec<Value>>,
    #[serde(default)]
    pub prefix: Option<String>,
    #[serde(default)]
    pub exists: Option<bool>,
    #[serde(default)]
    pub ge: Option<f64>,
    #[serde(default)]
    pub le: Option<f64>,
    #[serde(default)]
    pub text_contains: Option<String>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Tree {
    pub ptr: String,
    pub name: String,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct When {
    #[serde(default)]
    pub relay_mode: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SseExpect {
    #[serde(default)]
    pub last_event_in: Vec<String>,
    #[serde(default)]
    pub min_events: usize,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Step {
    pub id: String,
    pub who: String,
    pub op: String,
    #[serde(default)]
    pub method: Option<String>,
    #[serde(default)]
    pub path: Option<String>,
    #[serde(default)]
    pub body: Option<Value>,
    #[serde(default)]
    pub headers: BTreeMap<String, String>,
    #[serde(default)]
    pub auth: Option<bool>,
    #[serde(default)]
    pub status: Option<u16>,
    #[serde(default)]
    pub status_any: Vec<u16>,
    #[serde(default)]
    pub expect: Vec<Expect>,
    #[serde(default)]
    pub capture: BTreeMap<String, String>,
    #[serde(default)]
    pub capture_header: BTreeMap<String, String>,
    #[serde(default)]
    pub observe: Vec<String>,
    #[serde(default)]
    pub observe_tree: Vec<Tree>,
    #[serde(default)]
    pub until: Vec<Expect>,
    /// Poll steps: if any of these holds the poll fails at once (a terminal state that can never become ready).
    #[serde(default)]
    pub fail_when: Vec<Expect>,
    /// Poll steps: JSON pointers whose closed scalar values are logged to stderr every 30 s (progress trace).
    #[serde(default)]
    pub trace: Vec<String>,
    /// Poll steps: a second instance whose GET /api/status closed scalars (`trace_remote`) join every trace line.
    #[serde(default)]
    pub trace_who: Option<String>,
    #[serde(default)]
    pub trace_remote: Vec<String>,
    #[serde(default)]
    pub budget: Option<String>,
    #[serde(default)]
    pub max_polls_budget: Option<String>,
    #[serde(default)]
    pub key: bool,
    #[serde(default)]
    pub when: Option<When>,
    #[serde(default)]
    pub source: String,
    #[serde(default)]
    pub provenance: String,
    #[serde(default)]
    pub requires: Vec<String>,
    #[serde(default)]
    pub claims: Vec<String>,
    #[serde(default)]
    pub timing: Option<String>,
    #[serde(default)]
    pub action: Option<String>,
    #[serde(default)]
    pub times: Option<u32>,
    #[serde(default)]
    pub of_step: Option<String>,
    #[serde(default)]
    pub per_poll_budget: Option<String>,
    #[serde(default)]
    pub sse_expect: Option<SseExpect>,
    #[serde(default)]
    pub raw_text: bool,
    #[serde(default)]
    pub client: Option<String>,
    /// Poll steps: an HTTP read timeout is recorded as a measured latency (the F5 evidence) and the poll goes on,
    /// instead of failing a step that is not the key step. `assert_latency` then judges the recorded value.
    #[serde(default)]
    pub timeout_is_latency: bool,
    /// Poll steps in a RED session: a terminal state (`fail_when`) or an expired deadline is recorded as an
    /// observation; the key step that follows decides RED. In a green session it is still a failure.
    #[serde(default)]
    pub red_tolerant: bool,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Scenario {
    pub id: String,
    pub title: String,
    pub claims: Vec<String>,
    pub steps: Vec<Step>,
    #[serde(default)]
    pub instances: Vec<String>,
    #[serde(default)]
    pub depends_on: Option<String>,
    #[serde(default)]
    pub requires: Vec<String>,
    #[serde(default)]
    pub incident: Option<String>,
    #[serde(default)]
    pub overrides: Map<String, Value>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Session {
    pub id: String,
    pub target: String,
    pub expect: String,
    #[serde(default)]
    pub requires: Vec<String>,
    #[serde(default)]
    pub overrides: Map<String, Value>,
    pub scenarios: Vec<String>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TargetDef {
    pub label: String,
    pub sha256: Option<String>,
    #[serde(default)]
    pub source_sha: Option<String>,
    pub path: Option<String>,
    #[serde(default)]
    pub zip_sha256: Option<String>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Scenarios {
    pub schema: String,
    pub oracle: Value,
    pub claims: Vec<String>,
    pub budgets_allowed: Vec<String>,
    pub targets: BTreeMap<String, TargetDef>,
    pub defaults: Map<String, Value>,
    pub scenarios: Vec<Scenario>,
    pub sessions: Vec<Session>,
}

pub fn budget(name: &str) -> Result<BudgetId> {
    Ok(match name {
        "WorkerStartup" => BudgetId::WorkerStartup,
        "ModelSelection" => BudgetId::ModelSelection,
        "OwnerPoll" => BudgetId::OwnerPoll,
        "Compile" => BudgetId::Compile,
        "RelayWindow" => BudgetId::RelayWindow,
        "ObservedStart" => BudgetId::ObservedStart,
        "ObservedStop" => BudgetId::ObservedStop,
        "HarnessOwnerStatusCheck" => BudgetId::HarnessOwnerStatusCheck,
        _ => return Err(format!("budget:unknown_name:{name}")),
    })
}

pub fn load_scenarios(path: &Path) -> Result<Scenarios> {
    let raw = fs::read(path).map_err(|e| io("scenarios:read", e))?;
    let parsed: Scenarios =
        serde_json::from_slice(&raw).map_err(|e| format!("scenarios:parse:{e}"))?;
    require(parsed.schema == SCHEMA, "scenarios:schema")?;
    Ok(parsed)
}

fn templates(value: &Value, out: &mut Vec<String>) {
    match value {
        Value::String(text) => template_names(text, out),
        Value::Array(items) => items.iter().for_each(|v| templates(v, out)),
        Value::Object(map) => {
            for (key, inner) in map {
                template_names(key, out);
                templates(inner, out);
            }
        }
        _ => {}
    }
}
fn template_names(text: &str, out: &mut Vec<String>) {
    let mut rest = text;
    while let Some(start) = rest.find("{{") {
        let after = &rest[start + 2..];
        match after.find("}}") {
            Some(end) => {
                out.push(after[..end].trim().to_owned());
                rest = &after[end + 2..];
            }
            None => {
                out.push(String::from("<unterminated>"));
                return;
            }
        }
    }
}
fn step_templates(step: &Step) -> Vec<String> {
    let mut found = Vec::new();
    if let Some(path) = &step.path {
        template_names(path, &mut found);
    }
    if let Some(body) = &step.body {
        templates(body, &mut found);
    }
    for (key, value) in &step.headers {
        template_names(key, &mut found);
        template_names(value, &mut found);
    }
    for expect in step.expect.iter().chain(&step.until) {
        for value in [&expect.eq, &expect.ne].into_iter().flatten() {
            templates(value, &mut found);
        }
    }
    found
}

/// Machine-checkable static validation. Returns every problem, not just the first.
pub fn validate(data: &Scenarios, evidence_dir: Option<&Path>) -> Vec<String> {
    let mut problems = Vec::new();
    let mut ids = BTreeSet::new();
    let scenario_ids: BTreeSet<&str> = data.scenarios.iter().map(|s| s.id.as_str()).collect();
    for name in &data.budgets_allowed {
        if budget(name).is_err() {
            problems.push(format!("budgets_allowed:unknown:{name}"));
        }
    }
    let mut covered = BTreeSet::new();
    for scenario in &data.scenarios {
        if scenario.steps.is_empty() {
            problems.push(format!("scenario:{}:no_steps", scenario.id));
        }
        for claim in &scenario.claims {
            covered.insert(claim.clone());
        }
        for key in scenario.overrides.keys() {
            if !data.defaults.contains_key(key) {
                problems.push(format!(
                    "scenario:{}:override_not_a_default:{key}",
                    scenario.id
                ));
            }
        }
        if let Some(dep) = &scenario.depends_on
            && !scenario_ids.contains(dep.as_str())
        {
            problems.push(format!("scenario:{}:unknown_dependency:{dep}", scenario.id));
        }
        for step in &scenario.steps {
            let tag = format!("{}/{}", scenario.id, step.id);
            if !ids.insert(step.id.clone()) {
                problems.push(format!("step:{tag}:duplicate_id"));
            }
            if !OPS.contains(&step.op.as_str()) {
                problems.push(format!("step:{tag}:unknown_op:{}", step.op));
            }
            if !WHO.contains(&step.who.as_str()) {
                problems.push(format!("step:{tag}:unknown_who:{}", step.who));
            }
            if step.source.trim().is_empty() {
                problems.push(format!("step:{tag}:missing_source"));
            }
            let kind = step.provenance.split([':', ' ']).next().unwrap_or("");
            if !PROVENANCE.contains(&kind) {
                problems.push(format!("step:{tag}:bad_provenance:{}", step.provenance));
            }
            if matches!(step.op.as_str(), "http" | "poll" | "sse") {
                match step.method.as_deref() {
                    Some(m) if METHODS.contains(&m) => {}
                    other => problems.push(format!("step:{tag}:bad_method:{other:?}")),
                }
                match step.path.as_deref() {
                    Some(p) if p.starts_with('/') || p.starts_with("{{") => {}
                    other => problems.push(format!("step:{tag}:bad_path:{other:?}")),
                }
            }
            if step.op == "poll" && step.until.is_empty() {
                problems.push(format!("step:{tag}:poll_without_until"));
            }
            if step.op == "control" && step.action.is_none() {
                problems.push(format!("step:{tag}:control_without_action"));
            }
            if step.op == "assert_latency" && step.of_step.is_none() {
                problems.push(format!("step:{tag}:assert_latency_without_of_step"));
            }
            if step.op == "sleep" && (step.budget.is_none() || step.times.is_none()) {
                problems.push(format!("step:{tag}:sleep_needs_budget_and_times"));
            }
            for name in [&step.budget, &step.max_polls_budget, &step.per_poll_budget]
                .into_iter()
                .flatten()
            {
                if !data.budgets_allowed.contains(name) || budget(name).is_err() {
                    problems.push(format!("step:{tag}:budget_not_allowed:{name}"));
                }
            }
            for expect in step.expect.iter().chain(&step.until) {
                if expect.ptr.is_none() && expect.text_contains.is_none() {
                    problems.push(format!("step:{tag}:expect_without_target"));
                }
            }
            for claim in &step.claims {
                covered.insert(claim.clone());
            }
            if let (Some(dir), Some(rest)) =
                (evidence_dir, step.provenance.strip_prefix("EXECUTED:"))
                && let Err(error) = check_executed_reference(dir, rest)
            {
                problems.push(format!("step:{tag}:executed_reference:{error}"));
            }
        }
    }
    for claim in &data.claims {
        if !covered.contains(claim)
            && claim != "CL-L3-02"
            && claim != "CL-L3-03"
            && claim != "CL-L3-10"
            && claim != "CL-L1-04"
        {
            problems.push(format!("claim:{claim}:no_scenario"));
        }
    }
    for scenario in &data.scenarios {
        for claim in &scenario.claims {
            if claim != "CL-L3-04" && !data.claims.contains(claim) {
                problems.push(format!("scenario:{}:unknown_claim:{claim}", scenario.id));
            }
        }
    }
    problems.extend(validate_sessions(data));
    problems
}

fn check_executed_reference(dir: &Path, reference: &str) -> Result<()> {
    let file_part = reference.split([' ', '(']).next().unwrap_or("");
    let (file, key) = match file_part.split_once('#') {
        Some((f, k)) => (f, Some(k)),
        None => (file_part, None),
    };
    if !file.contains(".json") && !file.contains(".py") {
        // e.g. `L3-JOURNEYS-run-03 (loopback)`: a named earlier receipt, not a probe file.
        return Ok(());
    }
    let path = dir.join(file);
    let raw = fs::read(&path).map_err(|e| io("evidence:read", e))?;
    if let Some(key) = key {
        let value: Value = serde_json::from_slice(&raw).map_err(|_| "evidence:parse".to_owned())?;
        let result = value.get("result").unwrap_or(&value);
        require(
            result.get(key).is_some(),
            &format!("evidence:key_missing:{key}"),
        )?;
    }
    Ok(())
}

fn validate_sessions(data: &Scenarios) -> Vec<String> {
    let mut problems = Vec::new();
    for session in &data.sessions {
        if !data.targets.contains_key(&session.target) {
            problems.push(format!(
                "session:{}:unknown_target:{}",
                session.id, session.target
            ));
        }
        for key in session.overrides.keys() {
            if !data.defaults.contains_key(key) {
                problems.push(format!(
                    "session:{}:override_not_a_default:{key}",
                    session.id
                ));
            }
        }
        if !matches!(session.expect.as_str(), "green" | "red") {
            problems.push(format!("session:{}:bad_expect", session.id));
        }
        let mut seen: BTreeSet<&str> = BTreeSet::new();
        let mut defined: BTreeSet<String> = data.defaults.keys().cloned().collect();
        defined.extend(BUILTIN_VARS.iter().map(|v| (*v).to_owned()));
        defined.insert("disclosure_digest".into());
        for id in &session.scenarios {
            let Some(scenario) = data.scenarios.iter().find(|s| &s.id == id) else {
                problems.push(format!("session:{}:unknown_scenario:{id}", session.id));
                continue;
            };
            if let Some(dep) = &scenario.depends_on
                && !seen.contains(dep.as_str())
            {
                problems.push(format!(
                    "session:{}:{id}:dependency_not_earlier:{dep}",
                    session.id
                ));
            }
            for step in &scenario.steps {
                for name in step_templates(step) {
                    if !defined.contains(&name) {
                        problems.push(format!(
                            "session:{}:{}:undefined_variable:{name}",
                            session.id, step.id
                        ));
                    }
                }
                defined.extend(step.capture.keys().cloned());
                defined.extend(step.capture_header.keys().cloned());
            }
            seen.insert(id.as_str());
        }
    }
    problems
}

// ---------------------------------------------------------------------------------------------
// Execution
// ---------------------------------------------------------------------------------------------

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EndpointInstance {
    pub origin: String,
    #[serde(default)]
    pub launch: Option<String>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EndpointTarget {
    pub implementation: String,
    pub source_sha: String,
    pub executable: String,
    pub executable_sha256: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Endpoints {
    pub schema: String,
    #[serde(default)]
    pub target: Option<EndpointTarget>,
    #[serde(default)]
    pub instances: BTreeMap<String, EndpointInstance>,
    #[serde(default)]
    pub control: BTreeMap<String, Vec<String>>,
    #[serde(default)]
    pub vars: Map<String, Value>,
}

pub fn load_endpoints(path: &Path) -> Result<Endpoints> {
    let raw = fs::read(path).map_err(|e| io("endpoints:read", e))?;
    let parsed: Endpoints =
        serde_json::from_slice(&raw).map_err(|e| format!("endpoints:parse:{e}"))?;
    require(parsed.schema == ENDPOINTS_SCHEMA, "endpoints:schema")?;
    Ok(parsed)
}

pub fn verify_target(target: &EndpointTarget) -> Result<()> {
    require(
        matches!(
            target.implementation.as_str(),
            "python-v0.1.4" | "python-historical" | "rust"
        ),
        "target:implementation",
    )?;
    if target.implementation == "python-v0.1.4" {
        require(
            target.source_sha == crate::process::ORACLE_SOURCE
                && target.executable_sha256 == crate::process::ORACLE_EXE,
            "target:uncertified_oracle",
        )?;
    }
    require(
        Path::new(&target.executable).is_absolute(),
        "target:absolute_executable_required",
    )?;
    require(
        digest_file(Path::new(&target.executable))? == target.executable_sha256,
        "target:executable_digest_mismatch",
    )
}

pub fn subst(value: &Value, vars: &Map<String, Value>) -> Result<Value> {
    match value {
        Value::String(text) => {
            let trimmed = text.trim();
            if let Some(name) = trimmed
                .strip_prefix("{{")
                .and_then(|t| t.strip_suffix("}}"))
                && !name.contains("{{")
            {
                return vars
                    .get(name.trim())
                    .cloned()
                    .ok_or_else(|| format!("template:undefined:{}", name.trim()));
            }
            Ok(Value::String(subst_text(text, vars)?))
        }
        Value::Array(items) => Ok(Value::Array(
            items
                .iter()
                .map(|v| subst(v, vars))
                .collect::<Result<_>>()?,
        )),
        Value::Object(map) => {
            let mut out = Map::new();
            for (key, inner) in map {
                out.insert(subst_text(key, vars)?, subst(inner, vars)?);
            }
            Ok(Value::Object(out))
        }
        other => Ok(other.clone()),
    }
}
pub fn subst_text(text: &str, vars: &Map<String, Value>) -> Result<String> {
    let mut out = String::new();
    let mut rest = text;
    while let Some(start) = rest.find("{{") {
        out.push_str(&rest[..start]);
        let after = &rest[start + 2..];
        let end = after.find("}}").ok_or("template:unterminated")?;
        let name = after[..end].trim();
        let value = vars
            .get(name)
            .ok_or_else(|| format!("template:undefined:{name}"))?;
        match value {
            Value::String(s) => out.push_str(s),
            Value::Null | Value::Array(_) | Value::Object(_) => {
                return Err(format!("template:not_scalar:{name}"));
            }
            other => out.push_str(&other.to_string()),
        }
        rest = &after[end + 2..];
    }
    out.push_str(rest);
    Ok(out)
}

fn holds(expect: &Expect, doc: &Value, text: Option<&str>) -> bool {
    if let Some(needle) = &expect.text_contains {
        return text.is_some_and(|t| t.contains(needle.as_str()));
    }
    let Some(ptr) = &expect.ptr else { return false };
    let found = doc.pointer(ptr);
    if let Some(want) = expect.exists {
        return found.is_some() == want;
    }
    let Some(found) = found else { return false };
    if let Some(eq) = &expect.eq
        && found != eq
    {
        return false;
    }
    if let Some(ne) = &expect.ne
        && found == ne
    {
        return false;
    }
    if let Some(set) = &expect.one_of
        && !set.contains(found)
    {
        return false;
    }
    if let Some(prefix) = &expect.prefix
        && !found
            .as_str()
            .is_some_and(|s| s.starts_with(prefix.as_str()))
    {
        return false;
    }
    if let Some(low) = expect.ge
        && !found.as_f64().is_some_and(|n| n >= low)
    {
        return false;
    }
    if let Some(high) = expect.le
        && !found.as_f64().is_some_and(|n| n <= high)
    {
        return false;
    }
    true
}
fn resolve_expect(expect: &Expect, vars: &Map<String, Value>) -> Result<Expect> {
    let mut out = expect.clone();
    if let Some(v) = &expect.eq {
        out.eq = Some(subst(v, vars)?);
    }
    if let Some(v) = &expect.ne {
        out.ne = Some(subst(v, vars)?);
    }
    Ok(out)
}

/// Diagnostic: closed scalars of a status document (long strings and secret-looking keys dropped), one stderr line each.
fn dump_status(label: &str, doc: &Value) {
    let mut flat = BTreeMap::new();
    flatten("", doc, &mut flat);
    for (ptr, value) in flat {
        let lower = ptr.to_ascii_lowercase();
        if [
            "join",
            "link",
            "secret",
            "token",
            "key",
            "grant",
            "launch",
            "cookie",
            "digest",
            "signature",
            "address",
            "url",
            "path",
        ]
        .iter()
        .any(|w| lower.contains(w))
        {
            continue;
        }
        let shown = closed_scalar(Some(&value));
        if shown != "<string>" && shown != "<structure>" {
            eprintln!("dump:{label}:{ptr}={shown}");
        }
    }
}

fn flatten(prefix: &str, value: &Value, out: &mut BTreeMap<String, Value>) {
    match value {
        Value::Object(map) => {
            for (key, inner) in map {
                flatten(&format!("{prefix}/{key}"), inner, out);
            }
        }
        Value::Array(items) => {
            for (index, inner) in items.iter().enumerate() {
                flatten(&format!("{prefix}/{index}"), inner, out);
            }
        }
        scalar => {
            out.insert(prefix.to_owned(), scalar.clone());
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct Options {
    pub capabilities: BTreeSet<String>,
    pub relay_mode: String,
    /// When true a key-step failure is the expected RED observation (historical executable).
    pub expect_red: bool,
    /// Sign in again before a request when the session is older than this (default `JourneySessionRenewAfter`).
    /// The product signs a browser session out after 3600 s (`app.py:1995 @ f4e366e9`, `Max-Age=3600`);
    /// renew five minutes early. A 401 renews once more, whatever the age.
    pub resign_after: Option<Duration>,
}

pub struct Runner<'a> {
    data: &'a Scenarios,
    endpoints: Endpoints,
    options: Options,
    clients: BTreeMap<String, Client>,
    signed_at: BTreeMap<String, Instant>,
    pub vars: Map<String, Value>,
    marks: BTreeMap<String, Instant>,
    latencies: BTreeMap<String, Vec<u64>>,
    pub steps: Vec<Value>,
    pub observations: Vec<Value>,
    pub timings: BTreeMap<String, u64>,
    failed: bool,
    /// Failure inside the scenario being run (diagnostic-continue mode only needs this one).
    scenario_failed: bool,
    diagnostic_continue: bool,
    red_hits: u32,
}

impl<'a> Runner<'a> {
    pub fn new(data: &'a Scenarios, endpoints: Endpoints, options: Options) -> Self {
        let mut vars = data.defaults.clone();
        for (key, value) in &endpoints.vars {
            vars.insert(key.clone(), value.clone());
        }
        vars.insert("relay_mode".into(), json!(options.relay_mode));
        Self {
            data,
            endpoints,
            options,
            clients: BTreeMap::new(),
            signed_at: BTreeMap::new(),
            vars,
            marks: BTreeMap::new(),
            latencies: BTreeMap::new(),
            steps: Vec::new(),
            observations: Vec::new(),
            timings: BTreeMap::new(),
            failed: false,
            scenario_failed: false,
            // Diagnostic aid, never used for a recording: later scenarios still run after an earlier one failed
            // (the receipt stays FAIL, so nothing can be mistaken for a pass).
            diagnostic_continue: std::env::var_os("TWOHOST_DIAGNOSTIC_CONTINUE").is_some(),
            red_hits: 0,
        }
    }

    /// Instances come from the orchestrator; helper surfaces are derived from captured variables.
    fn origin_of(&self, who: &str) -> Result<String> {
        if let Some(instance) = self.endpoints.instances.get(who) {
            return Ok(instance.origin.clone());
        }
        let text = |name: &str| {
            self.vars
                .get(name)
                .and_then(Value::as_str)
                .map(str::to_owned)
                .ok_or_else(|| format!("endpoint:variable_missing:{name}"))
        };
        match who {
            "registry" => text("registry_origin"),
            "directory" => text("directory_origin"),
            "relay_proxy" => {
                let port = self
                    .vars
                    .get("relay_proxy_port")
                    .and_then(Value::as_u64)
                    .ok_or("endpoint:variable_missing:relay_proxy_port")?;
                Ok(format!("http://127.0.0.1:{port}"))
            }
            other => Err(format!("endpoint:missing_instance:{other}")),
        }
    }

    fn client(&mut self, who: &str, auth: bool) -> Result<&Client> {
        if !self.clients.contains_key(who) {
            let origin = self.origin_of(who)?;
            let mut client = Client::new(&origin)?;
            if auth {
                let launch = self
                    .endpoints
                    .instances
                    .get(who)
                    .and_then(|i| i.launch.as_deref())
                    .ok_or("endpoint:launch_missing")?;
                client.sign_in(launch)?;
                self.signed_at.insert(who.to_owned(), Instant::now());
            }
            self.clients.insert(who.to_owned(), client);
        }
        self.clients
            .get(who)
            .ok_or_else(|| "endpoint:client".into())
    }

    /// The orchestrator replaced an instance (restart/upgrade): forget its session.
    pub fn reload_instance(&mut self, who: &str, instance: EndpointInstance) {
        self.clients.remove(who);
        self.signed_at.remove(who);
        self.endpoints.instances.insert(who.to_owned(), instance);
    }

    /// The browser launch value is one-use and expires in two minutes (open-demo.html): sign in to every
    /// launched instance up front, and again right after a replacement.
    fn prime(&mut self) -> Result<()> {
        let names: Vec<String> = self
            .endpoints
            .instances
            .iter()
            .filter(|(_, i)| i.launch.is_some())
            .map(|(k, _)| k.clone())
            .collect();
        for name in names {
            self.client(&name, true)?;
        }
        Ok(())
    }

    pub fn run_session(&mut self, session_id: &str) -> Result<Value> {
        let session = self
            .data
            .sessions
            .iter()
            .find(|s| s.id == session_id)
            .ok_or("session:unknown")?
            .clone();
        require(
            session.expect == "red" || !self.options.expect_red,
            "session:expect_mismatch",
        )?;
        // Session overrides (owner 2 GB consent, the v0.1.3 model choice, ...) apply to every scenario of the session.
        for (key, value) in &session.overrides {
            self.vars.insert(key.clone(), value.clone());
        }
        self.prime()?;
        let started = Instant::now();
        let mut result = "PASS";
        for scenario_id in &session.scenarios {
            let scenario = self
                .data
                .scenarios
                .iter()
                .find(|s| &s.id == scenario_id)
                .ok_or("session:unknown_scenario")?
                .clone();
            let saved: Map<String, Value> = scenario
                .overrides
                .keys()
                .filter_map(|k| self.vars.get(k).map(|v| (k.clone(), v.clone())))
                .collect();
            for (key, value) in &scenario.overrides {
                self.vars.insert(key.clone(), value.clone());
            }
            let missing: Vec<_> = scenario
                .requires
                .iter()
                .filter(|r| !self.options.capabilities.contains(*r))
                .cloned()
                .collect();
            self.scenario_failed = false;
            for step in &scenario.steps {
                let skip = self.skip_reason(step, &missing);
                let aborted = if self.diagnostic_continue {
                    self.scenario_failed
                } else {
                    self.failed
                };
                if aborted || skip.is_some() {
                    let why = if aborted {
                        "aborted_after_failure".to_owned()
                    } else {
                        skip.unwrap_or_default()
                    };
                    if aborted || !why.starts_with("when:") {
                        self.steps.push(json!({"scenario":scenario.id,"id":step.id,"status":"NOT_RUN","reason":why}));
                    }
                    continue;
                }
                self.execute(&scenario.id, step);
            }
            for (key, value) in saved {
                self.vars.insert(key, value);
            }
        }
        let any_not_run = self.steps.iter().any(|s| s["status"] == "NOT_RUN");
        if self.failed {
            result = "FAIL";
        } else if self.options.expect_red {
            result = if self.red_hits > 0 {
                "RED_AS_EXPECTED"
            } else {
                "UNEXPECTED_GREEN"
            };
        } else if any_not_run {
            result = "PARTIAL";
        }
        Ok(json!({
            "schema":"mycelium.journeys.twohost.receipt.v1",
            "session":session.id,"target":session.target,"expect":session.expect,
            "result":result,"elapsed_ms":elapsed_ms(started),"steps":self.steps,
            "observations":self.observations,"timings":self.timings,"red_hits":self.red_hits,
        }))
    }

    fn skip_reason(&self, step: &Step, missing_scenario: &[String]) -> Option<String> {
        if let Some(when) = &step.when
            && let Some(mode) = &when.relay_mode
            && mode != &self.options.relay_mode
        {
            return Some(format!("when:relay_mode={mode}"));
        }
        if let Some(m) = missing_scenario.first() {
            return Some(format!("capability_missing:{m}"));
        }
        step.requires
            .iter()
            .find(|r| !self.options.capabilities.contains(*r))
            .map(|r| format!("capability_missing:{r}"))
    }

    fn execute(&mut self, scenario: &str, step: &Step) {
        let started = Instant::now();
        let outcome = self.run_step(step);
        let mut record = json!({"scenario":scenario,"id":step.id,"elapsed_ms":elapsed_ms(started)});
        match outcome {
            Ok(StepOutcome::Done { http, fields }) => {
                record["status"] = json!("PASS");
                if let Some(h) = http {
                    record["http_status"] = json!(h);
                }
                if let Some(fields) = fields {
                    self.observations
                        .push(json!({"id":step.id,"scenario":scenario,"fields":fields}));
                }
                if let Some(name) = &step.timing {
                    self.record_timing(name, started);
                }
            }
            Ok(StepOutcome::NotRun(reason)) => {
                record["status"] = json!("NOT_RUN");
                record["reason"] = json!(reason);
            }
            Err(error) => {
                if step.key && self.options.expect_red {
                    self.red_hits += 1;
                    record["status"] = json!("RED");
                    record["error"] = json!(error);
                    self.observations
                        .push(json!({"id":step.id,"scenario":scenario,"fields":{"red":true}}));
                } else {
                    self.failed = true;
                    self.scenario_failed = true;
                    record["status"] = json!("FAIL");
                    record["error"] = json!(error);
                }
            }
        }
        eprintln!(
            "step:{scenario}:{}:{}:{}ms{}",
            step.id,
            record["status"].as_str().unwrap_or("?"),
            record["elapsed_ms"],
            record["error"]
                .as_str()
                .map(|e| format!(":{}", e.chars().take(400).collect::<String>()))
                .unwrap_or_default()
        );
        self.steps.push(record);
    }

    fn record_timing(&mut self, name: &str, started: Instant) {
        if name.ends_with("_issued") {
            self.marks.insert(name.to_owned(), started);
        } else if name == "start_to_serving" {
            if let Some(mark) = self.marks.get("start_issued") {
                self.timings.insert(name.to_owned(), elapsed_ms(*mark));
            }
        } else {
            self.timings.insert(name.to_owned(), elapsed_ms(started));
        }
    }

    fn run_step(&mut self, step: &Step) -> Result<StepOutcome> {
        match step.op.as_str() {
            "note" => Ok(StepOutcome::NotRun("note".into())),
            "sleep" => {
                let each = duration(budget(step.budget.as_deref().ok_or("sleep:budget")?)?)?;
                for _ in 0..step.times.unwrap_or(1) {
                    thread::sleep(each);
                }
                Ok(StepOutcome::Done {
                    http: None,
                    fields: None,
                })
            }
            "control" => self.control(step),
            "assert_latency" => {
                let of = step.of_step.as_deref().ok_or("latency:of_step")?;
                let limit = duration(budget(
                    step.per_poll_budget.as_deref().ok_or("latency:budget")?,
                )?)?;
                let list = self.latencies.get(of).ok_or("latency:no_polls_recorded")?;
                require(!list.is_empty(), "latency:no_polls_recorded")?;
                let max = list.iter().copied().max().unwrap_or(0);
                self.timings.insert(format!("{of}_status_poll_max_ms"), max);
                require(
                    Duration::from_millis(max) <= limit,
                    &format!(
                        "latency:status_poll_exceeded:{max}ms>{}ms",
                        limit.as_millis()
                    ),
                )?;
                Ok(StepOutcome::Done {
                    http: None,
                    fields: Some(json!({"status_polls":list.len(),"all_within_budget":true})),
                })
            }
            "http" | "poll" | "sse" => self.request(step),
            other => Err(format!("op:unknown:{other}")),
        }
    }

    fn control(&mut self, step: &Step) -> Result<StepOutcome> {
        let action = step.action.as_deref().ok_or("control:action")?;
        let limit = duration(budget(step.budget.as_deref().unwrap_or("Compile"))?)?;
        if !self.run_control(action, &step.who, step.path.as_deref().unwrap_or(""), limit)? {
            return Ok(StepOutcome::NotRun(format!("control_unavailable:{action}")));
        }
        Ok(StepOutcome::Done {
            http: None,
            fields: Some(json!({"control":action,"ok":true})),
        })
    }

    /// Runs the orchestrator's control command. `false`: the orchestrator offers no such action.
    fn run_control(
        &mut self,
        action: &str,
        who: &str,
        path: &str,
        limit: Duration,
    ) -> Result<bool> {
        let Some(command) = self.endpoints.control.get(action).cloned() else {
            return Ok(false);
        };
        let (program, args) = command.split_first().ok_or("control:empty_command")?;
        let mut child = Command::new(program)
            .args(args)
            .arg(who)
            .arg(path)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| io("control:spawn", e))?;
        let deadline = Instant::now() + limit;
        loop {
            if child
                .try_wait()
                .map_err(|e| io("control:wait", e))?
                .is_some()
            {
                break;
            }
            if Instant::now() >= deadline {
                child.kill().map_err(|e| io("control:kill", e))?;
                child.wait().map_err(|e| io("control:reap", e))?;
                return Err(format!("control:{action}:deadline"));
            }
            thread::sleep(duration(BudgetId::OwnerPoll)?);
        }
        let output = child
            .wait_with_output()
            .map_err(|e| io("control:output", e))?;
        require(
            output.status.success(),
            &format!("control:{action}:nonzero_exit"),
        )?;
        // The command may report a replaced instance: {"instance":"owner","origin":...,"launch":...}.
        if let Some(line) = String::from_utf8_lossy(&output.stdout).lines().last()
            && let Ok(value) = serde_json::from_str::<Value>(line)
            && let (Some(replaced), Some(origin)) =
                (value["instance"].as_str(), value["origin"].as_str())
        {
            self.reload_instance(
                replaced,
                EndpointInstance {
                    origin: origin.to_owned(),
                    launch: value["launch"].as_str().map(str::to_owned),
                },
            );
            self.prime()?;
        }
        Ok(true)
    }

    /// New browser session for an authenticated instance (the orchestrator makes a new launch value through the
    /// app's own native-only `POST /native-session`). Without a `resign` control nothing happens.
    fn resign(&mut self, who: &str) -> Result<bool> {
        let limit = duration(BudgetId::WorkerStartup)?;
        self.run_control("resign", who, "", limit)
    }

    fn send(&mut self, step: &Step, auth: bool) -> Result<Response> {
        let method = step.method.as_deref().ok_or("request:method")?;
        let path = subst_text(step.path.as_deref().ok_or("request:path")?, &self.vars)?;
        let who = step.client.clone().unwrap_or_else(|| step.who.clone());
        let mut headers = Vec::new();
        for (key, value) in &step.headers {
            headers.push((subst_text(key, &self.vars)?, subst_text(value, &self.vars)?));
        }
        let body = match &step.body {
            Some(b) => serde_json::to_vec(&subst(b, &self.vars)?)
                .map_err(|_| "request:encode".to_owned())?,
            None => Vec::new(),
        };
        let kind = if method == "GET" {
            BudgetId::ModelSelection
        } else {
            BudgetId::WorkerStartup
        };
        if auth {
            let limit = match self.options.resign_after {
                Some(limit) => limit,
                None => duration(BudgetId::JourneySessionRenewAfter)?,
            };
            if self
                .signed_at
                .get(&who)
                .is_some_and(|t| t.elapsed() >= limit)
            {
                self.resign(&who)?;
            }
        }
        let first = self.send_once(&who, auth, method, &path, &headers, &body, kind);
        match first {
            Ok(response) if auth && response.status == 401 && self.resign(&who)? => {
                self.send_once(&who, auth, method, &path, &headers, &body, kind)
            }
            other => other,
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn send_once(
        &mut self,
        who: &str,
        auth: bool,
        method: &str,
        path: &str,
        headers: &[(String, String)],
        body: &[u8],
        kind: BudgetId,
    ) -> Result<Response> {
        let origin = self.origin_of(who)?;
        let client = self.client(who, auth)?;
        let mut sent: Vec<(&str, &str)> = Vec::new();
        for (key, value) in headers {
            sent.push((key.as_str(), value.as_str()));
        }
        if !body.is_empty()
            && !headers
                .iter()
                .any(|(k, _)| k.eq_ignore_ascii_case("content-type"))
        {
            sent.push(("Content-Type", "application/json"));
        }
        if auth {
            sent.push(("Origin", origin.as_str()));
            sent.push(("X-Mycelium-Action", "1"));
        }
        client.request(method, path, body, &sent, auth, kind)
    }

    fn accept_status(step: &Step, response: &Response) -> Result<()> {
        let ok = if !step.status_any.is_empty() {
            step.status_any.contains(&response.status)
        } else {
            response.status == step.status.unwrap_or(200)
        };
        if ok {
            return Ok(());
        }
        // Only a closed error code from the body is surfaced, never free text.
        let code = response
            .json()
            .ok()
            .and_then(|v| {
                v["error"]
                    .as_str()
                    .or_else(|| v["error"]["code"].as_str())
                    .map(str::to_owned)
            })
            .filter(|c| c.len() <= 96 && c.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_'));
        Err(match code {
            Some(code) => format!("http:unexpected_status:{}:error={code}", response.status),
            None => format!("http:unexpected_status:{}", response.status),
        })
    }

    /// Best-effort read of the traced second instance's /api/status (never fails the step).
    fn remote_trace_doc(&mut self, step: &Step) -> Option<Value> {
        let who = step.trace_who.as_ref()?;
        let mut probe = step.clone();
        probe.who = who.clone();
        probe.client = None;
        probe.method = Some("GET".into());
        probe.path = Some("/api/status".into());
        probe.body = None;
        let has_session = self
            .endpoints
            .instances
            .get(who)
            .is_some_and(|i| i.launch.is_some());
        self.send(&probe, has_session)
            .ok()
            .and_then(|r| r.json().ok())
    }

    fn request(&mut self, step: &Step) -> Result<StepOutcome> {
        // Instances launched by the orchestrator carry a one-use launch value; helper clients do not.
        let has_session = self
            .endpoints
            .instances
            .get(&step.who)
            .is_some_and(|i| i.launch.is_some());
        let auth = step.auth.unwrap_or(true) && has_session;
        let deadline_budget = match (&step.op[..], &step.budget) {
            ("poll", Some(name)) => Some(duration(budget(name)?)?),
            _ => None,
        };
        let started = Instant::now();
        let mut polls = 0u64;
        let response = loop {
            let t0 = Instant::now();
            let sent = self.send(step, auth);
            polls += 1;
            let response = match sent {
                Ok(response) => response,
                Err(error)
                    if step.timeout_is_latency
                        && deadline_budget.is_some()
                        && is_read_timeout(&error) =>
                {
                    // The owner did not answer within the client's read budget: that stall IS the measurement.
                    self.latencies
                        .entry(step.id.clone())
                        .or_default()
                        .push(elapsed_ms(t0));
                    eprintln!(
                        "trace:{}:t={}s:polls={polls}:read_timeout_recorded_as_latency:{error}",
                        step.id,
                        started.elapsed().as_secs()
                    );
                    if deadline_budget.is_some_and(|limit| started.elapsed() >= limit) {
                        if self.options.expect_red && step.red_tolerant {
                            return Ok(red_observation(None, "deadline"));
                        }
                        return Err(format!("poll:{}:deadline_after_{polls}_polls", step.id));
                    }
                    thread::sleep(duration(BudgetId::OwnerPoll)?);
                    continue;
                }
                Err(error) => return Err(error),
            };
            self.latencies
                .entry(step.id.clone())
                .or_default()
                .push(elapsed_ms(t0));
            let Some(limit) = deadline_budget else {
                break response;
            };
            Self::accept_status(step, &response)?;
            let doc = response.json()?;
            let remote = self.remote_trace_doc(step);
            let trace_line = |doc: &Value| -> String {
                let mut parts: Vec<String> = step
                    .trace
                    .iter()
                    .map(|p| format!("{p}={}", closed_scalar(doc.pointer(p))))
                    .collect();
                if let Some(who) = &step.trace_who {
                    parts.extend(step.trace_remote.iter().map(|p| {
                        format!(
                            "{who}{p}={}",
                            closed_scalar(remote.as_ref().and_then(|d| d.pointer(p)))
                        )
                    }));
                }
                parts.join(":")
            };
            for stop in &step.fail_when {
                let stop = resolve_expect(stop, &self.vars)?;
                if holds(&stop, &doc, None) {
                    dump_status("owner", &doc);
                    if let Some(r) = &remote {
                        dump_status("peer", r);
                    }
                    if self.options.expect_red && step.red_tolerant {
                        eprintln!(
                            "trace:{}:red_tolerant_terminal:{}",
                            step.id,
                            trace_line(&doc)
                        );
                        return Ok(red_observation(
                            Some(response.status),
                            &closed_scalar(stop.ptr.as_deref().and_then(|p| doc.pointer(p))),
                        ));
                    }
                    return Err(format!(
                        "poll:{}:terminal:{}={}:after_{polls}_polls:{}",
                        step.id,
                        stop.ptr.clone().unwrap_or_default(),
                        closed_scalar(stop.ptr.as_deref().and_then(|p| doc.pointer(p))),
                        trace_line(&doc)
                    ));
                }
            }
            if !step.trace.is_empty() && polls % TRACE_EVERY_POLLS == 1 {
                eprintln!(
                    "trace:{}:t={}s:polls={polls}:{}",
                    step.id,
                    started.elapsed().as_secs(),
                    trace_line(&doc)
                );
            }
            let mut ready = true;
            for until in &step.until {
                if !holds(&resolve_expect(until, &self.vars)?, &doc, None) {
                    ready = false;
                }
            }
            if ready {
                break response;
            }
            if started.elapsed() >= limit && self.options.expect_red && step.red_tolerant {
                return Ok(red_observation(Some(response.status), "deadline"));
            }
            require(
                started.elapsed() < limit,
                &format!("poll:{}:deadline_after_{polls}_polls", step.id),
            )?;
            thread::sleep(duration(BudgetId::OwnerPoll)?);
        };
        Self::accept_status(step, &response)?;
        let http = response.status;
        if step.op == "sse" {
            return self.finish_sse(step, &response, http);
        }
        let text = response.text().ok().map(str::to_owned);
        let doc = if step.raw_text || response.body.is_empty() {
            Value::Null
        } else {
            response.json()?
        };
        for expect in &step.expect {
            let resolved = resolve_expect(expect, &self.vars)?;
            require(
                holds(&resolved, &doc, text.as_deref()),
                &format!(
                    "expect_failed:{}:{}:got={}",
                    step.id,
                    expect.ptr.clone().unwrap_or_else(|| "text".into()),
                    closed_scalar(expect.ptr.as_deref().and_then(|p| doc.pointer(p)))
                ),
            )?;
        }
        for (var, ptr) in &step.capture {
            let found = doc
                .pointer(ptr)
                .ok_or_else(|| format!("capture_missing:{var}:{ptr}"))?;
            self.vars.insert(var.clone(), found.clone());
        }
        for (var, header) in &step.capture_header {
            let found = response
                .headers
                .get(&header.to_ascii_lowercase())
                .ok_or_else(|| format!("capture_header_missing:{var}:{header}"))?;
            // Set-Cookie may arrive as several lines: a cookie jar would send every name=value pair back.
            let value = found
                .lines()
                .map(|l| l.split(';').next().unwrap_or(l).trim())
                .collect::<Vec<_>>()
                .join("; ");
            self.vars.insert(var.clone(), json!(value));
        }
        let mut fields = BTreeMap::new();
        for ptr in &step.observe {
            let found = doc
                .pointer(ptr)
                .ok_or_else(|| format!("observe_missing:{ptr}"))?;
            require(
                !found.is_object() && !found.is_array(),
                "observe:non_scalar",
            )?;
            fields.insert(ptr.clone(), found.clone());
        }
        for tree in &step.observe_tree {
            // Absent subtrees are themselves an observation.
            match doc.pointer(&tree.ptr) {
                Some(found) => flatten(&format!("{}:", tree.name), found, &mut fields),
                None => {
                    fields.insert(format!("{}:", tree.name), Value::Null);
                }
            }
        }
        let wants_observation = !step.observe.is_empty() || !step.observe_tree.is_empty();
        Ok(StepOutcome::Done {
            http: Some(http),
            fields: wants_observation.then(|| json!(fields)),
        })
    }

    fn finish_sse(&mut self, step: &Step, response: &Response, http: u16) -> Result<StepOutcome> {
        let events = response.events()?;
        let want = step.sse_expect.clone().unwrap_or_default();
        require(events.len() >= want.min_events, "sse:too_few_events")?;
        let last = events
            .last()
            .and_then(|e| e["event"].as_str())
            .unwrap_or("")
            .to_owned();
        require(
            want.last_event_in.is_empty() || want.last_event_in.contains(&last),
            &format!("sse:last_event_unexpected:{last}"),
        )?;
        Ok(StepOutcome::Done {
            http: Some(http),
            fields: Some(json!({"event_count_ge_min":true,"last_event":last})),
        })
    }
}

/// A tolerated poll end in a RED session (recorded, never a pass of the key step that follows).
fn red_observation(http: Option<u16>, ended: &str) -> StepOutcome {
    StepOutcome::Done {
        http,
        fields: Some(json!({"poll_ended_without_ready":ended})),
    }
}

/// The client's own read budget ran out (`http.rs` reports the io kind of the failed read).
fn is_read_timeout(error: &str) -> bool {
    error == "http:deadline"
        || ["http:line:io:", "http:read:io:", "http:read_timeout:io:"]
            .iter()
            .any(|p| {
                error
                    .strip_prefix(p)
                    .is_some_and(|k| k == "WouldBlock" || k == "TimedOut")
            })
}

/// Diagnostics show only closed scalars (state words, numbers, booleans), never free text or secrets.
/// Progress trace cadence for poll steps, in polls (OwnerPoll is 500 ms, so about every 30 s).
const TRACE_EVERY_POLLS: u64 = 60;

fn closed_scalar(value: Option<&Value>) -> String {
    match value {
        None => "absent".into(),
        Some(Value::String(t))
            if t.len() <= 64
                && t.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-' || b == b'.') =>
        {
            t.clone()
        }
        Some(Value::String(_)) => "<string>".into(),
        Some(Value::Number(n)) => n.to_string(),
        Some(Value::Bool(b)) => b.to_string(),
        Some(Value::Null) => "null".into(),
        Some(_) => "<structure>".into(),
    }
}

enum StepOutcome {
    Done {
        http: Option<u16>,
        fields: Option<Value>,
    },
    NotRun(String),
}
fn elapsed_ms(from: Instant) -> u64 {
    u64::try_from(from.elapsed().as_millis()).unwrap_or(u64::MAX)
}

// ---------------------------------------------------------------------------------------------
// Golden: capture twice (independent Python runs) -> keep only fields equal in both.
// ---------------------------------------------------------------------------------------------

pub fn stabilize(first: &Value, second: &Value) -> Result<Value> {
    let a = first["observations"]
        .as_array()
        .ok_or("stabilize:a_not_array")?;
    let b = second["observations"]
        .as_array()
        .ok_or("stabilize:b_not_array")?;
    require(
        !a.is_empty() && a.len() == b.len(),
        "stabilize:observation_count",
    )?;
    let mut stable = Vec::new();
    let mut unstable = Vec::new();
    for (left, right) in a.iter().zip(b) {
        require(left["id"] == right["id"], "stabilize:id_order")?;
        let (lf, rf) = (
            left["fields"].as_object().ok_or("stabilize:fields")?,
            right["fields"].as_object().ok_or("stabilize:fields")?,
        );
        let mut keep = Map::new();
        for (key, value) in lf {
            if rf.get(key) == Some(value) {
                keep.insert(key.clone(), value.clone());
            } else {
                unstable.push(json!({"id":left["id"],"field":key}));
            }
        }
        for key in rf.keys().filter(|k| !lf.contains_key(*k)) {
            unstable.push(json!({"id":left["id"],"field":key}));
        }
        stable.push(json!({"id":left["id"],"scenario":left["scenario"],"fields":keep}));
    }
    Ok(json!({"observations":stable,"unstable":unstable}))
}

/// Golden = stabilized observation list. A new run must reproduce every stable field of every
/// observation, in order; fields listed unstable are ignored; extra new fields are a mismatch.
pub fn compare_golden(golden: &Value, new: &Value) -> Result<()> {
    compare(&strip(golden, golden)?, &strip(new, golden)?)
}
fn strip(doc: &Value, golden: &Value) -> Result<Value> {
    let unstable: BTreeSet<(String, String)> = golden["unstable"]
        .as_array()
        .map(|list| {
            list.iter()
                .filter_map(|u| {
                    Some((
                        u["id"].as_str()?.to_owned(),
                        u["field"].as_str()?.to_owned(),
                    ))
                })
                .collect()
        })
        .unwrap_or_default();
    let list = doc["observations"]
        .as_array()
        .ok_or("golden:observations")?;
    let mut out = Vec::new();
    for item in list {
        let id = item["id"].as_str().ok_or("golden:id")?;
        let mut fields = Map::new();
        for (key, value) in item["fields"].as_object().ok_or("golden:fields")? {
            if !unstable.contains(&(id.to_owned(), key.clone())) {
                fields.insert(key.clone(), value.clone());
            }
        }
        out.push(json!({"id":id,"fields":fields}));
    }
    Ok(Value::Array(out))
}

// ---------------------------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------------------------

fn flag<'a>(args: &'a [String], name: &str) -> Option<&'a str> {
    args.iter()
        .position(|a| a == name)
        .and_then(|i| args.get(i + 1))
        .map(String::as_str)
}
fn write_new(path: &Path, value: &Value) -> Result<()> {
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|e| io("receipt:create_new", e))?;
    let bytes = serde_json::to_vec_pretty(value).map_err(|_| "receipt:encode".to_owned())?;
    file.write_all(&bytes).map_err(|e| io("receipt:write", e))?;
    file.sync_all().map_err(|e| io("receipt:sync", e))
}
fn read_json(path: &str) -> Result<Value> {
    serde_json::from_slice(&fs::read(path).map_err(|e| io("json:read", e))?)
        .map_err(|_| "json:parse".into())
}

/// `journey-tests twohost <validate|plan|run|stabilize> ...`
pub fn cli(args: &[String]) -> Result<()> {
    let sub = args
        .first()
        .map(String::as_str)
        .ok_or("usage:twohost validate|plan|run|stabilize")?;
    match sub {
        "validate" => {
            let data = load_scenarios(Path::new(
                flag(args, "--scenarios").ok_or("usage:--scenarios")?,
            ))?;
            let evidence = flag(args, "--evidence").map(Path::new);
            let problems = validate(&data, evidence);
            let steps: usize = data.scenarios.iter().map(|s| s.steps.len()).sum();
            println!(
                "{}",
                json!({"scenarios":data.scenarios.len(),"steps":steps,"sessions":data.sessions.len(),"problems":problems})
            );
            require(problems.is_empty(), "twohost:validation_failed")
        }
        "plan" => {
            let data = load_scenarios(Path::new(
                flag(args, "--scenarios").ok_or("usage:--scenarios")?,
            ))?;
            let session_id = flag(args, "--session").ok_or("usage:--session")?;
            let caps: BTreeSet<String> = flag(args, "--capabilities")
                .map(|c| {
                    c.split(',')
                        .filter(|s| !s.is_empty())
                        .map(str::to_owned)
                        .collect()
                })
                .unwrap_or_default();
            let mode = flag(args, "--relay-mode").unwrap_or("external");
            println!("{}", plan(&data, session_id, &caps, mode)?);
            Ok(())
        }
        "stabilize" => {
            require(
                args.len() >= 5 && args[3] == "--out",
                "usage:twohost stabilize A.json B.json --out golden.json",
            )?;
            let (a, b) = (read_json(&args[1])?, read_json(&args[2])?);
            let mut golden = stabilize(&a, &b)?;
            golden["schema"] = json!(GOLDEN_SCHEMA);
            golden["session"] = a["session"].clone();
            write_new(Path::new(&args[4]), &golden)?;
            println!(
                "STABILIZED:observations={}:unstable_fields={}",
                golden["observations"].as_array().map_or(0, Vec::len),
                golden["unstable"].as_array().map_or(0, Vec::len)
            );
            Ok(())
        }
        "run" => run_cli(args),
        _ => Err("usage:twohost validate|plan|run|stabilize".into()),
    }
}

pub fn plan(
    data: &Scenarios,
    session_id: &str,
    caps: &BTreeSet<String>,
    mode: &str,
) -> Result<Value> {
    let session = data
        .sessions
        .iter()
        .find(|s| s.id == session_id)
        .ok_or("plan:unknown_session")?;
    let mut rows = Vec::new();
    for id in &session.scenarios {
        let scenario = data
            .scenarios
            .iter()
            .find(|s| &s.id == id)
            .ok_or("plan:unknown_scenario")?;
        for step in &scenario.steps {
            let mut status = "RUN".to_owned();
            if let Some(when) = &step.when
                && when.relay_mode.as_deref().is_some_and(|m| m != mode)
            {
                status = "SKIP_WHEN".into();
            } else if let Some(missing) = scenario
                .requires
                .iter()
                .chain(&step.requires)
                .find(|r| !caps.contains(*r))
            {
                status = format!("NOT_RUN:capability_missing:{missing}");
            }
            rows.push(json!({"scenario":scenario.id,"step":step.id,"op":step.op,"who":step.who,"key":step.key,"provenance":step.provenance.split(' ').next().unwrap_or(""),"status":status}));
        }
    }
    Ok(
        json!({"session":session.id,"target":session.target,"expect":session.expect,"relay_mode":mode,"steps":rows}),
    )
}

fn run_cli(args: &[String]) -> Result<()> {
    let data = load_scenarios(Path::new(
        flag(args, "--scenarios").ok_or("usage:--scenarios")?,
    ))?;
    let problems = validate(&data, None);
    require(
        problems.is_empty(),
        &format!("scenarios_invalid:{}", problems.join(";")),
    )?;
    let endpoints = load_endpoints(Path::new(
        flag(args, "--endpoints").ok_or("usage:--endpoints")?,
    ))?;
    let receipt_path = Path::new(flag(args, "--receipt").ok_or("usage:--receipt")?);
    require(!receipt_path.exists(), "receipt:already_exists")?;
    let session_id = flag(args, "--session").ok_or("usage:--session")?;
    let session = data
        .sessions
        .iter()
        .find(|s| s.id == session_id)
        .ok_or("session:unknown")?;
    let target = endpoints.target.clone();
    if let Some(t) = &target {
        verify_target(t)?;
    }
    let options = Options {
        capabilities: flag(args, "--capabilities")
            .map(|c| {
                c.split(',')
                    .filter(|s| !s.is_empty())
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default(),
        relay_mode: flag(args, "--relay-mode").unwrap_or("external").to_owned(),
        expect_red: session.expect == "red",
        resign_after: None,
    };
    let mut runner = Runner::new(&data, endpoints, options);
    let mut receipt = runner.run_session(session_id)?;
    if let Some(t) = &target {
        verify_target(t)?; // the executable bytes are unchanged after the run
        receipt["target_identity"] = json!({"implementation":t.implementation,"source_sha":t.source_sha,"executable_sha256":t.executable_sha256,"verified_before_and_after":true});
    }
    if matches!(
        receipt["result"].as_str(),
        Some("PASS" | "PARTIAL" | "RED_AS_EXPECTED")
    ) {
        if let Some(path) = flag(args, "--capture") {
            let golden = json!({"schema":GOLDEN_SCHEMA,"session":session_id,"observations":receipt["observations"],"unstable":[]});
            write_new(Path::new(path), &golden)?;
        } else if let Some(path) = flag(args, "--golden") {
            let golden = read_json(path)?;
            if let Err(error) = compare_golden(&golden, &receipt) {
                receipt["result"] = json!("FAIL");
                receipt["error"] = json!(error);
            }
        }
    }
    write_new(receipt_path, &receipt)?;
    println!(
        "{}:session={}:steps={}:observations={}",
        receipt["result"].as_str().unwrap_or("?"),
        session_id,
        receipt["steps"].as_array().map_or(0, Vec::len),
        receipt["observations"].as_array().map_or(0, Vec::len)
    );
    match receipt["result"].as_str() {
        Some("PASS" | "PARTIAL" | "RED_AS_EXPECTED") => Ok(()),
        _ => Err(receipt["error"]
            .as_str()
            .unwrap_or("twohost:session_failed")
            .to_owned()),
    }
}
