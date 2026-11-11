// SPDX-License-Identifier: AGPL-3.0-only
//! Characterization of the actual headless composition. No model, seed server,
//! serving workflow or native process is created by these lifetime checks.
use super::*;
use owner_run::{action::DemoFacts, owner_chain::ChainPorts as _, runner::Chain};

struct Root(PathBuf);
impl Root {
    fn new(tag: &str) -> Self {
        let path = std::env::temp_dir().join(format!("l10-chain-{tag}-{}", std::process::id()));
        assert!(!path.exists(), "fresh fixture root required");
        Self(path)
    }
}
impl Drop for Root {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).expect("remove owned fixture root");
    }
}

/// Only the action ladder is injected to install an idle Runner. No Start,
/// announcement, prepared model or successful workflow is represented.
struct StopFacts;
impl DemoFacts for StopFacts {
    fn owner(&self) -> bool {
        true
    }
    fn ensure_exchange(&mut self) -> bool {
        true
    }
    fn members_joined_without_relay(&self) -> Option<u64> {
        panic!("Stop must not query relay")
    }
}

#[test]
fn actual_headless_factory_and_installed_view_do_not_retain_core_after_owner_drop() {
    for installed in [false, true] {
        let root = Root::new(if installed { "installed" } else { "lazy" });
        let core = Arc::new(Mutex::new(
            load_core(root.0.clone()).expect("load actual Core"),
        ));
        let weak = Arc::downgrade(&core);
        let mut runs = owner_runs(&core);
        let view = runs.view();
        core.lock().unwrap().run.demo = view.clone();
        assert!(view.status().is_none());
        if installed {
            runs.demo_action(Action::DemoStop, &Map::new(), &mut StopFacts)
                .expect("install idle Runner");
            assert_eq!(view.status().unwrap().to_value()["state"], "idle");
        }
        runs.close();
        drop(runs);
        drop(core);
        assert!(
            weak.upgrade().is_none(),
            "Host/Runner strong retention after owning close/drop"
        );
        assert!(!view.active());
        assert_eq!(view.status().is_some(), installed);
    }
}

/// R32 acceptance RED: the actual production factory, not a test CompilePort.
#[test]
fn r32_actual_headless_compile_boundary_is_mounted() {
    let root = Root::new("r32-compile-boundary");
    let core = Arc::new(Mutex::new(load_core(root.0.clone()).unwrap()));
    let mut ports = HeadlessChainPorts::new(core);
    assert!(
        ports.ports().compile.is_some(),
        "production HeadlessChainPorts has no CompilePort; planner preparation is unreachable"
    );
}

/// Source start_compile checks owner/running/ack before source or native work.
/// Exercise the public action service with the real persistent HeadlessHost.
#[test]
fn r32_public_compile_refuses_missing_preparation_consent() {
    use api::desktop::Service;
    let root = Root::new("r32-public-compile");
    let core = Arc::new(Mutex::new(load_core(root.0.clone()).unwrap()));
    let published = Arc::new(PublishedStatus::new());
    let runs = Arc::new(Mutex::new(owner_runs(&core)));
    core.lock().unwrap().run.demo = runs.lock().unwrap().view();
    let backend = PersistentBackend::new(
        root.0.clone(),
        HeadlessHost {
            core: core.clone(),
            runs: Some(runs.clone()),
        },
        Rest { core, published },
    );
    let control = Control::new(SignedBackend::new(backend, now));
    let error = control
        .action(
            "/api/serving/compile",
            json!({"model_preparation_acknowledged": false})
                .as_object()
                .unwrap(),
        )
        .expect_err("missing acknowledgement must refuse");
    runs.lock().unwrap().close();
    // app.py:344-351/2011-2032 collapses builtin ValueError to this code;
    // its private bounded exception identity must still name the real refusal.
    assert_eq!((error.status, error.code), (409, "action_not_completed"));
    assert_eq!(error.raised.identity(), ("ValueError", "builtins"));
    let record: Value =
        serde_json::from_slice(&fs::read(root.0.join("last-action-error.private.json")).unwrap())
            .unwrap();
    assert_eq!(record["exception_type"], "ValueError");
    assert_eq!(record["exception_module"], "builtins");
}

/// Executed f4 app.py:1343-1399 (R34 oracle-preflight-e/f): closed and
/// owner checks precede body validation on every advanced serving route.
/// This uses the real Control -> PersistentBackend -> HeadlessHost, no plan.
#[test]
fn r34_public_compiler_and_workflow_guards_precede_request_shape() {
    use api::desktop::Service;
    let root = Root::new("r34-public-guards");
    let core = Arc::new(Mutex::new(load_core(root.0.clone()).unwrap()));
    let published = Arc::new(PublishedStatus::new());
    let backend = PersistentBackend::new(
        root.0.clone(),
        HeadlessHost {
            core: core.clone(),
            runs: None,
        },
        Rest {
            core: core.clone(),
            published,
        },
    );
    let control = Control::new(SignedBackend::new(backend, now));
    for (closed, target, expected) in [
        (true, false, "application_closed"),
        (true, true, "application_closed"),
        (false, true, "owner_action_required"),
    ] {
        {
            let mut c = core.lock().unwrap();
            c.closed = closed;
            c.target = target;
        }
        for route in ["compile", "start", "activate", "status", "stop"] {
            let result = control.action(
                &format!("/api/serving/{route}"),
                json!({"unrelated":true}).as_object().unwrap(),
            );
            let error = result.expect_err("closed/target serving request must refuse");
            assert_eq!(
                (error.status, error.code),
                (400, expected),
                "{route} closed={closed} target={target}"
            );
        }
    }
    assert!(!root.0.join("chain-runs").exists());
    assert!(!root.0.join("last-action-error.private.json").exists());
}

