// SPDX-License-Identifier: AGPL-3.0-only
use crate::{
    Result,
    http::Chat,
    lifecycle::PowerIo,
    observe::{Observer, SnapshotStore},
    soak::{Evidence, Policy, Restart, Run, Verdict},
};
use budgets::Span;
pub trait SoakIo: Observer + PowerIo {
    fn chat(&mut self) -> Result<Chat>;
    fn restart_owner(&mut self) -> Result<(Restart, Run)>;
    fn visitor(&mut self) -> Result<bool>;
    fn resources(&mut self, host: crate::Host) -> Result<()>;
}
#[derive(Clone, Copy)]
pub struct Limits {
    pub poll: Span,
    pub power_sample: Span,
}
#[derive(serde::Serialize)]
pub struct Receipt {
    pub samples: Vec<serde_json::Value>,
    pub evidence: Evidence,
    pub verdict: Verdict,
    pub battery_stop_percentages: [Option<u8>; 2],
    pub cleanup_errors: Vec<String>,
    pub power_causes: Vec<crate::lifecycle::PowerCause>,
}
pub fn drive_owned<I: SoakIo, S: SnapshotStore>(
    io: &mut I,
    store: &mut S,
    p: Policy,
    limits: Limits,
    attempt: crate::attempt::Attempt,
) -> std::result::Result<Receipt, crate::attempt::FinalizeFailure> {
    let receipt = drive(io, store, p, limits);
    let value = serde_json::to_value(&receipt).map_err(|_| crate::attempt::FinalizeFailure {
        first: crate::Fault::new(
            "soak_receipt_encoding_failed",
            crate::FaultKind::Observation,
        ),
        cleanup: None,
    })?;
    attempt.finish(&value)?;
    Ok(receipt)
}
/// Registry admission for an operational driver; offline `drive` remains an
/// explicitly supplied-policy replay. No registry fallback is permitted.
pub fn drive_registered(
    registry: &impl crate::harness_budgets::Registry,
    io: &mut impl SoakIo,
    store: &mut impl SnapshotStore,
    policy: Policy,
    limits: Limits,
) -> Result<Receipt> {
    let mut policy = policy;
    crate::harness_budgets::bind_soak(registry, &mut policy)?;
    Ok(drive(io, store, policy, limits))
}
pub fn drive<I: SoakIo, S: SnapshotStore>(
    io: &mut I,
    store: &mut S,
    p: Policy,
    limits: Limits,
) -> Receipt {
    use crate::{
        Fault, FaultKind, Host,
        lifecycle::PowerWatch,
        observe::failure_snapshot,
        schedule::{Completion, Job, Machine},
        soak::{HostMetricsSample, MailboxSample},
    };
    let mut samples = vec![];
    let mut evidence = Evidence::default();
    let mut cleanup_errors = vec![];
    let mut power_causes = vec![];
    let mut percentages = [None; 2];
    let mut first = None;
    let mut admitted = false;
    let execute = (|| -> Result<()> {
        p.validate()?;
        if limits.poll == Span::ZERO {
            return Err(Fault::new("invalid_soak_poll", FaultKind::Input));
        }
        let mut watch = PowerWatch::new(limits.power_sample)?;
        let initial = watch.check(io);
        percentages = watch.percentages;
        power_causes = watch.cause_ledger.clone();
        initial?;
        let owner = io.status(Host::Owner);
        let member = io.status(Host::Member);
        let (http, owner) = owner?;
        let (member_http, _) = member?;
        if http != 200 || member_http != 200 {
            return Err(Fault::new("initial_status_refused", FaultKind::Observation));
        }
        let mut last_run = Run::from_status(&owner);
        let initial_chat = io.chat()?;
        let start = io.now();
        let end = start
            .checked_add(p.requested_ms)
            .ok_or(Fault::new("soak_clock_overflow", FaultKind::Input))?;
        let mut machine = Machine::new(p.clone(), &initial_chat, &last_run)?;
        admitted = true;
        while io.now() < end {
            let guard = watch.check(io);
            percentages = watch.percentages;
            power_causes = watch.cause_ledger.clone();
            if let Err(e) = guard {
                cleanup_errors.extend(watch.causes.iter().map(|s| s.to_string()));
                if e.incomplete() {
                    machine.evidence.incomplete_reason = Some(e.code.into());
                } else {
                    machine.evidence.product_failure = Some(e.code.into());
                }
                first = Some(e);
                break;
            }
            let at = io
                .now()
                .checked_sub(start)
                .ok_or(Fault::new("clock_went_backwards", FaultKind::Observation))?;
            for task in machine.poll(at)? {
                let done = (|| -> Result<Completion> {
                    match task.job {
                        Job::Sample => {
                            // Observe both hosts and both metric streams even when one fails.
                            let owner = io.status(Host::Owner);
                            let member = io.status(Host::Member);
                            let om = io.metrics(Host::Owner);
                            let mm = io.metrics(Host::Member);
                            let (http, value) = owner?;
                            let (member_http, _) = member?;
                            let owner_metrics = om?;
                            let member_metrics = mm?;
                            if http != 200 {
                                return Err(Fault::new(
                                    "owner_status_refused",
                                    FaultKind::Observation,
                                ));
                            }
                            samples.push(serde_json::json!({"at_ms":at,"hosts":{"owner":owner_metrics,"laptop":member_metrics},"owner_run":crate::observe::redact(&value["owner_run"],&[])}));
                            last_run = Run::from_status(&value);
                            Ok(Completion::Sample {
                                run: last_run.clone(),
                                member_http,
                                metrics: Some(HostMetricsSample {
                                    at_ms: at,
                                    owner: owner_metrics,
                                    member: member_metrics,
                                }),
                                published: value["network"]["state"] == "public",
                            })
                        }
                        Job::Mailbox => {
                            let owner = io.mailbox(Host::Owner);
                            let member = io.mailbox(Host::Member);
                            let owner_resource = io.resources(Host::Owner);
                            let member_resource = io.resources(Host::Member);
                            let owner = owner?;
                            let member = member?;
                            owner_resource?;
                            member_resource?;
                            Ok(Completion::Mailbox(MailboxSample {
                                at_ms: at,
                                owner: owner.database_bytes,
                                member: member.database_bytes,
                                owner_storage: owner.storage_bytes,
                                member_storage: member.storage_bytes,
                            }))
                        }
                        Job::Chat => Ok(Completion::Chat(io.chat()?)),
                        Job::Restart => {
                            let (receipt, run) = io.restart_owner()?;
                            last_run = run.clone();
                            Ok(Completion::Restart { receipt, run })
                        }
                        Job::Visitor => Ok(Completion::Visitor(io.visitor()?)),
                    }
                })();
                if let Err(e) = done {
                    first.get_or_insert(e);
                }
                machine.complete(
                    task,
                    io.now()
                        .checked_sub(start)
                        .ok_or(Fault::new("clock_went_backwards", FaultKind::Observation))?,
                    done,
                )?;
                if first.is_some() {
                    break;
                }
            }
            if first.is_some() {
                break;
            }
            let now = io.now();
            if now >= end {
                break;
            }
            io.sleep(limits.poll.min(Span::from_millis(end - now)))?;
            if io.now() <= now {
                return Err(Fault::new("clock_not_advancing", FaultKind::Observation));
            }
        }
        let at = io
            .now()
            .checked_sub(start)
            .ok_or(Fault::new("clock_went_backwards", FaultKind::Observation))?;
        let final_owner = io.status(Host::Owner);
        let owner = io.mailbox(Host::Owner);
        let member = io.mailbox(Host::Member);
        match final_owner {
            Ok((200, v)) => last_run = Run::from_status(&v),
            Ok(_) => {
                first.get_or_insert(Fault::new("owner_status_refused", FaultKind::Observation));
            }
            Err(e) => {
                first.get_or_insert(e);
            }
        }
        let fallback = MailboxSample {
            at_ms: at,
            owner: None,
            member: None,
            owner_storage: 0,
            member_storage: 0,
        };
        let mailbox = match (owner, member) {
            (Ok(o), Ok(m)) => MailboxSample {
                at_ms: at,
                owner: o.database_bytes,
                member: m.database_bytes,
                owner_storage: o.storage_bytes,
                member_storage: m.storage_bytes,
            },
            (Err(e), _) | (_, Err(e)) => {
                first.get_or_insert(e);
                fallback
            }
        };
        if let Some(e) = first {
            if e.incomplete() {
                machine
                    .evidence
                    .incomplete_reason
                    .get_or_insert(e.code.into());
            } else {
                machine
                    .evidence
                    .product_failure
                    .get_or_insert(e.code.into());
            }
        }
        let _verdict = machine.finish(at, &last_run, mailbox);
        evidence = machine.evidence;
        Ok(())
    })();
    if let Err(e) = execute {
        first.get_or_insert(e);
        if e.incomplete() {
            evidence.incomplete_reason.get_or_insert(e.code.into());
        } else {
            evidence.product_failure.get_or_insert(e.code.into());
        }
    }
    let verdict = crate::soak::evaluate(&p, &evidence);
    if !verdict.scenario_passed {
        let cause = first.unwrap_or(Fault::new("soak_verdict_failed", FaultKind::Product));
        if let Err(e) = failure_snapshot(io, store, cause, &[]) {
            cleanup_errors.push(e.code.into());
        }
        if admitted
            && cause.code != "battery_floor"
            && let Err(e) = io.stop_demo()
        {
            cleanup_errors.push(e.code.into());
        }
    }
    Receipt {
        samples,
        evidence,
        verdict,
        battery_stop_percentages: percentages,
        cleanup_errors,
        power_causes,
    }
}
