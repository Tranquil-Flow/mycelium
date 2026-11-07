// SPDX-License-Identifier: AGPL-3.0-only
//! Whole oracle-recorded runs through the real Rust loop: FX-SLICE1-run-r12 (probe `r12-probe-run.py`,
//! derived from L7's `round11-capture-chain-compile.py` stub app) drives v0.1.4's REAL `OwnerRun` thread over
//! the real `AppDemoChain` steps model -> contributor -> descriptors -> observe -> compile for 2- and
//! 3-member fleets and stops at `slices` (the one stubbed step, ending with the port's own tagged
//! refusal). Here the same inputs feed [`Runner`] + [`OwnerChain`] over scripted ports, time is a simulated
//! clock that moves when a step waits, and compared exactly are: the final status document (steps, details,
//! participants with layers and resource rows, results), `ctx.state` (participants, descriptors, rollover
//! binding, proposal, worker wall limits, compile state, layers), the announcements, the teardown, the
//! number of retire / build calls and the failed step with its closed code.
//!
//! The observation context behind `ObservePort` is a test double (L6/L9 own the real one, seam
//! SE-SLICE1-OBSERVE): these runs prove the step order, waits and state hand-over, never the observation.
#[path = "support/slices_chain.rs"]
mod slices_chain;
mod support;
#[path = "support/threaded_compile.rs"]
mod threaded_compile;
#[path = "support/work_chain.rs"]
mod work_chain;
use owner_run::chain::{PortError, StepEnv, StepError};
use owner_run::machine::{Identity, MonitorProbe};
use owner_run::owner_chain::{ChainPorts, OwnerChain, Ports};
use owner_run::runner::{Clocks, Runner, Timing};
use owner_run::state::RunState;
use owner_run::steps::contributor::ContributorPort;
use owner_run::steps::exchange::{Announcement, ClockSkew, MemberRow, ReportsPort};
use owner_run::steps::model::{ModelPort, PreparedModel, PreparedSource, Reverify};
use owner_run::steps::observe::ObservePort;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Instant;
use support::compile_double::{Port as CompileDouble, inputs_of};

const NOW: f64 = 1_800_000_000.0;

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
}

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_run_r12.json")).expect("fixture parses")
}

/// A clock that moves only when a step waits (`pause`).
struct SimClock(Mutex<f64>);
impl Clocks for SimClock {
    fn mono(&self) -> f64 {
        1000.0 + *lock(&self.0)
    }
    fn wall(&self) -> f64 {
        NOW + *lock(&self.0)
    }
    fn simulated(&self) -> bool {
        true
    }
    fn advance(&self, seconds: f64) {
        *lock(&self.0) += seconds;
    }
}

#[derive(Default)]
struct Record {
    announced: Vec<Value>,
    teardowns: Vec<bool>,
    retired_observe: usize,
    retired_compile: usize,
    builds: usize,
    forgot: usize,
}

struct CasePorts {
    rec: Arc<Mutex<Record>>,
    inputs: Value,
    model: PreparedModel,
    compile: CompileDouble,
    cfg_policy: Option<Value>,
    prepared_digest: String,
    source_bytes: Value,
    collected: bool,
}

impl ModelPort for CasePorts {
    fn owner_and_running(&self) -> bool {
        true
    }
    fn swarm_created(&self) -> bool {
        true
    }
    fn has_closing_thread(&self) -> bool {
        false
    }
    fn closing_thread_alive(&mut self) -> bool {
        false
    }
    fn acquisition_state(&mut self) -> String {
        "prepared".into()
    }
    fn reverify_available(&self) -> bool {
        false
    }
    fn reverify(&mut self) -> Reverify {
        Reverify::Done
    }
    fn prepared_source(&mut self) -> Result<PreparedSource, StepError> {
        Ok(PreparedSource {
            model: self.model.clone(),
            manifest_digest: self.prepared_digest.clone(),
            source_bytes: self.source_bytes.clone(),
        })
    }
    fn storage_consent_cap(&mut self) -> Result<Option<Value>, StepError> {
        Ok(self
            .cfg_policy
            .as_ref()
            .and_then(|p| p.get("storage_bytes").cloned()))
    }
    fn check_rollover_members(&mut self, _: &Value) -> Result<(), StepError> {
        Ok(())
    }
}

