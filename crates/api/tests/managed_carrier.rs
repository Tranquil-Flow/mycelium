// SPDX-License-Identifier: AGPL-3.0-only
//! CONTROLLED signed receipt peer over actual localhost QUIC. No native hello,
//! model, worker Stop, target mailbox execution, or physical-host claim.
use api::managed_carrier::{HostClock, LocalMailboxCarrier};
use budgets::{BudgetId as B, Entries, MonoInstant, Span};
use protocol::identity::Identity;
use runtime::managed_owner::{
    Limits, ManagedControlOwner, OwnerError, OwnerHost,
    session::{CallFence, ManagedSessionFactory, SessionHost, SharedManagedOwner},
};
use serde_json::{Value, json};
use std::{
    fs,
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};
use transport::Clock;
static NEXT: AtomicU64 = AtomicU64::new(0);
fn scope() -> Value {
    json!({"target_node_id":"member", "target_endpoint_id":"member-endpoint", "target_key_digest":format!("sha256:{}", "a".repeat(64)), "membership_generation":1, "member_incarnation":"inc-1", "run_id":"run", "deployment_id":"deployment"})
}
fn member() -> Value {
    json!({"node_id":"member", "endpoint_id":"member-endpoint", "verification_key_digest":scope()["target_key_digest"], "generation":1, "incarnation":"inc-1", "lifecycle_state":"RUNNING", "peer_class":"mac_mlx_iroh", "lease_expires_at":1e12})
}
fn allowance() -> Span {
    B::PyMyceliumA13MacosManagedControlManagedcontrolclientInitTimeoutL1933
        .span(Entries::ZERO)
        .unwrap()
}
struct Root(PathBuf);
impl Root {
    fn new() -> Self {
        use std::os::unix::fs::DirBuilderExt;
        let path = std::env::temp_dir().join(format!(
            "l10-carrier-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::DirBuilder::new().mode(0o700).create(&path).unwrap();
        Self(path)
    }
}
impl Drop for Root {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
#[derive(Clone)]
struct Host {
    signer: Arc<Identity>,
    clock: HostClock,
    carrier: Arc<LocalMailboxCarrier>,
    member: Arc<Mutex<Value>>,
    root: Arc<Root>,
}
impl Clock for Host {
    fn monotonic(&self) -> MonoInstant {
        self.clock.monotonic()
    }
    fn wall_seconds(&self) -> f64 {
        self.clock.wall_seconds()
    }
}
impl Host {
    fn raw(
        &self,
        prepared: &Value,
        end: MonoInstant,
        fence: Option<&CallFence>,
    ) -> Result<Value, OwnerError> {
        if prepared["operation"] == "enqueue" {
            let disk: Value =
                serde_json::from_slice(&fs::read(self.root.0.join("owner.json")).unwrap()).unwrap();
            let id = prepared["envelope"]["statement"]["native_command"]["command_id"]
                .as_str()
                .unwrap();
            assert_eq!(
                disk["commands"][id]["enqueue_request"], *prepared,
                "actual durable pre-IO intent"
            );
        }
        let bytes = protocol::canonical::to_bytes(&prepared["envelope"]).unwrap();
        let response = self.carrier.call(&self.clock, &bytes, end, fence)?;
        protocol::strict::object(&response, protocol::managed::MAX_FRAME_BYTES)
            .map_err(|_| OwnerError("managed_control_json_invalid"))
    }
}
impl OwnerHost for Host {
    fn signer(&self) -> &Identity {
        &self.signer
    }
    fn current_member(&mut self, _: &str) -> Result<Value, OwnerError> {
        Ok(self.member.lock().unwrap().clone())
    }
    fn nonce(&mut self) -> Result<String, OwnerError> {
        Ok(format!("nonce-{}", NEXT.fetch_add(1, Ordering::Relaxed)))
    }
    fn exchange(&mut self, p: &Value, d: MonoInstant) -> Result<Value, OwnerError> {
        self.raw(p, d, None)
    }
    fn wait(&mut self, a: Span, _: MonoInstant) -> Result<(), OwnerError> {
        std::thread::sleep(Duration::from_millis(a.millis()));
        Ok(())
    }
}
impl SessionHost for Host {
    fn exchange_fenced(
        &mut self,
        p: &Value,
        d: MonoInstant,
        f: &CallFence,
    ) -> Result<Value, OwnerError> {
        self.raw(p, d, Some(f))
    }
    fn wait_fenced(&mut self, a: Span, _: MonoInstant, f: &CallFence) -> Result<(), OwnerError> {
        f.wait_for(a)
    }
}
fn reply(
    bytes: &[u8],
    key: &Identity,
    clock: &HostClock,
    wrong_binding: bool,
) -> Result<Vec<u8>, OwnerError> {
    let envelope = protocol::strict::object(bytes, protocol::managed::MAX_FRAME_BYTES).unwrap();
    let s = &envelope["statement"];
    let enqueue = s["protocol"] == "mycelium.managed_control.owner_command.v1";
    protocol::managed::verify(
        &envelope,
        s["protocol"].as_str().unwrap(),
        &key.public_key_record(),
        key.endpoint_id(),
        clock.wall_seconds(),
        protocol::TimeLimits {
            max_ttl_seconds: B::ControlTtl.span(Entries::ZERO).unwrap().millis() as f64 / 1000.0,
            clock_skew_seconds: B::ControlClockSkew.span(Entries::ZERO).unwrap().millis() as f64
                / 1000.0,
        },
    )
    .unwrap();
    for (k, v) in scope().as_object().unwrap() {
        assert_eq!(s[k], *v, "independent controlled peer scope");
    }
    let now = clock.wall_seconds();
    let ttl = B::ManagedControlExchangeResponseTtl
        .span(Entries::ZERO)
        .unwrap();
    let mut receipt = scope();
    receipt.as_object_mut().unwrap().extend(json!({"protocol":if enqueue {"mycelium.managed_control.enqueue_receipt.v1"} else {"mycelium.managed_control.result_receipt.v1"},
        "request_id":s["request_id"], "swarm_id":"swarm", "owner_node_id":"owner", "owner_endpoint_id":key.endpoint_id(),
        "issued_at":now, "expires_at":now+ttl.millis() as f64/1000.0, "command_id":s["native_command"]["command_id"],
        "command_digest":protocol::canonical::digest(&s["native_command"]).unwrap(), "state":if enqueue {"pending"} else {"completed"}}).as_object().unwrap().clone());
    if !enqueue {
        receipt["native_response"] = json!({"protocol":protocol::native::CONTROL_PROTOCOL, "command_id":s["native_command"]["command_id"], "node_id":"member", "ok":true, "route_ready":false, "result":{"controlled_fixture":true}});
    }
    if !enqueue {
        receipt["native_response_digest"] =
            json!(protocol::canonical::digest(&receipt["native_response"]).unwrap());
    }
    if wrong_binding {
        receipt["member_incarnation"] = json!("wrong-incarnation");
    }
    protocol::canonical::to_bytes(&json!({"protocol":protocol::managed::SIGNED_ENVELOPE_PROTOCOL, "signature":key.sign(&receipt).unwrap(), "verification_key":key.public_key_record(), "statement":receipt})).map_err(|_| OwnerError("invalid_control_result"))
}
fn make_host(wrong_binding: bool) -> Host {
    let signer = Arc::new(Identity::from_secret("owner-endpoint", &[7; 32]).unwrap());
    let clock = HostClock::default();
    let peer_key = signer.clone();
    let peer_clock = clock.clone();
    let carrier = Arc::new(
        LocalMailboxCarrier::start(move |bytes| {
            reply(bytes, &peer_key, &peer_clock, wrong_binding)
        })
        .unwrap(),
    );
    Host {
        signer,
        clock,
        carrier,
        member: Arc::new(Mutex::new(member())),
        root: Arc::new(Root::new()),
    }
}
#[test]
fn actual_journal_factory_and_carrier_validate_signed_receipts_and_scope() {
    for wrong in [false, true] {
        let host = make_host(wrong);
        let owner = ManagedControlOwner::open(
            &host.root.0,
            &host.signer,
            "swarm",
            "owner",
            Limits::registered().unwrap(),
        )
        .unwrap();
        let h = host.clone();
        let owner = SharedManagedOwner::new(owner, move || Ok(h.clone()));
        let factory =
            ManagedSessionFactory::new(owner.clone(), "run", "deployment", &[member()]).unwrap();
        let session = factory
            .open(
                &[],
                "member",
                "run",
                "deployment",
                allowance().millis() as f64 / 1000.0,
            )
            .unwrap();
        let response = session.send("hello-controlled", "hello", json!({}));
        if wrong {
            assert!(
                response.is_err(),
                "signed wrong binding must be refused: {response:?}"
            );
        } else {
            assert_eq!(response.unwrap()["result"]["controlled_fixture"], true);
        }
        assert!(
            factory
                .open(&[], "member", "foreign-run", "deployment", 1.0)
                .is_err()
        );
        host.member.lock().unwrap()["incarnation"] = json!("replaced");
        assert!(!factory.binding_current("member"));
        assert!(
            factory
                .open(&[], "member", "run", "deployment", 1.0)
                .is_err()
        );
        owner.close().unwrap();
        assert!(
            factory
                .open(&[], "member", "run", "deployment", 1.0)
                .is_err()
        );
        // Transport close does not manufacture a native Stop response.
        assert!(session.stop_response().is_none());
        host.carrier.close().unwrap();
    }
}
#[test]
fn owner_close_fences_a_held_actual_raw_call_and_preserves_unresolved_intent() {
    let mut host = make_host(false);
    host.carrier.close().unwrap();
    let (entered_tx, entered_rx) = std::sync::mpsc::sync_channel(1);
    let (release_tx, release_rx) = std::sync::mpsc::sync_channel(1);
    struct Release(Option<std::sync::mpsc::SyncSender<()>>);
    impl Drop for Release {
        fn drop(&mut self) {
            if let Some(tx) = self.0.take()
                && tx.send(()).is_err()
            {
                eprintln!("held-call release: receiver already disconnected");
            }
        }
    }
    let release = Release(Some(release_tx));
    let key = host.signer.clone();
    let clock = host.clock.clone();
    host.carrier = Arc::new(
        LocalMailboxCarrier::start(move |bytes| {
            entered_tx.send(()).unwrap();
            release_rx.recv().unwrap();
            reply(bytes, &key, &clock, false)
        })
        .unwrap(),
    );
    let owner = ManagedControlOwner::open(
        &host.root.0,
        &host.signer,
        "swarm",
        "owner",
        Limits::registered().unwrap(),
    )
    .unwrap();
    let h = host.clone();
    let owner = SharedManagedOwner::new(owner, move || Ok(h.clone()));
    let session = owner
        .session("member", "run", "deployment", allowance())
        .unwrap();
    let (done_tx, done_rx) = std::sync::mpsc::sync_channel(1);
    let caller = std::thread::spawn(move || {
        let result = session.send("held-command", "hello", json!({}));
        assert!(session.stop_response().is_none());
        done_tx.send(result).unwrap();
    });
    let patience = Duration::from_millis(allowance().millis());
    entered_rx.recv_timeout(patience).unwrap();
    let closing = owner.clone();
    let closer = std::thread::spawn(move || closing.close());
    let result = done_rx.recv_timeout(patience);
    // Release even on the failed assertion path so no callback/thread is stranded.
    drop(release);
    caller.join().unwrap();
    closer.join().unwrap().unwrap();
    assert!(
        result.unwrap().is_err(),
        "late held reply must not be published"
    );
    assert!(
        owner
            .session("member", "run", "deployment", allowance())
            .is_err()
    );
    let disk: Value =
        serde_json::from_slice(&fs::read(host.root.0.join("owner.json")).unwrap()).unwrap();
    assert!(
        disk["commands"]["held-command"].is_object(),
        "unresolved real intent survives local closure"
    );
    host.carrier.close().unwrap();
}

#[test]
fn revoked_transport_epoch_cannot_send_or_reactivate() {
    let host = make_host(false);
    let journal = ManagedControlOwner::open(
        &host.root.0,
        &host.signer,
        "swarm",
        "owner",
        Limits::registered().unwrap(),
    )
    .unwrap();
    let captured = host.clone();
    let owner = SharedManagedOwner::new(journal, move || Ok(captured.clone()));
    let session = owner
        .session("member", "run", "deployment", allowance())
        .unwrap();
    host.carrier.revoke();
    let result = session.send("must-not-send", "hello", json!({}));
    // This is a fully valid signed command: removing revoke yields a successful
    // controlled response, not a malformed fixture/panic masquerading as RED.
    assert!(
        result.is_err(),
        "revoked carrier published a valid signed response: {result:?}"
    );
    assert!(session.stop_response().is_none());
    owner.close().unwrap();
    host.carrier.close().unwrap();
    let end = host.monotonic().checked_add(allowance()).unwrap();
    assert_eq!(
        host.carrier
            .call(&host.clock, b"still closed", end, None)
            .unwrap_err()
            .0,
        "managed_owner_closed"
    );
}
