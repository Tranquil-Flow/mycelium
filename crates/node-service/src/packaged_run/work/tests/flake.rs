// SPDX-License-Identifier: AGPL-3.0-only
//! Lane FLK: the intermittent failures of rows 6 (slices) and 7 (publish) of the ten-row gate,
//! each as a deterministic test (an injected hold, never a sleep deciding the outcome).
use super::*;

/// The run state of `handoff_and_push_before_publish` up to the slices step: composed work over
/// both hosts on loopback, the proposal and the model the step reads.
fn composed(f: &mut Fixture) -> (PackagedChainPorts, RunState, Env) {
    let ports = PackagedChainPorts::new(&f.host, None, None);
    let mut state = run_state(f, &ports);
    let mut env = Env { at: Instant::now() };
    ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap();
    ready_for_slices(&mut state);
    (ports, state, env)
}

/// What a renewal does when it reaches its put-back (lane FLK patch 2).
#[derive(Clone, Copy, PartialEq)]
enum AtPutBack {
    /// Waits there until the test releases it: the renewal is in flight for as long as the test
    /// needs (its take and its seed round trip have run).
    Hold,
    /// Its thread dies there: the Session never comes back (FLK-02).
    Panic,
    /// It proceeds at once: the wrapper only steps the renewal's clock (FLK2-02).
    Pass,
}
/// Met once by a renewal thread that took its Session and is about to put it back; the take, the
/// signed heartbeat and the seed's answer are the product's own (`owner_ports::renew_contributor`,
/// `app_host::renew_member`). Never released by elapsed time: a test that fails before its release
/// drops the sender and the renewal proceeds.
struct Gate {
    mode: AtPutBack,
    met: AtomicBool,
    entered: Mutex<Option<std::sync::mpsc::Sender<()>>>,
    release: Mutex<Option<std::sync::mpsc::Receiver<()>>>,
}
impl Gate {
    fn new(
        mode: AtPutBack,
    ) -> (
        Arc<Self>,
        std::sync::mpsc::Receiver<()>,
        std::sync::mpsc::Sender<()>,
    ) {
        let (entered, at_gate) = std::sync::mpsc::channel();
        let (release, released) = std::sync::mpsc::channel();
        let gate = Arc::new(Self {
            mode,
            met: AtomicBool::new(false),
            entered: Mutex::new(Some(entered)),
            release: Mutex::new(Some(released)),
        });
        (gate, at_gate, release)
    }
    fn meet(&self) {
        if self.met.swap(true, Ordering::SeqCst) {
            return;
        }
        if let Some(entered) = lock(&self.entered).take() {
            entered.send(()).unwrap_or(());
        }
        match self.mode {
            AtPutBack::Hold => {
                if let Some(released) = lock(&self.release).take() {
                    // Err only when the test dropped its sender: proceed either way.
                    released.recv().unwrap_or(());
                }
            }
            AtPutBack::Panic => panic!("lane FLK: the renewal thread dies between take and put-back"),
            AtPutBack::Pass => {}
        }
    }
}
/// A renewal thread stopped at its gate, and the sender that releases it.
type HeldRenewal = (std::thread::JoinHandle<()>, std::sync::mpsc::Sender<()>);
/// The bound a test waits for a renewal thread to reach its gate (failure detection only, never
/// a release): one renewal pass on the wire (`Network::await_renewal_pass`).
fn gate_bound() -> Duration {
    span(BudgetId::PyMyceliumA13MacosPeerRemotemacmemberInitTimeoutL117).unwrap() * 2
}
/// The owner's host as `owner_ports::renew_contributor` sees it, meeting `gate` on the first state
/// acquisition while the contributor's Session is out: its put-back. Its renewal clock reads the
/// wall clock `ahead` seconds forward (0 but in the FLK2-02 tests: a forward step of the clock).
#[derive(Clone)]
struct HeldContributor {
    host: PackagedRun,
    gate: Arc<Gate>,
    ahead: f64,
}
impl owner_ports::RunHost for HeldContributor {
    type Managed = crate::packaged_run::Managed;
    fn with<R>(&self, f: impl FnOnce(&mut api::owner_ports::RunView<'_, Self::Managed>) -> R) -> R {
        let out = lock(&self.host.run)
            .contributor
            .as_ref()
            .is_some_and(|m| m.session.is_none());
        if out {
            self.gate.meet();
        }
        self.host.with(f)
    }
    fn bootstrap_lifecycle(&self) -> Arc<Mutex<()>> {
        self.host.bootstrap_lifecycle()
    }
    fn membership_current(&self) -> bool {
        self.host.membership_current()
    }
    fn admit_contributor(&self, root: &Path) -> Result<(), PublicError> {
        self.host.admit_contributor(root)
    }
    fn close_contributor(&self) -> Result<(), PublicError> {
        self.host.close_contributor()
    }
    fn resource_check(&self) -> Option<&'static str> {
        self.host.resource_check()
    }
    fn acquisition(&self) -> Option<api::acquisition::Acquisition> {
        self.host.acquisition()
    }
    fn bootstrap(
        &self,
        node: &str,
        run: &str,
        deployment: &str,
        offer: &str,
    ) -> Result<(), api::installation_descriptor::Failure> {
        self.host.bootstrap(node, run, deployment, offer)
    }
    fn sidecar(&self) -> Result<PathBuf, api::installation_descriptor::Failure> {
        self.host.sidecar()
    }
    fn renewal_clock(&self) -> f64 {
        app_host::now() + self.ahead
    }
}
/// The owner's contributor renewal (`owner_ports::renew_contributor`, the product's pass) on its
/// own thread, stopped at its put-back as `mode` says; returns once it is there.
fn contributor_renewal(f: &Fixture, mode: AtPutBack) -> HeldRenewal {
    let (gate, at_gate, release) = Gate::new(mode);
    let held = HeldContributor {
        host: f.host.clone(),
        gate,
        ahead: 0.0,
    };
    let renewal = std::thread::spawn(move || owner_ports::renew_contributor(&held));
    at_gate
        .recv_timeout(gate_bound())
        .expect("the contributor renewal took its Session and reached its put-back");
    (renewal, release)
}
/// The joined Mac's authority as `app_host::renew_member` sees it, meeting `gate` on the first
/// state acquisition while its Session is out for the renewal: its put-back. Its renewal clock
/// reads the wall clock `ahead` seconds forward (0 but in the FLK2-02 tests).
struct HeldMember {
    authority: Arc<Mutex<Authority>>,
    network: Arc<Network>,
    gate: Arc<Gate>,
    ahead: f64,
}
impl HeldMember {
    fn meet_at_put_back(&self) {
        let out = {
            let a = lock(&self.authority);
            a.session.is_none() && a.session_view.is_some()
        };
        if out {
            self.gate.meet();
        }
    }
}
impl app_host::Cell for HeldMember {
    type Managed = crate::packaged_run::Managed;
    fn with<R>(&self, f: impl FnOnce(&mut app_host::HostView<'_, Self::Managed>) -> R) -> R {
        self.meet_at_put_back();
        self.network.cell(&self.authority).with(f)
    }
    fn try_with<R>(
        &self,
        f: impl FnOnce(&mut app_host::HostView<'_, Self::Managed>) -> R,
    ) -> Option<R> {
        self.meet_at_put_back();
        self.network.cell(&self.authority).try_with(f)
    }
    fn member_seed_options(&self) -> api::seed::SeedOptions {
        self.network.cell(&self.authority).member_seed_options()
    }
    fn renewal_clock(&self) -> f64 {
        app_host::now() + self.ahead
    }
}
/// The joined Mac's renewal (`app_host::renew_member`, the pass `Network::renew_member` runs) on its
/// own thread, stopped at its put-back as `mode` says; returns once it is there.
fn member_renewal(f: &Fixture, mode: AtPutBack) -> HeldRenewal {
    let (gate, at_gate, release) = Gate::new(mode);
    let held = HeldMember {
        authority: f.member_authority.clone(),
        network: f.network.clone(),
        gate,
        ahead: 0.0,
    };
    let renewal = std::thread::spawn(move || app_host::renew_member(&held));
    at_gate
        .recv_timeout(gate_bound())
        .expect("the joined Mac's renewal took its Session and reached its put-back");
    (renewal, release)
}
/// The rest of `Network::renew_member` after its pass: the work session follows the authority, and
/// a membership no longer current closes the work.
fn member_renewal_tail(f: &Fixture) {
    f.network.sync_work_session(&f.member_authority);
    if crate::packaged_network::membership_state(&f.member_authority).as_deref() != Some("current")
    {
        f.network.close_work();
    }
}

/// The packaged run's reports, except that its work announcement runs while the owner's own
/// contributor is in the middle of a renewal: the product's `owner_ports::renew_contributor` runs on
/// its own thread and is held at its put-back (Session out, its seed round trip done) for the
/// length of the announcement, then released whatever the announcement answered.
struct RenewalInFlight<'a> {
    inner: &'a mut dyn ReportsPort,
    fixture: &'a Fixture,
    /// The announcement's own answer, as the step saw it.
    announced: Option<Result<(), String>>,
}
impl ReportsPort for RenewalInFlight<'_> {
    fn forget_reports(&mut self) {
        self.inner.forget_reports();
    }
    fn announce(&mut self, a: &Announcement) -> Result<(), StepError> {
        self.inner.announce(a)
    }
    fn announce_work(&mut self, a: &Announcement, limits: &Value) -> Result<(), StepError> {
        let (renewal, release) = contributor_renewal(self.fixture, AtPutBack::Hold);
        let answer = self.inner.announce_work(a, limits);
        release.send(()).unwrap_or(());
        renewal.join().expect("the renewal pass returned");
        assert!(
            self.fixture
                .host
                .with(|c| c.run.contributor.as_ref().expect("still enrolled").session.is_some()),
            "the renewal put its Session back"
        );
        self.announced = Some(match &answer {
            Ok(()) => Ok(()),
            Err(StepError::Refused(e)) => Err(e.code.clone()),
            Err(other) => Err(format!("{other:?}")),
        });
        answer
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        self.inner.reports()
    }
    fn members(&mut self) -> Vec<MemberRow> {
        self.inner.members()
    }
    fn own_node_id(&mut self) -> Option<String> {
        self.inner.own_node_id()
    }
    fn member_identity(&mut self, node: &str) -> Value {
        self.inner.member_identity(node)
    }
    fn clock_skew(&mut self, nodes: &[String]) -> Result<Option<ClockSkew>, StepError> {
        self.inner.clock_skew(nodes)
    }
    fn tick_local(&mut self) {
        self.inner.tick_local();
    }
}

