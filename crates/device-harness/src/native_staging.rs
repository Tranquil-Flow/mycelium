// SPDX-License-Identifier: AGPL-3.0-only
//! Raw, pinned native-input staging, before relocation/finalization. Never a
//! packaged-executable or minimum-OS admission; no native effect implementation.
use crate::{
    Fault, FaultKind, Host, Result,
    effects::Clock,
    evidence_inputs::{Content, Inputs, Reference},
};
use budgets::Span;
use serde::Serialize;
pub trait Io {
    fn source_identity(&mut self, source: &Reference, bound: Span) -> Result<Content>;
    /// Exclusive on each host. A partial/failed staging root stays spent.
    fn create_root(&mut self, host: Host, root: &str, bound: Span) -> Result<()>;
    fn copy(
        &mut self,
        source: &Reference,
        host: Host,
        root: &str,
        relative: &str,
        bound: Span,
    ) -> Result<()>;
    fn readback(&mut self, host: Host, root: &str, relative: &str, bound: Span) -> Result<Content>;
}
#[derive(Debug, Serialize)]
pub struct StagedResource {
    pub host: Host,
    pub path: String,
    pub content: Content,
}
#[derive(Debug, Serialize)]
pub struct Receipt {
    pub native_inputs_sha256: String,
    pub oracle_manifest_sha256: String,
    pub resources: Vec<StagedResource>,
    pub release_qualified: bool,
}
fn fault(code: &'static str) -> Fault {
    Fault::new(code, FaultKind::Observation)
}
pub fn stage<I: Io, C: Clock>(
    io: &mut I,
    clock: &C,
    inputs: &Inputs,
    root: &str,
    total: Span,
) -> Result<Receipt> {
    if !root.starts_with('/')
        || root.contains(['\0', '\n', '\r', '\\'])
        || root[1..]
            .split('/')
            .any(|p| p.is_empty() || p == "." || p == "..")
    {
        return Err(fault("native_stage_root_invalid"));
    }
    let end = clock
        .now()
        .checked_add(total.millis())
        .ok_or(fault("native_stage_timeout"))?;
    let remaining = || {
        end.checked_sub(clock.now())
            .filter(|n| *n > 0)
            .map(Span::from_millis)
            .ok_or(fault("native_stage_timeout"))
    };
    for resource in inputs.resources() {
        let observed = io.source_identity(&resource.source, remaining()?)?;
        remaining()?;
        if observed != resource.source.content {
            return Err(fault("native_stage_source_mismatch"));
        }
    }
    for host in [Host::Owner, Host::Member] {
        io.create_root(host, root, remaining()?)?;
        remaining()?;
    }
    let mut resources = Vec::new();
    for host in [Host::Owner, Host::Member] {
        for resource in inputs.resources() {
            io.copy(
                &resource.source,
                host,
                root,
                resource.destination,
                remaining()?,
            )?;
            let content = io.readback(host, root, resource.destination, remaining()?)?;
            remaining()?;
            if content != resource.source.content {
                return Err(fault("native_stage_readback_mismatch"));
            }
            resources.push(StagedResource {
                host,
                path: resource.destination.into(),
                content,
            });
        }
    }
    Ok(Receipt {
        native_inputs_sha256: inputs.native_identity().sha256.clone(),
        oracle_manifest_sha256: inputs.oracle_identity().sha256.clone(),
        resources,
        release_qualified: false,
    })
}
