// SPDX-License-Identifier: AGPL-3.0-only
//! Real Runner + OwnerChain through publish/targets; all host IO explicitly scripted.
//! Activation remains the real closed refusal. Rollover case injects the captured
//! prior context, not a fake successful serving run or automatic-rollover claim.
use super::*;
use owner_run::machine::MonitorProbe;
use owner_run::machine::{OwnerRunError, Reason};
use owner_run::runner::Chain;
use owner_run::steps::activate::ActivationPort;
use owner_run::steps::publish::{Fence, ServingIdentity, WorkPort, WorkPorts};
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
    stage: Arc<Mutex<String>>,
    work_calls: Arc<Mutex<Vec<Value>>>,
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
        Ok(())
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        let mut rows = self.base.reports()?;
        if self.work {
            lock(&self.calls).push("reports".into());
            if matches!(lock(&self.stage).as_str(), "publish" | "targets") {
                lock(&self.work_calls).push(json!("reports"));
            }
            if let Some(error) = self.cfg["member_error"]
                .as_str()
                .filter(|_| *lock(&self.stage) == "targets")
            {
                let (node, mut row) = rows.pop_last().unwrap();
                row["state"] = json!("error");
                row["detail"] = json!(error);
                return Ok(BTreeMap::from([(node, row)]));
            }
            for row in rows.values_mut() {
                row["state"] = json!(if *lock(&self.stage) == "slices"
                    || self.cfg["stop_targets"] == true
                {
                    "slice_ready"
                } else {
                    "work_running"
                });
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
struct Workflow {
    cfg: Value,
    stage: Arc<Mutex<String>>,
    calls: Arc<Mutex<Vec<Value>>>,
    entered: mpsc::Sender<()>,
    release: mpsc::Receiver<()>,
    released: bool,
}
impl Workflow {
    fn hold(&mut self) -> Result<(), StepError> {
        // Python threading.Event stays set: later workflow status reads must
        // not consume another release token and park the stopped driver again.
        if self.released {
            return Ok(());
        }
        self.entered.send(()).expect("test receives held entry");
        self.release.recv().map_err(|_| StepError::Stopped)?;
        self.released = true;
        Ok(())
    }
}
impl WorkPort for Workflow {
    fn install_cleanup_progress(&mut self) -> Result<(), StepError> {
        lock(&self.calls).push(json!("install_cleanup_progress"));
        Ok(())
    }
    fn start_serving(&mut self, proposal: &Value) -> Result<Value, PortError> {
        lock(&self.calls).push(json!(["start_serving", proposal]));
        if self.cfg["stop_publish"] == true && self.hold().is_err() {
            return Err(PortError::Failed {
                code: None,
                text: "test_release_disconnected".into(),
            });
        }
        if let Some(code) = self.cfg["start_error"].as_str() {
            return Err(PortError::Failed {
                code: None,
                text: code.into(),
            });
        }
        Ok(json!({"registry_origin":"http://scripted.invalid"}))
    }
    fn workflow_state(&mut self) -> Result<Value, StepError> {
        lock(&self.calls).push(json!("workflow_state"));
        if self.cfg["stop_targets"] == true && *lock(&self.stage) == "targets" {
            self.hold()?;
        }
        if self.serving_host() && *lock(&self.stage) == "serving" {
            // Scripted, labelled: a workflow whose startup challenge qualified the route.
            return Ok(json!({"state":"api_running","error_code":null,
                "startup":{"startup_qualification_complete":true,
                "unavailable_deployment_count":0,"url":"http://127.0.0.1:4385/"}}));
        }
        Ok(if self.cfg["workflow_failed"] == true {
            json!({"state":"failed","error_code":"workflow_died"})
        } else {
            json!({"state":"awaiting_activation"})
        })
    }
    fn serving_identity(&mut self, _: &RunState) -> Result<ServingIdentity, StepError> {
        if !self.serving_host() {
            return Err(owner_run::steps::not_ported("serving"));
        }
        Ok(ServingIdentity::Current)
    }
    fn commit_serving(
        &mut self,
        _: &RunState,
        a: &Announcement,
        _: Option<&Value>,
        fence: Fence<'_>,
    ) -> Result<(), StepError> {
        if !self.serving_host() {
            return Err(owner_run::steps::not_ported("serving"));
        }
        let calls = self.calls.clone();
        fence(&mut || {
            lock(&calls).push(json!(["commit_serving", a.phase]));
            Ok(())
        })
    }
}
impl Workflow {
    /// A test-only host that lends the serving collaborators (never set by a captured case).
    fn serving_host(&self) -> bool {
        self.cfg["serving_host"] == true
    }
}
impl ActivationPort for Workflow {
    fn activate(&mut self, proposal: &Value) -> Result<(), PortError> {
        lock(&self.calls).push(json!(["activate", proposal]));
        if self.cfg["stop_activate"] == true && self.hold().is_err() {
            return Err(PortError::Failed {
                code: None,
                text: "test_release_disconnected".into(),
            });
        }
        if let Some(error) = self.cfg["activate_error"].as_str() {
            return Err(PortError::Failed {
                code: None,
                text: error.into(),
            });
        }
        Ok(())
    }
}
struct Host {
    base: SplitHost,
    reports: Reports,
    source: Source,
    workflow: Workflow,
    /// `chain.monitor(ctx)` calls (only a serving run is monitored).
    monitored: Arc<Mutex<u32>>,
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
    fn work_ports(&mut self) -> Option<WorkPorts<'_>> {
        Some(WorkPorts {
            reports: &mut self.reports,
            workflow: &mut self.workflow,
        })
    }
    fn activation_port(&mut self) -> Option<&mut dyn ActivationPort> {
        if self.workflow.cfg["activation_host"] == true {
            Some(&mut self.workflow)
        } else {
            None
        }
    }
    fn teardown(&mut self, failed: bool, env: &mut dyn StepEnv) -> Result<(), String> {
        self.base.teardown(failed, env)
    }
    fn monitor(&mut self, _: &RunState) -> MonitorProbe {
        *lock(&self.monitored) += 1;
        MonitorProbe::Code(None)
    }
}
struct PriorContext {
    inner: OwnerChain<Host>,
    binding: Option<Value>,
    calls: Arc<Mutex<Vec<String>>>,
    work_calls: Arc<Mutex<Vec<Value>>>,
    stage: Arc<Mutex<String>>,
    activation: bool,
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
        *lock(&self.stage) = s.to_owned();
        if matches!(s, "publish" | "targets") || (s == "activate" && self.activation) {
            lock(&self.work_calls).push(json!(s));
        }
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
    fn rollover_due(&mut self) -> Option<bool> {
        self.inner.rollover_due()
    }
    fn ceiling_reached(&mut self) -> owner_run::machine::CeilingProbe {
        self.inner.ceiling_reached()
    }
    fn monitor(&mut self) -> MonitorProbe {
        self.inner.monitor()
    }
}
fn replay(name: &str, case: &Value) {
    let rec = Arc::new(Mutex::new(Record::default()));
    let calls = Arc::new(Mutex::new(vec![]));
    let work_calls = Arc::new(Mutex::new(vec![]));
    let stage = Arc::new(Mutex::new(String::new()));
    let (entered_tx, entered) = mpsc::channel();
    let (release, release_rx) = mpsc::channel();
    let mut base = case_ports(&rec, case);
    // This probe pins chain._monotonic to 1000 independently of the builder
    // clock (probe-chain-work.py:256). Its completed builder needs no simulated
    // countdown; the r12 helper's one artificial pending poll would add 0.5 s.
    base.compile.delay = 0;
    let host = Host {
        workflow: Workflow {
            cfg: case["cfg"].clone(),
            stage: stage.clone(),
            calls: work_calls.clone(),
            entered: entered_tx,
            release: release_rx,
            released: false,
        },
        base,
        reports: Reports {
            base: case_ports(&rec, case).exchange,
            cfg: case["cfg"].clone(),
            calls: calls.clone(),
            work: false,
            stage: stage.clone(),
            work_calls: work_calls.clone(),
        },
        source: Source {
            cfg: case["cfg"].clone(),
        },
        monitored: Arc::default(),
    };
    let sink = Arc::new(Mutex::new(RunState::new("", "", Reason::Start)));
    let chain = PriorContext {
        inner: OwnerChain::new(host).with_state_sink(sink.clone()),
        binding: case["inputs"]
            .get("prior_rollover_binding")
            .filter(|v| !v.is_null())
            .cloned(),
        calls: calls.clone(),
        work_calls: work_calls.clone(),
        stage,
        activation: case["cfg"]["activation_host"] == true,
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
    let mut held = None;
    if case["cfg"]["stop_publish"] == true
        || case["cfg"]["stop_targets"] == true
        || case["cfg"]["stop_activate"] == true
    {
        entered
            .recv_timeout(support::patience())
            .expect("work step held");
        // Read status and Stop on another thread while the actual chain collaborator is held.
        // Always release the collaborator before asserting or joining that observer.
        held = Some(std::thread::scope(|scope| {
            let (tx, rx) = mpsc::channel();
            let rr = &runner;
            let observer = scope.spawn(move || {
                let before = rr.view().snapshot();
                let active = rr.view().active();
                rr.stop();
                tx.send((before, rr.view().snapshot(), active))
                    .expect("observer result receiver");
            });
            let received = rx.recv_timeout(support::patience());
            release_after_runner.send(()).expect("driver owns receiver");
            observer.join().expect("observer joined");
            received.expect("status and Stop responsive while work held")
        }));
    }
    eventually(name, || !runner.thread_alive() && runner.quiescent());
    let mut snapshot = runner.snapshot();
    runner.close();
    if let Some((mut before, mut after, active)) = held {
        before["started_at"] = json!(!before["started_at"].is_null());
        after["started_at"] = json!(!after["started_at"].is_null());
        assert!(active, "held run is active");
        assert_eq!(
            before, case["held_snapshot"],
            "{name}: held complete status"
        );
        assert_eq!(
            after, case["stop_snapshot"],
            "{name}: stop complete status before release"
        );
    }
    snapshot["started_at"] = json!(!snapshot["started_at"].is_null());
    assert_eq!(
        snapshot.as_object().unwrap().len(),
        case["snapshot"].as_object().unwrap().len(),
        "{name}: status keys"
    );
    for (key, want) in case["snapshot"].as_object().unwrap() {
        assert_eq!(&snapshot[key], want, "{name}: complete Runner status/{key}");
    }
    assert_eq!(
        json!(*lock(&work_calls)),
        case["work_calls"],
        "{name}: ordered workflow calls and proposal"
    );
    let state = lock(&sink);
    assert_eq!(
        json!({
            "participants":state.participants,"descriptors":state.descriptors,"rollover_binding":state.rollover_binding,
            "local_node":state.local_node,"slice_mode":state.model.as_ref().map(|m|m.slice_mode),
            "manifest_digest":state.model.as_ref().map(|m|m.manifest_digest.clone()),
            "proposal":state.proposal,"worker_wall_limits":state.worker_wall_limits,"wall_cap_fallback_nodes":state.wall_cap_fallback_nodes,
            "execution_clock_started_at":state.execution_clock_started_at,"registry_origin":state.registry_origin,
            "compile_state":state.compile_state,"layers":state.layers,"work_limits":state.work_limits,"work_nodes":state.work_nodes
        }),
        case["state"],
        "{name}: complete context through targets"
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
fn missing_work_ports_preserve_exact_legacy_refusals() {
    let f = fixture();
    let rec = Arc::new(Mutex::new(Record::default()));
    let mut host = case_ports(&rec, &f["cases"]["two-even-q3-06b"]);
    assert!(host.work_ports().is_none());
    assert!(host.activation_port().is_none());
    let mut chain = OwnerChain::new(host);
    let world = support::vworld::World::new(&json!({}));
    let mut env = support::vworld::VEnv::new(&world);
    for (step, tag, seam) in [
        ("publish", "NP-SLICE1-publish", "SE-L10-EXCHANGE"),
        ("targets", "NP-SLICE1-targets", "SE-L9-RUN-SESSION"),
        ("activate", "NP-SLICE1-activate", "SE-L9-RUN-SESSION"),
        ("unknown", "NP-SLICE1-unknown", "SE-SLICE1-UNKNOWN"),
    ] {
        let Err(StepError::Refused(e)) = chain.run_step(step, &mut env) else {
            panic!("missing port must refuse");
        };
        assert_eq!(e.code, "owner_run_step_not_ported");
        assert_eq!(
            e.message,
            Some(format!(
                "The {step} step is not ported to the Rust run chain yet ({tag}, waiting for seam {seam}); the previous release runs it."
            ))
        );
        assert_eq!(e.fix, None);
    }
    assert!(world.calls().as_array().unwrap().is_empty());
    assert!(env.results.is_empty());
    assert!(lock(&rec).announced.is_empty());
}
#[test]
fn publish_targets_through_owner_chain_and_runner_matches_oracle() {
    let f: Value =
        serde_json::from_str(include_str!("../fixtures/oracle_chain_work_r15.json")).unwrap();
    for (name, case) in f["cases"].as_object().unwrap() {
        replay(name, case);
        println!("WORK-CHAIN-CASE-PASS {name}");
    }
}

#[test]
fn activation_through_owner_chain_and_runner_matches_oracle() {
    let f: Value =
        serde_json::from_str(include_str!("../fixtures/oracle_chain_activate_r17.json")).unwrap();
    let cases = f["cases"].as_object().unwrap();
    assert!(!cases.is_empty());
    for (name, case) in cases {
        replay(name, case);
        println!("ORACLE-ACTIVATE-CHAIN-R17 {name}");
    }
}

/// Proves: B-RUN-10 (a host that lends the serving collaborators takes the run through `serving` on
/// the real Runner and OwnerChain: the route origin is published, the run stays `serving` while the
/// owner's live status keeps monitoring it, and a Stop then tears it down with failed=false).
/// Scripted, labelled host IO: no native worker, route or chat is involved.
#[test]
fn a_serving_run_stays_serving_and_monitored_until_stop_tears_it_down() {
    let f: Value =
        serde_json::from_str(include_str!("../fixtures/oracle_chain_activate_r17.json")).unwrap();
    let mut case = f["cases"]["whole"].clone();
    case["cfg"]["serving_host"] = json!(true);
    let rec = Arc::new(Mutex::new(Record::default()));
    let calls = Arc::new(Mutex::new(vec![]));
    let work_calls = Arc::new(Mutex::new(vec![]));
    let stage = Arc::new(Mutex::new(String::new()));
    let monitored = Arc::new(Mutex::new(0));
    let (entered, _entered) = mpsc::channel();
    let (_release, release) = mpsc::channel();
    let mut base = case_ports(&rec, &case);
    base.compile.delay = 0;
    let host = Host {
        workflow: Workflow {
            cfg: case["cfg"].clone(),
            stage: stage.clone(),
            calls: work_calls.clone(),
            entered,
            release,
            released: false,
        },
        base,
        reports: Reports {
            base: case_ports(&rec, &case).exchange,
            cfg: case["cfg"].clone(),
            calls: calls.clone(),
            work: false,
            stage: stage.clone(),
            work_calls: work_calls.clone(),
        },
        source: Source {
            cfg: case["cfg"].clone(),
        },
        monitored: monitored.clone(),
    };
    let chain = PriorContext {
        inner: OwnerChain::new(host),
        binding: None,
        calls,
        work_calls: work_calls.clone(),
        stage,
        activation: true,
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
    runner.start("start", None).unwrap();
    eventually("the run leaves its steps", || {
        matches!(
            runner.snapshot()["state"].as_str(),
            Some("serving" | "failed" | "stopped")
        )
    });
    let serving = runner.snapshot();
    assert_eq!(serving["state"], "serving", "{serving}");
    assert_eq!(serving["route_ready"], true, "{serving}");
    assert_eq!(
        serving["registry_origin"], "http://127.0.0.1:4385",
        "{serving}"
    );
    assert_eq!(serving["steps"][9]["detail"], "route ready: open the chat");
    assert!(
        lock(&work_calls).contains(&json!(["commit_serving", "serving"])),
        "the serving announcement went through the fenced commit: {:?}",
        lock(&work_calls)
    );
    let before = *lock(&monitored);
    for _ in 0..2 {
        assert_eq!(runner.status()["state"], "serving");
    }
    assert!(
        *lock(&monitored) >= before + 2,
        "every live status monitors the serving run"
    );
    assert!(
        lock(&rec).teardowns.is_empty(),
        "nothing torn down while serving"
    );
    runner.stop();
    eventually("the stopped run settles", || {
        !runner.thread_alive() && runner.quiescent()
    });
    let end = runner.snapshot();
    runner.close();
    assert_eq!(end["state"], "stopped", "{end}");
    assert_eq!(
        json!(lock(&rec).teardowns),
        json!([false]),
        "one Stop teardown"
    );
}
