// SPDX-License-Identifier: AGPL-3.0-only
//! CL-L1-07: one ordered admission catalogue and its historical-epoch projection.
//!
//! [`EPOCHS`] lists, in admission order, every cohort a `tests/common/*_admission.rs` helper or
//! `tests/inventory_r52_lane_a.rs` projects (r19 .. r50, r52 lane a), then the RU-053 epoch. For each epoch
//! `data/admission-catalogue.tsv` (`epoch kind identity number sha256`) declares:
//!
//! * `manifest`: the sealed manifest file, its data-row count and its sha256 (the manifest bytes);
//! * `key`: the ordered keys, each with the sha256 of its exact review record line (a `python-bounds.tsv`
//!   row or a manual-adjudication verdict; for RU-053 the admission line itself);
//! * `census`: discovery cohorts only, each key it added to the census with the sha256 of its census line;
//! * `pin`: the exact oracle line pins the cohort added (lines its records cite that no earlier review cited).
//!   R52 and RU-053 declare them in one file instead (`pins`: file, pin count, sha256);
//! * `id`: the registry ids the cohort added (targets no earlier review named);
//! * `base` (RU-053 only): the pinned pre-RU-053 pending base (count, sha256 of the sorted keys).
//!
//! Every fact is checked against the sealed manifest and the live checked-in tables: a missing or an extra
//! pin, a dropped key or a flipped record digest is refused with its own code, never accepted.
//! [`as_of`] is the live view with every later cohort removed; [`added_after`] is what those cohorts added.
//!
//! An RU-053 record (`data/ru053-admissions.tsv`, pins in `data/ru053-admission-pins.tsv`) names its census
//! key; the v0.1.4 site (the key's own line) and that line's sha256 (ledger encoding, no strip(): the oracle
//! line without its terminator); the Rust carrier `path:line` and a token that line holds; the consumer hops
//! (`rust/path.rs:Item=v014/path:line` joined by ` > `, from a product root to the carrier, the last hop at
//! the carrier mirroring the v0.1.4 site); per consecutive hop pair the reviewed edge rows that join them
//! (their `path:line` sites joined by `,`, one list per pair, lists joined by ` > `); the behaviour test(s)
//! (`package::target::fn`, joined by ` | `); and the parity test(s) or `-`.
//!
//! The record is checked against reviewed tables only (mapping-only, RU-053 re-cut): no Rust source is
//! parsed and nothing is inferred beyond the listed rows. Every reviewed row pins a repo-relative path, that
//! file's sha256, a 1-based line and the line's exact bytes (`data/ru053-reviewed-{roots,edges,tests,crates}.tsv`,
//! read through the [`Tree`]). The checker (a) recomputes each used row's sha256 and compares the exact line
//! bytes, (b) requires the chain to be contiguous by exact identity of the row endpoints, from a reviewed root
//! to the reviewed carrier row, with every crate it touches pinned by its `Cargo.toml` and `src/lib.rs`, and
//! (c) requires each test to be actually collected: a `kind = ["test"]` cargo metadata target whose source is
//! the pinned file, with no required features, and a structurally parsed manifest that leaves it on the
//! default harness. Anything missing, stale, duplicated, malformed, unknown or not listed is refused with one
//! stable code per class. A carrier site alone, however well it matches, is refused, and so is any of the
//! three guarded stats rows ([`RU053_GUARDED`]). `report()` counts an RU-053 row as reviewed only when the
//! whole catalogue, including its record, validates.
use super::adjudication as adj;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

/// `epoch kind identity number sha256`, one row per manifest/key/census/pin/id/pins/base fact.
pub const CATALOGUE: &str = include_str!("../../data/admission-catalogue.tsv");
pub const CATALOGUE_HEADER: &str = "epoch\tkind\tidentity\tnumber\tsha256";
/// The RU-053 admission records (one row per admitted census key).
pub const RU053_ADMISSIONS: &str = include_str!("../../data/ru053-admissions.tsv");
pub const RU053_HEADER: &str = "key\tsite\tsite_sha256\tcarrier\tcarrier_token\tconsumer\tconsumer_calls\tbehaviour_test\tparity_test";
/// The reviewed RU-053 tables (workspace-relative, read through the [`Tree`]). Each row ends in the same pin
/// columns: `path line sha256 text`.
pub const RU053_ROOTS_PATH: &str = "test-support/budget_model/data/ru053-reviewed-roots.tsv";
pub const RU053_ROOTS_HEADER: &str = "root\tpath\tline\tsha256\ttext";
pub const RU053_EDGES_PATH: &str = "test-support/budget_model/data/ru053-reviewed-edges.tsv";
pub const RU053_EDGES_HEADER: &str = "from\tto\tpath\tline\tsha256\ttext";
pub const RU053_TESTS_PATH: &str = "test-support/budget_model/data/ru053-reviewed-tests.tsv";
pub const RU053_TESTS_HEADER: &str = "test\tpath\tline\tsha256\ttext";
pub const RU053_CRATES_PATH: &str = "test-support/budget_model/data/ru053-reviewed-crates.tsv";
pub const RU053_CRATES_HEADER: &str = "crate\tpath\tline\tsha256\ttext";
/// Refusal codes of the mapping-only RU-053 record check, one per class.
pub const UNRESOLVED_HOP: &str = "ru053-unresolved-consumer-hop";
pub const NOT_PRODUCT: &str = "ru053-consumer-not-product";
pub const DUPLICATE_HOP: &str = "ru053-duplicate-consumer-hop";
pub const UNCONNECTED: &str = "ru053-unconnected-consumer-hop";
pub const UNMAPPED_EDGE: &str = "ru053-unmapped-edge";
pub const UNMAPPED_CRATE: &str = "ru053-unmapped-crate";
pub const OUTSIDE_ENDPOINT: &str = "ru053-carrier-outside-endpoint";
pub const STALE_PIN: &str = "ru053-stale-pin";
pub const DUPLICATE_ROW: &str = "ru053-duplicate-row";
pub const MALFORMED_ROW: &str = "ru053-malformed-row";
pub const UNCOLLECTED_TEST: &str = "ru053-uncollected-test";
/// The exact oracle line pins the RU-053 records add (`path line sha256`).
pub const RU053_PINS: &str = include_str!("../../data/ru053-admission-pins.tsv");
pub const RU053_PINS_NAME: &str = "ru053-admission-pins.tsv";
pub const RU053: &str = "ru053";
/// The last cohort before RU-053: its view is the pinned pre-RU-053 base.
pub const LAST_HISTORICAL: &str = "r52";
/// The prefix of every pending-row closure violation (`require_complete`).
pub const UNREVIEWED: &str = "unreviewed-candidate: ";
/// The three guarded stats rows RU-053 keeps excluded (RULINGS.tsv RU-053, RU-055): never admissible.
pub const RU053_GUARDED: [&str; 3] = [
    "mycelium_swarm_directory/stats_docs.py:45:41:validator-keyword:high",
    "mycelium_swarm_directory/stats_docs.py:76:35:validator-keyword:high",
    "mycelium_swarm_directory/stats_docs.py:80:41:validator-keyword:high",
];

pub type PinMap = BTreeMap<(String, usize), String>;

/// A refusal: a stable code plus the offending detail.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Refusal {
    pub code: &'static str,
    pub detail: String,
}

impl std::fmt::Display for Refusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.detail)
    }
}

fn refuse<T>(code: &'static str, detail: impl Into<String>) -> Result<T, Refusal> {
    Err(Refusal {
        code,
        detail: detail.into(),
    })
}

