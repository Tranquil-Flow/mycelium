// SPDX-License-Identifier: AGPL-3.0-only
//! Non-destructive state/cache operations. The capability deliberately has no delete.
use crate::{
    Fault, FaultKind, Result,
    effects::{Command, CommandKind},
    stranger::CacheRole,
};
use budgets::Span;
use serde_json::Value;
use std::path::{Path, PathBuf};
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Kind {
    Missing,
    Directory,
    File,
    Symlink,
}
pub trait StateIo {
    fn app_running(&mut self) -> Result<bool>;
    fn kind(&mut self, path: &Path) -> Result<Kind>;
    fn children(&mut self, path: &Path) -> Result<Vec<String>>;
    fn cache_status(&mut self, path: &Path) -> Result<Option<Value>>;
    fn rename(&mut self, from: &Path, to: &Path) -> Result<()>;
    fn mkdir_private(&mut self, path: &Path) -> Result<()>;
    fn clone_tree(&mut self, from: &Path, to: &Path, bound: Span) -> Result<()>;
}
#[derive(Debug, Eq, PartialEq)]
pub struct SeedReceipt {
    pub cloned: Vec<String>,
    pub displaced: Vec<PathBuf>,
}
fn require(ok: bool, code: &'static str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(Fault::new(code, FaultKind::Admission))
    }
}
fn path_valid(p: &Path) -> bool {
    p.is_absolute()
        && p.components().all(|c| {
            matches!(
                c,
                std::path::Component::RootDir | std::path::Component::Normal(_)
            )
        })
        && p.to_str().is_some_and(|s| !s.contains('\0'))
}
fn sibling(state: &Path, p: &Path, tag: &str) -> bool {
    path_valid(state)
        && path_valid(p)
        && state.parent() == p.parent()
        && state
            .file_name()
            .and_then(|s| s.to_str())
            .zip(p.file_name().and_then(|s| s.to_str()))
            .is_some_and(|(s, p)| {
                let prefix = format!("{s}{tag}");
                p.strip_prefix(&prefix)
                    .is_some_and(|suffix| !suffix.is_empty())
            })
}
fn stopped<I: StateIo>(io: &mut I) -> Result<()> {
    require(!io.app_running()?, "app_still_running")
}
fn complete<I: StateIo>(io: &mut I, p: &Path) -> Result<bool> {
    Ok(io
        .cache_status(p)?
        .as_ref()
        .is_some_and(crate::preflight::cache_complete))
}
pub fn fresh<I: StateIo>(io: &mut I, state: &Path, aside: &Path) -> Result<Option<PathBuf>> {
    stopped(io)?;
    require(sibling(state, aside, ".pre-v02-"), "set_aside_path_invalid")?;
    require(io.kind(aside)? == Kind::Missing, "set_aside_exists")?;
    match io.kind(state)? {
        Kind::Missing => return Ok(None),
        Kind::Directory => {}
        _ => return Err(Fault::new("state_dirs_invalid", FaultKind::Admission)),
    }
    stopped(io)?;
    io.rename(state, aside)?;
    require(
        io.kind(state)? == Kind::Missing && io.kind(aside)? == Kind::Directory,
        "fresh_state_move_unproven",
    )?;
    Ok(Some(aside.into()))
}
pub fn seed<I: StateIo>(
    io: &mut I,
    state: &Path,
    source: &Path,
    role: CacheRole,
    displaced: &Path,
    clone_bound: Span,
) -> Result<SeedReceipt> {
    stopped(io)?;
    require(clone_bound != Span::ZERO, "invalid_clone_budget")?;
    require(
        sibling(state, source, ".pre-v02-") || sibling(state, source, ".pre-v014-"),
        "source_not_a_set_aside_state",
    )?;
    require(
        sibling(state, displaced, ".seed-displaced-"),
        "displaced_path_invalid",
    )?;
    require(
        io.kind(state)? == Kind::Directory && io.kind(source)? == Kind::Directory,
        "state_dirs_invalid",
    )?;
    let rel = if role == CacheRole::Target {
        Path::new("target/serving")
    } else {
        Path::new("")
    };
    let root = state.join(rel);
    let sources = source.join(rel);
    require(
        io.kind(&root)? == Kind::Directory,
        if role == CacheRole::Target {
            "destination_root_missing_target"
        } else {
            "destination_root_missing_owner"
        },
    )?;
    let mut receipt = SeedReceipt {
        cloned: vec![],
        displaced: vec![],
    };
    match io.kind(&sources)? {
        Kind::Missing => return Ok(receipt),
        Kind::Directory => {}
        _ => return Err(Fault::new("source_root_invalid", FaultKind::Admission)),
    }
    let mut names = io.children(&sources)?;
    names.sort();
    names.dedup();
    for name in names {
        if !name.starts_with("model-source") {
            continue;
        }
        require(
            Path::new(&name).components().count() == 1 && !name.contains(['/', '\\', '\0']),
            "cache_name_invalid",
        )?;
        let src = sources.join(&name);
        let dst = root.join(&name);
        if io.kind(&src)? != Kind::Directory || !complete(io, &src)? {
            continue;
        }
        let kind = io.kind(&dst)?;
        require(kind != Kind::Symlink, "cache_destination_symlink")?;
        if kind != Kind::Missing {
            if complete(io, &dst)? {
                continue;
            }
            let aside = displaced.join(rel).join(&name);
            require(io.kind(&aside)? == Kind::Missing, "cache_displaced_exists")?;
            require(
                matches!(io.kind(displaced)?, Kind::Directory | Kind::Missing),
                "cache_displaced_root_invalid",
            )?;
            stopped(io)?;
            io.mkdir_private(
                aside
                    .parent()
                    .ok_or(Fault::new("cache_path_invalid", FaultKind::Input))?,
            )?;
            io.rename(&dst, &aside)?;
            receipt.displaced.push(aside);
        }
        stopped(io)?;
        io.clone_tree(&src, &dst, clone_bound)?;
        require(complete(io, &dst)?, "clone_incomplete")?;
        require(
            io.kind(&src)? == Kind::Directory && complete(io, &src)?,
            "clone_source_lost",
        )?;
        receipt.cloned.push(name);
    }
    Ok(receipt)
}
pub fn clone_command(from: &Path, to: &Path) -> Result<Command> {
    require(path_valid(from) && path_valid(to), "clone_path_invalid")?;
    Ok(Command {
        kind: CommandKind::Local,
        program: "/bin/cp".into(),
        args: vec![
            "-cRp".into(),
            from.to_str()
                .ok_or(Fault::new("clone_path_invalid", FaultKind::Input))?
                .into(),
            to.to_str()
                .ok_or(Fault::new("clone_path_invalid", FaultKind::Input))?
                .into(),
        ],
    })
}
