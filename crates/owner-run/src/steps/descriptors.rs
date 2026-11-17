// SPDX-License-Identifier: AGPL-3.0-only
//! `_step_descriptors` (`demo_chain.py:562-725 @ f4e366e9`): the joined Macs report in with signed
//! descriptors. The base wait ends at `Descriptors` (180 s) unless a member is visibly downloading the
//! model (its bytes must rise within `DownloadStall`; verification after the last byte has its own
//! window), silent members are waited for while their seed lease is live (up to `MemberSilence`), and a
//! descriptor must outlive its use by `DescriptorMargin`. Every message is v0.1.4's.
use super::exchange::{
    ReportsPort, check_rollover_members, publish_participants, refuse_skewed_peers, remote_members,
    run_reports, truthy,
};
use crate::chain::{StepEnv, StepError, wait_with};
use crate::machine::OwnerRunError;
use crate::state::RunState;
use budgets::{BudgetId, Entries};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

pub(crate) fn secs(id: BudgetId) -> f64 {
    id.span(Entries::ZERO)
        .map_or(f64::NAN, |s| s.millis() as f64 / 1000.0)
}

/// `_VERIFY_PHASES`: after its last byte a member verifies, then compiles.
fn verify_phase(detail: Option<&str>) -> Option<&'static str> {
    match detail {
        Some("model_verifying") => Some("verifying"),
        Some("model_compiling") => Some("compiling"),
        _ => None,
    }
}

/// `type(x) is int` (a JSON bool is not an int, a float is not either).
fn py_int(v: Option<&Value>) -> Option<i128> {
    let n = v?.as_number()?;
    n.as_i64()
        .map(i128::from)
        .or_else(|| n.as_u64().map(i128::from))
}

/// `type(x) in (int, float)` (a bool is neither).
fn py_number(v: Option<&Value>) -> Option<f64> {
    v?.as_number()?.as_f64()
}

fn mb(value: i128) -> String {
    format!("{} MB", value.div_euclid(1_000_000))
}

/// `_descriptor_left(card, now)`: seconds until a signed descriptor expires; `None` when unreadable.
fn descriptor_left(card: &Value, now: f64) -> Option<f64> {
    let expires = card.get("statement")?.as_object()?.get("expires_at");
    let expires = py_number(expires)?;
    if expires.is_nan() {
        return None;
    }
    Some(expires - now)
}

/// `_download_note(progress, first, now, phase=)`.
fn download_note(
    progress: Option<&Value>,
    first: (i128, f64),
    now: f64,
    phase: Option<&str>,
) -> String {
    let progress = progress.filter(|p| p.is_object());
    let done = py_int(progress.and_then(|p| p.get("done_bytes")));
    let total = py_int(progress.and_then(|p| p.get("total_bytes")));
    if let Some(phase) = phase {
        let size = match total {
            Some(t) if t > 0 => format!(" ({})", mb(t)),
            _ => String::new(),
        };
        return format!("waiting for a Mac that is {phase} the downloaded model{size}");
    }
    let mut note = "waiting for a Mac that is downloading the model".to_owned();
    let (Some(done), Some(total)) = (done, total) else {
        return note;
    };
    if total <= 0 {
        return note;
    }
    note.push_str(&format!(": {} of {}", mb(done), mb(total)));
    let elapsed = now - first.1;
    if first.0 >= 0 && elapsed > 0.0 && done > first.0 {
        let rate = (done - first.0) as f64 / elapsed;
        let left = (total - done).max(0) as f64 / rate;
        let eta = if left >= 60.0 {
            format!("{} min", (left / 60.0).ceil())
        } else {
            format!("{} s", left.ceil())
        };
        note.push_str(&format!(", {:.1} MB/s, about {eta} left", rate / 1e6));
    }
    note
}

/// `_member_failure_message(code, report, stall=, silent_max=, verify=)`.
fn failure_message(
    code: &str,
    report: Option<&Value>,
    stall: f64,
    silent_max: f64,
    verify: Option<f64>,
) -> Option<String> {
    let progress = report
        .and_then(|r| r.get("progress"))
        .filter(|p| p.is_object());
    let done = py_int(progress.and_then(|p| p.get("done_bytes")));
    let total = py_int(progress.and_then(|p| p.get("total_bytes")));
    let where_ = match (done, total) {
        (Some(d), Some(t)) if t > 0 => format!(" ({} of {} downloaded)", mb(d), mb(t)),
        _ => String::new(),
    };
    match code {
        "member_download_stalled" => Some(format!(
            "The joined Mac kept reporting, but its model download made no progress for {} s{where_}.",
            stall as i64
        )),
        "member_model_verify_stalled" => Some(format!(
            "The joined Mac has the whole model{} but did not finish verifying and compiling it within {} s.",
            where_.replace("downloaded", "here"),
            verify.unwrap_or(0.0) as i64
        )),
        "member_unreachable" => Some(format!(
            "The joined Mac stopped answering while downloading the model and did not come back within {} s{where_}. Check its network, then press Start again; its download resumes.",
            silent_max as i64
        )),
        _ => None,
    }
}