fn sha(text: &str) -> String {
    format!("{:x}", Sha256::digest(text.as_bytes()))
}

fn is_hex64(text: &str) -> bool {
    text.len() == 64 && text.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

/// How a manifest names its keys and the record/candidate digests it sealed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Layout {
    /// Key in column 0; the sealed review-record digest in `record`; discovery rows' candidate digest in
    /// `census`, for every row or only rows whose `discovery.0` column equals `discovery.1`.
    Keyed {
        record: usize,
        census: Option<usize>,
        discovery: Option<(usize, &'static str)>,
    },
    /// `kind identity line sha256` rows (`record`, `candidate`, `added-pin`, `source`): the R52 lane-a manifest.
    Kinded,
    /// The RU-053 admission records: the record digest is the digest of the admission line itself.
    Ru053,
}

/// One cohort in admission order: its sealed manifest bytes and how to read them.
#[derive(Clone, Copy, Debug)]
pub struct EpochSpec {
    pub id: &'static str,
    pub manifest_name: &'static str,
    pub manifest: &'static str,
    pub layout: Layout,
}

const fn reviewed_only(record: usize) -> Layout {
    Layout::Keyed {
        record,
        census: None,
        discovery: None,
    }
}

const fn discovery(record: usize, census: usize) -> Layout {
    Layout::Keyed {
        record,
        census: Some(census),
        discovery: None,
    }
}

macro_rules! epoch {
    ($id:literal, $name:literal, $layout:expr) => {
        EpochSpec {
            id: $id,
            manifest_name: $name,
            manifest: include_str!(concat!("../../data/", $name)),
            layout: $layout,
        }
    };
}

/// Every admitted cohort in admission order.
pub const EPOCHS: &[EpochSpec] = &[
    epoch!("r19", "python-r19-ratio-admissions.tsv", reviewed_only(4)),
    epoch!("r21", "python-r21-ratio-admissions.tsv", reviewed_only(4)),
    epoch!(
        "r23",
        "python-r23-seed-invite-admission.tsv",
        reviewed_only(4)
    ),
    epoch!(
        "r24",
        "python-r24-member-lease-admission.tsv",
        reviewed_only(4)
    ),
    epoch!("r25", "python-r25-counts.tsv", discovery(3, 2)),
    epoch!(
        "r26",
        "python-r26-seed-inputs-admission.tsv",
        reviewed_only(4)
    ),
    epoch!("r27", "python-r27-delivery-wake.tsv", discovery(3, 2)),
    epoch!(
        "r28",
        "python-r28-seed-http-admission.tsv",
        reviewed_only(4)
    ),
    epoch!("r29", "python-r29-retry-multiplier.tsv", discovery(3, 2)),
    epoch!("r30", "python-r30-workflow-tokens.tsv", discovery(3, 2)),
    epoch!("r31", "python-r31-seed-admission.tsv", reviewed_only(4)),
    epoch!(
        "r32",
        "python-r32-verifier-cohort.tsv",
        Layout::Keyed {
            record: 3,
            census: Some(2),
            discovery: Some((1, "new-discovery")),
        }
    ),
    epoch!("r36", "python-r36-stats-cohort.tsv", reviewed_only(3)),
    epoch!(
        "r50",
        "python-r50-recovery-forward-cohort.tsv",
        reviewed_only(3)
    ),
    epoch!("r52", "python-r52-lane-a-pins.tsv", Layout::Kinded),
    epoch!("ru053", "ru053-admissions.tsv", Layout::Ru053),
];

/// A validated epoch.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Epoch {
    pub id: String,
    pub manifest: String,
    pub manifest_sha256: String,
    /// Admission order.
    pub keys: Vec<String>,
    /// Key -> sha256 of its exact review record line.
    pub records: BTreeMap<String, String>,
    /// Discovery key -> sha256 of its exact census line (keys this cohort added to the census).
    pub census: BTreeMap<String, String>,
    /// The exact oracle line pins this cohort added.
    pub pins: PinMap,
    /// The registry ids this cohort added.
    pub ids: BTreeSet<String>,
}

/// The validated catalogue.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Catalogue {
    pub epochs: Vec<Epoch>,
    /// The pinned pre-RU-053 pending base.
    pub base: BTreeSet<String>,
}

impl Catalogue {
    pub fn epoch(&self, id: &str) -> Option<&Epoch> {
        self.epochs.iter().find(|e| e.id == id)
    }
    pub fn ids(&self) -> Vec<&str> {
        self.epochs.iter().map(|e| e.id.as_str()).collect()
    }
}

/// Census, reviewed keys, oracle line pins and registry ids as of an epoch.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct View {
    pub census: BTreeSet<String>,
    pub reviewed: BTreeSet<String>,
    pub pins: PinMap,
    pub ids: BTreeSet<String>,
}

impl View {
    pub fn pending(&self) -> BTreeSet<String> {
        self.census.difference(&self.reviewed).cloned().collect()
    }
    /// `(census, reviewed, pending)`, the triple every historical count assertion pins.
    pub fn triple(&self) -> (usize, usize, usize) {
        (
            self.census.len(),
            self.reviewed.len(),
            self.census.difference(&self.reviewed).count(),
        )
    }
}

/// Read access to the workspace an RU-053 record cites.
pub trait Tree {
    /// A workspace-relative file, or `None`.
    fn read(&self, path: &str) -> Option<String>;
    /// `cargo metadata --format-version 1 --no-deps --offline` of the workspace (the actual test collection).
    fn metadata(&self) -> Result<String, String> {
        cargo_metadata()
    }
}

/// This workspace on disk (the two directories above this crate).
pub struct Workspace;

fn workspace_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

/// Workspace-relative, `/`-separated, no parent or absolute component.
fn relative(path: &str) -> bool {
    !path.is_empty()
        && !path.starts_with('/')
        && !path.contains('\\')
        && path
            .split('/')
            .all(|part| !part.is_empty() && part != "." && part != "..")
}

/// The workspace's actual cargo metadata, run once.
fn cargo_metadata() -> Result<String, String> {
    static METADATA: OnceLock<Result<String, String>> = OnceLock::new();
    METADATA
        .get_or_init(|| {
            let cargo = std::env::var_os("CARGO").unwrap_or_else(|| "cargo".into());
            let output = std::process::Command::new(cargo)
                .args([
                    "metadata",
                    "--format-version",
                    "1",
                    "--no-deps",
                    "--offline",
                ])
                .current_dir(workspace_root())
                .output()
                .map_err(|error| format!("cargo metadata: {error}"))?;
            if !output.status.success() {
                return Err(format!("cargo metadata: {}", output.status));
            }
            String::from_utf8(output.stdout).map_err(|error| format!("cargo metadata: {error}"))
        })
        .clone()
}

impl Tree for Workspace {
    fn read(&self, path: &str) -> Option<String> {
        if !relative(path) {
            return None;
        }
        std::fs::read_to_string(workspace_root().join(path)).ok()
    }
}

/// Injectable catalogue inputs; review tables, census and registry are always the checked-in ones.
pub struct Inputs<'a> {
    pub catalogue: &'a str,
    pub ru053: &'a str,
    pub ru053_pins: &'a str,
    pub tree: &'a dyn Tree,
}

impl Inputs<'static> {
    pub fn production() -> Self {
        Inputs {
            catalogue: CATALOGUE,
            ru053: RU053_ADMISSIONS,
            ru053_pins: RU053_PINS,
            tree: &Workspace,
        }
    }
}