/// Lane FLK, row 6 `member_work_renewing` (orchestrator gate `20261115T065248Z`, 54.7 s into the
/// slices step, nothing else failed). The contributor's renewal loop (every 20 s,
/// `app_host.rs:995-1004`) takes the owner's own contributor's Session out for one seed heartbeat;
/// the slices step's work announcement (`slices.rs:207`) re-registers the owner's local worker
/// (`Reports::announce_work` -> `Work::local`), which reads that Session and answers
/// `member_work_renewing` while it is out (`packaged_run/work.rs:244`). `compose` retries that
/// code (`:444-446`); the announcement does not, so the step fails whenever a renewal is in flight
/// at that instant. The step must not fail on a renewal in flight.
#[test]
fn the_slices_step_survives_a_contributor_renewal_in_flight_at_its_work_announcement() {
    let mut f = Fixture::new();
    let (mut ports, mut state, mut env) = composed(&mut f);
    let slices = ports.slices();
    let mut reports = RenewalInFlight {
        inner: slices.reports,
        fixture: &f,
        announced: None,
    };
    let outcome = owner_run::steps::slices::run_slices_step(
        &mut env,
        &mut reports,
        slices.slices,
        &mut state,
        span(BudgetId::OwnerPoll).unwrap().as_secs_f64(),
    );
    let announced = reports.announced.clone();
    ports.close_work().unwrap();
    assert!(
        announced == Some(Ok(())) && outcome.is_ok(),
        "the work announcement during the renewal flight answered {announced:?}; the slices step: {outcome:?}"
    );
}

/// Lane FLK, the other half of the brief's question: can the contributor's Session stay absent
/// (a renewal that failed and did not put it back)? A renewal against a seed that is gone fails
/// (`contributor renew: ...` on stderr) and still restores the Session (`owner_ports.rs:319-325`
/// runs whatever `peer::renew` answered). Only a panic inside the renewal would lose it (the
/// renewal loop's thread would end with it); none is reachable from the gate's logs.
#[test]
fn a_failed_contributor_renewal_puts_the_session_back() {
    let f = Fixture::new();
    let seed = f.host.with(|c| c.authority.seed.clone().unwrap());
    seed.close();
    owner_ports::renew_contributor(&f.host);
    assert!(
        f.host
            .with(|c| c.run.contributor.as_ref().unwrap().session.is_some()),
        "a failed renewal left the contributor without its Session"
    );
}

