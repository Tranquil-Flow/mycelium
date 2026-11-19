// SPDX-License-Identifier: AGPL-3.0-only
//! The owner-pin field of the owner-signed run announcement (PH3B U2, slice S4b): an ADDITIVE,
//! versioned key `owner_control` that carries what `shard_member --owner-control` carries (the
//! owner's member-control endpoint key, never an address). Contract: plans/S4B-PLAN.md section 0.
//! Model-free; the signed read response is produced by the real `SignedRoutes` callback.
use api::{
    app_host,
    joined_member::work::{self, OwnerTrust, Pins, admit_command},
    member_control,
    run_exchange::{
        Announce, RunExchange,
        owner_pin::{FIELD, OwnerControl, OwnerPin, REFUSALS, VerifiedAnnouncement},
    },
    run_exchange_client::{ExchangeClient, Membership},
    run_exchange_routes::{DISCOVERY, PATH_READ, READ, RESPONSE, SignedRoutes},
    seed::{SeedClient, SeedOptions},
    wire::Text,
};
use protocol::{TimeLimits, identity::Identity, managed};
use serde_json::{Value, json};
#[allow(dead_code)]
#[path = "support/member_remote.rs"]
mod member_remote;

#[path = "owner_pin/result_retention.rs"]
mod result_retention;

fn oracle() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_signed_exchange.json")).unwrap()
}
/// The fixture's owner exchange (`signed_exchange.rs`): announcement `collect`, run-1 / dep-1.
fn owner_exchange(fixture: &Value) -> (RunExchange, String, f64) {
    let row = &fixture["rows"][0];
    let now = row["now"].as_f64().unwrap();
    let exchange = RunExchange::new(Box::new(move || now));
    let node = row["member"]["node_id"].as_str().unwrap().to_owned();
    exchange.announce(Announce { phase:"collect".into(),run_id:Some("run-1".into()),deployment_id:Some("dep-1".into()),per_node:json!({node.clone():{"bootstrap_offer_id":"offer-one"},"other-node":{"bootstrap_offer_id":"offer-secret"}}).as_object().unwrap().clone(),..Default::default() }).unwrap();
    (exchange, node, now)
}
fn owner_signer() -> Identity {
    Identity::from_secret("owner-fixed", &[7; 32]).unwrap()
}
fn limits() -> TimeLimits {
    TimeLimits {
        max_ttl_seconds: 300.0,
        clock_skew_seconds: 30.0,
    }
}
/// The seed-signed read response for the fixture's member (real `SignedRoutes::dispatch_guarded`).
fn signed_read(exchange: &RunExchange, fixture: &Value, now: f64) -> Value {
    let signer = owner_signer();
    let observe = |_: &str, _: f64, _: Option<f64>| {};
    let routes = SignedRoutes {
        exchange,
        signer: &signer,
        swarm_id: fixture["swarm_id"].as_str().unwrap(),
        owner_node_id: fixture["owner_node_id"].as_str().unwrap(),
        observe_clock: &observe,
        response_ttl: budgets::Span::from_millis(60_000),
    };
    let row = &fixture["rows"][0];
    assert_eq!(row["path"], PATH_READ);
    routes
        .dispatch_guarded(PATH_READ, &row["envelope"], &row["member"], now)
        .unwrap()
}