impl ContributorPort for CasePorts {
    fn ensure_local_contributor(&mut self) -> Result<String, StepError> {
        Ok(self.inputs["local_node"]
            .as_str()
            .expect("local")
            .to_owned())
    }
    fn exchange_available(&self) -> bool {
        true
    }
    fn attach_agent(&mut self, _: &str) -> Result<(), StepError> {
        Ok(())
    }
}

impl ReportsPort for CasePorts {
    fn forget_reports(&mut self) {
        lock(&self.rec).forgot += 1;
    }
    fn announce(&mut self, a: &Announcement) -> Result<(), StepError> {
        let mut row = json!({
            "deployment_id": a.deployment_id, "manifest_digest": a.manifest_digest, "phase": a.phase,
            "run_id": a.run_id, "slice_mode": a.slice_mode,
        });
        if let Some(per) = &a.per_node {
            row["per_node"] = Value::Object(per.iter().cloned().collect());
        }
        lock(&self.rec).announced.push(row);
        Ok(())
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        Ok(self.inputs["nodes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|n| {
                let n = n.as_str().unwrap();
                (
                    n.to_owned(),
                    json!({
                        "run_id": "run-1", "deployment_id": "dep-1", "state": "bootstrap_running",
                        "received_at": NOW,
                        "descriptor": {"statement": {"target_node_id": n, "expires_at": NOW + 1_000_000.0}},
                    }),
                )
            })
            .collect())
    }
    fn members(&mut self) -> Vec<MemberRow> {
        self.inputs["members_full"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| MemberRow {
                node_id: m["node_id"].as_str().unwrap().to_owned(),
                lifecycle_state: m["lifecycle_state"].as_str().map(str::to_owned),
                lease_expires_at: m["lease_expires_at"].as_f64(),
            })
            .collect()
    }
    fn own_node_id(&mut self) -> Option<String> {
        self.inputs["owner_authority"].as_str().map(str::to_owned)
    }
    fn member_identity(&mut self, node: &str) -> Value {
        let m = &self.inputs["current_members"][node];
        json!([
            m["generation"],
            m["incarnation"],
            m["verification_key_digest"],
            m["endpoint_id"]
        ])
    }
    fn clock_skew(&mut self, _: &[String]) -> Result<Option<ClockSkew>, StepError> {
        Ok(Some(ClockSkew {
            tolerance: 30.0,
            exceeded: Vec::new(),
        }))
    }
    fn tick_local(&mut self) {}
}

impl ObservePort for CasePorts {
    fn prepare(&mut self, _: &str, _: &str, _: &[Value]) -> Result<(), PortError> {
        Ok(())
    }
    fn rebuild(&mut self, _: &str, _: &str, _: &[Value]) -> Result<String, PortError> {
        Ok("awaiting_bootstrap_workers".into())
    }
    fn facts_nodes(&mut self) -> Vec<String> {
        self.inputs["facts"]
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect()
    }
    fn offers(&mut self) -> Vec<(String, String)> {
        self.facts_nodes()
            .into_iter()
            .map(|n| {
                let offer = format!("offer-{n}");
                (n, offer)
            })
            .collect()
    }
    fn collect(&mut self) -> Result<(), PortError> {
        self.collected = true;
        Ok(())
    }
    fn observed_hosts(&mut self) -> Vec<Option<String>> {
        if !self.collected {
            return Vec::new();
        }
        self.inputs["hosts"]
            .as_array()
            .unwrap()
            .iter()
            .map(|h| h.as_str().map(str::to_owned))
            .collect()
    }
    fn retire_bootstrap(&mut self) -> Result<(), PortError> {
        lock(&self.rec).retired_observe += 1;
        Ok(())
    }
}