/// Lane FLK probe (not a gate): the natural renewal window, with no injected hold. A thread runs
/// the real `owner_ports::renew_contributor` once a second (the product runs it every 20 s) while
/// this thread re-registers the owner's local worker the way the work announcement does
/// (`Work::local`). Prints the renewal durations and how many registrations answered
/// `member_work_renewing`; a product run's chance per announcement is about (renewal time / 20 s).
#[test]
#[ignore = "probe: prints the natural renewal window (lane FLK repeat harness)"]
fn probe_the_natural_contributor_renewal_window() {
    let mut f = Fixture::new();
    let (mut ports, _state, _env) = composed(&mut f);
    let network = ports.work.run_network();
    let stop = Arc::new(AtomicBool::new(false));
    let host = f.host.clone();
    let renewing = {
        let stop = stop.clone();
        std::thread::spawn(move || {
            let mut took = Vec::new();
            while !stop.load(Ordering::SeqCst) {
                let at = Instant::now();
                owner_ports::renew_contributor(&host);
                took.push(at.elapsed().as_secs_f64() * 1000.0);
                // A probe's pacing only (the product waits 20 s): nothing is decided by it.
                std::thread::sleep(span(BudgetId::OwnerPoll).unwrap() * 2);
            }
            took
        })
    };
    let (mut calls, mut refused) = (0u32, 0u32);
    let until = Instant::now() + span(BudgetId::RouterClose).unwrap() * 5;
    while Instant::now() < until {
        calls += 1;
        match ports.work.local(&network) {
            Err(StepError::Refused(e)) if e.code == "member_work_renewing" => refused += 1,
            Err(other) => panic!("{other:?}"),
            Ok(()) => {}
        }
        std::thread::sleep(span(BudgetId::OwnerPoll).unwrap() / 50);
    }
    stop.store(true, Ordering::SeqCst);
    let mut took = renewing.join().unwrap();
    took.sort_by(f64::total_cmp);
    let at = |q: f64| took[((took.len() - 1) as f64 * q) as usize];
    eprintln!(
        "FLK_RENEWAL {}",
        json!({"renewals":took.len(),"renew_ms_p50":at(0.5),"renew_ms_p90":at(0.9),
            "renew_ms_max":at(1.0),"registrations":calls,"renewing":refused})
    );
    ports.close_work().unwrap();
}

/// A stage plan as large as the real one: one shard per participant of `bytes` (a multiple of
/// 64 MiB), segments of 64 MiB, the fixture's recipe with its files replaced. The bytes are one
/// random MiB repeated (the first byte of each segment names the stage); every recipe hash is computed here (segments in memory, the whole file by
/// `/usr/bin/openssl`, outside this crate's dependencies).
fn big_plan(
    f: &Fixture,
    state: &RunState,
    bytes: u64,
) -> (Plan, Arc<runtime::artifact_journal::PinnedObjects>) {
    use std::io::Write;
    const SEGMENT: u64 = 64 << 20;
    assert!(bytes > 0 && bytes.is_multiple_of(SEGMENT), "{bytes}");
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../../../planner/tests/fixtures/source-slices.json"
    ))
    .unwrap();
    let case = &fixture["cases"][0];
    let objects = f.root.join("stage-shards");
    fs::create_dir(&objects).unwrap();
    let block = app_host::random(1 << 20).unwrap();
    let mut segment: Vec<u8> = block.repeat((SEGMENT >> 20) as usize);
    let (mut files, mut plans) = (Map::new(), Map::new());
    for (i, node) in state.participants.iter().enumerate() {
        // Distinct bytes per stage: two equal objects would be one content address.
        segment[0] = i as u8;
        let segment_digest = protocol::canonical::sha256_prefixed(&segment);
        let name = format!("flk-stage-{i}.safetensors");
        let path = objects.join(&name);
        let mut out = fs::File::create(&path).unwrap();
        for _ in 0..bytes / SEGMENT {
            out.write_all(&segment).unwrap();
        }
        out.sync_all().unwrap();
        drop(out);
        let digest = std::process::Command::new("/usr/bin/openssl")
            .args(["dgst", "-sha256", "-r"])
            .arg(&path)
            .output()
            .unwrap();
        let hex = String::from_utf8(digest.stdout).unwrap();
        let content = format!("sha256:{}", hex.split_whitespace().next().unwrap());
        let segments: Vec<Value> = (0..bytes / SEGMENT)
            .map(|k| json!({"offset":k * SEGMENT,"length":SEGMENT,"sha256":segment_digest}))
            .collect();
        let row = json!({"content_digest":content,"path":format!("deployment/{name}"),
            "size_bytes":bytes});
        let mut recipe = case["node_plans"][format!("node-{i}")].clone();
        recipe["node_id"] = json!(node);
        recipe["files"] = json!([{"content_digest":content,"path":format!("deployment/{name}"),
            "segments":segments,"size_bytes":bytes}]);
        recipe["download_bytes"] = json!(bytes);
        recipe["inline_bytes"] = json!(0);
        recipe.as_object_mut().unwrap().remove("node_plan_digest");
        recipe["node_plan_digest"] = json!(protocol::canonical::digest(&recipe).unwrap());
        files.insert(node.clone(), json!([row]));
        plans.insert(node.clone(), recipe);
    }
    let objects = objects::view(&f.root, &json!({"node_files":files})).unwrap();
    (
        Plan {
            run_id: state.run_id.clone(),
            deployment_id: state.deployment_id.clone(),
            model: json!({"model_id":"Qwen/Qwen3-0.6B","resolved_commit":"c1899de289a04d12100db370d81485cdf75e47ca","manifest_digest":"sha256:00"}),
            document: json!({"node_files":files,"node_slice_plans":plans}),
            objects: objects.path,
            journal: f.root.join("journal"),
            ranges: state
                .participants
                .iter()
                .enumerate()
                .map(|(i, n)| (n.clone(), [14 * i as u64, 14 * (i as u64 + 1)]))
                .collect(),
            tokens: 39,
            forbidden_source: f.root.join("snapshot"),
            worker_walls: state.worker_wall_limits.clone(),
            owner_root: f.root.join("owner"),
            challenge: None,
        },
        objects.pinned,
    )
}

