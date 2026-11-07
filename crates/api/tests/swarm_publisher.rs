// SPDX-License-Identifier: AGPL-3.0-only
use api::swarm::{
    core::{Config, Directory},
    json::{J, dumps_response},
    limits::Limits,
    publisher::{DirectoryPort, Error, Listing, OwnerState, Phase, SwarmPublisher},
    store::MemoryStore,
    wire::ed25519_verify,
};
use ed25519_dalek::SigningKey;
use serde_json::Value;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicI64, AtomicUsize, Ordering},
};

const NOW: i64 = 1_800_000_000;
struct Authority {
    core: Directory<MemoryStore>,
    offline: Mutex<bool>,
    calls: AtomicUsize,
}
impl DirectoryPort for Authority {
    fn call(&self, method: &str, path: &str, body: &[u8]) -> Result<Value, Error> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        if *self.offline.lock().unwrap() {
            return Err(Error::new("swarm_directory_unreachable"));
        }
        let (status, value) = self
            .core
            .handle(method, path, body, "ip:127.0.0.1")
            .unwrap();
        let value = serde_json::from_str(&dumps_response(&value)).unwrap();
        Error::response(status, value)
    }
}
fn authority() -> (Arc<Authority>, Arc<AtomicI64>) {
    let clock = Arc::new(AtomicI64::new(NOW));
    let c = clock.clone();
    let core = Directory::new(
        MemoryStore::new(),
        Box::new(ed25519_verify),
        Box::new(move || c.load(Ordering::SeqCst)),
        Config {
            limits: Limits::source().unwrap(),
            allow_loopback_entries: false,
            allow_private_entries: false,
        },
    )
    .unwrap();
    (
        Arc::new(Authority {
            core,
            offline: Mutex::new(false),
            calls: AtomicUsize::new(0),
        }),
        clock,
    )
}
fn listing() -> Listing {
    Listing {
        name: "Café swarm".into(),
        description: "Public description".into(),
        access_mode: "request".into(),
        open_quota: None,
    }
}
fn serving() -> OwnerState {
    OwnerState::Serving {
        model: "Qwen/Qwen3-0.6B".into(),
        members: 2,
        entry_origin: "https://entry.example.org".into(),
    }
}
fn swarms(a: &Authority) -> Vec<Value> {
    a.call("GET", "/swarms", b"").unwrap()["swarms"]
        .as_array()
        .unwrap()
        .clone()
}
fn seconds(id: budgets::BudgetId) -> i64 {
    (id.span(budgets::Entries::ZERO).unwrap().millis() / 1000) as i64
}
fn interval() -> i64 {
    seconds(budgets::BudgetId::PyMyceliumA13MacosSwarmPublishHeartbeatSecondsL42)
}

#[test]
fn opt_in_publish_renew_material_update_and_withdraw() {
    let (a, clock) = authority();
    let mut p = SwarmPublisher::memory(SigningKey::from_bytes(&[7; 32]), a.clone()).unwrap();
    assert_eq!(p.reconcile(NOW, &serving()).unwrap().phase, Phase::Unlisted);
    assert_eq!(a.calls.load(Ordering::SeqCst), 0);
    p.set_listing(Some(listing())).unwrap();
    assert_eq!(p.reconcile(NOW, &serving()).unwrap().phase, Phase::Listed);
    let first = swarms(&a).remove(0);
    assert_eq!(first["record"]["protocol"], "mycelium.swarm.record.v1");
    assert_eq!(first["status"], "serving");
    assert_eq!(first["members"], 2);
    let before = a.calls.load(Ordering::SeqCst);
    p.reconcile(NOW, &serving()).unwrap();
    assert_eq!(a.calls.load(Ordering::SeqCst), before);
    clock.store(NOW + interval(), Ordering::SeqCst);
    p.reconcile(NOW + interval(), &serving()).unwrap();
    assert!(a.calls.load(Ordering::SeqCst) > before);
    let changed = OwnerState::Serving {
        model: "Qwen/Qwen3-4B".into(),
        members: 3,
        entry_origin: "https://entry.example.org".into(),
    };
    p.reconcile(NOW + interval(), &changed).unwrap();
    let current = swarms(&a).remove(0);
    assert_eq!(current["record"]["model"], "Qwen/Qwen3-4B");
    assert_eq!(current["members"], 3);
    let mut options = listing();
    options.access_mode = "invite".into();
    p.set_listing(Some(options)).unwrap();
    p.reconcile(NOW + interval(), &changed).unwrap();
    assert_eq!(swarms(&a)[0]["access_mode"], "invite");
    p.set_listing(None).unwrap();
    assert_eq!(
        p.reconcile(NOW + interval(), &changed).unwrap().phase,
        Phase::Unlisted
    );
    assert!(swarms(&a).is_empty());
}

#[test]
fn withdrawal_debt_blocks_republication_and_retries_at_bounded_cadence() {
    let (a, clock) = authority();
    let mut p = SwarmPublisher::memory(SigningKey::from_bytes(&[8; 32]), a.clone()).unwrap();
    p.set_listing(Some(listing())).unwrap();
    p.reconcile(NOW, &serving()).unwrap();
    *a.offline.lock().unwrap() = true;
    let state = p.reconcile(NOW, &OwnerState::Paused).unwrap();
    assert_eq!(state.phase, Phase::WithdrawPending);
    assert_eq!(
        state.error_code.as_deref(),
        Some("swarm_directory_unreachable")
    );
    let calls = a.calls.load(Ordering::SeqCst);
    p.reconcile(NOW, &serving()).unwrap();
    assert_eq!(a.calls.load(Ordering::SeqCst), calls);
    *a.offline.lock().unwrap() = false;
    clock.store(NOW + interval(), Ordering::SeqCst);
    assert_eq!(
        p.reconcile(NOW + interval(), &OwnerState::Paused)
            .unwrap()
            .phase,
        Phase::Unlisted
    );
    assert!(swarms(&a).is_empty());
    p.reconcile(NOW + interval(), &serving()).unwrap();
    assert_eq!(swarms(&a).len(), 1);
}

