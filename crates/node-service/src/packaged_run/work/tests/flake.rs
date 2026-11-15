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
            drop(entered.send(()));
        }
        match self.mode {
            AtPutBack::Hold => {
                if let Some(released) = lock(&self.release).take() {
                    // Err only when the test dropped its sender: proceed either way.
                    drop(released.recv());
                }
            }
            AtPutBack::Panic => panic!("lane FLK: the renewal thread dies between take and put-back"),
        }
    }
}
/// The bound a test waits for a renewal thread to reach its gate (failure detection only, never
/// a release): one renewal pass on the wire (`Network::await_renewal_pass`).
fn gate_bound() -> Duration {
    span(BudgetId::PyMyceliumA13MacosPeerRemotemacmemberInitTimeoutL117).unwrap() * 2
}
/// The owner's host as `owner_ports::renew_contributor` sees it, meeting `gate` on the first state
/// acquisition while the contributor's Session is out: its put-back.
#[derive(Clone)]
struct HeldContributor {
    host: PackagedRun,
    gate: Arc<Gate>,
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
}
/// The owner's contributor renewal (`owner_ports::renew_contributor`, the product's pass) on its
/// own thread, stopped at its put-back as `mode` says; returns once it is there.
fn contributor_renewal(
    f: &Fixture,
    mode: AtPutBack,
) -> (std::thread::JoinHandle<()>, std::sync::mpsc::Sender<()>) {
    let (gate, at_gate, release) = Gate::new(mode);
    let held = HeldContributor {
        host: f.host.clone(),
        gate,
    };
    let renewal = std::thread::spawn(move || owner_ports::renew_contributor(&held));
    at_gate
        .recv_timeout(gate_bound())
        .expect("the contributor renewal took its Session and reached its put-back");
    (renewal, release)
}
/// The joined Mac's authority as `app_host::renew_member` sees it, meeting `gate` on the first
/// state acquisition while its Session is out for the renewal: its put-back.
struct HeldMember {
    authority: Arc<Mutex<Authority>>,
    network: Arc<Network>,
    gate: Arc<Gate>,
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
}
/// The joined Mac's renewal (`app_host::renew_member`, the pass `Network::renew_member` runs) on its
/// own thread, stopped at its put-back as `mode` says; returns once it is there.
fn member_renewal(
    f: &Fixture,
    mode: AtPutBack,
) -> (std::thread::JoinHandle<()>, std::sync::mpsc::Sender<()>) {
    let (gate, at_gate, release) = Gate::new(mode);
    let held = HeldMember {
        authority: f.member_authority.clone(),
        network: f.network.clone(),
        gate,
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
        drop(release.send(()));
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
    drop(release.send(()));
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
    let started: Arc<Mutex<Option<(std::thread::JoinHandle<()>, std::sync::mpsc::Sender<()>)>>> =
        Arc::default();
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
    drop(release.send(()));
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
