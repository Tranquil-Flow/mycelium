// SPDX-License-Identifier: AGPL-3.0-only
//! Replay retention at a typed transaction/persistence seam. Accounted bytes are
//! synthetic page charges, never a SQLite physical-size measurement.
use super::*;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
#[cfg(test)]
#[path = "mailbox_tests.rs"]
mod tests;

#[derive(Clone, Serialize, Deserialize)]
struct Row {
    sequence: u64,
    nonce: u64,
    request: u64,
    valid_until: u64,
}
#[derive(Clone, Serialize, Deserialize)]
struct CommandRow {
    terminal: bool,
    retired_at: Option<u64>,
    updated_at: u64,
    claimed_request: Option<u64>,
}
#[derive(Clone, Default, Serialize, Deserialize)]
struct Mailbox {
    high_water: u64,
    rows: BTreeMap<u64, Row>,
    nonces: BTreeMap<u64, u64>,
    requests: BTreeMap<u64, u64>,
    // Empty historically; corrected uses a fixed non-clearing Bloom filter.
    history: Vec<u8>,
    pruned: u64,
    peak_rows: u64,
    commands: BTreeMap<u64, CommandRow>,
}
impl Mailbox {
    // Typed identities substitute for already-authenticated nonces/request IDs.
    // Four deterministic hash probes model Bloom semantics, not SHA-256/crypto.
    fn positions(&self, kind: u64, id: u64) -> [u64; 4] {
        std::array::from_fn(|i| {
            crate::random(17, id, kind, i as u64) % (self.history.len() as u64 * 8)
        })
    }
    fn historical(&mut self, kind: u64, id: u64, remember: bool) -> bool {
        if self.history.is_empty() {
            return false;
        }
        let mut present = true;
        for bit in self.positions(kind, id) {
            let byte = &mut self.history[(bit / 8) as usize];
            let mask = 1 << (bit % 8);
            if *byte & mask == 0 {
                present = false;
                if remember {
                    *byte |= mask;
                }
            }
        }
        present
    }
    fn prune(&mut self, now: u64) -> SimResult<u64> {
        let keep = count(B::ReplayKeep)? as usize;
        let eligible = self.rows.len().saturating_sub(keep);
        let keys: Vec<u64> = self
            .rows
            .iter()
            .take(eligible)
            .filter(|(_, r)| {
                !self
                    .commands
                    .values()
                    .any(|c| !c.terminal && c.claimed_request == Some(r.request))
            })
            .take(count(B::RetentionBatch)? as usize)
            .filter(|(_, r)| r.valid_until < now)
            .map(|(&s, _)| s)
            .collect();
        for key in &keys {
            let r = self.rows.remove(key).ok_or("prune_row_missing")?;
            self.historical(0, r.nonce, true);
            self.historical(1, r.request, true);
            self.nonces.remove(&r.nonce);
            self.requests.remove(&r.request);
            self.pruned += 1;
        }
        let age = ms(B::RetiredCommandAge)?;
        let mut retired: Vec<_> = self
            .commands
            .iter()
            .filter(|(_, row)| {
                row.terminal
                    && row.retired_at.is_some_and(|t| now.saturating_sub(t) > age)
                    && now.saturating_sub(row.updated_at) > age
            })
            .map(|(&id, row)| (row.retired_at, row.updated_at, id))
            .collect();
        retired.sort_unstable();
        for (_, _, id) in retired.into_iter().take(count(B::RetentionBatch)? as usize) {
            self.commands.remove(&id).ok_or("retired_command_missing")?;
        }
        Ok(keys.len() as u64)
    }
    fn retry(&self, sequence: u64, nonce: u64, request: u64, now: u64) -> bool {
        self.rows
            .get(&sequence)
            .is_some_and(|r| r.nonce == nonce && r.request == request && now < r.valid_until)
    }
    fn admit(
        &mut self,
        sequence: u64,
        nonce: u64,
        request: u64,
        now: u64,
        mode: Mode,
    ) -> SimResult<bool> {
        if sequence <= self.high_water
            || self.nonces.contains_key(&nonce)
            || self.requests.contains_key(&request)
        {
            return Ok(false);
        }
        if mode == Mode::Corrected {
            if self.history.is_empty() {
                self.history = vec![0; bytes(B::ReplayHistoryPerSigner)? as usize];
            }
            if self.historical(0, nonce, false) || self.historical(1, request, false) {
                return Ok(false);
            }
            self.prune(now)?;
            // A live full window refuses new work; no live retry is evicted.
            if self.rows.len() as u64 >= count(B::ReplayCapacity)? {
                return Ok(false);
            }
        }
        let valid_until = add(now, add(ms(B::ControlTtl)?, ms(B::ControlClockSkew)?)?)?;
        self.rows.insert(
            sequence,
            Row {
                sequence,
                nonce,
                request,
                valid_until,
            },
        );
        self.nonces.insert(nonce, sequence);
        self.requests.insert(request, sequence);
        self.high_water = sequence;
        self.peak_rows = self.peak_rows.max(self.rows.len() as u64);
        Ok(true)
    }
    fn accounted_bytes(&self) -> SimResult<u64> {
        let page = bytes(B::ReplayPage)?;
        self.peak_rows
            .checked_add(self.commands.len() as u64)
            .and_then(|n| n.checked_mul(page))
            .and_then(|n| n.checked_add(self.history.len() as u64))
            .ok_or_else(|| "mailbox_size_overflow".into())
    }
}
#[derive(Clone)]
enum Event {
    Batch(u64),
    Reopen,
    Replay,
    Fresh,
}
pub(super) fn run(c: &Config, input: &[Input]) -> SimResult<Outcome> {
    let burst = count(B::ReplayCapacity)? - count(B::ReplayKeep)?;
    let batches = bytes(B::MailboxCeiling)? / bytes(B::ReplayPage)? / burst + 1;
    let interval = add(
        add(ms(B::ControlTtl)?, ms(B::ControlClockSkew)?)?,
        ms(B::OwnerPoll)?,
    )?;
    let mut db = Mailbox::default();
    let mut next = 1;
    let mut peak = 0;
    drive(c, input, Event::Batch(0), |e, q, o| {
        let now = q.clock.mono_ms;
        match e {
            Event::Batch(batch) => {
                reached(o, "traffic_batch", now);
                reached(o, "prune_attempt", now);
                if batch == 0 {
                    db.commands.insert(
                        1,
                        CommandRow {
                            terminal: false,
                            retired_at: None,
                            updated_at: now,
                            claimed_request: Some(1),
                        },
                    );
                    db.commands.insert(
                        2,
                        CommandRow {
                            terminal: true,
                            retired_at: Some(now),
                            updated_at: now,
                            claimed_request: None,
                        },
                    );
                }
                for _ in 0..burst {
                    if db.admit(next, next, next, now, c.mode)? {
                        inc(o, "accepted");
                    } else {
                        inc(o, "traffic_refusals");
                    }
                    next += 1;
                }
                if batch == 0 {
                    put(
                        o,
                        "live_retry_preserved",
                        u64::from(db.retry(next - 1, next - 1, next - 1, now)),
                    );
                    chat(o, now);
                }
                peak = peak.max(db.accounted_bytes()?);
                if batch + 1 < batches {
                    at(q, add(now, interval)?, 1, Event::Batch(batch + 1))?;
                } else {
                    at(q, add(now, ms(B::RetiredCommandAge)?)?, 1, Event::Reopen)?;
                }
            }
            Event::Reopen => {
                reached(o, "mailbox_reopen", now);
                // Persisted data, not a fresh empty mailbox; decoder is also the
                // reopen boundary. No database or historical product is executed.
                let image = serde_json::to_vec(&db).map_err(|e| format!("snapshot_encode:{e}"))?;
                db = serde_json::from_slice(&image).map_err(|e| format!("snapshot_decode:{e}"))?;
                if c.mode == Mode::Corrected {
                    db.prune(now)?;
                }
                put(o, "peak_bytes", peak);
                put(o, "mailbox_ceiling", bytes(B::MailboxCeiling)?);
                put(o, "retained_rows", db.rows.len() as u64);
                put(o, "pruned", db.pruned);
                put(
                    o,
                    "pending_command_preserved",
                    u64::from(db.commands.contains_key(&1)),
                );
                put(
                    o,
                    "claimed_auth_preserved",
                    u64::from(db.rows.contains_key(&1)),
                );
                put(
                    o,
                    "old_nonce_pruned",
                    u64::from(!db.nonces.contains_key(&2)),
                );
                put(
                    o,
                    "retired_command_pruned",
                    u64::from(!db.commands.contains_key(&2)),
                );
                at(q, now, 2, Event::Replay)?;
                at(q, now, 3, Event::Fresh)?;
            }
            Event::Replay => {
                reached(o, "replay_attempt", now);
                // Old sequence, old nonce at a fresh sequence, old request at a
                // fresh sequence: high-water alone cannot protect the latter two.
                for (sequence, nonce, request) in
                    [(1, next, next), (next, 2, next), (next, next, 2)]
                {
                    if !db.admit(sequence, nonce, request, now, c.mode)? {
                        inc(o, "replay_refusals_after_reopen");
                    }
                    next += 1;
                }
            }
            Event::Fresh => {
                reached(o, "fresh_after_reopen", now);
                if now < c.authority_until_ms && db.admit(next, next, next, now, c.mode)? {
                    inc(o, "fresh_after_reopen");
                    chat(o, now);
                }
            }
        }
        Ok(())
    })
}
