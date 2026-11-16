// SPDX-License-Identifier: AGPL-3.0-only
//! Lane CHATPAGE patch 2 (REVIEW-laneCHATPAGE-astra CP-01): Quit must not take the relay away
//! before the run's teardown published `stopped`. v0.1.4's `DemoApp.close` stops and joins the run
//! (`demo_app.close`: `run.stop(); run.join(60)`, demo_app.py:199-203 @ f4e366e9), whose teardown
//! announces `stopped` (demo_chain.py:1381-1390), and closes the public relay only after that join
//! and the steps after it (app.py:1815-1850, relay at :1848-1850). The packaged `AppCleanup::begin`
//! started the relay's close on its own thread at Quit's begin, beside the run's close, so a joined
//! Mac reading the owner's exchange through the relay could never read `stopped`.
//!
//! Model-free: the fixture's two packaged hosts on loopback; the run is a stand-in chain whose
//! model step "ran" (`ctx.state['model']` set, nothing loaded) and whose teardown is the REAL
//! `PackagedChainPorts::teardown_run`, deliberately held until the test releases it. The window's
//! Quit is the composition's own (`packaged::assemble` + `close_run_first`). The joined Mac's
//! standing read is its own signed `RunExchangeClient.read` statement (run_exchange.py:284-321)
//! posted to the relay's loopback proxy, which forwards it over its verified-TLS hop to the owner's
//! seed (the public edge's TLS is Cloudflare's and is not exercised here).
use super::stopped::chosen_model;
use super::*;
use crate::desktop_host::DesktopServices;
use api::{
    actions::Action,
    run_exchange_client::{ExchangeClient, Membership},
    run_exchange_routes::{DISCOVERY, PATH_READ, READ, RESPONSE},
    seed::{SeedClient, SeedOptions},
};
use desktop::{
    lifecycle::{Cleanup as _, Poll},
    services::{ServiceRequest, Services as _},
};
use owner_run::{action::DemoFacts, owner_chain::ChainPorts as _};
use std::{num::NonZeroUsize, sync::mpsc};

fn patience() -> Duration {
    span(BudgetId::WorkerStartup).unwrap()
}
struct Facts;
impl DemoFacts for Facts {
    fn owner(&self) -> bool {
        true
    }
    fn ensure_exchange(&mut self) -> bool {
        true
    }
    fn members_joined_without_relay(&self) -> Option<u64> {
        Some(0)
    }
}

/// A run over the fixture's two Macs whose model step ran: every step answers `Ok` (`serving`
/// announces `serving` as the serving step does), the teardown is the REAL packaged one over
/// `ctx.state`, held until the test releases it (bounded by the worker startup patience).
struct Announcing {
    ports: PackagedChainPorts,
    nodes: (String, String),
    state: Option<RunState>,
    torn: mpsc::Sender<()>,
    release: mpsc::Receiver<()>,
}
impl Chain for Announcing {
    fn new_run(
        &mut self,
        run: &str,
        deployment: &str,
        reason: owner_run::Reason,
        _: Option<&Value>,
        _: Option<&Value>,
    ) {
        let seed = self
            .ports
            .work
            .host()
            .with(|c| c.authority.seed.clone().unwrap());
        seed.exchange
            .set_owner_control(Some(OwnerControl::new(self.ports.work.owner.public())));
        let (local, remote) = self.nodes.clone();
        let mut state = RunState::new(run, deployment, reason);
        state.participants = vec![local.clone(), remote];
        state.local_node = Some(local);
        state.model = Some(chosen_model());
        self.state = Some(state);
    }
    fn run_step(&mut self, step: &str, _: &mut dyn StepEnv) -> Result<(), StepError> {
        if step == "serving" {
            let state = self.state.as_ref().expect("new_run ran");
            owner_run::steps::exchange::announce(&mut self.ports.reports, state, "serving", None)?;
        }
        Ok(())
    }
    fn teardown(&mut self, failed: bool, env: &mut dyn StepEnv) -> Result<(), String> {
        self.torn.send(()).unwrap_or_default();
        // The deliberately delayed teardown: chats, the route's close, then `stopped`.
        let _ = self.release.recv_timeout(patience());
        let state = self.state.as_ref().expect("new_run ran");
        self.ports.teardown_run(failed, state, env)
    }
}