struct Track {
    done: i128,
    rose: f64,
    flat: f64,
    phase: Option<&'static str>,
    polled: f64,
    heard: bool,
    first: (i128, f64),
}

/// The wait's bookkeeping (`seen`, `reasons`, `windows`, `stale`, `stale_since` in the oracle).
struct Collector {
    expected: Vec<String>,
    started: f64,
    base: f64,
    stall: f64,
    silence: f64,
    silent_max: f64,
    verify_min: f64,
    verify_rate: f64,
    margin: f64,
    seen: BTreeMap<String, Track>,
    reasons: BTreeMap<String, String>,
    windows: BTreeMap<String, f64>,
    stale: BTreeSet<String>,
    stale_since: BTreeMap<String, f64>,
}

impl Collector {
    fn usable(&self, report: Option<&Value>, wall: f64) -> bool {
        let Some(card) = report
            .and_then(|r| r.get("descriptor"))
            .filter(|c| !c.is_null())
        else {
            return false;
        };
        descriptor_left(card, wall).is_none_or(|left| left >= self.margin)
    }

    fn verify_window(&self, progress: Option<&Value>) -> f64 {
        let total = py_int(
            progress
                .filter(|p| p.is_object())
                .and_then(|p| p.get("total_bytes")),
        )
        .unwrap_or(0);
        self.verify_min.max(total as f64 / self.verify_rate)
    }

