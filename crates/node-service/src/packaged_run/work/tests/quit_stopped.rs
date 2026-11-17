// SPDX-License-Identifier: AGPL-3.0-only
//! Lane CHATPAGE (REVIEW-laneCHATPAGE-astra CP-01, REVIEW-laneCHATPAGE-p2-astra CP2-01/CP2-02):
//! Quit closes the run FIRST and the relay AFTER, as v0.1.4 does. `DemoApp.close` stops and joins
//! the run (`demo_app.close`: `run.stop(); run.join(60)`, demo_app.py:199-203 @ f4e366e9), whose
//! teardown announces `stopped` (demo_chain.py:1381-1390), and only after that join (and the
//! workflow Stop, app.py:1835-1838) runs `public_relay.close()` (app.py:1848-1850). The relay
//! therefore waits for the run's close with the run's whole allowance (the 60 s Quit allowance),
//! never less, and then closes; whether a joined Mac reads `stopped` before that is best effort,
//! as in v0.1.4 (no wait for any Mac, no reply counted as a receipt).
//!
//! Model-free: the fixture's two packaged hosts on loopback; the run is a stand-in chain whose
//! model step "ran" (`ctx.state['model']` set, nothing loaded) and whose teardown is the REAL
//! `PackagedChainPorts::teardown_run`, held twice by the test (before it and after it published).
//! The window's Quit is the composition's own (`packaged::assemble` + `close_run_first`) driven by
//! the shell's own `desktop::lifecycle::Shutdown`; a Quit "requested 45 s ago" is the cleanup's
//! clock origin bound in the past (no real wait). The joined Mac's standing read is its own signed
//! `RunExchangeClient.read` statement (run_exchange.py:284-321) posted to the relay's loopback
//! proxy, which forwards it over its verified-TLS hop to the owner's seed (the public edge's TLS is
//! Cloudflare's and is not exercised here).
use super::stopped::chosen_model;
use super::*;
use crate::desktop_host::DesktopServices;
use api::{
    actions::Action,
    run_exchange_client::{ExchangeClient, Membership},
    run_exchange_routes::{DISCOVERY, PATH_READ, READ, RESPONSE},
    seed::{SeedClient, SeedOptions},
};
use budgets::Span;
use desktop::{
    lifecycle::{Cleanup as _, QuitSource, Shutdown, State},
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
/// `ctx.state`, held until the test releases it, and held again once it published (each hold
/// bounded by the worker startup patience; a dropped sender releases it at once).
struct Announcing {
    ports: PackagedChainPorts,
    nodes: (String, String),
    state: Option<RunState>,
    torn: mpsc::Sender<()>,
    release: mpsc::Receiver<()>,
    published: mpsc::Sender<()>,
    finish: mpsc::Receiver<()>,
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
        self.release.recv_timeout(patience()).ok();
        let state = self.state.as_ref().expect("new_run ran");
        let torn = self.ports.teardown_run(failed, state, env);
        // `stopped` is published; the run's close has not ended while this teardown runs.
        self.published.send(()).unwrap_or_default();
        self.finish.recv_timeout(patience()).ok();
        torn
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
    published: mpsc::Receiver<()>,
    finish: mpsc::Sender<()>,
    mac_b: MacB,
    _run: OwnerRun,
    f: Fixture,
}
fn served() -> Served {
    let f = Fixture::new();
    let (torn_tx, torn) = mpsc::channel();
    let (release, release_rx) = mpsc::channel();
    let (published_tx, published) = mpsc::channel();
    let (finish, finish_rx) = mpsc::channel();
    let local = f
        .host
        .with(|c| c.run.contributor.as_ref().unwrap().node.clone());
    let mut chain: Option<Box<dyn Chain>> = Some(Box::new(Announcing {
        ports: PackagedChainPorts::new(&f.host, None, None),
        nodes: (local, f.remote.clone()),
        state: None,
        torn: torn_tx,
        release: release_rx,
        published: published_tx,
        finish: finish_rx,
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
        published,
        finish,
        mac_b,
        _run: run,
        f,
    }
}
/// What the shell's Quit ended with: its state, its exit status and the real time it took.
type Verdict = (State, Option<i32>, Duration);
/// The window's Quit over the served run as the shell runs it: `Shutdown` over the composition's
/// cleanup, requested `into` the allowance ago on the shell's clock (the cleanup's clock origin is
/// bound that far in the past: no real wait), ticked on its own thread every 20 ms until it has a
/// result (`expire` at the deadline, as the shell does), bounded by twice the allowance.
fn quit(
    services: DesktopServices,
    config: &RunConfig,
    f: &Fixture,
    into: Duration,
) -> mpsc::Receiver<Verdict> {
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
        let origin = began
            .checked_sub(into)
            .expect("the monotonic clock reaches that far back");
        cleanup.bind_clock(origin).unwrap();
        let mut shutdown = Shutdown::new(cleanup, allowance).unwrap();
        let shell_now = || Span::from_millis(u64::try_from(origin.elapsed().as_millis()).unwrap());
        shutdown.request(QuitSource::Menu, Span::ZERO);
        let limit = Duration::from_millis(allowance.millis()) * 2;
        let state = loop {
            let state = shutdown.tick(shell_now());
            if state != State::Stopping || began.elapsed() > limit {
                break state;
            }
            std::thread::sleep(Duration::from_millis(20));
        };
        tx.send((state, shutdown.exit_status(), began.elapsed()))
            .unwrap_or_default();
    });
    rx
}
fn relay_listens(port: u16) -> bool {
    std::net::TcpStream::connect(("127.0.0.1", port)).is_ok()
}

/// Quit requested `into` its allowance ago: while the run's teardown is held (nothing announced
/// yet) the relay still carries Mac B's reads; once the teardown published `stopped` (held again
/// there: the run's close has not ended), Mac B reading through the relay reads `stopped` for the
/// run; the teardown ends, Quit ends `Verified` within its allowance and the relay is closed.
fn stopped_is_published_while_the_relay_is_up(into: Duration, label: &str) {
    let s = served();
    let allowance = Duration::from_millis(desktop::lifecycle::quit_allowance().unwrap().millis());
    let verdict = quit(s.services, &s.config, &s.f, into);
    s.torn
        .recv_timeout(patience())
        .expect("Quit stops the run: its teardown begins");
    // The teardown is held: nothing is announced yet. Mac B keeps reading through the relay.
    let poll = span(BudgetId::OwnerPoll).unwrap();
    let mut held = Vec::new();
    for _ in 0..5 {
        held.push(s.mac_b.read(s.port).map(|a| a["phase"].clone()));
        std::thread::sleep(poll / 10);
    }
    s.release.send(()).unwrap_or_default();
    s.published
        .recv_timeout(patience())
        .expect("the teardown publishes stopped");
    // Published; the teardown (so the run's close) has not ended: the relay must still be up.
    let read = s.mac_b.read(s.port);
    s.finish.send(()).unwrap_or_default();
    let (state, exit, took) = verdict
        .recv_timeout(allowance * 2 + patience())
        .expect("Quit ends");
    eprintln!(
        "{label} state={state:?} exit={exit:?} took_ms={} held={held:?} read={read:?}",
        took.as_millis()
    );
    assert!(
        held.iter().all(|r| *r == Ok(json!("serving"))),
        "the relay was closed before the run's teardown announced stopped: {held:?}"
    );
    let read = read.expect("the relay was closed before the run's close ended");
    assert_eq!(
        (&read["phase"], &read["run_id"]),
        (&json!("stopped"), &json!(s.run_id)),
        "{read}"
    );
    assert_eq!(
        (state, exit),
        (State::Verified, Some(0)),
        "Quit after {took:?}"
    );
    assert!(took < allowance - into, "{took:?}");
    assert!(!relay_listens(s.port), "Quit left the relay listening");
}

/// Proves: CP-01 (Quit publishes `stopped` before the relay closes, and a joined Mac reading the
/// owner's exchange through the relay after the teardown published it and before the relay closed
/// reads it). Quit requested now.
#[test]
fn quit_publishes_stopped_to_a_mac_reading_through_the_relay_before_the_relay_closes() {
    stopped_is_published_while_the_relay_is_up(
        Duration::ZERO,
        "CHATPAGE_P3_QUIT_STOPPED_THROUGH_RELAY",
    );
}

/// Proves: CP2-02 (no relay reserve cuts the run's allowance short). The same Quit, but its
/// teardown runs 45 s into the 60 s allowance (3/4 of it): patch 2 started the relay's close at
/// the deadline minus the relay's own close budgets (20 s, so at 40 s) whatever the run still did,
/// so a teardown slower than that published `stopped` with the relay already gone. The run's join
/// keeps its whole allowance and the relay waits for it.
#[test]
fn a_teardown_slower_than_the_relay_reserve_still_publishes_stopped_while_the_relay_is_up() {
    let allowance = Duration::from_millis(desktop::lifecycle::quit_allowance().unwrap().millis());
    stopped_is_published_while_the_relay_is_up(
        allowance * 3 / 4,
        "CHATPAGE_P3_QUIT_STOPPED_PAST_RESERVE",
    );
}

/// Proves: CP2-02's bound (the run's allowance runs out). Quit requested 3 s before its deadline,
/// and the run's teardown never gets past its first hold: until the deadline the relay still
/// carries Mac B's reads (it waits for the run's join); at the deadline the run's join gives up
/// (v0.1.4 `run.join(60)` returns, app.py:1835-1838 stops the workflow) and the relay closes
/// (app.py:1848-1850) although nothing announced `stopped` (best effort, as in v0.1.4); the Quit
/// ends at the deadline (exit 0, `Deadline`), bounded by the relay's own close budgets after it.
#[test]
fn at_the_quit_deadline_the_relay_closes_although_the_runs_teardown_still_runs() {
    let s = served();
    let allowance = Duration::from_millis(desktop::lifecycle::quit_allowance().unwrap().millis());
    let left = Duration::from_secs(3);
    let verdict = quit(s.services, &s.config, &s.f, allowance - left);
    s.torn
        .recv_timeout(patience())
        .expect("Quit stops the run: its teardown begins");
    let before = s.mac_b.read(s.port).map(|a| a["phase"].clone());
    let (state, exit, took) = verdict
        .recv_timeout(allowance * 2 + patience())
        .expect("Quit ends");
    let listens = relay_listens(s.port);
    let seed = s.f.host.with(|c| c.authority.seed.clone().unwrap());
    let announced = seed.exchange.announcement(None).to_value();
    eprintln!(
        "CHATPAGE_P3_QUIT_DEADLINE state={state:?} exit={exit:?} took_ms={} before={before:?} announced={}",
        took.as_millis(),
        announced["phase"]
    );
    assert_eq!(
        before,
        Ok(json!("serving")),
        "the relay was closed before the run's join allowance ran out"
    );
    assert_eq!(
        (state, exit),
        (
            State::Failed(desktop::lifecycle::CleanupError::Deadline),
            Some(0)
        ),
        "Quit after {took:?}"
    );
    assert!(took >= left, "Quit ended before its deadline: {took:?}");
    assert!(took < left + patience(), "{took:?}");
    assert!(!listens, "the relay outlived the Quit deadline");
    assert_eq!(announced["phase"], json!("serving"), "{announced}");
}

/// Proves: CP-01's bound (Quit stays bounded when Mac B is absent). Mac B read through the relay,
/// then stopped polling (it quit, or its network went): the run's teardown announces `stopped`,
/// nobody reads it, and Quit still ends `Verified` well within its allowance, the relay closed.
#[test]
fn quit_with_the_joined_mac_gone_still_ends_within_its_allowance() {
    let s = served();
    let allowance = Duration::from_millis(desktop::lifecycle::quit_allowance().unwrap().millis());
    let verdict = quit(s.services, &s.config, &s.f, Duration::ZERO);
    s.torn
        .recv_timeout(patience())
        .expect("Quit stops the run: its teardown begins");
    s.release.send(()).unwrap_or_default();
    s.finish.send(()).unwrap_or_default();
    let (state, exit, took) = verdict
        .recv_timeout(allowance * 2 + patience())
        .expect("Quit ends");
    eprintln!(
        "CHATPAGE_P3_QUIT_MAC_B_GONE state={state:?} exit={exit:?} took_ms={}",
        took.as_millis()
    );
    assert_eq!(
        (state, exit),
        (State::Verified, Some(0)),
        "Quit after {took:?}"
    );
    assert!(
        took < allowance / 2,
        "Quit waited on an absent Mac: {took:?}"
    );
    assert!(!relay_listens(s.port), "Quit left the relay listening");
    let seed = s.f.host.with(|c| c.authority.seed.clone().unwrap());
    let announced = seed.exchange.announcement(None).to_value();
    assert_eq!(
        (&announced["phase"], &announced["run_id"]),
        (&json!("stopped"), &json!(s.run_id)),
        "{announced}"
    );
}
