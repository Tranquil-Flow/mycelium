// SPDX-License-Identifier: AGPL-3.0-only
//! Real CLIENT journal + factory + signed QUIC receipt peer; no model, mailbox or native worker.
use super::*;
use protocol::identity::Identity;
use runtime::managed_owner::{
    Limits, ManagedControlOwner, OwnerError, OwnerHost,
    session::{CallFence, ManagedSessionFactory, SessionHost, SharedManagedOwner},
};
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    sync::{
        Mutex,
        atomic::{AtomicU64, Ordering},
    },
};
static NEXT: AtomicU64 = AtomicU64::new(0);
fn signer() -> Identity {
    Identity::from_secret("owner-endpoint", &[7; 32]).unwrap()
}
fn member() -> Value {
    json!({"node_id":"member","endpoint_id":"member-endpoint","verification_key_digest":format!("sha256:{}","a".repeat(64)),"generation":1,"incarnation":"inc-1","lifecycle_state":"RUNNING","peer_class":"mac_mlx_iroh","lease_expires_at":1000000.0})
}
fn binding() -> Value {
    json!({"target_node_id":"member","target_endpoint_id":"member-endpoint","target_key_digest":format!("sha256:{}","a".repeat(64)),"membership_generation":1,"member_incarnation":"inc-1","run_id":"run","deployment_id":"deployment"})
}
struct Root(PathBuf);
impl Root {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "l9-journal-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        Self(path)
    }
}
impl Drop for Root {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
struct JournalTime {
    start: Instant,
    offset: AtomicU64,
}
impl JournalTime {
    fn new() -> Self {
        Self {
            start: Instant::now(),
            offset: AtomicU64::new(100000),
        }
    }
    fn deadline(&self) -> MonoInstant {
        self.monotonic()
            .checked_add(B::RouterClose.span(Entries::ZERO).unwrap())
            .unwrap()
    }
}
impl Clock for JournalTime {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(
            ClockDomain(91),
            self.offset.load(Ordering::SeqCst) + self.start.elapsed().as_millis() as u64,
        )
    }
    fn wall_seconds(&self) -> f64 {
        1000.0
    }
}
struct Host<'a, 'h, 'r> {
    carrier: &'a NodeControlClient<'h, 'r, JournalTime>,
    clock: &'a JournalTime,
    identity: &'a Identity,
    peer: &'a EndpointAddr,
    authority: &'a Mutex<runtime::iroh_control::ControlAuthority>,
    fence: ConnectionFence,
    nonce: &'a AtomicU64,
    root: &'a Root,
    calls: &'a Mutex<Vec<Value>>,
}
impl Clock for Host<'_, '_, '_> {
    fn monotonic(&self) -> MonoInstant {
        self.clock.monotonic()
    }
    fn wall_seconds(&self) -> f64 {
        self.clock.wall_seconds()
    }
}
impl Host<'_, '_, '_> {
    fn exchange_local(
        &mut self,
        p: &Value,
        d: MonoInstant,
        local: Option<&CallFence>,
    ) -> Result<Value, OwnerError> {
        let s = &p["envelope"]["statement"];
        self.calls.lock().unwrap().push(p.clone());
        if p["operation"] == "enqueue" {
            let disk: Value =
                serde_json::from_slice(&fs::read(self.root.0.join("owner.json")).unwrap()).unwrap();
            assert_eq!(
                disk["commands"][s["native_command"]["command_id"].as_str().unwrap()]["enqueue_request"],
                *p,
                "journal signature persisted before actual carrier IO"
            );
        }
        let bytes = protocol::canonical::to_bytes(&p["envelope"]).unwrap();
        let key = self.identity.public_key_record();
        let expected_binding = binding();
        // Borrow a live watch handle, never keep the host's mutex across IO.
        let mut authority = self.authority.lock().unwrap().clone();
        ManagedControlClient::new(self.carrier, self.clock)
            .call_envelope(
                ControlRequest {
                    peer: self.peer,
                    fence: self.fence,
                    command: &bytes,
                    deadline: d,
                },
                &mut authority,
                &ReceiptBinding {
                    kind: if p["operation"] == "enqueue" {
                        ReceiptKind::Enqueue
                    } else {
                        ReceiptKind::Result
                    },
                    owner_key: &key,
                    request_id: s["request_id"].as_str().unwrap(),
                    swarm_id: "swarm",
                    owner_node_id: "owner",
                    binding: &expected_binding,
                    native_command: &s["native_command"],
                },
                local,
            )
            .map_err(|e| OwnerError(e.0))
    }
}
impl OwnerHost for Host<'_, '_, '_> {
    fn signer(&self) -> &Identity {
        self.identity
    }
    fn current_member(&mut self, _node: &str) -> Result<Value, OwnerError> {
        Ok(member())
    }
    fn nonce(&mut self) -> Result<String, OwnerError> {
        Ok(format!(
            "{:032x}",
            self.nonce.fetch_add(1, Ordering::SeqCst) + 1
        ))
    }
    fn exchange(&mut self, p: &Value, d: MonoInstant) -> Result<Value, OwnerError> {
        self.exchange_local(p, d, None)
    }
    fn wait(&mut self, a: budgets::Span, _d: MonoInstant) -> Result<(), OwnerError> {
        std::thread::sleep(Duration::from_millis(a.millis()));
        Ok(())
    }
}
impl SessionHost for Host<'_, '_, '_> {
    fn exchange_fenced(
        &mut self,
        p: &Value,
        d: MonoInstant,
        fence: &CallFence,
    ) -> Result<Value, OwnerError> {
        self.exchange_local(p, d, Some(fence))
    }
    fn wait_fenced(
        &mut self,
        a: budgets::Span,
        _d: MonoInstant,
        fence: &CallFence,
    ) -> Result<(), OwnerError> {
        fence.wait_for(a)
    }
}
pub(super) fn child(expected: EndpointId, mode: &str) {
    let rt = runtime();
    let clock = Time::new();
    let mut host = bind_host_protocols(
        &rt,
        &clock,
        clock.deadline(),
        iroh::SecretKey::from_bytes(&[94; 32]),
        &[Channel::NodeControl],
    )
    .unwrap();
    let address = host.address().unwrap();
    let socket = *address.ip_addrs().next().unwrap();
    println!(
        "L9_READY {}",
        json!({"id":address.id.to_string(),"socket":socket.to_string(),"pid":std::process::id()})
    );
    std::io::stdout().flush().unwrap();
    let key = signer();
    let mut result_count = 0;
    loop {
        let conn = host.accept(&clock, clock.deadline()).unwrap();
        let fence = ConnectionFence {
            endpoint_id: *expected.as_bytes(),
            local_generation: 3,
            remote_generation: 2,
        };
        let mut receiver = runtime::iroh_control_receive::NodeControlReceiver::new(
            conn.clone(),
            &rt,
            &clock,
            fence,
            || fence,
        )
        .unwrap();
        let delivery = receiver.receive(clock.deadline()).unwrap();
        let envelope =
            protocol::strict::object(delivery.bytes(), protocol::managed::MAX_FRAME_BYTES).unwrap();
        let s = &envelope["statement"];
        let enqueue = s["protocol"] == "mycelium.managed_control.owner_command.v1";
        protocol::managed::verify(
            &envelope,
            if enqueue {
                "mycelium.managed_control.owner_command.v1"
            } else {
                "mycelium.managed_control.owner_result.v1"
            },
            &key.public_key_record(),
            "owner-endpoint",
            1000.0,
            protocol::TimeLimits {
                max_ttl_seconds: B::ControlTtl.span(Entries::ZERO).unwrap().millis() as f64
                    / 1000.0,
                clock_skew_seconds: B::ControlClockSkew.span(Entries::ZERO).unwrap().millis()
                    as f64
                    / 1000.0,
            },
        )
        .unwrap();
        for (field, value) in binding().as_object().unwrap() {
            assert_eq!(
                s[field], *value,
                "independently pinned request scope {field}"
            );
        }
        assert_eq!(s["swarm_id"], "swarm");
        assert_eq!(s["native_command"]["run_id"], "run");
        assert_eq!(s["native_command"]["deployment_id"], "deployment");
        assert_eq!(s["native_command"]["command"], "hello");
        if !enqueue {
            result_count += 1;
        }
        let held = !enqueue && mode == "journal-hold";
        let state = if enqueue || (mode == "journal-pending" && result_count == 1) {
            "pending"
        } else if mode == "journal-failed" {
            "failed"
        } else {
            "completed"
        };
        let mut receipt = binding();
        receipt.as_object_mut().unwrap().extend(json!({"protocol":if enqueue{"mycelium.managed_control.enqueue_receipt.v1"}else{"mycelium.managed_control.result_receipt.v1"},"request_id":s["request_id"],"swarm_id":"swarm","owner_node_id":"owner","owner_endpoint_id":"owner-endpoint","issued_at":1000.0,"expires_at":1060.0,"command_id":s["native_command"]["command_id"],"command_digest":protocol::canonical::sha256_prefixed(&protocol::canonical::to_bytes(&s["native_command"]).unwrap()),"state":state}).as_object().unwrap().clone());
        if !enqueue {
            let native = if state == "completed" {
                json!({"protocol":protocol::native::CONTROL_PROTOCOL,"command_id":s["native_command"]["command_id"],"node_id":"member","ok":true,"route_ready":false,"result":{}})
            } else if state == "failed" {
                json!({"protocol":protocol::native::CONTROL_PROTOCOL,"command_id":s["native_command"]["command_id"],"node_id":"member","ok":false,"route_ready":false,"error":{"code":"controlled_failure"}})
            } else {
                Value::Null
            };
            receipt["native_response_digest"] = if native.is_null() {
                Value::Null
            } else {
                json!(protocol::canonical::sha256_prefixed(
                    &protocol::canonical::to_bytes(&native).unwrap()
                ))
            };
            receipt["native_response"] = native;
        }
        let signature = key.sign(&receipt).unwrap();
        if !enqueue && mode == "journal-forged" {
            receipt["run_id"] = json!("foreign");
        }
        let reply = json!({"protocol":protocol::managed::SIGNED_ENVELOPE_PROTOCOL,"statement":receipt,"signature":signature,"verification_key":key.public_key_record()});
        let hold = if held {
            println!("L9_RECEIVED true");
            std::io::stdout().flush().unwrap();
            Some(delivery)
        } else {
            delivery
                .respond(&protocol::canonical::to_bytes(&reply).unwrap())
                .unwrap();
            None
        };
        rt.block_on(async {
            let reason = tokio::time::timeout(
                Duration::from_millis(
                    B::NodeControlCallTimeoutCeiling
                        .span(Entries::ZERO)
                        .unwrap()
                        .millis(),
                ),
                conn.closed(),
            )
            .await
            .unwrap();
            let iroh::endpoint::ConnectionError::ApplicationClosed(close) = reason else {
                panic!("call did not close explicitly: {reason}")
            };
            assert_eq!(close.error_code, 7u32.into());
            assert_eq!(close.reason.as_ref(), b"control call ended");
        });
        drop(hold);
        drop(receiver);
        drop(conn);
        if !enqueue && (state != "pending" || held) {
            break;
        }
    }
    host.close(&clock, clock.deadline()).unwrap();
    drop(host);
    drop(rt);
    assert!(UdpSocket::bind(socket).is_ok());
    println!("L9_CLOSED true");
}
fn journal_exchange(
    mode: &str,
    close: Option<bool>,
) -> (
    Result<Value, runtime::managed_owner::session::SessionError>,
    usize,
    Duration,
) {
    let _pair = MANAGED_PAIR.lock().unwrap_or_else(|e| e.into_inner());
    let rt = runtime();
    let clock = JournalTime::new();
    let mut host = bind_host(&rt, &clock, clock.deadline()).unwrap();
    let socket = *host.address().unwrap().ip_addrs().next().unwrap();
    let peer = Peer::spawn(host.address().unwrap().id, mode);
    let address = peer.address.clone();
    let root = Root::new();
    let identity = signer();
    let fence = ConnectionFence {
        endpoint_id: *address.id.as_bytes(),
        local_generation: 2,
        remote_generation: 3,
    };
    let (_tx, rx) = tokio::sync::watch::channel(Some(fence));
    let authority = Mutex::new(rx);
    let nonce = AtomicU64::new(0);
    let calls = Mutex::new(Vec::new());
    let carrier = NodeControlClient::new(&host, &clock, Entries::new(1)).unwrap();
    let owner = SharedManagedOwner::new(
        ManagedControlOwner::open(
            &root.0,
            &identity,
            "swarm",
            "owner",
            Limits::registered().unwrap(),
        )
        .unwrap(),
        || {
            Ok(Host {
                carrier: &carrier,
                clock: &clock,
                identity: &identity,
                peer: &address,
                authority: &authority,
                fence,
                nonce: &nonce,
                root: &root,
                calls: &calls,
            })
        },
    );
    let factory =
        ManagedSessionFactory::new(owner.clone(), "run", "deployment", &[member()]).unwrap();
    let session = factory
        .open(&[], "member", "run", "deployment", 30.0)
        .unwrap();
    let (result, mut peer, elapsed) = if let Some(close) = close {
        std::thread::scope(|scope| {
            let control = scope.spawn(|| {
                let peer = peer;
                assert_eq!(peer.line("L9_RECEIVED "), "true");
                let started = Instant::now();
                if close {
                    owner.close().unwrap();
                } else {
                    session
                        .interrupt_command("journal-hello", "cancelled_locally")
                        .unwrap();
                }
                (peer, started)
            });
            let result = session.send("journal-hello", "hello", json!({}));
            let (peer, started) = control.join().unwrap();
            (result, peer, started.elapsed())
        })
    } else {
        (
            session.send("journal-hello", "hello", json!({})),
            peer,
            Duration::ZERO,
        )
    };
    assert_eq!(
        session.returncode(),
        None,
        "receipt peer is not native cleanup"
    );
    assert_eq!(session.stop_response(), None);
    owner.close().unwrap();
    drop(session);
    drop(factory);
    drop(owner);
    drop(carrier);
    host.close(&clock, clock.deadline()).unwrap();
    drop(host);
    drop(rt);
    assert_socket_released(socket, "journal carrier endpoint retained its socket");
    let count = calls.lock().unwrap().len();
    if count >= 2 {
        peer.finish();
        println!("L9_JOURNAL_REAPED pid={} mode={mode}", peer.child.id());
    } else {
        drop(peer);
    }
    (result, count, elapsed)
}
#[test]
fn real_journal_factory_signed_carrier_completes() {
    let (result, count, _) = journal_exchange("journal-valid", None);
    assert_eq!(
        result.expect("real journal/factory carrier must receive a verified raw envelope")["ok"],
        true
    );
    assert_eq!(count, 2);
}
#[test]
fn real_journal_pending_failed_and_forged_receipts_are_not_promoted() {
    let (result, count, _) = journal_exchange("journal-pending", None);
    assert_eq!(result.unwrap()["ok"], true);
    assert_eq!(count, 3, "pending must poll again");
    let (result, count, _) = journal_exchange("journal-failed", None);
    let response = result.unwrap();
    assert_eq!(response["ok"], false);
    assert_eq!(response["error"]["code"], "controlled_failure");
    assert_eq!(count, 2);
    let (result, count, _) = journal_exchange("journal-forged", None);
    assert_eq!(
        result.unwrap_err().0,
        "managed_control_response_signature_invalid"
    );
    assert_eq!(count, 2);
}
#[test]
fn real_journal_held_carrier_interrupt_is_prompt_and_never_publishes() {
    let (result, count, elapsed) = journal_exchange("journal-hold", Some(false));
    assert_eq!(result.unwrap_err().0, "cancelled_locally");
    assert_eq!(count, 2);
    assert!(
        elapsed < allowance(),
        "held carrier ignored local interrupt for {elapsed:?}"
    );
}
#[test]
fn real_journal_held_carrier_owner_close_is_prompt_and_local() {
    let (result, count, elapsed) = journal_exchange("journal-hold", Some(true));
    assert_eq!(result.unwrap_err().0, "managed_owner_closed");
    assert_eq!(count, 2);
    assert!(
        elapsed < allowance(),
        "held carrier ignored local owner close for {elapsed:?}"
    );
}