/// Existing bytes stay: an owner that never sets the pin answers the executed v0.1.4 oracle's
/// announcement exactly (same keys, same canonical bytes once the per-process incarnation is
/// set aside, as `signed_exchange.rs` does), in the same insertion order, under a valid
/// signature, and the response statement keeps its closed key set.
#[test]
fn an_announcement_without_the_pin_keeps_the_oracle_bytes_and_a_valid_signature() {
    let fixture = oracle();
    let (exchange, node, now) = owner_exchange(&fixture);
    let response = signed_read(&exchange, &fixture, now);
    let signer = owner_signer();
    let statement = managed::verify(
        &response,
        RESPONSE,
        &signer.public_key_record(),
        signer.endpoint_id(),
        now,
        limits(),
    )
    .expect("the response signature stays valid");
    let mut announcement = statement["announcement"].clone();
    let incarnation = announcement
        .as_object_mut()
        .unwrap()
        .remove("owner_incarnation")
        .unwrap();
    let expected = &fixture["rows"][0]["expected"]["ok"];
    assert_eq!(announcement, expected["announcement"]);
    assert_eq!(
        protocol::canonical::to_bytes(&announcement).unwrap(),
        protocol::canonical::to_bytes(&expected["announcement"]).unwrap(),
        "canonical announcement bytes"
    );
    let keys = |v: &Value| v.as_object().unwrap().keys().cloned().collect::<Vec<_>>();
    assert_eq!(keys(&statement), keys(expected), "closed response keys");
    // The owner's own serialisation (insertion order, `json.dumps` text), pinned at base 981c025.
    let dumped = String::from_utf8(exchange.announcement(Some(&node)).dumps(Text::Ascii))
        .unwrap()
        .replace(incarnation.as_str().unwrap(), "owner-process-INCARNATION");
    assert_eq!(
        dumped,
        r#"{"protocol":"mycelium.a13_demo_run_announcement.v1","phase":"collect","run_id":"run-1","deployment_id":"dep-1","model":null,"slice_mode":false,"manifest_digest":null,"work_limits":null,"issued_at":1793267919.837374,"owner_incarnation":"owner-process-INCARNATION","bootstrap_offer_id":"offer-one","node_slice_plan":null,"member_resources":null,"model_source":null}"#
    );
}

// ---- U2 RED: the field, the member-side check, the control endpoint (seam: owner_pin_unimplemented) ----

