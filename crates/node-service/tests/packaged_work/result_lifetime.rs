// SPDX-License-Identifier: AGPL-3.0-only
//! Defensive packaged lifetime regression: disposable enrollment, loopback and empty custody.
use super::{Service, app_host, fixture, joined};
use api::{
    member_control::Remote,
    run_exchange::{
        Announce, RunExchange,
        owner_pin::{OwnerControl, OwnerPin, VerifiedAnnouncement},
    },
    run_exchange_routes::{DISCOVERY, PATH_READ, READ, SignedRoutes},
};
use budgets::{BudgetId as B, Entries};
use serde_json::{Value, json};
use std::{
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

// Reuse the maintained signed client, not a new probe or standalone client.
#[allow(dead_code)]
#[path = "../../../api/tests/support/member_remote.rs"]
mod member_remote;

/// Read a fresh owner process's real RunExchange using this fixture's enrolled member
/// and seed. No unsigned announcement setter and no direct Pins access. The seed's
/// actual current-member guard supplies the row to the existing signing route.
fn read_process(p: &fixture::Joined, exchange: &RunExchange) -> VerifiedAnnouncement {
    let client = p.client();
    let now = app_host::now();
    let request_id = "rrp-process-read";
    let ttl = B::ControlTtl.span(Entries::ZERO).unwrap().millis() as f64 / 1000.0;
    let mut statement = Value::Object(client.binding(DISCOVERY, DISCOVERY));
    for (key, value) in json!({
        "protocol":READ,"swarm_id":client.member().swarm_id,
        "request_id":request_id,"sequence":1,"nonce":"rrp-process-nonce",
        "issued_at":now,"expires_at":now + ttl
    })
    .as_object()
    .unwrap()
    {
        statement[key] = value.clone();
    }
    let request = client.sign(&statement).unwrap();
    let coordinator = &p.seed.coordinator;
    let routes = SignedRoutes {
        exchange,
        signer: coordinator.signer(),
        swarm_id: coordinator.swarm_id(),
        owner_node_id: coordinator.seed_node_id(),
        observe_clock: &|_, _, _| {},
        response_ttl: B::ManagedControlExchangeResponseTtl
            .span(Entries::ZERO)
            .unwrap(),
    };
    let response = coordinator
        .with_current_member(p.session.node_id(), |member, now| {
            Ok(routes.dispatch_guarded(PATH_READ, &request, member, now))
        })
        .unwrap()
        .unwrap();
    VerifiedAnnouncement::from_response(&client, &response, request_id).unwrap()
}

fn registration(p: &fixture::Joined, service: &Service) -> joined::Verified {
    joined::verify(
        &p.seed.coordinator,
        &service.registration().unwrap(),
        p.owner.public(),
        "run-s4cb2",
        "dep-s4cb2",
        &|address| address.ip().is_loopback(),
        app_host::now(),
    )
    .unwrap()
}

/// Real serial dispatch and completion. Signed status is the completion barrier,
/// including active-slot release, not a sleep or a result read racing that release.
fn complete_discard(call: &impl Fn(Value) -> Value, id: u64) -> Value {
    assert_eq!(
        call(json!({"command":"discard","operation_id":id})),
        json!({"ok":true,"result":{"accepted":true,"operation_id":id}})
    );
    let deadline = Instant::now()
        + Duration::from_millis(B::RouterClose.span(Entries::ZERO).unwrap().millis());
    loop {
        let status = call(json!({"command":"status"}));
        assert_eq!(status["ok"], true, "signed status: {status}");
        if status["result"]["active_operation_id"].is_null() {
            assert_eq!(
                status["result"]["last_operation"],
                json!({"command":"discard","operation_id":id,"ok":true})
            );
            assert_eq!(status["result"]["joined"], true);
            assert_eq!(status["result"]["native_owned"], false);
            assert_eq!(status["result"]["shard_owned"], false);
            break;
        }
        assert!(
            Instant::now() < deadline,
            "discard did not finish: {status}"
        );
    }
    let result = call(json!({"command":"result","operation_id":id}));
    assert_eq!(
        result,
        json!({"ok":true,"result":{"operation_id":id,"finished":true,
            "reply":{"ok":true,"result":{"inert_bytes_discarded":false,
                "nothing_owned":true,"route_ready":false}}}})
    );
    result
}

#[test]
fn accepted_owner_process_update_retires_read_results_without_stopping_work() {
    // Refuse the maintained client's optional operator/LAN configuration before
    // creating even the disposable seed; this test never reads a real key file.
    for name in [
        "B8_MEMBER_CONTROL",
        "B8_LAN_IP",
        "B8_LAN_ALLOW",
        "B8_OWNER_CONTROL_KEY",
        "B8_REMOTE_MEMBER",
    ] {
        assert!(
            std::env::var_os(name).is_none(),
            "fixture-only: unset {name}"
        );
    }
    let p = fixture::Joined::new();
    let original = p.announced();
    let mut service = Service::start(
        p.member_root.clone(),
        Arc::new(Mutex::new(Some(p.session.clone()))),
        p.signer.clone(),
        original.clone(),
    )
    .unwrap();
    let registered = registration(&p, &service);
    let remote = Remote {
        addr: registered.address().clone(),
        enrolled: Some(p.signer.verification_key_digest().to_owned()),
    };
    let stale = member_remote::with_owner(&p.owner, |owner, client, deadline| {
        // Every call signs a fresh command and verifies the member's enrolled key,
        // sequence and command digest through the maintained production Remote.
        let call = |request| {
            remote
                .call(owner, client, request, deadline(), app_host::now())
                .unwrap()
        };
        let first = complete_discard(&call, 1);
        let refresh = p.announced();
        assert_eq!(
            original.announcement()["owner_incarnation"],
            refresh.announcement()["owner_incarnation"]
        );
        service.update(refresh.clone()).unwrap();
        assert_eq!(
            call(json!({"command":"result","operation_id":1})),
            first,
            "ordinary refresh must preserve a valid terminal retry"
        );

        // A new real exchange generates I2. Keep even issued_at identical to the
        // accepted refresh so this transition changes only the signed incarnation.
        let issued = refresh.announcement()["issued_at"].as_f64().unwrap();
        let replacement = RunExchange::new(Box::new(move || issued));
        replacement.set_owner_control(Some(OwnerControl::new(p.owner.public())));
        replacement
            .announce(Announce {
                phase: "compile".into(),
                run_id: Some("run-s4cb2".into()),
                deployment_id: Some("dep-s4cb2".into()),
                ..Default::default()
            })
            .unwrap();
        let current = read_process(&p, &replacement);
        assert_eq!(
            OwnerPin::from_announcement(&refresh).unwrap(),
            OwnerPin::from_announcement(&current).unwrap()
        );
        let mut old_body = refresh.announcement().clone();
        let mut new_body = current.announcement().clone();
        assert_ne!(
            old_body
                .as_object_mut()
                .unwrap()
                .remove("owner_incarnation"),
            new_body
                .as_object_mut()
                .unwrap()
                .remove("owner_incarnation")
        );
        assert_eq!(old_body, new_body, "no other signed announcement change");
        service.update(current).unwrap();
        // Deliberately poll only AFTER the successful update boundary; this is not
        // a demand to recall an answer authorized before that boundary.
        let stale = call(json!({"command":"result","operation_id":1}));
        assert_eq!(registration(&p, &service).address(), registered.address());

        // Exercise the current generation before the decisive assertion, even on
        // the old implementation. No blanket Stop/rebind may substitute for repin.
        let second = complete_discard(&call, 2);
        service.update(read_process(&p, &replacement)).unwrap();
        assert_eq!(
            call(json!({"command":"result","operation_id":2})),
            second,
            "current-generation refresh must also preserve its valid retry"
        );
        assert_eq!(
            call(json!({"command":"discard","operation_id":2})),
            json!({"ok":false,"code":"operator_operation_id_invalid"}),
            "retention never re-executes an operation"
        );
        stale
    });
    // The maintained client has closed its host; join both service loops before
    // the intended RED assertion. Fixture Drop closes its private seed and root.
    service.close().unwrap();
    assert_eq!(service.registration().unwrap_err(), "member_work_closed");
    service.close().unwrap();
    drop(service);
    drop(p);
    assert_eq!(
        stale,
        json!({"ok":true,"result":{"operation_id":1,"finished":false}}),
        "accepted packaged owner-process update retained a preceding read result"
    );
}