    /// `have()`: true when every expected Mac is ready; `Err(GiveUp)` is returned as `Ok(Some(true))`
    /// with `gave_up` set by the caller.
    fn have(
        &mut self,
        env: &mut dyn StepEnv,
        port: &mut dyn ReportsPort,
        state: &RunState,
    ) -> Result<Option<bool>, StepError> {
        refuse_skewed_peers(port, &self.expected)?;
        let reports = run_reports(port, state)?;
        let wall0 = env.wall();
        let ready: Vec<String> = self
            .expected
            .iter()
            .filter(|n| self.usable(reports.get(*n), wall0))
            .cloned()
            .collect();
        self.stale = self
            .expected
            .iter()
            .filter(|n| {
                !ready.contains(n)
                    && reports
                        .get(*n)
                        .and_then(|r| r.get("descriptor"))
                        .is_some_and(truthy)
            })
            .cloned()
            .collect();
        publish_participants(env, port, state)?;
        let now = env.now();
        let wall = env.wall();
        let mut live: Option<Vec<String>> = None;
        let (mut active, mut resigning) = (false, false);
        let mut notes: Vec<String> = Vec::new();
        self.reasons.clear();
        let empty = json!({});
        for node in self.expected.clone() {
            let report = reports.get(&node).unwrap_or(&empty);
            if self.stale.contains(&node) {
                // A live member re-signs within half a descriptor's life; one stale for a whole stall
                // budget is not coming back.
                let since = *self.stale_since.entry(node.clone()).or_insert(now);
                if now - since < self.stall {
                    resigning = true;
                }
                continue;
            }
            self.stale_since.remove(&node);
            let rstate = report.get("state").and_then(Value::as_str);
            let downloading = rstate == Some("downloading_model")
                || (self.seen.contains_key(&node) && rstate == Some("error"));
            if ready.contains(&node) || !downloading {
                if rstate == Some("error")
                    && let Some(detail) = report.get("detail").filter(|d| truthy(d))
                {
                    self.reasons
                        .insert(node.clone(), detail.as_str().unwrap_or_default().to_owned());
                }
                continue;
            }
            let progress = report.get("progress").filter(|p| truthy(p));
            let done = py_int(progress.and_then(|p| p.get("done_bytes"))).unwrap_or(-1);
            let silent_for = py_number(report.get("received_at")).map_or(0.0, |heard| wall - heard);
            // Verifying/compiling after the last byte: its own phase and window (bytes stay flat at the
            // total by design).
            let detail = report.get("detail").and_then(Value::as_str);
            let phase = if rstate == Some("downloading_model") && verify_phase(detail).is_some() {
                detail.and_then(|d| verify_phase(Some(d)).map(|_| d))
            } else {
                None
            };
            let phase_word = phase.and_then(|d| verify_phase(Some(d)));
            let limit = match phase {
                None => self.stall,
                Some(_) => self.verify_window(report.get("progress")),
            };
            if phase.is_some() {
                self.windows.insert(node.clone(), limit);
            }
            // Per member: last byte rise (or phase change), and the time it was HEARD without one
            // (silences do not count as a stall, and do not reset it).
            let silence = self.silence;
            let track = self.seen.entry(node.clone()).or_insert(Track {
                done,
                rose: now,
                flat: 0.0,
                phase: phase_word,
                polled: now,
                heard: true,
                first: (done, now),
            });
            if done > track.done || phase_word != track.phase {
                track.done = done.max(track.done);
                track.rose = now;
                track.flat = 0.0;
                track.phase = phase_word;
            } else if silent_for < silence && track.heard {
                track.flat += now - track.polled;
            }
            track.polled = now;
            track.heard = silent_for < silence;
            let idle = now - track.rose; // bounds a member alternating silence and flat reports
            let (flat, first) = (track.flat, track.first);
            if silent_for >= silence {
                // Not heard from: a link drop, not a stalled download.
                if live.is_none() {
                    live = Some(remote_members(
                        port,
                        state.local_node.as_deref().unwrap_or_default(),
                        env.wall(),
                    ));
                }
                let alive = live.as_ref().is_some_and(|l| l.contains(&node));
                if alive && silent_for < self.silent_max && idle < self.silent_max + limit {
                    active = true;
                    notes.push(format!(
                        "the joined Mac has not been heard from for {} s (link drop?); waiting up to {} s",
                        silent_for as i64, self.silent_max as i64
                    ));
                } else {
                    self.reasons
                        .insert(node.clone(), "member_unreachable".into());
                }
                continue;
            }
            if flat < limit && idle < self.silent_max + limit {
                active = true;
                notes.push(download_note(
                    report.get("progress"),
                    first,
                    now,
                    phase_word,
                ));
            } else if phase.is_some() {
                self.reasons
                    .insert(node.clone(), "member_model_verify_stalled".into());
            } else {
                let code = if rstate == Some("error") {
                    report
                        .get("detail")
                        .filter(|d| truthy(d))
                        .and_then(Value::as_str)
                        .unwrap_or("member_download_stalled")
                } else {
                    "member_download_stalled"
                };
                self.reasons.insert(node.clone(), code.to_owned());
            }
        }
        let tail = if notes.is_empty() {
            if resigning {
                "; waiting for a Mac to re-sign its descriptor".to_owned()
            } else {
                String::new()
            }
        } else {
            format!("; {}", notes.join("; "))
        };
        env.progress(&format!(
            "{} of {} Macs reported{tail}",
            ready.len(),
            self.expected.len()
        ));
        if ready.len() == self.expected.len() {
            return Ok(Some(true));
        }
        if now - self.started >= self.base && !(active || resigning) {
            return Ok(Some(false)); // GiveUp: proceed with the Macs that reported
        }
        Ok(None)
    }
}

fn exception(class: &str) -> StepError {
    StepError::Exception {
        class: class.to_owned(),
    }
}

/// `c['statement']['target_node_id']` with Python's exception classes for a malformed card.
fn target_node_id(card: &Value) -> Result<String, StepError> {
    let object = card.as_object().ok_or_else(|| exception("TypeError"))?;
    let statement = object
        .get("statement")
        .ok_or_else(|| exception("KeyError"))?;
    let statement = statement
        .as_object()
        .ok_or_else(|| exception("TypeError"))?;
    match statement.get("target_node_id") {
        None => Err(exception("KeyError")),
        Some(Value::String(s)) => Ok(s.clone()),
        Some(_) => Err(exception("TypeError")),
    }
}