/// One review record: its exact line, the registry ids it names and the oracle lines it cites.
struct Review {
    line: String,
    targets: BTreeSet<String>,
    cited: BTreeMap<(String, usize), String>,
}

/// The checked-in census, reviews, citation pins and registry, loaded once.
struct Live {
    pins: PinMap,
    reviews: BTreeMap<String, Review>,
    /// Census key -> exact census line.
    candidates: BTreeMap<String, String>,
    census: BTreeSet<String>,
    /// `all_reviewed()` keys (RU-053 admissions are not among them).
    reviewed: BTreeSet<String>,
    registry: BTreeSet<String>,
}

fn citations(key: &str, text: &str) -> Result<BTreeMap<(String, usize), String>, Refusal> {
    let mut cited = BTreeMap::new();
    for part in text.split(" | ") {
        let parsed = part.split_once(" @ ").and_then(|(site, rest)| {
            let (_, digest) = rest.split_once(" sha256=")?;
            let (path, line) = site.rsplit_once(':')?;
            Some(((path.to_string(), line.parse::<usize>().ok()?), digest))
        });
        match parsed {
            Some((site, digest)) => {
                cited.insert(site, digest.to_string());
            }
            None => return refuse("citation-shape", format!("{key}: {part}")),
        }
    }
    Ok(cited)
}

fn load_live() -> Result<Live, Refusal> {
    let data_error = |error: String| Refusal {
        code: "live-data",
        detail: error,
    };
    let parsed = adj::parse(&adj::TABLES).map_err(data_error)?;
    let registry: BTreeSet<String> = budgets::table::BUDGET_TABLE
        .iter()
        .map(|entry| entry.id.name().to_string())
        .collect();
    let named = |text: &str| -> BTreeSet<String> {
        text.split('+')
            .filter(|t| registry.contains(*t))
            .map(str::to_string)
            .collect()
    };
    let mut reviews = BTreeMap::new();
    let mut add = |key: &str, review: Review| {
        if reviews.insert(key.to_string(), review).is_some() {
            return refuse("duplicate-review", key);
        }
        Ok(())
    };
    for line in include_str!("../../data/python-bounds.tsv").lines().skip(1) {
        let f: Vec<_> = line.split('\t').collect();
        if f.len() != 16 {
            return refuse("live-data", format!("bounds shape: {}", f[0]));
        }
        let mut targets = named(f[11]);
        targets.extend(named(f[14]));
        add(
            f[0],
            Review {
                line: line.to_string(),
                targets,
                cited: BTreeMap::new(),
            },
        )?;
    }
    for text in [
        adj::TABLES.a,
        adj::TABLES.b,
        adj::TABLES.forwarded_thresholds,
    ] {
        for line in text.lines().skip(1) {
            let f: Vec<_> = line.split('\t').collect();
            if f.len() != 6 {
                return refuse("live-data", format!("verdict shape: {}", f[0]));
            }
            add(
                f[0],
                Review {
                    line: line.to_string(),
                    targets: named(f[2]),
                    cited: citations(f[0], f[3])?,
                },
            )?;
        }
    }
    let candidates = include_str!("../../data/python-candidates.tsv")
        .lines()
        .skip(1)
        .map(|line| {
            (
                line.split('\t').next().unwrap_or("").to_string(),
                line.to_string(),
            )
        })
        .collect();
    let reviewed = super::all_reviewed()
        .map_err(data_error)?
        .into_iter()
        .map(|row| row.key)
        .collect();
    Ok(Live {
        pins: parsed.pins,
        reviews,
        candidates,
        census: super::candidate_keys().map_err(data_error)?,
        reviewed,
        registry,
    })
}

fn live() -> Result<&'static Live, Refusal> {
    static LIVE: OnceLock<Result<Live, Refusal>> = OnceLock::new();
    LIVE.get_or_init(load_live).as_ref().map_err(Clone::clone)
}

/// One catalogue row.
struct Row<'a> {
    epoch: &'a str,
    kind: &'a str,
    identity: &'a str,
    number: usize,
    sha256: &'a str,
}

fn catalogue_rows(text: &str) -> Result<Vec<Row<'_>>, Refusal> {
    let mut lines = text.lines();
    if lines.next() != Some(CATALOGUE_HEADER) {
        return refuse("catalogue-shape", "header");
    }
    lines
        .map(|line| {
            let f: Vec<_> = line.split('\t').collect();
            match (f.len(), f.get(3).and_then(|n| n.parse().ok())) {
                (5, Some(number)) if f.iter().all(|s| !s.is_empty() && s.trim() == *s) => Ok(Row {
                    epoch: f[0],
                    kind: f[1],
                    identity: f[2],
                    number,
                    sha256: f[4],
                }),
                _ => refuse("catalogue-shape", line),
            }
        })
        .collect()
}

/// What a manifest itself seals: ordered keys, record digests, discovery candidate digests, pins.
#[derive(Default)]
struct Sealed {
    rows: usize,
    keys: Vec<String>,
    records: BTreeMap<String, String>,
    census: BTreeMap<String, String>,
    pins: PinMap,
    lines: BTreeMap<String, String>,
}

fn sealed(spec: &EpochSpec, text: &str) -> Result<Sealed, Refusal> {
    let mut out = Sealed::default();
    let mut lines = text.lines();
    let header = lines.next().unwrap_or("");
    let shape = |line: &str| Refusal {
        code: "manifest-shape",
        detail: format!("{}: {line}", spec.id),
    };
    if spec.layout == Layout::Ru053 && header != RU053_HEADER {
        return refuse("ru053-shape", "header");
    }
    for line in lines {
        out.rows += 1;
        let f: Vec<_> = line.split('\t').collect();
        match spec.layout {
            Layout::Keyed {
                record,
                census,
                discovery,
            } => {
                let digest = f.get(record).ok_or_else(|| shape(line))?;
                out.keys.push(f[0].to_string());
                out.records.insert(f[0].to_string(), digest.to_string());
                if let Some(column) = census {
                    let added = match discovery {
                        Some((at, value)) => f.get(at) == Some(&value),
                        None => true,
                    };
                    if added {
                        let digest = f.get(column).ok_or_else(|| shape(line))?;
                        out.census.insert(f[0].to_string(), digest.to_string());
                    }
                }
            }
            Layout::Kinded => {
                if f.len() != 4 {
                    return Err(shape(line));
                }
                match f[0] {
                    "record" => {
                        out.keys.push(f[1].to_string());
                        out.records.insert(f[1].to_string(), f[3].to_string());
                    }
                    "added-pin" => {
                        let n = f[2].parse().map_err(|_| shape(line))?;
                        out.pins.insert((f[1].to_string(), n), f[3].to_string());
                    }
                    _ => {}
                }
            }
            Layout::Ru053 => {
                if f.len() != RU053_HEADER.split('\t').count()
                    || f.iter().any(|s| s.is_empty() || s.trim() != *s)
                {
                    return refuse("ru053-shape", line);
                }
                out.keys.push(f[0].to_string());
                out.records.insert(f[0].to_string(), sha(line));
                out.lines.insert(f[0].to_string(), line.to_string());
            }
        }
    }
    Ok(out)
}

fn pins_file(text: &str) -> Result<PinMap, Refusal> {
    let mut lines = text.lines();
    if lines.next() != Some(adj::PINS_HEADER) {
        return refuse("pins-file", "header");
    }
    let mut pins = PinMap::new();
    for line in lines {
        let f: Vec<_> = line.split('\t').collect();
        let n = f.get(1).and_then(|n| n.parse::<usize>().ok());
        match (f.len(), n) {
            (3, Some(n)) if n >= 1 && relative(f[0]) && is_hex64(f[2]) => {
                if pins
                    .insert((f[0].to_string(), n), f[2].to_string())
                    .is_some()
                {
                    return refuse("extra-pin", line);
                }
            }
            _ => return refuse("pins-file", line),
        }
    }
    Ok(pins)
}