/// Lane FLK repeat probe (not a gate): rows 6 and 7 of the ten-row gate without a model. Both
/// packaged hosts in this process, the work transport on loopback or (with `MYCELIUM_LAN_IP`
/// and `MYCELIUM_FLK_LAN=1`) on this Mac's LAN interface as in the gate; a stage shard per host of
/// `MYCELIUM_FLK_SHARD_MIB` MiB (default 896, the real stages are ~905 MB); the slices step
/// pushes both; the publish step's production admission (`Proven`: `prepare_native` on both
/// members at once, each verifying its 896 MiB) then refuses by name, because no native program
/// is configured here. Prints one `FLK_OUTCOME` line: the rows reached, their walls and codes.
/// Any control code (`control_*`), `member_work_renewing` or a staging timeout is a reproduction.
#[test]
#[ignore = "probe: lane FLK repeat harness (large push, real prepare_native, no model)"]
fn probe_rows_6_and_7_with_a_real_sized_push() {
    let mib: u64 = std::env::var("MYCELIUM_FLK_SHARD_MIB")
        .ok()
        .map_or(896, |v| v.parse().unwrap());
    let mut f = if std::env::var("MYCELIUM_FLK_LAN").is_ok_and(|v| v == "1") {
        let ip = lan_ip();
        Fixture::on(WorkNetwork::Fixed(NetworkChoice(Some((
            ip,
            runtime::iroh_host::LanPolicy::private_only(),
        )))))
    } else {
        Fixture::new()
    };
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let mut state = run_state(&mut f, &ports);
    let mut env = Env { at: Instant::now() };
    // With MYCELIUM_FLK_LOOPS=1 the hosts' own loops run beside the run, at the product's periods
    // (`app_host.rs`: the owner's contributor renewal and the joined Mac's renewal every
    // `RenewLoopSelfClosedWait` = 20 s, the joined Mac's standing pass every
    // `StandingLoopSelfClosedWait` = 2 s); otherwise the test ticks the joined Mac's agent itself.
    let loops = std::env::var("MYCELIUM_FLK_LOOPS").is_ok_and(|v| v == "1");
    let stop = Arc::new(AtomicBool::new(false));
    let mut threads = Vec::new();
    let agent = Arc::new(Mutex::new(std::mem::take(&mut f.agent)));
    if loops {
        let every = |id: BudgetId| span(id).unwrap();
        let pace = |stop: &AtomicBool, period: Duration| {
            // A loop's period only (as `HostLoop`'s `recv_timeout`); it decides no outcome.
            let until = Instant::now() + period;
            while !stop.load(Ordering::SeqCst) && Instant::now() < until {
                std::thread::sleep(period.min(span(BudgetId::OwnerPoll).unwrap()));
            }
            !stop.load(Ordering::SeqCst)
        };
        let renew = every(BudgetId::PyMyceliumA13MacosAppDemoappRenewLoopSelfClosedWaitL917);
        let standing = every(BudgetId::PyMyceliumA13MacosAppDemoappStandingLoopSelfClosedWaitL1203);
        let (host, s1) = (f.host.clone(), stop.clone());
        threads.push(std::thread::spawn(move || {
            while pace(&s1, renew) {
                owner_ports::renew_contributor(&host);
            }
        }));
        let (network, authority, s2) =
            (f.network.clone(), f.member_authority.clone(), stop.clone());
        threads.push(std::thread::spawn(move || {
            while pace(&s2, renew) {
                network.renew_member(&authority);
            }
        }));
        // The product's own standing pass (`packaged_network::standing_loop`), its own agent.
        let (mut pass, s3) = (
            crate::packaged_network::standing_loop(f.member.clone()),
            stop.clone(),
        );
        threads.push(std::thread::spawn(move || {
            while pace(&s3, standing) {
                pass();
            }
        }));
    }
    let begun = Instant::now();
    let plan = big_plan(&f, &state, mib << 20);
    let plan_ms = begun.elapsed().as_millis();
    let begun = Instant::now();
    ports
        .work
        .compose(&state, plan, Arc::new(NoCodec), &mut env)
        .unwrap();
    ready_for_slices(&mut state);
    let slices = ports.slices();
    let six = owner_run::steps::slices::run_slices_step(
        &mut env,
        slices.reports,
        slices.slices,
        &mut state,
        span(BudgetId::OwnerPoll).unwrap().as_secs_f64(),
    );
    let six_ms = begun.elapsed().as_millis();
    let code = |r: &Result<(), StepError>| match r {
        Ok(()) => Value::Null,
        Err(StepError::Refused(e)) => json!(e.code),
        Err(other) => json!(format!("{other:?}")),
    };
    let mut outcome = json!({"mib":mib,"plan_ms":plan_ms,"row6_ms":six_ms,"row6":code(&six)});
    if six.is_ok() {
        let begun = Instant::now();
        // The joined Mac's own agent reports its share (the handoff test's tick).
        let until = Instant::now() + span(BudgetId::RouterClose).unwrap() * 15;
        while ports.work.shares_ready().is_err() && Instant::now() < until {
            if loops {
                std::thread::sleep(span(BudgetId::OwnerPoll).unwrap());
            } else {
                lock(&agent).tick(&f.member);
            }
        }
        let publication = ports.publication();
        let seven = owner_run::steps::publish::run_publish_step(
            &mut env,
            publication.reports,
            publication.workflow,
            &mut state,
            span(BudgetId::OwnerPoll).unwrap().as_secs_f64(),
        );
        outcome["row7_ms"] = json!(begun.elapsed().as_millis());
        outcome["row7"] = code(&seven);
        outcome["workflow"] = ports.work.workflow_state().unwrap_or(Value::Null);
    }
    let begun = Instant::now();
    let closed = ports.close_work();
    outcome["close_ms"] = json!(begun.elapsed().as_millis());
    outcome["close"] = json!(format!("{closed:?}"));
    outcome["loops"] = json!(loops);
    stop.store(true, Ordering::SeqCst);
    for thread in threads {
        thread.join().unwrap();
    }
    f.agent = std::mem::take(&mut *lock(&agent));
    eprintln!("FLK_OUTCOME {outcome}");
}

/// The proposal and the model the slices and publish steps read (as the handoff test sets them).
fn ready_for_slices(state: &mut RunState) {
    let wall = BudgetId::MemberChosenWall
        .span(Entries::ZERO)
        .unwrap()
        .millis()
        / 1000;
    state.proposal = Some(json!({"max_combined_rss_bytes":2_147_483_648u64,
        "max_additional_disk_bytes":4_000_000_000u64,"max_wall_seconds":wall}));
    use owner_run::steps::model::{ModelStepOutput, PreparedModel, PreparedSource};
    state.model = Some(ModelStepOutput {
        forced_slice: false,
        model_id: "Qwen/Qwen3-0.6B".into(),
        resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
        manifest_digest: "sha256:00".into(),
        slice_mode: true,
        choice: None,
        prepared: PreparedSource {
            model: PreparedModel {
                model_id: "Qwen/Qwen3-0.6B".into(),
                resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
                architecture: "qwen3".into(),
                label: "constructor-only inert source fixture".into(),
                choice: None,
                shape_registered: true,
                owner_storage_need: 0,
            },
            manifest_digest: "sha256:00".into(),
            source_bytes: json!(0),
        },
    });
}