/// The joined Mac (Mac B): its own exchange client over its membership, posting its signed
/// `read()` statement to the owner's relay instead of dialing the seed.
pub(super) struct MacB {
    client: ExchangeClient,
}
impl MacB {
    pub(super) fn new(root: &Path) -> Self {
        let m = api::peer::load_membership_state(root)
            .unwrap()
            .expect("Mac B is joined");
        let text = |k: &str| m[k].as_str().unwrap().to_owned();
        let records = m["seed_key_records"].as_array().unwrap().clone();
        let seed = SeedClient::new(
            &text("seed_url"),
            &text("swarm_id"),
            &text("seed_key_digest"),
            &records,
            &SeedOptions::default(),
        )
        .unwrap();
        let (signer, _, node) = app_host::identity(root).unwrap();
        let member = Membership {
            swarm_id: text("swarm_id"),
            owner_node_id: text("seed_node_id"),
            owner_key: records[0].clone(),
            node_id: node,
            generation: m["membership_generation"].as_u64().unwrap(),
            incarnation: text("incarnation"),
        };
        Self {
            client: ExchangeClient::new(
                seed,
                Arc::new(signer),
                member,
                Box::new(app_host::now),
                Box::new(|| app_host::random(16).ok().map(|b| app_host::hex(&b))),
            ),
        }
    }
    /// `read()` through the relay's loopback proxy: the owner's verified announcement, else why
    /// not (nothing listens any more, or the relay's answer).
    pub(super) fn read(&self, port: u16) -> Result<Value, String> {
        use std::io::{Read, Write};
        let c = &self.client;
        let request_id = c.nonce().unwrap();
        let now = c.now();
        let ttl = span(BudgetId::ControlTtl).unwrap().as_secs_f64();
        let mut statement = Value::Object(c.binding(DISCOVERY, DISCOVERY));
        for (k, v) in [
            ("protocol", json!(READ)),
            ("swarm_id", json!(c.member().swarm_id)),
            ("request_id", json!(request_id)),
            ("sequence", json!(1)),
            ("nonce", json!(c.nonce().unwrap())),
            ("issued_at", json!(now)),
            ("expires_at", json!(now + ttl)),
        ] {
            statement[k] = v;
        }
        let body = protocol::canonical::to_bytes(&c.sign(&statement).unwrap()).unwrap();
        let mut stream = std::net::TcpStream::connect(("127.0.0.1", port))
            .map_err(|e| format!("relay_unreachable:{:?}", e.kind()))?;
        stream.set_read_timeout(Some(patience())).unwrap();
        let head = format!(
            "POST {PATH_READ} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n",
            body.len()
        );
        let mut raw = Vec::new();
        stream
            .write_all(head.as_bytes())
            .and_then(|()| stream.write_all(&body))
            .and_then(|()| stream.read_to_end(&mut raw).map(drop))
            .map_err(|e| format!("relay_io:{:?}", e.kind()))?;
        let split = raw
            .windows(4)
            .position(|w| w == b"\r\n\r\n")
            .ok_or_else(|| format!("relay_reply_invalid:{}", String::from_utf8_lossy(&raw)))?;
        let head = String::from_utf8_lossy(&raw[..split]);
        let answer: Value = serde_json::from_slice(&raw[split + 4..]).unwrap_or(Value::Null);
        if head.split(' ').nth(1) != Some("200") {
            return Err(format!("relay_answer:{head}:{answer}"));
        }
        let verified = c
            .verified(&answer, RESPONSE, &request_id, DISCOVERY, DISCOVERY)
            .map_err(|e| format!("answer_unverified:{}", e.0))?;
        Ok(verified["announcement"].clone())
    }
}

