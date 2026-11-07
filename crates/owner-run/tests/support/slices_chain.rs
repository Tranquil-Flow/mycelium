// SPDX-License-Identifier: AGPL-3.0-only
//! Real Runner + OwnerChain through slices; all host IO is explicitly scripted.
//! Publish remains the real closed refusal. Rollover case injects the captured
//! prior context, not a fake successful serving run or automatic-rollover claim.
use super::*;
use owner_run::machine::{OwnerRunError, Reason};
use owner_run::runner::Chain;
use owner_run::steps::slices::{SlicesPort, SlicesPorts};
use std::sync::mpsc;

struct Source {
    cfg: Value,
}
impl SlicesPort for Source {
    fn node_slice_plans(&mut self) -> Result<Option<BTreeMap<String, Value>>, StepError> {
        Ok(self.cfg["plans"]
            .as_object()
            .map(|m| m.iter().map(|(k, v)| (k.clone(), v.clone())).collect()))
    }
    fn storage_policy(&mut self) -> Result<Option<Value>, StepError> {
        Ok(self.cfg["policy"].get("storage_bytes").cloned())
    }
}
struct Reports {
    base: Exchange,
    cfg: Value,
    calls: Arc<Mutex<Vec<String>>>,
    work: bool,
    entered: mpsc::Sender<()>,
    release: mpsc::Receiver<()>,
}
impl ReportsPort for Reports {
    fn forget_reports(&mut self) {
        self.base.forget_reports();
    }
    fn announce(&mut self, a: &Announcement) -> Result<(), StepError> {
        self.base.announce(a)
    }
    fn announce_work(&mut self, a: &Announcement, limits: &Value) -> Result<(), StepError> {
        self.base.announce(a)?;
        lock(&self.base.0.rec).announced.last_mut().unwrap()["work_limits"] = limits.clone();
        lock(&self.calls).push("announce:work".into());
        self.work = true;
        if self.cfg["announce_error"] == true {
            return Err(OwnerRunError::new("work_announce_failed").into());
        }
        if self.cfg["stop_work"] == true {
            self.entered.send(()).expect("test receives entry");
            // Dropping the test sender on unwind releases the driver before Runner joins.
            self.release.recv().map_err(|_| StepError::Stopped)?;
        }
        Ok(())
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        let mut rows = self.base.reports()?;
        if self.work {
            lock(&self.calls).push("reports".into());
            if let Some(error) = self.cfg["member_error"].as_str() {
                let (node, mut row) = rows.pop_last().unwrap();
                row["state"] = json!("error");
                row["detail"] = json!(error);
                return Ok(BTreeMap::from([(node, row)]));
            }
            for row in rows.values_mut() {
                row["state"] = json!("slice_ready");
                row["progress"] = json!({"done_bytes":125000000,"total_bytes":250000000});
            }
        }
        Ok(rows)
    }
    fn members(&mut self) -> Vec<MemberRow> {
        self.base.members()
    }
    fn own_node_id(&mut self) -> Option<String> {
        self.base.own_node_id()
    }
    fn member_identity(&mut self, n: &str) -> Value {
        self.base.member_identity(n)
    }
    fn clock_skew(&mut self, n: &[String]) -> Result<Option<ClockSkew>, StepError> {
        self.base.clock_skew(n)
    }
    fn tick_local(&mut self) {
        self.base.tick_local();
    }
}
struct Host {
    base: SplitHost,
    reports: Reports,
    source: Source,
}
impl ChainPorts for Host {
    fn ports(&mut self) -> Ports<'_> {
        let mut ports = self.base.ports();
        ports.reports = Some(&mut self.reports);
        ports
    }
    fn slices_ports(&mut self) -> Option<SlicesPorts<'_>> {
        Some(SlicesPorts {
            reports: &mut self.reports,
            slices: &mut self.source,
        })
    }
    fn teardown(&mut self, failed: bool, env: &mut dyn StepEnv) -> Result<(), String> {
        self.base.teardown(failed, env)
    }
}
struct PriorContext {
    inner: OwnerChain<Host>,
    binding: Option<Value>,
    calls: Arc<Mutex<Vec<String>>>,
}
impl Chain for PriorContext {
    fn new_run(&mut self, r: &str, d: &str, reason: Reason, c: Option<&Value>, b: Option<&Value>) {
        self.inner.new_run(
            r,
            d,
            if self.binding.is_some() {
                Reason::Rollover
            } else {
                reason
            },
            c,
            self.binding.as_ref().or(b),
        );
    }
    fn run_step(&mut self, s: &str, e: &mut dyn StepEnv) -> Result<(), StepError> {
        if s == "slices" {
            lock(&self.calls).push("slices".into());
        }
        self.inner.run_step(s, e)
    }
    fn rollover_binding(&self) -> Option<Value> {
        self.inner.rollover_binding()
    }
    fn teardown(&mut self, f: bool, e: &mut dyn StepEnv) -> Result<(), String> {
        self.inner.teardown(f, e)
    }
}
fn replay(name: &str, case: &Value) {
    let rec = Arc::new(Mutex::new(Record::default()));
    let calls = Arc::new(Mutex::new(vec![]));
    let (entered_tx, entered) = mpsc::channel();
    let (release, release_rx) = mpsc::channel();
    let host = Host {
        base: case_ports(&rec, case),
        reports: Reports {
            base: case_ports(&rec, case).exchange,
            cfg: case["cfg"].clone(),
            calls: calls.clone(),
            work: false,
            entered: entered_tx,
            release: release_rx,
        },
        source: Source {
            cfg: case["cfg"].clone(),
        },
    };
    let sink = Arc::new(Mutex::new(RunState::new("", "", Reason::Start)));
    let chain = PriorContext {
        inner: OwnerChain::new(host).with_state_sink(sink.clone()),
        binding: case["inputs"]
            .get("prior_rollover_binding")
            .filter(|v| !v.is_null())
            .cloned(),
        calls: calls.clone(),
    };
    let runner = Runner::spawn(
        Box::new(chain),
        Box::new(|| Identity {
            run_id: "run-1".into(),
            deployment_id: "dep-1".into(),
        }),
        Arc::new(SimClock(Mutex::new(0.0))),
        Timing {
            poll: support::tick(),
            rebuild_join: Timing::registered().rebuild_join,
        },
    )
    .unwrap();
    let release_after_runner = release;
    runner.start("start", None).unwrap();
    if case["cfg"]["stop_work"] == true {
        entered
            .recv_timeout(support::patience())
            .expect("work announcement reached");
        runner.stop();
        release_after_runner.send(()).expect("driver owns receiver");
    }
    eventually(name, || !runner.thread_alive() && runner.quiescent());
    let mut snapshot = runner.snapshot();
    runner.close();
    snapshot["started_at"] = json!(!snapshot["started_at"].is_null());
    assert_eq!(
        snapshot.as_object().unwrap().len(),
        case["snapshot"].as_object().unwrap().len(),
        "{name}: status keys"
    );
    for (key, want) in case["snapshot"].as_object().unwrap() {
        assert_eq!(&snapshot[key], want, "{name}: complete Runner status/{key}");
    }
    let state = lock(&sink);
    assert_eq!(
        json!({
            "participants":state.participants,"descriptors":state.descriptors,"rollover_binding":state.rollover_binding,
            "local_node":state.local_node,"slice_mode":state.model.as_ref().map(|m|m.slice_mode),
            "manifest_digest":state.model.as_ref().map(|m|m.manifest_digest.clone()),
            "proposal":state.proposal,"worker_wall_limits":state.worker_wall_limits,"wall_cap_fallback_nodes":state.wall_cap_fallback_nodes,
            "compile_state":state.compile_state,"layers":state.layers,"work_limits":state.work_limits,"work_nodes":state.work_nodes
        }),
        case["state"],
        "{name}: complete context through slices"
    );
    assert_eq!(
        json!(*lock(&calls)),
        case["slice_calls"],
        "{name}: slices call order"
    );
    assert_eq!(
        state.chain_compile_state.as_ref().unwrap(),
        &case["chain_compile_state"],
        "{name}: actual planner state"
    );
    assert_eq!(
        state.member_records.as_ref().unwrap(),
        &case["records"],
        "{name}: planner inputs"
    );
    let r = lock(&rec);
    assert_eq!(
        json!(r.announced),
        case["announces"],
        "{name}: exact work payload and announcement order"
    );
    assert_eq!(
        json!(r.teardowns),
        case["teardowns"],
        "{name}: scripted cleanup call (not proof)"
    );
    assert_eq!(
        json!(r.retired_observe + r.retired_compile),
        case["retire_calls"],
        "{name}: retire calls"
    );
    assert_eq!(
        json!(r.builds),
        case["build_calls"],
        "{name}: scripted builder starts"
    );
}
#[test]
fn default_work_announcement_refuses_without_losing_limits() {
    let f = fixture();
    let case = &f["cases"]["two-even-q3-06b"];
    let rec = Arc::new(Mutex::new(Record::default()));
    let mut host = case_ports(&rec, case);
    let result = host.exchange.announce_work(
        &Announcement {
            phase: "work".into(),
            run_id: "run-1".into(),
            deployment_id: "dep-1".into(),
            model_id: "model".into(),
            resolved_commit: "commit".into(),
            slice_mode: true,
            manifest_digest: "manifest".into(),
            per_node: None,
        },
        &json!({"max_wall_seconds":1}),
    );
    let Err(StepError::Refused(e)) = result else {
        panic!("missing host must refuse")
    };
    assert_eq!(e.code, "owner_run_step_not_ported");
    assert!(e.message.unwrap().contains("NP-SLICE1-slices"));
    assert!(
        lock(&rec).announced.is_empty(),
        "default must not forward a lossy announcement"
    );
}
#[test]
fn slices_through_owner_chain_and_runner_matches_oracle() {
    let cases: Value =
        serde_json::from_str(include_str!("../fixtures/oracle_chain_r14.json")).unwrap();
    for (name, case) in cases["cases"].as_object().unwrap() {
        replay(name, case);
    }
}