/// The doubles in separate fields so the chain can borrow several at once.
struct SplitHost {
    model: Model,
    contributor: Contributor,
    exchange: Exchange,
    observe: Observe,
    compile: CompileDouble,
    rec: Arc<Mutex<Record>>,
}
struct Model(CasePorts);
struct Contributor(CasePorts);
struct Exchange(CasePorts);
struct Observe(CasePorts);
impl ModelPort for Model {
    fn owner_and_running(&self) -> bool {
        self.0.owner_and_running()
    }
    fn swarm_created(&self) -> bool {
        self.0.swarm_created()
    }
    fn has_closing_thread(&self) -> bool {
        false
    }
    fn closing_thread_alive(&mut self) -> bool {
        false
    }
    fn acquisition_state(&mut self) -> String {
        self.0.acquisition_state()
    }
    fn reverify_available(&self) -> bool {
        false
    }
    fn reverify(&mut self) -> Reverify {
        Reverify::Done
    }
    fn prepared_source(&mut self) -> Result<PreparedSource, StepError> {
        self.0.prepared_source()
    }
    fn storage_consent_cap(&mut self) -> Result<Option<Value>, StepError> {
        self.0.storage_consent_cap()
    }
    fn check_rollover_members(&mut self, b: &Value) -> Result<(), StepError> {
        self.0.check_rollover_members(b)
    }
}
impl ContributorPort for Contributor {
    fn ensure_local_contributor(&mut self) -> Result<String, StepError> {
        self.0.ensure_local_contributor()
    }
    fn exchange_available(&self) -> bool {
        true
    }
    fn attach_agent(&mut self, n: &str) -> Result<(), StepError> {
        self.0.attach_agent(n)
    }
}
impl ReportsPort for Exchange {
    fn forget_reports(&mut self) {
        self.0.forget_reports()
    }
    fn announce(&mut self, a: &Announcement) -> Result<(), StepError> {
        self.0.announce(a)
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        self.0.reports()
    }
    fn members(&mut self) -> Vec<MemberRow> {
        self.0.members()
    }
    fn own_node_id(&mut self) -> Option<String> {
        self.0.own_node_id()
    }
    fn member_identity(&mut self, n: &str) -> Value {
        self.0.member_identity(n)
    }
    fn clock_skew(&mut self, n: &[String]) -> Result<Option<ClockSkew>, StepError> {
        self.0.clock_skew(n)
    }
    fn tick_local(&mut self) {}
}
impl ObservePort for Observe {
    fn prepare(&mut self, a: &str, b: &str, c: &[Value]) -> Result<(), PortError> {
        self.0.prepare(a, b, c)
    }
    fn rebuild(&mut self, a: &str, b: &str, c: &[Value]) -> Result<String, PortError> {
        self.0.rebuild(a, b, c)
    }
    fn facts_nodes(&mut self) -> Vec<String> {
        self.0.facts_nodes()
    }
    fn offers(&mut self) -> Vec<(String, String)> {
        self.0.offers()
    }
    fn collect(&mut self) -> Result<(), PortError> {
        self.0.collect()
    }
    fn observed_hosts(&mut self) -> Vec<Option<String>> {
        self.0.observed_hosts()
    }
    fn retire_bootstrap(&mut self) -> Result<(), PortError> {
        self.0.retire_bootstrap()
    }
}

impl ChainPorts for SplitHost {
    fn ports(&mut self) -> Ports<'_> {
        Ports {
            model: &mut self.model,
            contributor: Some(&mut self.contributor),
            reports: Some(&mut self.exchange),
            observe: Some(&mut self.observe),
            compile: Some(&mut self.compile),
        }
    }
    fn teardown(&mut self, failed: bool, _: &mut dyn StepEnv) -> Result<(), String> {
        let mut rec = lock(&self.rec);
        rec.teardowns.push(failed);
        rec.retired_compile = self.compile.retired;
        rec.builds = self.compile.requests.len();
        Ok(())
    }
    fn monitor(&mut self, _: &RunState) -> MonitorProbe {
        MonitorProbe::Code(None)
    }
}