/// A product source file: `crates/<crate>/src/**.rs`.
fn product_source(path: &str) -> bool {
    relative(path)
        && path.ends_with(".rs")
        && path
            .strip_prefix("crates/")
            .and_then(|rest| rest.split_once('/'))
            .is_some_and(|(_, rest)| rest.starts_with("src/"))
}

/// An ASCII identifier: letters, digits and `_`, not starting with a digit.
fn ident(text: &str) -> bool {
    !text.is_empty()
        && !text.starts_with(|c: char| c.is_ascii_digit())
        && text.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
}

fn site(text: &str) -> Option<(String, usize)> {
    let (path, line) = text.rsplit_once(':')?;
    let line: usize = line.parse().ok()?;
    (relative(path) && line >= 1).then(|| (path.to_string(), line))
}

/// The crate directory (`crates/<crate>`) holding a workspace path.
fn crate_of(path: &str) -> Option<String> {
    let rest = path.strip_prefix("crates/")?;
    let (name, _) = rest.split_once('/')?;
    Some(format!("crates/{name}"))
}

/// A product fn identity, `crates/<crate>/src/<file>.rs:<Item>` (`Item` a `::`-path of identifiers): its file.
fn fn_identity(id: &str) -> Option<&str> {
    let (stem, item) = id.split_once(".rs:")?;
    let path = &id[..stem.len() + 3];
    (product_source(path) && item.split("::").all(ident)).then_some(path)
}

/// One reviewed pin: a workspace file, its sha256, a 1-based line and that line's exact bytes.
#[derive(Clone, Debug, PartialEq, Eq)]
struct Pin {
    path: String,
    line: usize,
    sha256: String,
    text: String,
}

impl Pin {
    fn site(&self) -> String {
        format!("{}:{}", self.path, self.line)
    }

    /// The file's sha256 recomputed and the pinned line's exact bytes (split on `\n` only).
    fn check(&self, tree: &dyn Tree) -> Result<String, Refusal> {
        match tree.read(&self.path) {
            Some(text)
                if sha(&text) == self.sha256
                    && text.split('\n').nth(self.line - 1) == Some(self.text.as_str()) =>
            {
                Ok(text)
            }
            _ => refuse(STALE_PIN, self.site()),
        }
    }
}

/// `header` rows, each `ids` identity columns and then `path line sha256 text`: exact, or refused.
fn pinned_rows<'a>(
    text: &'a str,
    header: &str,
    table: &str,
) -> Result<Vec<(Vec<&'a str>, Pin)>, Refusal> {
    let malformed = |detail: &str| Refusal {
        code: MALFORMED_ROW,
        detail: format!("{table}: {detail}"),
    };
    let Some(body) = text.strip_suffix('\n') else {
        return Err(malformed("no final newline"));
    };
    let mut lines = body.split('\n');
    if lines.next() != Some(header) {
        return Err(malformed("header"));
    }
    let ids = header.split('\t').count() - 4;
    lines
        .map(|row| {
            let f: Vec<&str> = row.split('\t').collect();
            if f.len() != ids + 4
                || row.contains('\r')
                || f[..ids + 3].iter().any(|s| s.is_empty() || s.trim() != *s)
                || f[ids + 3].trim().is_empty()
            {
                return Err(malformed(row));
            }
            match f[ids + 1].parse::<usize>() {
                Ok(line)
                    if line >= 1
                        && line.to_string() == f[ids + 1]
                        && relative(f[ids])
                        && is_hex64(f[ids + 2]) =>
                {
                    Ok((
                        f[..ids].to_vec(),
                        Pin {
                            path: f[ids].to_string(),
                            line,
                            sha256: f[ids + 2].to_string(),
                            text: f[ids + 3].to_string(),
                        },
                    ))
                }
                _ => Err(malformed(row)),
            }
        })
        .collect()
}

/// One reviewed edge row: `from` reaches `to` on the pinned line (for a carrier row `to` is that line's site).
struct Edge {
    from: String,
    to: String,
    pin: Pin,
}

/// The reviewed RU-053 tables: roots, edges (keyed by site), tests and crate context (keyed by path).
#[derive(Default)]
struct Reviewed {
    roots: BTreeMap<String, Pin>,
    edges: BTreeMap<String, Edge>,
    /// Every fn identity an edge row names.
    endpoints: BTreeSet<String>,
    tests: BTreeMap<String, Pin>,
    crates: BTreeMap<String, Pin>,
}

impl Reviewed {
    fn load(tree: &dyn Tree) -> Result<Self, Refusal> {
        let read = |path: &str| {
            tree.read(path).ok_or_else(|| Refusal {
                code: MALFORMED_ROW,
                detail: format!("{path}: unreadable"),
            })
        };
        let malformed = |table: &str, pin: &Pin| Refusal {
            code: MALFORMED_ROW,
            detail: format!("{table}: {}", pin.site()),
        };
        let duplicate = |table: &str, pin: &Pin| Refusal {
            code: DUPLICATE_ROW,
            detail: format!("{table}: {}", pin.site()),
        };
        let mut out = Reviewed::default();
        let mut sites = BTreeSet::new();
        let roots = read(RU053_ROOTS_PATH)?;
        for (ids, pin) in pinned_rows(&roots, RU053_ROOTS_HEADER, "roots")? {
            let name = ids[0].rsplit("::").next().unwrap_or("");
            let name = name.rsplit(':').next().unwrap_or("");
            if fn_identity(ids[0]) != Some(pin.path.as_str())
                || !pin.text.contains(&format!("fn {name}("))
            {
                return Err(malformed("roots", &pin));
            }
            if !sites.insert(pin.site()) || out.roots.contains_key(ids[0]) {
                return Err(duplicate("roots", &pin));
            }
            out.roots.insert(ids[0].to_string(), pin);
        }
        let edges = read(RU053_EDGES_PATH)?;
        let mut pairs = BTreeSet::new();
        for (ids, pin) in pinned_rows(&edges, RU053_EDGES_HEADER, "edges")? {
            let (from, to) = (ids[0], ids[1]);
            let carrier = to == pin.site();
            if fn_identity(from) != Some(pin.path.as_str())
                || !(carrier || fn_identity(to).is_some())
                || from == to
            {
                return Err(malformed("edges", &pin));
            }
            if !pairs.insert((from, to)) || out.edges.contains_key(&pin.site()) {
                return Err(duplicate("edges", &pin));
            }
            out.endpoints.insert(from.to_string());
            if !carrier {
                out.endpoints.insert(to.to_string());
            }
            out.edges.insert(
                pin.site(),
                Edge {
                    from: from.to_string(),
                    to: to.to_string(),
                    pin,
                },
            );
        }
        let tests = read(RU053_TESTS_PATH)?;
        let mut sites = BTreeSet::new();
        for (ids, pin) in pinned_rows(&tests, RU053_TESTS_HEADER, "tests")? {
            let parts: Vec<&str> = ids[0].split("::").collect();
            let shaped = parts.len() == 3
                && parts.iter().all(|p| ident(p))
                && pin.path.ends_with(".rs")
                && pin.line >= 2
                && pin.text.starts_with(&format!("fn {}(", parts[2]));
            if !shaped {
                return Err(malformed("tests", &pin));
            }
            if !sites.insert(pin.site()) || out.tests.contains_key(ids[0]) {
                return Err(duplicate("tests", &pin));
            }
            out.tests.insert(ids[0].to_string(), pin);
        }
        let crates = read(RU053_CRATES_PATH)?;
        for (ids, pin) in pinned_rows(&crates, RU053_CRATES_HEADER, "crates")? {
            let shaped = crate_of(&pin.path).as_deref() == Some(ids[0])
                && [
                    format!("{}/Cargo.toml", ids[0]),
                    format!("{}/src/lib.rs", ids[0]),
                ]
                .contains(&pin.path);
            if !shaped {
                return Err(malformed("crates", &pin));
            }
            if out.crates.contains_key(&pin.path) {
                return Err(duplicate("crates", &pin));
            }
            out.crates.insert(pin.path.clone(), pin);
        }
        Ok(out)
    }
}

