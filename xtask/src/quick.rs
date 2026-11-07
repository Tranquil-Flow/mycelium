// SPDX-License-Identifier: AGPL-3.0-only
//! `cargo xtask quick`: the fast edit-compile-test loop (process fix P4).
//!
//! It finds the workspace crates changed versus a base revision (committed branch work, staged and
//! unstaged edits, and untracked files), adds every crate that depends on them, then runs
//! `cargo fmt --check`, `cargo clippy -p <crates> --all-targets -- -D warnings` and
//! `cargo nextest run --profile quick -p <crates>`. The first failing step stops the run.
//!
//! Selection is pure and testable ([`Workspace::affected`]); only [`cli`] touches git and cargo.
//! Every failure is reported with a specific `QUICK:` code and a non-zero exit; nothing is skipped
//! silently.
use serde::Deserialize;
use std::{
    collections::{BTreeMap, BTreeSet},
    env,
    ffi::OsString,
    path::{Path, PathBuf},
    process::Command,
    time::Instant,
};

/// Root-relative files that change the build of every crate. A change to one of them selects the
/// whole workspace, because the diff alone cannot say which crates it reaches.
pub const GLOBAL_FILES: [&str; 6] = [
    "Cargo.toml",
    "Cargo.lock",
    "rust-toolchain",
    "rust-toolchain.toml",
    ".cargo/config",
    ".cargo/config.toml",
];

/// The branch quick compares against unless `--base` says otherwise.
pub const DEFAULT_BASE: &str = "main";

const USAGE: &str = "USAGE: cargo xtask quick [--base REV] [--list] [--root PATH]";

#[derive(Debug, Deserialize)]
struct Metadata {
    workspace_root: String,
    packages: Vec<Package>,
}
#[derive(Debug, Deserialize)]
struct Package {
    name: String,
    manifest_path: String,
    dependencies: Vec<Dependency>,
}
#[derive(Debug, Deserialize)]
struct Dependency {
    name: String,
}

/// One workspace crate: its directory relative to the workspace root (`/` separated, empty for a
/// root package) and the workspace crates it depends on (normal, dev and build edges alike).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Member {
    pub name: String,
    pub dir: String,
    pub depends_on: BTreeSet<String>,
}

/// The crates selected for a quick run and why.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Affected {
    /// Crates with a changed file, or the whole workspace when `everything` is set.
    pub changed: BTreeSet<String>,
    /// `changed` plus every workspace crate that depends on one of them, transitively.
    pub crates: BTreeSet<String>,
    /// A root build file (see [`GLOBAL_FILES`]) changed, so every crate is selected.
    pub everything: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Workspace {
    members: Vec<Member>,
}

impl Workspace {
    /// Build the workspace graph from `cargo metadata --no-deps --format-version 1` output.
    pub fn from_metadata(json: &str) -> Result<Self, String> {
        let metadata: Metadata = serde_json::from_str(json)
            .map_err(|error| format!("QUICK: cargo metadata: {error}"))?;
        let root = metadata.workspace_root.trim_end_matches('/').to_string();
        let names: BTreeSet<&str> = metadata.packages.iter().map(|p| p.name.as_str()).collect();
        let mut members = Vec::new();
        for package in &metadata.packages {
            let manifest_dir = Path::new(&package.manifest_path)
                .parent()
                .and_then(Path::to_str)
                .ok_or_else(|| format!("QUICK: {}: manifest path has no parent", package.name))?;
            let dir = manifest_dir
                .strip_prefix(&root)
                .ok_or_else(|| {
                    format!(
                        "QUICK: {}: manifest {manifest_dir} is outside workspace root {root}",
                        package.name
                    )
                })?
                .trim_matches('/')
                .to_string();
            let depends_on = package
                .dependencies
                .iter()
                .filter(|dependency| {
                    dependency.name != package.name && names.contains(dependency.name.as_str())
                })
                .map(|dependency| dependency.name.clone())
                .collect();
            members.push(Member {
                name: package.name.clone(),
                dir,
                depends_on,
            });
        }
        if members.is_empty() {
            return Err("QUICK: cargo metadata lists no workspace crates".into());
        }
        members.sort_by(|a, b| a.name.cmp(&b.name));
        Ok(Self { members })
    }

