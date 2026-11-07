// SPDX-License-Identifier: AGPL-3.0-only
//! `xtask gate`: run one command through the lane host guard, parse per-target
//! counts from cargo's test output (and cargo `--message-format=json` lines), and
//! append exactly one JSON evidence record. Replaces `tools/v02.py gate`.
//!
//! A run that executed nothing is never `PASS`: the verdict is `RAN-NOTHING`,
//! `ZERO-TESTS` or `NO-TEST-OUTPUT`, and the process exits 1 for every verdict
//! other than `PASS`.
use crate::release::Manifest;
use serde_json::Value;
use std::{
    collections::BTreeMap,
    env,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    process::Command,
    time::{Instant, SystemTime, UNIX_EPOCH},
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TargetCount {
    pub target: String,
    pub ok: bool,
    pub passed: u64,
    pub failed: u64,
    pub ignored: u64,
}
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Parsed {
    pub targets: Vec<TargetCount>,
    /// Sorted, de-duplicated `test NAME ... FAILED` names.
    pub failed_tests: Vec<String>,
    pub compile_errors: usize,
    /// A cargo JSON `build-finished` message reported `success: false`.
    pub build_failed: bool,
    /// Non-doc targets that ran no test at all (informational).
    pub zero_test_targets: Vec<String>,
    /// `Running` lines that never printed a libtest result (custom harness or cut log).
    pub unreported_targets: Vec<String>,
}
impl Parsed {
    pub fn total(&self) -> (u64, u64, u64) {
        (
            self.targets.iter().map(|t| t.passed).sum(),
            self.targets.iter().map(|t| t.failed).sum(),
            self.targets.iter().map(|t| t.ignored).sum(),
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// The command must run tests: no executed test, no `PASS`.
    Test,
    /// fmt, clippy, lint: exit status decides.
    Check,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    Pass,
    Fail,
    RanNothing,
    CompileError,
    ExitNonzeroNoTestFailure,
    ZeroTests,
    NoTestOutput,
    Killed,
}
impl Verdict {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Pass => "PASS",
            Self::Fail => "FAIL",
            Self::RanNothing => "RAN-NOTHING",
            Self::CompileError => "COMPILE-ERROR",
            Self::ExitNonzeroNoTestFailure => "EXIT-NONZERO-NO-TEST-FAILURE",
            Self::ZeroTests => "ZERO-TESTS",
            Self::NoTestOutput => "NO-TEST-OUTPUT",
            Self::Killed => "KILLED",
        }
    }
}

fn binary_name(path: &str) -> &str {
    let file = path.rsplit('/').next().unwrap_or(path);
    match file.rsplit_once('-') {
        Some((name, hash)) if hash.len() == 16 && hash.bytes().all(|b| b.is_ascii_hexdigit()) => {
            name
        }
        _ => file,
    }
}
fn running_label(line: &str) -> Option<String> {
    if !line.starts_with(char::is_whitespace) {
        return None;
    }
    let line = line.trim();
    if let Some(rest) = line.strip_prefix("Doc-tests ") {
        return Some(format!("doc:{}", rest.trim()));
    }
    let rest = line.strip_prefix("Running ")?.trim();
    // `cargo run` announces the program as Running `path args`: not a test target.
    if rest.starts_with('`') {
        return None;
    }
    match rest.rsplit_once(" (") {
        Some((spec, path)) if path.ends_with(')') => Some(format!(
            "{} ({})",
            spec.trim(),
            binary_name(path.trim_end_matches(')'))
        )),
        _ => Some(rest.to_string()),
    }
}
/// `test result: ok. N passed; N failed; N ignored; ...`
fn test_result(line: &str) -> Option<(bool, u64, u64, u64)> {
    let rest = line.strip_prefix("test result: ")?;
    let ok = rest.starts_with("ok.");
    if !ok && !rest.starts_with("FAILED.") {
        return None;
    }
    let (mut passed, mut failed, mut ignored) = (None, None, None);
    for segment in rest.split(';') {
        let words: Vec<&str> = segment.split_whitespace().collect();
        if let [.., number, word] = words.as_slice()
            && let Ok(n) = number.parse::<u64>()
        {
            match *word {
                "passed" => passed = Some(n),
                "failed" => failed = Some(n),
                "ignored" => ignored = Some(n),
                _ => {}
            }
        }
    }
    Some((ok, passed?, failed?, ignored?))
}
fn is_compile_error_line(line: &str) -> bool {
    if line.starts_with("error[E") {
        return true;
    }
    let Some(rest) = line.strip_prefix("error: ") else {
        return false;
    };
    if rest.starts_with("test failed") {
        return false;
    }
    // `error: 2 targets failed:` summarises test failures, not compilation.
    let digits = rest.bytes().take_while(u8::is_ascii_digit).count();
    !(digits > 0
        && (rest[digits..].starts_with(" target failed")
            || rest[digits..].starts_with(" targets failed")))
}

pub fn parse_output(text: &str) -> Parsed {
    let mut parsed = Parsed::default();
    let mut current: Option<String> = None;
    let mut failed = std::collections::BTreeSet::new();
    for line in text.lines() {
        if let Some(label) = running_label(line) {
            if let Some(previous) = current.replace(label) {
                parsed.unreported_targets.push(previous);
            }
            continue;
        }
        if let Some((ok, passed, failed_n, ignored)) = test_result(line) {
            let target = current.take().unwrap_or_else(|| "?".to_string());
            if passed + failed_n == 0 && !target.starts_with("doc:") {
                parsed.zero_test_targets.push(target.clone());
            }
            parsed.targets.push(TargetCount {
                target,
                ok,
                passed,
                failed: failed_n,
                ignored,
            });
            continue;
        }
        if let Some(name) = line
            .strip_prefix("test ")
            .and_then(|rest| rest.strip_suffix(" ... FAILED"))
        {
            failed.insert(name.to_string());
        } else if line.starts_with('{') {
            if let Ok(message) = serde_json::from_str::<Value>(line) {
                match message.get("reason").and_then(Value::as_str) {
                    Some("compiler-message")
                        if message.pointer("/message/level").and_then(Value::as_str)
                            == Some("error") =>
                    {
                        parsed.compile_errors += 1;
                    }
                    Some("build-finished")
                        if message.get("success").and_then(Value::as_bool) == Some(false) =>
                    {
                        parsed.build_failed = true;
                    }
                    _ => {}
                }
            }
        } else if is_compile_error_line(line) {
            parsed.compile_errors += 1;
        }
    }
    if let Some(previous) = current {
        parsed.unreported_targets.push(previous);
    }
    parsed.failed_tests = failed.into_iter().collect();
    parsed
}

pub fn verdict(parsed: &Parsed, exit: Option<i32>, kind: Kind) -> Verdict {
    let Some(code) = exit else {
        return Verdict::Killed;
    };
    if parsed.build_failed {
        return match kind {
            Kind::Test => Verdict::CompileError,
            Kind::Check => Verdict::Fail,
        };
    }
    if kind == Kind::Check {
        return if code == 0 {
            Verdict::Pass
        } else {
            Verdict::Fail
        };
    }
    let (passed, failed, _) = parsed.total();
    if code != 0 && parsed.targets.is_empty() {
        return if parsed.compile_errors == 0 {
            Verdict::RanNothing
        } else {
            Verdict::CompileError
        };
    }
    if failed > 0 || !parsed.failed_tests.is_empty() {
        return Verdict::Fail;
    }
    if code != 0 {
        return Verdict::ExitNonzeroNoTestFailure;
    }
    if parsed.targets.is_empty() {
        return Verdict::NoTestOutput;
    }
    if passed == 0 {
        return Verdict::ZeroTests;
    }
    Verdict::Pass
}

/// `cargo [+toolchain] test|t|nextest|bench ...` must run tests; everything else is a check.
pub fn infer_kind(argv: &[String]) -> Kind {
    let is_cargo = argv
        .first()
        .is_some_and(|a| a.rsplit('/').next() == Some("cargo"));
    let subcommand = argv
        .iter()
        .skip(1)
        .find(|a| !a.starts_with('+') && !a.starts_with('-'));
    match subcommand.map(String::as_str) {
        Some("test" | "t" | "nextest" | "bench") if is_cargo => Kind::Test,
        _ => Kind::Check,
    }
}

pub fn summary(parsed: &Parsed, verdict: Verdict) -> String {
    let (passed, failed, ignored) = parsed.total();
    let mut text = format!(
        "{}: {passed} passed / {failed} failed / {ignored} ignored over {} targets",
        verdict.as_str(),
        parsed.targets.len()
    );
    if !parsed.failed_tests.is_empty() {
        let shown: Vec<&str> = parsed
            .failed_tests
            .iter()
            .take(8)
            .map(String::as_str)
            .collect();
        text.push_str(&format!("\n  failed: {}", shown.join(", ")));
    }
    if !parsed.zero_test_targets.is_empty() {
        text.push_str(&format!(
            "\n  0-test targets: {}",
            parsed.zero_test_targets.len()
        ));
    }
    if parsed.compile_errors > 0 {
        text.push_str(&format!("\n  compile errors: {}", parsed.compile_errors));
    }
    if !parsed.unreported_targets.is_empty() {
        text.push_str(&format!(
            "\n  targets with no libtest result: {}",
            parsed.unreported_targets.len()
        ));
    }
    text
}

// ------------------------------------------------------------------ running

pub struct Options {
    pub lane: String,
    pub name: String,
    pub kind: Option<Kind>,
    pub registry: PathBuf,
    pub guard: PathBuf,
    pub argv: Vec<String>,
    /// `--candidate MANIFEST`: bind the record to the candidate's exact bytes. The worktree HEAD
    /// must equal the manifest's source commit and the exe/bundle must hash to the frozen
    /// digests before the command starts and again after it ends.
    pub candidate: Option<PathBuf>,
}

fn home() -> Result<PathBuf, String> {
    env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| "GATE: HOME is not set".to_string())
}
fn expand(path: &str) -> Result<PathBuf, String> {
    match path.strip_prefix("~/") {
        Some(rest) => Ok(home()?.join(rest)),
        None => Ok(PathBuf::from(path)),
    }
}
pub fn default_registry() -> Result<PathBuf, String> {
    Ok(home()?.join("Documents/mycelium-core-program/runs/v2/v02/registry"))
}
pub(crate) fn word(value: &str, what: &str) -> Result<(), String> {
    let ok = !value.is_empty()
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b))
        && !value.starts_with('.');
    if ok {
        Ok(())
    } else {
        Err(format!(
            "USAGE: {what} must be [A-Za-z0-9._-]+ and not start with '.'"
        ))
    }
}

