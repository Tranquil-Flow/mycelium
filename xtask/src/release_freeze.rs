// SPDX-License-Identifier: AGPL-3.0-only
//! `xtask release freeze`: the producer of a candidate manifest (CL-L12-01, CL-DOD-08).
//!
//! It freezes exactly what the release machine later re-hashes: the source commit (the checkout
//! must be AT that commit with a clean tree, untracked files included), the executable's sha256 and
//! the bundle digest (`release::bundle_digest`: every regular file and every link target inside the
//! bundle, never followed; links out of it and specials refused). The
//! manifest is created exclusively (an existing file is never overwritten) with absolute paths, so
//! a later `release candidate|status|emit|produce` binds its evidence to these bytes. Nothing is
//! built, signed, launched or published here.
use crate::release::{self, fail, io_text, usage};
use serde_json::json;
use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    process::Command,
    time::{SystemTime, UNIX_EPOCH},
};

fn git(repo: &Path, args: &[&str]) -> Result<String, String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(args)
        .output()
        .map_err(|e| fail("freeze_git_unavailable", io_text(&e)))?;
    if !output.status.success() {
        return Err(fail(
            "freeze_git_failed",
            format!(
                "git {}: {}",
                args.join(" "),
                String::from_utf8_lossy(&output.stderr).trim()
            ),
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

/// Freeze `exe` + `bundle` built from `repo` at `sha` into a new manifest file at `out`.
/// Returns the manifest file's sha256.
pub fn freeze(
    repo: &Path,
    sha: &str,
    exe: &Path,
    bundle: &Path,
    out: &Path,
) -> Result<String, String> {
    let head = git(repo, &["rev-parse", "HEAD"])?;
    if head.trim() != sha {
        return Err(fail(
            "freeze_source_mismatch",
            format!("{} is at {}, not {sha}", repo.display(), head.trim()),
        ));
    }
    let dirty = git(repo, &["status", "--porcelain=v1", "--untracked-files=all"])?;
    if !dirty.trim().is_empty() {
        return Err(fail(
            "freeze_tree_dirty",
            format!(
                "{} has uncommitted or untracked files: {}",
                repo.display(),
                dirty.lines().next().unwrap_or_default().trim()
            ),
        ));
    }
    let absolute = |p: &Path, what: &str| -> Result<PathBuf, String> {
        fs::canonicalize(p).map_err(|e| {
            fail(
                "freeze_path_invalid",
                format!("{what} {}: {}", p.display(), io_text(&e)),
            )
        })
    };
    let (exe, bundle) = (absolute(exe, "exe")?, absolute(bundle, "bundle")?);
    // The candidate's executable is the bundle's own (lane RELF item 7, review F5).
    if exe != bundle.join("Contents/MacOS/mycelium") {
        return Err(fail(
            "freeze_exe_outside_bundle",
            format!(
                "exe {} is not {}/Contents/MacOS/mycelium",
                exe.display(),
                bundle.display()
            ),
        ));
    }
    if !fs::symlink_metadata(&exe).is_ok_and(|m| m.is_file()) {
        return Err(fail(
            "freeze_path_invalid",
            format!("exe {} is not a regular file", exe.display()),
        ));
    }
    let exe_sha256 = release::file_sha256(&exe)
        .map_err(|e| fail("freeze_exe_unreadable", format!("{}: {e}", exe.display())))?;
    let bundle_sha256 = release::bundle_digest(&bundle)?;
    let frozen_at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|e| fail("clock_invalid", e))?
        .as_secs_f64()
        .floor();
    let text = serde_json::to_string_pretty(&json!({
        "schema": 1,
        "candidate": {"source_commit": sha, "exe_sha256": exe_sha256, "bundle_sha256": bundle_sha256},
        "frozen_at": frozen_at,
        "exe_path": exe.display().to_string(),
        "bundle_dir": bundle.display().to_string(),
    }))
    .map_err(|e| fail("freeze_encode", e))?
        + "\n";
    // The machine's own parser must accept what was frozen, and the bytes must still match.
    let manifest = release::Manifest::parse(&text, Path::new("/"))?;
    manifest.verify_bytes()?;
    let io = |e: std::io::Error| {
        fail(
            "freeze_out_unwritable",
            format!("{}: {}", out.display(), io_text(&e)),
        )
    };
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(out)
        .map_err(io)?;
    file.write_all(text.as_bytes()).map_err(io)?;
    file.sync_all().map_err(io)?;
    Ok(manifest.sha256)
}

pub const FREEZE_USAGE: &str =
    "xtask release freeze <sha> --exe FILE --bundle DIR --out MANIFEST [--repo DIR]";

/// `xtask release freeze <sha> --exe FILE --bundle DIR --out MANIFEST [--repo DIR]`
pub fn cli(args: &[String]) -> Result<u8, String> {
    let Some((sha, rest)) = args.split_first() else {
        return Err(usage(FREEZE_USAGE));
    };
    if sha.len() != 40
        || !sha
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(usage("the candidate sha is 40 lower-case hex"));
    }
    let mut values = std::collections::BTreeMap::new();
    let mut it = rest.iter();
    while let Some(key) = it.next() {
        if !["--exe", "--bundle", "--out", "--repo"].contains(&key.as_str()) {
            return Err(usage(&format!("unknown option {key}; {FREEZE_USAGE}")));
        }
        let value = it
            .next()
            .ok_or_else(|| usage(&format!("{key} needs a value")))?;
        if values.insert(key.as_str(), PathBuf::from(value)).is_some() {
            return Err(usage(&format!("repeated option {key}")));
        }
    }
    let need = |key: &str| {
        values
            .get(key)
            .cloned()
            .ok_or_else(|| usage(&format!("{key} is required; {FREEZE_USAGE}")))
    };
    let repo = match values.get("--repo") {
        Some(repo) => repo.clone(),
        None => PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .ok_or_else(|| fail("freeze_repo_unknown", "no workspace parent"))?
            .to_path_buf(),
    };
    let out = need("--out")?;
    let digest = freeze(&repo, sha, &need("--exe")?, &need("--bundle")?, &out)?;
    println!("frozen {} manifest_sha256 {digest}", out.display());
    Ok(0)
}