#[test]
fn version_refusal_has_specific_ui_code() {
    assert_eq!(
        Error::response(400, serde_json::json!({"error":"protocol_invalid"}))
            .unwrap_err()
            .code,
        "swarm_directory_version_unsupported"
    );
    let (a, _) = authority();
    let bad = J::obj(vec![("protocol", J::str("mycelium.swarm.record.v3"))]);
    let result = a.call("POST", "/records", dumps_response(&bad).as_bytes());
    assert_eq!(
        result.unwrap_err().code,
        "swarm_directory_version_unsupported"
    );
}

#[test]
fn host_adapter_requires_owner_route_qualification_and_handles_all_stop_events() {
    use api::swarm::publisher::host::{OwnerView, PublisherHost, Stop};
    let (a, _) = authority();
    let p = SwarmPublisher::memory(SigningKey::from_bytes(&[9; 32]), a.clone()).unwrap();
    let mut host = PublisherHost::new(p);
    assert_eq!(
        host.listing_preference(false, Some(listing()))
            .unwrap_err()
            .code,
        "swarm_owner_role_required"
    );
    host.listing_preference(true, Some(listing())).unwrap();
    let view = |ready| OwnerView {
        is_owner: true,
        run_state: "serving",
        route_ready: ready,
        registry_origin: Some("http://127.0.0.1:9999"),
        model: Some("Qwen/Qwen3-0.6B"),
        members: 2,
        public_entry_origin: Some("https://entry.example.org"),
    };
    host.observe(NOW, view(false)).unwrap();
    assert!(swarms(&a).is_empty());
    assert_eq!(host.observe(NOW, view(true)).unwrap().phase, Phase::Listed);
    for stop in [Stop::Pause, Stop::Quit, Stop::Leave] {
        host.stop(NOW, stop).unwrap();
        assert!(swarms(&a).is_empty());
        host.listing_preference(true, Some(listing())).unwrap();
        assert_eq!(host.observe(NOW, view(true)).unwrap().phase, Phase::Listed);
    }
    let mut member = view(true);
    member.is_owner = false;
    host.observe(NOW, member).unwrap();
    assert!(swarms(&a).is_empty());
    assert!(!host.status().enabled);
}

struct LostReply {
    authority: Arc<Authority>,
    lose: Mutex<Option<&'static str>>,
}
impl DirectoryPort for LostReply {
    fn call(&self, method: &str, path: &str, body: &[u8]) -> Result<Value, Error> {
        let result = self.authority.call(method, path, body)?;
        let mut lose = self.lose.lock().unwrap();
        if lose.is_some_and(|v| v == path) {
            *lose = None;
            return Err(Error::new("swarm_directory_unreachable"));
        }
        Ok(result)
    }
}
#[test]
fn lost_record_and_delete_acknowledgments_reconcile_without_orphans() {
    let (a, clock) = authority();
    let port = Arc::new(LostReply {
        authority: a.clone(),
        lose: Mutex::new(Some("/records")),
    });
    let mut p = SwarmPublisher::memory(SigningKey::from_bytes(&[10; 32]), port.clone()).unwrap();
    p.set_listing(Some(listing())).unwrap();
    assert_eq!(p.reconcile(NOW, &serving()).unwrap().phase, Phase::Failed);
    let id = swarms(&a)[0]["record"]["swarm_id"].clone();
    let now = NOW + interval();
    clock.store(now, Ordering::SeqCst);
    assert_eq!(p.reconcile(now, &serving()).unwrap().phase, Phase::Listed);
    assert_eq!(swarms(&a)[0]["record"]["swarm_id"], id);
    *port.lose.lock().unwrap() = Some("/delete");
    p.set_listing(None).unwrap();
    assert_eq!(
        p.reconcile(now, &OwnerState::Quit).unwrap().phase,
        Phase::WithdrawPending
    );
    assert!(swarms(&a).is_empty());
    let now = now + interval();
    clock.store(now, Ordering::SeqCst);
    assert_eq!(
        p.reconcile(now, &OwnerState::Quit).unwrap().phase,
        Phase::Unlisted
    );
    assert!(swarms(&a).is_empty());
}

#[test]
fn open_access_is_explicit_and_invalid_public_fields_never_cross_the_port() {
    let (a, _) = authority();
    let mut p = SwarmPublisher::memory(SigningKey::from_bytes(&[11; 32]), a.clone()).unwrap();
    let mut invalid = listing();
    invalid.name = "too long".repeat(20);
    p.set_listing(Some(invalid)).unwrap();
    assert_eq!(
        p.reconcile(NOW, &serving()).unwrap().error_code.as_deref(),
        Some("swarm_name_invalid")
    );
    assert_eq!(a.calls.load(Ordering::SeqCst), 0);
    let mut open = listing();
    open.access_mode = "open".into();
    open.open_quota =
        Some(serde_json::json!({"max_requests":20,"max_tokens":4000,"ttl_seconds":86400}));
    p.set_listing(Some(open)).unwrap();
    let now = NOW + interval();
    assert_eq!(p.reconcile(now, &serving()).unwrap().phase, Phase::Listed);
    assert_eq!(swarms(&a)[0]["access_mode"], "open");
}