/// Lane FLK, rows 6 and 7 `control_connect_timeout` (gate9 run 2: one at the start of the joined
/// Mac's transfer, `owner_work_failed: control_call_failed:control_connect_timeout`; mut3: every
/// result poll of the joined Mac's `prepare_native` for 280 s). In both runs, and in no passing
/// run, the joined Mac started a NEW member work service during the step
/// (`l11_work_network:... role=member_work` between compile and the failure): its old one, whose
/// control and carrier addresses the owner verified when the run composed, was closed, so every
/// later owner call dialled an address nothing accepts on any more.
///
/// The joined Mac's renewal loop (every 20 s) takes its Session out for one seed heartbeat
/// (`app_host::renew_member`, `session.take()` + `session_view`). Its standing pass (every 2 s)
/// reads the acceptor context's membership binding from that live Session only
/// (`packaged_network.rs` `accept_context` -> `app_host::membership_binding`), finds none, and
/// closes the work service (`member_agent.rs` "let Some(binding) = &context.binding else {
/// host.close_work(); return; }"), silently. The next pass starts a new one at a new address.
/// A standing pass during the joined Mac's renewal must not end the run's work.
#[test]
fn a_joined_macs_standing_pass_during_its_renewal_keeps_the_runs_work_service() {
    let mut f = Fixture::new();
    let (mut ports, mut state, mut env) = composed(&mut f);
    // `app_host::renew_member`'s flight on the joined Mac: the product's pass, held at its put-back.
    let (renewal, release) = member_renewal(&f, AtPutBack::Hold);
    f.agent.tick(&f.member);
    release.send(()).unwrap_or(());
    renewal.join().expect("the renewal pass returned");
    member_renewal_tail(&f);
    // The standing loop's next pass, after the renewal (as on the gate's joined Mac).
    f.agent.tick(&f.member);
    let slices = ports.slices();
    let outcome = owner_run::steps::slices::run_slices_step(
        &mut env,
        slices.reports,
        slices.slices,
        &mut state,
        span(BudgetId::OwnerPoll).unwrap().as_secs_f64(),
    );
    let flow = ports.work.workflow_state().unwrap_or(Value::Null);
    let closed = ports.close_work();
    assert!(
        outcome.is_ok(),
        "the push to the members failed after the joined Mac's standing pass during its renewal: \
         {outcome:?}; workflow error {}; close {closed:?}",
        flow["error_code"]
    );
}

/// Lane FLK patch 2, FLK-01 (REVIEW-laneFLK-astra.md, "exact surviving interleaving"). The joined
/// Mac's standing pass reads its acceptor context in several separately locked observations
/// (`PackagedMember::accept_context`: the membership state, the renewal marker, the live binding,
/// the work session). A renewal that takes the Session after the pass read "current" and "not
/// renewing" but before it read the binding leaves the pass with no binding and no fallback:
/// `MemberAgent` closes the work service, the next pass opens a new one at a new address, and the
/// owner's run keeps dialling the old one. Here the product's renewal (`app_host::renew_member`)
/// starts exactly inside the pass (after its membership state was read) and is held at its
/// put-back until the pass returned. The run's work service must survive.
#[test]
fn a_joined_macs_renewal_that_starts_inside_its_standing_pass_keeps_the_runs_work_service() {
    let mut f = Fixture::new();
    let (mut ports, mut state, mut env) = composed(&mut f);
    let started: Arc<Mutex<Option<HeldRenewal>>> = Arc::default();
    {
        let (authority, network, started) = (
            f.member_authority.clone(),
            f.network.clone(),
            started.clone(),
        );
        *lock(&f.member.inside_accept_context) = Some(Box::new(move || {
            let (gate, at_gate, release) = Gate::new(AtPutBack::Hold);
            let held = HeldMember {
                authority,
                network,
                gate,
                ahead: 0.0,
            };
            let renewal = std::thread::spawn(move || app_host::renew_member(&held));
            at_gate
                .recv_timeout(gate_bound())
                .expect("the joined Mac's renewal took its Session inside the standing pass");
            *lock(&started) = Some((renewal, release));
        }));
    }
    f.agent.tick(&f.member);
    let (renewal, release) = lock(&started)
        .take()
        .expect("the standing pass read its acceptor context, and the renewal started inside it");
    release.send(()).unwrap_or(());
    renewal.join().expect("the renewal pass returned");
    member_renewal_tail(&f);
    f.agent.tick(&f.member);
    let slices = ports.slices();
    let outcome = owner_run::steps::slices::run_slices_step(
        &mut env,
        slices.reports,
        slices.slices,
        &mut state,
        span(BudgetId::OwnerPoll).unwrap().as_secs_f64(),
    );
    let flow = ports.work.workflow_state().unwrap_or(Value::Null);
    let closed = ports.close_work();
    assert!(
        outcome.is_ok(),
        "the push to the members failed after a renewal began inside the joined Mac's standing \
         pass: {outcome:?}; workflow error {}; close {closed:?}",
        flow["error_code"]
    );
}

/// The run's local work-session cell after one more acquisition of the host (whoever acquires
/// next maintains it, `PackagedRun::with`): whether the owner's local worker still admits on it.
fn local_snapshot_held(f: &Fixture, ports: &PackagedChainPorts) -> bool {
    let cell = lock(&ports.work.resident).local_session.clone();
    f.host.with(|_| ());
    lock(&cell).is_some()
}
fn code(result: &Result<(), StepError>) -> String {
    match result {
        Ok(()) => "ok".into(),
        Err(StepError::Refused(e)) => e.code.clone(),
        Err(other) => format!("{other:?}"),
    }
}

/// Lane FLK patch 2, FLK-02 (owner side). A contributor renewal whose thread dies between its take
/// and its put-back (`owner_ports::renew_contributor`) never brings the Session back. At the base
/// (`c20d4b40`) the owner's local registration then refused (`Work::local`: no Session,
/// `member_work_renewing`); patch 1 lets it register on the preceding snapshot until that lease
/// expires (up to the 300 s seed lease). A renewal that never comes back must not leave its
/// snapshot in use: the registration refuses as at the base, and the local worker's admission
/// cell no longer holds the old Session.
#[test]
fn a_contributor_renewal_whose_thread_dies_leaves_no_snapshot_in_use() {
    let mut f = Fixture::new();
    let (mut ports, _state, _env) = composed(&mut f);
    let network = ports.work.run_network();
    let (renewal, _release) = contributor_renewal(&f, AtPutBack::Panic);
    assert!(renewal.join().is_err(), "the renewal thread died at its put-back");
    let local = ports.work.local(&network);
    let held = local_snapshot_held(&f, &ports);
    let closed = ports.close_work();
    assert!(
        code(&local) == "member_work_renewing" && !held,
        "after the contributor's renewal thread died: the registration answered {}, the local \
         admission still holds the old Session: {held} (close {closed:?})",
        code(&local)
    );
}