fn case_ports(rec: &Arc<Mutex<Record>>, case: &Value) -> SplitHost {
    let i = &case["inputs"];
    let row = &case["model_row"];
    let model = PreparedModel {
        model_id: row["model_id"].as_str().unwrap().into(),
        resolved_commit: row["resolved_commit"].as_str().unwrap().into(),
        architecture: row["architecture"].as_str().unwrap().into(),
        label: row["label"].as_str().unwrap().into(),
        choice: row["choice"].as_str().map(str::to_owned),
        shape_registered: true,
        owner_storage_need: row["owner_storage_need"].as_u64().unwrap(),
    };
    let make = || CasePorts {
        rec: rec.clone(),
        inputs: i.clone(),
        model: model.clone(),
        compile: CompileDouble {
            inputs: inputs_of(i),
            adapter: i["adapter"].clone(),
            slice_mode: i["slice_mode"].as_bool().unwrap(),
            delay: 1,
            polls: 0,
            retired: 0,
            requests: vec![],
        },
        cfg_policy: (!i["policy"].is_null()).then(|| i["policy"].clone()),
        prepared_digest: i["prepared"]["manifest_digest"].as_str().unwrap().into(),
        source_bytes: i["prepared"]["source_bytes"].clone(),
        collected: false,
    };
    let compile = make().compile;
    // observe's `collect` flag is shared by the one observation context: keep it in the Observe wrapper only
    SplitHost {
        model: Model(make()),
        contributor: Contributor(make()),
        exchange: Exchange(make()),
        observe: Observe(make()),
        compile,
        rec: rec.clone(),
    }
}

fn eventually(what: &str, mut condition: impl FnMut() -> bool) {
    let started = Instant::now();
    while !condition() {
        assert!(
            started.elapsed() < support::patience(),
            "timed out waiting for {what}"
        );
        std::thread::sleep(support::tick() / 3);
    }
}

fn run_case(name: &str, case: &Value) {
    let rec = Arc::new(Mutex::new(Record::default()));
    let host = case_ports(&rec, case);
    let sink = Arc::new(Mutex::new(RunState::new(
        "",
        "",
        owner_run::machine::Reason::Start,
    )));
    let chain = OwnerChain::new(host).with_state_sink(sink.clone());
    let clock = Arc::new(SimClock(Mutex::new(0.0)));
    let runner = Runner::spawn(
        Box::new(chain),
        Box::new(|| Identity {
            run_id: "run-1".into(),
            deployment_id: "dep-1".into(),
        }),
        clock,
        Timing {
            poll: support::tick(),
            rebuild_join: 180.0,
        },
    )
    .expect("the loop thread starts");
    let ceiling = case["cfg"].get("ceiling").filter(|c| !c.is_null());
    runner.start("start", ceiling).expect("start");
    eventually(&format!("{name}: the run to end"), || {
        !runner.thread_alive() && runner.quiescent()
    });
    let mut snapshot = runner.snapshot();
    runner.close();
    let started = !snapshot["started_at"].is_null();
    snapshot["started_at"] = started.into();
    let want = &case["snapshot"];
    assert_eq!(&snapshot, want, "{name}: final status document");

    let state = lock(&sink).clone();
    let compiled = state.compile_state.is_some();
    let layers: serde_json::Map<String, Value> = state
        .layers
        .iter()
        .map(|(k, v)| (k.clone(), json!([v[0], v[1]])))
        .collect();
    let got = json!({
        "participants": state.participants,
        "descriptors": state.descriptors,
        "rollover_binding": state.rollover_binding,
        "local_node": state.local_node,
        "slice_mode": state.model.as_ref().map(|m| m.slice_mode),
        "manifest_digest": state.model.as_ref().map(|m| m.manifest_digest.clone()),
        "proposal": state.proposal,
        "worker_wall_limits": if compiled { json!(state.worker_wall_limits) } else { Value::Null },
        "wall_cap_fallback_nodes": if compiled { json!(state.wall_cap_fallback_nodes) } else { Value::Null },
        "compile_state": state.compile_state,
        "layers": if compiled { Value::Object(layers) } else { Value::Null },
    });
    assert_eq!(got, case["state"], "{name}: ctx.state");
    assert_eq!(
        state.chain_compile_state.clone().unwrap_or(Value::Null),
        case["chain_compile_state"],
        "{name}: _chain_compile_state"
    );
    assert_eq!(
        state.member_records.clone().unwrap_or(Value::Null),
        case["records"],
        "{name}: _member_capability_reports"
    );
    let record = lock(&rec);
    assert_eq!(
        Value::Array(record.announced.clone()),
        case["announces"],
        "{name}: announcements"
    );
    assert_eq!(
        json!(record.teardowns),
        case["teardowns"],
        "{name}: teardown(failed)"
    );
    assert_eq!(
        record.retired_observe + record.retired_compile,
        case["retire_calls"].as_u64().unwrap() as usize,
        "{name}: retire_bootstrap calls (observe + compile)"
    );
    assert_eq!(
        record.builds as u64, case["build_calls"],
        "{name}: builder starts"
    );
    assert_eq!(record.forgot, 1, "{name}: forget_reports once");
}

