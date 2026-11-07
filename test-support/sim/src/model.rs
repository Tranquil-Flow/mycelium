// SPDX-License-Identifier: AGPL-3.0-only
//! Faithful historical MODELS, not product transitions. v0.1.4 references and
//! section-7/L0 crosswalk are in README.md. All waits come from L1 BudgetIds.
use crate::{Scheduler, SimResult, span};
use budgets::{BudgetId as B, Entries};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Incident {
    F1,
    F2,
    F3,
    F4,
    F5,
    F13,
    F14,
    F15,
    F15b,
    F16,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Mode {
    Historical,
    Corrected,
    AlwaysRefuse,
    MissingProof,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Config {
    pub incident: Incident,
    pub mode: Mode,
    pub rollovers: u32,
    pub close_multiplier: u64,
    /// Fixture authority, not authentication: L5 must replace this seam. Owner
    /// local permission is distinct from member consent. This absolute cap is
    /// immutable across fresh workers; rollover never renews authority.
    pub owner_local_allowed: bool,
    pub member_granted: bool,
    pub authority_until_ms: u64,
}
impl Config {
    pub fn standard(incident: Incident, mode: Mode, close_multiplier: u64) -> SimResult<Self> {
        Ok(Self {
            incident,
            mode,
            rollovers: 2,
            close_multiplier,
            owner_local_allowed: true,
            member_granted: true,
            authority_until_ms: span(match incident {
                Incident::F2 => B::RetiredCommandAge,
                Incident::F13 => B::RelayGrantedWindowCap,
                _ => B::WorkerDefaultWall,
            })?
            .checked_mul(match incident {
                Incident::F2 => 2,
                Incident::F13 => 1,
                _ => 8,
            })
            .map_err(|_| "authority_overflow")?
            .millis(),
        })
    }
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkerLife {
    pub generation: u32,
    pub member_origin_ms: u64,
    pub owner_origin_ms: u64,
    pub member_expiry_ms: u64,
    pub owner_expiry_ms: u64,
    pub cleanup_by_ms: u64,
    pub closed_at_ms: Option<u64>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum Command {
    Begin,
    WallJump { delta_ms: i64 },
    Observe,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Input {
    pub at_ms: u64,
    pub task: u32,
    pub command: Command,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
enum Event {
    Input(Command),
    Start,
    Serving(u32),
    Stop(u32),
    Close(u32),
    OuterWait(u32),
    Expire(u32),
    Chat(u32),
    NextChat(u32),
    Replacement(u32),
}
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Outcome {
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub metrics: BTreeMap<String, u64>,
    pub transitions: BTreeMap<String, u32>,
    pub log: Vec<String>,
    pub starts: u32,
    pub served: u32,
    pub rollovers: u32,
    pub successful_chats: u32,
    pub interrupted_chats: u32,
    pub expiry_losses: u32,
    pub forbidden_replacements: u32,
    pub swallowed_errors: u32,
    pub diagnostics: Vec<String>,
    pub cleanup_proofs: u32,
    pub workers: Vec<WorkerLife>,
    pub deadlines: Vec<(u64, u64, u64)>, // member expiry, owner expiry, scheduled stop
}
pub(crate) fn reached(o: &mut Outcome, name: &str, now: u64) {
    *o.transitions.entry(name.into()).or_default() += 1;
    o.log.push(format!("{now}:{name}"));
}
pub(crate) fn add(a: u64, b: u64) -> SimResult<u64> {
    a.checked_add(b).ok_or_else(|| "time_overflow".into())
}
pub(crate) fn sub(a: u64, b: u64) -> SimResult<u64> {
    a.checked_sub(b).ok_or_else(|| "time_underflow".into())
}
/// Complete event stream is deterministic; late completions retain generation.
pub fn run(config: &Config, input: &[Input]) -> SimResult<Outcome> {
    if config.incident.is_extended() {
        return crate::incidents::run(config, input);
    }
    if config.rollovers == 0 || config.rollovers > 16 || !(1..=3).contains(&config.close_multiplier)
    {
        return Err("invalid_config".into());
    }
    let mut q = Scheduler::default();
    for i in input {
        q.at(i.at_ms, i.task, Event::Input(i.command.clone()))?;
    }
    let mut o = Outcome::default();
    let mut generation = 0;
    let mut serving = false;
    let mut closed = true;
    let mut terminal = false;
    let mut expiry = 0;
    let mut inflight = false;
    let mut begun = false;
    let mut count = 0;
    while let Some(item) = q.pop()? {
        count += 1;
        if count > 1000 {
            return Err("event_limit".into());
        }
        let now = q.clock.mono_ms;
        match item.event {
            Event::Input(Command::Observe) => reached(&mut o, "observe", now),
            Event::Input(Command::WallJump { delta_ms }) => {
                q.clock.jump_wall(delta_ms)?;
                reached(&mut o, "wall_jump", now);
            }
            Event::Input(Command::Begin) => {
                if begun {
                    return Err("duplicate_begin".into());
                }
                begun = true;
                q.at(now, 1, Event::Start)?;
            }
            Event::Start => {
                reached(&mut o, "start_attempt", now);
                if !closed || terminal || config.mode == Mode::AlwaysRefuse {
                    reached(&mut o, "refused", now);
                    continue;
                }
                let admission_end = add(
                    add(now, span(B::ObservedStart)?.millis())?,
                    span(B::WorkerCleanupReserve)?.millis(),
                )?;
                if !config.owner_local_allowed
                    || !config.member_granted
                    || admission_end >= config.authority_until_ms
                {
                    reached(&mut o, "authority_refused", now);
                    o.diagnostics.push("authority_refused:ConsentBound".into());
                    continue;
                }
                generation += 1;
                o.starts += 1;
                closed = false;
                let member_wall = if config.incident == Incident::F4 {
                    span(B::WorkerDefaultWall)?
                } else {
                    span(B::MemberChosenWall)?
                };
                let stagger = if config.incident == Incident::F15 {
                    span(B::WorkerStartup)?.millis()
                } else {
                    0
                };
                let member_expiry = add(now, member_wall.millis())?.min(config.authority_until_ms);
                let owner_origin = add(now, stagger)?;
                let owner_expiry = add(owner_origin, span(B::WorkerDefaultWall)?.millis())?
                    .min(config.authority_until_ms);
                expiry = member_expiry.min(owner_expiry);
                let authorized_wall = budgets::Span::from_millis(sub(expiry, now)?);
                let lead = B::EffectiveRolloverLead
                    .wall_span(authorized_wall, Entries::new(2))
                    .map_err(|_| "budget_derivation")?
                    .millis();
                o.workers.push(WorkerLife {
                    generation,
                    member_origin_ms: now,
                    owner_origin_ms: owner_origin,
                    member_expiry_ms: member_expiry,
                    owner_expiry_ms: owner_expiry,
                    cleanup_by_ms: sub(expiry, span(B::WorkerCleanupReserve)?.millis())?,
                    closed_at_ms: None,
                });
                let due = if config.mode == Mode::Historical && config.incident == Incident::F4 {
                    expiry
                } else if config.mode == Mode::Historical && config.incident == Incident::F15 {
                    sub(owner_expiry, lead)?
                } else {
                    sub(expiry, lead)?
                };
                o.deadlines.push((member_expiry, owner_expiry, due));
                q.at(expiry, 0, Event::Expire(generation))?;
                q.after(span(B::ObservedStart)?, 1, Event::Serving(generation))?;
                if o.rollovers < config.rollovers {
                    q.at(due, 2, Event::Stop(generation))?;
                    // A real in-flight request spans the switching boundary, not an
                    // unused generated action. Its allowed outcome is explicit interruption.
                    q.at(
                        sub(due, span(B::OwnerPoll)?.millis())?,
                        3,
                        Event::Chat(generation),
                    )?;
                }
            }
            Event::Serving(g) if g == generation && !terminal && !closed => {
                serving = true;
                o.served += 1;
                reached(&mut o, "serving", now);
                q.at(now, 3, Event::NextChat(g))?;
            }
            Event::Chat(g) if g == generation => {
                reached(&mut o, "crossing_chat", now);
                if serving {
                    inflight = true;
                }
            }
            Event::NextChat(g) if g == generation && serving && !terminal => {
                o.successful_chats += 1;
                reached(&mut o, "chat_success", now);
                // Qualification horizon ends after the post-second-rollover chat,
                // rather than allowing an intentionally unrenewed final worker to expire.
                if o.rollovers == config.rollovers {
                    break;
                }
            }
            Event::Expire(g) if g == generation && !closed => {
                serving = false;
                o.expiry_losses += 1;
                reached(&mut o, "worker_expired", now);
            }
            Event::Stop(g) if g == generation => {
                reached(&mut o, "stop", now);
                serving = false;
                if inflight {
                    o.interrupted_chats += 1;
                    inflight = false;
                    reached(&mut o, "chat_interrupted_explicitly", now);
                }
                let close = span(B::ObservedStop)?
                    .checked_mul(config.close_multiplier)
                    .map_err(|_| "budget_overflow")?;
                q.after(close, 1, Event::Close(g))?;
                if config.mode == Mode::Historical && config.incident == Incident::F4 {
                    q.at(now, 3, Event::Replacement(g))?;
                }
                let outer = if config.mode == Mode::Historical && config.incident == Incident::F16 {
                    add(now, span(B::HistoricalOuterStop)?.millis())?
                } else {
                    sub(expiry, span(B::WorkerCleanupReserve)?.millis())?.max(now)
                };
                q.at(outer, 2, Event::OuterWait(g))?;
            }
            Event::OuterWait(g) if g == generation && !closed => {
                reached(&mut o, "outer_wait_expired", now);
                terminal = true;
                if config.mode == Mode::Historical && config.incident == Incident::F16 {
                    o.swallowed_errors += 1;
                } else {
                    o.diagnostics
                        .push("rollover_cleanup_unproven:StopDeadline".into());
                }
            }
            Event::Close(g) if g == generation => {
                reached(&mut o, "close_completion", now);
                if config.mode == Mode::MissingProof {
                    o.diagnostics
                        .push("target_cleanup_unproven:MissingReceipt".into());
                    continue;
                }
                closed = true;
                o.cleanup_proofs += 1;
                o.workers.last_mut().ok_or("missing_worker")?.closed_at_ms = Some(now);
                reached(&mut o, "cleanup_proven", now);
                if !terminal {
                    q.at(now, 3, Event::Replacement(g))?;
                }
            }
            Event::Replacement(g) if g == generation => {
                reached(&mut o, "replacement_attempt", now);
                if !closed {
                    o.forbidden_replacements += 1;
                    terminal = true;
                    o.diagnostics
                        .push("replacement_forbidden:CleanupUnproven".into());
                } else if !terminal {
                    o.rollovers += 1;
                    reached(&mut o, "fresh_run", now);
                    q.at(now, 1, Event::Start)?;
                }
            }
            _ => {
                reached(&mut o, "stale_completion_ignored", now);
            }
        }
    }
    Ok(o)
}
/// Independent acceptance predicates, not calls back into scheduling/budget code.
pub fn violations(config: &Config, o: &Outcome) -> Vec<String> {
    if config.incident.is_extended() {
        return crate::incidents::violations(config, o);
    }
    let mut v = Vec::new();
    if o.starts == 0 || o.served == 0 || o.successful_chats == 0 {
        v.push("authorized_progress_unreached".into());
    }
    if o.rollovers < config.rollovers {
        v.push("automatic_rollovers_missing".into());
    }
    if o.interrupted_chats < config.rollovers {
        v.push("crossing_chat_unreached".into());
    }
    if o.expiry_losses != 0 {
        v.push("expiry_driven_service_loss".into());
    }
    if o.forbidden_replacements != 0 {
        v.push("forbidden_replacement_attempt".into());
    }
    if o.swallowed_errors != 0 {
        v.push("stop_error_swallowed".into());
    }
    if o.workers
        .iter()
        .any(|w| w.closed_at_ms.is_some_and(|t| t > w.cleanup_by_ms))
    {
        v.push("cleanup_exceeded_original_deadline".into());
    }
    if o.workers.iter().any(|w| {
        w.member_expiry_ms > config.authority_until_ms
            || w.owner_expiry_ms > config.authority_until_ms
    }) {
        v.push("authority_extended".into());
    }
    if o.cleanup_proofs < o.rollovers {
        v.push("cleanup_not_proven".into());
    }
    v
}
