// SPDX-License-Identifier: AGPL-3.0-only
//! Fake-qualified package effect composition. No native adapter or live command
//! is supplied here; helpers must be Rust executables, never the candidate app.
use crate::{
    Fault, FaultKind, Host, Result,
    effects::{Command, Transport},
    package::{Entry, Finalizer, Package},
};
use budgets::Span;
use serde_json::Value;
use std::collections::BTreeMap;

fn safe_absolute(path: &str) -> bool {
    path.starts_with('/')
        && !path.contains(['\0', '\n', '\r', '\\'])
        && path[1..]
            .split('/')
            .all(|s| !s.is_empty() && s != "." && s != "..")
}
fn validate_input(input: &DeviceInput) -> Result<()> {
    if !safe_absolute(&input.path) || !input.path.ends_with("-DEVICE-TEST-NOT-FOR-RELEASE.zip") {
        return Err(Fault::new("device_zip_path_invalid", FaultKind::Input));
    }
    if !crate::package::version_ok(&input.version)
        || !crate::package::hex(&input.zip_sha256, 64)
        || !crate::package::hex(&input.exe_sha256, 64)
    {
        return Err(Fault::new("invalid_candidate_identity", FaultKind::Input));
    }
    Ok(())
}
pub trait FinalizerIo: Transport {
    fn read_finalized_tree(&mut self, root: &str) -> Result<BTreeMap<String, Entry>>;
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FinalizerFailure {
    pub rc: i32,
}
impl std::fmt::Display for FinalizerFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "finalizer_failed_rc{}", self.rc)
    }
}
pub struct CommandFinalizer<I> {
    pub io: I,
    pub helper: String,
    pub output: String,
    pub bound: Span,
    pub failure: Option<FinalizerFailure>,
}
impl<I: FinalizerIo> Finalizer for CommandFinalizer<I> {
    fn finalize(&mut self, p: &Package, version: &str) -> Result<BTreeMap<String, Entry>> {
        self.failure = None;
        let leaf = self.helper.rsplit('/').next().unwrap_or("");
        if !safe_absolute(&self.helper)
            || !safe_absolute(&self.output)
            || self.helper == p.executable_path
            || self.helper.contains(".app/")
            || leaf.starts_with("python")
            || matches!(leaf, "sh" | "bash" | "zsh" | "env")
            || self.helper.ends_with(".py")
            || self.bound == Span::ZERO
            || self.output == p.root
            || self.output.starts_with(&(p.root.clone() + "/"))
        {
            return Err(Fault::new("finalizer_arguments_invalid", FaultKind::Input));
        }
        let command = Command {
            kind: crate::effects::CommandKind::Local,
            program: self.helper.clone(),
            args: [
                "finalize",
                "--package-root",
                &p.root,
                "--source-sha",
                &p.source_sha,
                "--version",
                version,
                "--output",
                &self.output,
            ]
            .map(str::to_owned)
            .to_vec(),
        };
        let output = self.io.execute(&command, self.bound)?;
        if output.rc != 0 {
            self.failure = Some(FinalizerFailure { rc: output.rc });
            return Err(Fault::new("finalizer_failed", FaultKind::Observation));
        }
        self.io.read_finalized_tree(&self.output)
    }
}
/// Archive identity from the library parser. CLI launch remains disabled.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeviceInput {
    pub path: String,
    pub version: String,
    pub zip_sha256: String,
    pub exe_sha256: String,
}
pub fn parse_device(args: &[String]) -> Result<DeviceInput> {
    let invalid = || Fault::new("invalid_device_arguments", FaultKind::Input);
    if !args.len().is_multiple_of(2) {
        return Err(invalid());
    }
    let mut fields = BTreeMap::new();
    for pair in args.chunks_exact(2) {
        if !matches!(
            pair[0].as_str(),
            "--device-test-zip" | "--version" | "--zip-sha256" | "--exe-sha256"
        ) || pair[1].is_empty()
            || fields.insert(pair[0].as_str(), pair[1].clone()).is_some()
        {
            return Err(invalid());
        }
    }
    let input = DeviceInput {
        path: fields.remove("--device-test-zip").ok_or_else(invalid)?,
        version: fields.remove("--version").ok_or_else(invalid)?,
        zip_sha256: fields.remove("--zip-sha256").ok_or_else(invalid)?,
        exe_sha256: fields.remove("--exe-sha256").ok_or_else(invalid)?,
    };
    validate_input(&input)?;
    Ok(input)
}
pub trait StageIo {
    fn read_archive(&mut self, host: Host, path: &str) -> Result<Vec<u8>>;
    /// Must create exclusively; existing roots never become a fresh attempt.
    fn create_root(&mut self, host: Host, path: &str) -> Result<()>;
    fn copy_archive(&mut self, local: &str, remote: &str, bound: Span) -> Result<()>;
    /// Receives already validated bytes. An adapter must not reopen mutable input.
    fn unpack(&mut self, host: Host, bytes: &[u8], root: &str, bound: Span) -> Result<()>;
    fn readback_manifest(&mut self, host: Host, root: &str) -> Result<Value>;
}
#[derive(Clone, Debug, serde::Serialize)]
pub struct Prepared {
    pub owner_root: String,
    pub member_root: String,
    pub manifest: Value,
    pub candidate: crate::stranger::Candidate,
}
pub struct StageLimits {
    pub copy: Span,
    pub unpack: Span,
}
pub fn prepare<I: StageIo>(
    io: &mut I,
    input: &DeviceInput,
    root: &str,
    attempt: &str,
    limits: StageLimits,
) -> Result<Prepared> {
    validate_input(input)?;
    if !safe_absolute(root)
        || attempt.is_empty()
        || attempt.len() > 160
        || !attempt
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
        || limits.copy == Span::ZERO
        || limits.unpack == Span::ZERO
    {
        return Err(Fault::new("stage_arguments_invalid", FaultKind::Input));
    }
    let bytes = io.read_archive(Host::Owner, &input.path)?;
    let checked = crate::package::verify_device(
        &input.path,
        &bytes,
        &input.version,
        &input.zip_sha256,
        &input.exe_sha256,
    )?;
    let owner_root = format!("{root}/unpack-{attempt}");
    let member_root = owner_root.clone();
    let remote_archive = format!("{member_root}/device.zip");
    // An interrupted attempt leaves a spent root. Never overwrite or silently reuse it.
    io.create_root(Host::Owner, &owner_root)?;
    io.create_root(Host::Member, &member_root)?;
    io.copy_archive(&input.path, &remote_archive, limits.copy)?;
    let remote = io.read_archive(Host::Member, &remote_archive)?;
    let remote_manifest = crate::package::verify_device(
        &input.path,
        &remote,
        &input.version,
        &input.zip_sha256,
        &input.exe_sha256,
    )?;
    if remote_manifest != checked {
        return Err(Fault::new(
            "remote_package_mismatch",
            FaultKind::Observation,
        ));
    }
    for (host, archive, destination) in [
        (Host::Owner, bytes.as_slice(), &owner_root),
        (Host::Member, remote.as_slice(), &member_root),
    ] {
        io.unpack(host, archive, destination, limits.unpack)?;
        if io.readback_manifest(host, destination)? != checked {
            return Err(Fault::new(
                "staged_manifest_readback_mismatch",
                FaultKind::Observation,
            ));
        }
    }
    let source_sha = checked["source_sha"]
        .as_str()
        .ok_or(Fault::new(
            "device_manifest_source_invalid",
            FaultKind::Admission,
        ))?
        .to_owned();
    let mut manifest = checked;
    manifest["zip_sha256"] = input.zip_sha256.clone().into();
    Ok(Prepared {
        owner_root,
        member_root,
        manifest,
        candidate: crate::stranger::Candidate {
            version: input.version.clone(),
            source_sha,
            zip_sha256: input.zip_sha256.clone(),
            exe_sha256: input.exe_sha256.clone(),
        },
    })
}
/// Validate/stage before entering any stranger host effect. The receipt carries
/// exact package identity even when the admitted stranger flow later fails.
pub fn with_device<I: StageIo>(
    io: &mut I,
    input: &DeviceInput,
    root: &str,
    attempt: &str,
    limits: StageLimits,
    run: impl FnOnce(&Prepared) -> Result<Value>,
) -> Result<Value> {
    let prepared = prepare(io, input, root, attempt, limits)?;
    let mut result = match run(&prepared) {
        Ok(value) => value,
        Err(e) => {
            serde_json::json!({"stranger_path_passed":false,"error":e.code,"kind":e.kind,"incomplete":e.incomplete(),"outcome":if e.incomplete(){"incomplete"}else{"failed"}})
        }
    };
    if !result.is_object() {
        return Err(Fault::new("device_receipt_invalid", FaultKind::Observation));
    }
    for key in [
        "package_root",
        "source_sha",
        "exe_sha256",
        "zip_sha256",
        "pre_ready_device_test",
    ] {
        result[key] = prepared.manifest[key].clone();
    }
    result["release_qualified"] = Value::Bool(false);
    Ok(result)
}