/// Lane FLK patch 2, FLK-02 (joined side). The joined Mac's renewal thread dies between its take and
/// its put-back (`app_host::renew_member`; the network's tail never runs). At the base the next
/// standing pass found no binding and closed the work service; patch 1 keeps reading the
/// preceding snapshot through the stale `session_view` marker until the lease expires. The pass
/// must find no binding, as at the base.
#[test]
fn a_joined_renewal_whose_thread_dies_leaves_no_snapshot_in_use() {
    use api::member_agent::MemberHost;
    let mut f = Fixture::new();
    let (mut ports, _state, _env) = composed(&mut f);
    let (renewal, _release) = member_renewal(&f, AtPutBack::Panic);
    assert!(renewal.join().is_err(), "the renewal thread died at its put-back");
    let binding = f.member.accept_context().and_then(|c| c.binding);
    let closed = ports.close_work();
    assert!(
        binding.is_none(),
        "after the joined Mac's renewal thread died, its standing pass still binds {binding:?} \
         (close {closed:?})"
    );
}

/// Lane FLK patch 2, FLK-02: a renewal the seed REFUSES (here the contributor's generation was
/// fenced, `Coordinator::advance_member_generation` to STOPPED: the operator's revocation; the
/// seed answers `seed_message_mismatch`) ends the fallback at once, not at the old lease's end. Patch 1 and the base put the refused Session back
/// (`owner_ports.rs:319-325`) and the run's work cell kept it, so the local worker went on
/// admitting on it until its lease expired; the registration must refuse by name at once (not
/// `member_work_renewing`, which `compose` would retry for the whole slices budget).
#[test]
fn a_contributor_renewal_the_seed_refuses_ends_the_snapshot_at_once() {
    let mut f = Fixture::new();
    let (mut ports, _state, _env) = composed(&mut f);
    let network = ports.work.run_network();
    let seed = f.host.with(|c| c.authority.seed.clone().unwrap());
    let (node, generation) = f.host.with(|c| {
        let m = c.run.contributor.as_ref().unwrap();
        (m.node.clone(), m.session.as_ref().unwrap().generation().unwrap())
    });
    seed.coordinator
        .advance_member_generation(&node, generation, "STOPPED")
        .unwrap();
    owner_ports::renew_contributor(&f.host);
    let held = local_snapshot_held(&f, &ports);
    let local = ports.work.local(&network);
    let closed = ports.close_work();
    eprintln!("FLK02_REFUSED local={} held={held} close={closed:?}", code(&local));
    assert!(
        !held && local.is_err() && code(&local) != "member_work_renewing",
        "after the seed refused the contributor's renewal: the local admission still holds the old \
         Session: {held}; the registration answered {}",
        code(&local)
    );
}

/// Lane FLK patch 2, FLK-02 (joined side): the joined Mac's renewal that the seed refuses because
/// its generation was fenced. The seed answers `seed_message_mismatch` (its row no longer has this
/// Session's generation, `protocol/src/seed_members.rs:1069-1078`), which records no terminal
/// disposition (`handoff::peer_translate` passes it through), so the base and patch 1 put the
/// refused Session back and stay `current`, binding and serving until the old lease ends. The
/// fallback must end at once: right after that renewal the membership is not current and the
/// standing pass finds no binding.
#[test]
fn a_joined_renewal_the_seed_refuses_ends_the_binding_at_once() {
    use api::member_agent::MemberHost;
    let mut f = Fixture::new();
    let (mut ports, _state, _env) = composed(&mut f);
    let seed = f.host.with(|c| c.authority.seed.clone().unwrap());
    let generation = lock(&f.member_authority)
        .session
        .as_ref()
        .unwrap()
        .generation()
        .unwrap();
    seed.coordinator
        .advance_member_generation(&f.remote, generation, "STOPPED")
        .unwrap();
    f.network.renew_member(&f.member_authority);
    let state = crate::packaged_network::membership_state(&f.member_authority);
    let binding = f.member.accept_context().and_then(|c| c.binding);
    let closed = ports.close_work();
    assert!(
        state.as_deref() != Some("current") && binding.is_none(),
        "after the seed refused the joined Mac's renewal: membership {state:?}, binding {binding:?} \
         (close {closed:?})"
    );
}

/// Lane FLK patch 2, FLK-02, as patch 3 (FLK2-01) restates it: a contributor renewal that outlives
/// its deadline (its thread slow or stuck past the renewal's own request allowance,
/// `api::renewal::allowance`) no longer stands in for the REGISTRATION. The flight is held at its
/// put-back and its deadline is aged to now (a clock read, not a sleep): before the deadline the
/// registration stands on the snapshot; after it the registration refuses as at the base, while
/// the resident admission cell keeps the lease-bounded snapshot as the base kept it (the flight is
/// still recorded: neither landed nor unwound nor refused); the late put-back of that same flight
/// still lands, and the registration succeeds again.
#[test]
fn a_contributor_renewal_past_its_deadline_refuses_the_registration_and_keeps_the_resident_cell() {
    let mut f = Fixture::new();
    let (mut ports, _state, _env) = composed(&mut f);
    let network = ports.work.run_network();
    let (renewal, release) = contributor_renewal(&f, AtPutBack::Hold);
    let during = ports.work.local(&network);
    f.host.with(|c| {
        c.run
            .contributor
            .as_mut()
            .unwrap()
            .flight
            .as_mut()
            .expect("the renewal recorded its flight")
            .deadline = Instant::now();
    });
    let overdue = ports.work.local(&network);
    let held = local_snapshot_held(&f, &ports);
    release.send(()).unwrap_or(());
    renewal.join().expect("the late renewal pass returned");
    let after = ports.work.local(&network);
    let closed = ports.close_work();
    assert!(
        code(&during) == "ok" && code(&overdue) == "member_work_renewing" && held
            && code(&after) == "ok",
        "in flight {}, past its deadline {} (snapshot held {held}), after the late put-back {} \
         (close {closed:?})",
        code(&during),
        code(&overdue),
        code(&after)
    );
}

