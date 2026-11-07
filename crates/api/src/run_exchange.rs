// SPDX-License-Identifier: AGPL-3.0-only
//! `run_exchange.RunExchange` (`run_exchange.py:1-321 @ f4e366e9`), the owner side's in-memory state (SE-L10-EXCHANGE,
//! SE-L10-REPORTS-PORT): the one announcement every member reads (phase, run and deployment ids, the pinned
//! model, per-node bootstrap offer / slice plan / work limits / resources), the fence that makes an idle refresh
//! conditional at the swap, the model-source hint, and the inbox of member reports the `descriptors` step reads
//! (`reports()`: the latest report of each node plus the owner's `received_at`). The signed-control routes
//! (`/managed-control/demo-run/read|report`) are a separate seam (SE-L10-EXCHANGE-ROUTES): they verify a member's
//! envelope on the seed mailbox and then call [`RunExchange::announcement`] and `report`.
use crate::{preferences::dumps_sorted, wire::Json};
use serde_json::{Map, Value};
use std::{
    collections::BTreeMap,
    sync::{Mutex, MutexGuard},
};

pub const ANNOUNCEMENT: &str = "mycelium.a13_demo_run_announcement.v1";
pub const MEMBER_REPORT: &str = "mycelium.a13_demo_member_report.v1";
/// `PHASES` (`run_exchange.py:55`).
pub const PHASES: [&str; 7] = [
    "idle", "collect", "observe", "compile", "work", "serving", "stopped",
];
/// `MEMBER_STATES` (`run_exchange.py:56-59`).
pub const MEMBER_STATES: [&str; 16] = [
    "waiting_for_consent",
    "idle",
    "joined",
    "downloading_model",
    "model_ready",
    "model_mismatch",
    "reported",
    "preparing",
    "bootstrap_running",
    "cleaning_up",
    "downloading_slice",
    "slice_ready",
    "work_running",
    "serving",
    "stopped",
    "error",
];
const REPORT_KEYS: [&str; 9] = [
    "protocol",
    "run_id",
    "deployment_id",
    "state",
    "detail",
    "model",
    "progress",
    "descriptor",
    "issued_at",
];
/// `MAX_REPORT_BYTES = 32 * 1024` (`run_exchange.py:67`), `MAX_CLOCK_RTT_SECONDS = 60.0` (:64), the
/// descriptor's `16384` (:101), the model strings' `128` (:96), `2**50` (:98): source constants.
const MAX_REPORT_CHARS: usize = 32 * 1024;
const MAX_CLOCK_RTT_SECONDS: f64 = 60.0;
const MAX_DESCRIPTOR_CHARS: usize = 16_384;
const MAX_MODEL_STRING: usize = 128;
const MAX_PROGRESS_BYTES: u64 = 1 << 50;
const NODE_EXTRA: [&str; 5] = [
    "bootstrap_offer_id",
    "node_slice_plan",
    "work_limits",
    "resources",
    "artifact_reuse_key",
];
const WORK_LIMIT_FIELDS: [&str; 4] = [
    "max_rss_bytes",
    "max_disk_bytes",
    "max_wall_seconds",
    "max_commands",
];
/// `model_resources.PUBLIC_RESOURCE_FIELDS` (`model_resources.py:534`).
const PUBLIC_RESOURCE_FIELDS: [&str; 11] = [
    "node_id",
    "start",
    "end",
    "stage_source_bytes",
    "resident_bytes",
    "kv_bytes",
    "load_peak_bytes",
    "max_rss_bytes",
    "max_disk_bytes",
    "disk_footprint_bytes",
    "basis",
];

/// A closed refusal. `class` is the oracle's exception class (`ValueError`, or `DemoExchangeError` for a
/// failed `_require`), `text` its message, `code` the `.code` a `DemoExchangeError` carries.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ExchangeError {
    pub class: &'static str,
    pub text: String,
    pub code: Option<String>,
}
fn value_error(text: &str) -> ExchangeError {
    ExchangeError {
        class: "ValueError",
        text: text.to_owned(),
        code: None,
    }
}
fn require(condition: bool, code: &str) -> Result<(), ExchangeError> {
    if condition {
        Ok(())
    } else {
        Err(ExchangeError {
            class: "DemoExchangeError",
            text: code.to_owned(),
            code: Some(code.to_owned()),
        })
    }
}

