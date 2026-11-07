// SPDX-License-Identifier: AGPL-3.0-only
//! Real CLIENT factory/journal, scripted membership; no native hello claim.
use super::*;
use runtime::managed_owner::session::ManagedSessionFactory;
fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../fixtures/managed-owner-factory-current-oracle.json"
    ))
    .unwrap()
}
fn status(expected: &Value) -> Value {
    if let Some(error) = expected.get("error") {
        json!({"error":error})
    } else {
        json!({"ok":true})
    }
}
#[test]
fn real_factory_matches_all_current_binding_oracle_cases() {
    let fixture = fixture();
    assert_eq!(fixture["cases"].as_array().unwrap().len(), 12);
    for case in fixture["cases"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let input = &case["input"];
        let r = Root::new();
        let inner = Arc::new(Mutex::new(Host::new(&r)));
        {
            let mut h = inner.lock().unwrap();
            h.member = input["current_member"].clone();
            h.member_error = if input["current_member_error"].is_string() {
                Some("controlled_member_lookup_failed")
            } else {
                None
            };
        }
        let owner = SharedManagedOwner::new(open(&r), host_factory(inner.clone(), None, false));
        let before = fs::read(r.0.join("owner.json")).unwrap();
        let factory = ManagedSessionFactory::new(
            owner.clone(),
            "run",
            "deployment",
            input["captured_members"].as_array().unwrap(),
        )
        .expect("source factory accepts captured members");
        assert!(
            inner.lock().unwrap().member_reads.is_empty(),
            "{name}: constructor must not resolve live membership"
        );
        if input["owner_closed"] == true {
            owner.close().unwrap();
        }
        let opened = match factory.open(&[], "member", "run", "deployment", 30.0) {
            Ok(session) => {
                json!({"ok":{"binding":session.binding().unwrap(),"timeout_seconds":session.timeout().millis() as f64/1000.0}})
            }
            Err(error) => json!({"error":error.0}),
        };
        assert_eq!(opened, case["factory_open"], "{name}: exact factory open");
        assert_eq!(
            factory.binding_current("member"),
            case["binding_current"].get("ok").is_some(),
            "{name}: current binding is not owner.session admission"
        );
        {
            let mut h = inner.lock().unwrap();
            h.member = input["captured_members"][0].clone();
            h.member_error = None;
        }
        assert_eq!(
            factory.binding_current("member"),
            case["binding_restored"].get("ok").is_some(),
            "{name}: restored authority on same factory"
        );
        let expected_reads: Vec<Value> = case["member_reads"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|read| read[0] != "owner_session")
            .map(|read| read[1].clone())
            .collect();
        {
            let h = inner.lock().unwrap();
            assert_eq!(
                json!(h.member_reads),
                json!(expected_reads),
                "{name}: fresh reads"
            );
            assert!(h.calls.is_empty());
            assert_eq!(h.nonce, 0);
        }
        assert_eq!(
            fs::read(r.0.join("owner.json")).unwrap(),
            before,
            "{name}: journal unchanged"
        );
        owner.close().unwrap();
    }
}
#[test]
fn real_factory_scope_and_constructor_refusals_match_oracle() {
    let expected = captured("factory");
    let r = Root::new();
    let inner = Arc::new(Mutex::new(Host::new(&r)));
    let member = inner.lock().unwrap().member.clone();
    let owner = SharedManagedOwner::new(open(&r), host_factory(inner.clone(), None, false));
    assert_eq!(
        session_status(ManagedSessionFactory::new(
            owner.clone(),
            "run",
            "deployment",
            &[]
        )),
        status(&expected["empty"])
    );
    assert_eq!(
        session_status(ManagedSessionFactory::new(
            owner.clone(),
            "run",
            "deployment",
            &[member.clone(), member.clone()]
        )),
        status(&expected["duplicates"])
    );
    let factory =
        ManagedSessionFactory::new(owner.clone(), "run", "deployment", &[member]).unwrap();
    for (name, argv, node, run, timeout) in [
        ("argv", vec!["no-exec".to_owned()], "member", "run", 30.0),
        ("node", vec![], "unknown", "run", 30.0),
        ("run", vec![], "member", "other", 30.0),
        ("timeout", vec![], "member", "run", 0.0),
    ] {
        assert_eq!(
            session_status(factory.open(&argv, node, run, "deployment", timeout)),
            status(&expected[name]),
            "{name}"
        );
    }
    for (name, timeout) in [("valid", 30.0), ("huge_timeout", 1e300)] {
        let session = factory
            .open(&[], "member", "run", "deployment", timeout)
            .unwrap();
        assert_eq!(
            json!({"ok":{"binding":session.binding().unwrap(),"timeout_seconds":session.timeout().millis() as f64/1000.0}}),
            expected[name]
        );
    }
    owner.close().unwrap();
}
#[test]
fn real_factory_implements_retained_hello_traits_over_same_journal() {
    use runtime::observe::HelloSessions;
    let r = Root::new();
    let inner = Arc::new(Mutex::new(Host::new(&r)));
    let member = inner.lock().unwrap().member.clone();
    let owner = SharedManagedOwner::new(open(&r), host_factory(inner.clone(), None, false));
    let factory =
        ManagedSessionFactory::new(owner.clone(), "run", "deployment", &[member]).unwrap();
    let mut factory: Box<dyn HelloSessions + '_> = Box::new(factory);
    assert_eq!(factory.run_id(), "run");
    assert_eq!(factory.deployment_id(), "deployment");
    assert_eq!(
        factory.bound_nodes(),
        ["member".to_owned()].into_iter().collect()
    );
    assert!(factory.binding_current("member"));
    let mut session = factory
        .open("member", fixture_budgets::session_seconds())
        .unwrap();
    assert_eq!(session.send_hello("trait-hello").unwrap()["ok"], true);
    assert_eq!(session.stop_response_ok(), None);
    session.close().unwrap();
    assert!(!session.cleanup_error());
    assert_eq!(session.stop_response_ok(), Some(true));
    let saved: Value = serde_json::from_slice(&fs::read(r.0.join("owner.json")).unwrap()).unwrap();
    assert!(saved["commands"]["trait-hello"]["enqueue_request"].is_object());
    assert_eq!(
        inner
            .lock()
            .unwrap()
            .calls
            .iter()
            .filter(|p| p["operation"] == "enqueue")
            .count(),
        2
    );
    owner.close().unwrap();
}
