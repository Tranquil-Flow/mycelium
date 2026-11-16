// SPDX-License-Identifier: AGPL-3.0-only
//! Lines for a failure that repeats (lane FLK patch 2, FLK-05): a control connect or a member
//! result poll can fail every poll (100 ms) for a whole operation (600 s). The first failure of a
//! key is printed at once; its repeats are counted, and at most one summary line per `period`
//! carries the count and the latest failure's fields; when the key ends (a success, or the caller
//! gave up) one outcome line says how many failures it had. The caller passes `now`, so the rate
//! is tested with an injected clock.
use std::{
    collections::HashMap,
    hash::Hash,
    time::{Duration, Instant},
};

/// What to print for one failure of a key.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Line {
    /// The key's first failure: print it in full.
    First,
    /// A summary: `repeats` failures since the last line, `failures` in all, `over_ms` since the
    /// first; print it with the latest failure's fields.
    Summary {
        repeats: u64,
        failures: u64,
        over_ms: u128,
    },
}
/// How a key that had failed ended.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Outcome {
    pub failures: u64,
    pub over_ms: u128,
}
struct Entry {
    first: Instant,
    printed: Instant,
    last: Instant,
    failures: u64,
    quiet: u64,
}
/// Repeated failures per key, with at most one line per key per `period` after the first.
pub struct Repeats<K> {
    period: Duration,
    keys: HashMap<K, Entry>,
}
impl<K: Eq + Hash + Clone> Repeats<K> {
    pub fn new(period: Duration) -> Self {
        Self {
            period,
            keys: HashMap::new(),
        }
    }
    /// One failure of `key` at `now`: the line to print, or `None` (counted, not printed).
    pub fn failed(&mut self, key: K, now: Instant) -> Option<Line> {
        let entry = self.keys.entry(key).or_insert(Entry {
            first: now,
            printed: now,
            last: now,
            failures: 0,
            quiet: 0,
        });
        entry.failures += 1;
        entry.last = now;
        if entry.failures == 1 {
            return Some(Line::First);
        }
        entry.quiet += 1;
        if now.saturating_duration_since(entry.printed) < self.period {
            return None;
        }
        let repeats = entry.quiet;
        entry.quiet = 0;
        entry.printed = now;
        Some(Line::Summary {
            repeats,
            failures: entry.failures,
            over_ms: now.saturating_duration_since(entry.first).as_millis(),
        })
    }
    /// `key` ended at `now`: its outcome when it had failed.
    pub fn ended(&mut self, key: &K, now: Instant) -> Option<Outcome> {
        self.keys.remove(key).map(|entry| Outcome {
            failures: entry.failures,
            over_ms: now.saturating_duration_since(entry.first).as_millis(),
        })
    }
    /// Every key `matches` selects ended at `now`, with its outcome.
    pub fn ended_where(&mut self, matches: impl Fn(&K) -> bool, now: Instant) -> Vec<(K, Outcome)> {
        let ended: Vec<K> = self.keys.keys().filter(|k| matches(k)).cloned().collect();
        ended
            .into_iter()
            .filter_map(|key| self.ended(&key, now).map(|outcome| (key, outcome)))
            .collect()
    }
    /// Forget keys with no failure for a whole `period` (a caller whose keys never end keeps its
    /// table bounded this way; a forgotten key that fails again starts with a first line).
    pub fn forget_idle(&mut self, now: Instant) {
        let period = self.period;
        self.keys
            .retain(|_, entry| now.saturating_duration_since(entry.last) < period);
    }
    /// [`Repeats::failed`] as the text to print: `head fields` for the first failure, the latest
    /// failure's `head fields` with the counts for a summary.
    pub fn line(&mut self, key: K, now: Instant, head: &str, fields: &str) -> Option<String> {
        Some(match self.failed(key, now)? {
            Line::First => format!("{head} {fields}"),
            Line::Summary {
                repeats,
                failures,
                over_ms,
            } => format!("{head} {fields} repeats={repeats} failures={failures} over_ms={over_ms}"),
        })
    }
    /// [`Repeats::ended`] as the text to print, for a key that had failed.
    pub fn outcome_line(&mut self, key: &K, now: Instant, head: &str, outcome: &str) -> Option<String> {
        self.ended(key, now).map(|o| {
            format!(
                "{head} outcome={outcome} failures={} over_ms={}",
                o.failures, o.over_ms
            )
        })
    }
}