/// Runs the step. On success `state.participants`, `state.descriptors` and (start) the rollover binding
/// are set.
pub fn run_descriptors_step(
    env: &mut dyn StepEnv,
    port: &mut dyn ReportsPort,
    state: &mut RunState,
    interval: f64,
) -> Result<(), StepError> {
    port.forget_reports();
    super::exchange::announce(port, state, "collect", None)?;
    let local = state.local_node.clone().unwrap_or_default();
    let mut expected = vec![local.clone()];
    let wall = env.wall();
    expected.extend(remote_members(port, &local, wall));
    state.participants.clone_from(&expected);

    let started = env.now();
    let mut collector = Collector {
        expected: expected.clone(),
        started,
        base: secs(BudgetId::Descriptors),
        stall: secs(BudgetId::DownloadStall),
        silence: secs(BudgetId::ReportSilence),
        silent_max: secs(BudgetId::MemberSilence),
        verify_min: secs(BudgetId::VerifyStall),
        verify_rate: match BudgetId::VerifyRate.read_entry().value {
            budgets::table::Value::BytesPerSecond(rate) if rate.get() > 0 => rate.get() as f64,
            _ => return Err(exception("BudgetError")),
        },
        margin: secs(BudgetId::DescriptorMargin),
        seen: BTreeMap::new(),
        reasons: BTreeMap::new(),
        windows: BTreeMap::new(),
        stale: BTreeSet::new(),
        stale_since: BTreeMap::new(),
    };
    // Log-only: keep the probes reachable even when the wait itself fails, which is the case that
    // actually stops the run ("N of M Macs reported") -- the `?` used to skip them entirely.
    let waited = wait_with(
        env,
        |env| {
            port.tick_local();
            collector.have(env, port, state)
        },
        f64::INFINITY,
        "member_descriptors_timeout",
        interval,
    )
    .map(|_| ());
    eprintln!("l10_desc_wait:{:?}", waited.as_ref().map(|_| "ok"));
    eprintln!("l10_desc_expected:{expected:?}");
    eprintln!(
        "l10_desc_seen:{:?}",
        collector.seen.keys().collect::<Vec<_>>()
    );
    eprintln!("l10_desc_reasons:{:?}", collector.reasons);
    waited?;
    // The step proceeds with the Macs that reported; the compiler decides if enough.
    let reports = run_reports(port, state)?;
    let wall = env.wall();
    let fresh: Vec<&String> = expected
        .iter()
        .filter(|n| collector.usable(reports.get(*n), wall))
        .collect();
    let mut cards: Vec<Value> = Vec::new();
    for node in &fresh {
        cards.push(
            reports[*node]
                .get("descriptor")
                .cloned()
                .unwrap_or(Value::Null),
        );
    }
    if cards.len() < 2 {
        if expected.iter().any(|n| {
            !fresh.contains(&n)
                && reports
                    .get(n)
                    .and_then(|r| r.get("descriptor"))
                    .is_some_and(truthy)
        }) {
            return Err(OwnerRunError::new("member_descriptor_stale").into());
        }
        if let Some(missing) = expected
            .iter()
            .find(|n| !fresh.contains(n) && collector.reasons.contains_key(*n))
        {
            // Keep the joined Mac on the page with its state and progress.
            publish_participants(env, port, state)?;
            let code = collector.reasons[missing].clone();
            let message = failure_message(
                &code,
                reports.get(missing),
                collector.stall,
                collector.silent_max,
                collector.windows.get(missing).copied(),
            );
            return Err(OwnerRunError::with(&code, message.as_deref(), None).into());
        }
    }
    let mut keyed: Vec<(String, Value)> = Vec::new();
    for card in &cards {
        keyed.push((target_node_id(card)?, card.clone()));
    }
    let mut participants: Vec<String> = keyed.iter().map(|(n, _)| n.clone()).collect();
    participants.sort();
    keyed.sort_by(|a, b| a.0.cmp(&b.0));
    state.participants = participants;
    state.descriptors = keyed.into_iter().map(|(_, c)| c).collect();
    publish_participants(env, port, state)?;
    if state.reason == crate::machine::Reason::Rollover {
        let binding = state
            .rollover_binding
            .clone()
            .ok_or_else(|| exception("KeyError"))?;
        let wall = env.wall();
        check_rollover_members(port, &local, wall, &binding)?;
        let mut want: Vec<String> = binding
            .get("members")
            .and_then(Value::as_object)
            .map(|m| m.keys().cloned().collect())
            .unwrap_or_default();
        want.sort();
        if state.participants != want {
            return Err(OwnerRunError::new("rollover_members_changed").into());
        }
    } else {
        let source = state.prepared().ok_or_else(|| exception("KeyError"))?;
        let mut members = serde_json::Map::new();
        for node in state.participants.clone() {
            members.insert(node.clone(), port.member_identity(&node));
        }
        state.rollover_binding = Some(json!({
            "model": [source.model.model_id, source.model.resolved_commit, source.manifest_digest],
            "members": Value::Object(members),
        }));
    }
    if cards.len() < 2 {
        return Err(OwnerRunError::new("two_macs_required").into());
    }
    Ok(())
}