/// An inner attribute anywhere in the file (`#`, `!`, `[` with any whitespace between): refusal-only, so it
/// also refuses the text in a comment or string.
fn inner_attribute(source: &str) -> bool {
    let bytes = source.as_bytes();
    let skip = |mut at: usize| {
        while bytes.get(at).is_some_and(u8::is_ascii_whitespace) {
            at += 1;
        }
        at
    };
    (0..bytes.len()).any(|at| {
        bytes[at] == b'#' && bytes.get(skip(at + 1)) == Some(&b'!') && {
            let bang = skip(at + 1);
            bytes.get(skip(bang + 1)) == Some(&b'[')
        }
    })
}

/// The test `package::target::fn` in `file` is actually collected: one cargo metadata target of `package`
/// named `target`, `kind = ["test"]`, `test = true`, its source exactly `file`, no required features; and the
/// package manifest, parsed as TOML, leaves it on the default harness (any `[[test]]` entry naming it or its
/// path holds only `name`, `path` (equal), `harness = true`, `test = true` or empty `required-features`;
/// `autotests` is not false). A file holding an inner attribute is refused.
fn collected(tree: &dyn Tree, id: &str, file: &str, source: &str) -> Result<(), String> {
    let parts: Vec<&str> = id.split("::").collect();
    let [package, target, _] = parts[..] else {
        return Err("identity".into());
    };
    if inner_attribute(source) {
        return Err(format!("{file}: inner attribute"));
    }
    let metadata: serde_json::Value = serde_json::from_str(&tree.metadata()?)
        .map_err(|error| format!("cargo metadata: {error}"))?;
    let root = metadata["workspace_root"]
        .as_str()
        .ok_or("cargo metadata: workspace_root")?;
    let rel = |value: &serde_json::Value| {
        value
            .as_str()
            .and_then(|abs| abs.strip_prefix(root))
            .and_then(|rest| rest.strip_prefix('/'))
            .map(str::to_string)
    };
    let packages: Vec<&serde_json::Value> = metadata["packages"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|p| p["name"] == package)
        .collect();
    let [found] = packages[..] else {
        return Err(format!("{} packages named {package}", packages.len()));
    };
    let manifest = rel(&found["manifest_path"]).ok_or("manifest_path")?;
    let dir = manifest
        .strip_suffix("/Cargo.toml")
        .ok_or("manifest_path")?;
    let targets = found["targets"].as_array().ok_or("targets")?;
    let named: Vec<&serde_json::Value> = targets.iter().filter(|t| t["name"] == target).collect();
    let [collected] = named[..] else {
        return Err(format!("{} targets named {target}", named.len()));
    };
    let sharing = targets
        .iter()
        .filter(|t| rel(&t["src_path"]).as_deref() == Some(file))
        .count();
    if collected["kind"] != serde_json::json!(["test"])
        || collected["test"] != serde_json::json!(true)
        || rel(&collected["src_path"]).as_deref() != Some(file)
        || sharing != 1
        || collected
            .get("required-features")
            .is_some_and(|f| *f != serde_json::json!([]))
    {
        return Err(format!("{package}: target {target} is not a default test"));
    }
    let own = file
        .strip_prefix(&format!("{dir}/"))
        .ok_or("target outside its package")?;
    let toml: toml::Table = tree
        .read(&manifest)
        .ok_or("manifest unreadable")?
        .parse()
        .map_err(|error| format!("{manifest}: {error}"))?;
    if toml
        .get("package")
        .and_then(|p| p.get("autotests"))
        .is_some_and(|v| v.as_bool() != Some(true))
    {
        return Err(format!("{manifest}: autotests"));
    }
    let entries = match toml.get("test") {
        None => &Vec::new(),
        Some(toml::Value::Array(entries)) => entries,
        Some(_) => return Err(format!("{manifest}: test is not an array of tables")),
    };
    for entry in entries {
        let table = entry.as_table().ok_or("[[test]] entry")?;
        let name = table.get("name").and_then(toml::Value::as_str);
        let path = table.get("path").and_then(toml::Value::as_str);
        if name != Some(target) && path != Some(own) {
            continue;
        }
        for (key, value) in table {
            let default = match key.as_str() {
                "name" => name == Some(target),
                "path" => path == Some(own),
                "harness" | "test" => value.as_bool() == Some(true),
                "required-features" => value.as_array().is_some_and(Vec::is_empty),
                _ => false,
            };
            if !default {
                return Err(format!("{manifest}: [[test]] {target} {key}"));
            }
        }
    }
    Ok(())
}

/// A behaviour or parity test: its reviewed pin is fresh, the line above it is exactly `#[test]`, and it is
/// actually collected.
fn check_test(tree: &dyn Tree, id: &str, pin: &Pin) -> Result<(), Refusal> {
    let source = pin.check(tree)?;
    if source.split('\n').nth(pin.line - 2) != Some("#[test]") {
        return refuse(STALE_PIN, pin.site());
    }
    collected(tree, id, &pin.path, &source).map_err(|detail| Refusal {
        code: UNCOLLECTED_TEST,
        detail: format!("{id}: {detail}"),
    })
}

/// An oracle line: `(path, line)`.
type Line = (String, usize);

/// What a checked RU-053 record cites: every oracle line (site and hops), its own site and the site digest.
struct Checked {
    cited: BTreeSet<Line>,
    site: Line,
    digest: String,
}