/// `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` (`_optional_segment`).
fn optional_segment(value: &Value) -> bool {
    match value {
        Value::Null => true,
        Value::String(s) => {
            let n = s.chars().count();
            let mut chars = s.chars();
            (1..=128).contains(&n)
                && chars.next().is_some_and(|c| c.is_ascii_alphanumeric())
                && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
        }
        _ => false,
    }
}
/// `_CODE = [a-z][a-z0-9_]{0,95}\Z`.
fn closed_code(text: &str) -> bool {
    let n = text.chars().count();
    let mut chars = text.chars();
    (1..=96).contains(&n)
        && chars.next().is_some_and(|c| c.is_ascii_lowercase())
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}
/// `type(x) in (int, float)` and finite (a JSON bool is neither).
fn finite_number(value: &Value) -> Option<f64> {
    match value {
        Value::Number(n) => n.as_f64().filter(|f| f.is_finite()),
        _ => None,
    }
}
/// `type(x) is int`.
fn plain_int(value: &Value) -> Option<i128> {
    match value {
        Value::Number(n) => n
            .as_i64()
            .map(i128::from)
            .or_else(|| n.as_u64().map(i128::from)),
        _ => None,
    }
}

/// `validate_report`: a closed member report (codes and counters, plus an optional signed descriptor).
pub fn validate_report(report: &Value) -> Result<Value, ExchangeError> {
    let invalid = "demo_report_invalid";
    let raw = dumps_sorted(report);
    let optional: Vec<&str> = vec!["clock_rtt_seconds"];
    let Some(map) = report.as_object() else {
        return Err(require(false, invalid).unwrap_err());
    };
    require(
        raw.chars().count() <= MAX_REPORT_CHARS
            && REPORT_KEYS.iter().all(|k| map.contains_key(*k))
            && map
                .keys()
                .all(|k| REPORT_KEYS.contains(&k.as_str()) || optional.contains(&k.as_str()))
            && map["protocol"] == MEMBER_REPORT,
        invalid,
    )?;
    let state_ok = map["state"]
        .as_str()
        .is_some_and(|s| MEMBER_STATES.contains(&s));
    let detail_ok = match &map["detail"] {
        Value::Null => true,
        Value::String(s) => closed_code(s),
        _ => false,
    };
    require(
        optional_segment(&map["run_id"])
            && optional_segment(&map["deployment_id"])
            && state_ok
            && detail_ok
            && finite_number(&map["issued_at"]).is_some(),
        invalid,
    )?;
    let rtt_ok = map.get("clock_rtt_seconds").is_none_or(|rtt| {
        finite_number(rtt).is_some_and(|f| (0.0..=MAX_CLOCK_RTT_SECONDS).contains(&f))
    });
    require(rtt_ok, invalid)?;
    let model_ok = match &map["model"] {
        Value::Null => true,
        Value::Object(m) => {
            m.len() == 2
                && ["model_id", "resolved_commit"].iter().all(|k| {
                    m.get(*k)
                        .and_then(Value::as_str)
                        .is_some_and(|s| s.chars().count() <= MAX_MODEL_STRING)
                })
        }
        _ => false,
    };
    require(model_ok, invalid)?;
    let progress_ok = match &map["progress"] {
        Value::Null => true,
        Value::Object(p) => {
            p.len() == 2
                && ["done_bytes", "total_bytes"].iter().all(|k| {
                    p.get(*k)
                        .and_then(plain_int)
                        .is_some_and(|v| (0..=i128::from(MAX_PROGRESS_BYTES)).contains(&v))
                })
        }
        _ => false,
    };
    require(progress_ok, invalid)?;
    let descriptor_ok = match &map["descriptor"] {
        Value::Null => true,
        d @ Value::Object(_) => dumps_sorted(d).chars().count() <= MAX_DESCRIPTOR_CHARS,
        _ => false,
    };
    require(descriptor_ok, invalid)?;
    Ok(report.clone())
}

/// `_check_node_extra`.
fn check_node_extra(extra: &Map<String, Value>) -> Result<(), ExchangeError> {
    let invalid = || value_error("demo_announcement_invalid");
    if !extra.keys().all(|k| NODE_EXTRA.contains(&k.as_str())) {
        return Err(invalid());
    }
    if let Some(reuse) = extra.get("artifact_reuse_key").filter(|v| !v.is_null()) {
        let ok = reuse.as_str().is_some_and(|s| {
            s.len() == 64
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        });
        if !ok {
            return Err(invalid());
        }
    }
    if let Some(limits) = extra.get("work_limits").filter(|v| !v.is_null()) {
        let ok = limits.as_object().is_some_and(|l| {
            l.len() == WORK_LIMIT_FIELDS.len()
                && WORK_LIMIT_FIELDS.iter().all(|k| l.contains_key(*k))
                && l.values().all(|v| plain_int(v).is_some_and(|n| n > 0))
        });
        if !ok {
            return Err(invalid());
        }
    }
    if let Some(resources) = extra.get("resources").filter(|v| !v.is_null()) {
        let ok = resources.as_object().is_some_and(|r| {
            r.len() == PUBLIC_RESOURCE_FIELDS.len()
                && PUBLIC_RESOURCE_FIELDS.iter().all(|k| r.contains_key(*k))
        });
        if !ok {
            return Err(invalid());
        }
    }
    Ok(())
}

