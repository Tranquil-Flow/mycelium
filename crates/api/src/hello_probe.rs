// SPDX-License-Identifier: AGPL-3.0-only
//! Lane SR round 2: log-only `l11_hello_*` probes over native hello and the bootstrap offer
//! exchange, on the owner and on a joined Mac.
//!
//! rv42/rv43 (physical, every time): run 1 Start -> serving -> Stop clean; run 2 Start in the same
//! app sessions reports 2 of 2, then "Native hello on every Mac" never completes and the step ends
//! ten minutes later `managed_control_response_timeout` / `member_observation_timeout`, with the
//! joined Mac `bootstrap_running` and the owner's own contributor `offer_response_invalid`. The
//! app logged nothing between Start and that end, so the first failed operation is unknown.
//!
//! Every line here names one step (offer publish / cleanup / retire, a bootstrap's offer checks
//! and the exact predicate that failed, a stage ACK, a pump's outcome, each hello collection
//! operation, an agent's card) with its node, run and offer. Lines are recorded at STATE
//! TRANSITIONS only: [`on_change`] drops a line whose state equals the last one recorded for the
//! same scope, so a poll loop that keeps failing the same way logs once, not once per tick. Every
//! line goes through [`seed::probe`]'s bounded recorder (never blocks, never panics, never writes
//! on the caller's thread), so a probe is safe under any lock. Wire codes, refusals and timing are
//! unchanged: these probes only observe.
use std::{
    collections::{BTreeSet, hash_map::DefaultHasher},
    fmt,
    hash::{Hash, Hasher},
    sync::Mutex,
};

/// The scopes whose last state is remembered; the oldest is forgotten first (it then logs again).
const SCOPES: usize = 128;

/// `(scope hash, state hash)` of the last line recorded per scope.
static LAST: Mutex<Vec<(u64, u64)>> = Mutex::new(Vec::new());

fn digest(text: &str) -> u64 {
    let mut hasher = DefaultHasher::new();
    text.hash(&mut hasher);
    hasher.finish()
}

/// Record `line` unless `state` is what was last recorded for `scope`. A contended or poisoned
/// memory records the line (never blocks: `try_lock`).
pub fn on_change(scope: &str, state: &str, line: fmt::Arguments<'_>) {
    let (scope, state) = (digest(scope), digest(state));
    let changed = match LAST.try_lock() {
        Ok(mut last) => remember(&mut last, scope, state),
        Err(_) => true,
    };
    if changed {
        seed::probe::record(line);
    }
}
/// Store `state` as `scope`'s last; whether it differs from the one remembered before.
fn remember(last: &mut Vec<(u64, u64)>, scope: u64, state: u64) -> bool {
    match last.iter_mut().find(|(s, _)| *s == scope) {
        Some(entry) => {
            let changed = entry.1 != state;
            entry.1 = state;
            changed
        }
        None => {
            if last.len() >= SCOPES {
                last.remove(0);
            }
            last.push((scope, state));
            true
        }
    }
}

/// Record `line` (a one-off event, never repeated per tick).
pub fn record(line: fmt::Arguments<'_>) {
    seed::probe::record(line);
}

/// Wall-clock milliseconds since the Unix epoch, as every probe line carries them.
pub fn wall_ms() -> u128 {
    seed::probe::wall_ms()
}

/// Seconds from `now` to an offer's `expires_at` (negative once it passed; NaN when absent).
pub fn expires_in(offer: &serde_json::Value, now: f64) -> f64 {
    offer["expires_at"].as_f64().map_or(f64::NAN, |t| t - now)
}

/// The names of the `expected` fields whose value in `actual` differs (`-` when none): the
/// binding predicate of an offer or ACK check, without any value (no keys, digests or ids).
pub fn mismatched(expected: &serde_json::Value, actual: &serde_json::Value) -> String {
    let names: Vec<&str> = expected
        .as_object()
        .into_iter()
        .flatten()
        .filter(|(k, v)| actual[k.as_str()] != **v)
        .map(|(k, _)| k.as_str())
        .collect();
    if names.is_empty() {
        "-".into()
    } else {
        names.join(",")
    }
}

