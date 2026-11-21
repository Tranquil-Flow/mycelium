// SPDX-License-Identifier: AGPL-3.0-only
//! `xtask no-home-paths [--root DIR]`: no personal home-folder path in any tracked file.
//!
//! A home path is `/Users/<name>` or `/home/<name>/`, where `<name>` is a run of `[A-Za-z0-9._-]`. It is
//! allowed only when `<name>` is one of the placeholders in `xtask/data/home-path-placeholders.txt`
//! (one per line, `#` comments). The account running the check is also refused by bare name (`$USER`,
//! six characters or more, unless it is a placeholder), which catches `ls -l` output and the like.
//! The same placeholder file drives the `.githooks/pre-commit` and `.githooks/commit-msg` hooks, so a
//! path is stopped before it enters a commit; this check is the backstop that every full test run
//! repeats (`xtask/tests/no_home_paths.rs`). Last line: `no-home-paths: PASS <files> files` (exit 0)
//! or one `path:line: <code> <name>` line per finding and `no-home-paths: FAIL <n> findings` (exit 1).
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    process::Command,
};

const USAGE: &str = "usage: xtask no-home-paths [--root DIR]";
/// Tracked path of the placeholder list, relative to the workspace root.
pub const PLACEHOLDERS: &str = "xtask/data/home-path-placeholders.txt";
/// A bare account name shorter than this is too likely to be an ordinary word to refuse on sight.
const MIN_BARE_USER: usize = 6;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Finding {
    pub path: String,
    pub line: usize,
    pub code: &'static str,
    pub name: String,
}

fn name_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'.' || b == b'_' || b == b'-'
}

/// Parse the placeholder list: one name per line, blank lines and `#` comments ignored.
pub fn parse_placeholders(text: &str) -> BTreeSet<String> {
    text.lines()
        .map(str::trim)
        .filter(|l| !l.is_empty() && !l.starts_with('#'))
        .map(str::to_string)
        .collect()
}

/// Every home-path or bare-user finding in `bytes`, as `(line, code, name)` (lines are 1-based).
pub fn scan(
    bytes: &[u8],
    placeholders: &BTreeSet<String>,
    user: Option<&str>,
) -> Vec<(usize, &'static str, String)> {
    let mut out = Vec::new();
    for (index, line) in bytes.split(|b| *b == b'\n').enumerate() {
        for (prefix, code, needs_slash) in [
            (&b"/Users/"[..], "home_path_users", false),
            (&b"/home/"[..], "home_path_home", true),
        ] {
            let mut at = 0;
            while let Some(found) = line[at..].windows(prefix.len()).position(|w| w == prefix) {
                let start = at + found + prefix.len();
                let end = start + line[start..].iter().take_while(|b| name_byte(**b)).count();
                at = start;
                if end == start || (needs_slash && line.get(end) != Some(&b'/')) {
                    continue;
                }
                let name = String::from_utf8_lossy(&line[start..end]).into_owned();
                if !placeholders.contains(&name) {
                    out.push((index + 1, code, name));
                }
            }
        }
        if let Some(user) = user.filter(|u| u.len() >= MIN_BARE_USER && !placeholders.contains(*u))
            && line.windows(user.len()).any(|w| w == user.as_bytes())
            && !out
                .iter()
                .any(|(l, _, n)| *l == index + 1 && n.contains(user))
        {
            out.push((index + 1, "current_user_name", user.to_string()));
        }
    }
    out
}

/// The account name to refuse by bare name: `$USER`, else `$LOGNAME`.
pub fn current_user() -> Option<String> {
    std::env::var("USER")
        .or_else(|_| std::env::var("LOGNAME"))
        .ok()
        .filter(|u| !u.is_empty())
}

fn tracked(root: &Path) -> Result<Vec<String>, String> {
    let out = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(["ls-files", "-z"])
        .output()
        .map_err(|e| format!("NO-HOME-PATHS: git ls-files: {:?}", e.kind()))?;
    if !out.status.success() {
        return Err(format!(
            "NO-HOME-PATHS: git ls-files exit {:?}",
            out.status.code()
        ));
    }
    Ok(out
        .stdout
        .split(|b| *b == 0)
        .filter(|p| !p.is_empty())
        .map(|p| String::from_utf8_lossy(p).into_owned())
        .collect())
}

/// Audit every tracked file under `root` (a Git work tree). Returns the files read and the findings.
pub fn audit(root: &Path, user: Option<&str>) -> Result<(usize, Vec<Finding>), String> {
    let list = fs::read_to_string(root.join(PLACEHOLDERS))
        .map_err(|e| format!("NO-HOME-PATHS: {PLACEHOLDERS}: {:?}", e.kind()))?;
    let placeholders = parse_placeholders(&list);
    let files = tracked(root)?;
    let mut findings = Vec::new();
    let mut read = 0;
    for path in &files {
        for (line, code, name) in scan(path.as_bytes(), &placeholders, user) {
            findings.push(Finding {
                path: path.clone(),
                line: line.saturating_sub(1),
                code,
                name,
            });
        }
        let full = root.join(path);
        // A tracked path deleted in the work tree (or a submodule) has no bytes to scan here.
        let Ok(meta) = fs::symlink_metadata(&full) else {
            continue;
        };
        let bytes = if meta.file_type().is_symlink() {
            fs::read_link(&full)
                .map_err(|e| format!("NO-HOME-PATHS: {path}: {:?}", e.kind()))?
                .into_os_string()
                .into_encoded_bytes()
        } else if meta.is_file() {
            fs::read(&full).map_err(|e| format!("NO-HOME-PATHS: {path}: {:?}", e.kind()))?
        } else {
            continue;
        };
        read += 1;
        for (line, code, name) in scan(&bytes, &placeholders, user) {
            findings.push(Finding {
                path: path.clone(),
                line,
                code,
                name,
            });
        }
    }
    Ok((read, findings))
}

pub fn cli(args: &[String]) -> Result<u8, String> {
    let mut root: Option<PathBuf> = None;
    let mut rest = args.iter();
    while let Some(arg) = rest.next() {
        match arg.as_str() {
            "--root" if root.is_none() => root = Some(PathBuf::from(rest.next().ok_or(USAGE)?)),
            _ => return Err(format!("{USAGE}\nunknown or repeated option {arg}")),
        }
    }
    let root = match root {
        Some(root) => root,
        None => PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .ok_or("NO-HOME-PATHS: no workspace parent")?
            .to_path_buf(),
    };
    let user = current_user();
    let (files, findings) = audit(&root, user.as_deref())?;
    for f in &findings {
        println!("{}:{}: {} {}", f.path, f.line, f.code, f.name);
    }
    if findings.is_empty() {
        println!("no-home-paths: PASS {files} files");
        Ok(0)
    } else {
        println!("no-home-paths: FAIL {} findings", findings.len());
        Ok(1)
    }
}
