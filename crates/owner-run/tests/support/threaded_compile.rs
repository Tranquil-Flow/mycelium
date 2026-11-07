// SPDX-License-Identifier: AGPL-3.0-only
//! Real Runner + OwnerChain + planner, with a genuinely held builder worker behind CompilePort.
//! Only the expensive builder output is scripted from FX-SLICE1-run-r12; this is not native execution.
use super::*;
use owner_run::steps::compile::{BuildPoll, CompileContext, CompileInputs, CompilePort};
use planner::chain_compile::BuildRequest;
use std::sync::{Condvar, mpsc};
use std::thread::JoinHandle;

#[derive(Default)]
struct Gate {
    released: Mutex<bool>,
    wake: Condvar,
}
impl Gate {
    fn release(&self) {
        *lock(&self.released) = true;
        self.wake.notify_all();
    }
    fn wait(&self) {
        let mut released = lock(&self.released);
        while !*released {
            released = self.wake.wait(released).unwrap();
        }
    }
}
struct ReleaseOnDrop(Arc<Gate>);
impl Drop for ReleaseOnDrop {
    fn drop(&mut self) {
        self.0.release();
    }
}

struct WorkerPort {
    inner: CompileDouble,
    gate: Arc<Gate>,
    entered: mpsc::Sender<()>,
    worker: Option<JoinHandle<BuildPoll>>,
}
impl CompilePort for WorkerPort {
    fn compile_context(&mut self) -> Result<CompileContext, PortError> {
        self.inner.compile_context()
    }
    fn compile_inputs(&mut self) -> Result<CompileInputs, PortError> {
        self.inner.compile_inputs()
    }
    fn retire_bootstrap(&mut self) -> Result<(), PortError> {
        self.inner.retire_bootstrap()
    }
    fn start_build(&mut self, request: &BuildRequest) {
        assert!(self.worker.is_none(), "one build per run");
        self.inner.start_build(request);
        let mut builder = self.inner.clone();
        builder.delay = 0;
        let gate = self.gate.clone();
        let entered = self.entered.clone();
        self.worker = Some(std::thread::spawn(move || {
            entered.send(()).unwrap();
            gate.wait();
            builder.poll_build()
        }));
    }
    fn poll_build(&mut self) -> BuildPoll {
        if self.worker.as_ref().is_some_and(JoinHandle::is_finished) {
            self.worker.take().unwrap().join().unwrap()
        } else {
            BuildPoll::Pending
        }
    }
}
impl Drop for WorkerPort {
    fn drop(&mut self) {
        self.gate.release();
        if let Some(worker) = self.worker.take() {
            worker.join().unwrap();
        }
    }
}

struct WorkerHost {
    host: SplitHost,
    compile: WorkerPort,
}
impl ChainPorts for WorkerHost {
    fn ports(&mut self) -> Ports<'_> {
        Ports {
            compile: Some(&mut self.compile),
            ..self.host.ports()
        }
    }
    fn teardown(&mut self, failed: bool, env: &mut dyn StepEnv) -> Result<(), String> {
        self.host.teardown(failed, env)
    }
}

struct WallClock(Instant);
impl Clocks for WallClock {
    fn mono(&self) -> f64 {
        self.0.elapsed().as_secs_f64()
    }
    fn wall(&self) -> f64 {
        NOW + self.mono()
    }
}

/// Proves: B-RUN-17 (status answers while compile is held), B-RUN-24 (the actual planner and admission
/// compose through the threaded builder boundary for both fleet sizes), B-RUN-08 (result publication).
#[test]
fn held_builder_keeps_status_responsive_then_matches_recorded_runs() {
    let doc = fixture();
    for name in [
        "two-even-q3-06b",
        "three-q3-8b",
        "two-build-raises",
        "two-member-wall-invalid",
    ] {
        let case = &doc["cases"][name];
        let rec = Arc::new(Mutex::new(Record::default()));
        let host = case_ports(&rec, case);
        let gate = Arc::new(Gate::default());
        let (entered_tx, entered_rx) = mpsc::channel();
        let compile = WorkerPort {
            inner: host.compile.clone(),
            gate: gate.clone(),
            entered: entered_tx,
            worker: None,
        };
        let sink = Arc::new(Mutex::new(RunState::new(
            "",
            "",
            owner_run::machine::Reason::Start,
        )));
        let runner = Runner::spawn(
            Box::new(OwnerChain::new(WorkerHost { host, compile }).with_state_sink(sink.clone())),
            Box::new(|| Identity {
                run_id: "run-1".into(),
                deployment_id: "dep-1".into(),
            }),
            Arc::new(WallClock(Instant::now())),
            Timing::registered(),
        )
        .unwrap();
        // Declared after runner: panic unwinding releases the builder before Runner joins.
        let release = ReleaseOnDrop(gate.clone());
        runner.start("start", None).unwrap();
        entered_rx
            .recv_timeout(support::patience())
            .expect("builder worker entered");
        let status = std::thread::scope(|scope| {
            let (tx, rx) = mpsc::channel();
            let runner = &runner;
            scope.spawn(move || tx.send(runner.status()).unwrap());
            let result = rx.recv_timeout(support::patience());
            // Even a broken locking implementation must be allowed to unwind and join.
            gate.release();
            result
        })
        .expect("status answers BEFORE the held builder is released");
        assert_eq!(status["state"], "running", "{name}");
        assert_eq!(status["step"], "compile", "{name}");
        assert!(
            status["build_seconds"].is_null(),
            "{name}: not finished yet"
        );
        eventually("held build's run to end", || {
            !runner.thread_alive() && runner.quiescent()
        });
        let mut snapshot = runner.snapshot();
        snapshot["started_at"] = (!snapshot["started_at"].is_null()).into();
        assert_eq!(
            snapshot, case["snapshot"],
            "{name}: threaded compile final status"
        );
        assert_eq!(
            lock(&sink)
                .chain_compile_state
                .clone()
                .unwrap_or(Value::Null),
            case["chain_compile_state"],
            "{name}: compiled state"
        );
        runner.close();
        drop(release);
        assert_eq!(
            Arc::strong_count(&gate),
            1,
            "builder and host were joined/dropped"
        );
    }
}
