// SPDX-License-Identifier: AGPL-3.0-only
//! `xtask release freeze`: the producer of a candidate manifest (CL-L12-01, CL-DOD-08).
//!
//! It freezes exactly what the release machine later re-hashes: the source commit (the checkout
//! must be AT that commit with a clean tree, untracked files included), the executable's sha256 and
//! the bundle digest (`release::bundle_digest`: every regular file, symlinks/specials refused). The
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
pub fn freeze(repo: &Path, sha: &str, exe: &Path, bundle: &Path, out: &Path) -> Result<String, String> {
    let _unused = (repo, sha, exe, bundle, out, git as fn(&Path, &[&str]) -> _, json!({}), OpenOptions::new(), SystemTime::now(), UNIX_EPOCH);
    let _also = (fs::canonicalize::<&Path>, release::file_sha256 as fn(&Path) -> _);
    Err(fail("freeze_not_implemented", "RED"))
}

pub const FREEZE_USAGE: &str =
    "xtask release freeze <sha> --exe FILE --bundle DIR --out MANIFEST [--repo DIR]";

/// `xtask release freeze <sha> --exe FILE --bundle DIR --out MANIFEST [--repo DIR]`
pub fn cli(args: &[String]) -> Result<u8, String> {
    let Some((sha, rest)) = args.split_first() else {
        return Err(usage(FREEZE_USAGE));
    };
    if sha.len() != 40 || !sha.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
        return Err(usage("the candidate sha is 40 lower-case hex"));
    }
    let mut values = std::collections::BTreeMap::new();
    let mut it = rest.iter();
    while let Some(key) = it.next() {
        if !["--exe", "--bundle", "--out", "--repo"].contains(&key.as_str()) {
            return Err(usage(&format!("unknown option {key}; {FREEZE_USAGE}")));
        }
        let value = it.next().ok_or_else(|| usage(&format!("{key} needs a value")))?;
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