/// RFC 4648 base32 without padding, lowercase (iroh parses it as a key too).
fn base32(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 32] = b"abcdefghijklmnopqrstuvwxyz234567";
    let (mut out, mut buffer, mut bits) = (String::new(), 0u32, 0u32);
    for byte in bytes {
        buffer = (buffer << 8) | u32::from(*byte);
        bits += 8;
        while bits >= 5 {
            bits -= 5;
            out.push(ALPHABET[((buffer >> bits) & 31) as usize] as char);
        }
    }
    if bits > 0 {
        out.push(ALPHABET[((buffer << (5 - bits)) & 31) as usize] as char);
    }
    out
}
fn pin_key(seed: u8) -> iroh::EndpointId {
    iroh::SecretKey::from_bytes(&[seed; 32]).public()
}
const MEMBER_NODE: &str = "remote-mac-s4b-member";
/// A joined member's signed exchange client: fresh enrolled key, the fixture's swarm, the owner
/// seed key of `owner_signer()` pinned as at join. No seed is dialled.
fn member_client(now: f64) -> ExchangeClient {
    let fixture = oracle();
    let signer = Identity::from_secret("node-identity-s4b-member", &[11; 32]).unwrap();
    let owner = owner_signer().public_key_record();
    let seed = SeedClient::new(
        "https://127.0.0.1:9",
        fixture["swarm_id"].as_str().unwrap(),
        owner["verification_key_digest"].as_str().unwrap(),
        std::slice::from_ref(&owner),
        &SeedOptions {
            ca_pem: Some(Vec::new()),
            ..SeedOptions::default()
        },
    )
    .unwrap();
    ExchangeClient::new(
        seed,
        std::sync::Arc::new(signer),
        Membership {
            swarm_id: fixture["swarm_id"].as_str().unwrap().into(),
            owner_node_id: fixture["owner_node_id"].as_str().unwrap().into(),
            owner_key: owner,
            node_id: MEMBER_NODE.into(),
            generation: 1,
            incarnation: "remote-incarnation-s4b".into(),
        },
        Box::new(move || now),
        Box::new(|| Some("nonce-s4b".into())),
    )
}
/// The seed's current row for that member (what `with_current_member` hands the route).
fn member_row(client: &ExchangeClient, now: f64) -> Value {
    let m = client.member();
    json!({"node_id":m.node_id,"endpoint_id":client.signer().endpoint_id(),
        "verification_key_digest":client.signer().verification_key_digest(),
        "generation":m.generation,"incarnation":m.incarnation,"peer_class":"mac_mlx_iroh",
        "lifecycle_state":"RUNNING","lease_expires_at":now + 300.0})
}
/// The member's signed read (as `ExchangeClient::read` builds it) answered by the real owner
/// route; returns (response, request id).
fn member_read(exchange: &RunExchange, client: &ExchangeClient, now: f64) -> (Value, String) {
    let fixture = oracle();
    let request_id = "request-s4b".to_owned();
    let ttl = budgets::BudgetId::ControlTtl
        .span(budgets::Entries::ZERO)
        .unwrap()
        .millis() as f64
        / 1000.0;
    let mut statement = Value::Object(client.binding(DISCOVERY, DISCOVERY));
    for (k, v) in json!({"protocol": READ, "swarm_id": client.member().swarm_id,
        "request_id": request_id, "sequence": 1, "nonce": "nonce-s4b", "issued_at": now,
        "expires_at": now + ttl})
    .as_object()
    .unwrap()
    {
        statement[k] = v.clone();
    }
    let signer = owner_signer();
    let observe = |_: &str, _: f64, _: Option<f64>| {};
    let routes = SignedRoutes {
        exchange,
        signer: &signer,
        swarm_id: fixture["swarm_id"].as_str().unwrap(),
        owner_node_id: fixture["owner_node_id"].as_str().unwrap(),
        observe_clock: &observe,
        response_ttl: budgets::Span::from_millis(60_000),
    };
    let response = routes
        .dispatch_guarded(
            PATH_READ,
            &client.sign(&statement).unwrap(),
            &member_row(client, now),
            now,
        )
        .unwrap();
    (response, request_id)
}
/// The same response statement after `edit`, signed again by `by` (a misbehaving owner, or a
/// stranger).
fn resigned(response: &Value, by: &Identity, edit: impl FnOnce(&mut Value)) -> Value {
    let mut statement = response["statement"].clone();
    edit(&mut statement);
    json!({"protocol": managed::SIGNED_ENVELOPE_PROTOCOL, "statement": statement,
        "signature": by.sign(&statement).unwrap(), "verification_key": by.public_key_record()})
}
fn pin_of(
    client: &ExchangeClient,
    response: &Value,
    request_id: &str,
) -> Result<OwnerPin, &'static str> {
    let verified =
        VerifiedAnnouncement::from_response(client, response, request_id).map_err(|e| e.0)?;
    OwnerPin::from_announcement(&verified).map_err(|e| e.0)
}

/// The owner publishes the pin as the closed, versioned field, appended after the existing keys,
/// under its signature; every other byte stays the oracle's; withdrawing it restores them.
#[test]
fn a_set_owner_pin_is_published_last_under_the_owner_signature() {
    let fixture = oracle();
    let (exchange, node, now) = owner_exchange(&fixture);
    let key = pin_key(21);
    exchange.set_owner_control(Some(OwnerControl::new(key)));
    let response = signed_read(&exchange, &fixture, now);
    let signer = owner_signer();
    let verify = |r: &Value| {
        managed::verify(
            r,
            RESPONSE,
            &signer.public_key_record(),
            signer.endpoint_id(),
            now,
            limits(),
        )
    };
    let statement = verify(&response).expect("signed with the pin");
    let mut announcement = statement["announcement"].clone();
    let map = announcement.as_object_mut().unwrap();
    assert_eq!(
        map.remove(FIELD).expect("the pin is published"),
        json!({"protocol": "mycelium.a13_demo_owner_control.v1", "endpoint_id": key.to_string()})
    );
    map.remove("owner_incarnation");
    assert_eq!(
        announcement,
        fixture["rows"][0]["expected"]["ok"]["announcement"]
    );
    let dumped = String::from_utf8(exchange.announcement(Some(&node)).dumps(Text::Ascii)).unwrap();
    assert!(
        dumped.ends_with(&format!(
            r#""model_source":null,"owner_control":{{"protocol":"mycelium.a13_demo_owner_control.v1","endpoint_id":"{key}"}}}}"#
        )),
        "{dumped}"
    );
    // The signature covers the pin: any edit of it is refused.
    let mut forged = response.clone();
    forged["statement"]["announcement"][FIELD]["endpoint_id"] = json!(pin_key(22).to_string());
    assert_eq!(
        verify(&forged).unwrap_err().0,
        "managed_control_signature_invalid"
    );
    exchange.set_owner_control(None);
    let again = verify(&signed_read(&exchange, &fixture, now)).unwrap();
    assert!(
        again["announcement"].get(FIELD).is_none(),
        "withdrawn: absent, not null"
    );
}

