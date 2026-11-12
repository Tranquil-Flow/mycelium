// SPDX-License-Identifier: AGPL-3.0-only
//! Lane GR: the headless launcher's public relay window renews while it serves, as
//! `DemoApp._relay_is_serving` (`app.py:769-776` @ f4e366e9) decides: an active owner run or an
//! enabled swarm listing. The relay's lifetime step (`public_relay.py:387-407`) is driven by the
//! test on an injected clock; no cloudflared is started (external provider: proxy only).
use super::*;
use api::{
    relay::Hooks,
    swarm::publisher::{DirectoryPort, Error as DirectoryError, app::AppPublisher},
};
use std::sync::atomic::{AtomicU64, Ordering};

struct Root(PathBuf);
impl Root {
    fn new(tag: &str) -> Self {
        let path = std::env::temp_dir().join(format!("gr-relay-{tag}-{}", std::process::id()));
        assert!(!path.exists(), "fresh fixture root required");
        Self(path)
    }
}
impl Drop for Root {
    fn drop(&mut self) {
        if !std::thread::panicking() {
            fs::remove_dir_all(&self.0).expect("remove owned fixture root");
        }
    }
}
/// A directory that is never reached: publishing the opt-in is local work.
struct Unreached;
impl DirectoryPort for Unreached {
    fn call(&self, _method: &str, _path: &str, _body: &[u8]) -> Result<Value, DirectoryError> {
        Err(DirectoryError::new("swarm_directory_unreachable"))
    }
}
fn allowance() -> Duration {
    span(BudgetId::RouterClose)
}
/// An external relay on `clock` (milliseconds), stepped by the test only.
fn start(
    core: &Shared,
    root: &Path,
    clock: &Arc<AtomicU64>,
    cap: Option<u64>,
) -> api::relay::Lifetime {
    let hooks_clock = clock.clone();
    let relay = OwnerRelay::new(root, move || {
        let clock = hooks_clock.clone();
        Hooks {
            clock: Arc::new(move || clock.load(Ordering::SeqCst) as f64 / 1000.0),
            lifetime_poll: None,
            ready_wait: span(BudgetId::RouterClose),
            binary: Arc::new(|| None),
            env: Vec::new(),
            release_observer: None,
        }
    });
    core.lock().unwrap().relay = relay;
    let port = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
        .unwrap()
        .local_addr()
        .unwrap()
        .port();
    let mut body = json!({"provider":"external","public_origin":"https://gr-relay.mycelium.now",
        "proxy_port":port,"relay_acknowledged":true,"max_seconds":WINDOW});
    if let Some(cap) = cap {
        body["max_renewed_seconds"] = json!(cap);
    }
    let started = start_relay(core, body.as_object().unwrap()).expect("external relay starts");
    assert_eq!(started.to_value()["renewals"], 0);
    core.lock()
        .unwrap()
        .relay
        .lifetime()
        .expect("a running relay")
}
const WINDOW: u64 = 30;
fn advance(clock: &AtomicU64, seconds: u64) {
    clock.fetch_add(seconds * 1000, Ordering::SeqCst);
}
fn core_with_listing(root: &Path, listed: bool) -> Shared {
    let mut loaded = load_core(root.to_path_buf()).expect("load actual Core");
    let publisher = AppPublisher::with_connect(
        &loaded.root,
        Box::new(|_origin: &str| Ok(Arc::new(Unreached) as Arc<dyn DirectoryPort>)),
    );
    if listed {
        publisher
            .publish(
                true,
                json!({"directory":"http://127.0.0.1:9999","name":"GR swarm","access_mode":"request"})
                    .as_object()
                    .unwrap(),
            )
            .expect("the opt-in is recorded");
        assert!(publisher.listing_enabled());
    }
    loaded.swarm_publisher = Arc::new(publisher);
    Arc::new(Mutex::new(loaded))
}

/// RED (GR c): a published swarm's relay renews shortly before expiry (window/3 = 10 s here),
/// keeps renewing up to its cap, then stops renewing with `relay_renewal_cap_reached` and closes
/// at the deadline (`public_relay.py:395-407`, status `:428-429`).
#[test]
fn a_published_swarm_keeps_its_relay_window_renewing_up_to_the_cap() {
    let root = Root::new("listed");
    let core = core_with_listing(&root.0, true);
    let clock = Arc::new(AtomicU64::new(1_000_000));
    let lifetime = start(&core, &root.0, &clock, Some(2 * WINDOW));
    advance(&clock, WINDOW - 5);
    lifetime.step();
    let status = lifetime.status().to_value();
    assert_eq!(
        (
            status["renewals"].clone(),
            status["expires_in_seconds"].clone()
        ),
        (json!(1), json!(WINDOW)),
        "a serving relay renews its window: {status}"
    );
    advance(&clock, WINDOW - 5);
    lifetime.step();
    let status = lifetime.status().to_value();
    assert_eq!(status["renewals"], 1, "{status}");
    assert_eq!(status["renewal_stopped"], "relay_renewal_cap_reached");
    assert_eq!(status["state"], "relay_process_running");
    advance(&clock, 5);
    lifetime.step();
    assert_eq!(lifetime.status().to_value()["state"], "stopped");
    core.lock().unwrap().relay.close();
}

