// SPDX-License-Identifier: AGPL-3.0-only
//! Static source-origin contract, not a behavior-changing timeout regression.
use budgets::BudgetId;

#[path = "support/managed_owner_fixture_budgets.rs"]
mod fixture_budgets;

#[test]
fn actual_consumed_helpers_keep_identity_unit_value_and_harness_origin() {
    use budgets::table::{Provenance, Purpose, Value};
    use budgets::{Entries, Span};
    use fixture_budgets::{BARRIER_WAIT_ID, SESSION_TIMEOUT_ID};
    for (id, name, millis, origin) in [
        (
            SESSION_TIMEOUT_ID,
            "ManagedOwnerFactoryFixtureSessionTimeout",
            30_000,
            SESSION_ORIGIN,
        ),
        (
            BARRIER_WAIT_ID,
            "ManagedOwnerFixtureBarrierWait",
            5_000,
            BARRIER_ORIGIN,
        ),
    ] {
        assert_eq!(
            id.name(),
            name,
            "consumed helper must retain source ID, not an equal-valued substitute"
        );
        assert_eq!(
            id.entry().value,
            Value::Duration(Span::from_millis(millis)),
            "consumed helper unit/value"
        );
        assert_eq!(id.entry().purpose, Purpose::Policy);
        assert_eq!(
            id.entry().provenance,
            Provenance::Source(origin),
            "consumed helper harness origin"
        );
        assert_eq!(id.span(Entries::new(0)).unwrap().millis(), millis);
        assert!(id.count().is_err(), "duration cannot be consumed as count");
    }
    assert_eq!(fixture_budgets::session_timeout().millis(), 30_000);
    assert_eq!(fixture_budgets::session_seconds(), 30.0);
    assert_eq!(fixture_budgets::barrier_wait().as_millis(), 5_000);
}

const SESSION_ORIGIN: &str = "crates/runtime/tests/support/managed_owner_factory.rs:166; crates/runtime/tests/support/managed_owner_sessions.rs:220,259,262,294,363 @ 9f37ca8be3d9c9b9921de6df903dbaf6adf41416; retained HelloSessions and held-client fixture session timeout; not a product ceiling or measured p99";
const BARRIER_ORIGIN: &str = "crates/runtime/tests/support/managed_owner_sessions.rs:225,267,299,368 @ 9f37ca8be3d9c9b9921de6df903dbaf6adf41416; held IO-entry signal wait before interrupt, concurrent progress, session close or owner close; not a product ceiling or measured p99";

fn function<'a>(source: &'a str, name: &str) -> &'a str {
    let start = source
        .find(&format!("fn {name}("))
        .expect("pinned function");
    let rest = &source[start..];
    &rest[..rest.find("\n#[test]").unwrap_or(rest.len())]
}

#[test]
fn admitted_fixture_ids_are_available() {
    for name in [
        "ManagedOwnerFactoryFixtureSessionTimeout",
        "ManagedOwnerFixtureBarrierWait",
    ] {
        assert!(
            BudgetId::from_name(name).is_some(),
            "missing admitted fixture ID {name}"
        );
    }
}

#[test]
fn only_the_ten_admitted_fixture_sites_consume_typed_helpers() {
    let sessions = include_str!("support/managed_owner_sessions.rs");
    let factory = include_str!("support/managed_owner_factory.rs");
    for (name, count) in [
        (
            "held_result_interrupt_is_responsive_and_fences_publication",
            1,
        ),
        (
            "pending_wait_releases_owner_so_another_session_can_progress",
            2,
        ),
        (
            "stop_retires_held_local_waiter_then_persists_its_own_intent",
            1,
        ),
        (
            "owner_close_wakes_held_io_without_remote_stop_or_exit_claim",
            1,
        ),
    ] {
        let body = function(sessions, name);
        assert_eq!(
            body.matches("fixture_budgets::session_timeout()").count(),
            count,
            "{name}: source-specific session origin"
        );
        assert_eq!(
            body.matches("fixture_budgets::barrier_wait()").count(),
            1,
            "{name}: source-specific barrier origin"
        );
    }
    assert_eq!(
        sessions
            .matches("fixture_budgets::session_timeout()")
            .count(),
        5
    );
    assert_eq!(
        sessions.matches("fixture_budgets::barrier_wait()").count(),
        4
    );
    assert_eq!(
        sessions.matches("Span::from_millis(30000)").count(),
        4,
        "excluded oracle/session inputs remain literal"
    );
    assert!(sessions.contains("fence.wait_for(Span::from_millis(5))?"));
    let body = function(
        factory,
        "real_factory_implements_retained_hello_traits_over_same_journal",
    );
    let compact: String = body.chars().filter(|c| !c.is_whitespace()).collect();
    assert!(
        compact.contains("factory.open(\"member\",fixture_budgets::session_seconds())"),
        "retained HelloSessions must consume source-specific seconds"
    );
    assert_eq!(
        factory
            .matches("fixture_budgets::session_seconds()")
            .count(),
        1
    );
}