/// The member takes the pin only from a response it verified against the seed key pinned at
/// join, and only in the closed v1 form; every refusal is named, in the contract's order.
#[test]
fn a_member_takes_the_pin_only_from_a_verified_well_formed_announcement() {
    let fixture = oracle();
    let (exchange, _, now) = owner_exchange(&fixture);
    let client = member_client(now);
    let key = pin_key(31);
    let owner = owner_signer();
    let mut seen = std::collections::BTreeSet::new();
    let mut refused = |r: Result<OwnerPin, &'static str>, code: &'static str, case: &str| {
        assert_eq!(r.as_ref().map(OwnerPin::endpoint), Err(&code), "{case}");
        seen.insert(code);
    };
    // No pin published.
    let (response, id) = member_read(&exchange, &client, now);
    refused(
        pin_of(&client, &response, &id),
        "owner_pin_missing",
        "absent",
    );
    // The pinned owner key, from a verified response.
    exchange.set_owner_control(Some(OwnerControl::new(key)));
    let (response, id) = member_read(&exchange, &client, now);
    let pin = pin_of(&client, &response, &id).expect("verified pin");
    assert_eq!(pin.endpoint(), key);
    assert_eq!(pin.seed_key_digest(), owner.verification_key_digest());
    // 1. Not verified: edited without the owner key, re-signed by a stranger, another request.
    let mut edited = response.clone();
    edited["statement"]["announcement"][FIELD]["endpoint_id"] = json!(pin_key(32).to_string());
    refused(
        pin_of(&client, &edited, &id),
        "owner_pin_announcement_unverified",
        "edited",
    );
    let stranger = Identity::from_secret(owner.endpoint_id(), &[12; 32]).unwrap();
    let foreign = resigned(&response, &stranger, |s| {
        s["announcement"][FIELD]["endpoint_id"] = json!(pin_key(32).to_string())
    });
    refused(
        pin_of(&client, &foreign, &id),
        "owner_pin_announcement_unverified",
        "stranger",
    );
    refused(
        pin_of(&client, &response, "request-other"),
        "owner_pin_announcement_unverified",
        "request",
    );
    // 2. Verified but not a closed demo-run state: an extra response key, an unknown phase.
    let extra = resigned(&response, &owner, |s| s["extra"] = json!(1));
    refused(
        pin_of(&client, &extra, &id),
        "demo_announcement_invalid",
        "extra key",
    );
    let phase = resigned(&response, &owner, |s| {
        s["announcement"]["phase"] = json!("bogus")
    });
    refused(
        pin_of(&client, &phase, &id),
        "demo_announcement_invalid",
        "phase",
    );
    // 4-6. The field itself, signed by the owner key (a misbehaving or newer owner).
    let v1 = "mycelium.a13_demo_owner_control.v1";
    for (field, code) in [
        (json!(null), "owner_pin_invalid"),
        (json!(key.to_string()), "owner_pin_invalid"),
        (
            json!({"protocol": 1, "endpoint_id": key.to_string()}),
            "owner_pin_invalid",
        ),
        (
            json!({"protocol": "mycelium.a13_demo_owner_control.v2", "endpoint_id": key.to_string()}),
            "owner_pin_version_unsupported",
        ),
        (json!({"protocol": v1}), "owner_pin_invalid"),
        (
            json!({"protocol": v1, "endpoint_id": key.to_string(), "addrs": ["192.168.1.2:4433"]}),
            "owner_pin_invalid",
        ),
        (
            json!({"protocol": v1, "endpoint_id": 7}),
            "owner_pin_endpoint_invalid",
        ),
        (
            json!({"protocol": v1, "endpoint_id": "zz"}),
            "owner_pin_endpoint_invalid",
        ),
        (
            json!({"protocol": v1, "endpoint_id": key.to_string().to_uppercase()}),
            "owner_pin_endpoint_invalid",
        ),
        // The same key in iroh's other accepted text form (52-char base32): one encoding only.
        (
            json!({"protocol": v1, "endpoint_id": base32(key.as_bytes())}),
            "owner_pin_endpoint_invalid",
        ),
        (
            json!({"protocol": v1, "endpoint_id": format!(" {key}")}),
            "owner_pin_endpoint_invalid",
        ),
    ] {
        let signed = resigned(&response, &owner, |s| {
            s["announcement"][FIELD] = field.clone()
        });
        refused(pin_of(&client, &signed, &id), code, &field.to_string());
    }
    let mut all = REFUSALS.to_vec();
    all.sort();
    assert_eq!(
        seen.into_iter().collect::<Vec<_>>(),
        all,
        "every refusal reached"
    );
}