/// What `announce(...)` takes (`run_exchange.py:139-160`).
#[derive(Clone, Debug, Default)]
pub struct Announce {
    pub phase: String,
    pub run_id: Option<String>,
    pub deployment_id: Option<String>,
    pub model: Option<Value>,
    pub slice_mode: bool,
    pub manifest_digest: Option<String>,
    pub work_limits: Option<Value>,
    pub per_node: Map<String, Value>,
}

/// `prepare_announcement`'s result: validated and serialised, committed by an in-memory swap.
#[derive(Clone, Debug)]
pub struct Prepared {
    document: Json,
    nodes: Map<String, Value>,
}
impl Prepared {
    pub fn document(&self) -> Value {
        self.document.to_value()
    }
    pub fn nodes(&self) -> Value {
        Value::Object(self.nodes.clone())
    }
}

/// The hint provider: `Err` is any exception inside it (the exchange then publishes no hint).
pub type HintProvider = Box<dyn Fn() -> Result<Option<Value>, ()> + Send + Sync>;
pub type ClockFn = Box<dyn Fn() -> f64 + Send + Sync>;

struct Inner {
    announcement: Json,
    per_node: Map<String, Value>,
    reports: BTreeMap<String, Value>,
    issued: BTreeMap<String, f64>,
}

/// Owner side of the demo-run exchange.
pub struct RunExchange {
    clock: ClockFn,
    inner: Mutex<Inner>,
    provider: Mutex<Option<HintProvider>>,
    incarnation: String,
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}
/// `'owner-process-' + uuid.uuid4().hex`.
fn incarnation() -> String {
    use std::io::Read;
    let mut u = [0u8; 16];
    // The randomness is a non-authorizing hint: a failed read leaves a zero-filled (still well-formed) one.
    std::fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut u))
        .unwrap_or(());
    u[6] = (u[6] & 0x0f) | 0x40;
    u[8] = (u[8] & 0x3f) | 0x80;
    format!(
        "owner-process-{}",
        u.iter().map(|b| format!("{b:02x}")).collect::<String>()
    )
}

impl RunExchange {
    /// `RunExchange(mailbox, clock=)`: announces phase `idle`.
    pub fn new(clock: ClockFn) -> Self {
        let exchange = Self {
            clock,
            inner: Mutex::new(Inner {
                announcement: Json::Obj(Vec::new()),
                per_node: Map::new(),
                reports: BTreeMap::new(),
                issued: BTreeMap::new(),
            }),
            provider: Mutex::new(None),
            incarnation: incarnation(),
        };
        let idle = Announce {
            phase: "idle".into(),
            ..Announce::default()
        };
        if let Ok(prepared) = exchange.prepare_announcement(idle) {
            exchange.commit_announcement(&prepared, false);
        }
        exchange
    }

    /// `announce`: prepare, commit, answer the committed announcement.
    pub fn announce(&self, announce: Announce) -> Result<Json, ExchangeError> {
        let prepared = self.prepare_announcement(announce)?;
        self.commit_announcement(&prepared, false);
        Ok(self.announcement(None))
    }

    /// `prepare_announcement`: validate and serialise outside any lifecycle lock; no transport on commit.
    pub fn prepare_announcement(&self, announce: Announce) -> Result<Prepared, ExchangeError> {
        if !PHASES.contains(&announce.phase.as_str()) {
            return Err(value_error("demo_announcement_invalid"));
        }
        let text = |v: &Option<String>| v.as_deref().map_or(Json::Null, Json::from);
        let value = |v: &Option<Value>| v.as_ref().map_or(Json::Null, Json::from);
        let document = Json::Obj(vec![
            ("protocol".into(), ANNOUNCEMENT.into()),
            ("phase".into(), announce.phase.as_str().into()),
            ("run_id".into(), text(&announce.run_id)),
            ("deployment_id".into(), text(&announce.deployment_id)),
            ("model".into(), value(&announce.model)),
            ("slice_mode".into(), announce.slice_mode.into()),
            ("manifest_digest".into(), text(&announce.manifest_digest)),
            ("work_limits".into(), value(&announce.work_limits)),
            ("issued_at".into(), Json::from(&Value::from((self.clock)()))),
            ("owner_incarnation".into(), self.incarnation.as_str().into()),
        ]);
        for extra in announce.per_node.values() {
            let extra = extra
                .as_object()
                .ok_or_else(|| value_error("demo_announcement_invalid"))?;
            check_node_extra(extra)?;
        }
        Ok(Prepared {
            document,
            nodes: announce.per_node,
        })
    }