struct Lane {
    worktree: PathBuf,
    target: Option<PathBuf>,
}
fn lane_row(registry: &Path, lane: &str) -> Result<Lane, String> {
    let path = registry.join("lanes.tsv");
    let text = fs::read_to_string(&path)
        .map_err(|e| format!("GATE: {}: {:?}", path.display(), e.kind()))?;
    let mut lines = text
        .lines()
        .filter(|l| !l.trim().is_empty() && !l.starts_with('#'));
    let header: Vec<&str> = lines
        .next()
        .ok_or("GATE: lanes.tsv is empty")?
        .split('\t')
        .collect();
    let column = |name: &str| header.iter().position(|h| *h == name);
    let (lane_col, tree_col) = (
        column("lane").ok_or("GATE: lanes.tsv has no lane column")?,
        column("worktree").ok_or("GATE: lanes.tsv has no worktree column")?,
    );
    for line in lines {
        let fields: Vec<&str> = line.split('\t').collect();
        if fields.get(lane_col) == Some(&lane) {
            let target = match column("target").and_then(|i| fields.get(i)) {
                Some(t) if !t.is_empty() => Some(expand(t)?),
                _ => None,
            };
            let worktree = fields.get(tree_col).ok_or("GATE: short lanes.tsv row")?;
            return Ok(Lane {
                worktree: expand(worktree)?,
                target,
            });
        }
    }
    Err(format!("USAGE: lane {lane} is not in {}", path.display()))
}