    pub fn names(&self) -> BTreeSet<String> {
        self.members.iter().map(|m| m.name.clone()).collect()
    }

    /// The crate that owns a root-relative path: the member with the longest matching directory.
    /// `None` for paths outside every crate (docs, evidence, runs, root files).
    pub fn owner_of(&self, path: &str) -> Option<&str> {
        self.members
            .iter()
            .filter(|member| {
                member.dir.is_empty()
                    || path == member.dir
                    || path
                        .strip_prefix(member.dir.as_str())
                        .is_some_and(|rest| rest.starts_with('/'))
            })
            .max_by_key(|member| member.dir.len())
            .map(|member| member.name.as_str())
    }

    /// Select crates for a list of changed root-relative paths.
    pub fn affected(&self, changed_files: &[String]) -> Affected {
        let everything = changed_files
            .iter()
            .any(|file| GLOBAL_FILES.contains(&file.as_str()));
        let changed: BTreeSet<String> = if everything {
            self.names()
        } else {
            changed_files
                .iter()
                .filter_map(|file| self.owner_of(file))
                .map(str::to_string)
                .collect()
        };
        let mut dependents: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
        for member in &self.members {
            for dependency in &member.depends_on {
                dependents
                    .entry(dependency.as_str())
                    .or_default()
                    .push(member.name.as_str());
            }
        }
        let mut crates = changed.clone();
        let mut pending: Vec<&str> = changed.iter().map(String::as_str).collect();
        while let Some(name) = pending.pop() {
            for dependent in dependents.get(name).into_iter().flatten() {
                if crates.insert((*dependent).to_string()) {
                    pending.push(dependent);
                }
            }
        }
        Affected {
            changed,
            crates,
            everything,
        }
    }
}

/// Split NUL-terminated `git -z` output into paths. A path that is not UTF-8 is an error, never
/// dropped: a dropped path could hide a changed crate.
pub fn split_nul(bytes: &[u8]) -> Result<Vec<String>, String> {
    bytes
        .split(|byte| *byte == 0)
        .filter(|chunk| !chunk.is_empty())
        .map(|chunk| {
            String::from_utf8(chunk.to_vec())
                .map_err(|_| "QUICK: git printed a path that is not UTF-8".to_string())
        })
        .collect()
}