/// The fixture, a run of [`Announcing`] serving, the window's services with a relay on a loopback
/// proxy port fronting the owner's seed, and Mac B having read `serving` through that relay.
struct Served {
    run_id: String,
    port: u16,
    services: DesktopServices,
    config: RunConfig,
    torn: mpsc::Receiver<()>,
    release: mpsc::Sender<()>,
    mac_b: MacB,
    _run: OwnerRun,
    f: Fixture,
}
fn served() -> Served {
    let f = Fixture::new();
    let (torn_tx, torn) = mpsc::channel();
    let (release, release_rx) = mpsc::channel();
    let local = f
        .host
        .with(|c| c.run.contributor.as_ref().unwrap().node.clone());
    let mut chain: Option<Box<dyn Chain>> = Some(Box::new(Announcing {
        ports: PackagedChainPorts::new(&f.host, None, None),
        nodes: (local, f.remote.clone()),
        state: None,
        torn: torn_tx,
        release: release_rx,
    }));
    let config = RunConfig::default();
    let run = OwnerRun::install_with(
        f.host.clone(),
        &config,
        None,
        Box::new(move || chain.take().expect("one run")),
    );
    let dirs = crate::packaged::prepare_dirs(&f.root.join("window")).unwrap();
    let node = NodeService::new(
        Arc::new(NoNative),
        "quit-stopped-window".into(),
        crate::native::development_qualification().unwrap(),
    );
    api::actions::Backend::publish(&mut node.clone()).unwrap();
    let services = DesktopServices::new(node, NonZeroUsize::new(4).unwrap(), &dirs.private);
    let port = std::net::TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap();
    let (tx, rx) = mpsc::sync_channel(1);
    services.submit(
        ServiceRequest {
            method: "POST".into(),
            target: "/api/network/start".into(),
            headers: vec![
                ("x-mycelium-action".into(), "1".into()),
                ("content-type".into(), "application/json".into()),
            ],
            body: serde_json::to_vec(&json!({"relay_acknowledged": true, "max_seconds": 1800,
                "provider": "external", "public_origin": "https://v02-quit-stopped.example.org",
                "proxy_port": port}))
            .unwrap(),
        },
        Box::new(move |reply| tx.send(reply).unwrap_or_default()),
    );
    let started = rx
        .recv_timeout(patience())
        .expect("the relay start answers");
    assert_eq!(
        started.status,
        200,
        "{}",
        String::from_utf8_lossy(&started.body)
    );
    // In the packaged composition the window's relay fronts the run's own host's seed
    // (`Network::bind_seed`, app.py:1612-1613); here that seed is the fixture owner's.
    services.network().bind_seed(&f.host.authority);
    lock(&run.runs)
        .demo_action(Action::DemoStart, &Map::new(), &mut Facts)
        .expect("start");
    let end = Instant::now() + patience();
    let status = loop {
        let status = config
            .observed()
            .and_then(|view| view.status())
            .map_or(Value::Null, |s| s.to_value());
        if status["state"] == "serving" {
            break status;
        }
        assert!(Instant::now() < end, "the run never served: {status}");
        std::thread::sleep(Duration::from_millis(10));
    };
    let run_id = status["run_id"].as_str().unwrap().to_owned();
    let mac_b = MacB::new(&f.root.join("member"));
    let read = mac_b.read(port).expect("Mac B reads through the relay");
    assert_eq!(
        (&read["phase"], &read["run_id"]),
        (&json!("serving"), &json!(run_id)),
        "{read}"
    );
    Served {
        run_id,
        port,
        services,
        config,
        torn,
        release,
        mac_b,
        _run: run,
        f,
    }
}
/// The window's Quit over the served run, polled to its verdict on its own thread (as the shell's
/// timer polls it), bounded by the Quit allowance: `(verdict, took)`.
fn quit(
    services: DesktopServices,
    config: &RunConfig,
    f: &Fixture,
) -> mpsc::Receiver<(Result<Poll, desktop::lifecycle::CleanupError>, Duration)> {
    let (_core, mut cleanup) = crate::packaged::assemble(
        services,
        crate::app_host::ModelSource::hub(f.root.join("model-sources")),
    );
    crate::packaged::close_run_first(&mut cleanup, config.clone());
    let allowance = desktop::lifecycle::quit_allowance().unwrap();
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let _core = _core;
        let began = Instant::now();
        cleanup.bind_clock(began).unwrap();
        cleanup.begin(allowance).unwrap();
        let deadline = began + Duration::from_millis(allowance.millis());
        let verdict = loop {
            match cleanup.poll() {
                Ok(Poll::Pending) if Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(20))
                }
                other => break other,
            }
        };
        tx.send((verdict, began.elapsed())).unwrap_or_default();
    });
    rx
}