fn command_output(program: &str, args: &[&str]) -> Result<String, String> {
    let output = Command::new(program)
        .args(args)
        .output()
        .map_err(|e| format!("{program}: {:?}", e.kind()))?;
    if !output.status.success() {
        return Err(format!("{program}: exit {:?}", output.status.code()));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}
pub(crate) fn local(format: &str) -> Result<String, String> {
    command_output("date", &[format])
}

pub(crate) fn json_line(entries: Vec<(&str, Value)>) -> Result<String, String> {
    let map: BTreeMap<&str, Value> = entries.into_iter().collect();
    serde_json::to_string(&map).map_err(|e| format!("GATE: encode record: {e}"))
}

/// Append one record, assigning the next `EV-<date>-<seq>` id under an exclusive lock.
pub(crate) fn append_record(
    path: &Path,
    date: &str,
    build: impl FnOnce(&str) -> Result<String, String>,
) -> Result<String, String> {
    let io = |e: std::io::Error| format!("GATE: {}: {:?}", path.display(), e.kind());
    let mut file = OpenOptions::new()
        .create(true)
        .read(true)
        .append(true)
        .open(path)
        .map_err(io)?;
    file.lock().map_err(io)?;
    let mut existing = String::new();
    file.read_to_string(&mut existing).map_err(io)?;
    let id = format!("EV-{date}-{:04}", existing.lines().count() + 1);
    let mut line = build(&id)?;
    line.push('\n');
    file.write_all(line.as_bytes()).map_err(io)?;
    file.sync_all().map_err(io)?;
    file.unlock().map_err(io)?;
    Ok(id)
}

/// Create `<stamp>-<name>.log` exclusively; a taken name gets `-2`, `-3`, ... (never truncates).
pub fn create_log(directory: &Path, stamp: &str, name: &str) -> Result<(File, PathBuf), String> {
    fs::create_dir_all(directory)
        .map_err(|e| format!("GATE: {}: {:?}", directory.display(), e.kind()))?;
    for attempt in 0..1000 {
        let file_name = if attempt == 0 {
            format!("{stamp}-{name}.log")
        } else {
            format!("{stamp}-{name}-{}.log", attempt + 1)
        };
        let path = directory.join(file_name);
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(file) => return Ok((file, path)),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(format!("GATE: {}: {:?}", path.display(), e.kind())),
        }
    }
    Err("GATE: no free log name".into())
}