/// Lane FLK patch 3, FLK2-01 (REVIEW-laneFLK-p2-astra.md). A healthy contributor renewal whose
/// thread is held past its flight deadline (its seed round trip answered, not yet put back) must
/// not end the owner's local work Service: the base (`c20d4b40`) kept the lease-bounded admission
/// cell whenever the contributor's Session was out. Crosses the real boundary: the slices step
/// announced the work (the local Service runs, phase `work`); the Service's operation loop is held
/// before its next idle admission (`packaged_work::operations`); the flight is aged past its
/// deadline without acquiring the host; the slices wait's report tick (`Reports::tick_local`:
/// `Service::report`, then `host.with` to submit it) is the next acquisition; only then is the
/// loop let through its admission; then the renewal lands. The loop must have admitted, and the
/// Service must still serve a further report and registration.
#[test]
fn an_overdue_contributor_renewal_that_lands_late_keeps_the_owners_local_work_service() {
    let mut f = Fixture::new();
    let (mut ports, mut state, mut env) = composed(&mut f);
    let network = ports.work.run_network();
    let slices = ports.slices();
    owner_run::steps::slices::run_slices_step(
        &mut env,
        slices.reports,
        slices.slices,
        &mut state,
        span(BudgetId::OwnerPoll).unwrap().as_secs_f64(),
    )
    .expect("the slices step announced the work");
    let reported = lock(&ports.work.resident)
        .local
        .as_ref()
        .expect("the owner's local work Service runs")
        .report();
    assert!(
        matches!(reported, Ok(Some(_))),
        "the local Service reports in the work phase: {reported:?}"
    );
    let (arrived, go, admitted) = lock(&ports.work.resident)
        .local
        .as_ref()
        .unwrap()
        .hold_next_admission();
    arrived
        .recv_timeout(gate_bound())
        .expect("the local Service's operation loop reached its next admission");
    let (renewal, release) = contributor_renewal(&f, AtPutBack::Hold);
    // Aged without an acquisition of the host: the report tick below is the next one.
    lock(&f.host.run)
        .contributor
        .as_mut()
        .unwrap()
        .flight
        .as_mut()
        .expect("the renewal recorded its flight")
        .deadline = Instant::now();
    ports.slices().reports.tick_local();
    go.send(()).unwrap_or(());
    let admission = admitted
        .recv_timeout(gate_bound())
        .unwrap_or_else(|_| Err("no admission answered".into()));
    // The other overdue reader is unchanged: the registration still refuses, as at the base.
    let registration = ports.work.local(&network);
    release.send(()).unwrap_or(());
    renewal.join().expect("the late renewal pass returned");
    let report = lock(&ports.work.resident)
        .local
        .as_ref()
        .map(crate::packaged_work::Service::report);
    let after = ports.work.local(&network);
    let closed = ports.close_work();
    assert!(
        code(&registration) == "member_work_renewing"
            && admission.is_ok()
            && matches!(report, Some(Ok(Some(_))))
            && code(&after) == "ok",
        "past its deadline the registration answered {} (base: member_work_renewing); the local \
         Service's admission after the report tick answered {admission:?}; after the late put-back \
         its report {report:?} and the registration {} (close {closed:?})",
        code(&registration),
        code(&after)
    );
}

/// Lane FLK patch 2, FLK-02 (joined side): past its deadline the joined Mac's renewal flight no
/// longer lends its snapshot's binding (the next standing pass closes the work service, as the
/// base did without a Session); its late put-back lands and the binding is back.
#[test]
fn a_joined_renewal_past_its_deadline_ends_the_binding() {
    use api::member_agent::MemberHost;
    let mut f = Fixture::new();
    let (mut ports, _state, _env) = composed(&mut f);
    let (renewal, release) = member_renewal(&f, AtPutBack::Hold);
    let during = f.member.accept_context().and_then(|c| c.binding).is_some();
    lock(&f.member_authority)
        .member_flight
        .as_mut()
        .expect("the renewal recorded its flight")
        .deadline = Instant::now();
    let overdue = f.member.accept_context().and_then(|c| c.binding);
    release.send(()).unwrap_or(());
    renewal.join().expect("the late renewal pass returned");
    member_renewal_tail(&f);
    let after = f.member.accept_context().and_then(|c| c.binding).is_some();
    let closed = ports.close_work();
    assert!(
        during && overdue.is_none() && after,
        "binding in flight {during}, past its deadline {overdue:?}, after the late put-back \
         {after} (close {closed:?})"
    );
}

/// What the seed itself recorded of a member's heartbeats (`Coordinator::member`): its last
/// heartbeat sequence, last liveness and lease. Unchanged across a pass = the seed accepted no
/// heartbeat in it.
fn seed_heard(f: &Fixture, node: &str) -> (Value, Value, Value) {
    let seed = f.host.with(|c| c.authority.seed.clone().unwrap());
    let row = seed.coordinator.member(node).unwrap();
    (
        row["last_heartbeat_sequence"].clone(),
        row["last_liveness_at"].clone(),
        row["lease_expires_at"].clone(),
    )
}
/// The wall-clock step that puts a Session's saved lease in the past (one second beyond it).
fn step_past(lease: f64) -> f64 {
    lease - app_host::now() + 1.0
}

/// Lane FLK patch 3, FLK2-02 (owner side). The owner Mac's wall clock steps forward past the
/// contributor's saved lease exactly while the product's renewal (`owner_ports::renew_contributor`)
/// builds its heartbeat: `Session::build_heartbeat` raises `membership_lease_expired` LOCALLY,
/// before any request (`member.rs:549-552`), which translates to `peer_member_expired`. The seed
/// refused nothing, so the Session goes back, as at the base; with the clock correct again the
/// next renewal succeeds and the contributor is current. No request reached the seed: the seed's
/// record is unchanged by the stepped pass, and the next pass's heartbeat is the very next
/// sequence (`Session::heartbeat` gives a sequence back only when the build fails, so the stepped
/// pass built no envelope to send).
#[test]
fn a_contributor_renewal_under_a_clock_stepped_past_its_lease_puts_the_session_back() {
    let mut f = Fixture::new();
    let (mut ports, _state, _env) = composed(&mut f);
    let network = ports.work.run_network();
    let (node, lease, root) = f.host.with(|c| {
        let m = c.run.contributor.as_ref().unwrap();
        (
            m.node.clone(),
            m.session.as_ref().unwrap().lease_expires_at().unwrap(),
            owner_ports::contributor_dir(&c.authority.root),
        )
    });
    let before = seed_heard(&f, &node);
    let stepped = HeldContributor {
        host: f.host.clone(),
        gate: Gate::new(AtPutBack::Pass).0,
        ahead: step_past(lease),
    };
    owner_ports::renew_contributor(&stepped);
    let between = seed_heard(&f, &node);
    let (put_back, refused) = f.host.with(|c| {
        let m = c.run.contributor.as_ref().unwrap();
        (m.session.is_some(), m.refused)
    });
    owner_ports::renew_contributor(&f.host);
    let after = seed_heard(&f, &node);
    let live = f.host.with(|c| {
        let m = c.run.contributor.as_ref().unwrap();
        m.session.as_ref().map(|s| (s.generation(), s.lease_expires_at()))
    });
    let state = api::peer::status_membership(&root, live, app_host::now()).0;
    let local = ports.work.local(&network);
    let closed = ports.close_work();
    assert!(
        put_back
            && refused.is_none()
            && between == before
            && after.0.as_u64() == before.0.as_u64().map(|n| n + 1)
            && state == "current"
            && code(&local) == "ok",
        "after the stepped pass: Session put back {put_back}, refusal recorded {refused:?}, the \
         seed's record {before:?} -> {between:?}; after the next pass: {after:?}, membership \
         {state}, registration {} (close {closed:?})",
        code(&local)
    );
}

