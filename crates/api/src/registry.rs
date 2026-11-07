// SPDX-License-Identifier: AGPL-3.0-only
//! General request registry, bounded replay history and authenticated generation
//! rebinding — mycelium_request_gateway/service.py:66-123,227-275,508-620,1208-1453
//! @ f4e366e9. Pure state machine: the host owns execution, clocks and backends and
//! calls the mutators below; nothing here starts work or invents a terminal event.
//! Cursor-reclaimed history is an explicit `resume_cursor_expired`, never a skip.
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::VecDeque;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct AdmissionError(pub &'static str);
type R<T> = Result<T, AdmissionError>;
fn fail<T>(code: &'static str) -> R<T> {
    Err(AdmissionError(code))
}
#[derive(Clone, Debug, PartialEq)]
pub struct Event {
    pub sequence: i64,
    pub kind: String,
    pub payload: Value,
    /// Set on delivery: the subscription's publisher generation.
    pub publisher_generation: Option<u64>,
}
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Config {
    pub max_buffered_events: usize,
    pub max_sessions: usize,
    /// Seconds a never-subscribed in-flight request is protected from eviction.
    pub eviction_grace: f64,
}
impl Config {
    /// service.py:315-326 construction ranges.
    pub fn new(max_buffered_events: usize, max_sessions: usize, eviction_grace: f64) -> R<Self> {
        if !(2..=65_536).contains(&max_buffered_events) {
            return fail("invalid_max_buffered_events");
        }
        if !(1..=65_536).contains(&max_sessions) {
            return fail("invalid_max_sessions");
        }
        Ok(Self {
            max_buffered_events,
            max_sessions,
            eviction_grace,
        })
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LastEventId {
    /// Plain `Last-Event-ID: n`: bound to the session's current generation.
    Cursor(i64),
    /// `(publisher_generation, cursor)` from a prior stream.
    Bound(i64, i64),
}
struct Session {
    request_id: String,
    events: VecDeque<Event>,
    latest_sequence: i64,
    discarded_through: i64,
    acknowledged_through: i64,
    terminal: Option<String>,
    worker_done: bool,
    active_subscription: bool,
    ever_subscribed: bool,
    cancellation_started: bool,
    publisher_generation: u64,
    owner_digest: Option<[u8; 32]>,
    created_at: f64,
    last_ack_at: Option<f64>,
    maximum_buffered: usize,
}
/// A claimed single-consumer stream. Dropping it does not close; call `close`.
#[derive(Debug)]
pub struct Subscription {
    pub request_id: String,
    pub publisher_generation: u64,
    cursor: i64,
    delivered: i64,
    closed: bool,
}
#[derive(Debug, PartialEq)]
pub enum Pull {
    Event(Event),
    /// Terminal delivered; the stream ends cleanly.
    End,
    /// Nothing newer yet; the host waits within its own bounded poll budget.
    Pending,
}
pub struct Registry {
    config: Config,
    sessions: Vec<Session>,
    closed: bool,
}
fn digest(token: &str) -> [u8; 32] {
    Sha256::digest(token.as_bytes()).into()
}
fn equal(a: &[u8; 32], b: &[u8; 32]) -> bool {
    a.iter().zip(b).fold(0u8, |n, (x, y)| n | (x ^ y)) == 0
}
/// contracts.py:27 `[A-Za-z0-9][A-Za-z0-9._~-]{0,127}`
pub fn valid_request_id(id: &str) -> bool {
    let b = id.as_bytes();
    !b.is_empty()
        && b.len() <= 128
        && b[0].is_ascii_alphanumeric()
        && b.iter()
            .all(|c| c.is_ascii_alphanumeric() || b"._~-".contains(c))
}
impl Registry {
    pub fn new(config: Config) -> Self {
        Self {
            config,
            sessions: Vec::new(),
            closed: false,
        }
    }
    pub fn len(&self) -> usize {
        self.sessions.len()
    }
    pub fn is_empty(&self) -> bool {
        self.sessions.is_empty()
    }
    pub fn close(&mut self) {
        self.closed = true;
    }
    fn find(&self, id: &str) -> R<usize> {
        if id.is_empty() {
            return fail("unknown_request");
        }
        self.sessions
            .iter()
            .position(|s| s.request_id == id)
            .ok_or(AdmissionError("unknown_request"))
    }
    fn owner(&self, at: usize, token: Option<&str>) -> R<()> {
        let Some(expected) = &self.sessions[at].owner_digest else {
            return Ok(());
        };
        match token {
            Some(t) if equal(&digest(t), expected) => Ok(()),
            _ => fail("session_owner_mismatch"),
        }
    }
    /// service.py:1397-1453. Returns the evicted sessions' ids whose execution the
    /// host must cancel (abandoned non-terminal ones).
    fn make_room(&mut self, now: f64, cancelled: &mut Vec<String>) -> R<()> {
        while self.sessions.len() >= self.config.max_sessions {
            let terminal = self.sessions.iter().position(|s| {
                s.terminal.as_ref().is_some_and(|kind| {
                    !s.active_subscription
                        && (s.worker_done || matches!(kind.as_str(), "completed" | "failed"))
                })
            });
            if let Some(at) = terminal {
                self.sessions.remove(at);
                continue;
            }
            let grace = self.config.eviction_grace;
            let abandoned = self.sessions.iter().position(|s| {
                s.terminal.is_none()
                    && !s.active_subscription
                    && (s.ever_subscribed || now - s.created_at >= grace)
            });
            match abandoned {
                Some(at) => {
                    cancelled.push(self.sessions.remove(at).request_id);
                }
                None => return fail("gateway_capacity_exhausted"),
            }
        }
        Ok(())
    }
    /// Admission bookkeeping for one request (service.py:440-506 after qualification
    /// capture): token bound, closed gateway, room, id validity and uniqueness, and the
    /// `accepted` event at sequence 0. `cancelled` receives evicted in-flight ids.
    pub fn submit(
        &mut self,
        request_id: &str,
        owner_token: Option<&str>,
        now: f64,
        cancelled: &mut Vec<String>,
    ) -> R<()> {
        self.submit_at_generation(request_id, owner_token, now, 0, cancelled)
    }
    /// `submit` for a signed adapter whose immutable grant already carries generation `g`: the
    /// session starts at `g - 1` so the first stream is generation `g` (the oracle's real flow is
    /// `g = 1` from `initial_generation = 0`, backend.py:650-676).
    pub fn submit_at_generation(
        &mut self,
        request_id: &str,
        owner_token: Option<&str>,
        now: f64,
        initial_generation: u64,
        cancelled: &mut Vec<String>,
    ) -> R<()> {
        self.submit_drawing(
            &mut || request_id.into(),
            owner_token,
            now,
            initial_generation,
            cancelled,
        )
        .map(|_| ())
    }
    /// `submit` for a host that owns the request-id source (service.py:440-506): the id is drawn only once the
    /// token is bounded, the gateway is open and there is room, so an earlier refusal consumes no id; the drawn
    /// id is then checked for validity and uniqueness. Returns the admitted id.
    pub fn submit_drawing(
        &mut self,
        draw: &mut dyn FnMut() -> String,
        owner_token: Option<&str>,
        now: f64,
        initial_generation: u64,
        cancelled: &mut Vec<String>,
    ) -> R<String> {
        if owner_token.is_some_and(|t| !(32..=256).contains(&t.len())) {
            return fail("invalid_session_token");
        }
        if self.closed {
            return fail("gateway_closed");
        }
        self.make_room(now, cancelled)?;
        let request_id = draw();
        if !valid_request_id(&request_id) {
            return fail("invalid_request_id");
        }
        if self.sessions.iter().any(|s| s.request_id == request_id) {
            return fail("duplicate_request_id");
        }
        let accepted = Event {
            sequence: 0,
            kind: "accepted".into(),
            payload: Value::Null,
            publisher_generation: None,
        };
        self.sessions.push(Session {
            request_id: request_id.clone(),
            events: VecDeque::from([accepted]),
            latest_sequence: 0,
            discarded_through: -1,
            acknowledged_through: -1,
            terminal: None,
            worker_done: false,
            active_subscription: false,
            ever_subscribed: false,
            cancellation_started: false,
            publisher_generation: initial_generation,
            owner_digest: owner_token.map(digest),
            created_at: now,
            last_ack_at: None,
            maximum_buffered: 1,
        });
        Ok(request_id)
    }
    /// service.py:508-576. `sync(id, expected, new)` is the host's authenticated
    /// generation handoff; anything but `Ok(true)` detaches, cancels, and refuses.
    pub fn subscribe(
        &mut self,
        request_id: &str,
        last_event_id: Option<LastEventId>,
        owner_token: Option<&str>,
        now: f64,
        sync: &mut dyn FnMut(&str, u64, u64) -> Result<bool, ()>,
    ) -> R<Subscription> {
        let at = self.find(request_id)?;
        self.owner(at, owner_token)?;
        let session_generation = i64::try_from(self.sessions[at].publisher_generation)
            .map_err(|_| AdmissionError("invalid_last_event_id"))?;
        let (previous, cursor) = match last_event_id {
            Some(LastEventId::Bound(g, c)) => (g, c),
            Some(LastEventId::Cursor(c)) => (session_generation, c),
            None => (session_generation, -1),
        };
        if cursor < -1 || previous < 0 {
            return fail("invalid_last_event_id");
        }
        let s = &mut self.sessions[at];
        if last_event_id.is_some() && previous != session_generation {
            return fail("stale_publisher_generation");
        }
        if s.active_subscription {
            return fail("stream_already_attached");
        }
        if cursor < s.discarded_through {
            return fail("resume_cursor_expired");
        }
        if cursor > s.latest_sequence {
            return fail("invalid_last_event_id");
        }
        while s.events.front().is_some_and(|e| e.sequence < cursor) {
            s.events.pop_front();
        }
        s.discarded_through = s.discarded_through.max(cursor - 1);
        s.acknowledged_through = s.acknowledged_through.max(cursor);
        s.active_subscription = true;
        s.ever_subscribed = true;
        s.last_ack_at = Some(now);
        let expected = s.publisher_generation;
        s.publisher_generation += 1;
        let subscription = Subscription {
            request_id: request_id.into(),
            publisher_generation: s.publisher_generation,
            cursor,
            delivered: cursor,
            closed: false,
        };
        if sync(request_id, expected, subscription.publisher_generation) != Ok(true) {
            s.active_subscription = false;
            s.cancellation_started = s.cancellation_started || s.terminal.is_none();
            return fail("publisher_generation_sync_failed");
        }
        Ok(subscription)
    }
    /// service.py:1222-1259, non-blocking: the host owns waiting.
    pub fn next_event(&mut self, sub: &mut Subscription) -> R<Pull> {
        if sub.closed {
            return fail("stream_closed");
        }
        let s = &self.sessions[self.find(&sub.request_id)?];
        if sub.cursor < s.discarded_through {
            return fail("resume_cursor_expired");
        }
        if let Some(event) = s.events.iter().find(|e| e.sequence > sub.cursor) {
            let mut event = event.clone();
            event.publisher_generation = Some(sub.publisher_generation);
            sub.delivered = event.sequence;
            return Ok(Pull::Event(event));
        }
        // The terminal is always the latest sequence (service.py:1244-1246).
        let ended = s.terminal.is_some() && sub.cursor >= s.latest_sequence;
        Ok(if ended { Pull::End } else { Pull::Pending })
    }
    /// service.py:1261-1275.
    pub fn ack(&mut self, sub: &mut Subscription, sequence: i64, now: f64) -> R<()> {
        if sub.closed {
            return fail("stream_closed");
        }
        let at = self.find(&sub.request_id)?;
        let s = &mut self.sessions[at];
        if sub.publisher_generation != s.publisher_generation
            || sequence <= sub.cursor
            || sequence > sub.delivered
        {
            return fail("invalid_stream_ack");
        }
        sub.cursor = sequence;
        s.acknowledged_through = s.acknowledged_through.max(sequence);
        s.last_ack_at = Some(now);
        Ok(())
    }
    /// Closing a stream never cancels inference.
    pub fn close_subscription(&mut self, sub: &mut Subscription) -> R<()> {
        if sub.closed {
            return Ok(());
        }
        sub.closed = true;
        let at = self.find(&sub.request_id)?;
        self.sessions[at].active_subscription = false;
        Ok(())
    }
    /// service.py:578-620 registry half. False when terminal or already cancelling.
    pub fn cancel(&mut self, request_id: &str, owner_token: Option<&str>) -> R<bool> {
        let at = self.find(request_id)?;
        self.owner(at, owner_token)?;
        let s = &mut self.sessions[at];
        if s.terminal.is_some() || s.cancellation_started {
            return Ok(false);
        }
        s.cancellation_started = true;
        Ok(true)
    }
    /// Worker side: append a non-terminal event (service.py:1208-1220).
    pub fn append(&mut self, request_id: &str, kind: &str, payload: Value, now: f64) -> R<i64> {
        let at = self.find(request_id)?;
        let s = &mut self.sessions[at];
        if s.terminal.is_some() {
            return fail("terminal_already_published");
        }
        if s.events.len() >= self.config.max_buffered_events {
            return fail("event_backpressure");
        }
        Ok(push(s, kind, payload, now))
    }
    /// service.py:1286-1297 `_trim_acknowledged_replay_locked`: drop acknowledged events from
    /// the front while more than `target_size` are buffered; the replay boundary follows.
    pub fn trim_acknowledged(&mut self, request_id: &str, target_size: usize) -> R<()> {
        let at = self.find(request_id)?;
        let s = &mut self.sessions[at];
        while s.events.len() > target_size
            && s.events
                .front()
                .is_some_and(|e| e.sequence <= s.acknowledged_through)
        {
            if let Some(removed) = s.events.pop_front() {
                s.discarded_through = s.discarded_through.max(removed.sequence);
            }
        }
        Ok(())
    }
    /// Worker side: the one terminal (service.py:1024-1077). Only already
    /// acknowledged history is trimmed to make room; a second terminal is ignored.
    pub fn append_terminal(
        &mut self,
        request_id: &str,
        kind: &str,
        payload: Value,
        now: f64,
    ) -> R<()> {
        let at = self.find(request_id)?;
        let capacity = self.config.max_buffered_events;
        let s = &mut self.sessions[at];
        if s.terminal.is_some() {
            return Ok(());
        }
        while s.events.len() > capacity - 1
            && s.events
                .front()
                .is_some_and(|e| e.sequence <= s.acknowledged_through)
        {
            if let Some(removed) = s.events.pop_front() {
                s.discarded_through = s.discarded_through.max(removed.sequence);
            }
        }
        if s.events.len() >= capacity {
            return fail("terminal_event_capacity_invariant");
        }
        push(s, kind, payload, now);
        s.terminal = Some(kind.into());
        Ok(())
    }
    /// service.py:1299-1313: a vanished consumer's oldest event is dropped; only the
    /// replay boundary moves, never the committed-delivery watermark.
    pub fn drop_oldest_unacknowledged(&mut self, request_id: &str) -> R<()> {
        let at = self.find(request_id)?;
        let s = &mut self.sessions[at];
        if let Some(removed) = s.events.pop_front() {
            s.discarded_through = s.discarded_through.max(removed.sequence);
        }
        Ok(())
    }
    pub fn mark_worker_done(&mut self, request_id: &str) -> R<()> {
        let at = self.find(request_id)?;
        self.sessions[at].worker_done = true;
        Ok(())
    }
    /// The gateway's own cancellation (`_cancel_session`): no owner check. False when already terminal or
    /// cancelling.
    pub fn cancel_unchecked(&mut self, request_id: &str) -> R<bool> {
        let at = self.find(request_id)?;
        let s = &mut self.sessions[at];
        if s.terminal.is_some() || s.cancellation_started {
            return Ok(false);
        }
        s.cancellation_started = true;
        Ok(true)
    }
    pub fn contains(&self, request_id: &str) -> bool {
        self.find(request_id).is_ok()
    }
    /// `None` when the request is unknown (never admitted, or evicted).
    pub fn worker_done(&self, request_id: &str) -> Option<bool> {
        self.find(request_id)
            .ok()
            .map(|at| self.sessions[at].worker_done)
    }
    pub fn terminal(&self, request_id: &str) -> Option<&str> {
        self.find(request_id)
            .ok()
            .and_then(|at| self.sessions[at].terminal.as_deref())
    }
    /// `_close_subscription(session)`: detach whatever stream is attached (a stalled consumer).
    pub fn detach_subscription(&mut self, request_id: &str) -> R<()> {
        let at = self.find(request_id)?;
        self.sessions[at].active_subscription = false;
        Ok(())
    }
    pub fn active_subscription(&self, request_id: &str) -> bool {
        self.find(request_id)
            .is_ok_and(|at| self.sessions[at].active_subscription)
    }
    pub fn latest_sequence(&self, request_id: &str) -> R<i64> {
        Ok(self.sessions[self.find(request_id)?].latest_sequence)
    }
    pub fn buffered_event_count(&self, request_id: &str) -> R<usize> {
        Ok(self.sessions[self.find(request_id)?].events.len())
    }
    pub fn maximum_observed_buffered_events(&self, request_id: &str) -> R<usize> {
        Ok(self.sessions[self.find(request_id)?].maximum_buffered)
    }
    pub fn acknowledged_through(&self, request_id: &str) -> R<i64> {
        Ok(self.sessions[self.find(request_id)?].acknowledged_through)
    }
    pub fn discarded_through(&self, request_id: &str) -> R<i64> {
        Ok(self.sessions[self.find(request_id)?].discarded_through)
    }
    pub fn publisher_generation(&self, request_id: &str) -> R<u64> {
        Ok(self.sessions[self.find(request_id)?].publisher_generation)
    }
    pub fn last_ack_at(&self, request_id: &str) -> R<Option<f64>> {
        Ok(self.sessions[self.find(request_id)?].last_ack_at)
    }
}
fn push(s: &mut Session, kind: &str, payload: Value, now: f64) -> i64 {
    if s.active_subscription && s.latest_sequence <= s.acknowledged_through {
        s.last_ack_at = Some(now); // nothing was owed until this event
    }
    let sequence = s.latest_sequence + 1;
    s.events.push_back(Event {
        sequence,
        kind: kind.into(),
        payload,
        publisher_generation: None,
    });
    s.latest_sequence = sequence;
    s.maximum_buffered = s.maximum_buffered.max(s.events.len());
    sequence
}