fn git(root: &Path, args: &[&str]) -> Result<Vec<u8>, String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(args)
        .output()
        .map_err(|error| format!("QUICK: git {}: {:?}", args.join(" "), error.kind()))?;
    if !output.status.success() {
        return Err(format!(
            "QUICK: git {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(output.stdout)
}

/// Files changed versus `base`: committed work since the merge base, staged and unstaged edits to
/// tracked files, deletions, and untracked files that are not ignored. Renames report both paths.
/// Also returns the merge-base commit used.
pub fn changed_files(root: &Path, base: &str) -> Result<(String, Vec<String>), String> {
    let merge_base = String::from_utf8(git(root, &["merge-base", base, "HEAD"])?)
        .map_err(|_| "QUICK: git merge-base printed non-UTF-8".to_string())?
        .trim()
        .to_string();
    if merge_base.is_empty() {
        return Err(format!("QUICK: no merge base between {base} and HEAD"));
    }
    let tracked = git(
        root,
        &["diff", "--name-only", "--no-renames", "-z", &merge_base],
    )?;
    let untracked = git(root, &["ls-files", "--others", "--exclude-standard", "-z"])?;
    let mut files: BTreeSet<String> = BTreeSet::new();
    files.extend(split_nul(&tracked)?);
    files.extend(split_nul(&untracked)?);
    Ok((merge_base, files.into_iter().collect()))
}

fn cargo_program() -> OsString {
    env::var_os("CARGO").unwrap_or_else(|| OsString::from("cargo"))
}

fn workspace_metadata(root: &Path) -> Result<String, String> {
    let output = Command::new(cargo_program())
        .current_dir(root)
        .args([
            "metadata",
            "--no-deps",
            "--format-version",
            "1",
            "--offline",
        ])
        .output()
        .map_err(|error| format!("QUICK: cargo metadata: {:?}", error.kind()))?;
    if !output.status.success() {
        return Err(format!(
            "QUICK: cargo metadata failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    String::from_utf8(output.stdout).map_err(|_| "QUICK: cargo metadata printed non-UTF-8".into())
}

/// The three steps, in order, as `cargo` argument lists. `crates` is non-empty for clippy/nextest.
pub fn steps(crates: &BTreeSet<String>) -> Vec<(&'static str, Vec<String>)> {
    let packages: Vec<String> = crates
        .iter()
        .flat_map(|name| ["-p".to_string(), name.clone()])
        .collect();
    let mut result = vec![("fmt", vec!["fmt".to_string(), "--check".to_string()])];
    if crates.is_empty() {
        return result;
    }
    let mut clippy = vec!["clippy".to_string()];
    clippy.extend(packages.iter().cloned());
    clippy.extend(
        ["--all-targets", "--", "-D", "warnings"]
            .iter()
            .map(|s| s.to_string()),
    );
    result.push(("clippy", clippy));
    let mut nextest = ["nextest", "run", "--profile", "quick", "--no-tests=warn"]
        .iter()
        .map(|s| s.to_string())
        .collect::<Vec<_>>();
    nextest.extend(packages);
    result.push(("nextest", nextest));
    result
}

struct Options {
    base: String,
    list_only: bool,
    root: PathBuf,
}

fn parse(args: &[String]) -> Result<Options, String> {
    let mut options = Options {
        base: DEFAULT_BASE.to_string(),
        list_only: false,
        root: PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .ok_or("QUICK: no workspace parent")?
            .to_path_buf(),
    };
    let (mut base_set, mut root_set) = (false, false);
    let mut iter = args.iter();
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--list" if !options.list_only => options.list_only = true,
            "--base" if !base_set => {
                options.base = iter
                    .next()
                    .ok_or_else(|| format!("{USAGE}\nQUICK: --base requires a revision"))?
                    .clone();
                base_set = true;
            }
            "--root" if !root_set => {
                options.root = PathBuf::from(
                    iter.next()
                        .ok_or_else(|| format!("{USAGE}\nQUICK: --root requires a path"))?,
                );
                root_set = true;
            }
            _ => return Err(format!("{USAGE}\nQUICK: unknown or repeated option {arg}")),
        }
    }
    Ok(options)
}

/// Entry point for `cargo xtask quick`. Exit codes: 0 every step passed (or `--list`), 1 a step
/// failed, 2 usage, git or metadata error (returned as `Err`).
pub fn cli(args: &[String]) -> Result<u8, String> {
    let options = parse(args)?;
    let workspace = Workspace::from_metadata(&workspace_metadata(&options.root)?)?;
    let (merge_base, files) = changed_files(&options.root, &options.base)?;
    let affected = workspace.affected(&files);
    let short = merge_base.get(..12).unwrap_or(&merge_base);
    println!(
        "quick: base {} (merge-base {short}), {} changed files, {} of {} crates selected{}",
        options.base,
        files.len(),
        affected.crates.len(),
        workspace.names().len(),
        if affected.everything {
            " (a root build file changed: whole workspace)"
        } else {
            ""
        }
    );
    for name in &affected.crates {
        let why = if affected.changed.contains(name) {
            "changed"
        } else {
            "depends on a changed crate"
        };
        println!("quick:   {name} ({why})");
    }
    if options.list_only {
        return Ok(0);
    }
    if affected.crates.is_empty() {
        println!("quick: no crate changed; only the format check runs");
    }
    let started = Instant::now();
    for (label, arguments) in steps(&affected.crates) {
        println!("quick: [{label}] cargo {}", arguments.join(" "));
        let step_started = Instant::now();
        let status = Command::new(cargo_program())
            .current_dir(&options.root)
            .args(&arguments)
            .status()
            .map_err(|error| format!("QUICK: cargo {label}: {:?}", error.kind()))?;
        if !status.success() {
            println!(
                "quick: FAIL at {label} ({status}) after {:.1}s ({:.1}s in total)",
                step_started.elapsed().as_secs_f64(),
                started.elapsed().as_secs_f64()
            );
            return Ok(1);
        }
        println!(
            "quick: [{label}] ok in {:.1}s",
            step_started.elapsed().as_secs_f64()
        );
    }
    println!("quick: PASS in {:.1}s", started.elapsed().as_secs_f64());
    Ok(0)
}
