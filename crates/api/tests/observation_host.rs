// SPDX-License-Identifier: AGPL-3.0-only
//! Authentic signed metadata + real L9 adapter; session/mailbox absence stays a refusal.
use api::{
    installation_descriptor::{self, Member, Paths, Source},
    observation_host::{ObservationBackend, VerifiedObservationHost},
};
use owner_run::{chain::PortError, steps::observe::ObservePort};
use protocol::identity::Identity;
use runtime::{
    observe::{HelloSessions, ObservationError, Offer},
    observe_owner::ObserveAdapter,
};
use serde_json::{Value, json};
use std::{collections::BTreeMap, io::Read};
const RUN: &str = "a13-test-qwen-20261029T100000Z";
struct Backend {
    members: BTreeMap<String, Value>,
    session_attempts: usize,
}
impl ObservationBackend for Backend {
    fn is_owner(&self) -> bool {
        true
    }
    fn running(&self) -> bool {
        true
    }
    fn prepared_manifest_digest(&mut self) -> Result<String, ObservationError> {
        Ok(format!("sha256:{}", "ab".repeat(32)))
    }
    fn swarm_id(&self) -> String {
        "swarm-1".into()
    }
    fn member(&self, node: &str) -> Option<Value> {
        self.members.get(node).cloned()
    }
    fn wall_clock(&self) -> f64 {
        1000.0
    }
    fn session_factory(
        &mut self,
        run: &str,
        deployment: &str,
        members: &[Value],
    ) -> Result<Box<dyn HelloSessions>, ObservationError> {
        assert_eq!((run, deployment), (RUN, "dep-1"));
        assert_eq!(members.len(), self.members.len());
        for member in members {
            assert_eq!(
                self.members.get(member["node_id"].as_str().unwrap()),
                Some(member)
            );
        }
        self.session_attempts += 1;
        // Deliberate missing-collaborator fixture, NOT scripted hello success.
        Err(ObservationError::new("managed_control_owner_unavailable"))
    }
    fn publish_offer(
        &mut self,
        _: &Value,
        _: &str,
        _: &str,
        _: &[u8],
    ) -> Result<Offer, ObservationError> {
        panic!("no offers before genuine sessions exist")
    }
    fn retire_offer(&mut self, _: &Offer) -> Result<Value, ObservationError> {
        panic!("no synthetic retirement")
    }
    fn request_stage_cleanup(&mut self, _: &Offer) -> Result<(), ObservationError> {
        panic!("no synthetic cleanup")
    }
}
fn fixture() -> (Backend, Vec<Value>, Vec<Identity>) {
    let mut members = BTreeMap::new();
    let mut cards = Vec::new();
    let mut signers = Vec::new();
    for n in ["one", "two"] {
        let mut secret = [0_u8; 32];
        std::fs::File::open("/dev/urandom")
            .unwrap()
            .read_exact(&mut secret)
            .unwrap();
        let signer = Identity::from_secret(&format!("endpoint-{n}"), &secret).unwrap();
        let member = Member {
            node_id: format!("member-{n}"),
            endpoint_id: signer.endpoint_id().into(),
            verification_key_digest: signer.verification_key_digest(),
            generation: 1,
            incarnation: format!("inc-{n}"),
        };
        let current = json!({"node_id":member.node_id,"endpoint_id":member.endpoint_id,"verification_key_digest":member.verification_key_digest,"generation":member.generation,"incarnation":member.incarnation,"lease_expires_at":2000,"peer_class":"mac_mlx_iroh","lifecycle_state":"NEW"});
        let card = installation_descriptor::installation_descriptor(
            &signer,
            &member,
            "swarm-1",
            RUN,
            "dep-1",
            true,
            &Source::Whole {
                source_root: "/scratch/source".into(),
                manifest_digest: format!("sha256:{}", "ab".repeat(32)),
            },
            &Paths {
                state_root: "/scratch/state".into(),
                endpoint_secret_file: "/scratch/key".into(),
                python_executable: "/scratch/host".into(),
                sidecar_binary: "/scratch/sidecar".into(),
            },
            1000.0,
        )
        .unwrap();
        members.insert(member.node_id, current);
        cards.push(card);
        signers.push(signer);
    }
    (
        Backend {
            members,
            session_attempts: 0,
        },
        cards,
        signers,
    )
}
#[test]
fn fresh_signed_reports_feed_authenticated_descriptors_into_observation() {
    use api::{
        run_exchange::RunExchange,
        run_exchange_routes::{DISCOVERY, PATH_REPORT, REPORT, RESPONSE, SignedRoutes},
    };
    use protocol::{TimeLimits, managed};
    let (backend, cards, signers) = fixture();
    let exchange = RunExchange::new(Box::new(|| 1000.0));
    let mut secret = [0_u8; 32];
    std::fs::File::open("/dev/urandom")
        .unwrap()
        .read_exact(&mut secret)
        .unwrap();
    let owner = Identity::from_secret("owner-fresh", &secret).unwrap();
    let observations = std::cell::RefCell::new(Vec::new());
    let observe = |node: &str, issued: f64, rtt: Option<f64>| {
        observations
            .borrow_mut()
            .push((node.to_owned(), issued, rtt))
    };
    let routes = SignedRoutes {
        exchange: &exchange,
        signer: &owner,
        swarm_id: "swarm-1",
        owner_node_id: "owner-node",
        observe_clock: &observe,
        response_ttl: budgets::Span::from_millis(60_000),
    };
    for (card, signer) in cards.iter().zip(&signers) {
        let node = card["statement"]["target_node_id"].as_str().unwrap();
        let member = &backend.members[node];
        let report = json!({"protocol":api::run_exchange::MEMBER_REPORT,"run_id":RUN,"deployment_id":"dep-1","state":"reported","detail":null,"model":null,"progress":null,"descriptor":card,"issued_at":1000.0});
        let statement = json!({"protocol":REPORT,"request_id":format!("report-{node}"),"sequence":1,"nonce":format!("nonce-{node}"),"swarm_id":"swarm-1","issued_at":1000.0,"expires_at":1030.0,"target_node_id":node,"target_endpoint_id":member["endpoint_id"],"target_key_digest":member["verification_key_digest"],"membership_generation":member["generation"],"member_incarnation":member["incarnation"],"run_id":DISCOVERY,"deployment_id":DISCOVERY,"report":report});
        let envelope = json!({"protocol":managed::SIGNED_ENVELOPE_PROTOCOL,"signature":signer.sign(&statement).unwrap(),"verification_key":signer.public_key_record(),"statement":statement});
        let response = routes
            .dispatch_guarded(PATH_REPORT, &envelope, member, 1000.0)
            .unwrap();
        let verified = managed::verify(
            &response,
            RESPONSE,
            &owner.public_key_record(),
            owner.endpoint_id(),
            1000.0,
            TimeLimits {
                max_ttl_seconds: 300.0,
                clock_skew_seconds: 30.0,
            },
        )
        .unwrap();
        assert_eq!(verified["target_node_id"], node);
        let mut revoked = member.clone();
        revoked["lifecycle_state"] = json!("REVOKED");
        assert_eq!(
            routes
                .dispatch_guarded(PATH_REPORT, &envelope, &revoked, 1000.0)
                .unwrap_err()
                .0,
            "managed_control_member_stale"
        );
    }
    assert_eq!(observations.borrow().len(), cards.len());
    let stored = exchange.reports();
    assert_eq!(stored.as_object().unwrap().len(), cards.len());
    let reported: Vec<Value> = stored
        .as_object()
        .unwrap()
        .values()
        .map(|r| r["descriptor"].clone())
        .collect();
    let mut adapter = ObserveAdapter::new(
        VerifiedObservationHost { backend },
        || 1000.0,
        || panic!("no native hello"),
    );
    let err = adapter.prepare(RUN, "dep-1", &reported).unwrap_err();
    assert!(
        matches!(err,PortError::Failed{code:Some(ref c),..} if c=="managed_control_owner_unavailable"),
        "{err:?}"
    );
    assert_eq!(adapter.host_mut().backend.session_attempts, 1);
    assert!(adapter.context().is_none());
}