/// A short form of an id for a log line (its first 24 characters; `-` when absent).
pub fn short(value: &serde_json::Value) -> String {
    value
        .as_str()
        .map_or_else(|| "-".into(), |s| s.chars().take(24).collect())
}

/// One target pump's transitions on the log (`l11_hello_pump`): started, each distinct step outcome
/// (an empty poll, a retryable or requeued exchange with its operation and cause, a completed
/// command, the native Stop after which the thread only awaits cleanup), and its end. Within one
/// command cycle an outcome is logged once; a completed command starts the next cycle.
pub struct PumpProbe {
    side: &'static str,
    node: String,
    run: String,
    seen: BTreeSet<String>,
}
impl PumpProbe {
    /// `side`: `member` (a joined Mac's pump) or `local` (the owner's own contributor's).
    pub fn started(side: &'static str, scope: &serde_json::Value) -> Self {
        let probe = Self {
            side,
            node: short(&scope["target_node_id"]),
            run: scope["run_id"].as_str().unwrap_or("-").to_owned(),
            seen: BTreeSet::new(),
        };
        probe.line("started");
        probe
    }
    fn line(&self, event: &str) {
        record(format_args!(
            "l11_hello_pump wall_ms={} side={} node={} run={} event={event}",
            wall_ms(),
            self.side,
            self.node,
            self.run,
        ));
    }
    /// One step's outcome.
    pub fn step(&mut self, outcome: &crate::target_pump::StepOutcome) {
        use crate::target_pump::StepOutcome;
        let event = match outcome {
            StepOutcome::Empty => "poll_empty".to_owned(),
            StepOutcome::Completed { stopped: false } => "command_completed".to_owned(),
            StepOutcome::Completed { stopped: true } => "stopped_awaiting_cleanup".to_owned(),
            StepOutcome::OutboundRetryable { operation, cause } => {
                format!("retryable:{operation}:{cause}")
            }
            StepOutcome::OutboundRequeued { operation, cause } => {
                format!("requeued:{operation}:{cause}")
            }
        };
        let completed = matches!(outcome, StepOutcome::Completed { .. });
        if self.seen.insert(event.clone()) {
            self.line(&event);
        }
        if completed {
            self.seen.clear();
        }
    }
    /// The pump thread's end: `ok`, or the code its loop ended with.
    pub fn ended(&self, outcome: &str) {
        self.line(&format!("ended:{outcome}"));
    }
}

/// Lane SR round 3 (review SR2-01): the owner's hello COLLECTION attempts. One attempt covers every
/// Mac and has no partial result; its refusal can come after every open, hello and stop of it
/// succeeded (native identity, distinct hosts / boots, current membership, or the captured-time
/// window `managed_observation_captured_stale`, whose 60 s skew bound counts from the attempt's
/// first clock reading: managed_observations.py:41,177,262-264 @ f4e366e9). rv44 session 1 run 2:
/// attempt 1 completed open/hello/stop for both Macs and still failed, and nothing logged why.
///
/// [`Collection::begin`] / [`Collection::end`] wrap one attempt; `end` records, for EVERY attempt,
///
/// ```text
/// l11_hello_collect wall_ms=<ms> node=<last node reached> run=<run> collection=<n> op=validate outcome=<ok|code> reached=<op> collect_ms=<ms>
/// ```
///
/// and, whenever the attempt's refusal differs from the previous attempt's (the observe step's
/// `observing hosts (retrying after <code>)`, of which the page shows only the last),
/// `l11_hello_collect_retry wall_ms=<ms> run=<run> collection=<n> reason=<code> previous=<code|->`.
/// The per-node lines carry the same `collection=<n>`. One owner collects per process; the state is
/// keyed by run, so a new run starts again at 1. Log-only.
#[derive(Default)]
struct Attempts {
    run: String,
    attempt: u64,
    started: Option<std::time::Instant>,
    node: String,
    reached: &'static str,
    last_refusal: Option<String>,
}
static COLLECTION: Mutex<Option<Attempts>> = Mutex::new(None);

