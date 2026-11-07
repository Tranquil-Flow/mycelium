// SPDX-License-Identifier: AGPL-3.0-only
//! API/shared-chain composition only: injected collaborators never create a
//! workflow, native session, source plan, or a successful serving run.
use api::owner_chain::{ChainPorts, HostChain, ModelAdapter, SharedHostChain, unwired};
use owner_run::{
    chain::{PortError, StepEnv, StepError},
    machine::{OwnerRunError, Reason},
    owner_chain::{ChainPorts as OwnerPorts, Ports},
    runner::Chain,
    steps::{
        exchange::{Announcement, ClockSkew, MemberRow, ReportsPort},
        model::{PreparedSource, Reverify},
        publish::{WorkPort, WorkPorts},
    },
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
};

struct Missing;
impl ChainPorts for Missing {
    fn owner_and_running(&self) -> bool {
        true
    }
    fn swarm_created(&self) -> bool {
        true
    }
    fn acquisition_state(&self) -> String {
        "closed".into()
    }
    fn reverify(&self) -> Reverify {
        Reverify::Failed
    }
    fn prepared_source(&self) -> Result<PreparedSource, StepError> {
        panic!("absent model must refuse")
    }
    fn storage_consent_cap(&self) -> Result<Option<Value>, StepError> {
        Ok(None)
    }
    fn check_rollover_members(&self, _: &Value) -> Result<(), StepError> {
        panic!("no rollover")
    }
}
#[derive(Default)]
struct Env {
    calls: Vec<String>,
}
impl StepEnv for Env {
    fn progress(&mut self, detail: &str) {
        self.calls.push(detail.into());
    }
    fn stopped(&self) -> bool {
        false
    }
    fn now(&mut self) -> f64 {
        self.calls.push("clock".into());
        0.0
    }
    fn wall(&mut self) -> f64 {
        panic!("no wall read")
    }
    fn set_result(&mut self, _: &str, _: Value) {
        panic!("no publication without proposal")
    }
    fn record_exception(&mut self, _: &str, _: &str) {
        panic!("no exception recording")
    }
    fn pause(&mut self, _: f64) -> bool {
        panic!("no wait without proposal")
    }
}
struct Reports;
impl ReportsPort for Reports {
    fn forget_reports(&mut self) {
        panic!("no descriptors")
    }
    fn announce(&mut self, _: &Announcement) -> Result<(), StepError> {
        panic!("no announcement")
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        panic!("no reports")
    }
    fn members(&mut self) -> Vec<MemberRow> {
        panic!("no members")
    }
    fn own_node_id(&mut self) -> Option<String> {
        panic!("no owner lookup")
    }
    fn member_identity(&mut self, _: &str) -> Value {
        panic!("no identity lookup")
    }
    fn clock_skew(&mut self, _: &[String]) -> Result<Option<ClockSkew>, StepError> {
        panic!("no clock diagnostics")
    }
    fn tick_local(&mut self) {
        panic!("no tick")
    }
}
type CleanupCallback = Arc<Mutex<Option<Box<dyn Fn() + Send>>>>;
struct Workflow {
    installs: Arc<Mutex<usize>>,
    callback: CleanupCallback,
}
impl WorkPort for Workflow {
    fn install_cleanup_progress(&mut self) -> Result<(), StepError> {
        *self.installs.lock().unwrap() += 1;
        // A retained host callback owns captures, never the temporary WorkPorts
        // borrow or its host. No Runner is captured and no success is fabricated.
        let weak = Arc::downgrade(&self.installs);
        *self.callback.lock().unwrap() = Some(Box::new(move || {
            if let Some(count) = weak.upgrade() {
                *count.lock().unwrap() += 1;
            }
        }));
        Ok(())
    }
    fn start_serving(&mut self, _: &Value) -> Result<Value, PortError> {
        panic!("missing proposal must refuse before start")
    }
    fn workflow_state(&mut self) -> Result<Value, StepError> {
        panic!("missing proposal must refuse before status")
    }
}
struct Host {
    model: ModelAdapter<Missing>,
    reports: Reports,
    workflow: Workflow,
}
impl OwnerPorts for Host {
    fn ports(&mut self) -> Ports<'_> {
        Ports {
            model: &mut self.model,
            contributor: None,
            reports: None,
            observe: None,
            compile: None,
        }
    }
    fn work_ports(&mut self) -> Option<WorkPorts<'_>> {
        Some(WorkPorts {
            reports: &mut self.reports,
            workflow: &mut self.workflow,
        })
    }
}

