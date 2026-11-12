// SPDX-License-Identifier: AGPL-3.0-only
//! `xtask release emit` and `xtask release produce`: how each release-profile cell gets its record
//! (CL-L12-01, CL-L12-07).
//!
//! * A **gate cell** is produced by `release produce <cell>`: the cell's own command (or an
//!   explicit one that the cell would select) runs through the real `xtask gate` with
//!   `--candidate`, so the record is bound to the candidate's exact bytes (the budget-model run
//!   included).
//! * A **receipt cell** (`release_receipt`, `device`, `rehearsal`, `release_matrix`) is produced
//!   by `release emit`: either a `PASS` record that names the producer and carries the sha256 of
//!   every artifact file (re-hashed whenever the machine reads the ledger), bound to the candidate
//!   after the exe and bundle are re-hashed; or an explicit `NOT_YET` record with a reason and the
//!   blocker ids. Nothing here interprets an artifact: the producer owns what its receipt means.
//!
//! Records are appended to `evidence.jsonl` under lane `RELEASE`, so `tools/v02.py verify <lane>`
//! (which only reads records of the lane being verified) never meets them.
use crate::{
    gate,
    release::{self, Claims, Manifest, Profile, fail, io_text, usage},
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

/// Record kinds `release emit` can write (a `gate` record only ever comes from `xtask gate`).
pub const EMIT_KINDS: [&str; 4] = ["release_receipt", "device", "rehearsal", "release_matrix"];
const LANE: &str = "RELEASE";
/// One artifact is hashed whole; refuse anything this large (the largest admitted file is the
/// 147 MB metallib, so this leaves wide headroom without letting a device image through).
const ARTIFACT_MAX_BYTES: u64 = 1 << 30;

#[derive(Default)]
struct Options {
    values: BTreeMap<String, String>,
    artifacts: Vec<String>,
    blockers: Vec<String>,
    flags: Vec<String>,
    tail: Option<Vec<String>>,
    positional: Vec<String>,
}
fn parse(
    args: &[String],
    valued: &[&str],
    flags: &[&str],
    repeated: &[&str],
) -> Result<Options, String> {
    let mut options = Options::default();
    let mut rest = args.iter();
    while let Some(arg) = rest.next() {
        let key = arg.as_str();
        if key == "--" {
            options.tail = Some(rest.cloned().collect());
            break;
        } else if flags.contains(&key) {
            if options.flags.iter().any(|f| f == key) {
                return Err(usage(&format!("repeated option {key}")));
            }
            options.flags.push(key.to_string());
        } else if valued.contains(&key) || repeated.contains(&key) {
            let value = rest
                .next()
                .ok_or_else(|| usage(&format!("{key} needs a value")))?
                .clone();
            match key {
                "--artifact" => options.artifacts.push(value),
                "--blocker" => options.blockers.push(value),
                _ => {
                    if options.values.insert(key.to_string(), value).is_some() {
                        return Err(usage(&format!("repeated option {key}")));
                    }
                }
            }
        } else if key.starts_with("--") {
            return Err(usage(&format!("unknown option {key}")));
        } else {
            options.positional.push(key.to_string());
        }
    }
    Ok(options)
}
fn registry_of(options: &Options) -> Result<PathBuf, String> {
    match options.values.get("--registry") {
        Some(r) => Ok(PathBuf::from(r)),
        None => gate::default_registry(),
    }
}
fn load_profile(options: &Options, registry: &Path) -> Result<Profile, String> {
    let path = options.values.get("--profile").map_or_else(
        || PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("release-profile.json"),
        PathBuf::from,
    );
    let read = |p: &Path, code: &str| {
        fs::read_to_string(p).map_err(|e| fail(code, format!("{}: {}", p.display(), io_text(&e))))
    };
    let claims = Claims::parse(&read(&registry.join("CLAIMS.tsv"), "claims_unreadable")?)?;
    Profile::parse(&read(&path, "profile_unreadable")?, &claims)
}
fn now() -> Result<f64, String> {
    Ok(SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|e| fail("clock_invalid", e))?
        .as_secs_f64())
}