/// RED (GR b): the relay's renewal never waits on the launcher's core lock: while another thread
/// holds the core (a long action), the lifetime step still decides and renews.
#[test]
fn relay_renewal_is_not_starved_by_a_held_core() {
    let root = Root::new("held");
    let core = core_with_listing(&root.0, true);
    let clock = Arc::new(AtomicU64::new(1_000_000));
    let lifetime = start(&core, &root.0, &clock, None);
    let (held_tx, held_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel::<()>();
    let holder = {
        let core = core.clone();
        std::thread::spawn(move || {
            let _held = core.lock().unwrap();
            held_tx.send(()).unwrap();
            release_rx.recv_timeout(allowance()).unwrap();
        })
    };
    held_rx.recv_timeout(allowance()).unwrap();
    advance(&clock, WINDOW - 5);
    let (stepped_tx, stepped_rx) = mpsc::channel();
    let stepper = {
        let lifetime = lifetime.clone();
        std::thread::spawn(move || {
            lifetime.step();
            stepped_tx.send(lifetime.status().to_value()).unwrap();
        })
    };
    let stepped = stepped_rx.recv_timeout(allowance());
    release_tx.send(()).unwrap();
    holder.join().unwrap();
    stepper.join().unwrap();
    let status = stepped.expect("the lifetime step finished while the core was held");
    assert_eq!(
        status["renewals"], 1,
        "renewed while the core was held: {status}"
    );
    core.lock().unwrap().relay.close();
}

/// Guard: with neither an active run nor a listing the window is not extended; the relay closes
/// at its deadline (`public_relay.py:393-407`).
#[test]
fn an_idle_relay_is_not_renewed_and_closes_at_its_deadline() {
    let root = Root::new("idle");
    let core = core_with_listing(&root.0, false);
    let clock = Arc::new(AtomicU64::new(1_000_000));
    let lifetime = start(&core, &root.0, &clock, None);
    advance(&clock, WINDOW - 5);
    lifetime.step();
    let status = lifetime.status().to_value();
    assert_eq!(
        (status["renewals"].clone(), status["state"].clone()),
        (json!(0), json!("relay_process_running"))
    );
    advance(&clock, 5);
    lifetime.step();
    assert_eq!(lifetime.status().to_value()["state"], "stopped");
    core.lock().unwrap().relay.close();
}

/// An owner run whose first step is held until the test releases it, then fails (the run ends).
struct HeldRun {
    entered: mpsc::Sender<()>,
    release: mpsc::Receiver<()>,
}
impl owner_run::runner::Chain for HeldRun {
    fn new_run(
        &mut self,
        _: &str,
        _: &str,
        _: owner_run::machine::Reason,
        _: Option<&Value>,
        _: Option<&Value>,
    ) {
    }
    fn run_step(
        &mut self,
        _: &str,
        _: &mut dyn api::owner_driver::StepEnv,
    ) -> Result<(), api::owner_driver::StepError> {
        self.entered.send(()).unwrap();
        self.release.recv_timeout(allowance()).unwrap();
        Err(api::owner_driver::StepError::Exception {
            class: "RuntimeError".into(),
        })
    }
    fn teardown(&mut self, _: bool, _: &mut dyn api::owner_driver::StepEnv) -> Result<(), String> {
        Ok(())
    }
}
/// The admission facts of an owner with a swarm (`demo_app.action`'s ladder passes).
struct OwnerFacts;
impl owner_run::action::DemoFacts for OwnerFacts {
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

/// GRF 4 (kills mutant M7): with the swarm listing OFF, an active owner run alone keeps the relay
/// window renewing (`_relay_is_serving`: `owner_run.active()` OR listing, app.py:769-776); once the
/// run has ended the window is no longer extended and the relay closes at its deadline.
#[test]
fn an_active_owner_run_keeps_its_relay_window_renewing_until_the_run_ends() {
    let root = Root::new("run");
    let core = core_with_listing(&root.0, false);
    let (entered_tx, entered) = mpsc::channel();
    let (release, held) = mpsc::channel();
    let mut chain = Some(HeldRun {
        entered: entered_tx,
        release: held,
    });
    let mut runs = DemoRuns::new(
        Box::new(move || Box::new(chain.take().expect("one run"))),
        Box::new(|| fresh_identity(now()).expect("identity")),
        Timing::registered(),
    );
    // As the launcher does before serving: the core keeps the run's view.
    core.lock().unwrap().run.demo = runs.view();
    let clock = Arc::new(AtomicU64::new(1_000_000));
    let lifetime = start(&core, &root.0, &clock, None);
    runs.demo_action(Action::DemoStart, &Map::new(), &mut OwnerFacts)
        .expect("the run is admitted");
    entered
        .recv_timeout(allowance())
        .expect("the run is in its first step");
    assert!(runs.view().active(), "the owner run is active");
    advance(&clock, WINDOW - 5);
    lifetime.step();
    let status = lifetime.status().to_value();
    assert_eq!(
        (
            status["renewals"].clone(),
            status["expires_in_seconds"].clone()
        ),
        (json!(1), json!(WINDOW)),
        "an active owner run keeps the relay window renewing: {status}"
    );
    // The run ends.
    release.send(()).unwrap();
    let deadline = std::time::Instant::now() + allowance();
    while runs.view().active() || !runs.quiescent() {
        assert!(
            std::time::Instant::now() < deadline,
            "the run never ended: {:?}",
            runs.status().map(|s| s.to_value())
        );
        std::thread::yield_now();
    }
    advance(&clock, WINDOW - 5);
    lifetime.step();
    let status = lifetime.status().to_value();
    assert_eq!(
        (status["renewals"].clone(), status["state"].clone()),
        (json!(1), json!("relay_process_running")),
        "no renewal once the run has ended: {status}"
    );
    advance(&clock, 5);
    lifetime.step();
    assert_eq!(lifetime.status().to_value()["state"], "stopped");
    runs.close();
    core.lock().unwrap().relay.close();
}