/// The owner's hello collection attempts on the log (see [`Attempts`]).
pub struct Collection;
impl Collection {
    /// A collection attempt of `run` starts.
    pub fn begin(run: &str) {
        if let Ok(mut slot) = COLLECTION.lock() {
            let state = slot.get_or_insert_with(Attempts::default);
            if state.run != run {
                *state = Attempts {
                    run: run.to_owned(),
                    ..Attempts::default()
                };
            }
            state.attempt = state.attempt.saturating_add(1);
            state.started = Some(std::time::Instant::now());
            state.node = "-".into();
            state.reached = "begin";
        }
    }
    /// The current attempt of `run` (`0` when none began).
    pub fn attempt(run: &str) -> u64 {
        COLLECTION
            .lock()
            .ok()
            .and_then(|slot| {
                slot.as_ref()
                    .filter(|s| s.run == run)
                    .map(|state| state.attempt)
            })
            .unwrap_or(0)
    }
    /// The attempt reached `op` on `node` (`open`, `hello`, `stop`, `binding_current`).
    pub fn reached(run: &str, node: &str, op: &'static str) {
        if let Ok(mut slot) = COLLECTION.lock()
            && let Some(state) = slot.as_mut().filter(|s| s.run == run)
        {
            state.node = node.to_owned();
            state.reached = op;
        }
    }
    /// The attempt of `run` ended with `refusal` (`None`: collected).
    pub fn end(run: &str, refusal: Option<&str>) {
        let Ok(mut slot) = COLLECTION.lock() else {
            return;
        };
        let Some(state) = slot.as_mut().filter(|s| s.run == run) else {
            return;
        };
        let outcome = refusal.unwrap_or("ok");
        let elapsed = state.started.map_or(0, |at| at.elapsed().as_millis());
        record(format_args!(
            "l11_hello_collect wall_ms={} node={} run={run} collection={} op=validate outcome={outcome} reached={} collect_ms={elapsed}",
            wall_ms(),
            short(&serde_json::json!(state.node)),
            state.attempt,
            state.reached,
        ));
        if let Some(code) = refusal
            && state.last_refusal.as_deref() != Some(code)
        {
            record(format_args!(
                "l11_hello_collect_retry wall_ms={} run={run} collection={} reason={code} previous={}",
                wall_ms(),
                state.attempt,
                state.last_refusal.as_deref().unwrap_or("-"),
            ));
            state.last_refusal = Some(code.to_owned());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_scope_logs_only_when_its_state_changes() {
        let mut last = Vec::new();
        assert!(remember(&mut last, 1, 10), "first state of a scope");
        assert!(!remember(&mut last, 1, 10), "same state again: dropped");
        assert!(remember(&mut last, 2, 10), "another scope is independent");
        assert!(!remember(&mut last, 1, 10));
        assert!(remember(&mut last, 1, 11), "a transition");
        assert!(remember(&mut last, 1, 10), "and back");
        for scope in 100..(100 + SCOPES as u64) {
            remember(&mut last, scope, 0);
        }
        assert_eq!(last.len(), SCOPES, "bounded");
        assert!(remember(&mut last, 1, 10), "a forgotten scope logs again");
    }

    #[test]
    fn the_binding_predicate_names_only_the_fields_that_differ() {
        let expected = json!({"run_id": "r1", "deployment_id": "d1", "target_node_id": "n"});
        assert_eq!(mismatched(&expected, &expected), "-");
        let actual = json!({"run_id": "r2", "deployment_id": "d1"});
        assert_eq!(mismatched(&expected, &actual), "run_id,target_node_id");
        assert_eq!(
            short(&json!("stage-0123456789abcdef0123456789")),
            "stage-0123456789abcdef01"
        );
        assert_eq!(short(&json!(null)), "-");
        assert!(expires_in(&json!({}), 1.0).is_nan());
        assert_eq!(expires_in(&json!({"expires_at": 10.0}), 4.0), 6.0);
    }

    fn secs(n: u64) -> std::time::Duration {
        std::time::Duration::from_secs(n)
    }

    /// Review SR2-01: the collection attempt counts per run, and a new run starts again at 1.
    #[test]
    fn collection_attempts_count_per_run() {
        let (first, second) = ("sr3-test-run-one", "sr3-test-run-two");
        Collection::begin(first);
        Collection::reached(first, "node-a", "open");
        Collection::end(first, Some("managed_observation_captured_stale"));
        Collection::begin(first);
        assert_eq!(Collection::attempt(first), 2);
        Collection::end(first, None);
        Collection::begin(second);
        assert_eq!(Collection::attempt(second), 1);
        assert_eq!(
            Collection::attempt(first),
            0,
            "only the current run is remembered"
        );
        Collection::end(first, Some("ignored"));
    }

    struct Lines(std::sync::mpsc::Sender<String>);
    impl seed::probe::Sink for Lines {
        fn write_line(&mut self, line: &[u8]) -> std::io::Result<()> {
            self.0
                .send(String::from_utf8_lossy(line).into_owned())
                .map_err(|_| std::io::Error::other("closed"))
        }
    }

    /// Review SR2-02: at the longest run id the protocol admits (`RUN_ID_RE`, 103 characters) and
    /// 24-character node / offer ids, a refusal naming EVERY binding field still fits one probe
    /// line whole, through the real recorder. Same layouts as `owner_observation::bootstrap`,
    /// `target::stage_ack_line` and [`Collection::end`].
    #[test]
    fn the_longest_hello_reason_lines_are_not_cut() {
        let run = format!("a{}-qwen-20261118T161640Z", "b".repeat(80));
        assert_eq!(run.len(), 103);
        let node = "remote-mac-ae10921ca8114f";
        let offer = "stage-0123456789abcdef0123";
        let fields = [
            "target_node_id",
            "target_endpoint_id",
            "target_key_digest",
            "membership_generation",
            "member_incarnation",
            "run_id",
            "deployment_id",
        ];
        let expected: serde_json::Value =
            fields.iter().map(|f| (f.to_string(), json!(1))).collect();
        let why = format!("binding_mismatch:{}", mismatched(&expected, &json!({})));
        let (tx, rx) = std::sync::mpsc::channel();
        let recorder = seed::probe::Recorder::with_sink(4, Box::new(Lines(tx)));
        recorder.record(format_args!(
            "l11_hello_owner_bootstrap wall_ms={} node={} run={run} offer={} outcome=offer_response_invalid reason={why} offer_expires_in_s={:.1}",
            wall_ms(),
            short(&json!(node)),
            short(&json!(offer)),
            -12345.6_f64,
        ));
        recorder.record(format_args!(
            "l11_hello_stage_ack wall_ms={} side=member node={} run={run} offer={} removed=false outcome=offer_response_invalid reason=answer_binding:{}",
            wall_ms(),
            short(&json!(node)),
            short(&json!(offer)),
            mismatched(&expected, &json!({})),
        ));
        recorder.record(format_args!(
            "l11_hello_collect wall_ms={} node={} run={run} collection=18446744073709551615 op=validate outcome=managed_observation_captured_stale reached=binding_current collect_ms=340282366920938463463374607431768211455",
            wall_ms(),
            short(&json!(node)),
        ));
        for _ in 0..3 {
            let line = rx
                .recv_timeout(secs(10))
                .expect("the recorder wrote the line");
            assert!(!line.ends_with("...\n"), "cut: {line}");
            assert!(line.len() < seed::probe::LINE_BYTES, "{}", line.len());
            assert!(line.contains(&run), "{line}");
            if line.contains("reason=") {
                for field in fields {
                    assert!(line.contains(field), "{field} lost: {line}");
                }
            }
        }
    }
}