/// Check one RU-053 record against the reviewed tables (mapping-only: no Rust source is parsed).
fn ru053_record(
    line: &str,
    tree: &dyn Tree,
    live: &Live,
    base: &BTreeSet<String>,
    reviewed: &Reviewed,
) -> Result<Checked, Refusal> {
    let f: Vec<_> = line.split('\t').collect();
    let key = f[0];
    if RU053_GUARDED.contains(&key) {
        return refuse("ru053-guarded-key", key);
    }
    let Some(candidate) = live.candidates.get(key) else {
        return refuse("ru053-unknown-key", key);
    };
    if !base.contains(key) {
        return refuse("ru053-not-pending", key);
    }
    let c: Vec<_> = candidate.split('\t').collect();
    let own = (c[1].to_string(), c[2].parse::<usize>().unwrap_or(0));
    if site(f[1]).as_ref() != Some(&own) {
        return refuse("ru053-site", format!("{key}: {}", f[1]));
    }
    if !is_hex64(f[2]) {
        return refuse("ru053-site-pin", format!("{key}: {}", f[2]));
    }
    // The Rust carrier: a product line whose text holds the token (refusal-only; the reviewed carrier row
    // below pins the line's exact bytes).
    let carrier = site(f[3]).filter(|(path, _)| product_source(path));
    let token = f[4];
    let resolved = carrier.as_ref().is_some_and(|(path, n)| {
        token != "-"
            && tree
                .read(path)
                .and_then(|text| text.lines().nth(n - 1).map(|l| l.contains(token)))
                .unwrap_or(false)
    });
    let Some(carrier) = carrier.filter(|_| resolved) else {
        return refuse(
            "ru053-unresolved-carrier",
            format!("{key}: {} {token}", f[3]),
        );
    };
    // The consumer hops: a reviewed root first, the carrier last, each a reviewed identity, none twice.
    let hops: Vec<&str> = if f[5] == "-" {
        Vec::new()
    } else {
        f[5].split(" > ").collect()
    };
    if hops.len() < 2 {
        return refuse("ru053-missing-consumer-mapping", key);
    }
    let mut cited = BTreeSet::from([own.clone()]);
    let mut ids: Vec<&str> = Vec::new();
    let mut last = None;
    for hop in &hops {
        let parsed = hop.split_once('=').and_then(|(rust, oracle)| {
            let (stem, _) = rust.split_once(".rs:")?;
            Some((rust, format!("{stem}.rs"), site(oracle)?))
        });
        let Some((id, path, oracle)) = parsed else {
            return refuse(UNRESOLVED_HOP, format!("{key}: {hop}"));
        };
        if !product_source(&path) {
            return refuse(NOT_PRODUCT, format!("{key}: {hop}"));
        }
        if !reviewed.roots.contains_key(id) && !reviewed.endpoints.contains(id) {
            return refuse(UNRESOLVED_HOP, format!("{key}: {hop}"));
        }
        if ids.contains(&id) {
            return refuse(DUPLICATE_HOP, format!("{key}: {hop}"));
        }
        ids.push(id);
        cited.insert(oracle.clone());
        last = Some((path, oracle));
    }
    if last != Some((carrier.0.clone(), own.clone())) {
        return refuse("ru053-consumer-carrier", key);
    }
    let end = ids[ids.len() - 1];
    let carrier_site = format!("{}:{}", carrier.0, carrier.1);
    let carrier_row = match reviewed.edges.get(&carrier_site) {
        Some(edge) if edge.from == end && edge.to == carrier_site => &edge.pin,
        _ => return refuse(OUTSIDE_ENDPOINT, format!("{key}: {carrier_site} {token}")),
    };
    let Some(root) = reviewed.roots.get(ids[0]) else {
        return refuse(UNMAPPED_EDGE, format!("{key}: root {}", ids[0]));
    };
    // The chain: per hop pair, the listed rows, contiguous by exact endpoint identity, no row listed twice.
    let calls: Vec<&str> = if f[6] == "-" {
        Vec::new()
    } else {
        f[6].split(" > ").collect()
    };
    let mut pins = vec![root, carrier_row];
    let mut listed = BTreeSet::new();
    for (n, pair) in ids.windows(2).enumerate() {
        let Some(list) = calls.get(n) else {
            return refuse(
                UNMAPPED_EDGE,
                format!("{key}: {} > {}", hops[n], hops[n + 1]),
            );
        };
        let mut at = pair[0];
        for step in list.split(',') {
            let Some(edge) = reviewed.edges.get(step) else {
                return refuse(UNMAPPED_EDGE, format!("{key}: {step}"));
            };
            if edge.from != at {
                return refuse(UNCONNECTED, format!("{key}: {step} from {}", edge.from));
            }
            if !listed.insert(step) {
                return refuse(DUPLICATE_ROW, format!("{key}: {step} listed twice"));
            }
            at = &edge.to;
            pins.push(&edge.pin);
        }
        if at != pair[1] {
            return refuse(UNCONNECTED, format!("{key}: {list} ends at {at}"));
        }
    }
    if calls.len() != ids.len() - 1 {
        return refuse(UNCONNECTED, format!("{key}: {} call lists", calls.len()));
    }
    // The test lists: each names a test at most once (exact identity, within the list), before any pin.
    let behaviour: Vec<&str> = f[7].split(" | ").collect();
    let parity: Vec<&str> = if f[8] == "-" {
        Vec::new()
    } else {
        f[8].split(" | ").collect()
    };
    for list in [&behaviour, &parity] {
        let mut named = BTreeSet::new();
        if let Some(test) = list.iter().find(|test| !named.insert(**test)) {
            return refuse(DUPLICATE_ROW, format!("{key}: {test} named twice"));
        }
    }
    for pin in &pins {
        pin.check(tree)?;
    }
    // The behaviour test (one or more) and the parity test(s) where one exists: reviewed, fresh, collected.
    if f[7] == "-" {
        return refuse("ru053-missing-behaviour-test", format!("{key}: -"));
    }
    for (test, missing) in behaviour
        .into_iter()
        .map(|t| (t, "ru053-missing-behaviour-test"))
        .chain(
            parity
                .into_iter()
                .map(|t| (t, "ru053-unresolved-parity-test")),
        )
    {
        let Some(pin) = reviewed.tests.get(test) else {
            return refuse(missing, format!("{key}: {test}"));
        };
        check_test(tree, test, pin)?;
        pins.push(pin);
    }
    // Every crate the record touches: its manifest and crate root, pinned.
    let mut crates = BTreeSet::new();
    for pin in &pins {
        match crate_of(&pin.path) {
            Some(name) => crates.insert(name),
            None => return refuse(UNMAPPED_CRATE, format!("{key}: {}", pin.path)),
        };
    }
    for name in &crates {
        for file in ["Cargo.toml", "src/lib.rs"] {
            let path = format!("{name}/{file}");
            let Some(pin) = reviewed.crates.get(&path) else {
                return refuse(UNMAPPED_CRATE, format!("{key}: {path}"));
            };
            pin.check(tree)?;
        }
    }
    Ok(Checked {
        cited,
        site: own,
        digest: f[2].to_string(),
    })
}

fn one<'r, 'a>(rows: &'r [&'r Row<'a>], kind: &str, epoch: &str) -> Result<&'r Row<'a>, Refusal> {
    let found: Vec<_> = rows.iter().filter(|r| r.kind == kind).collect();
    match found[..] {
        [row] => Ok(*row),
        _ => refuse(
            if kind == "manifest" {
                "manifest-pin"
            } else {
                "pins-file"
            },
            format!("{epoch}: {} {kind} rows", found.len()),
        ),
    }
}