    /// `commit_announcement`: the in-memory swap; an idle refresh is conditional at the swap itself.
    pub fn commit_announcement(&self, prepared: &Prepared, idle_refresh: bool) -> bool {
        let mut inner = lock(&self.inner);
        if idle_refresh {
            let current = inner.announcement.get("phase").cloned();
            let current_idle = matches!(&current, Some(Json::Str(p)) if p == "idle");
            let current_phase_ok =
                matches!(&current, Some(Json::Str(p)) if p == "idle" || p == "stopped");
            let same_model = current_idle
                && inner.announcement.get("model").map(Json::to_value)
                    == prepared.document.get("model").map(Json::to_value);
            let new_idle =
                matches!(prepared.document.get("phase"), Some(Json::Str(p)) if p == "idle");
            if !new_idle || !current_phase_ok || same_model {
                return false;
            }
        }
        inner.announcement = prepared.document.clone();
        inner.per_node = prepared.nodes.clone();
        true
    }

    /// `set_model_source_provider(provider)`; `None` clears it.
    pub fn set_model_source_provider(&self, provider: Option<HintProvider>) {
        *lock(&self.provider) = provider;
    }
    fn model_source_hint(&self, phase: &str) -> Json {
        if matches!(phase, "idle" | "stopped") {
            return Json::Null;
        }
        match lock(&self.provider).as_ref().map(|p| p()) {
            Some(Ok(Some(hint))) => Json::from(&hint),
            _ => Json::Null,
        }
    }

    /// `update_node(node_id, **extra)`.
    pub fn update_node(&self, node: &str, extra: Map<String, Value>) -> Result<(), ExchangeError> {
        check_node_extra(&extra)?;
        let mut inner = lock(&self.inner);
        let slot = inner
            .per_node
            .entry(node.to_owned())
            .or_insert_with(|| Value::Object(Map::new()));
        if let Value::Object(existing) = slot {
            existing.extend(extra);
        }
        Ok(())
    }

    /// `announcement(node_id)`: what a member reads, in the oracle's key order.
    pub fn announcement(&self, node: Option<&str>) -> Json {
        let (mut value, extra) = {
            let inner = lock(&self.inner);
            let extra = node
                .and_then(|n| inner.per_node.get(n))
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            (inner.announcement.clone(), extra)
        };
        let get = |key: &str| extra.get(key).map_or(Json::Null, Json::from);
        value.set("bootstrap_offer_id", get("bootstrap_offer_id"));
        value.set("node_slice_plan", get("node_slice_plan"));
        if extra
            .get("artifact_reuse_key")
            .is_some_and(|v| !v.is_null())
        {
            value.set("artifact_reuse_key", get("artifact_reuse_key"));
        }
        if extra.get("work_limits").is_some_and(|v| !v.is_null()) {
            value.set("work_limits", get("work_limits"));
        }
        value.set("member_resources", get("resources"));
        let phase = match value.get("phase") {
            Some(Json::Str(p)) => p.clone(),
            _ => String::new(),
        };
        value.set("model_source", self.model_source_hint(&phase));
        value
    }

    /// `reports()`: the latest report of each node, with the owner's `received_at`.
    pub fn reports(&self) -> Value {
        Value::Object(lock(&self.inner).reports.clone().into_iter().collect())
    }
    /// `submit_local`: an in-process report from the owner's own local contributor.
    pub fn submit_local(&self, node: &str, report: &Value) -> Result<(), ExchangeError> {
        let mut value = validate_report(report)?;
        if let Value::Object(map) = &mut value {
            map.insert("received_at".into(), Value::from((self.clock)()));
        }
        lock(&self.inner).reports.insert(node.to_owned(), value);
        Ok(())
    }
    /// The signed route alone may submit a remote report; equal timestamps win, older ones do not.
    pub(crate) fn submit_remote_verified_at(
        &self,
        node: &str,
        report: &Value,
        received_at: Option<f64>,
    ) -> Result<(), ExchangeError> {
        let mut report = validate_report(report)?;
        let issued = report["issued_at"]
            .as_f64()
            .ok_or_else(|| value_error("demo_report_invalid"))?;
        let mut inner = lock(&self.inner);
        if inner
            .issued
            .get(node)
            .is_none_or(|previous| issued >= *previous)
        {
            report["received_at"] = Value::from(received_at.unwrap_or_else(|| (self.clock)()));
            inner.issued.insert(node.to_owned(), issued);
            inner.reports.insert(node.to_owned(), report);
        }
        Ok(())
    }
    /// `forget_reports`: also clears the remote report watermark, but not clock observations.
    pub fn forget_reports(&self) {
        let mut inner = lock(&self.inner);
        inner.reports.clear();
        inner.issued.clear();
    }
}