/// An artifact is a regular, non-empty, bounded file reached without a symlink at the leaf.
fn artifact_entry(path: &str) -> Result<Value, String> {
    let invalid = |why: String| fail("emit_artifact_invalid", format!("{path}: {why}"));
    let meta = fs::symlink_metadata(path).map_err(|e| invalid(io_text(&e)))?;
    if meta.file_type().is_symlink() {
        return Err(invalid("a symlink is not an artifact".into()));
    }
    if !meta.is_file() {
        return Err(invalid("not a regular file".into()));
    }
    if meta.len() == 0 {
        return Err(invalid("empty".into()));
    }
    if meta.len() > ARTIFACT_MAX_BYTES {
        return Err(invalid(format!(
            "{} bytes exceeds {ARTIFACT_MAX_BYTES}",
            meta.len()
        )));
    }
    let canonical = fs::canonicalize(path).map_err(|e| invalid(io_text(&e)))?;
    let sha256 = release::file_sha256(&canonical).map_err(invalid)?;
    Ok(json!({"path": canonical.display().to_string(), "sha256": sha256, "bytes": meta.len()}))
}

/// `xtask release emit <kind> <name> ...`
pub fn emit(args: &[String]) -> Result<u8, String> {
    let options = parse(
        args,
        &[
            "--manifest",
            "--profile",
            "--registry",
            "--producer",
            "--reason",
        ],
        &["--pass", "--not-yet"],
        &["--artifact", "--blocker"],
    )?;
    let [kind, name] = options.positional.as_slice() else {
        return Err(usage("emit needs <kind> <name>"));
    };
    if !EMIT_KINDS.contains(&kind.as_str()) {
        return Err(fail(
            "emit_kind_unsupported",
            format!(
                "{kind}: one of {}; a gate record only comes from `xtask gate` / `release produce`",
                EMIT_KINDS.join(", ")
            ),
        ));
    }
    let registry = registry_of(&options)?;
    let profile = load_profile(&options, &registry)?;
    let cell = profile
        .cells
        .iter()
        .find(|c| c.verify.kind == *kind && c.verify.name.as_deref() == Some(name.as_str()));
    let operator = cell.is_some_and(|c| c.verify.operator_receipt);
    if cell.is_none() {
        return Err(fail(
            "emit_unknown_cell",
            format!(
                "the profile {} has no {kind} cell named {name}",
                profile.name
            ),
        ));
    }
    let has = |flag: &str| options.flags.iter().any(|f| f == flag);
    let (pass, not_yet) = (has("--pass"), has("--not-yet"));
    if pass == not_yet {
        return Err(usage("give exactly one of --pass and --not-yet"));
    }
    let producer = options.values.get("--producer");
    let mut entries: Vec<(&str, Value)> = vec![
        ("at", Value::from(now()?)),
        ("kind", Value::from(kind.as_str())),
        ("lane", Value::from(LANE)),
        ("name", Value::from(name.as_str())),
    ];
    if let Some(producer) = producer.filter(|p| !p.trim().is_empty()) {
        entries.push(("producer", Value::from(producer.as_str())));
    }
    if not_yet {
        if !options.artifacts.is_empty() {
            return Err(usage("--not-yet takes no --artifact"));
        }
        let reason = options
            .values
            .get("--reason")
            .filter(|r| !r.trim().is_empty())
            .ok_or_else(|| fail("emit_reason_missing", "a NOT_YET record states why"))?;
        entries.push(("reason", Value::from(reason.as_str())));
        entries.push(("blockers", Value::from(options.blockers.clone())));
        entries.push(("verdict", Value::from("NOT_YET")));
    } else {
        if options.values.contains_key("--reason") || !options.blockers.is_empty() {
            return Err(usage("--reason and --blocker belong to --not-yet"));
        }
        if options.artifacts.is_empty() {
            return Err(fail(
                "emit_artifact_missing",
                "a PASS names the artifact file(s) it rests on (--artifact FILE)",
            ));
        }
        if producer.is_none_or(|p| p.trim().is_empty()) {
            return Err(fail(
                "emit_producer_missing",
                "a PASS names the tool or person that produced the artifact (--producer TEXT)",
            ));
        }
        let artifacts = options
            .artifacts
            .iter()
            .map(|p| artifact_entry(p))
            .collect::<Result<Vec<_>, _>>()?;
        let manifest_path = options
            .values
            .get("--manifest")
            .ok_or_else(|| usage("--manifest is required for --pass"))?;
        let manifest = Manifest::load(Path::new(manifest_path))?;
        manifest.verify_bytes()?;
        if operator {
            // An operator receipt must name the candidate it was taken on (lane RELF item 4).
            let probe = release::Record {
                id: String::new(),
                kind: kind.clone(),
                at: 0.0,
                value: json!({"artifacts": artifacts}),
            };
            release::names_candidate(&manifest, &probe)
                .map_err(|why| fail("operator_receipt_unbound", why))?;
            entries.push(("operator_receipt", Value::from(true)));
        }
        // Every receipt states this cell's PASS for this candidate (review RELF F14).
        let files = options
            .artifacts
            .iter()
            .map(|p| {
                fs::read(p).map_err(|e| fail("artifact_missing", format!("{p}: {}", io_text(&e))))
            })
            .collect::<Result<Vec<_>, _>>()?;
        release::receipt_result(&manifest, name, &files).map_err(|(code, why)| fail(code, why))?;
        entries.push(("artifacts", Value::from(artifacts)));
        entries.push(("candidate", manifest.candidate.to_json()));
        entries.push((
            "commit",
            Value::from(manifest.candidate.source_commit.as_str()),
        ));
        entries.push(("exit", Value::from(0)));
        entries.push(("verdict", Value::from("PASS")));
    }
    let id = gate::append_record(
        &registry.join("evidence.jsonl"),
        &gate::local("+%Y%m%d")?,
        |id| {
            entries.push(("id", Value::from(id)));
            gate::json_line(entries)
        },
    )?;
    println!(
        "{id} {kind} {name} {}",
        if pass { "PASS" } else { "NOT_YET" }
    );
    Ok(0)
}

