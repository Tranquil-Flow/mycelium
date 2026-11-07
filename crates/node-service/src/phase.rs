// SPDX-License-Identifier: AGPL-3.0-only
//! Ordered progress milestones for the native/desktop qualification. The owner prints
//! `SLICE_PHASE` lines and the L11 probe prints its receipts; a harness that hits its
//! (unchanged) deadline uses [`Timeline`] to say which phase was in progress. This
//! observes only; it never gates, retries or extends any budget.
use serde_json::{Value, json};
use std::{sync::OnceLock, time::Instant};
/// One milestone and the phase that is in progress while it has not yet been seen.
pub struct Milestone {
    pub marker: &'static str,
    pub phase: &'static str,
}
/// In the order they must occur in a passing desktop smoke.
pub const MILESTONES: &[Milestone] = &[
    Milestone {
        marker: "L11_NATIVE_READY",
        phase: "desktop_boot",
    },
    Milestone {
        marker: "job_begin",
        phase: "page_send",
    },
    Milestone {
        marker: "member_registered",
        phase: "worker_startup",
    },
    Milestone {
        marker: "staging_done",
        phase: "staging",
    },
    Milestone {
        marker: "member_ready",
        phase: "member_handshake",
    },
    Milestone {
        marker: "owner_leaf_ready",
        phase: "owner_leaf_startup",
    },
    Milestone {
        marker: "token_grant",
        phase: "grant",
    },
    Milestone {
        marker: "token_signed",
        phase: "token_compute",
    },
    Milestone {
        marker: "page_ack",
        phase: "page_ack",
    },
    Milestone {
        marker: "L11_SLICE_CHAT_DOM_TOKEN",
        phase: "dom_token",
    },
    Milestone {
        marker: "L11_SLICE_STOP_VERIFIED",
        phase: "stop",
    },
    Milestone {
        marker: "L11_SLICE_CHAT_DOM_TERMINAL",
        phase: "terminal",
    },
    Milestone {
        marker: "L11_CLEANUP:Verified",
        phase: "cleanup",
    },
];
const PREFIX: &str = "SLICE_PHASE";
/// Receipts the test-owned probe script records for each chat-page fetch.
const PAGE_PREFIX: &str = "L11_SLICE_PAGE_FETCH_";
/// Receipts the shell prints for the web view's inactive-scheduling policy and for each
/// change of the window's occlusion.
const WEBVIEW_PREFIXES: [&str; 2] = ["L11_WEBVIEW_INACTIVE_POLICY:", "L11_WINDOW_OCCLUSION:"];
static START: OnceLock<Instant> = OnceLock::new();
/// The documented marker line. `detail` must not contain spaces.
pub fn format(name: &str, detail: &str, wall_ms: u64, since_start_ms: u64) -> String {
    format!("{PREFIX} {name} detail={detail} wall_ms={wall_ms} since_start_ms={since_start_ms}")
}
/// Print a milestone line to stderr (the evidence log of a probe run).
pub fn mark(name: &str, detail: &str) {
    let since = START.get_or_init(Instant::now).elapsed();
    let wall = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX));
    eprintln!(
        "{}",
        format(
            name,
            detail,
            wall,
            u64::try_from(since.as_millis()).unwrap_or(u64::MAX)
        )
    );
}
fn present(log: &str, marker: &str) -> bool {
    log.lines().any(|line| {
        line == marker
            || line
                .strip_prefix(PREFIX)
                .and_then(|rest| rest.strip_prefix(' '))
                .and_then(|rest| rest.split(' ').next())
                == Some(marker)
    })
}
/// A whole line that says the run already failed (not merely mentions a failure).
fn failure_code(log: &str) -> Option<String> {
    log.lines().find_map(|line| {
        line.strip_prefix("slice driver failed: ")
            .or_else(|| line.strip_prefix("L11_SLICE_FAILED_"))
            .map(str::to_owned)
    })
}
/// Where a stalled run was.
#[derive(Debug, PartialEq, Eq)]
pub struct Stall {
    pub phase: &'static str,
    pub awaiting: &'static str,
    pub last_seen: Option<(&'static str, u64)>,
}
/// First-seen times (harness-elapsed milliseconds) of each milestone.
#[derive(Default)]
pub struct Timeline {
    seen: Vec<(&'static str, u64)>,
    failure: Option<(String, u64)>,
    page: Vec<(String, u64)>,
    webview: Vec<(String, u64)>,
}
impl Timeline {
    /// Record milestones now present in `log` that were not seen before.
    pub fn observe(&mut self, log: &str, elapsed_ms: u64) {
        for line in log.lines().filter(|l| l.starts_with(PAGE_PREFIX)) {
            if !self.page.iter().any(|(seen, _)| seen == line) {
                self.page.push((line.to_owned(), elapsed_ms));
            }
        }
        for line in log
            .lines()
            .filter(|l| WEBVIEW_PREFIXES.iter().any(|p| l.starts_with(p)))
        {
            // Occlusion flips back and forth, so a state is recorded again when it recurs
            // after a different one; only a verbatim repeat of the latest line is a duplicate.
            let known = self.webview.iter().filter(|(seen, _)| seen == line).count();
            let occurrences = log.lines().filter(|l| *l == line).count();
            if known < occurrences {
                self.webview.push((line.to_owned(), elapsed_ms));
            }
        }
        if self.failure.is_none()
            && let Some(code) = failure_code(log)
        {
            self.failure = Some((code, elapsed_ms));
        }
        for milestone in MILESTONES {
            if !self.seen.iter().any(|(m, _)| *m == milestone.marker)
                && present(log, milestone.marker)
            {
                self.seen.push((milestone.marker, elapsed_ms));
            }
        }
    }
    /// The first definitive failure line seen: the owner driver or the probe gave up.
    pub fn failure(&self) -> Option<(String, u64)> {
        self.failure.clone()
    }
    pub fn complete(&self) -> bool {
        MILESTONES
            .iter()
            .all(|m| self.seen.iter().any(|(s, _)| *s == m.marker))
    }
    /// The first milestone not yet seen. On a complete run this is the last one.
    pub fn stall(&self) -> Stall {
        let next = MILESTONES
            .iter()
            .find(|m| !self.seen.iter().any(|(s, _)| *s == m.marker))
            .unwrap_or(&MILESTONES[MILESTONES.len() - 1]);
        Stall {
            phase: next.phase,
            awaiting: next.marker,
            last_seen: self.seen.iter().max_by_key(|(_, at)| *at).copied(),
        }
    }
    pub fn report(&self, outcome: &str, deadline_ms: u64) -> Value {
        let stall = self.stall();
        json!({
            "outcome": outcome,
            "deadline_ms": deadline_ms,
            "complete": self.complete(),
            "stalled_phase": if self.complete() { Value::Null } else { json!(stall.phase) },
            "awaiting": if self.complete() { Value::Null } else { json!(stall.awaiting) },
            "last_seen": stall.last_seen.map(|(marker, at)| json!({"marker": marker, "elapsed_ms": at})),
            "page_fetch": self.page.iter().map(|(line, at)| json!({"line": line, "elapsed_ms": at})).collect::<Vec<_>>(),
            "webview_state": self.webview.iter().map(|(line, at)| json!({"line": line, "elapsed_ms": at})).collect::<Vec<_>>(),
            "failure": self.failure.as_ref().map(|(code, at)| json!({"code": code, "elapsed_ms": at})),
            "seen": self.seen.iter().map(|(marker, at)| json!({"marker": marker, "elapsed_ms": at})).collect::<Vec<_>>(),
        })
    }
}