/// Lane FLK patch 3, FLK2-02 (joined side): the joined Mac's wall clock steps past its saved lease
/// while the product's renewal (`app_host::renew_member`) builds its heartbeat. The local
/// `membership_lease_expired` is no seed refusal: the Session goes back (the network's tail may
/// close the work on the recorded `expired` disposition, as the base did), no request reached the
/// seed, and with the clock correct again the next renewal (`Network::renew_member`) succeeds, the
/// membership is current and the standing pass binds again.
#[test]
fn a_joined_renewal_under_a_clock_stepped_past_its_lease_puts_the_session_back() {
    use api::member_agent::MemberHost;
    let mut f = Fixture::new();
    let (mut ports, _state, _env) = composed(&mut f);
    let lease = lock(&f.member_authority)
        .session
        .as_ref()
        .unwrap()
        .lease_expires_at()
        .unwrap();
    let before = seed_heard(&f, &f.remote);
    let stepped = HeldMember {
        authority: f.member_authority.clone(),
        network: f.network.clone(),
        gate: Gate::new(AtPutBack::Pass).0,
        ahead: step_past(lease),
    };
    app_host::renew_member(&stepped);
    member_renewal_tail(&f);
    let between = seed_heard(&f, &f.remote);
    let put_back = lock(&f.member_authority).session.is_some();
    f.network.renew_member(&f.member_authority);
    let after = seed_heard(&f, &f.remote);
    let state = crate::packaged_network::membership_state(&f.member_authority);
    let binding = f.member.accept_context().and_then(|c| c.binding).is_some();
    let closed = ports.close_work();
    assert!(
        put_back
            && between == before
            && after.0.as_u64() == before.0.as_u64().map(|n| n + 1)
            && state.as_deref() == Some("current")
            && binding,
        "after the stepped pass: Session put back {put_back}, the seed's record {before:?} -> \
         {between:?}; after the next pass: {after:?}, membership {state:?}, binding {binding} \
         (close {closed:?})"
    );
}

/// Lane FLK patch 2, FLK-02: a late put-back never overwrites a newer state. While a contributor
/// renewal is held out, the contributor is stopped and started again (a new Session) and the new
/// contributor's own renewal takes ITS Session out. The old pass's put-back must not put the old
/// Session into the new contributor (where the new pass's put-back would then find the slot full
/// and drop the renewed one); the new pass lands its own.
#[test]
fn a_late_contributor_put_back_never_overwrites_a_newer_flight() {
    let f = Fixture::new();
    let (old, release_old) = contributor_renewal(&f, AtPutBack::Hold);
    owner_ports::stop_local_contributor(&f.host).unwrap();
    owner_ports::start_local_contributor(&f.host).unwrap();
    let (new, release_new) = contributor_renewal(&f, AtPutBack::Hold);
    let newer = f.host.with(|c| {
        c.run
            .contributor
            .as_ref()
            .and_then(|m| m.flight.as_ref().map(|flight| flight.id))
    });
    release_old.send(()).unwrap_or(());
    old.join().expect("the old renewal pass returned");
    let between = f.host.with(|c| {
        let m = c.run.contributor.as_ref().unwrap();
        (m.session.is_some(), m.flight.as_ref().map(|flight| flight.id))
    });
    release_new.send(()).unwrap_or(());
    new.join().expect("the new renewal pass returned");
    let landed = f.host.with(|c| {
        let m = c.run.contributor.as_ref().unwrap();
        (m.session.is_some(), m.flight.is_none())
    });
    assert!(
        newer.is_some() && between == (false, newer) && landed == (true, true),
        "newer flight {newer:?}; after the old put-back (Session in, flight) {between:?}; after \
         the new one (Session in, no flight) {landed:?}"
    );
}

/// The owner's authority as `app_host::renew_owner` sees it (the owner's renewal loop runs it over
/// the authority's own lock), meeting `gate` on the first acquisition while its `owner_session` is
/// out: its put-back.
struct HeldOwner {
    authority: Arc<Mutex<Authority>>,
    gate: Arc<Gate>,
}
impl HeldOwner {
    fn meet_at_put_back(&self) {
        let out = {
            let a = lock(&self.authority);
            a.owner_session.is_none() && a.seed.is_some()
        };
        if out {
            self.gate.meet();
        }
    }
}
impl app_host::Cell for HeldOwner {
    type Managed = crate::packaged_run::Managed;
    fn with<R>(&self, f: impl FnOnce(&mut app_host::HostView<'_, Self::Managed>) -> R) -> R {
        self.meet_at_put_back();
        app_host::Cell::with(&*self.authority, f)
    }
    fn try_with<R>(
        &self,
        f: impl FnOnce(&mut app_host::HostView<'_, Self::Managed>) -> R,
    ) -> Option<R> {
        self.meet_at_put_back();
        app_host::Cell::try_with(&*self.authority, f)
    }
}

/// Lane FLK patch 2, FLK-03. The owner's authority renews its own membership every 20 s
/// (`app_host::renew_owner` takes `owner_session` out for one heartbeat). The run's reports answer
/// the owner's own node from that slot (`OwnerPorts::own_node_id`), so during the heartbeat they
/// answer none and `exchange::remote_members` no longer excludes the owner's authority row: the
/// descriptors step expects a phantom joined Mac (`descriptors.rs:391`) and a rollover's member
/// check answers `rollover_members_changed` (`exchange.rs:124-151`). With the owner's renewal held
/// at its put-back, the joined Macs are exactly the real one and the rollover binding still holds.
#[test]
fn the_owners_own_node_is_no_joined_mac_while_the_owners_authority_renews() {
    use owner_run::steps::exchange::{check_rollover_members, remote_members};
    let mut f = Fixture::new();
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let state = run_state(&mut f, &ports);
    let local = state.local_node.clone().unwrap();
    let wall = app_host::now();
    let before = remote_members(&mut ports.reports, &local, wall);
    // The rollover binding as the descriptors step records it (`descriptors.rs:503-510`).
    let mut members = Map::new();
    for node in [&local, &f.remote] {
        members.insert(node.clone(), ports.reports.member_identity(node));
    }
    let binding = json!({"members": members});
    let (gate, at_gate, release) = Gate::new(AtPutBack::Hold);
    let held = HeldOwner {
        authority: f.host.authority.clone(),
        gate,
    };
    let renewal = std::thread::spawn(move || app_host::renew_owner(&held));
    at_gate
        .recv_timeout(gate_bound())
        .expect("the owner's renewal took its Session and reached its put-back");
    let during = remote_members(&mut ports.reports, &local, wall);
    let rollover = check_rollover_members(&mut ports.reports, &local, wall, &binding);
    release.send(()).unwrap_or(());
    renewal.join().expect("the owner's renewal pass returned");
    assert!(
        before == vec![f.remote.clone()] && during == before && rollover.is_ok(),
        "joined Macs before {before:?}, during the owner's renewal {during:?}; rollover check \
         {rollover:?}"
    );
}