/// The member's control endpoint obeys only the announced, verified owner key: none before a pin,
/// the pinned key after, a re-pin moves it, and a verified announcement without a pin clears it.
/// The operator's CLI pin keeps today's rule.
#[test]
fn the_member_control_endpoint_obeys_only_the_announced_owner_key() {
    let fixture = oracle();
    let (exchange, _, now) = owner_exchange(&fixture);
    let client = member_client(now);
    let me = pin_key(40);
    let (a, b) = (
        iroh::SecretKey::from_bytes(&[41; 32]),
        iroh::SecretKey::from_bytes(&[42; 32]),
    );
    let command = |owner: &iroh::SecretKey| {
        member_control::Owner::new(owner)
            .unwrap()
            .command(&me, json!({"command":"status"}), app_host::now())
            .unwrap()
    };
    let pins = Pins::new();
    let trust = OwnerTrust::Announced(pins.clone());
    let admit = |remote: &iroh::SecretKey, signed_by: &iroh::SecretKey| {
        admit_command(
            &trust,
            remote.public(),
            &command(signed_by),
            &me,
            0,
            app_host::now(),
        )
        .map(|(_, request)| request["command"].clone())
    };
    assert_eq!(
        admit(&a, &a),
        Err("owner_pin_missing".to_owned()),
        "no pin yet"
    );
    let read = |control: Option<OwnerControl>| {
        exchange.set_owner_control(control);
        let (response, id) = member_read(&exchange, &client, now);
        VerifiedAnnouncement::from_response(&client, &response, &id).unwrap()
    };
    assert_eq!(
        pins.repin(&read(Some(OwnerControl::new(a.public()))))
            .unwrap()
            .endpoint(),
        a.public()
    );
    assert_eq!(pins.current().map(|p| p.endpoint()), Some(a.public()));
    assert_eq!(admit(&a, &a), Ok(json!("status")));
    assert_eq!(
        admit(&b, &b),
        Err("member_control_owner_unpinned".to_owned()),
        "other key"
    );
    assert_eq!(
        admit(&a, &b),
        Err("member_control_owner_unpinned".to_owned()),
        "signed by other"
    );
    assert_eq!(
        admit(&b, &a),
        Err("member_control_owner_unpinned".to_owned()),
        "other connection"
    );
    pins.repin(&read(Some(OwnerControl::new(b.public()))))
        .unwrap();
    assert_eq!(admit(&b, &b), Ok(json!("status")), "re-pinned");
    assert_eq!(
        admit(&a, &a),
        Err("member_control_owner_unpinned".to_owned()),
        "old pin gone"
    );
    assert_eq!(pins.repin(&read(None)).unwrap_err().0, "owner_pin_missing");
    assert_eq!(pins.current(), None, "fail closed");
    assert_eq!(admit(&b, &b), Err("owner_pin_missing".to_owned()));
    let operator = OwnerTrust::Operator(a.public());
    assert_eq!(
        admit_command(&operator, a.public(), &command(&a), &me, 0, app_host::now())
            .map(|(_, r)| r["command"].clone()),
        Ok(json!("status"))
    );
}