/// Validate the whole catalogue against the sealed manifests and the checked-in tables.
pub fn validate_with(inputs: &Inputs) -> Result<Catalogue, Refusal> {
    let rows = catalogue_rows(inputs.catalogue)?;
    let mut order: Vec<&str> = Vec::new();
    for row in &rows {
        if order.last() != Some(&row.epoch) {
            if order.contains(&row.epoch) {
                return refuse("epoch-order", format!("{} is not contiguous", row.epoch));
            }
            order.push(row.epoch);
        }
    }
    let expected: Vec<&str> = EPOCHS.iter().map(|e| e.id).collect();
    if order != expected {
        return refuse("epoch-order", order.join(","));
    }
    let live = live()?;
    let reviewed = Reviewed::load(inputs.tree)?;
    let text_of = |spec: &EpochSpec| match spec.layout {
        Layout::Ru053 => inputs.ru053,
        _ => spec.manifest,
    };
    // Base reviews: every review record outside the historical cohorts' sealed key sets.
    let mut historical = BTreeSet::new();
    for spec in EPOCHS.iter().filter(|e| e.layout != Layout::Ru053) {
        historical.extend(sealed(spec, spec.manifest)?.keys);
    }
    let mut prior_pins: BTreeSet<(String, usize)> = BTreeSet::new();
    let mut prior_ids: BTreeSet<String> = BTreeSet::new();
    for (key, review) in &live.reviews {
        if !historical.contains(key) {
            prior_pins.extend(review.cited.keys().cloned());
            prior_ids.extend(review.targets.iter().cloned());
        }
    }
    // The pre-RU-053 base: the live census minus the reviews of every cohort up to the last historical one.
    let mut base_census = live.census.clone();
    let mut base_reviewed = live.reviewed.clone();
    let last = EPOCHS
        .iter()
        .position(|e| e.id == LAST_HISTORICAL)
        .unwrap_or(EPOCHS.len());
    for spec in EPOCHS
        .get(last + 1..)
        .unwrap_or(&[])
        .iter()
        .filter(|e| e.layout != Layout::Ru053)
    {
        let s = sealed(spec, spec.manifest)?;
        for key in &s.keys {
            base_reviewed.remove(key);
        }
        for key in s.census.keys() {
            base_census.remove(key);
        }
    }
    let base: BTreeSet<String> = base_census.difference(&base_reviewed).cloned().collect();

    let mut seen_keys = BTreeSet::new();
    let mut epochs = Vec::new();
    for spec in EPOCHS {
        let mine: Vec<&Row> = rows.iter().filter(|r| r.epoch == spec.id).collect();
        let text = text_of(spec);
        let manifest = one(&mine, "manifest", spec.id)?;
        let s = sealed(spec, text)?;
        if manifest.identity != spec.manifest_name
            || manifest.number != s.rows
            || manifest.sha256 != sha(text)
        {
            return refuse("manifest-pin", spec.id);
        }
        // Ordered keys, exactly the manifest's.
        let declared: Vec<(&str, &str)> = mine
            .iter()
            .filter(|r| r.kind == "key")
            .map(|r| (r.identity, r.sha256))
            .collect();
        let names: Vec<&str> = declared.iter().map(|(k, _)| *k).collect();
        if let Some(key) = s.keys.iter().find(|k| !names.contains(&k.as_str())) {
            return refuse("dropped-key", format!("{}: {key}", spec.id));
        }
        if let Some(key) = names.iter().find(|k| !s.keys.iter().any(|m| m == *k)) {
            return refuse("extra-key", format!("{}: {key}", spec.id));
        }
        if names != s.keys {
            return refuse("key-order", spec.id);
        }
        for key in &names {
            if !seen_keys.insert(key.to_string()) {
                return refuse("duplicate-key", format!("{}: {key}", spec.id));
            }
        }
        // Each key's exact record: the catalogue digest, the manifest's sealed digest and the live line agree.
        for (key, digest) in &declared {
            let line = match spec.layout {
                Layout::Ru053 => s.lines.get(*key).map(String::as_str),
                _ => live.reviews.get(*key).map(|r| r.line.as_str()),
            };
            let Some(line) = line else {
                return refuse("missing-record", format!("{}: {key}", spec.id));
            };
            if sha(line) != *digest || s.records.get(*key).map(String::as_str) != Some(*digest) {
                return refuse("record-hash", format!("{}: {key}", spec.id));
            }
        }
        // Discovery cohorts: exactly the census keys they added, each bound to its census line.
        let census: BTreeMap<String, String> = mine
            .iter()
            .filter(|r| r.kind == "census")
            .map(|r| (r.identity.to_string(), r.sha256.to_string()))
            .collect();
        if census.keys().ne(s.census.keys()) {
            return refuse("census-key", spec.id);
        }
        for (key, digest) in &census {
            let line = live.candidates.get(key).map(|l| sha(l));
            if line.as_ref() != Some(digest) || s.census.get(key) != Some(digest) {
                return refuse("census-hash", format!("{}: {key}", spec.id));
            }
        }
        // What the records cite (RU-053: semantic record checks first).
        let mut cited: BTreeMap<(String, usize), Option<String>> = BTreeMap::new();
        let mut sites = Vec::new();
        let mut targets = BTreeSet::new();
        for key in &names {
            match spec.layout {
                Layout::Ru053 => {
                    let checked =
                        ru053_record(&s.lines[*key], inputs.tree, live, &base, &reviewed)?;
                    cited.extend(checked.cited.into_iter().map(|l| (l, None)));
                    sites.push((checked.site, checked.digest));
                }
                _ => {
                    let review = &live.reviews[*key];
                    for (line, digest) in &review.cited {
                        cited.insert(line.clone(), Some(digest.clone()));
                    }
                    targets.extend(review.targets.iter().cloned());
                }
            }
        }
        // The exact added pin set: declared == cited minus every earlier pin.
        let pins = match spec.layout {
            Layout::Keyed { .. } => {
                if mine.iter().any(|r| r.kind == "pins") {
                    return refuse("pins-file", spec.id);
                }
                let mut pins = PinMap::new();
                for r in mine.iter().filter(|r| r.kind == "pin") {
                    if pins
                        .insert((r.identity.to_string(), r.number), r.sha256.to_string())
                        .is_some()
                    {
                        return refuse(
                            "extra-pin",
                            format!("{}: {}:{}", spec.id, r.identity, r.number),
                        );
                    }
                }
                pins
            }
            Layout::Kinded | Layout::Ru053 => {
                let (name, file) = match spec.layout {
                    Layout::Kinded => (spec.manifest_name, text),
                    _ => (RU053_PINS_NAME, inputs.ru053_pins),
                };
                let pins = match spec.layout {
                    Layout::Kinded => s.pins.clone(),
                    _ => pins_file(file)?,
                };
                let row = one(&mine, "pins", spec.id)?;
                if mine.iter().any(|r| r.kind == "pin")
                    || row.identity != name
                    || row.number != pins.len()
                    || row.sha256 != sha(file)
                {
                    return refuse("pins-file", spec.id);
                }
                pins
            }
        };
        let mut before = prior_pins.clone();
        if spec.layout == Layout::Ru053 {
            before.extend(live.pins.keys().cloned());
        }
        let added: BTreeSet<&(String, usize)> = cited
            .keys()
            .filter(|line| !before.contains(*line))
            .collect();
        if let Some((path, n)) = added.iter().find(|line| !pins.contains_key(**line)) {
            return refuse("missing-pin", format!("{}: {path}:{n}", spec.id));
        }
        if let Some(((path, n), _)) = pins.iter().find(|(line, _)| !added.contains(line)) {
            return refuse("extra-pin", format!("{}: {path}:{n}", spec.id));
        }
        for (line, digest) in &pins {
            let live_digest = live.pins.get(line);
            let cited_digest = cited.get(line).cloned().flatten();
            let ok = match spec.layout {
                Layout::Ru053 => live_digest.is_none(),
                _ => live_digest == Some(digest) && cited_digest.as_ref() == Some(digest),
            };
            if !ok || !is_hex64(digest) {
                return refuse("pin-digest", format!("{}: {}:{}", spec.id, line.0, line.1));
            }
        }
        for (own, digest) in &sites {
            let pinned = pins.get(own).or_else(|| live.pins.get(own));
            match pinned {
                None => return refuse("missing-pin", format!("{}: {}:{}", spec.id, own.0, own.1)),
                Some(pin) if pin != digest => {
                    return refuse(
                        "ru053-site-pin",
                        format!("{}: {}:{}", spec.id, own.0, own.1),
                    );
                }
                Some(_) => {}
            }
        }
        prior_pins.extend(cited.keys().cloned());
        // The exact added registry ids: declared == named targets minus every earlier one.
        let ids: BTreeSet<String> = mine
            .iter()
            .filter(|r| r.kind == "id")
            .map(|r| r.identity.to_string())
            .collect();
        if let Some(id) = ids.iter().find(|id| !live.registry.contains(*id)) {
            return refuse("unknown-id", format!("{}: {id}", spec.id));
        }
        let derived: BTreeSet<String> = targets.difference(&prior_ids).cloned().collect();
        if let Some(id) = derived.iter().find(|id| !ids.contains(*id)) {
            return refuse("missing-id", format!("{}: {id}", spec.id));
        }
        if let Some(id) = ids.iter().find(|id| !derived.contains(*id)) {
            return refuse("extra-id", format!("{}: {id}", spec.id));
        }
        prior_ids.extend(targets);
        // The pinned pre-RU-053 base lives on the RU-053 epoch, and only there.
        let bases: Vec<_> = mine.iter().filter(|r| r.kind == "base").collect();
        if spec.layout == Layout::Ru053 {
            let joined = base.iter().cloned().collect::<Vec<_>>().join("\n");
            match bases[..] {
                [row]
                    if row.identity == "pending"
                        && row.number == base.len()
                        && row.sha256 == sha(&joined) => {}
                _ => return refuse("base-pin", format!("{} pending", base.len())),
            }
        } else if !bases.is_empty() {
            return refuse("base-pin", spec.id);
        }
        if let Some(row) = mine.iter().find(|r| {
            !matches!(
                r.kind,
                "manifest" | "key" | "census" | "pin" | "pins" | "id" | "base"
            )
        }) {
            return refuse("catalogue-shape", format!("{}: {}", spec.id, row.kind));
        }
        epochs.push(Epoch {
            id: spec.id.to_string(),
            manifest: spec.manifest_name.to_string(),
            manifest_sha256: manifest.sha256.to_string(),
            keys: names.iter().map(|k| k.to_string()).collect(),
            records: declared
                .iter()
                .map(|(k, d)| (k.to_string(), d.to_string()))
                .collect(),
            census,
            pins,
            ids,
        });
    }
    Ok(Catalogue { epochs, base })
}