/// No workflow is not success/readiness: source app.py:1394-1399 returns the
/// unchanged absent state for Stop, even before a run/Compiler is installed.
#[test]
fn r34_public_stop_without_workflow_returns_actual_absent_state() {
    use api::desktop::Service;
    let root = Root::new("r34-public-stop");
    let core = Arc::new(Mutex::new(load_core(root.0.clone()).unwrap()));
    let backend = PersistentBackend::new(
        root.0.clone(),
        HeadlessHost {
            core: core.clone(),
            runs: None,
        },
        Rest {
            core: core.clone(),
            published: Arc::new(PublishedStatus::new()),
        },
    );
    let control = Control::new(SignedBackend::new(backend, now));
    let expected = json!({"compilation":null,"serving":null,"route_ready":false});
    assert_eq!(
        control.action("/api/serving/stop", &Map::new()).unwrap(),
        expected
    );
    assert_eq!(
        control.action("/api/serving/status", &Map::new()).unwrap(),
        expected
    );
    assert!(core.lock().unwrap().compiler.is_none());
    assert!(!root.0.join("chain-runs").exists());
}

struct NoEffects;
impl owner_run::chain::StepEnv for NoEffects {
    fn progress(&mut self, _: &str) {
        panic!("missing capability must not publish progress")
    }
    fn stopped(&self) -> bool {
        false
    }
    fn now(&mut self) -> f64 {
        panic!("missing capability must not sample clock")
    }
    fn wall(&mut self) -> f64 {
        panic!("missing capability must not sample wall")
    }
    fn set_result(&mut self, _: &str, _: Value) {
        panic!("missing capability must not publish result")
    }
    fn record_exception(&mut self, _: &str, _: &str) {
        panic!("missing capability must not call observer")
    }
    fn pause(&mut self, _: f64) -> bool {
        panic!("missing capability must not wait")
    }
}

#[test]
fn actual_headless_ports_share_one_agent_and_keep_missing_producers_closed() {
    let root = Root::new("ports");
    let core = Arc::new(Mutex::new(load_core(root.0.clone()).unwrap()));
    let mut ports = HeadlessChainPorts::new(core.clone());
    assert!(Arc::ptr_eq(
        &ports.contributor.host.0,
        &ports.reports.host.0
    ));
    assert!(Arc::ptr_eq(&ports.contributor.agent, &ports.reports.agent));
    assert!(ports.ports().contributor.is_some());
    assert!(ports.ports().reports.is_some());
    assert!(ports.ports().observe.is_some());
    assert!(matches!(
        ports.ports().observe.unwrap().prepare("run", "deployment", &[]),
        Err(owner_run::chain::PortError::Failed { code: Some(code), .. }) if code == "owner_action_required"
    ));
    assert!(ports.ports().compile.is_some());
    assert!(ports.slices_ports().is_none());
    assert!(ports.work_ports().is_none());
    let mut chain = SharedHostChain::new(ports);
    chain.new_run(
        "run",
        "deployment",
        owner_run::machine::Reason::Start,
        None,
        None,
    );
    core.lock().unwrap().running = true;
    assert_eq!(
        chain.run_step("compile", &mut NoEffects),
        Err(owner_run::machine::OwnerRunError::new("two_distinct_hosts_required").into())
    );
    for step in ["slices", "publish", "targets", "activate", "serving"] {
        assert_eq!(
            chain.run_step(step, &mut NoEffects),
            Err(owner_run::steps::not_ported(step))
        );
    }
    drop(chain);
    drop(core);
}

#[test]
fn r33_actual_compiler_preflight_is_ordered_and_leaves_no_files_or_workers() {
    let root = Root::new("r33-preflight");
    let core = Arc::new(Mutex::new(load_core(root.0.clone()).unwrap()));
    let mut ports = HeadlessChainPorts::new(core.clone());
    for (target, running, expected) in [
        (true, true, "model_preparation_consent_required"),
        (false, false, "model_preparation_consent_required"),
        (false, true, "two_distinct_hosts_required"),
    ] {
        {
            let mut c = core.lock().unwrap();
            c.target = target;
            c.running = running;
        }
        let error = ports.ports().compile.unwrap().compile_inputs().unwrap_err();
        assert_eq!(error.code_or("unexpected"), expected);
        assert!(!root.0.join("chain-runs").exists());
    }
    let held = ports.compiler.0.lock().unwrap();
    let view = compiler::status(&core).to_value();
    assert_eq!(
        view,
        json!({"compilation":null,"serving":null,"route_ready":false})
    );
    drop(held);
    ports.teardown(true, &mut NoEffects).unwrap();
    ports.teardown(true, &mut NoEffects).unwrap();
    assert!(!root.0.join("chain-runs").exists());
}
