// SPDX-License-Identifier: AGPL-3.0-only
//! Synchronous scheduler: (absolute monotonic tick, explicit task ordinal,
//! insertion ordinal). No OS clocks, threads, sockets or sleeps.
use budgets::{BudgetId, Entries, Span};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
pub type SimResult<T> = Result<T, String>;
pub fn span(id: BudgetId) -> SimResult<Span> {
    id.span(Entries::new(2))
        .map_err(|e| format!("budget:{e:?}"))
}
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Clock {
    pub mono_ms: u64,
    pub wall_ms: i64,
}
impl Clock {
    pub fn advance_to(&mut self, at: u64) -> SimResult<()> {
        let delta = at.checked_sub(self.mono_ms).ok_or("clock_went_backwards")?;
        let wall = self
            .wall_ms
            .checked_add(i64::try_from(delta).map_err(|_| "clock_overflow")?)
            .ok_or("clock_overflow")?;
        self.mono_ms = at;
        self.wall_ms = wall;
        Ok(())
    }
    pub fn jump_wall(&mut self, delta: i64) -> SimResult<()> {
        self.wall_ms = self.wall_ms.checked_add(delta).ok_or("clock_overflow")?;
        Ok(())
    }
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Scheduled<E> {
    pub at_ms: u64,
    pub task: u32,
    pub ordinal: u64,
    pub event: E,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Scheduler<E> {
    pub clock: Clock,
    queue: Vec<Scheduled<E>>,
    next: u64,
}
impl<E> Default for Scheduler<E> {
    fn default() -> Self {
        Self {
            clock: Clock::default(),
            queue: Vec::new(),
            next: 0,
        }
    }
}
impl<E> Scheduler<E> {
    pub fn at(&mut self, at_ms: u64, task: u32, event: E) -> SimResult<()> {
        if at_ms < self.clock.mono_ms {
            return Err("event_in_past".into());
        }
        let next = self.next.checked_add(1).ok_or("ordinal_overflow")?;
        self.queue.push(Scheduled {
            at_ms,
            task,
            ordinal: self.next,
            event,
        });
        self.next = next;
        Ok(())
    }
    pub fn after(&mut self, delay: Span, task: u32, event: E) -> SimResult<()> {
        let at = self
            .clock
            .mono_ms
            .checked_add(delay.millis())
            .ok_or("clock_overflow")?;
        self.at(at, task, event)
    }
    pub fn pop(&mut self) -> SimResult<Option<Scheduled<E>>> {
        let index = self
            .queue
            .iter()
            .enumerate()
            .min_by_key(|(_, e)| (e.at_ms, e.task, e.ordinal))
            .map(|(i, _)| i);
        if let Some(i) = index {
            let e = self.queue.remove(i);
            self.clock.advance_to(e.at_ms)?;
            Ok(Some(e))
        } else {
            Ok(None)
        }
    }
    pub fn pending(&self) -> usize {
        self.queue.len()
    }
}
/// SplitMix64 finalizer over a named counter. No shared mutable RNG: unrelated
/// nodes/streams cannot perturb a task's random choices. Version is trace-pinned.
pub fn random(seed: u64, entity: u64, stream: u64, counter: u64) -> u64 {
    let mut z = seed.wrapping_add(entity.wrapping_mul(0x9e3779b97f4a7c15))
        ^ stream.rotate_left(17)
        ^ counter.rotate_left(31);
    z = (z ^ (z >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94d049bb133111eb);
    z ^ (z >> 31)
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum DeliveryFault {
    None,
    Drop,
    Duplicate,
    Reorder,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Message {
    pub id: u64,
    pub value: u64,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum Io {
    Add {
        node: u32,
    },
    Put {
        node: u32,
        key: u64,
        value: u64,
    },
    WriteComplete {
        node: u32,
        boot: u64,
        key: u64,
        value: u64,
    },
    Send {
        from: u32,
        to: u32,
        message: Message,
        fault: DeliveryFault,
    },
    Deliver {
        from: u32,
        to: u32,
        boot: u64,
        message: Message,
    },
    Partition {
        a: u32,
        b: u32,
        enabled: bool,
    },
    DiskFailure {
        node: u32,
        enabled: bool,
    },
    Crash {
        node: u32,
    },
    Restart {
        node: u32,
    },
    WallJump {
        delta_ms: i64,
    },
}
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Node {
    pub boot: u64,
    pub running: bool,
    pub disk_failed: bool,
    pub durable: BTreeMap<u64, u64>,
    pub memory: BTreeMap<u64, u64>,
    pub inbox: Vec<Message>,
}
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct World {
    pub driver: Scheduler<Io>,
    pub nodes: BTreeMap<u32, Node>,
    pub partitions: BTreeSet<(u32, u32)>,
    pub log: Vec<String>,
}
impl World {
    fn node(&self, id: u32) -> SimResult<&Node> {
        self.nodes.get(&id).ok_or_else(|| "unknown_node".into())
    }
    fn node_mut(&mut self, id: u32) -> SimResult<&mut Node> {
        self.nodes.get_mut(&id).ok_or_else(|| "unknown_node".into())
    }
    pub fn drain(&mut self, max_events: usize) -> SimResult<()> {
        for _ in 0..max_events {
            if !self.step()? {
                return Ok(());
            }
        }
        if self.driver.pending() == 0 {
            Ok(())
        } else {
            Err("event_limit".into())
        }
    }
    pub fn step(&mut self) -> SimResult<bool> {
        let Some(e) = self.driver.pop()? else {
            return Ok(false);
        };
        self.log.push(format!(
            "{}:{}:{}:{:?}",
            e.at_ms, e.task, e.ordinal, e.event
        ));
        match e.event {
            Io::Add { node } => {
                if self.nodes.contains_key(&node) {
                    return Err("duplicate_node".into());
                }
                self.nodes.insert(
                    node,
                    Node {
                        running: true,
                        ..Node::default()
                    },
                );
            }
            Io::Put { node, key, value } => {
                let n = self.node(node)?;
                if !n.running {
                    return Err("node_down".into());
                }
                self.driver.after(
                    span(BudgetId::OwnerPoll)?,
                    node,
                    Io::WriteComplete {
                        node,
                        boot: n.boot,
                        key,
                        value,
                    },
                )?;
            }
            Io::WriteComplete {
                node,
                boot,
                key,
                value,
            } => {
                let n = self.node_mut(node)?;
                if !n.running || n.boot != boot {
                    self.log.push("write:stale_generation".into());
                } else if n.disk_failed {
                    self.log.push("write:disk_failure".into());
                } else {
                    n.durable.insert(key, value);
                    n.memory.insert(key, value);
                    self.log.push("write:committed".into());
                }
            }
            Io::Send {
                from,
                to,
                message,
                fault,
            } => {
                if !self.node(from)?.running {
                    return Err("sender_down".into());
                }
                let boot = self.node(to)?.boot;
                if fault == DeliveryFault::Drop {
                    self.log.push("send:dropped".into());
                } else {
                    let delay = if fault == DeliveryFault::Reorder {
                        span(BudgetId::MemberRestartGrace)?
                    } else {
                        span(BudgetId::OwnerPoll)?
                    };
                    let copies = if fault == DeliveryFault::Duplicate {
                        2
                    } else {
                        1
                    };
                    for _ in 0..copies {
                        self.driver.after(
                            delay,
                            to,
                            Io::Deliver {
                                from,
                                to,
                                boot,
                                message: message.clone(),
                            },
                        )?;
                    }
                }
            }
            Io::Deliver {
                from,
                to,
                boot,
                message,
            } => {
                if self.partitions.contains(&(from.min(to), from.max(to))) {
                    self.log.push("delivery:partition".into());
                } else {
                    let n = self.node_mut(to)?;
                    if n.running && n.boot == boot {
                        n.inbox.push(message);
                    } else {
                        self.log.push("delivery:stale_generation".into());
                    }
                }
            }
            Io::Partition { a, b, enabled } => {
                let pair = (a.min(b), a.max(b));
                if enabled {
                    self.partitions.insert(pair);
                } else {
                    self.partitions.remove(&pair);
                }
            }
            Io::DiskFailure { node, enabled } => self.node_mut(node)?.disk_failed = enabled,
            Io::Crash { node } => {
                let n = self.node_mut(node)?;
                n.boot = n.boot.checked_add(1).ok_or("boot_overflow")?;
                n.running = false;
                n.memory.clear();
                n.inbox.clear();
            }
            Io::Restart { node } => {
                let n = self.node_mut(node)?;
                if n.running {
                    return Err("node_already_running".into());
                }
                n.running = true;
                n.memory = n.durable.clone();
            }
            Io::WallJump { delta_ms } => self.driver.clock.jump_wall(delta_ms)?,
        }
        Ok(true)
    }
}