/// Proves: B-CONSENT-15 (the real descriptors binding is exported unchanged for Runner to retain).
#[test]
fn owner_chain_exports_the_recorded_descriptor_binding() {
    use owner_run::runner::Chain;
    let doc = fixture();
    for name in ["two-even-q3-06b", "three-q3-8b"] {
        let case = &doc["cases"][name];
        let rec = Arc::new(Mutex::new(Record::default()));
        let mut chain = OwnerChain::new(case_ports(&rec, case));
        chain.new_run(
            "run-1",
            "dep-1",
            owner_run::machine::Reason::Start,
            None,
            None,
        );
        assert!(chain.rollover_binding().is_none());
        let world = support::vworld::World::new(&json!({}));
        let mut env = support::vworld::VEnv::new(&world);
        for step in ["model", "contributor", "descriptors"] {
            chain.run_step(step, &mut env).unwrap();
        }
        let binding = chain
            .rollover_binding()
            .expect("descriptors froze membership");
        assert_eq!(binding, case["state"]["rollover_binding"], "{name}");
        chain.new_run(
            "run-2",
            "dep-2",
            owner_run::machine::Reason::Rollover,
            None,
            Some(&binding),
        );
        assert_eq!(chain.state().rollover_binding.as_ref(), Some(&binding));
    }
}

fn group(pick: impl Fn(&str) -> bool, at_least: usize) {
    let doc = fixture();
    let mut n = 0;
    for (name, case) in doc["cases"].as_object().unwrap() {
        if pick(name) {
            n += 1;
            run_case(name, case);
        }
    }
    assert!(n >= at_least, "only {n} runs (need {at_least})");
}

/// Proves: B-RUN-01 (the steps run in order and every finished step is `done`), B-RUN-08 (participants are
/// listed with labels, layers and resource rows), B-RUN-24 (the route is compiled from exactly the Macs that
/// reported in), B-CONSENT-07, B-CONSENT-08, B-CONSENT-09 (per-worker wall limits), B-RUN-03 (the run stops at
/// the first step that is not ported with its tagged refusal and tears down with failed=true).
#[test]
fn oracle_recorded_runs_of_two_and_three_members_reach_slices() {
    group(
        |n| {
            matches!(
                n,
                "two-even-q3-06b"
                    | "two-skew-q3-06b"
                    | "three-q3-8b"
                    | "three-q3-14b"
                    | "two-q25-05b"
                    | "three-q3-8b-with-grants"
                    | "three-q3-8b-member-caps"
                    | "two-even-cache-hit"
                    | "two-even-within-ceiling"
            )
        },
        9,
    );
}

/// Proves: B-CONSENT-16 (a compiled proposal outside the owner's execution ceiling ends the run at compile
/// with `execution_proposal_outside_ceiling`, before anything is published).
#[test]
fn a_run_with_a_ceiling_the_proposal_exceeds_fails_at_compile() {
    group(|n| n.contains("outside-ceiling"), 2);
}

/// Proves: B-RUN-07 (`chain_compile_timeout` when the builder never finishes within the Compile budget),
/// B-RUN-24 (a failing builder ends the run with `chain_compile_failed`), B-CONSENT-07
/// (`member_wall_limit_invalid` for a joined Mac's time limit outside 60..3,600 s).
#[test]
fn compile_timeouts_and_failures_end_the_run_at_compile() {
    group(
        |n| {
            matches!(
                n,
                "three-build-never-finishes" | "two-build-raises" | "two-member-wall-invalid"
            )
        },
        3,
    );
}