/// Proves: CP-01 (Quit publishes `stopped` before the relay closes, and a joined Mac reading the
/// owner's exchange through the relay reads it). The run's teardown is held: while it is, the
/// relay still answers Mac B (Quit did not remove the transport ahead of the announcement); once
/// it is released, Mac B, polling through the relay as its standing loop does, reads `stopped`
/// for the run before the relay goes; Quit then ends `Verified` within its allowance, the relay
/// closed.
#[test]
fn quit_publishes_stopped_to_a_mac_reading_through_the_relay_before_the_relay_closes() {
    let s = served();
    let allowance = desktop::lifecycle::quit_allowance().unwrap();
    let verdict = quit(s.services, &s.config, &s.f);
    s.torn
        .recv_timeout(patience())
        .expect("Quit stops the run: its teardown begins");
    // The teardown is held: nothing is announced yet. Mac B keeps reading through the relay.
    let poll = span(BudgetId::OwnerPoll).unwrap();
    let mut held = Vec::new();
    for _ in 0..10 {
        held.push(s.mac_b.read(s.port).map(|a| a["phase"].clone()));
        std::thread::sleep(poll / 10);
    }
    s.release.send(()).unwrap_or_default();
    assert!(
        held.iter().all(|r| *r == Ok(json!("serving"))),
        "the relay was closed before the run's teardown announced stopped: {held:?}"
    );
    // Mac B polls through the relay (its standing loop's read) until the relay is gone.
    let end = Instant::now() + patience();
    let mut reads = Vec::new();
    let gone = loop {
        match s.mac_b.read(s.port) {
            Ok(a) => reads.push((a["phase"].clone(), a["run_id"].clone())),
            Err(why) => break why,
        }
        assert!(Instant::now() < end, "the relay never closed: {reads:?}");
        std::thread::sleep(poll / 10);
    };
    assert_eq!(
        reads.last(),
        Some(&(json!("stopped"), json!(s.run_id))),
        "Mac B never read stopped through the relay before it closed ({gone}): {reads:?}"
    );
    let (verdict, took) = verdict
        .recv_timeout(Duration::from_millis(allowance.millis()) + patience())
        .expect("Quit ends");
    eprintln!(
        "CHATPAGE_P2_QUIT_STOPPED_THROUGH_RELAY verdict={verdict:?} took_ms={} reads={}",
        took.as_millis(),
        reads.len()
    );
    assert_eq!(verdict, Ok(Poll::Verified), "Quit after {took:?}");
    assert!(took < Duration::from_millis(allowance.millis()), "{took:?}");
    assert!(
        std::net::TcpStream::connect(("127.0.0.1", s.port)).is_err(),
        "Quit left the relay listening"
    );
}

