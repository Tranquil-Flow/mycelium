// SPDX-License-Identifier: AGPL-3.0-only
//! Cooperative compiler adapter. An OS supervisor must enforce the same absolute
//! deadline for a native child; a synchronous callback is not preemptive isolation.
use crate::{admission::CompileWindow, control_adapter::Prepared};
use serde_json::Value;
#[derive(Debug)]
pub enum CompileFailure {
    Rejected(crate::Error),
    Failed {
        cause: crate::Error,
        stop: node::Result<Vec<node::Effect>>,
    },
}
impl Prepared {
    /// Compile once against reserved shares. Failure requests L6 stop but never
    /// releases capacity: the supervisor must deliver independent cleanup proof.
    pub fn compile(
        &mut self,
        core: &mut node::Core,
        clock: &mut impl FnMut() -> node::Now,
        compiler: impl FnOnce(CompileWindow) -> crate::Result<Value>,
    ) -> Result<Value, CompileFailure> {
        let rejected = |code| CompileFailure::Rejected(crate::Error::new(code));
        if self.attempted {
            return Err(rejected("compile_already_attempted"));
        }
        if core.state().owner != self.owner {
            return Err(rejected("compile_reservation_stale"));
        }
        for id in &self.identities {
            if id.node_id == self.owner.node_id {
                continue;
            }
            if core.state().members.get(&id.node_id).is_none_or(|m| {
                m.membership_generation != id.generation.to_string()
                    || m.incarnation != id.incarnation
                    || m.endpoint != id.endpoint_id
                    || m.key_digest != id.verification_key_digest
            }) {
                return Err(rejected("compile_reservation_stale"));
            }
        }
        if core.state().phase != node::RunPhase::Starting
            || core.state().workers.len() != self.starts.len()
        {
            return Err(rejected("compile_reservation_stale"));
        }
        for effect in &self.starts {
            let node::Effect::Start { ticket, caps } = effect else {
                return Err(rejected("compile_reservation_stale"));
            };
            if core.state().workers.get(&ticket.node).is_none_or(|w| {
                w.ticket != *ticket || w.caps != *caps || w.phase != node::WorkerPhase::Starting
            }) {
                return Err(rejected("compile_reservation_stale"));
            }
        }
        let window = self
            .window
            .ok_or_else(|| rejected("compile_not_admitted"))?;
        self.attempted = true;
        let start = clock();
        if let Err(cause) = window.remaining(start.mono) {
            return Err(CompileFailure::Failed {
                cause,
                stop: core.cancel(start),
            });
        }
        let output = compiler(window);
        let end = clock();
        let output = output.and_then(|v| {
            end.mono
                .elapsed_since(start.mono)
                .map_err(|_| crate::Error::new("compile_clock_invalid"))?;
            window.remaining(end.mono)?;
            Ok(v)
        });
        match output {
            Ok(v) => Ok(v),
            Err(cause) => Err(CompileFailure::Failed {
                cause,
                stop: core.cancel(end),
            }),
        }
    }
}