/// Returns the process exit code: the guard's code when it refuses, 1 for any
/// verdict other than PASS, else 0.
pub fn run(options: &Options) -> Result<u8, String> {
    if options.argv.is_empty() {
        return Err("USAGE: gate needs a command after `--`".into());
    }
    let lane = lane_row(&options.registry, &options.lane)?;
    if !options.guard.is_file() {
        return Err(format!(
            "GATE: guard script {} is missing; refusing to run unguarded",
            options.guard.display()
        ));
    }
    let guard = Command::new("bash")
        .arg(&options.guard)
        .output()
        .map_err(|e| format!("GATE: guard did not start: {:?}", e.kind()))?;
    match guard.status.code() {
        Some(0) => {}
        Some(code) => {
            println!(
                "GUARD {code}: {}",
                String::from_utf8_lossy(&guard.stdout).trim()
            );
            return Ok(u8::try_from(code).unwrap_or(1));
        }
        None => return Err("GATE: guard was killed by a signal".into()),
    }
    let kind = options.kind.unwrap_or_else(|| infer_kind(&options.argv));
    let commit = command_output(
        "git",
        &[
            "-C",
            &lane.worktree.display().to_string(),
            "rev-parse",
            "HEAD",
        ],
    )
    .unwrap_or_else(|e| format!("<git rev-parse failed: {e}>"));
    let candidate = match &options.candidate {
        Some(path) => {
            let manifest = Manifest::load(path).map_err(|e| format!("GATE: {e}"))?;
            if manifest.candidate.source_commit != commit {
                return Err(format!(
                    "GATE: candidate_source_mismatch: the lane worktree is at {commit}, the candidate is {}",
                    manifest.candidate.source_commit
                ));
            }
            manifest.verify_bytes().map_err(|e| format!("GATE: {e}"))?;
            Some(manifest)
        }
        None => None,
    };
    let load = command_output("sysctl", &["-n", "vm.loadavg"])
        .unwrap_or_else(|e| format!("<sysctl failed: {e}>"));
    let (log_file, log_path) = create_log(
        &options.registry.join("logs").join(&options.lane),
        &local("+%Y%m%dT%H%M%S")?,
        &options.name,
    )?;
    let started_at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|e| format!("CLOCK: {e}"))?
        .as_secs_f64();
    let clock = Instant::now();
    let mut command = Command::new("nice");
    command
        .args(["-n", "19"])
        .args(&options.argv)
        .current_dir(&lane.worktree)
        .env_remove("PYTHONPATH")
        .env("CARGO_INCREMENTAL", "0")
        .env("CARGO_PROFILE_DEV_DEBUG", "0")
        .env("CARGO_PROFILE_TEST_DEBUG", "0")
        .stdout(
            log_file
                .try_clone()
                .map_err(|e| format!("GATE: log handle: {:?}", e.kind()))?,
        )
        .stderr(log_file);
    if let Some(target) = &lane.target {
        command.env("CARGO_TARGET_DIR", target);
    }
    let status = command
        .status()
        .map_err(|e| format!("GATE: could not start `nice`: {:?}", e.kind()))?;
    let wall = (clock.elapsed().as_secs_f64() * 10.0).round() / 10.0;
    if let Some(manifest) = &candidate {
        manifest.verify_bytes().map_err(|e| {
            format!(
                "GATE: candidate_bytes_changed: {e}; no record written, log kept at {}",
                log_path.display()
            )
        })?;
    }
    let exit = status.code();
    let text = fs::read(&log_path).map_err(|e| format!("GATE: re-read log: {:?}", e.kind()))?;
    let parsed = parse_output(&String::from_utf8_lossy(&text));
    let verdict = verdict(&parsed, exit, kind);
    let (passed, failed, ignored) = parsed.total();
    let targets: Vec<Value> = parsed
        .targets
        .iter()
        .map(|t| {
            serde_json::to_value(BTreeMap::from([
                ("failed", Value::from(t.failed)),
                ("ignored", Value::from(t.ignored)),
                ("ok", Value::from(t.ok)),
                ("passed", Value::from(t.passed)),
                ("target", Value::from(t.target.as_str())),
            ]))
        })
        .collect::<Result<_, _>>()
        .map_err(|e| format!("GATE: encode targets: {e}"))?;
    let id = append_record(
        &options.registry.join("evidence.jsonl"),
        &local("+%Y%m%d")?,
        |id| {
            let mut entries = vec![
                ("argv", Value::from(options.argv.clone())),
                ("at", Value::from(started_at)),
                ("commit", Value::from(commit.as_str())),
                ("exit", exit.map_or(Value::Null, Value::from)),
                ("failed_tests", Value::from(parsed.failed_tests.clone())),
                ("id", Value::from(id)),
                ("kind", Value::from("gate")),
                ("lane", Value::from(options.lane.as_str())),
                ("load", Value::from(load.as_str())),
                ("log", Value::from(log_path.display().to_string())),
                ("name", Value::from(options.name.as_str())),
                ("targets", Value::from(targets)),
                (
                    "total",
                    serde_json::to_value(BTreeMap::from([
                        ("failed", failed),
                        ("ignored", ignored),
                        ("passed", passed),
                    ]))
                    .map_err(|e| format!("GATE: encode totals: {e}"))?,
                ),
                ("verdict", Value::from(verdict.as_str())),
                ("wall_s", Value::from(wall)),
            ];
            if let Some(manifest) = &candidate {
                entries.push(("candidate", manifest.candidate.to_json()));
            }
            json_line(entries)
        },
    )?;
    println!(
        "{id} {} @ {} exit {} in {wall}s -> {}",
        options.name,
        commit.chars().take(7).collect::<String>(),
        exit.map_or("signal".to_string(), |c| c.to_string()),
        summary(&parsed, verdict)
    );
    Ok(u8::from(verdict != Verdict::Pass))
}