/// Proves: CP-01's read chance (Mac B at its own cadence). A joined Mac does not poll at once:
/// its standing loop waits 2 s between passes (`self.closed.wait(2)`, app.py:1203 @ f4e366e9).
/// Here Mac B read `serving` through the relay while the teardown was held, then, the teardown
/// released, waits until the owner's run reads `stopped` and one standing-loop wait more before
/// its next read: the relay must still carry that read, and it reads `stopped`. A relay closed as
/// soon as the run's close ended would be gone by then.
#[test]
fn a_mac_at_its_standing_cadence_reads_stopped_before_the_relay_closes() {
    let s = served();
    let allowance = desktop::lifecycle::quit_allowance().unwrap();
    let verdict = quit(s.services, &s.config, &s.f);
    s.torn
        .recv_timeout(patience())
        .expect("Quit stops the run: its teardown begins");
    let held = s.mac_b.read(s.port).map(|a| a["phase"].clone());
    s.release.send(()).unwrap_or_default();
    assert_eq!(
        held,
        Ok(json!("serving")),
        "the relay closed during the teardown"
    );
    let end = Instant::now() + patience();
    loop {
        let status = s
            .config
            .observed()
            .and_then(|view| view.status())
            .map_or(Value::Null, |s| s.to_value());
        if status["state"] == "stopped" {
            break;
        }
        assert!(Instant::now() < end, "the run never stopped: {status}");
        std::thread::sleep(Duration::from_millis(10));
    }
    std::thread::sleep(
        span(BudgetId::PyMyceliumA13MacosAppDemoappStandingLoopSelfClosedWaitL1203).unwrap(),
    );
    let read = s.mac_b.read(s.port);
    let (verdict, took) = verdict
        .recv_timeout(Duration::from_millis(allowance.millis()) + patience())
        .expect("Quit ends");
    eprintln!(
        "CHATPAGE_P2_QUIT_STANDING_CADENCE verdict={verdict:?} took_ms={} read={read:?}",
        took.as_millis()
    );
    let read = read.expect("the relay still carried Mac B's next standing read");
    assert_eq!(
        (&read["phase"], &read["run_id"]),
        (&json!("stopped"), &json!(s.run_id)),
        "{read}"
    );
    assert_eq!(verdict, Ok(Poll::Verified), "Quit after {took:?}");
    assert!(
        took < Duration::from_millis(allowance.millis() / 2),
        "{took:?}"
    );
    assert!(
        std::net::TcpStream::connect(("127.0.0.1", s.port)).is_err(),
        "Quit left the relay listening"
    );
}

/// Proves: CP-01's bound (Quit stays bounded when Mac B is absent). Mac B read through the relay,
/// then stopped polling (it quit, or its network went): the run's teardown announces `stopped`,
/// nobody reads it, and Quit still ends `Verified` well within its allowance, the relay closed.
#[test]
fn quit_with_the_joined_mac_gone_still_ends_within_its_allowance() {
    let s = served();
    let allowance = desktop::lifecycle::quit_allowance().unwrap();
    let verdict = quit(s.services, &s.config, &s.f);
    s.torn
        .recv_timeout(patience())
        .expect("Quit stops the run: its teardown begins");
    s.release.send(()).unwrap_or_default();
    let (verdict, took) = verdict
        .recv_timeout(Duration::from_millis(allowance.millis()) + patience())
        .expect("Quit ends");
    eprintln!(
        "CHATPAGE_P2_QUIT_MAC_B_GONE verdict={verdict:?} took_ms={}",
        took.as_millis()
    );
    assert_eq!(verdict, Ok(Poll::Verified), "Quit after {took:?}");
    assert!(
        took < Duration::from_millis(allowance.millis() / 2),
        "Quit waited on an absent Mac: {took:?}"
    );
    assert!(
        std::net::TcpStream::connect(("127.0.0.1", s.port)).is_err(),
        "Quit left the relay listening"
    );
    let seed = s.f.host.with(|c| c.authority.seed.clone().unwrap());
    let announced = seed.exchange.announcement(None).to_value();
    assert_eq!(
        (&announced["phase"], &announced["run_id"]),
        (&json!("stopped"), &json!(s.run_id)),
        "{announced}"
    );
}