/// The checked-in catalogue, validated once.
pub fn catalogue() -> Result<Catalogue, Refusal> {
    static CATALOGUE_VALID: OnceLock<Result<Catalogue, Refusal>> = OnceLock::new();
    CATALOGUE_VALID
        .get_or_init(|| validate_with(&Inputs::production()))
        .clone()
}

/// The validated ordered keys one epoch admitted.
pub fn admitted(epoch: &str) -> Result<Vec<String>, Refusal> {
    match catalogue()?.epoch(epoch) {
        Some(e) => Ok(e.keys.clone()),
        None => refuse("unknown-epoch", epoch),
    }
}

fn position(catalogue: &Catalogue, epoch: &str) -> Result<usize, Refusal> {
    match catalogue.epochs.iter().position(|e| e.id == epoch) {
        Some(at) => Ok(at),
        None => refuse("unknown-epoch", epoch),
    }
}

fn projection<T>(detail: impl Into<String>) -> Result<T, Refusal> {
    refuse("projection", detail)
}

/// The live view (RU-053 admissions counted as reviewed) with every cohort after `epoch` removed.
pub fn as_of_with(inputs: &Inputs, epoch: &str) -> Result<View, Refusal> {
    let catalogue = validate_with(inputs)?;
    project(&catalogue, epoch)
}

fn project(catalogue: &Catalogue, epoch: &str) -> Result<View, Refusal> {
    let at = position(catalogue, epoch)?;
    let live = live()?;
    let mut view = View {
        census: live.census.clone(),
        reviewed: live.reviewed.clone(),
        pins: live.pins.clone(),
        ids: live.registry.clone(),
    };
    for e in catalogue.epochs.iter().filter(|e| e.id == RU053) {
        for key in &e.keys {
            if !view.reviewed.insert(key.clone()) {
                return projection(format!("RU-053 key already reviewed: {key}"));
            }
        }
        for (line, digest) in &e.pins {
            if view.pins.insert(line.clone(), digest.clone()).is_some() {
                return projection(format!("RU-053 pin already pinned: {}:{}", line.0, line.1));
            }
        }
    }
    for e in &catalogue.epochs[at + 1..] {
        for key in &e.keys {
            if !view.reviewed.remove(key) {
                return projection(format!("{}: review {key}", e.id));
            }
        }
        for key in e.census.keys() {
            if !view.census.remove(key) {
                return projection(format!("{}: census {key}", e.id));
            }
        }
        for (line, digest) in &e.pins {
            if view.pins.remove(line).as_ref() != Some(digest) {
                return projection(format!("{}: pin {}:{}", e.id, line.0, line.1));
            }
        }
        for id in &e.ids {
            if !view.ids.remove(id) {
                return projection(format!("{}: id {id}", e.id));
            }
        }
    }
    Ok(view)
}

/// The checked-in catalogue's view as of `epoch`.
pub fn as_of(epoch: &str) -> Result<View, Refusal> {
    project(&catalogue()?, epoch)
}

/// What every cohort after `epoch` added: census keys, reviews, pins and registry ids.
pub fn added_after_with(inputs: &Inputs, epoch: &str) -> Result<View, Refusal> {
    added(&validate_with(inputs)?, epoch)
}

fn added(catalogue: &Catalogue, epoch: &str) -> Result<View, Refusal> {
    let at = position(catalogue, epoch)?;
    let mut view = View::default();
    for e in &catalogue.epochs[at + 1..] {
        view.reviewed.extend(e.keys.iter().cloned());
        view.census.extend(e.census.keys().cloned());
        view.pins
            .extend(e.pins.iter().map(|(l, d)| (l.clone(), d.clone())));
        view.ids.extend(e.ids.iter().cloned());
    }
    Ok(view)
}

pub fn added_after(epoch: &str) -> Result<View, Refusal> {
    added(&catalogue()?, epoch)
}

fn ru053_keys(catalogue: &Catalogue) -> BTreeSet<String> {
    catalogue
        .epoch(RU053)
        .map(|e| e.keys.iter().cloned().collect())
        .unwrap_or_default()
}

/// The RU-053 keys whose records validate (with the whole catalogue).
pub fn ru053_admitted_with(inputs: &Inputs) -> Result<BTreeSet<String>, Refusal> {
    Ok(ru053_keys(&validate_with(inputs)?))
}

pub fn ru053_admitted() -> Result<BTreeSet<String>, Refusal> {
    Ok(ru053_keys(&catalogue()?))
}

/// The closure violations: every base key not admitted, as `require_complete` words them.
pub fn closure(base: &BTreeSet<String>, admitted: &BTreeSet<String>) -> Vec<String> {
    base.difference(admitted)
        .map(|key| format!("{UNREVIEWED}{key}"))
        .collect()
}

/// Evaluate a catalogue: the pinned pre-RU-053 base minus its validated RU-053 admissions.
pub fn evaluate_with(inputs: &Inputs) -> Result<Vec<String>, Refusal> {
    let catalogue = validate_with(inputs)?;
    Ok(closure(&catalogue.base, &ru053_keys(&catalogue)))
}

/// The catalogue's projected closure: the pinned pre-RU-053 base minus the validated admitted RU-053 keys.
pub fn projected_closure() -> Result<Vec<String>, Refusal> {
    let catalogue = catalogue()?;
    Ok(closure(&catalogue.base, &ru053_keys(&catalogue)))
}