#[test]
fn authenticated_descriptors_reach_real_observation_factory_boundary_without_fake_hello() {
    let (backend, cards, _signers) = fixture();
    let mut adapter = ObserveAdapter::new(
        VerifiedObservationHost { backend },
        || 1000.0,
        || panic!("no hello nonce before sessions"),
    );
    let err = adapter.prepare(RUN, "dep-1", &cards).unwrap_err();
    assert!(
        matches!(err,PortError::Failed{code:Some(ref c),..} if c=="managed_control_owner_unavailable"),
        "{err:?}"
    );
    assert_eq!(adapter.host_mut().backend.session_attempts, 1);
    assert!(adapter.context().is_none());
}
#[test]
fn tampered_or_readmitted_member_is_refused_before_session_or_mailbox_effects() {
    for readmitted in [false, true] {
        let (mut backend, mut cards, _signers) = fixture();
        if readmitted {
            backend.members.get_mut("member-two").unwrap()["generation"] = json!(2);
        } else {
            cards[1]["statement"]["facts"]["source_root"] = json!("/tampered");
        }
        let mut adapter = ObserveAdapter::new(
            VerifiedObservationHost { backend },
            || 1000.0,
            || panic!("no nonce"),
        );
        let err = adapter.prepare(RUN, "dep-1", &cards).unwrap_err();
        let expected = if readmitted {
            "installation_descriptor_generation_mismatch"
        } else {
            "installation_descriptor_signature_invalid"
        };
        assert!(
            matches!(err,PortError::Failed{code:Some(ref c),..} if c==expected),
            "{err:?}"
        );
        assert_eq!(adapter.host_mut().backend.session_attempts, 0);
        assert!(adapter.context().is_none());
    }
}