const USAGE: &str = "USAGE: cargo xtask gate <LANE> <NAME> [--kind test|check] [--registry DIR] [--guard PATH] -- <command...>\n       cargo xtask gate parse <LOG> [--exit N] [--kind test|check]";

fn kind_arg(value: &str) -> Result<Kind, String> {
    match value {
        "test" => Ok(Kind::Test),
        "check" => Ok(Kind::Check),
        _ => Err(format!("USAGE: --kind must be test or check, got {value}")),
    }
}

/// `xtask gate ...` entry point (arguments after the word `gate`).
pub fn cli(args: &[String]) -> Result<u8, String> {
    if args.first().map(String::as_str) == Some("parse") {
        return cli_parse(&args[1..]);
    }
    let mut positional = Vec::new();
    let (mut kind, mut registry, mut guard, mut candidate) = (None, None, None, None);
    let mut rest = args.iter();
    let mut argv = None;
    while let Some(arg) = rest.next() {
        match arg.as_str() {
            "--" => {
                argv = Some(rest.cloned().collect::<Vec<_>>());
                break;
            }
            "--kind" if kind.is_none() => {
                kind = Some(kind_arg(rest.next().ok_or(USAGE)?)?);
            }
            "--registry" if registry.is_none() => {
                registry = Some(PathBuf::from(rest.next().ok_or(USAGE)?));
            }
            "--guard" if guard.is_none() => {
                guard = Some(PathBuf::from(rest.next().ok_or(USAGE)?));
            }
            "--candidate" if candidate.is_none() => {
                candidate = Some(PathBuf::from(rest.next().ok_or(USAGE)?));
            }
            other if !other.starts_with("--") && positional.len() < 2 => {
                positional.push(other.to_string());
            }
            other => {
                return Err(format!(
                    "USAGE: unknown or repeated option {other}\n{USAGE}"
                ));
            }
        }
    }
    let [lane, name] = positional.as_slice() else {
        return Err(USAGE.into());
    };
    if lane == "parse" {
        return Err("USAGE: `parse` is reserved".into());
    }
    word(lane, "lane")?;
    word(name, "name")?;
    let argv = argv.filter(|a| !a.is_empty()).ok_or(USAGE)?;
    let registry = match registry {
        Some(r) => r,
        None => default_registry()?,
    };
    let guard = guard.unwrap_or_else(|| registry.join("../tools/lane-guard.sh"));
    run(&Options {
        lane: lane.clone(),
        name: name.clone(),
        kind,
        registry,
        guard,
        argv,
        candidate,
    })
}

