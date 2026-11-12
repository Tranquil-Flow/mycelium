// SPDX-License-Identifier: AGPL-3.0-only
//! `xtask release`: the resumable release state machine (CL-L12-01, CL-L12-07).
//!
//! The machine reads **records, never prose**: the evidence ledger (`evidence.jsonl`, the rows
//! `xtask gate` and the device runner append), the claim table (`CLAIMS.tsv`) and a profile that
//! turns each mandatory claim into machine-checkable *cells*. A candidate is ACCEPTED only when
//! every cell of every mandatory claim is green on that candidate's exact bytes: the exe digest
//! and the bundle digest are bound into each record (`candidate` object) and re-hashed from disk.
//!
//! States: `preflight -> package -> {cert, device} -> inventory -> set-aside -> proof ->
//! promotion`. Every run re-derives every state from the records (a state recorded `done` can
//! regress when a later record or the bytes change); the attempt root keeps the progress, an
//! append-only event log, the set-aside seal and the promotion receipt. Publication exists only
//! as a refusing stub that needs an owner token file; nothing here can reach a network.
use crate::gate;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

pub(crate) fn fail(code: &str, detail: impl std::fmt::Display) -> String {
    format!("RELEASE: {code}: {detail}")
}
/// Splits a `RELEASE: code: detail` string back into its parts.
fn split_error(error: &str) -> (String, String) {
    let body = error.strip_prefix("RELEASE: ").unwrap_or(error);
    match body.split_once(": ") {
        Some((code, detail)) => (code.to_string(), detail.to_string()),
        None => (body.to_string(), String::new()),
    }
}
fn is_hex(text: &str, length: usize) -> bool {
    text.len() == length
        && text
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
fn sha256_bytes(bytes: &[u8]) -> String {
    hex(&Sha256::digest(bytes))
}
pub(crate) fn io_text(error: &std::io::Error) -> String {
    format!("{:?}", error.kind())
}

// ------------------------------------------------------------------ states

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum State {
    Preflight,
    Package,
    Cert,
    Device,
    Inventory,
    SetAside,
    Proof,
    Promotion,
}
pub const STATES: [State; 8] = [
    State::Preflight,
    State::Package,
    State::Cert,
    State::Device,
    State::Inventory,
    State::SetAside,
    State::Proof,
    State::Promotion,
];
impl State {
    pub fn id(self) -> &'static str {
        match self {
            Self::Preflight => "preflight",
            Self::Package => "package",
            Self::Cert => "cert",
            Self::Device => "device",
            Self::Inventory => "inventory",
            Self::SetAside => "set-aside",
            Self::Proof => "proof",
            Self::Promotion => "promotion",
        }
    }
    pub fn parse(text: &str) -> Option<Self> {
        STATES.into_iter().find(|s| s.id() == text)
    }
    /// States that must be `done` before this one is evaluated.
    pub fn needs(self) -> &'static [State] {
        match self {
            Self::Preflight => &[],
            Self::Package => &[Self::Preflight],
            Self::Cert | Self::Device => &[Self::Package],
            Self::Inventory => &[Self::Cert, Self::Device],
            Self::SetAside => &[Self::Inventory],
            Self::Proof => &[Self::SetAside],
            Self::Promotion => &[Self::Proof],
        }
    }
    /// States whose evidence is a profile cell; the others are effects (seal, decision).
    fn takes_cells(self) -> bool {
        !matches!(self, Self::SetAside | Self::Promotion)
    }
}