#[test]
fn legacy_host_preserves_every_missing_collaborator_refusal_and_lazy_state() {
    let mut chain = HostChain::new(Missing);
    assert!(chain.state().is_none());
    for step in ["model", "descriptors"] {
        assert_eq!(
            chain.run_step(step, &mut Env::default()),
            Err(StepError::Exception {
                class: "KeyError".into()
            })
        );
    }
    chain.new_run("run", "deployment", Reason::Start, Some(&json!(42)), None);
    assert_eq!(chain.state().unwrap().execution_ceiling, Some(json!(42)));
    for (step, tag, seam) in [
        (
            "contributor",
            "NP-L10-contributor",
            "SE-L10-LOCAL-CONTRIBUTOR",
        ),
        ("descriptors", "NP-L10-descriptors", "SE-L10-REPORTS-PORT"),
        ("observe", "NP-L10-observe", "SE-L9-OBSERVE-PORT"),
        ("compile", "NP-L10-compile", "SE-L7-CHAIN-COMPILE"),
    ] {
        assert_eq!(
            chain.run_step(step, &mut Env::default()),
            Err(unwired(step, tag, seam)),
            "{step}"
        );
    }
    for step in ["slices", "publish", "targets", "activate"] {
        assert_eq!(
            chain.run_step(step, &mut Env::default()),
            Err(owner_run::steps::not_ported(step)),
            "{step}"
        );
    }
}

#[test]
fn api_dispatch_reaches_shared_publish_and_preserves_partial_failure_state() {
    let installs = Arc::new(Mutex::new(0));
    let callback = Arc::new(Mutex::new(None));
    let mut chain = SharedHostChain::new(Host {
        model: ModelAdapter::new(Missing),
        reports: Reports,
        workflow: Workflow {
            installs: installs.clone(),
            callback: callback.clone(),
        },
    });
    chain.new_run("run", "deployment", Reason::Start, None, None);
    let mut env = Env::default();
    // Independent executed r16 source case; only this missing-proposal path is
    // projected here, not its unrelated prepared plan/descriptor fields.
    let fixture: Value = serde_json::from_str(include_str!(
        "../../owner-run/tests/fixtures/oracle_work_r15_v2.json"
    ))
    .unwrap();
    let expected = &fixture["cases"]["publish-missing-proposal"];
    assert_eq!(
        chain.run_step("publish", &mut env),
        Err(OwnerRunError::new(expected["outcome"]["code"].as_str().unwrap()).into())
    );
    assert_eq!(*installs.lock().unwrap(), 1);
    assert_eq!(env.calls, ["clock"]);
    assert_eq!(
        chain.state().unwrap().execution_clock_started_at,
        expected["state"]["execution_clock_started_at"].as_f64()
    );
    assert_eq!(chain.state().unwrap().registry_origin, None);
    assert!(chain.state().unwrap().proposal.is_none());
    // The temporary WorkPorts borrow is over; its callback is still callable.
    callback.lock().unwrap().as_ref().unwrap()();
    assert_eq!(*installs.lock().unwrap(), 2);
    drop(chain);
    assert_eq!(
        Arc::strong_count(&installs),
        1,
        "callback must not retain host state strongly"
    );
    drop(installs);
    callback.lock().unwrap().as_ref().unwrap()();
}