/// `xtask release produce <cell> --lane L --manifest M [-- command...]`
pub fn produce(args: &[String]) -> Result<u8, String> {
    let options = parse(
        args,
        &["--lane", "--manifest", "--profile", "--registry", "--guard"],
        &[],
        &[],
    )?;
    let [cell_id] = options.positional.as_slice() else {
        return Err(usage("produce needs <cell>"));
    };
    let lane = options.values.get("--lane").ok_or_else(|| {
        usage("--lane is required (the lane whose worktree is at the candidate commit)")
    })?;
    gate::word(lane, "lane")?;
    let manifest = options
        .values
        .get("--manifest")
        .ok_or_else(|| usage("--manifest is required"))?;
    let registry = registry_of(&options)?;
    let profile = load_profile(&options, &registry)?;
    let Some(cell) = profile.cells.iter().find(|c| c.id == *cell_id) else {
        return Err(fail(
            "produce_unknown_cell",
            format!("the profile {} has no cell {cell_id}", profile.name),
        ));
    };
    if cell.verify.kind != "gate" {
        return Err(fail(
            "produce_not_a_gate_cell",
            format!(
                "{cell_id} is a {} cell: write its record with `xtask release emit {} {}`",
                cell.verify.kind,
                cell.verify.kind,
                cell.verify.name.as_deref().unwrap_or(cell_id)
            ),
        ));
    }
    let argv = match &options.tail {
        Some(_) if cell.verify.argv_is_hint => {
            return Err(fail(
                "produce_tool_cell_runs_its_own_command",
                format!(
                    "{cell_id} is produced only by its own command: {:?}",
                    cell.hint
                ),
            ));
        }
        Some(tail) if !tail.is_empty() => tail.clone(),
        Some(_) => return Err(usage("nothing follows `--`")),
        None if cell.verify.argv_is_hint => {
            let loaded = Manifest::load(Path::new(manifest))?;
            let path =
                fs::canonicalize(manifest).map_err(|e| fail("manifest_unreadable", io_text(&e)))?;
            cell.hint
                .iter()
                .map(|h| match h.as_str() {
                    "<manifest>" => path.display().to_string(),
                    "<manifest_sha256>" => loaded.sha256.clone(),
                    "<bundle>" => loaded.bundle_dir.display().to_string(),
                    _ => h.clone(),
                })
                .collect()
        }
        None => cell.hint.clone(),
    };
    let lacks: Vec<&String> = cell
        .verify
        .argv_has
        .iter()
        .filter(|t| !argv.contains(t))
        .collect();
    let forbidden: Vec<&String> = cell
        .verify
        .argv_forbids
        .iter()
        .filter(|t| argv.contains(t))
        .collect();
    if !lacks.is_empty() || !forbidden.is_empty() {
        return Err(fail(
            "produce_argv_mismatch",
            format!(
                "the command would not satisfy cell {cell_id}: lacks {lacks:?}, has forbidden {forbidden:?}"
            ),
        ));
    }
    let guard = options
        .values
        .get("--guard")
        .map_or_else(|| registry.join("../tools/lane-guard.sh"), PathBuf::from);
    gate::run(&gate::Options {
        lane: lane.clone(),
        name: cell.id.clone(),
        kind: Some(if cell.verify.runs == release::Runs::Tests {
            gate::Kind::Test
        } else {
            gate::Kind::Check
        }),
        registry,
        guard,
        argv,
        candidate: Some(PathBuf::from(manifest)),
    })
}
