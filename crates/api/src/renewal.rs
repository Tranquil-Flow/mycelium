// SPDX-License-Identifier: AGPL-3.0-only
//! A renewal flight (lane FLK patch 2, FLK-02): the one seed round trip during which a membership
//! renewal holds the live Session out of its slot (`owner_ports::renew_contributor`,
//! `app_host::renew_member`). The slot records the flight, so its readers can tell a renewal in
//! flight (the preceding snapshot stands in) from a Session that is gone (they refuse, as before
//! the snapshot fallback existed):
//!
//! * every take starts a [`Flight`] with a fresh incarnation and a deadline;
//! * only the put-back of that same incarnation lands it; a late put-back of an older flight never
//!   overwrites a newer state (a replaced contributor, a later flight, a new Session);
//! * an unwinding renewal (its thread panics between take and put-back) ends its flight without a
//!   Session ([`Flight`] holders use an owned guard whose `Drop` does it);
//! * past its deadline a flight no longer stands in, whether its thread is slow or gone;
//! * a renewal the seed refuses ([`membership_refused`]) does not put the refused Session back.
//!
//! The lease, consent and seed checks of the Session itself are unchanged.
use budgets::{BudgetId, Entries};
use std::{
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, Instant},
};

/// One renewal's custody of a live Session, recorded in the slot it was taken from.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Flight {
    /// Unique per take in this process: a put-back lands only its own flight.
    pub id: u64,
    /// After this instant the flight counts as lost: no snapshot stands in for it any more.
    /// Public so a test can age a held flight; production sets it only in [`Flight::begin`].
    pub deadline: Instant,
}
impl Flight {
    /// A flight taken now, lost after [`allowance`].
    pub fn begin() -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(1);
        Self {
            id: NEXT.fetch_add(1, Ordering::Relaxed),
            deadline: Instant::now() + allowance(),
        }
    }
    /// Whether the preceding snapshot may stand in for the Session this flight holds.
    pub fn stands_in(&self) -> bool {
        Instant::now() < self.deadline
    }
}

/// How long one renewal pass may hold the Session: the member seed client's own request allowance
/// (`PyMyceliumA13MacosPeerRemotemacmemberInitTimeoutL117`, peer.py:117, the `SeedClient` timeout,
/// `seed.rs:160-164`) for its heartbeat and for its answer. The same bound the joined Mac's leave
/// waits for a renewal pass on the wire (`packaged_network::Network::await_renewal_pass`).
pub fn allowance() -> Duration {
    BudgetId::PyMyceliumA13MacosPeerRemotemacmemberInitTimeoutL117
        .span(Entries::ZERO)
        .map_or(Duration::ZERO, |span| Duration::from_millis(span.millis()))
        * 2
}

/// A renewal answer by which the seed refused THIS Session's membership, rather than a transport
/// or local failure: revoked, expired, removed, a stale generation (`peer::translate_with` records a
/// terminal disposition for these four), or a seed row that no longer matches the Session's
/// generation, incarnation or endpoint (`seed_message_mismatch`, protocol `seed_members.rs`
/// `receive`: the generation changed, which `handoff::peer_translate` does not map). Such a
/// Session is never put back: no reader may keep using it until its old lease ends.
pub fn membership_refused(code: &str) -> bool {
    matches!(
        code,
        "peer_member_revoked"
            | "peer_member_expired"
            | "peer_member_unknown"
            | "peer_generation_stale"
            | "seed_message_mismatch"
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn a_flight_stands_in_until_its_deadline_and_each_take_is_its_own() {
        let a = Flight::begin();
        let b = Flight::begin();
        assert_ne!(a.id, b.id);
        assert!(a.stands_in());
        assert!(a.deadline <= Instant::now() + allowance());
        let mut aged = a.clone();
        aged.deadline = Instant::now();
        assert!(!aged.stands_in(), "past its deadline a flight no longer stands in");
    }
    #[test]
    fn only_a_seed_refusal_of_the_membership_counts_as_refused() {
        for code in [
            "peer_member_revoked",
            "peer_member_expired",
            "peer_member_unknown",
            "peer_generation_stale",
            "seed_message_mismatch",
        ] {
            assert!(membership_refused(code), "{code}");
        }
        for code in [
            "peer_transport_unreachable",
            "action_not_completed",
            "peer_clock_invalid",
            "peer_membership_missing",
            "seed_heartbeat_sequence_stale",
        ] {
            assert!(!membership_refused(code), "{code}");
        }
    }
}
