// SPDX-License-Identifier: AGPL-3.0-only
use budgets::Span;
use device_harness::harness_budgets::{KeeperLimits, L1};
use device_harness::{
    Fault, FaultKind, Result, effects::Clock, native_session::*, preflight::ProcessIdentity,
    stranger::Candidate,
};
use serde_json::{Value, json};
use std::{cell::Cell, collections::VecDeque, rc::Rc};
#[derive(Clone, Default)]
struct Tick(Rc<Cell<u64>>);
impl Clock for Tick {
    fn now(&self) -> u64 {
        self.0.get()
    }
    fn sleep(&mut self, s: Span) -> Result<()> {
        self.0.set(self.now() + s.millis());
        Ok(())
    }
}
fn binding() -> Binding {
    Binding {
        candidate: Candidate {
            version: "0.2.0".into(),
            source_sha: "a".repeat(40),
            zip_sha256: "b".repeat(64),
            exe_sha256: "c".repeat(64),
        },
        process: ProcessIdentity {
            pid: 42,
            birth: "fixture-birth".into(),
            executable: "/fixture/Mycelium.app/Contents/MacOS/mycelium".into(),
        },
        launch_epoch_ms: 1000,
        desktop_manifest_sha256: "d".repeat(64),
        native_inputs_sha256: "e".repeat(64),
    }
}
fn page(http: u16, json: Value) -> Reply {
    Reply {
        http,
        json,
        headers: vec![],
    }
}
fn expired() -> Reply {
    page(401, json!({"error":"open_from_native_launcher"}))
}
fn renewal() -> Vec<Reply> {
    vec![
        page(200, json!({"launch_file_ready":true})),
        Reply {
            http: 303,
            json: Value::Null,
            headers: vec![
                (
                    "Set-Cookie".into(),
                    "a13session=fresh-fixture; HttpOnly; SameSite=Strict; Path=/".into(),
                ),
                ("Location".into(), "/".into()),
            ],
        },
    ]
}
struct Fake {
    clock: Tick,
    observation: Observation,
    pages: VecDeque<Result<Reply>>,
    session: Option<Session>,
    requests: Vec<Request>,
    events: Vec<String>,
    bounds: Vec<u64>,
    delays: VecDeque<u64>,
    form_port: u16,
    form_path: String,
    form_modified: u64,
    stale_save: bool,
    fail_credential: bool,
    change_after: Option<usize>,
}
impl Fake {
    fn new(pages: Vec<Reply>) -> Self {
        let b = binding();
        Self {
            clock: Tick::default(),
            observation: Observation {
                binding: b.clone(),
                control_modified_ms: 1000,
                port: 49152,
            },
            pages: pages.into_iter().map(Ok).collect(),
            session: Some(Session {
                binding: b,
                port: 49152,
                cookie: "a13session=old-fixture".into(),
            }),
            requests: vec![],
            events: vec![],
            bounds: vec![],
            delays: VecDeque::new(),
            form_port: 49152,
            form_path: "/session".into(),
            form_modified: 1000,
            stale_save: false,
            fail_credential: false,
            change_after: None,
        }
    }
    fn event(&mut self, s: &str, b: Span) {
        self.events.push(s.into());
        self.bounds.push(b.millis());
        assert!(b > Span::ZERO);
        let d = self.delays.pop_front().unwrap_or(0);
        self.clock.0.set(self.clock.now() + d);
    }
    fn adapter(self) -> Adapter<Self, Tick> {
        let t = self.clock.clone();
        Adapter::new(self, t, binding()).unwrap()
    }
}
impl Io for Fake {
    fn observe(&mut self, b: Span) -> Result<Observation> {
        self.event("observe", b);
        if self.change_after.is_some_and(|n| self.requests.len() >= n) {
            self.observation.binding.process.birth = "reused".into();
        }
        Ok(self.observation.clone())
    }
    fn load_session(&mut self, b: Span) -> Result<Option<Session>> {
        self.event("load", b);
        Ok(self.session.clone())
    }
    fn native_credential(&mut self, b: Span) -> Result<String> {
        self.event("credential", b);
        if self.fail_credential {
            Err(Fault::new(
                "fixture_private_control_refused",
                FaultKind::Observation,
            ))
        } else {
            Ok("NATIVE_FIXTURE_SECRET".into())
        }
    }
    fn launch_form(&mut self, b: Span) -> Result<LaunchForm> {
        self.event("form", b);
        Ok(LaunchForm {
            port: self.form_port,
            path: self.form_path.clone(),
            launch: "LAUNCH_FIXTURE_SECRET".into(),
            modified_ms: self.form_modified,
        })
    }
    fn exchange(&mut self, r: Request, b: Span) -> Result<Reply> {
        self.event("exchange", b);
        self.requests.push(r);
        self.pages.pop_front().expect("unexpected HTTP")
    }
    fn save_session(&mut self, s: &Session, b: Span) -> Result<()> {
        self.event("save", b);
        if !self.stale_save {
            self.session = Some(s.clone())
        }
        Ok(())
    }
}
fn total() -> Span {
    Span::from_millis(20_000)
}
#[test]
fn native_member_authenticates_once_sends_exact_csrf_body_and_never_replays_action() {
    use device_harness::member::{Action, perform};
    for action in [Action::Start, Action::Capability] {
        let mut pages = vec![expired()];
        pages.extend(renewal());
        pages.extend([
            page(200, json!({"authority":{"membership_state":"current"}})),
            expired(),
        ]);
        let mut a = Fake::new(pages).adapter();
        assert_eq!(
            perform(&mut a, action, total()).unwrap_err().code,
            "join_terminal_error"
        );
        let requests = &a.io.requests;
        assert_eq!(requests.len(), 5);
        assert!(matches!(
            requests[4].path,
            "/api/start" | "/api/capability/report"
        ));
        assert_eq!(requests[4].body, b"{}");
        assert_eq!(requests[4].headers["X-Mycelium-Action"], "1");
        assert_eq!(requests[4].headers["Origin"], "http://127.0.0.1:49152");
        assert_eq!(requests[4].headers["Cookie"], "a13session=fresh-fixture");
        assert!(!requests[4].headers.contains_key("X-Native-Authorization"));
    }
}
#[test]
fn native_member_total_deadline_spans_authentication_and_action_without_replay() {
    let mut f = Fake::new(vec![page(200, json!({})), page(200, json!({}))]);
    f.delays = std::iter::repeat_n(total().millis() / 10, 10).collect();
    let mut a = f.adapter();
    assert_eq!(
        device_harness::member::perform(&mut a, device_harness::member::Action::Start, total())
            .unwrap_err()
            .code,
        "owner_status_timeout"
    );
    assert_eq!(
        a.io.requests
            .iter()
            .filter(|r| r.path == "/api/start")
            .count(),
        1
    );
    assert_eq!(a.io.requests.len(), 2);
}
#[test]
fn native_member_read_only_status_and_unknown_action_refuse_before_any_effect() {
    use device_harness::http::{Http, Method, Request as HttpRequest};
    let mut a = Fake::new(vec![page(
        200,
        json!({"authority":{"membership_state":"current"}}),
    )])
    .adapter();
    assert_eq!(
        device_harness::member::perform(&mut a, device_harness::member::Action::Status, total())
            .unwrap()["authority"]["membership_state"],
        "current"
    );
    assert_eq!(a.io.requests.len(), 1);
    let before = a.io.events.len();
    for path in ["/api/stop", "/api/reset", "http://external/api/start"] {
        assert!(
            a.call(
                HttpRequest {
                    path,
                    method: Method::Post,
                    body: Some(json!({})),
                    chat: false
                },
                total()
            )
            .is_err()
        );
    }
    assert_eq!(a.io.events.len(), before);
}
#[test]
fn expired_owner_session_renews_via_native_reopen_exact_requests_and_receipt() {
    let mut pages = vec![expired()];
    pages.extend(renewal());
    pages.push(page(200, json!({"owner_run":{"state":"serving"}})));
    let mut a = Fake::new(pages).adapter();
    let s = a.check(total()).unwrap();
    assert_eq!(s.value["owner_run"]["state"], "serving");
    assert_eq!(s.receipt.renewals, 1);
    assert!(!s.receipt.release_qualified);
    let rs = &a.io.requests;
    assert_eq!(
        rs.iter().map(|r| r.path).collect::<Vec<_>>(),
        vec!["/api/status", "/native-session", "/session", "/api/status"]
    );
    assert!(rs.iter().all(|r| r.port == 49152 && !r.follow_redirects));
    assert_eq!(rs[1].method, "POST");
    assert_eq!(rs[1].body, b"{}");
    assert_eq!(
        rs[1].headers["X-Native-Authorization"],
        "NATIVE_FIXTURE_SECRET"
    );
    assert!(!rs[1].headers.contains_key("Origin"));
    assert!(!rs[1].headers.contains_key("Cookie"));
    assert_eq!(rs[2].headers["Origin"], "null");
    assert_eq!(rs[2].body, b"launch=LAUNCH_FIXTURE_SECRET");
    assert_eq!(
        rs[2].headers["Content-Type"],
        "application/x-www-form-urlencoded"
    );
    assert_eq!(rs[3].headers["Cookie"], "a13session=fresh-fixture");
    let out = serde_json::to_string(&s.receipt).unwrap();
    for secret in [
        "NATIVE_FIXTURE_SECRET",
        "LAUNCH_FIXTURE_SECRET",
        "a13session=",
    ] {
        assert!(!out.contains(secret));
    }
}
#[test]
fn second_refusal_is_not_a_renewal_loop_and_other_failures_never_reopen() {
    let mut pages = vec![expired()];
    pages.extend(renewal());
    pages.push(expired());
    let mut a = Fake::new(pages).adapter();
    assert_eq!(a.check(total()).err().unwrap().code, "owner_status_refused");
    assert_eq!(
        a.io.requests
            .iter()
            .filter(|r| r.path == "/native-session")
            .count(),
        1
    );
    for reply in [
        page(500, json!({})),
        page(401, json!({"error":"other"})),
        page(403, json!({"error":"open_from_native_launcher"})),
    ] {
        let mut a = Fake::new(vec![reply]).adapter();
        assert_eq!(a.check(total()).err().unwrap().code, "owner_status_refused");
        assert_eq!(a.io.requests.len(), 1);
    }
    let mut f = Fake::new(vec![]);
    f.pages.push_back(Err(Fault::new(
        "owner_status_timeout",
        FaultKind::Observation,
    )));
    let mut a = f.adapter();
    assert_eq!(a.check(total()).err().unwrap().code, "owner_status_timeout");
    assert_eq!(a.io.requests.len(), 1);
}
#[test]
fn successful_cached_check_does_not_renew_and_absent_session_opens_once() {
    let mut a = Fake::new(vec![page(200, json!({"owner_run":{}}))]).adapter();
    assert_eq!(a.check(total()).unwrap().receipt.renewals, 0);
    assert_eq!(a.io.requests.len(), 1);
    let mut pages = renewal();
    pages.push(page(200, json!({})));
    let mut f = Fake::new(pages);
    f.session = None;
    let mut a = f.adapter();
    assert_eq!(a.check(total()).unwrap().receipt.renewals, 1);
    assert_eq!(a.io.requests.len(), 3);
}
#[test]
fn candidate_process_resource_or_stale_control_mismatch_refuses_before_http() {
    for field in [
        "candidate",
        "pid",
        "birth",
        "exe",
        "desktop",
        "native",
        "mtime",
        "port",
        "session",
    ] {
        let mut f = Fake::new(vec![]);
        match field {
            "candidate" => f.observation.binding.candidate.exe_sha256 = "f".repeat(64),
            "pid" => f.observation.binding.process.pid = 43,
            "birth" => f.observation.binding.process.birth = "other".into(),
            "exe" => f.observation.binding.process.executable = "/other".into(),
            "desktop" => f.observation.binding.desktop_manifest_sha256 = "f".repeat(64),
            "native" => f.observation.binding.native_inputs_sha256 = "f".repeat(64),
            "mtime" => f.observation.control_modified_ms = 999,
            "port" => f.observation.port = 0,
            _ => f.session.as_mut().unwrap().binding.candidate.zip_sha256 = "f".repeat(64),
        }
        let mut a = f.adapter();
        assert!(a.check(total()).is_err(), "{field}");
        assert!(a.io.requests.is_empty(), "{field}");
    }
}
#[test]
fn revalidate_listener_after_http_and_never_publish_reused_pid_status() {
    let mut f = Fake::new(vec![page(200, json!({}))]);
    f.change_after = Some(1);
    let mut a = f.adapter();
    assert_eq!(
        a.check(total()).err().unwrap().code,
        "native_candidate_binding_changed"
    );
}
#[test]
fn reopen_form_redirect_cookie_and_private_store_fail_closed() {
    for bad in [
        "form_port",
        "form_path",
        "form_stale",
        "cookie",
        "redirect",
        "native",
        "store",
        "credential",
    ] {
        let mut pages = vec![expired()];
        pages.extend(renewal());
        let mut f = Fake::new(pages);
        match bad {
            "form_port" => f.form_port = 49153,
            "form_path" => f.form_path = "https://attacker.invalid/session".into(),
            "form_stale" => f.form_modified = 999,
            "cookie" => {
                f.pages[2].as_mut().unwrap().headers[0].1 =
                    "a13session=bad\r\nInjected: value".into()
            }
            "redirect" => f.pages[2].as_mut().unwrap().http = 302,
            "native" => f.pages[1].as_mut().unwrap().json = json!({"launch_file_ready":false}),
            "store" => f.stale_save = true,
            _ => f.fail_credential = true,
        }
        let mut a = f.adapter();
        assert!(a.check(total()).is_err(), "{bad}");
        assert!(a.io.requests.len() <= 3);
    }
}
#[test]
fn one_total_deadline_covers_identity_file_reopen_and_http_not_per_step_resets() {
    let mut pages = vec![expired()];
    pages.extend(renewal());
    pages.push(page(200, json!({})));
    // Cross the deadline in successive phases without sleeping or sockets.
    for phase in 0..12 {
        let mut f = Fake::new(vec![]);
        f.pages = pages
            .iter()
            .map(|p| {
                Ok(Reply {
                    http: p.http,
                    json: p.json.clone(),
                    headers: p.headers.clone(),
                })
            })
            .collect();
        f.delays = (0..=phase)
            .map(|n| if n == phase { 20_001 } else { 0 })
            .collect();
        let mut a = f.adapter();
        let result = a.check(total());
        assert!(a.io.events.len() > phase, "did not reach phase {phase}");
        assert_eq!(
            result.err().unwrap().code,
            "owner_status_timeout",
            "phase {phase}"
        );
    }
    let mut a = Fake::new(vec![]).adapter();
    assert!(a.check(Span::ZERO).is_err());
    assert!(a.io.events.is_empty());
}
#[derive(Default)]
struct KeeperOps {
    state: device_harness::keeper::State,
    starts: usize,
    alerts: Vec<device_harness::keeper::Alert>,
}
impl device_harness::keeper::Io for KeeperOps {
    fn owner_status(&mut self) -> Result<Value> {
        Err(Fault::new("unbound_owner_adapter", FaultKind::Observation))
    }
    fn member_clocks(&mut self) -> Result<Vec<device_harness::keeper::MemberClock>> {
        Ok(vec![device_harness::keeper::MemberClock {
            peer: "fixture-member".into(),
            age_ms: Some(1_565_000),
        }])
    }
    fn public_healthy(&mut self) -> Result<bool> {
        Ok(true)
    }
    fn load(&mut self) -> Result<device_harness::keeper::State> {
        Ok(self.state.clone())
    }
    fn save(
        &mut self,
        prior: &device_harness::keeper::State,
        next: &device_harness::keeper::State,
    ) -> Result<()> {
        assert_eq!(&self.state, prior);
        self.state = next.clone();
        Ok(())
    }
    fn alert(&mut self, a: device_harness::keeper::Alert) -> Result<()> {
        self.alerts.push(a);
        Ok(())
    }
    fn start(&mut self) -> Result<()> {
        assert_eq!(self.state.recoveries.len(), 1);
        self.starts += 1;
        Ok(())
    }
}
fn keeper_status(state: &str) -> Value {
    json!({"swarm":{"swarm_id":"fixture-swarm"},"owner_run":{"state":state,"cleanup_unproven":null,"error_code":"rollover_members_changed","rollover":{"state":"failed"}}})
}
fn keeper_limits() -> device_harness::harness_budgets::KeeperLimits {
    KeeperLimits::from_registry(&L1).unwrap()
}
#[test]
fn keeper_composes_real_native_adapter_renewal_member_alert_and_durable_recovery() {
    let mut pages = vec![expired()];
    pages.extend(renewal());
    pages.extend([
        page(200, keeper_status("failed")),
        page(200, keeper_status("failed")),
        page(200, keeper_status("serving")),
    ]);
    let mut k = device_harness::keeper::NativeKeeper::new(
        Fake::new(pages).adapter(),
        KeeperOps::default(),
        total(),
    );
    let c = device_harness::keeper::check(&mut k, 100, "fixture-swarm", keeper_limits()).unwrap();
    assert!(c.start_submitted);
    assert!(c.serving_observed);
    assert_eq!(k.operations.starts, 1);
    assert!(c.alerts.iter().any(|a| a.code == "member_silent"));
    assert_eq!(
        k.owner
            .io
            .requests
            .iter()
            .filter(|r| r.path == "/native-session")
            .count(),
        1
    );
}
#[test]
fn keeper_check_shares_one_renewal_allowance_and_one_observation_deadline() {
    let mut pages = vec![expired()];
    pages.extend(renewal());
    pages.push(page(200, keeper_status("failed")));
    pages.push(expired());
    pages.extend(renewal());
    pages.extend([
        page(200, keeper_status("failed")),
        page(200, keeper_status("serving")),
    ]);
    let mut k = device_harness::keeper::NativeKeeper::new(
        Fake::new(pages).adapter(),
        KeeperOps::default(),
        total(),
    );
    assert_eq!(
        device_harness::keeper::check(&mut k, 100, "fixture-swarm", keeper_limits())
            .unwrap_err()
            .code,
        "owner_status_refused"
    );
    assert_eq!(
        k.owner
            .io
            .requests
            .iter()
            .filter(|r| r.path == "/native-session")
            .count(),
        1
    );
    assert_eq!(k.operations.starts, 0);
    let mut f = Fake::new(vec![
        page(200, keeper_status("failed")),
        page(200, keeper_status("failed")),
        page(200, keeper_status("serving")),
    ]);
    f.delays = [0, 0, 0, 12_000, 0, 0, 0, 0, 12_000].into();
    let mut k =
        device_harness::keeper::NativeKeeper::new(f.adapter(), KeeperOps::default(), total());
    assert_eq!(
        device_harness::keeper::check(&mut k, 100, "fixture-swarm", keeper_limits())
            .unwrap_err()
            .code,
        "owner_status_timeout"
    );
    assert_eq!(k.operations.starts, 0);
}