fn cli_parse(args: &[String]) -> Result<u8, String> {
    let (mut log, mut exit, mut kind) = (None, None, Kind::Test);
    let mut rest = args.iter();
    while let Some(arg) = rest.next() {
        match arg.as_str() {
            "--exit" if exit.is_none() => {
                exit = Some(
                    rest.next()
                        .ok_or(USAGE)?
                        .parse::<i32>()
                        .map_err(|e| format!("USAGE: --exit: {e}"))?,
                );
            }
            "--kind" => kind = kind_arg(rest.next().ok_or(USAGE)?)?,
            other if !other.starts_with("--") && log.is_none() => log = Some(other.to_string()),
            other => {
                return Err(format!(
                    "USAGE: unknown or repeated option {other}\n{USAGE}"
                ));
            }
        }
    }
    let log = log.ok_or(USAGE)?;
    let exit = exit.ok_or("USAGE: gate parse needs --exit N (the command's exit status)")?;
    let text = fs::read(&log).map_err(|e| format!("GATE: {log}: {:?}", e.kind()))?;
    let parsed = parse_output(&String::from_utf8_lossy(&text));
    let verdict = verdict(&parsed, Some(exit), kind);
    println!("{}", summary(&parsed, verdict));
    Ok(u8::from(verdict != Verdict::Pass))
}
