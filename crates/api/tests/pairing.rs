// SPDX-License-Identifier: AGPL-3.0-only
//! pairing.py:200-248, app.py:1673-1677 @ f4e366e9. Real L5 Ed25519;
//! fake host lifecycle/clock/publication, no owner creation or grant issuance.
use api::{
    actions::{Action, Backend, Control},
    desktop::Service,
    schema::PublicError,
};
use protocol::identity::Identity;
use serde_json::{Map, Value, json};
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};
struct Host {
    target: bool,
    closed: bool,
    publications: Arc<AtomicUsize>,
}
impl Backend for Host {
    fn prepare(&mut self, _: Action, _: &Map<String, Value>) -> Result<(), PublicError> {
        if self.closed {
            return Err(PublicError::new(400, "application_closed"));
        }
        if self.target {
            return Err(PublicError::new(400, "owner_role_required"));
        }
        Ok(())
    }
    fn execute(&mut self, _: Action, _: &Map<String, Value>) -> Result<Value, PublicError> {
        panic!("signed verification must not delegate to host")
    }
    fn publish(&mut self) -> Result<(), PublicError> {
        self.publications.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
}
fn fixture() -> Value {
    let id = Identity::from_secret("endpoint-preview", &[7; 32]).unwrap();
    let mut value = json!({"protocol":"mycelium.pairing_request.v1","node_id":"node-preview","identity_endpoint_id":id.endpoint_id(),"identity_key_digest":id.verification_key_digest(),"identity_verification_key":id.public_key_record(),"peer_class":"mac_mlx_iroh","invite_protocol":"mycelium.invite_bundle.v1","recipient_encryption_key":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","pairing_nonce":"pair-nonce","issued_at":1000.0,"expires_at":1100.0});
    value["signature"] = id.sign(&value).unwrap();
    value
}
fn body(request: Value) -> Map<String, Value> {
    json!({"request":request}).as_object().unwrap().clone()
}
#[test]
fn real_signature_preview_and_tampering_obey_exact_public_contract() {
    let count = Arc::new(AtomicUsize::new(0));
    let service = Control::new(api::pairing::SignedBackend::new(
        Host {
            target: false,
            closed: false,
            publications: count.clone(),
        },
        || 1000.0,
    ));
    let signed = fixture();
    let preview = service
        .action("/api/pair/preview", &body(signed.clone()))
        .unwrap();
    assert_eq!(
        preview,
        json!({"node_id":"node-preview","identity_key_digest":signed["identity_key_digest"],"peer_class":"mac_mlx_iroh","expires_at":1100.0})
    );
    assert_eq!(count.load(Ordering::SeqCst), 1);
    let mut bad = signed;
    bad["node_id"] = json!("other");
    assert_eq!(
        service.action("/api/pair/preview", &body(bad)).unwrap_err(),
        PublicError::new(409, "pairing_request_signature_invalid")
    );
    assert_eq!(count.load(Ordering::SeqCst), 1);
}
#[test]
fn signed_preview_closed_shape_lifetime_and_refusal_precedence() {
    use api::pairing::preview;
    let signed = fixture();
    preview(&signed, 1100.0).unwrap(); // Source is > expiry, not >=.
    assert_eq!(
        preview(&signed, 1100.5).unwrap_err().code,
        "pairing_request_expired"
    );
    assert_eq!(
        preview(&signed, f64::NAN).unwrap_err().code,
        "pairing_time_invalid"
    );
    for (field, value, code) in [
        ("extra", json!(true), "pairing_request_invalid"),
        ("node_id", json!("bad/name"), "pairing_request_invalid"),
        (
            "recipient_encryption_key",
            json!("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB"),
            "pairing_handoff_invalid",
        ),
        ("issued_at", json!(true), "pairing_time_invalid"),
        ("expires_at", json!(1000), "pairing_request_invalid"),
        (
            "node_id",
            json!("other"),
            "pairing_request_signature_invalid",
        ),
        (
            "peer_class",
            json!("unsupported"),
            "pairing_request_signature_invalid",
        ),
    ] {
        let mut bad = signed.clone();
        bad[field] = value;
        assert_eq!(preview(&bad, 1200.0).unwrap_err().code, code, "{field}");
    }
    let id = Identity::from_secret("endpoint-preview", &[7; 32]).unwrap();
    let mut unsupported = signed;
    unsupported["peer_class"] = json!("unsupported");
    unsupported.as_object_mut().unwrap().remove("signature");
    unsupported["signature"] = id.sign(&unsupported).unwrap();
    assert_eq!(
        preview(&unsupported, 1200.0).unwrap_err().code,
        "pairing_request_invalid"
    );
    for (closed, target, code) in [
        (true, true, "application_closed"),
        (false, true, "owner_role_required"),
        (false, false, "invalid_request"),
    ] {
        let c = Control::new(api::pairing::SignedBackend::new(
            Host {
                closed,
                target,
                publications: Arc::new(AtomicUsize::new(0)),
            },
            || 1000.0,
        ));
        assert_eq!(
            c.action("/api/pair/preview", &Map::new()).unwrap_err().code,
            code
        );
    }
}
#[test]
fn two_unchanged_l3_pairing_observations_over_real_http() {
    use api::{
        desktop::{Desktop, Session},
        http::serve_connection,
        schema::Snapshot,
        status::StatusOwner,
    };
    use budgets::BudgetId;
    use journey_tests::{compare, duration, http::Client, project};
    use std::{
        net::{Ipv4Addr, TcpListener},
        thread,
        time::Instant,
    };
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let host = listener.local_addr().unwrap().to_string();
    let origin = format!("http://{host}");
    let owner = StatusOwner::new(1).unwrap();
    owner
        .refresh(|| {
            Ok((
                1,
                Snapshot::parse(
                    serde_json::from_str(include_str!("../contracts/status-example.json")).unwrap(),
                )
                .unwrap(),
            ))
        })
        .unwrap();
    let control = Control::new(api::pairing::SignedBackend::new(
        Host {
            target: false,
            closed: false,
            publications: Arc::new(AtomicUsize::new(0)),
        },
        || 1000.0,
    ));
    let session = Session::new(
        "launch".into(),
        "session".into(),
        Instant::now() + duration(BudgetId::WorkerStartup).unwrap(),
        duration(BudgetId::WorkerStartup).unwrap(),
    )
    .unwrap();
    let app = Desktop::new(host, Arc::new(control), owner.status(), session);
    let job = thread::spawn(move || {
        for _ in 0..3 {
            let (s, _) = listener.accept().unwrap();
            serve_connection(
                s,
                duration(BudgetId::ModelSelection).unwrap(),
                |r| app.preflight(r),
                |r| app.handle(r),
            )
            .unwrap();
        }
    });
    let mut c = Client::new(&origin).unwrap();
    c.sign_in("launch").unwrap();
    let request = fixture();
    let good = c
        .post("/api/pair/preview", &json!({"request":request}))
        .unwrap();
    let verified = good.json().unwrap();
    assert_eq!(verified["node_id"], request["node_id"]);
    assert_eq!(
        verified["identity_key_digest"],
        request["identity_key_digest"]
    );
    let mut tampered = request;
    tampered["node_id"] = json!("node-preview-tampered");
    let bad = c
        .post("/api/pair/preview", &json!({"request":tampered}))
        .unwrap();
    job.join().unwrap();
    let golden: Value = serde_json::from_str(include_str!(
        "../../../test-support/journeys/fixtures/python-v014.json"
    ))
    .unwrap();
    for (id, reply) in [
        ("valid_signed_request_preview", good),
        ("tampered_signed_request_refused", bad),
    ] {
        let expected = golden["observations"]
            .as_array()
            .unwrap()
            .iter()
            .find(|v| v["id"] == id)
            .unwrap();
        let keys = expected["fields"]
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>();
        let mut actual = expected.clone();
        actual["status"] = json!(reply.status);
        actual["fields"] = project(&reply.json().unwrap(), &keys).unwrap();
        compare(&json!([expected]), &json!([actual])).unwrap();
    }
    eprintln!(
        "L3_ADDITIONAL_OBSERVATIONS=2; WHOLE_L3_JOURNEYS=0; SIGNATURES=real_L5; HOST=fake_lifecycle_clock_publication"
    );
}
