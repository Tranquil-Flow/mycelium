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
    (ports, state, env)
}

/// The packaged run's reports, except that its work announcement runs while the owner's own
/// contributor is in the middle of a renewal: exactly the state `owner_ports::renew_contributor`
/// holds for the length of one seed heartbeat (`owner_ports.rs:299` takes the Session out,
/// `:319-325` puts it back after `peer::renew`). The hold is released after the announcement
/// returned, whatever it answered.
struct RenewalInFlight<'a> {
    inner: &'a mut dyn ReportsPort,
    host: PackagedRun,
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
        let session = self.host.with(|c| {
            c.run
                .contributor
                .as_mut()
                .expect("the contributor step enrolled the owner's own worker")
                .session
                .take()
        });
        assert!(session.is_some(), "a live Session to hold");
        let answer = self.inner.announce_work(a, limits);
        self.host.with(|c| {
            let member = c.run.contributor.as_mut().expect("still enrolled");
            assert!(member.session.is_none(), "nothing else put a Session back");
            member.session = session;
        });
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
    let host = f.host.clone();
    let slices = ports.slices();
    let mut reports = RenewalInFlight {
        inner: slices.reports,
        host,
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
/// the real `owner_ports::renew_contributor` back to back (the product runs it every 20 s) while
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