/// The period of the summary lines: the registered interval at which a member reports its state
/// (`PyFinalMyceliumA13MacosDemoTargetReportIntervalL177C82`, 10 s, demo_target.py:177), reused
/// for these diagnostics (no budget of their own is registered).
pub fn period() -> Duration {
    budgets::BudgetId::PyFinalMyceliumA13MacosDemoTargetReportIntervalL177C82
        .span(budgets::Entries::ZERO)
        .map_or(Duration::ZERO, |span| Duration::from_millis(span.millis()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use budgets::BudgetId as B;
    fn span(id: B) -> Duration {
        Duration::from_millis(id.span(budgets::Entries::ZERO).unwrap().millis())
    }
    /// A key failing every 100 ms for 600 s (the result-poll pace and the operation wall of
    /// FLK-05), on an injected clock: one first line, at most one summary per period, one outcome.
    #[test]
    fn a_failure_repeated_every_poll_for_a_whole_operation_prints_a_bounded_number_of_lines() {
        let period = period();
        let mut repeats = Repeats::new(period);
        let start = Instant::now();
        // The owner's result-poll pace and an operation's wall (`joined_owner/remote.rs:66-70`).
        let (poll, wall) = (
            span(B::PyMyceliumA13MacosManagedControlHttpRetryInitialSecondsL317),
            span(B::MemberChosenWall),
        );
        let polls = (wall.as_millis() / poll.as_millis()) as u32;
        let mut lines = Vec::new();
        let mut texts = Vec::new();
        let mut text = Repeats::new(period);
        for i in 0..polls {
            let now = start + poll * i;
            if let Some(line) = repeats.failed("op 3", now) {
                lines.push((now, line));
            }
            // The same failures as the printed text, with the call's own fields.
            let fields = format!("member=e50ab3b324 remote=[10.0.0.2:61856] attempt={} operation_ms={}", i + 1, (poll * i).as_millis());
            if let Some(line) = text.line(3u64, now, "member result poll 3 (prepare_native): control_connect_timeout", &fields) {
                texts.push(line);
            }
        }
        assert_eq!(texts.len(), lines.len(), "one text per line");
        assert!(texts[0].ends_with("attempt=1 operation_ms=0"), "{}", texts[0]);
        let last = texts.last().unwrap();
        assert!(
            last.contains("member=e50ab3b324 remote=[10.0.0.2:61856] attempt=")
                && last.contains(" repeats=")
                && last.contains(" failures="),
            "a summary keeps the latest failure's fields: {last}"
        );
        let end = text
            .outcome_line(&3, start + wall, "member result poll 3 (prepare_native)", "member_operation_wall_exceeded")
            .unwrap();
        assert!(end.ends_with(&format!("outcome=member_operation_wall_exceeded failures={polls} over_ms={}", wall.as_millis())), "{end}");
        let outcome = repeats.ended(&"op 3", start + wall);
        let bound = 1 + (wall.as_millis() / period.as_millis()) as usize;
        assert!(
            lines.len() <= bound,
            "{} lines for {polls} failures (at most {bound})",
            lines.len()
        );
        assert_eq!(lines.first().map(|l| l.1), Some(Line::First));
        for pair in lines.windows(2) {
            assert!(pair[1].0 - pair[0].0 >= period, "two lines within one period");
        }
        let summarised: u64 = lines
            .iter()
            .map(|(_, line)| match line {
                Line::First => 1,
                Line::Summary { repeats, .. } => *repeats,
            })
            .sum();
        assert!(summarised <= u64::from(polls), "no failure counted twice");
        assert_eq!(
            outcome,
            Some(Outcome {
                failures: u64::from(polls),
                over_ms: wall.as_millis()
            })
        );
        assert_eq!(repeats.ended(&"op 3", start + wall), None, "ended once");
    }
    #[test]
    fn keys_are_counted_apart_and_idle_ones_are_forgotten() {
        let period = period();
        let mut repeats = Repeats::new(period);
        let start = Instant::now();
        assert_eq!(repeats.failed(("a", 1), start), Some(Line::First));
        assert_eq!(repeats.failed(("b", 1), start), Some(Line::First));
        assert_eq!(repeats.failed(("a", 1), start + period), Some(Line::Summary {
            repeats: 1,
            failures: 2,
            over_ms: period.as_millis()
        }));
        repeats.forget_idle(start + period);
        let ended = repeats.ended_where(|k| k.1 == 1, start + period);
        assert_eq!(ended.len(), 1, "b had no failure for a whole period: forgotten");
        assert_eq!(ended[0].0, ("a", 1));
    }
}
