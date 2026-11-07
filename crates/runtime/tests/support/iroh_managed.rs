// SPDX-License-Identifier: AGPL-3.0-only
// Recorded L5 signed receipts cross a real local QUIC connection. The peer is a
// receipt fixture, NOT ManagedControlOwner, a mailbox pump or a native worker.
use protocol::managed_receipt::ReceiptKind;
use runtime::iroh_managed::{ManagedControlClient, ReceiptBinding};
// Shared by recorded-receipt and journal-driven receipt fixtures. These are
// independent no-model pairs, not production carrier concurrency scenarios.
static MANAGED_PAIR: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn managed_row(name: &str) -> serde_json::Value {
    let rows: Vec<serde_json::Value> = serde_json::from_str(include_str!(
        "../../../protocol/fixtures/continuation-managed-receipts.json"
    ))
    .unwrap();
    rows.into_iter().find(|row| row["name"] == name).unwrap()
}
fn receipt_binding(row: &serde_json::Value) -> ReceiptBinding<'_> {
    let c = &row["context"];
    ReceiptBinding {
        kind: if c["operation"] == "enqueue" {
            ReceiptKind::Enqueue
        } else {
            ReceiptKind::Result
        },
        owner_key: &c["key"],
        request_id: c["request_id"].as_str().unwrap(),
        swarm_id: c["swarm_id"].as_str().unwrap(),
        owner_node_id: c["owner_node_id"].as_str().unwrap(),
        binding: &c["binding"],
        native_command: &c["command"],
    }
}
fn managed_request() -> serde_json::Value {
    // Existing L6 owner_result request shape, same pinned L5 fixture identity.
    let mut envelope = managed_row("poll-command")["value"]["command_envelope"].clone();
    envelope["statement"]["protocol"] = "mycelium.managed_control.owner_result.v1".into();
    let signer = protocol::identity::Identity::from_secret("endpoint-owner", &[17; 32]).unwrap();
    envelope["signature"] = signer.sign(&envelope["statement"]).unwrap();
    envelope
}
struct ManagedTime {
    inner: Time,
    wall: f64,
    revoke: Option<tokio::sync::watch::Sender<Option<ConnectionFence>>>,
    expire: bool,
}
impl Clock for ManagedTime {
    fn monotonic(&self) -> MonoInstant {
        self.inner.monotonic()
    }
    fn wall_seconds(&self) -> f64 {
        if let Some(revoke) = &self.revoke {
            revoke.send_replace(None);
        }
        if self.expire {
            self.inner.offset.set(
                B::NodeControlCallTimeoutCeiling
                    .span(Entries::ZERO)
                    .unwrap()
                    .millis(),
            );
        }
        self.wall
    }
}
fn managed_child(expected: EndpointId, mode: &str) {
    let rt = runtime();
    let clock = Time::new();
    let mut host = bind_host_protocols(
        &rt,
        &clock,
        clock.deadline(),
        iroh::SecretKey::from_bytes(&[93; 32]),
        &[Channel::NodeControl],
    )
    .unwrap();
    let address = host.address().unwrap();
    let socket = *address.ip_addrs().next().unwrap();
    println!(
        "L9_READY {}",
        serde_json::json!({"id":address.id.to_string(),"socket":socket.to_string(),"pid":std::process::id()})
    );
    std::io::stdout().flush().unwrap();
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
    // Existing valid signed owner-command fixture, unchanged over the carrier.
    let request = managed_request();
    protocol::managed::verify(
        &request,
        "mycelium.managed_control.owner_result.v1",
        &managed_row("result-completed")["context"]["key"],
        "endpoint-owner",
        1000.0,
        protocol::TimeLimits {
            max_ttl_seconds: B::ControlTtl.span(Entries::ZERO).unwrap().millis() as f64 / 1000.0,
            clock_skew_seconds: B::ControlClockSkew.span(Entries::ZERO).unwrap().millis() as f64
                / 1000.0,
        },
    )
    .unwrap();
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(delivery.bytes()).unwrap(),
        request
    );
    let row = managed_row(
        mode.strip_prefix("managed-row-")
            .unwrap_or("result-completed"),
    );
    let bytes = match mode {
        "managed-malformed" => b"not-json".to_vec(),
        "managed-forged" => {
            let mut value: serde_json::Value =
                serde_json::from_str(row["envelope_utf8"].as_str().unwrap()).unwrap();
            value["statement"]["run_id"] = "foreign-run".into();
            serde_json::to_vec(&value).unwrap()
        }
        _ => row["envelope_utf8"].as_str().unwrap().as_bytes().to_vec(),
    };
    let held = if mode == "managed-hold" {
        Some(delivery)
    } else if mode == "managed-close" {
        conn.close(7u32.into(), b"fixture closed before reply");
        Some(delivery)
    } else {
        delivery.respond(&bytes).unwrap();
        None
    };
    if mode != "managed-close" {
        rt.block_on(async {
            let patience = Duration::from_millis(
                B::NodeControlCallTimeoutCeiling
                    .span(Entries::ZERO)
                    .unwrap()
                    .millis(),
            );
            let reason = tokio::time::timeout(patience, conn.closed()).await.unwrap();
            let iroh::endpoint::ConnectionError::ApplicationClosed(close) = reason else {
                panic!("expected explicit call close: {reason}");
            };
            assert_eq!(close.error_code, 7u32.into());
            assert_eq!(close.reason.as_ref(), b"control call ended");
        });
    }
    drop(held);
    drop(receiver);
    // A received close enters noq's Draining state before its paced reciprocal
    // close necessarily leaves the socket. Iroh 1.0.2 endpoint.close() may then
    // abort that pending transmit. Keep this fixture's executor alive until the
    // initiator has completed its ORIGINAL endpoint close, not a second close.
    // The reader does not block the current-thread executor; both this wait and
    // our endpoint close share the original, unchanged RouterClose allowance.
    let close_deadline = clock.deadline();
    let (tx, rx) = tokio::sync::oneshot::channel();
    let reader = std::thread::spawn(move || {
        let mut line = String::new();
        std::io::stdin().read_line(&mut line).unwrap();
        tx.send(line).unwrap();
    });
    let done = rt.block_on(async {
        tokio::time::timeout(allowance(), rx)
            .await
            .expect("initiator endpoint close deadline")
            .expect("initiator endpoint close reader")
    });
    reader.join().unwrap();
    assert_eq!(done.trim(), "initiator_closed");
    host.close(&clock, close_deadline).unwrap();
    assert!(
        conn.stats().frame_tx.connection_close > 0,
        "peer endpoint shutdown discarded reciprocal CONNECTION_CLOSE"
    );
    drop(conn);
    drop(host);
    drop(rt);
    println!("L9_CLOSED true");
}
fn managed_exchange(
    mode: &str,
    row: &serde_json::Value,
    wall: f64,
) -> Result<serde_json::Value, runtime::iroh_control::ControlError> {
    managed_exchange_kind(mode, row, wall, false)
}
fn managed_exchange_kind(
    mode: &str,
    row: &serde_json::Value,
    wall: f64,
    raw_envelope: bool,
) -> Result<serde_json::Value, runtime::iroh_control::ControlError> {
    // These are independent receipt fixtures, not a carrier concurrency test.
    // Keep one managed fixture pair active: overlapping pairs intermittently
    // exceeded the unchanged RouterClose allowance (EV-1147/1153/1154). Other
    // carrier scenarios still run concurrently; no product deadline is relaxed.
    let _pair = MANAGED_PAIR
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    let rt = runtime();
    let mut clock = ManagedTime {
        inner: Time::new(),
        wall,
        revoke: None,
        expire: mode == "managed-expire",
    };
    let mut host = bind_host(&rt, &clock, clock.inner.deadline()).unwrap();
    let socket = *host.address().unwrap().ip_addrs().next().unwrap();
    let mut peer = Peer::spawn(host.address().unwrap().id, mode);
    let fence = ConnectionFence {
        endpoint_id: *peer.address.id.as_bytes(),
        local_generation: 2,
        remote_generation: 3,
    };
    let (tx, mut authority) = tokio::sync::watch::channel(Some(fence));
    if mode == "managed-revoke" {
        clock.revoke = Some(tx.clone());
    }
    let raw = NodeControlClient::new(&host, &clock, Entries::new(1)).unwrap();
    let client = ManagedControlClient::new(&raw, &clock);
    let command = protocol::canonical::to_bytes(&managed_request()).unwrap();
    let request = ControlRequest {
        peer: &peer.address,
        fence,
        command: &command,
        deadline: clock.inner.deadline(),
    };
    let result = if raw_envelope {
        client.call_envelope(request, &mut authority, &receipt_binding(row), None)
    } else {
        client.call(request, &mut authority, &receipt_binding(row))
    };
    drop(raw);
    let close_started = Instant::now();
    let closed = host.close(&clock, clock.inner.deadline());
    closed.unwrap_or_else(|error| {
        panic!(
            "managed fixture {mode} close failed after {:?}: {error:?}",
            close_started.elapsed()
        )
    });
    drop(host);
    drop(rt);
    assert_socket_released(socket, "managed carrier endpoint retained its socket");
    let input = peer.child.stdin.as_mut().unwrap();
    writeln!(input, "initiator_closed").unwrap();
    input.flush().unwrap();
    peer.finish();
    result
}
#[test]
fn managed_raw_envelope_shares_strict_receipt_and_final_admission() {
    for name in [
        "result-completed",
        "result-pending",
        "result-failed",
        "result-reconciliation_required",
    ] {
        let row = managed_row(name);
        let envelope: serde_json::Value =
            serde_json::from_str(row["envelope_utf8"].as_str().unwrap()).unwrap();
        assert_eq!(
            managed_exchange_kind(&format!("managed-row-{name}"), &row, 1000.0, true).unwrap(),
            envelope
        );
    }
    let row = managed_row("result-completed");
    for (mode, wall, code) in [
        ("managed-revoke", 1000.0, "cancelled"),
        ("managed-expire", 1000.0, "control_read_timeout"),
        (
            "managed-forged",
            1000.0,
            "managed_control_response_signature_invalid",
        ),
        (
            "managed-malformed",
            1000.0,
            "managed_control_response_invalid",
        ),
        ("managed-valid", 2000.0, "managed_control_message_expired"),
    ] {
        assert_eq!(
            managed_exchange_kind(mode, &row, wall, true).unwrap_err().0,
            code,
            "{mode}"
        );
    }
    let mut wrong = row.clone();
    wrong["context"]["binding"]["run_id"] = "foreign-run".into();
    assert_eq!(
        managed_exchange_kind("managed-valid", &wrong, 1000.0, true)
            .unwrap_err()
            .0,
        "managed_control_response_invalid"
    );
}
#[test]
fn managed_signed_receipt_final_admission_rechecks_deadline_and_authority() {
    let row = managed_row("result-completed");
    assert_eq!(
        managed_exchange("managed-revoke", &row, 1000.0)
            .unwrap_err()
            .0,
        "cancelled"
    );
    assert_eq!(
        managed_exchange("managed-expire", &row, 1000.0)
            .unwrap_err()
            .0,
        "control_read_timeout"
    );
}
#[test]
fn managed_signed_receipt_timeout_and_peer_close_never_fabricate_stop() {
    let row = managed_row("result-completed");
    assert_eq!(
        managed_exchange("managed-hold", &row, 1000.0)
            .unwrap_err()
            .0,
        "control_read_timeout"
    );
    assert_eq!(
        managed_exchange("managed-close", &row, 1000.0)
            .unwrap_err()
            .0,
        "control_read_failed"
    );
}
#[test]
fn managed_signed_receipt_keeps_pending_failed_and_reconciliation_states() {
    for name in [
        "result-pending",
        "result-claimed",
        "result-failed",
        "result-reconciliation_required",
        "result-expired",
    ] {
        let row = managed_row(name);
        assert_eq!(
            managed_exchange(&format!("managed-row-{name}"), &row, 1000.0).unwrap(),
            row["value"]
        );
    }
}
#[test]
fn managed_signed_receipt_crosses_real_quic_unchanged() {
    let row = managed_row("result-completed");
    assert_eq!(
        managed_exchange("managed-valid", &row, 1000.0).unwrap(),
        row["value"]
    );
}
#[test]
fn managed_signed_receipt_rejects_foreign_identity_run_deployment_and_request() {
    for field in [
        "target_node_id",
        "run_id",
        "deployment_id",
        "membership_generation",
    ] {
        let mut row = managed_row("result-completed");
        row["context"]["binding"][field] = "foreign".into();
        assert_eq!(
            managed_exchange("managed-valid", &row, 1000.0)
                .unwrap_err()
                .0,
            "managed_control_response_invalid",
            "{field}"
        );
    }
    let mut row = managed_row("result-completed");
    row["context"]["key"] = protocol::identity::Identity::from_secret("endpoint-owner", &[18; 32])
        .unwrap()
        .public_key_record();
    assert_eq!(
        managed_exchange("managed-valid", &row, 1000.0)
            .unwrap_err()
            .0,
        "managed_control_response_signature_invalid"
    );
    let mut row = managed_row("result-completed");
    row["context"]["request_id"] = "foreign-request".into();
    assert_eq!(
        managed_exchange("managed-valid", &row, 1000.0)
            .unwrap_err()
            .0,
        "managed_control_response_invalid"
    );
}
#[test]
fn managed_signed_receipt_rejects_forgery_malformed_and_expired() {
    let row = managed_row("result-completed");
    assert_eq!(
        managed_exchange("managed-forged", &row, 1000.0)
            .unwrap_err()
            .0,
        "managed_control_response_signature_invalid"
    );
    assert!(managed_exchange("managed-malformed", &row, 1000.0).is_err());
    assert_eq!(
        managed_exchange("managed-valid", &row, 2000.0)
            .unwrap_err()
            .0,
        "managed_control_message_expired"
    );
    // The executed L5 managed-vectors expiry boundary is 1330.0 / 1330.001.
    assert_eq!(
        managed_exchange("managed-valid", &row, 1330.001)
            .unwrap_err()
            .0,
        "managed_control_message_expired"
    );
}
