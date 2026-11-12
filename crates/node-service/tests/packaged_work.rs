// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "iroh-loopback")]
#[path = "../../api/tests/support/work_member.rs"]
mod fixture;
use api::{
    app_host,
    joined_owner::{link::MemberLink, remote::RemoteMember},
    member_control::joined,
};
use node_service::packaged_work::Service;
use serde_json::json;
use std::sync::{Arc, Mutex};
#[test]
fn enrolled_packaged_control_dispatches_without_pair_request_and_closes_all_owned_loops() {
    let p = fixture::Joined::new();
    let result = Service::start(
        p.member_root.clone(),
        Arc::new(Mutex::new(Some(p.session.clone()))),
        p.signer.clone(),
        p.announced(),
    );
    assert!(
        result.is_ok(),
        "real packaged producer refused: {:?}",
        result.err()
    );
    let mut service = result.unwrap();
    let document = service.registration().unwrap();
    let verified = joined::verify(
        &p.seed.coordinator,
        &document,
        p.owner.public(),
        "run-s4cb2",
        "dep-s4cb2",
        &|a| a.ip().is_loopback(),
        app_host::now(),
    )
    .unwrap();
    let mut link =
        RemoteMember::already_joined(p.owner.clone(), &p.seed.coordinator, &verified, None)
            .unwrap();
    let wait = std::time::Duration::from_millis(
        budgets::BudgetId::RouterClose
            .span(budgets::Entries::ZERO)
            .unwrap()
            .millis(),
    );
    let status = link.call(json!({"command":"status"}), wait).unwrap();
    assert_eq!(status["joined"], true);
    assert_eq!(status["native_owned"], false);
    let consent = link
        .call(json!({"command":"consent_status"}), wait)
        .unwrap();
    assert_eq!(consent["consent"]["state"], "granted");
    assert_eq!(
        link.call(json!({"command":"consent","policy":p.policy}), wait)
            .unwrap_err()
            .0,
        "member_local_consent_required"
    );
    assert_eq!(
        link.call(json!({"command":"pair_request"}), wait)
            .unwrap_err()
            .0,
        "member_already_joined"
    );
    drop(link);
    service.close().unwrap();
    assert_eq!(service.registration().unwrap_err(), "member_work_closed");
    service.close().unwrap();
}