/// Over a real loopback NodeControl endpoint: the library control loop answers the announced
/// owner's signed call and drops a call from any other key.
#[test]
fn the_control_loop_answers_the_announced_owner_and_drops_any_other() {
    use budgets::{BudgetId as B, Entries};
    use runtime::iroh_host::LoopbackHost;
    use std::sync::{Arc, atomic::AtomicBool, mpsc};
    use transport::{Channel, Clock};
    let fixture = oracle();
    let (exchange, _, now) = owner_exchange(&fixture);
    let client = member_client(now);
    let (a, b) = (
        iroh::SecretKey::from_bytes(&[51; 32]),
        iroh::SecretKey::from_bytes(&[52; 32]),
    );
    exchange.set_owner_control(Some(OwnerControl::new(a.public())));
    let (response, id) = member_read(&exchange, &client, now);
    let pins = Pins::new();
    pins.repin(&VerifiedAnnouncement::from_response(&client, &response, &id).unwrap())
        .unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    let (ready_tx, ready_rx) = mpsc::sync_channel(1);
    let flag = stop.clone();
    let member = std::thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .unwrap();
        let clock = api::managed_carrier::HostClock::default();
        let until = |id: B| {
            clock
                .monotonic()
                .checked_add(id.span(Entries::ZERO).unwrap())
                .unwrap()
        };
        let mut host = LoopbackHost::bind_protocols(
            &rt,
            &clock,
            until(B::RouterClose),
            iroh::SecretKey::generate(),
            &[Channel::NodeControl],
        )
        .unwrap();
        ready_tx.send(host.address().unwrap()).unwrap();
        let (dispatcher, _requests) = work::Dispatcher::new().unwrap();
        let status = work::Live::new(json!({"joined":true}));
        let signer = Identity::from_secret("member-app", &[53; 32]).unwrap();
        let me = host.address().unwrap().id;
        work::control_loop(
            &rt,
            &clock,
            &mut host,
            &OwnerTrust::Announced(pins),
            me,
            &dispatcher,
            &signer,
            &status,
            &flag,
        )
        .unwrap();
        host.close(&clock, until(B::RouterClose)).unwrap();
    });
    let addr = ready_rx.recv().unwrap();
    let call = |owner: &iroh::SecretKey| {
        member_remote::with_owner(owner, |o, c, deadline| {
            member_control::Remote {
                addr: addr.clone(),
                enrolled: None,
            }
            .call(
                o,
                c,
                json!({"command":"status"}),
                deadline(),
                app_host::now(),
            )
            .map_err(|e| e.code)
        })
    };
    let answered = call(&a).expect("the announced owner is answered");
    assert_eq!(answered["ok"], true, "{answered}");
    assert_eq!(answered["result"]["joined"], true);
    assert!(call(&b).is_err(), "another key gets no answer");
    stop.store(true, std::sync::atomic::Ordering::SeqCst);
    member.join().unwrap();
}