// ------------------------------------------------------------------ candidate

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Candidate {
    pub source_commit: String,
    pub exe_sha256: String,
    pub bundle_sha256: String,
}
impl Candidate {
    pub fn to_json(&self) -> Value {
        json!({"source_commit": self.source_commit, "exe_sha256": self.exe_sha256,
               "bundle_sha256": self.bundle_sha256})
    }
}
#[derive(Debug, Clone)]
pub struct Manifest {
    pub candidate: Candidate,
    pub frozen_at: f64,
    pub exe_path: PathBuf,
    pub bundle_dir: PathBuf,
    /// sha256 of the manifest file bytes.
    pub sha256: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawManifest {
    schema: u64,
    candidate: Candidate,
    frozen_at: f64,
    exe_path: String,
    bundle_dir: String,
}
impl Manifest {
    /// Relative paths are resolved against `base` (the manifest's directory).
    pub fn parse(text: &str, base: &Path) -> Result<Self, String> {
        let raw: RawManifest =
            serde_json::from_str(text).map_err(|e| fail("manifest_invalid", e))?;
        if raw.schema != 1 {
            return Err(fail("manifest_invalid", "schema must be 1"));
        }
        if !raw.frozen_at.is_finite() || raw.frozen_at < 0.0 {
            return Err(fail(
                "manifest_invalid",
                "frozen_at must be epoch seconds >= 0",
            ));
        }
        if !is_hex(&raw.candidate.source_commit, 40) {
            return Err(fail(
                "candidate_source_invalid",
                "source_commit is 40 lower-case hex",
            ));
        }
        for (name, digest) in [
            ("exe_sha256", &raw.candidate.exe_sha256),
            ("bundle_sha256", &raw.candidate.bundle_sha256),
        ] {
            if !is_hex(digest, 64) {
                return Err(fail(
                    "candidate_digest_invalid",
                    format!("{name} is 64 lower-case hex"),
                ));
            }
        }
        if raw.exe_path.is_empty() || raw.bundle_dir.is_empty() {
            return Err(fail(
                "manifest_path_invalid",
                "exe_path and bundle_dir are required",
            ));
        }
        Ok(Self {
            candidate: raw.candidate,
            frozen_at: raw.frozen_at,
            exe_path: base.join(&raw.exe_path),
            bundle_dir: base.join(&raw.bundle_dir),
            sha256: sha256_bytes(text.as_bytes()),
        })
    }
    pub fn load(path: &Path) -> Result<Self, String> {
        let text = fs::read_to_string(path).map_err(|e| {
            fail(
                "manifest_unreadable",
                format!("{}: {}", path.display(), io_text(&e)),
            )
        })?;
        let base = path.parent().unwrap_or(Path::new("."));
        Self::parse(&text, base)
    }
    /// Re-hash the exe and the bundle from disk and compare with the frozen digests.
    pub fn verify_bytes(&self) -> Result<(), String> {
        let exe = file_sha256(&self.exe_path).map_err(|e| {
            fail(
                "package_exe_unreadable",
                format!("{}: {e}", self.exe_path.display()),
            )
        })?;
        if exe != self.candidate.exe_sha256 {
            return Err(fail(
                "package_exe_mismatch",
                format!(
                    "{} hashes to {exe}, the candidate froze {}",
                    self.exe_path.display(),
                    self.candidate.exe_sha256
                ),
            ));
        }
        let bundle = bundle_digest(&self.bundle_dir).map_err(|e| {
            fail(
                "package_bundle_unreadable",
                format!("{}: {e}", self.bundle_dir.display()),
            )
        })?;
        if bundle != self.candidate.bundle_sha256 {
            return Err(fail(
                "package_bundle_mismatch",
                format!(
                    "{} lists as {bundle}, the candidate froze {}",
                    self.bundle_dir.display(),
                    self.candidate.bundle_sha256
                ),
            ));
        }
        Ok(())
    }
}

/// Streams the file through sha256; the error is an IO kind, callers add the code.
pub fn file_sha256(path: &Path) -> Result<String, String> {
    let mut file = File::open(path).map_err(|e| io_text(&e))?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; 1 << 16];
    loop {
        let read = file.read(&mut buffer).map_err(|e| io_text(&e))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(hex(&hasher.finalize()))
}
/// sha256 of the sorted listing `"<sha256>  <size>  <relpath>\n"` of every regular file below
/// `dir`. A symlink, device, fifo or socket anywhere below `dir` is refused.
pub fn bundle_digest(dir: &Path) -> Result<String, String> {
    let mut entries = BTreeMap::new();
    walk(dir, dir, &mut entries)?;
    if entries.is_empty() {
        return Err(fail("bundle_empty", dir.display()));
    }
    let listing: String = entries
        .iter()
        .map(|(relative, (digest, size))| format!("{digest}  {size}  {relative}\n"))
        .collect();
    Ok(sha256_bytes(listing.as_bytes()))
}
fn walk(root: &Path, dir: &Path, out: &mut BTreeMap<String, (String, u64)>) -> Result<(), String> {
    let listing = fs::read_dir(dir).map_err(|e| {
        fail(
            "bundle_unreadable",
            format!("{}: {}", dir.display(), io_text(&e)),
        )
    })?;
    for entry in listing {
        let entry = entry.map_err(|e| fail("bundle_unreadable", io_text(&e)))?;
        let path = entry.path();
        let meta = fs::symlink_metadata(&path).map_err(|e| {
            fail(
                "bundle_unreadable",
                format!("{}: {}", path.display(), io_text(&e)),
            )
        })?;
        if meta.is_dir() {
            walk(root, &path, out)?;
        } else if meta.is_file() {
            let relative = relative_name(root, &path)?;
            let digest = file_sha256(&path)
                .map_err(|e| fail("bundle_unreadable", format!("{}: {e}", path.display())))?;
            out.insert(relative, (digest, meta.len()));
        } else if meta.file_type().is_symlink() {
            // v0.1.4's signable layout keeps relative links inside the bundle
            // (`Frameworks/mlx.metallib -> ../Resources/mlx.metallib`). A link is never followed:
            // its target text is part of the frozen bytes, and a link out of the bundle is refused.
            let relative = relative_name(root, &path)?;
            let target = fs::read_link(&path).map_err(|e| {
                fail(
                    "bundle_unreadable",
                    format!("{}: {}", path.display(), io_text(&e)),
                )
            })?;
            let text = target
                .to_str()
                .ok_or_else(|| fail("bundle_unreadable", "non-UTF-8 link target"))?;
            if !link_stays_inside(&relative, &target) || !resolves_inside(root, &path) {
                return Err(fail(
                    "bundle_link_outside",
                    format!("{} -> {text}", path.display()),
                ));
            }
            out.insert(
                relative,
                (
                    format!("link:{}", sha256_bytes(text.as_bytes())),
                    text.len() as u64,
                ),
            );
        } else {
            return Err(fail("bundle_special_file", path.display()));
        }
    }
    Ok(())
}
fn relative_name(root: &Path, path: &Path) -> Result<String, String> {
    Ok(path
        .strip_prefix(root)
        .map_err(|e| fail("bundle_unreadable", e))?
        .to_str()
        .ok_or_else(|| fail("bundle_unreadable", "non-UTF-8 path"))?
        .replace('\\', "/"))
}
/// Whether the link at `link` resolves inside `root` on disk, following the links its target
/// passes through (lane RELF item 7, review F5: `esc -> up/../..` with `up -> ..` is lexically
/// inside but leaves the bundle). Nothing is read through a link; a missing component is taken as
/// named, so a dangling chain that would land outside is refused too. At most 40 hops.
fn resolves_inside(root: &Path, link: &Path) -> bool {
    use std::path::Component;
    fn resolve(root: &Path, mut at: PathBuf, target: &Path, hops: &mut u32) -> Option<PathBuf> {
        if target.is_absolute() {
            return None;
        }
        for component in target.components() {
            match component {
                Component::CurDir => {}
                Component::ParentDir => {
                    at.pop();
                }
                Component::Normal(name) => {
                    let next = at.join(name);
                    let is_link =
                        fs::symlink_metadata(&next).is_ok_and(|m| m.file_type().is_symlink());
                    if is_link {
                        *hops += 1;
                        if *hops > 40 {
                            return None;
                        }
                        let inner = fs::read_link(&next).ok()?;
                        at = resolve(root, at, &inner, hops)?;
                    } else {
                        at = next;
                    }
                }
                Component::RootDir | Component::Prefix(_) => return None,
            }
            if !at.starts_with(root) {
                return None;
            }
        }
        Some(at)
    }
    let (Ok(root), Some(parent)) = (fs::canonicalize(root), link.parent()) else {
        return false;
    };
    let (Ok(start), Ok(target)) = (fs::canonicalize(parent), fs::read_link(link)) else {
        return false;
    };
    let mut hops = 0;
    resolve(&root, start, &target, &mut hops).is_some()
}
/// Whether `target`, read relative to the folder holding the link at `relative` (a path inside
/// the bundle), names a path inside the bundle. Lexical only: nothing is resolved on disk.
fn link_stays_inside(relative: &str, target: &Path) -> bool {
    use std::path::Component;
    if target.is_absolute() {
        return false;
    }
    let mut depth = relative.split('/').count().saturating_sub(1);
    for component in target.components() {
        match component {
            Component::ParentDir => match depth.checked_sub(1) {
                Some(up) => depth = up,
                None => return false,
            },
            Component::Normal(_) => depth += 1,
            Component::CurDir => {}
            Component::RootDir | Component::Prefix(_) => return false,
        }
    }
    true
}

// ------------------------------------------------------------------ claims, ledger, profile

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Claim {
    pub id: String,
    pub lane: String,
    pub status: String,
    pub critical: bool,
    pub blocked_by: String,
}
#[derive(Debug, Clone, Default)]
pub struct Claims {
    pub rows: Vec<Claim>,
    pub sha256: String,
}
impl Claims {
    pub fn parse(tsv: &str) -> Result<Self, String> {
        let mut lines = tsv.lines().filter(|l| !l.trim().is_empty());
        let header: Vec<&str> = lines.next().unwrap_or("").split('\t').collect();
        let column = |name: &str| {
            header
                .iter()
                .position(|h| *h == name)
                .ok_or_else(|| fail("claims_header_invalid", format!("no `{name}` column")))
        };
        let (id, lane, status, critical, blocked) = (
            column("id")?,
            column("lane")?,
            column("status")?,
            column("critical")?,
            column("blocked_by")?,
        );
        let mut rows: Vec<Claim> = Vec::new();
        for (number, line) in lines.enumerate() {
            let fields: Vec<&str> = line.split('\t').collect();
            let field = |index: usize| {
                fields.get(index).copied().ok_or_else(|| {
                    fail(
                        "claims_row_invalid",
                        format!("row {}: too few columns", number + 2),
                    )
                })
            };
            let claim = Claim {
                id: field(id)?.to_string(),
                lane: field(lane)?.to_string(),
                status: field(status)?.to_string(),
                critical: match field(critical)? {
                    "yes" => true,
                    "no" => false,
                    other => {
                        return Err(fail(
                            "claims_row_invalid",
                            format!("{}: critical is yes|no, got {other}", field(id)?),
                        ));
                    }
                },
                blocked_by: field(blocked)?.to_string(),
            };
            if !["met", "partial", "open", "blocked"].contains(&claim.status.as_str()) {
                return Err(fail(
                    "claims_status_invalid",
                    format!("{}: {}", claim.id, claim.status),
                ));
            }
            if rows.iter().any(|c| c.id == claim.id) {
                return Err(fail("claims_duplicate_id", &claim.id));
            }
            rows.push(claim);
        }
        Ok(Self {
            rows,
            sha256: sha256_bytes(tsv.as_bytes()),
        })
    }
    pub fn get(&self, id: &str) -> Option<&Claim> {
        self.rows.iter().find(|c| c.id == id)
    }
}

#[derive(Debug, Clone)]
pub struct Record {
    pub id: String,
    pub kind: String,
    pub at: f64,
    pub value: Value,
}
#[derive(Debug, Clone, Default)]
pub struct Ledger {
    pub records: Vec<Record>,
    pub sha256: String,
}
impl Ledger {
    pub fn parse(text: &str) -> Result<Self, String> {
        let mut records: Vec<Record> = Vec::new();
        let mut ids = BTreeSet::new();
        for (index, line) in text.lines().enumerate() {
            if line.trim().is_empty() {
                continue;
            }
            let at = format!("evidence.jsonl:{}", index + 1);
            let value: Value = serde_json::from_str(line)
                .map_err(|e| fail("ledger_line_invalid", format!("{at}: {e}")))?;
            let text_of = |key: &str| {
                value
                    .get(key)
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
            };
            let (Some(id), Some(kind)) = (text_of("id"), text_of("kind")) else {
                return Err(fail(
                    "ledger_line_invalid",
                    format!("{at}: a record needs a string id and kind"),
                ));
            };
            if !ids.insert(id.to_string()) {
                return Err(fail("ledger_duplicate_id", format!("{at}: {id}")));
            }
            records.push(Record {
                id: id.to_string(),
                kind: kind.to_string(),
                at: value.get("at").and_then(Value::as_f64).unwrap_or(0.0),
                value,
            });
        }
        Ok(Self {
            records,
            sha256: sha256_bytes(text.as_bytes()),
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Bind {
    /// The record's `candidate` object equals the manifest candidate (source, exe, bundle).
    Bytes,
    /// Source-only checks (fmt, lint): the record's `commit` equals the candidate source commit.
    Source,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Runs {
    /// A gate that must have executed tests (never fewer than one passed).
    Tests,
    /// A gate decided by its exit status (fmt, clippy, lint, build).
    Check,
    /// A typed receipt record (device runner): verdict PASS, no counts.
    Receipt,
}
#[derive(Debug, Clone)]
pub struct Verify {
    pub kind: String,
    pub lane: Option<String>,
    pub name: Option<String>,
    pub argv_has: Vec<String>,
    pub argv_forbids: Vec<String>,
    pub runs: Runs,
    pub min_passed: u64,
    pub required_targets: Vec<String>,
    /// A tool cell (lane RELF item 4): the record's argv must BE the cell's hint for this
    /// candidate (`<manifest>` any path, `<manifest_sha256>` this manifest's sha256, `<bundle>` its
    /// `bundle_dir`), and a hint naming `<manifest_sha256>` needs the tool's
    /// `: PASS manifest_sha256 <sha256>` line in the log. `produce` runs only that command.
    pub argv_is_hint: bool,
    /// An operator's receipt (the launch smoke): labelled so, and one of its artifact files must
    /// name this candidate (manifest sha256 and exe sha256), so a receipt written for another
    /// candidate never turns the cell green.
    pub operator_receipt: bool,
}
#[derive(Debug, Clone)]
pub struct Cell {
    pub id: String,
    pub state: State,
    pub claim: String,
    pub bind: Bind,
    pub verify: Verify,
    pub hint: Vec<String>,
}
#[derive(Debug, Clone)]
pub struct Profile {
    pub name: String,
    pub sha256: String,
    pub mandatory_claims: Vec<String>,
    pub cells: Vec<Cell>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawProfile {
    schema: u64,
    name: String,
    require_critical_lanes: Vec<String>,
    mandatory_claims: Vec<String>,
    cells: Vec<RawCell>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawCell {
    id: String,
    state: String,
    claim: String,
    bind: String,
    verify: RawVerify,
    #[serde(default)]
    hint: Vec<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawVerify {
    runs: String,
    kind: Option<String>,
    lane: Option<String>,
    name: Option<String>,
    #[serde(default)]
    argv_has: Vec<String>,
    #[serde(default)]
    argv_forbids: Vec<String>,
    min_passed: Option<u64>,
    #[serde(default)]
    required_targets: Vec<String>,
    #[serde(default)]
    argv_is_hint: bool,
    #[serde(default)]
    operator_receipt: bool,
}
impl Profile {
    pub fn parse(text: &str, claims: &Claims) -> Result<Self, String> {
        let raw: RawProfile = serde_json::from_str(text).map_err(|e| fail("profile_invalid", e))?;
        if raw.schema != 1 {
            return Err(fail("profile_invalid", "schema must be 1"));
        }
        let mut cells = Vec::new();
        for cell in raw.cells {
            cells.push(Self::cell(cell)?);
        }
        let mut seen = BTreeSet::new();
        for cell in &cells {
            if !seen.insert(cell.id.clone()) {
                return Err(fail("profile_duplicate_cell", &cell.id));
            }
        }
        let mut mandatory = BTreeSet::new();
        for claim in &raw.mandatory_claims {
            if !mandatory.insert(claim.clone()) {
                return Err(fail("profile_duplicate_claim", claim));
            }
        }
        for id in raw
            .mandatory_claims
            .iter()
            .chain(cells.iter().map(|c| &c.claim))
        {
            if claims.get(id).is_none() {
                return Err(fail("profile_claim_unknown", id));
            }
        }
        let omitted: Vec<&str> = claims
            .rows
            .iter()
            .filter(|c| c.critical && raw.require_critical_lanes.contains(&c.lane))
            .filter(|c| !mandatory.contains(&c.id))
            .map(|c| c.id.as_str())
            .collect();
        if !omitted.is_empty() {
            return Err(fail(
                "critical_claim_not_mandatory",
                format!(
                    "critical claims of lanes {:?} left out of the profile: {}",
                    raw.require_critical_lanes,
                    omitted.join(", ")
                ),
            ));
        }
        let uncovered: Vec<&str> = raw
            .mandatory_claims
            .iter()
            .filter(|m| !cells.iter().any(|c| &c.claim == *m))
            .map(String::as_str)
            .collect();
        if !uncovered.is_empty() {
            return Err(fail("mandatory_claim_without_cell", uncovered.join(", ")));
        }
        if let Some(cell) = cells.iter().find(|c| !mandatory.contains(&c.claim)) {
            return Err(fail(
                "cell_claim_not_mandatory",
                format!("{} -> {}", cell.id, cell.claim),
            ));
        }
        Ok(Self {
            name: raw.name,
            sha256: sha256_bytes(text.as_bytes()),
            mandatory_claims: raw.mandatory_claims,
            cells,
        })
    }
    fn cell(raw: RawCell) -> Result<Cell, String> {
        let state = State::parse(&raw.state).ok_or_else(|| {
            fail(
                "profile_invalid",
                format!("{}: unknown state {}", raw.id, raw.state),
            )
        })?;
        if !state.takes_cells() {
            return Err(fail(
                "profile_cell_state_invalid",
                format!("{}: {} takes no cells", raw.id, state.id()),
            ));
        }
        let bind = match raw.bind.as_str() {
            "bytes" => Bind::Bytes,
            "source" => Bind::Source,
            other => return Err(fail("profile_invalid", format!("{}: bind {other}", raw.id))),
        };
        let v = raw.verify;
        let runs = match v.runs.as_str() {
            "tests" => Runs::Tests,
            "check" => Runs::Check,
            "receipt" => Runs::Receipt,
            other => return Err(fail("profile_invalid", format!("{}: runs {other}", raw.id))),
        };
        if runs == Runs::Tests && v.min_passed.unwrap_or(0) == 0 {
            return Err(fail(
                "profile_tests_cell_allows_zero",
                format!("{}: a tests cell needs min_passed >= 1", raw.id),
            ));
        }
        if runs != Runs::Tests && (v.min_passed.is_some() || !v.required_targets.is_empty()) {
            return Err(fail(
                "profile_invalid",
                format!("{}: counts only apply to tests cells", raw.id),
            ));
        }
        if v.argv_is_hint && (runs == Runs::Receipt || raw.hint.is_empty()) {
            return Err(fail(
                "profile_invalid",
                format!("{}: argv_is_hint needs a gate cell with a hint", raw.id),
            ));
        }
        if v.operator_receipt && runs != Runs::Receipt {
            return Err(fail(
                "profile_invalid",
                format!("{}: operator_receipt is for receipt cells", raw.id),
            ));
        }
        if v.argv_is_hint && v.kind.as_deref().is_some_and(|k| k != "gate") {
            return Err(fail(
                "profile_invalid",
                format!("{}: argv_is_hint is for gate records", raw.id),
            ));
        }
        if v.argv_has.is_empty() && v.name.is_none() {
            return Err(fail(
                "profile_verify_unselective",
                format!("{}: name the record (name or argv_has)", raw.id),
            ));
        }
        Ok(Cell {
            id: raw.id,
            state,
            claim: raw.claim,
            bind,
            verify: Verify {
                kind: v.kind.unwrap_or_else(|| "gate".to_string()),
                lane: v.lane,
                name: v.name,
                argv_has: v.argv_has,
                argv_forbids: v.argv_forbids,
                runs,
                min_passed: v.min_passed.unwrap_or(0),
                required_targets: v.required_targets,
                argv_is_hint: v.argv_is_hint,
                operator_receipt: v.operator_receipt,
            },
            hint: raw.hint,
        })
    }
}

// ------------------------------------------------------------------ loading

#[derive(Debug, Clone)]
pub struct Inputs {
    pub manifest: PathBuf,
    pub profile: PathBuf,
    /// Directory holding `evidence.jsonl` and `CLAIMS.tsv`; relative record logs resolve here.
    pub registry: PathBuf,
    /// Attempt roots live below this directory, one per attempt id.
    pub attempts: PathBuf,
}
#[derive(Debug, Clone)]
pub struct Loaded {
    pub manifest: Manifest,
    pub claims: Claims,
    pub profile: Profile,
    pub ledger: Ledger,
    pub registry: PathBuf,
}
fn read(path: &Path, code: &str) -> Result<String, String> {
    fs::read_to_string(path).map_err(|e| fail(code, format!("{}: {}", path.display(), io_text(&e))))
}
/// Reads the manifest, claim table, profile and ledger named by `inputs`.
pub fn load(inputs: &Inputs) -> Result<Loaded, String> {
    let manifest = Manifest::load(&inputs.manifest)?;
    let claims = Claims::parse(&read(
        &inputs.registry.join("CLAIMS.tsv"),
        "claims_unreadable",
    )?)?;
    let profile = Profile::parse(&read(&inputs.profile, "profile_unreadable")?, &claims)?;
    let ledger = Ledger::parse(&read(
        &inputs.registry.join("evidence.jsonl"),
        "ledger_unreadable",
    )?)?;
    Ok(Loaded {
        manifest,
        claims,
        profile,
        ledger,
        registry: inputs.registry.clone(),
    })
}
impl Loaded {
    pub fn env(&self) -> Env<'_> {
        Env {
            manifest: &self.manifest,
            profile: &self.profile,
            ledger: &self.ledger,
            claims: &self.claims,
            log_base: &self.registry,
        }
    }
}

// ------------------------------------------------------------------ evaluation

#[derive(Debug, Clone, PartialEq)]
pub enum Outcome {
    Green {
        ev: String,
        passed: u64,
        log_sha256: Option<String>,
    },
    Red {
        code: &'static str,
        evs: Vec<String>,
        detail: String,
    },
}
#[derive(Debug, Clone, PartialEq)]
pub struct CellResult {
    pub cell: String,
    pub state: State,
    pub claim: String,
    pub outcome: Outcome,
}
pub struct Env<'a> {
    pub manifest: &'a Manifest,
    pub profile: &'a Profile,
    pub ledger: &'a Ledger,
    pub claims: &'a Claims,
    /// Relative `log` paths in records resolve against this directory (the registry).
    pub log_base: &'a Path,
}

fn red(code: &'static str, evs: Vec<String>, detail: impl Into<String>) -> Outcome {
    Outcome::Red {
        code,
        evs,
        detail: detail.into(),
    }
}
fn strings(value: &Value, key: &str) -> Option<Vec<String>> {
    value
        .get(key)?
        .as_array()?
        .iter()
        .map(|v| v.as_str().map(str::to_string))
        .collect()
}
fn selects(record: &Record, v: &Verify) -> bool {
    if record.kind != v.kind {
        return false;
    }
    let text = |key: &str| record.value.get(key).and_then(Value::as_str);
    if v.lane
        .as_deref()
        .is_some_and(|lane| text("lane") != Some(lane))
        || v.name
            .as_deref()
            .is_some_and(|name| text("name") != Some(name))
    {
        return false;
    }
    let argv = strings(&record.value, "argv").unwrap_or_default();
    v.argv_has.iter().all(|t| argv.contains(t)) && !v.argv_forbids.iter().any(|t| argv.contains(t))
}
enum Binding {
    Bound,
    Differs(Vec<&'static str>),
    Unbound,
}
fn binding(record: &Record, cell: &Cell, candidate: &Candidate) -> Binding {
    match cell.bind {
        Bind::Source => {
            if record.value.get("commit").and_then(Value::as_str)
                == Some(candidate.source_commit.as_str())
            {
                Binding::Bound
            } else {
                Binding::Differs(vec!["commit"])
            }
        }
        Bind::Bytes => {
            let Some(bound) = record.value.get("candidate") else {
                return Binding::Unbound;
            };
            let differs: Vec<&'static str> = [
                ("source_commit", &candidate.source_commit),
                ("exe_sha256", &candidate.exe_sha256),
                ("bundle_sha256", &candidate.bundle_sha256),
            ]
            .into_iter()
            .filter(|(key, want)| bound.get(*key).and_then(Value::as_str) != Some(want.as_str()))
            .map(|(key, _)| key)
            .collect();
            if differs.is_empty() {
                Binding::Bound
            } else {
                Binding::Differs(differs)
            }
        }
    }
}
fn hint_text(cell: &Cell) -> String {
    let command = cell.hint.join(" ");
    if cell.verify.kind == "gate" {
        format!("run: xtask gate <LANE> <NAME> --candidate <manifest> -- {command}")
    } else {
        format!("produce a `{}` record: {command}", cell.verify.kind)
    }
}
fn selector_text(v: &Verify) -> String {
    format!(
        "kind {} name {:?} argv has {:?} lacks {:?}",
        v.kind, v.name, v.argv_has, v.argv_forbids
    )
}

pub fn evaluate_cell(env: &Env, cell: &Cell) -> CellResult {
    CellResult {
        cell: cell.id.clone(),
        state: cell.state,
        claim: cell.claim.clone(),
        outcome: decide(env, cell),
    }
}
pub fn evaluate_all(env: &Env) -> Vec<CellResult> {
    env.profile
        .cells
        .iter()
        .map(|c| evaluate_cell(env, c))
        .collect()
}

fn is_not_yet(record: &Record) -> bool {
    record.value.get("verdict").and_then(Value::as_str) == Some("NOT_YET")
}
/// An explicit NOT_YET statement decides the cell when it is the latest record that selects the
/// cell (ledger order breaks a tie in `at`): the producer says it cannot produce the evidence yet,
/// and says why. A later PASS or gate record supersedes it; a later NOT_YET supersedes a PASS.
fn latest_not_yet(matching: &[&Record]) -> Option<Outcome> {
    let rank = |index: usize, record: &Record| (record.at, index);
    let newest_other = matching
        .iter()
        .enumerate()
        .filter(|(_, r)| !is_not_yet(r))
        .map(|(i, r)| rank(i, r))
        .fold(None::<(f64, usize)>, |best, now| match best {
            Some(b) if b >= now => Some(b),
            _ => Some(now),
        });
    let (index, record) = matching
        .iter()
        .enumerate()
        .filter(|(_, r)| is_not_yet(r))
        .max_by(|(ia, a), (ib, b)| {
            rank(*ia, a)
                .partial_cmp(&rank(*ib, b))
                .unwrap_or(std::cmp::Ordering::Equal)
        })?;
    if newest_other.is_some_and(|other| other >= rank(index, record)) {
        return None;
    }
    let text = |key: &str| record.value.get(key).and_then(Value::as_str).unwrap_or("");
    let blockers = strings(&record.value, "blockers").unwrap_or_default();
    let mut detail = format!("not yet producible: {}", text("reason"));
    if !text("producer").is_empty() {
        detail.push_str(&format!("; producer: {}", text("producer")));
    }
    if !blockers.is_empty() {
        detail.push_str(&format!("; blocked by {}", blockers.join(", ")));
    }
    Some(red("cell_not_yet", vec![record.id.clone()], detail))
}

fn decide(env: &Env, cell: &Cell) -> Outcome {
    let candidate = &env.manifest.candidate;
    let matching: Vec<&Record> = env
        .ledger
        .records
        .iter()
        .filter(|r| selects(r, &cell.verify))
        .collect();
    if let Some(not_yet) = latest_not_yet(&matching) {
        return not_yet;
    }
    let matching: Vec<&Record> = matching.into_iter().filter(|r| !is_not_yet(r)).collect();
    if matching.is_empty() {
        return red(
            "cell_missing",
            vec![],
            format!(
                "no record ({}); {}",
                selector_text(&cell.verify),
                hint_text(cell)
            ),
        );
    }
    let mut bound = Vec::new();
    let mut differing: Vec<(&Record, Vec<&'static str>)> = Vec::new();
    for record in matching {
        match binding(record, cell, candidate) {
            Binding::Bound => bound.push(record),
            Binding::Differs(fields) => differing.push((record, fields)),
            Binding::Unbound => {}
        }
    }
    if bound.is_empty() {
        if differing.is_empty() {
            return red(
                "cell_unbound",
                vec![],
                format!(
                    "matching records carry no candidate binding; {}",
                    hint_text(cell)
                ),
            );
        }
        let evs: Vec<String> = differing.iter().map(|(r, _)| r.id.clone()).collect();
        if cell.bind == Bind::Source {
            return red(
                "cell_wrong_commit",
                evs,
                format!(
                    "records are for another commit; the candidate source_commit is {}",
                    candidate.source_commit
                ),
            );
        }
        let detail = differing
            .iter()
            .map(|(r, fields)| format!("{} differs in {}", r.id, fields.join(", ")))
            .collect::<Vec<_>>()
            .join("; ");
        return red(
            "cell_wrong_digest",
            evs,
            format!("records are bound to other bytes: {detail}"),
        );
    }
    let fresh: Vec<&Record> = bound
        .iter()
        .copied()
        .filter(|r| r.at >= env.manifest.frozen_at)
        .collect();
    let Some(latest) = fresh
        .iter()
        .copied()
        .reduce(|a, b| if b.at >= a.at { b } else { a })
    else {
        return red(
            "cell_stale",
            bound.iter().map(|r| r.id.clone()).collect(),
            format!(
                "every bound record predates the candidate freeze at {}",
                env.manifest.frozen_at
            ),
        );
    };
    judge(env, cell, latest)
}

type PerTarget = Vec<(u64, u64, u64)>;

fn judge(env: &Env, cell: &Cell, record: &Record) -> Outcome {
    let id = || vec![record.id.clone()];
    let v = &cell.verify;
    let value = &record.value;
    let verdict = value.get("verdict").and_then(Value::as_str).unwrap_or("");
    let exit = value.get("exit").and_then(Value::as_i64);
    if verdict.is_empty() {
        return red("record_inconsistent", id(), "no verdict");
    }
    if verdict != "PASS" {
        let executed_nothing = ["ZERO-TESTS", "NO-TEST-OUTPUT", "RAN-NOTHING"].contains(&verdict);
        if executed_nothing && v.runs == Runs::Tests {
            return red(
                "cell_zero_tests",
                id(),
                format!("verdict {verdict}: no test executed"),
            );
        }
        return red(
            "cell_not_green",
            id(),
            format!("verdict {verdict}, exit {exit:?}"),
        );
    }
    if exit.is_some_and(|code| code != 0) {
        return red(
            "record_inconsistent",
            id(),
            format!("verdict PASS with exit {exit:?}"),
        );
    }
    let mut passed = 0;
    let mut expected: Option<PerTarget> = None;
    if v.runs != Runs::Receipt {
        let total = value.get("total");
        let count = |key: &str| total.and_then(|t| t.get(key)).and_then(Value::as_u64);
        let (Some(p), Some(f), Some(i)) = (count("passed"), count("failed"), count("ignored"))
        else {
            return red(
                "record_inconsistent",
                id(),
                "no total.{passed,failed,ignored}",
            );
        };
        let targets = value
            .get("targets")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let sum = |key: &str| -> u64 {
            targets
                .iter()
                .filter_map(|t| t.get(key).and_then(Value::as_u64))
                .sum()
        };
        if (sum("passed"), sum("failed"), sum("ignored")) != (p, f, i) {
            return red(
                "record_inconsistent",
                id(),
                "per-target counts do not add up to the totals",
            );
        }
        if f != 0
            || !strings(value, "failed_tests").is_some_and(|names| names.is_empty())
            || targets
                .iter()
                .any(|t| t.get("ok").and_then(Value::as_bool) != Some(true))
        {
            return red(
                "record_inconsistent",
                id(),
                "verdict PASS with failures recorded",
            );
        }
        passed = p;
        let number = |t: &Value, key: &str| t.get(key).and_then(Value::as_u64).unwrap_or(0);
        expected = Some(
            targets
                .iter()
                .map(|t| {
                    (
                        number(t, "passed"),
                        number(t, "failed"),
                        number(t, "ignored"),
                    )
                })
                .collect(),
        );
        if v.runs == Runs::Tests {
            if p == 0 {
                return red(
                    "cell_zero_tests",
                    id(),
                    "verdict PASS but zero tests passed",
                );
            }
            if p < v.min_passed {
                return red(
                    "cell_too_few_tests",
                    id(),
                    format!("{p} passed, the cell requires {}", v.min_passed),
                );
            }
            let missing: Vec<&str> = v
                .required_targets
                .iter()
                .filter(|want| {
                    !targets.iter().any(|t| {
                        let label = t.get("target").and_then(Value::as_str).unwrap_or("");
                        let label = label.split(" (").next().unwrap_or(label);
                        (label == want.as_str() || label.starts_with(&format!("{want} ")))
                            && number(t, "passed") > 0
                    })
                })
                .map(String::as_str)
                .collect();
            if !missing.is_empty() {
                return red(
                    "cell_target_missing",
                    id(),
                    format!("no passing test in target(s): {}", missing.join(", ")),
                );
            }
        }
    }
    let log_sha256 = match check_log(env, record, v.runs, exit, expected) {
        Ok(sha) => sha,
        Err((code, detail)) => return red(code, id(), detail),
    };
    if v.argv_is_hint
        && let Err((code, detail)) = check_tool_run(env, cell, record)
    {
        return red(code, id(), detail);
    }
    if v.operator_receipt
        && let Err(detail) = names_candidate(env.manifest, record)
    {
        return red("operator_receipt_unbound", id(), detail);
    }
    if v.runs == Runs::Receipt
        && let Err((code, detail)) = receipt_states_pass(env, v, record)
    {
        return red(code, id(), detail);
    }
    Outcome::Green {
        ev: record.id.clone(),
        passed,
        log_sha256,
    }
}

/// The record's argv instantiated from the cell's hint for this manifest.
pub fn hint_matches(hint: &[String], argv: &[String], manifest: &Manifest) -> bool {
    hint.len() == argv.len()
        && hint.iter().zip(argv).all(|(h, a)| match h.as_str() {
            "<manifest>" => !a.is_empty(),
            "<manifest_sha256>" => *a == manifest.sha256,
            "<bundle>" => Path::new(a) == manifest.bundle_dir,
            _ => h == a,
        })
}
/// A tool cell's record: its argv is the cell's own command for this manifest, and (when the
/// command names the manifest's sha256) its log carries the tool's PASS line for that sha256.
fn check_tool_run(env: &Env, cell: &Cell, record: &Record) -> Result<(), (&'static str, String)> {
    let argv = strings(&record.value, "argv").unwrap_or_default();
    if !hint_matches(&cell.hint, &argv, env.manifest) {
        return Err((
            "tool_argv_not_hint",
            format!(
                "the record ran {argv:?}, not the cell's own command {:?} for this manifest",
                cell.hint
            ),
        ));
    }
    if !cell.hint.iter().any(|h| h == "<manifest_sha256>") {
        return Ok(());
    }
    let name = record
        .value
        .get("log")
        .and_then(Value::as_str)
        .unwrap_or("");
    let path = if Path::new(name).is_absolute() {
        PathBuf::from(name)
    } else {
        env.log_base.join(name)
    };
    let log = fs::read(&path).map_err(|e| {
        (
            "log_missing",
            format!("{}: {}", path.display(), io_text(&e)),
        )
    })?;
    let want = format!(": PASS manifest_sha256 {}", env.manifest.sha256);
    if !String::from_utf8_lossy(&log)
        .lines()
        .any(|l| l.contains(&want))
    {
        return Err((
            "tool_log_unbound",
            format!(
                "the log has no tool PASS line for manifest {}",
                env.manifest.sha256
            ),
        ));
    }
    Ok(())
}
/// A receipt is green only on a file that states THIS cell's result for THIS frozen candidate
/// (review RELF F14: plain receipts were self-asserted): the record's artifact files (or, for a
/// record without artifacts, its log) must hold a line with `<name>: PASS manifest_sha256
/// <manifest sha256>`, and no line `<name>: <result>` whose result is not `PASS`.
fn receipt_states_pass(
    env: &Env,
    verify: &Verify,
    record: &Record,
) -> Result<(), (&'static str, String)> {
    let mut files = Vec::new();
    match record.value.get("artifacts").and_then(Value::as_array) {
        Some(listed) => {
            for entry in listed {
                let path = entry.get("path").and_then(Value::as_str).unwrap_or("");
                files.push(
                    fs::read(path)
                        .map_err(|e| ("artifact_missing", format!("{path}: {}", io_text(&e))))?,
                );
            }
        }
        None => {
            let name = record
                .value
                .get("log")
                .and_then(Value::as_str)
                .unwrap_or("");
            let path = if Path::new(name).is_absolute() {
                PathBuf::from(name)
            } else {
                env.log_base.join(name)
            };
            files.push(fs::read(&path).map_err(|e| {
                (
                    "log_missing",
                    format!("{}: {}", path.display(), io_text(&e)),
                )
            })?);
        }
    }
    receipt_result(env.manifest, verify.name.as_deref().unwrap_or(""), &files)
}
/// The result rule of [`receipt_states_pass`] over the receipt's file contents (`emit` applies it
/// to the artifacts it is given before it writes a PASS).
pub fn receipt_result(
    manifest: &Manifest,
    name: &str,
    files: &[Vec<u8>],
) -> Result<(), (&'static str, String)> {
    let tag = format!("{name}: ");
    let want = format!("{name}: PASS manifest_sha256 {}", manifest.sha256);
    let mut stated = false;
    for bytes in files {
        for line in String::from_utf8_lossy(bytes).lines() {
            if let Some((_, rest)) = line.split_once(&tag)
                && rest.split_whitespace().next() != Some("PASS")
            {
                return Err((
                    "receipt_result_failed",
                    format!("the receipt states a result other than PASS: {line}"),
                ));
            }
            stated |= line.contains(&want);
        }
    }
    if stated {
        Ok(())
    } else {
        Err((
            "receipt_unbound",
            format!("no file of this receipt states `{want}`"),
        ))
    }
}
/// One artifact file of an operator receipt names this candidate: its manifest sha256 and its exe
/// sha256 both appear in the file.
pub fn names_candidate(manifest: &Manifest, record: &Record) -> Result<(), String> {
    let listed = record
        .value
        .get("artifacts")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let names = listed.iter().any(|entry| {
        entry
            .get("path")
            .and_then(Value::as_str)
            .and_then(|p| fs::read(p).ok())
            .is_some_and(|bytes| {
                let text = String::from_utf8_lossy(&bytes);
                text.contains(&manifest.sha256) && text.contains(&manifest.candidate.exe_sha256)
            })
    });
    if names {
        Ok(())
    } else {
        Err(format!(
            "no artifact of this operator receipt names manifest {} and exe {}",
            manifest.sha256, manifest.candidate.exe_sha256
        ))
    }
}
/// A receipt that lists `artifacts` is only as good as those files: each is re-hashed from disk.
fn check_artifacts(record: &Record) -> Result<(), (&'static str, String)> {
    let Some(listed) = record.value.get("artifacts") else {
        return Ok(());
    };
    let Some(listed) = listed.as_array().filter(|a| !a.is_empty()) else {
        return Err((
            "receipt_no_artifacts",
            "the artifacts list is empty".to_string(),
        ));
    };
    for entry in listed {
        let (Some(path), Some(want)) = (
            entry.get("path").and_then(Value::as_str),
            entry.get("sha256").and_then(Value::as_str),
        ) else {
            return Err((
                "record_inconsistent",
                "an artifact needs a path and a sha256".to_string(),
            ));
        };
        match file_sha256(Path::new(path)) {
            Ok(have) if have == want => {}
            Ok(have) => {
                return Err((
                    "artifact_changed",
                    format!("{path} hashes to {have}, the record states {want}"),
                ));
            }
            Err(e) => return Err(("artifact_missing", format!("{path}: {e}"))),
        }
    }
    Ok(())
}

/// Re-reads the record's log: it must exist, and for gate records re-parsing it with the gate's
/// own parser must give the per-target counts the record states.
fn check_log(
    env: &Env,
    record: &Record,
    runs: Runs,
    exit: Option<i64>,
    expected: Option<PerTarget>,
) -> Result<Option<String>, (&'static str, String)> {
    if runs == Runs::Receipt {
        check_artifacts(record)?;
    }
    let Some(name) = record.value.get("log").and_then(Value::as_str) else {
        return if runs == Runs::Receipt {
            if record.value.get("artifacts").is_some() {
                Ok(None)
            } else {
                Err((
                    "receipt_no_artifacts",
                    "a receipt record names neither a log nor artifacts".to_string(),
                ))
            }
        } else {
            Err(("log_missing", "the record names no log".to_string()))
        };
    };
    let path = if Path::new(name).is_absolute() {
        PathBuf::from(name)
    } else {
        env.log_base.join(name)
    };
    let bytes = fs::read(&path).map_err(|e| {
        (
            "log_missing",
            format!("{}: {}", path.display(), io_text(&e)),
        )
    })?;
    let sha = sha256_bytes(&bytes);
    let Some(expected) = expected else {
        return Ok(Some(sha));
    };
    let parsed = gate::parse_output(&String::from_utf8_lossy(&bytes));
    let seen: PerTarget = parsed
        .targets
        .iter()
        .map(|t| (t.passed, t.failed, t.ignored))
        .collect();
    let (p, f, i) = parsed.total();
    if seen != expected {
        return Err((
            "log_disagrees",
            format!(
                "log has {p} passed / {f} failed / {i} ignored over {} targets; the record states other counts",
                seen.len()
            ),
        ));
    }
    let kind = if runs == Runs::Tests {
        gate::Kind::Test
    } else {
        gate::Kind::Check
    };
    let code = exit.and_then(|c| i32::try_from(c).ok());
    if gate::verdict(&parsed, code, kind) != gate::Verdict::Pass {
        return Err((
            "log_disagrees",
            "re-parsing the log does not give a PASS".to_string(),
        ));
    }
    Ok(Some(sha))
}

// ------------------------------------------------------------------ the machine

#[derive(Debug, Clone, PartialEq)]
pub struct Block {
    pub state: State,
    pub cell: Option<String>,
    pub claim: Option<String>,
    pub code: String,
    pub detail: String,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Status {
    Pending,
    Done,
    Blocked,
}
impl Status {
    pub fn id(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Done => "done",
            Self::Blocked => "blocked",
        }
    }
    fn parse(text: &str) -> Option<Self> {
        [Self::Pending, Self::Done, Self::Blocked]
            .into_iter()
            .find(|s| s.id() == text)
    }
}
#[derive(Debug, Clone)]
pub struct RunOptions {
    pub attempt: String,
    /// `true`: `--attempt` (must not exist); `false`: `--resume` (must exist).
    pub create: bool,
    pub stop_after: Option<State>,
}
#[derive(Debug, Clone)]
pub struct Report {
    /// `accepted`, `blocked` or `in-progress`.
    pub outcome: &'static str,
    pub states: Vec<(State, Status)>,
    pub blocks: Vec<Block>,
    pub receipt: Option<PathBuf>,
}

#[derive(Serialize, Deserialize, Clone)]
#[serde(deny_unknown_fields)]
struct StateEntry {
    status: String,
    first_done_revision: Option<u64>,
    evidence: Vec<String>,
    blocks: Vec<BlockRow>,
}
#[derive(Serialize, Deserialize, Clone)]
#[serde(deny_unknown_fields)]
struct BlockRow {
    cell: Option<String>,
    claim: Option<String>,
    code: String,
    detail: String,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct StateFile {
    schema: u64,
    attempt: String,
    candidate: Candidate,
    manifest_sha256: String,
    profile_sha256: String,
    revision: u64,
    outcome: String,
    states: BTreeMap<String, StateEntry>,
}
const STATE_FILE: &str = "state.json";
const EVENTS_FILE: &str = "events.jsonl";
const SEAL_FILE: &str = "set-aside.json";
const RECEIPT_FILE: &str = "promotion-receipt.json";

fn attempt_id(id: &str) -> Result<(), String> {
    let ok = !id.is_empty()
        && !id.starts_with('.')
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b));
    if ok {
        Ok(())
    } else {
        Err(fail(
            "attempt_id_invalid",
            format!("{id:?} must be [A-Za-z0-9._-]+ and not start with '.'"),
        ))
    }
}
fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}
fn write_atomic(dir: &Path, name: &str, bytes: &[u8]) -> Result<(), String> {
    let temporary = dir.join(format!("{name}.tmp"));
    let io = |e: std::io::Error| {
        fail(
            "attempt_io",
            format!("{}: {}", temporary.display(), io_text(&e)),
        )
    };
    let mut file = File::create(&temporary).map_err(io)?;
    file.write_all(bytes).map_err(io)?;
    file.sync_all().map_err(io)?;
    fs::rename(&temporary, dir.join(name)).map_err(io)
}
fn append_event(dir: &Path, event: &Value) -> Result<(), String> {
    let path = dir.join(EVENTS_FILE);
    let io = |e: std::io::Error| fail("attempt_io", format!("{}: {}", path.display(), io_text(&e)));
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(io)?;
    file.write_all(format!("{event}\n").as_bytes())
        .map_err(io)?;
    file.sync_all().map_err(io)
}
fn read_state(dir: &Path) -> Result<StateFile, String> {
    let path = dir.join(STATE_FILE);
    let text = fs::read_to_string(&path).map_err(|e| {
        fail(
            "state_corrupt",
            format!("{}: {}", path.display(), io_text(&e)),
        )
    })?;
    let state: StateFile = serde_json::from_str(&text)
        .map_err(|e| fail("state_corrupt", format!("{}: {e}", path.display())))?;
    if state.schema != 1
        || state
            .states
            .values()
            .any(|s| Status::parse(&s.status).is_none())
    {
        return Err(fail(
            "state_corrupt",
            format!("{}: unknown schema or status", path.display()),
        ));
    }
    Ok(state)
}
fn write_state(dir: &Path, state: &StateFile) -> Result<(), String> {
    let text = serde_json::to_string_pretty(state).map_err(|e| fail("attempt_io", e))?;
    write_atomic(dir, STATE_FILE, text.as_bytes())
}

fn block_from(state: State, error: &str) -> Block {
    let (code, detail) = split_error(error);
    Block {
        state,
        cell: None,
        claim: None,
        code,
        detail,
    }
}
fn red_blocks(results: &[CellResult], state: Option<State>) -> Vec<Block> {
    results
        .iter()
        .filter(|r| state.is_none_or(|s| r.state == s))
        .filter_map(|r| match &r.outcome {
            Outcome::Red { code, evs, detail } => Some(Block {
                state: r.state,
                cell: Some(r.cell.clone()),
                claim: Some(r.claim.clone()),
                code: (*code).to_string(),
                detail: if evs.is_empty() {
                    detail.clone()
                } else {
                    format!("{detail} [{}]", evs.join(", "))
                },
            }),
            Outcome::Green { .. } => None,
        })
        .collect()
}
fn green_evidence(results: &[CellResult], state: State) -> Vec<String> {
    results
        .iter()
        .filter(|r| r.state == state)
        .filter_map(|r| match &r.outcome {
            Outcome::Green { ev, .. } => Some(ev.clone()),
            Outcome::Red { .. } => None,
        })
        .collect()
}

/// The authoritative decision: the bytes still equal the frozen digests, every cell of every
/// mandatory claim is green now, and no mandatory claim is marked blocked in the table.
fn decision_blocks(loaded: &Loaded) -> Vec<Block> {
    let mut blocks = Vec::new();
    if let Err(e) = loaded.manifest.verify_bytes() {
        blocks.push(block_from(State::Promotion, &e));
    }
    blocks.extend(red_blocks(&evaluate_all(&loaded.env()), None));
    for id in &loaded.profile.mandatory_claims {
        if let Some(claim) = loaded.claims.get(id).filter(|c| c.status == "blocked") {
            blocks.push(Block {
                state: State::Promotion,
                cell: None,
                claim: Some(id.clone()),
                code: "claim_blocked".to_string(),
                detail: format!("the claim table marks {id} blocked by {}", claim.blocked_by),
            });
        }
    }
    blocks
}
fn receipt(loaded: &Loaded, name: &str) -> Value {
    let results = evaluate_all(&loaded.env());
    let claims: Vec<Value> = loaded
        .profile
        .mandatory_claims
        .iter()
        .map(|id| {
            let cells: Vec<Value> = results
                .iter()
                .filter(|r| &r.claim == id)
                .filter_map(|r| match &r.outcome {
                    Outcome::Green {
                        ev,
                        passed,
                        log_sha256,
                    } => Some(json!({
                        "cell": r.cell, "state": r.state.id(), "ev": ev, "passed": passed,
                        "log_sha256": log_sha256,
                    })),
                    Outcome::Red { .. } => None,
                })
                .collect();
            json!({"claim": id,
                   "status_in_table": loaded.claims.get(id).map(|c| c.status.clone()),
                   "cells": cells})
        })
        .collect();
    json!({
        "schema": 1,
        "decision": "ACCEPTED",
        "attempt": name,
        "candidate": loaded.manifest.candidate.to_json(),
        "manifest_sha256": loaded.manifest.sha256,
        "profile": loaded.profile.name,
        "profile_sha256": loaded.profile.sha256,
        "ledger_sha256": loaded.ledger.sha256,
        "ledger_records": loaded.ledger.records.len(),
        "claims_sha256": loaded.claims.sha256,
        "claims": claims,
        "publication": "NOT_PERFORMED: owner token required",
    })
}

struct Attempt<'a> {
    dir: &'a Path,
    name: &'a str,
    loaded: &'a Loaded,
}
impl Attempt<'_> {
    fn seal(&self) -> Value {
        json!({"schema": 1, "attempt": self.name, "candidate": self.loaded.manifest.candidate.to_json(),
               "manifest_sha256": self.loaded.manifest.sha256, "profile_sha256": self.loaded.profile.sha256})
    }
    /// Writes the seal once; an existing seal must equal this attempt's.
    fn set_aside(&self) -> Result<Option<Block>, String> {
        let path = self.dir.join(SEAL_FILE);
        let expected = self.seal();
        match fs::read_to_string(&path) {
            Ok(text) => {
                let same = serde_json::from_str::<Value>(&text).is_ok_and(|seal| seal == expected);
                Ok((!same).then(|| Block {
                    state: State::SetAside,
                    cell: None,
                    claim: None,
                    code: "set_aside_conflict".to_string(),
                    detail: format!("{} differs from this candidate's seal", path.display()),
                }))
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                let text =
                    serde_json::to_string_pretty(&expected).map_err(|e| fail("attempt_io", e))?;
                let io = |e: std::io::Error| {
                    fail("attempt_io", format!("{}: {}", path.display(), io_text(&e)))
                };
                let mut file = OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(&path)
                    .map_err(io)?;
                file.write_all(text.as_bytes()).map_err(io)?;
                file.sync_all().map_err(io)?;
                Ok(None)
            }
            Err(e) => Err(fail(
                "attempt_io",
                format!("{}: {}", path.display(), io_text(&e)),
            )),
        }
    }
    fn seal_matches(&self) -> Option<Block> {
        let path = self.dir.join(SEAL_FILE);
        let matches = fs::read_to_string(&path)
            .ok()
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
            .is_some_and(|seal| seal == self.seal());
        (!matches).then(|| Block {
            state: State::Proof,
            cell: None,
            claim: None,
            code: "set_aside_conflict".to_string(),
            detail: format!(
                "{} is missing or differs from this candidate's seal",
                path.display()
            ),
        })
    }
    fn evaluate(&self, state: State) -> Result<(Vec<Block>, Vec<String>), String> {
        let loaded = self.loaded;
        let results = evaluate_all(&loaded.env());
        let mut blocks = Vec::new();
        if matches!(state, State::Package | State::SetAside | State::Proof)
            && let Err(e) = loaded.manifest.verify_bytes()
        {
            blocks.push(block_from(state, &e));
        }
        match state {
            State::SetAside if blocks.is_empty() => blocks.extend(self.set_aside()?),
            State::Proof => blocks.extend(self.seal_matches()),
            State::Promotion => {
                blocks.extend(decision_blocks(loaded));
                if blocks.is_empty() {
                    self.write_receipt()?;
                }
            }
            _ => {}
        }
        if state.takes_cells() {
            blocks.extend(red_blocks(&results, Some(state)));
        }
        let evidence = if state.takes_cells() {
            green_evidence(&results, state)
        } else {
            Vec::new()
        };
        Ok((blocks, evidence))
    }
    fn write_receipt(&self) -> Result<(), String> {
        let text = serde_json::to_string_pretty(&receipt(self.loaded, self.name))
            .map_err(|e| fail("attempt_io", e))?;
        let path = self.dir.join(RECEIPT_FILE);
        if fs::read_to_string(&path).is_ok_and(|old| old == text) {
            return Ok(());
        }
        if path.exists() {
            self.set_receipt_aside("superseded")?;
        }
        write_atomic(self.dir, RECEIPT_FILE, text.as_bytes())
    }
    /// Never deletes: the old receipt is renamed with the reason and a counter.
    fn set_receipt_aside(&self, why: &str) -> Result<(), String> {
        let from = self.dir.join(RECEIPT_FILE);
        for n in 1..=u32::from(u16::MAX) {
            let to = self.dir.join(format!("{RECEIPT_FILE}.{why}-{n}"));
            if !to.exists() {
                return fs::rename(&from, &to)
                    .map_err(|e| fail("attempt_io", format!("{}: {}", to.display(), io_text(&e))));
            }
        }
        Err(fail("attempt_io", "no free receipt name"))
    }
}

fn lock(dir: &Path) -> Result<File, String> {
    let path = dir.join("lock");
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(&path)
        .map_err(|e| fail("attempt_io", format!("{}: {}", path.display(), io_text(&e))))?;
    match file.try_lock() {
        Ok(()) => Ok(file),
        Err(std::fs::TryLockError::WouldBlock) => Err(fail("attempt_locked", path.display())),
        Err(std::fs::TryLockError::Error(e)) => Err(fail("attempt_io", io_text(&e))),
    }
}

fn fresh_state(name: &str, loaded: &Loaded) -> StateFile {
    StateFile {
        schema: 1,
        attempt: name.to_string(),
        candidate: loaded.manifest.candidate.clone(),
        manifest_sha256: loaded.manifest.sha256.clone(),
        profile_sha256: loaded.profile.sha256.clone(),
        revision: 0,
        outcome: "in-progress".to_string(),
        states: STATES
            .iter()
            .map(|s| {
                (
                    s.id().to_string(),
                    StateEntry {
                        status: Status::Pending.id().to_string(),
                        first_done_revision: None,
                        evidence: Vec::new(),
                        blocks: Vec::new(),
                    },
                )
            })
            .collect(),
    }
}

pub fn run(inputs: &Inputs, options: &RunOptions) -> Result<Report, String> {
    attempt_id(&options.attempt)?;
    let dir = inputs.attempts.join(&options.attempt);
    if !options.create && !dir.is_dir() {
        return Err(fail("attempt_missing", dir.display()));
    }
    let loaded = load(inputs)?;
    if options.create {
        fs::create_dir_all(&inputs.attempts).map_err(|e| {
            fail(
                "attempt_io",
                format!("{}: {}", inputs.attempts.display(), io_text(&e)),
            )
        })?;
        match fs::create_dir(&dir) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                return Err(fail("attempt_exists", dir.display()));
            }
            Err(e) => {
                return Err(fail(
                    "attempt_io",
                    format!("{}: {}", dir.display(), io_text(&e)),
                ));
            }
        }
    }
    let _guard = lock(&dir)?;
    let mut file = if options.create {
        fresh_state(&options.attempt, &loaded)
    } else {
        let file = read_state(&dir)?;
        if file.attempt != options.attempt
            || file.candidate != loaded.manifest.candidate
            || file.manifest_sha256 != loaded.manifest.sha256
            || file.profile_sha256 != loaded.profile.sha256
        {
            return Err(fail(
                "attempt_identity_changed",
                "the candidate, its manifest or the profile differ from the ones this attempt started with; start a new attempt",
            ));
        }
        if STATES.iter().any(|s| !file.states.contains_key(s.id())) {
            return Err(fail("state_corrupt", "a state entry is missing"));
        }
        file
    };
    if file.outcome == "accepted" && options.stop_after.is_some() {
        return Err(fail(
            "attempt_accepted",
            "--stop-after would re-open an accepted attempt",
        ));
    }
    let attempt = Attempt {
        dir: &dir,
        name: &options.attempt,
        loaded: &loaded,
    };
    let mut status: BTreeMap<State, Status> =
        STATES.iter().map(|s| (*s, Status::Pending)).collect();
    let mut all_blocks = Vec::new();
    for state in STATES {
        let ready = state.needs().iter().all(|n| status[n] == Status::Done);
        let (outcome, blocks, evidence) = if ready {
            let (blocks, evidence) = attempt.evaluate(state)?;
            let outcome = if blocks.is_empty() {
                Status::Done
            } else {
                Status::Blocked
            };
            (outcome, blocks, evidence)
        } else {
            (Status::Pending, Vec::new(), Vec::new())
        };
        status.insert(state, outcome);
        file.revision += 1;
        let revision = file.revision;
        let entry = file
            .states
            .get_mut(state.id())
            .ok_or_else(|| fail("state_corrupt", state.id()))?;
        if outcome == Status::Done && entry.first_done_revision.is_none() {
            entry.first_done_revision = Some(revision);
        }
        entry.status = outcome.id().to_string();
        entry.evidence = evidence.clone();
        entry.blocks = blocks
            .iter()
            .map(|b| BlockRow {
                cell: b.cell.clone(),
                claim: b.claim.clone(),
                code: b.code.clone(),
                detail: b.detail.clone(),
            })
            .collect();
        all_blocks.extend(blocks.clone());
        file.outcome = if state == State::Promotion && outcome == Status::Done {
            "accepted"
        } else if all_blocks.is_empty() {
            "in-progress"
        } else {
            "blocked"
        }
        .to_string();
        write_state(&dir, &file)?;
        append_event(
            &dir,
            &json!({"attempt": options.attempt, "revision": revision, "at": now_secs(),
                    "state": state.id(), "status": outcome.id(), "evidence": evidence,
                    "blocks": blocks.iter().map(|b| b.code.clone()).collect::<Vec<_>>()}),
        )?;
        if options.stop_after == Some(state) {
            break;
        }
    }
    let accepted = status[&State::Promotion] == Status::Done;
    let finished = if accepted {
        "accepted"
    } else if all_blocks.is_empty() {
        "in-progress"
    } else {
        "blocked"
    };
    if !accepted && dir.join(RECEIPT_FILE).exists() {
        attempt.set_receipt_aside("invalidated")?;
        append_event(
            &dir,
            &json!({"attempt": options.attempt, "revision": file.revision, "at": now_secs(),
                    "event": "promotion receipt set aside: no longer accepted"}),
        )?;
    }
    file.outcome = finished.to_string();
    write_state(&dir, &file)?;
    Ok(Report {
        outcome: finished,
        states: STATES.iter().map(|s| (*s, status[s])).collect(),
        blocks: all_blocks,
        receipt: accepted.then(|| dir.join(RECEIPT_FILE)),
    })
}

// ------------------------------------------------------------------ publication stub

/// A token file is four short lines; anything larger is not one.
const TOKEN_MAX_BYTES: u64 = 1024;

/// The publication stub. It can never succeed: it returns `Err` for every input. It checks, in
/// order: the attempt exists and is still accepted on the exact bytes, an owner token file is
/// given, is a regular file, has the exact four-line shape and names this candidate's digests.
/// A fully valid token still ends in `publication_not_implemented`: the owner publishes outside
/// this tool. It reads files and writes nothing.
pub fn publish(inputs: &Inputs, attempt: &str, token: Option<&Path>) -> Result<(), String> {
    attempt_id(attempt)?;
    let dir = inputs.attempts.join(attempt);
    if !dir.is_dir() {
        return Err(fail("attempt_missing", dir.display()));
    }
    let loaded = load(inputs)?;
    let state = read_state(&dir)?;
    if state.manifest_sha256 != loaded.manifest.sha256
        || state.profile_sha256 != loaded.profile.sha256
    {
        return Err(fail(
            "attempt_identity_changed",
            "the manifest or profile differ from the attempt's",
        ));
    }
    let blocks = decision_blocks(&loaded);
    if state.outcome != "accepted" || !dir.join(RECEIPT_FILE).is_file() || !blocks.is_empty() {
        let why = blocks
            .first()
            .map_or_else(|| "no promotion receipt".to_string(), |b| b.code.clone());
        return Err(fail(
            "publish_not_accepted",
            format!("attempt {attempt} is not an accepted candidate now ({why})"),
        ));
    }
    let path = token.ok_or_else(|| {
        fail(
            "owner_token_missing",
            "publication needs --owner-token FILE from the owner",
        )
    })?;
    let meta = fs::symlink_metadata(path).map_err(|e| {
        fail(
            "owner_token_unreadable",
            format!("{}: {}", path.display(), io_text(&e)),
        )
    })?;
    if !meta.is_file() {
        return Err(fail("owner_token_not_regular", path.display()));
    }
    if meta.len() > TOKEN_MAX_BYTES {
        return Err(fail("owner_token_invalid", "too large for a token file"));
    }
    let mut text = String::new();
    File::open(path)
        .and_then(|f| f.take(TOKEN_MAX_BYTES).read_to_string(&mut text))
        .map_err(|e| fail("owner_token_unreadable", io_text(&e)))?;
    let lines: Vec<&str> = text.strip_suffix('\n').unwrap_or("").split('\n').collect();
    let digest = |line: &str, key: &str| -> Option<String> {
        line.strip_prefix(&format!("{key}="))
            .filter(|d| is_hex(d, 64))
            .map(str::to_string)
    };
    let [header, action, exe, bundle] = lines.as_slice() else {
        return Err(fail(
            "owner_token_invalid",
            "expected the four-line OWNER-PUBLISH-TOKEN v1 shape",
        ));
    };
    let (Some(exe), Some(bundle)) = (digest(exe, "exe_sha256"), digest(bundle, "bundle_sha256"))
    else {
        return Err(fail(
            "owner_token_invalid",
            "exe_sha256 and bundle_sha256 must be 64 lower-case hex",
        ));
    };
    if *header != "OWNER-PUBLISH-TOKEN v1" || *action != "action=publish" {
        return Err(fail(
            "owner_token_invalid",
            "expected OWNER-PUBLISH-TOKEN v1 and action=publish",
        ));
    }
    if exe != loaded.manifest.candidate.exe_sha256
        || bundle != loaded.manifest.candidate.bundle_sha256
    {
        return Err(fail(
            "owner_token_mismatch",
            "the token names other bytes than this candidate",
        ));
    }
    Err(fail(
        "publication_not_implemented",
        "this tool never publishes; the owner publishes outside it, from the accepted receipt",
    ))
}

// ------------------------------------------------------------------ command line

pub(crate) const USAGE: &str = "USAGE: xtask release freeze <sha> --exe FILE --bundle DIR --out MANIFEST [--repo DIR]\n       xtask release candidate <sha> --manifest FILE (--attempt ID | --resume ID) [--profile FILE] [--registry DIR] [--attempts DIR] [--stop-after STATE]\n       xtask release emit <kind> <name> (--pass --producer TEXT --artifact FILE... | --not-yet --reason TEXT [--blocker ID]...) [--manifest FILE] [--profile FILE] [--registry DIR]\n       xtask release produce <cell> --lane LANE --manifest FILE [--profile FILE] [--registry DIR] [--guard PATH] [-- <command...>]\n       xtask release publish <sha> --manifest FILE --attempt ID [--owner-token FILE] [--profile FILE] [--registry DIR] [--attempts DIR]";

struct Parsed {
    sha: String,
    inputs: Inputs,
    attempt: Option<String>,
    resume: Option<String>,
    stop_after: Option<State>,
    token: Option<PathBuf>,
}
pub(crate) fn usage(what: &str) -> String {
    format!("USAGE: {what}\n{USAGE}")
}
fn parse_args(args: &[String]) -> Result<Parsed, String> {
    let sha = args
        .first()
        .ok_or_else(|| usage("a candidate sha is required"))?;
    if !is_hex(sha, 40) {
        return Err(usage("the candidate sha is 40 lower-case hex"));
    }
    let mut values: BTreeMap<&str, String> = BTreeMap::new();
    let mut rest = args[1..].iter();
    while let Some(arg) = rest.next() {
        let key = match arg.as_str() {
            "--manifest" | "--profile" | "--registry" | "--attempts" | "--attempt" | "--resume"
            | "--stop-after" | "--owner-token" => arg.as_str(),
            other => return Err(usage(&format!("unknown option {other}"))),
        };
        let value = rest
            .next()
            .ok_or_else(|| usage(&format!("{key} needs a value")))?;
        if values.insert(key, value.clone()).is_some() {
            return Err(usage(&format!("repeated option {key}")));
        }
    }
    let stop_after = match values.get("--stop-after") {
        Some(name) => {
            Some(State::parse(name).ok_or_else(|| usage(&format!("unknown state {name}")))?)
        }
        None => None,
    };
    let registry = match values.get("--registry") {
        Some(r) => PathBuf::from(r),
        None => gate::default_registry()?,
    };
    let manifest = values
        .get("--manifest")
        .ok_or_else(|| usage("--manifest is required"))?;
    Ok(Parsed {
        sha: sha.clone(),
        inputs: Inputs {
            manifest: PathBuf::from(manifest),
            profile: values.get("--profile").map_or_else(
                || PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("release-profile.json"),
                PathBuf::from,
            ),
            attempts: values
                .get("--attempts")
                .map_or_else(|| registry.join("release/attempts"), PathBuf::from),
            registry,
        },
        attempt: values.get("--attempt").cloned(),
        resume: values.get("--resume").cloned(),
        stop_after,
        token: values.get("--owner-token").map(PathBuf::from),
    })
}
fn render(attempt: &str, sha: &str, report: &Report) -> String {
    let mut out = format!(
        "release {attempt} @ {}: {}\n",
        &sha[..7],
        report.outcome.to_uppercase()
    );
    for (state, status) in &report.states {
        out.push_str(&format!("  {:<10} {}\n", state.id(), status.id()));
    }
    for block in &report.blocks {
        out.push_str(&format!(
            "  BLOCKED {}: {} [{} {}]: {}\n",
            block.state.id(),
            block.code,
            block.cell.as_deref().unwrap_or("-"),
            block.claim.as_deref().unwrap_or("-"),
            block.detail
        ));
    }
    if let Some(receipt) = &report.receipt {
        out.push_str(&format!("  receipt: {}\n", receipt.display()));
    }
    out
}

/// `xtask release ...` entry point (arguments after the word `release`). Exit codes: 0 accepted,
/// 1 not accepted, 2 usage or unreadable input, 3 a publication refusal.
pub fn cli(args: &[String]) -> Result<u8, String> {
    let Some((command, rest)) = args.split_first() else {
        return Err(usage("a release subcommand is required"));
    };
    match command.as_str() {
        "candidate" => {
            let parsed = parse_args(rest)?;
            let (name, create) = match (&parsed.attempt, &parsed.resume) {
                (Some(a), None) => (a.clone(), true),
                (None, Some(r)) => (r.clone(), false),
                _ => return Err(usage("give exactly one of --attempt and --resume")),
            };
            let manifest = Manifest::load(&parsed.inputs.manifest)?;
            if manifest.candidate.source_commit != parsed.sha {
                return Err(fail(
                    "candidate_identity_unknown",
                    format!(
                        "{} is not the manifest's source_commit {}",
                        parsed.sha, manifest.candidate.source_commit
                    ),
                ));
            }
            let report = run(
                &parsed.inputs,
                &RunOptions {
                    attempt: name.clone(),
                    create,
                    stop_after: parsed.stop_after,
                },
            )?;
            print!("{}", render(&name, &parsed.sha, &report));
            Ok(u8::from(report.outcome != "accepted"))
        }
        "status" => {
            let parsed = parse_args(rest)?;
            let loaded = load(&parsed.inputs)?;
            if loaded.manifest.candidate.source_commit != parsed.sha {
                return Err(fail("candidate_identity_unknown", &parsed.sha));
            }
            for result in evaluate_all(&loaded.env()) {
                let operator = loaded
                    .profile
                    .cells
                    .iter()
                    .any(|c| c.id == result.cell && c.verify.operator_receipt);
                match &result.outcome {
                    Outcome::Green { ev, passed, .. } => println!(
                        "green {:<10} {:<20} {} {ev} ({passed} passed){}",
                        result.state.id(),
                        result.cell,
                        result.claim,
                        if operator { " [operator receipt]" } else { "" }
                    ),
                    Outcome::Red { code, evs, detail } => println!(
                        "RED   {:<10} {:<20} {} {code} {}: {detail}",
                        result.state.id(),
                        result.cell,
                        result.claim,
                        evs.join(",")
                    ),
                }
            }
            let blocks = decision_blocks(&loaded);
            let reds = blocks.iter().filter(|b| b.cell.is_some()).count();
            for block in blocks.iter().filter(|b| b.cell.is_none()) {
                println!("BLOCK {}: {}", block.code, block.detail);
            }
            println!(
                "status: {} cells, {reds} red; {}",
                loaded.profile.cells.len(),
                if blocks.is_empty() {
                    "every mandatory claim is green on this candidate's bytes"
                } else {
                    "NOT accepted"
                }
            );
            Ok(u8::from(!blocks.is_empty()))
        }
        "freeze" => crate::release_freeze::cli(rest),
        "check-bundle" => crate::release_check::cli(rest),
        "emit" => crate::release_emit::emit(rest),
        "produce" => crate::release_emit::produce(rest),
        "publish" => {
            let parsed = parse_args(rest)?;
            let Some(name) = parsed
                .attempt
                .as_deref()
                .filter(|_| parsed.resume.is_none())
            else {
                return Err(usage("publish takes --attempt ID"));
            };
            let manifest = Manifest::load(&parsed.inputs.manifest)?;
            if manifest.candidate.source_commit != parsed.sha {
                return Err(fail("candidate_identity_unknown", &parsed.sha));
            }
            match publish(&parsed.inputs, name, parsed.token.as_deref()) {
                Ok(()) => Err(fail(
                    "publication_not_implemented",
                    "unreachable: the stub never succeeds",
                )),
                Err(refusal) => {
                    eprintln!("{refusal}");
                    Ok(3)
                }
            }
        }
        other => Err(usage(&format!("unknown release subcommand {other}"))),
    }
}