/// Proves: L13-08 / rv49 certification (ledger 286). A Rollover/Rebuild run's observe step calls
/// `rebuild`; it is `rebuild_with_current_members` (`chain_compile.py:480-546` @ f4e366e9): drop the
/// cards of members that are no longer current, refuse below two before any effect, then prepare a
/// fresh observation (same verified path as `prepare`). Never `NP-L10-observation-rebuild`, which
/// stopped every automatic continuation at native hello (`rollover_failed`).
#[test]
fn a_rollover_rebuilds_its_observation_over_the_current_members() {
    // Both current: the rebuild reaches the real session factory, as `prepare` does.
    let (backend, cards, _signers) = fixture();
    let mut adapter = ObserveAdapter::new(
        VerifiedObservationHost { backend },
        || 1000.0,
        || panic!("no hello nonce before sessions"),
    );
    let err = adapter.rebuild(RUN, "dep-1", &cards).unwrap_err();
    assert!(
        matches!(err,PortError::Failed{code:Some(ref c),..} if c=="managed_control_owner_unavailable"),
        "{err:?}"
    );
    assert_eq!(adapter.host_mut().backend.session_attempts, 1);
    assert!(adapter.context().is_none());
    // A vanished member's card is dropped, not fatal; one current member left refuses before any
    // session or mailbox effect.
    let (mut backend, cards, _signers) = fixture();
    backend.members.remove("member-two");
    let mut adapter = ObserveAdapter::new(
        VerifiedObservationHost { backend },
        || 1000.0,
        || panic!("no nonce"),
    );
    let err = adapter.rebuild(RUN, "dep-1", &cards).unwrap_err();
    assert!(
        matches!(err,PortError::Failed{code:Some(ref c),..} if c=="rebuild_requires_two_current_members"),
        "{err:?}"
    );
    assert_eq!(adapter.host_mut().backend.session_attempts, 0);
    // A re-admitted member (new generation) is no longer the card's member: dropped too.
    let (mut backend, cards, _signers) = fixture();
    backend.members.get_mut("member-two").unwrap()["generation"] = json!(2);
    let mut adapter = ObserveAdapter::new(
        VerifiedObservationHost { backend },
        || 1000.0,
        || panic!("no nonce"),
    );
    let err = adapter.rebuild(RUN, "dep-1", &cards).unwrap_err();
    assert!(
        matches!(err,PortError::Failed{code:Some(ref c),..} if c=="rebuild_requires_two_current_members"),
        "{err:?}"
    );
    assert_eq!(adapter.host_mut().backend.session_attempts, 0);
}
