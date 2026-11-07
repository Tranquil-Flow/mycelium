// SPDX-License-Identifier: AGPL-3.0-only
use crate::{Fault, FaultKind, Result, http::Chat, soak::*};
use std::collections::BTreeMap;
#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub enum Job {
    Sample,
    Mailbox,
    Chat,
    Visitor,
    Restart,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Task {
    pub job: Job,
    pub sequence: u64,
    pub issued_ms: u64,
}
pub enum Completion {
    Sample {
        run: Run,
        member_http: u16,
        metrics: Option<crate::soak::HostMetricsSample>,
        published: bool,
    },
    Mailbox(MailboxSample),
    Chat(Chat),
    Visitor(bool),
    Restart {
        receipt: Restart,
        run: Run,
    },
}
pub struct Machine {
    policy: Policy,
    pub evidence: Evidence,
    tracker: RolloverTracker,
    pending: BTreeMap<Job, Task>,
    next: BTreeMap<Job, u64>,
    sequence: u64,
    last_ms: u64,
    restart_started: bool,
    stopped: bool,
}
impl Machine {
    pub fn new(policy: Policy, first_chat: &Chat, run: &Run) -> Result<Self> {
        policy.validate()?;
        if !first_chat.ok() || !run.serving() {
            return Err(Fault::new(
                "soak_start_not_distributed_output",
                FaultKind::Admission,
            ));
        }
        let mut tracker = RolloverTracker::default();
        tracker.observe(0, run)?;
        Ok(Self {
            policy,
            evidence: Evidence {
                clock_started_at_distributed_output: true,
                ..Evidence::default()
            },
            tracker,
            pending: BTreeMap::new(),
            next: [
                (Job::Sample, 0),
                (Job::Mailbox, 0),
                (Job::Chat, 0),
                (Job::Visitor, 0),
            ]
            .into(),
            sequence: 0,
            last_ms: 0,
            restart_started: false,
            stopped: false,
        })
    }
    fn advance(&mut self, at: u64) -> Result<()> {
        if at < self.last_ms {
            return Err(Fault::new("clock_went_backwards", FaultKind::Observation));
        }
        self.last_ms = at;
        Ok(())
    }
    fn issue(&mut self, job: Job, at: u64) -> Result<Task> {
        self.sequence = self
            .sequence
            .checked_add(1)
            .ok_or(Fault::new("task_sequence_overflow", FaultKind::Observation))?;
        let task = Task {
            job,
            sequence: self.sequence,
            issued_ms: at,
        };
        self.pending.insert(job, task);
        Ok(task)
    }
    pub fn poll(&mut self, at: u64) -> Result<Vec<Task>> {
        self.advance(at)?;
        if self.stopped || at >= self.policy.requested_ms {
            return Ok(vec![]);
        }
        let mut tasks = vec![];
        for (job, interval, reason) in [
            (
                Job::Sample,
                self.policy.sample_every_ms,
                "status_schedule_overrun",
            ),
            (
                Job::Mailbox,
                self.policy.mailbox_every_ms,
                "mailbox_schedule_overrun",
            ),
            (
                Job::Chat,
                self.policy.chat_every_ms,
                "chat_schedule_overrun",
            ),
            (
                Job::Visitor,
                self.policy.visitor_every_ms,
                "visitor_schedule_overrun",
            ),
        ] {
            if job == Job::Visitor && !self.evidence.published {
                continue;
            }
            let next = *self
                .next
                .get(&job)
                .ok_or(Fault::new("schedule_state_invalid", FaultKind::Input))?;
            if at >= next {
                if at - next >= interval || self.pending.contains_key(&job) {
                    self.evidence.errors.push(reason.into());
                }
                let following = at
                    .checked_div(interval)
                    .and_then(|s| s.checked_add(1))
                    .and_then(|s| s.checked_mul(interval))
                    .ok_or(Fault::new("schedule_overflow", FaultKind::Input))?;
                self.next.insert(job, following);
                if !self.pending.contains_key(&job) {
                    tasks.push(self.issue(job, at)?);
                }
            }
        }
        if !self.restart_started
            && self.policy.restart_at_ms.is_some_and(|when| at >= when)
            && !self.pending.contains_key(&Job::Chat)
            && !self.pending.contains_key(&Job::Visitor)
        {
            self.restart_started = true;
            self.tracker.begin_restart();
            self.evidence.restart_window = Some(RestartWindow {
                begin_ms: Some(at),
                end_ms: None,
            });
            tasks.push(self.issue(Job::Restart, at)?);
        }
        Ok(tasks)
    }
    pub fn complete(&mut self, task: Task, at: u64, result: Result<Completion>) -> Result<()> {
        self.advance(at)?;
        if self.pending.get(&task.job) != Some(&task) || at < task.issued_ms {
            return Err(Fault::new("stale_task_completion", FaultKind::Input));
        }
        // Validate shape BEFORE consuming a pending slot.
        if let Ok(ref c) = result {
            let job = match c {
                Completion::Sample { .. } => Job::Sample,
                Completion::Mailbox(_) => Job::Mailbox,
                Completion::Chat(_) => Job::Chat,
                Completion::Visitor(_) => Job::Visitor,
                Completion::Restart { .. } => Job::Restart,
            };
            if job != task.job {
                return Err(Fault::new("completion_kind_mismatch", FaultKind::Input));
            }
        }
        self.pending.remove(&task.job);
        match result {
            Err(f) => self.fail(f),
            Ok(Completion::Sample {
                run,
                member_http,
                metrics,
                published,
            }) => {
                self.evidence.samples += 1;
                self.evidence.published |= published;
                if let Some(mut sample) = metrics.filter(|m| m.owner.valid() && m.member.valid()) {
                    sample.at_ms = at;
                    self.evidence.host_metrics.push(sample);
                    self.evidence.metrics_samples += 1;
                } else {
                    self.fail(Fault::new(
                        "host_metrics_unobserved",
                        FaultKind::Observation,
                    ));
                }
                if member_http != 200 {
                    self.fail(Fault::new(
                        "member_status_unobserved",
                        FaultKind::Observation,
                    ));
                }
                if let Err(e) = self.tracker.observe(at, &run) {
                    self.fail(e);
                }
                if run.state == "failed" {
                    self.fail(Fault::new("route_failed", FaultKind::Product));
                }
            }
            Ok(Completion::Mailbox(mut sample)) => {
                sample.at_ms = at;
                self.evidence.mailbox_samples.push(sample);
            }
            Ok(Completion::Chat(mut c)) => {
                c.start_ms = task.issued_ms;
                c.end_ms = at;
                self.evidence.chats.push(c);
            }
            Ok(Completion::Visitor(completed)) => self.evidence.visitor_completed.push(completed),
            Ok(Completion::Restart { receipt, run }) => {
                if receipt.expectation != self.policy.restart_expectation || !receipt.satisfied() {
                    self.evidence.errors.push("owner_restart_failed".into());
                }
                self.tracker.end_restart(at, &run);
                self.evidence.restart_window = Some(RestartWindow {
                    begin_ms: Some(task.issued_ms),
                    end_ms: Some(at),
                });
                self.evidence.restart = Some(receipt);
            }
        }
        Ok(())
    }
    fn fail(&mut self, f: Fault) {
        self.stopped = true;
        if f.kind == FaultKind::Product {
            self.evidence.product_failure.get_or_insert(f.code.into());
        } else {
            self.evidence.incomplete_reason.get_or_insert(f.code.into());
        }
    }
    pub fn finish(&mut self, at: u64, run: &Run, mut mailbox: MailboxSample) -> Verdict {
        if let Err(e) = self.advance(at) {
            self.fail(e);
        }
        self.stopped = true;
        if !self.pending.is_empty() {
            self.evidence
                .errors
                .push("inflight_observation_missing".into());
        }
        if let Err(e) = self.tracker.observe(at, run) {
            self.fail(e);
        }
        self.evidence.duration_ms = at;
        self.evidence.final_serving = run.serving();
        self.evidence.rollovers = self.tracker.rollovers.clone();
        self.evidence.pending_rollover = self.tracker.pending.is_some();
        mailbox.at_ms = at;
        self.evidence.mailbox_samples.push(mailbox);
        evaluate(&self.policy, &self.evidence)
    }
}
