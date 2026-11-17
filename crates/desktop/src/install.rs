// SPDX-License-Identifier: AGPL-3.0-only
//! Per-user install / upgrade / rollback / uninstall, ported from the v0.1.4 macOS installer
//! (`mycelium_a13_packaging/installer_core.py` and `mycelium_a13_macos/installer.py` at
//! f4e366e9). Same layout (under v0.2's own folder, owner ruling 25), same record file, same verify-before-write order, same refusal codes,
//! and the same signed package documents (`crate::docs`: Ed25519 manifest, install source, key pin,
//! rollback floor). A same-version reinstall drops the rollback target, an uninstall with a damaged
//! record refuses, the register-target check runs after the install like v0.1.4's, the running
//! version's directory is unlinked by uninstall (deferred self-removal), and the payload copy is
//! `/usr/bin/ditto`. Differences that remain are listed in `L11-DESKTOP.md`.
//!
//! The installer never reads or writes inside the user-state root: it only makes the (empty,
//! owner-only) folder when it is absent. Explicit roots (`Roots::new`) refuse the real home and
//! the real `/Applications` and `~/Applications`; only the product entry point
//! (`Roots::for_current_user`, `HOME`) may name the real home.
use crate::docs;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt, symlink},
    path::{Component, Path, PathBuf},
    process::Command,
};

pub const PROTOCOL: &str = "mycelium.a13_macos_installer.v1";
// Owner ruling 25: v0.2 has its own identity so it sits beside v0.1.4 and never touches its
// files (ruling 20). v0.1.4 uses `~/Applications/Mycelium A13.app`, the payload `Mycelium A13
// Demo.app`, and `Application Support/{Mycelium/{programs,service}, Mycelium A13 macOS Demo,
// Mycelium A13 Demo}`; every v0.2 name below differs from all of them, and the data folder does
// not even begin with `Mycelium`, so no prefix match on v0.1.4's folders can include it.
pub const PAYLOAD_DIRECTORY: &str = "Mycelium.app";
pub const APPLICATIONS_ENTRY: &str = "Mycelium.app";
/// v0.2's one folder under `~/Library/Application Support` (named by its bundle identifier, the
/// macOS convention); it holds `programs/` and `state/`.
pub const SUPPORT_FOLDER: &str = "org.mycelium.app";
pub const FIRST_USE_PIN: &str = docs::FIRST_USE_PIN;
/// v0.1.4 (PyInstaller) and v0.2 executables; the only names an installed payload may carry.
const EXECUTABLES: [&str; 2] = ["Mycelium A13 Demo", "mycelium"];
const RECORD: &str = "versions.json";
const LOCK: &str = ".install.lock";
const PARTIAL: &str = ".partial";
const STALE: &str = ".stale-";
const MAX_RECORD_BYTES: u64 = 64 * 1024;

#[derive(Debug, Eq, PartialEq)]
pub struct Refusal(pub &'static str);
pub type Result<T> = std::result::Result<T, Refusal>;
fn refuse<T>(code: &'static str) -> Result<T> {
    Err(Refusal(code))
}
/// A raw `OSError` in the oracle: `install_io_error`.
fn io_err(_: std::io::Error) -> Refusal {
    Refusal("install_io_error")
}
/// A `HostPlatformError` in the oracle is not an `OSError`: the generic `installer_failed`.
fn host_err<T>() -> Result<T> {
    refuse("installer_failed")
}

// ---- roots -------------------------------------------------------------------------------

#[derive(Debug)]
pub struct Roots {
    home: PathBuf,
    applications: PathBuf,
}
fn same_place(a: &Path, b: &Path) -> bool {
    match (a.canonicalize(), b.canonicalize()) {
        (Ok(a), Ok(b)) => a == b,
        _ => a == b,
    }
}
impl Roots {
    /// Both paths absolute; `home` must exist. The caller's real home and real Applications
    /// folders are refused so a scratch run can never be pointed at them by accident.
    pub fn new(home: &Path, applications: &Path) -> Result<Self> {
        if !home.is_absolute() || !applications.is_absolute() || !home.is_dir() {
            return refuse("install_root_invalid");
        }
        if let Some(real) = std::env::var_os("HOME").map(PathBuf::from) {
            if same_place(home, &real) {
                return refuse("install_root_is_real_home");
            }
            if same_place(applications, &real.join("Applications")) {
                return refuse("install_root_is_real_applications");
            }
        }
        if same_place(applications, Path::new("/Applications")) {
            return refuse("install_root_is_real_applications");
        }
        Ok(Self {
            home: home.into(),
            applications: applications.into(),
        })
    }
    /// The product's roots: `$HOME` and `$HOME/Applications`, as the v0.1.4 installer
    /// (`Path.home()`). Used only by the app's own `--install` family; every script and test
    /// that must not touch the real home runs the app with `HOME` pointing at a scratch folder.
    pub fn for_current_user() -> Result<Self> {
        let Some(home) = std::env::var_os("HOME").map(PathBuf::from) else {
            return refuse("install_root_invalid");
        };
        if !home.is_absolute() || !home.is_dir() {
            return refuse("install_root_invalid");
        }
        let applications = home.join("Applications");
        Ok(Self { home, applications })
    }
    fn support(&self) -> PathBuf {
        self.home.join("Library/Application Support")
    }
    pub fn programs(&self) -> PathBuf {
        self.support().join(SUPPORT_FOLDER).join("programs")
    }
    pub fn state(&self) -> PathBuf {
        self.support().join(SUPPORT_FOLDER).join("state")
    }
    pub fn application_entry(&self) -> PathBuf {
        self.applications.join(APPLICATIONS_ENTRY)
    }
    pub fn version_dir(&self, version: &str) -> PathBuf {
        self.programs().join(version)
    }
}

// ---- seams (every OS effect) ------------------------------------------------------------------

#[derive(Clone, Debug)]
pub struct Observed {
    pub system: String,
    pub machine: String,
    pub macos_version: String,
}
/// Copies one tree to a destination that does not exist yet.
pub type CopyFn = Box<dyn Fn(&Path, &Path) -> std::io::Result<()>>;
pub struct Seams {
    pub observe: Box<dyn Fn() -> Observed>,
    pub now: Box<dyn Fn() -> f64>,
    /// Remove `com.apple.quarantine` from a staged, digest-verified copy; `false` when it could not.
    pub clear_quarantine: Box<dyn Fn(&Path) -> bool>,
    pub copy: CopyFn,
    /// The running executable, links resolved (uninstall unlinks the version it runs from).
    pub running_executable: Box<dyn Fn() -> PathBuf>,
}
impl Seams {
    pub fn host() -> Self {
        Self {
            observe: Box::new(|| Observed {
                system: if cfg!(target_os = "macos") {
                    "Darwin".into()
                } else {
                    std::env::consts::OS.into()
                },
                machine: if cfg!(target_arch = "aarch64") {
                    "arm64".into()
                } else {
                    std::env::consts::ARCH.into()
                },
                macos_version: Command::new("/usr/bin/sw_vers")
                    .arg("-productVersion")
                    .output()
                    .ok()
                    .and_then(|o| String::from_utf8(o.stdout).ok())
                    .unwrap_or_default()
                    .trim()
                    .to_string(),
            }),
            now: Box::new(|| {
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map_or(0.0, |d| d.as_secs_f64())
            }),
            clear_quarantine: Box::new(|path| {
                Command::new("/usr/bin/xattr")
                    .args(["-d", "-r", "-s", "com.apple.quarantine"])
                    .arg(path)
                    .output()
                    .is_ok_and(|o| o.status.success())
            }),
            copy: Box::new(ditto_copy),
            running_executable: Box::new(|| {
                std::env::current_exe()
                    .and_then(|p| p.canonicalize())
                    .unwrap_or_default()
            }),
        }
    }
}

/// `/usr/bin/ditto <source> <destination>`, exactly the v0.1.4 call: links, modes, extended
/// attributes, ACLs and resource forks are ditto's. Output is inherited, as in v0.1.4.
pub fn ditto_copy(from: &Path, to: &Path) -> std::io::Result<()> {
    let status = Command::new("/usr/bin/ditto").arg(from).arg(to).status()?;
    if status.success() {
        Ok(())
    } else {
        Err(std::io::Error::other("ditto failed"))
    }
}

// ---- digest (byte-compatible with Python `build.bundle_digest`) -------------------------------

fn lexical(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for part in path.components() {
        match part {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other),
        }
    }
    out
}
fn file_sha256(path: &Path) -> std::io::Result<String> {
    let mut file = File::open(path)?;
    let mut hash = Sha256::new();
    let mut buffer = vec![0u8; 1 << 20];
    loop {
        let n = file.read(&mut buffer)?;
        if n == 0 {
            return Ok(format!("{:x}", hash.finalize()));
        }
        hash.update(&buffer[..n]);
    }
}
fn walk_tree(
    base: &Path,
    root: &Path,
    dir: &Path,
    out: &mut BTreeMap<String, String>,
) -> Result<()> {
    for child in fs::read_dir(dir).map_err(io_err)? {
        let child = child.map_err(io_err)?;
        let path = child.path();
        let relative = path
            .strip_prefix(root)
            .map_err(|_| Refusal("install_io_error"))?
            .to_string_lossy()
            .into_owned();
        let kind = child.file_type().map_err(io_err)?;
        if kind.is_symlink() {
            let target = fs::read_link(&path).map_err(io_err)?;
            if target.is_absolute() {
                return refuse("payload_link_absolute");
            }
            let parent = path.parent().unwrap_or(root);
            let inside = |p: &Path| p == base || p.starts_with(base);
            let parent_relative = parent.strip_prefix(root).unwrap_or(Path::new(""));
            if !inside(&lexical(&base.join(parent_relative).join(&target))) {
                return refuse("payload_link_escapes_bundle");
            }
            let Ok(resolved) = path.canonicalize() else {
                return refuse("payload_link_dangling");
            };
            if !inside(&resolved) {
                return refuse("payload_link_escapes_bundle");
            }
            out.insert(relative, format!("link:{}", target.to_string_lossy()));
        } else if kind.is_file() {
            out.insert(
                relative,
                format!("sha256:{}", file_sha256(&path).map_err(io_err)?),
            );
        } else if kind.is_dir() {
            walk_tree(base, root, &path, out)?;
        } else {
            return refuse("payload_special_file");
        }
    }
    Ok(())
}
/// Python `json.dumps(ensure_ascii=True)` string escaping.
fn python_json_string(text: &str, out: &mut String) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) >= 0x80 => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}
/// Link-aware digest of an `.app`: `sha256:<hex>` per regular file, `link:<target>` per link;
/// absolute links, links leaving the bundle, dangling links and special files are refused.
pub fn bundle_digest(root: &Path) -> Result<String> {
    let base = root.canonicalize().map_err(io_err)?;
    let mut tree = BTreeMap::new();
    walk_tree(&base, root, root, &mut tree)?;
    let mut canonical = String::from("{");
    for (index, (path, value)) in tree.iter().enumerate() {
        if index > 0 {
            canonical.push(',');
        }
        python_json_string(path, &mut canonical);
        canonical.push(':');
        python_json_string(value, &mut canonical);
    }
    canonical.push('}');
    Ok(format!("sha256:{:x}", Sha256::digest(canonical.as_bytes())))
}

// ---- the installation record (`versions.json`, v0.1.4-shaped, kept as JSON so nothing is dropped) ---

type Rec = Map<String, Value>;
fn field<'a>(record: &'a Rec, key: &str) -> Option<&'a str> {
    record.get(key).and_then(Value::as_str)
}
fn entry_shaped(value: &Value) -> bool {
    value
        .as_object()
        .is_some_and(|e| field(e, "version").is_some() && field(e, "payload_digest").is_some())
}
/// `_record_shaped`: the fields install/rollback index into; anything else is a damaged record.
fn record_shaped(record: &Rec) -> bool {
    record.get("current").is_some_and(entry_shaped)
        && record
            .get("previous")
            .is_none_or(|p| p.is_null() || entry_shaped(p))
        && [
            "rollback_floor",
            "publisher_key_digest",
            "publisher_pin_source",
        ]
        .iter()
        .all(|k| field(record, k).is_some())
}
fn current_of(record: &Rec) -> &Rec {
    record["current"].as_object().unwrap_or(record)
}
fn previous_of(record: &Rec) -> Option<&Rec> {
    record.get("previous").and_then(Value::as_object)
}
fn version_of(entry: &Rec) -> &str {
    field(entry, "version").unwrap_or_default()
}

/// The oracle's `open_private_directory` walk: no link anywhere on the path, every missing part
/// made `0700` when `create`, the final directory exactly `0700` and ours. `Ok(false)`: the final
/// part is absent and `create` is off. Path-based (the oracle holds descriptors).
fn private_walk(path: &Path, create: bool, owner: u32) -> std::result::Result<bool, ()> {
    let path = lexical(path);
    if !path.is_absolute() || path == Path::new("/") {
        return Err(());
    }
    let parts: Vec<_> = path.components().skip(1).collect();
    let mut current = PathBuf::from("/");
    for (index, part) in parts.iter().enumerate() {
        let final_part = index == parts.len() - 1;
        current.push(part);
        match fs::symlink_metadata(&current) {
            Ok(meta) => {
                if meta.file_type().is_symlink() || !meta.is_dir() {
                    return Err(());
                }
                if final_part && (meta.uid() != owner || meta.mode() & 0o7777 != 0o700) {
                    return Err(());
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                if final_part && !create {
                    return Ok(false);
                }
                if !create {
                    return Err(());
                }
                fs::DirBuilder::new()
                    .mode(0o700)
                    .create(&current)
                    .map_err(|_| ())?;
                fs::set_permissions(&current, fs::Permissions::from_mode(0o700)).map_err(|_| ())?;
            }
            Err(_) => return Err(()),
        }
    }
    Ok(true)
}
fn owner_of(roots: &Roots) -> Result<u32> {
    Ok(fs::metadata(&roots.home).map_err(io_err)?.uid())
}
fn private_dir(path: &Path, owner: u32) -> Result<()> {
    match private_walk(path, true, owner) {
        Ok(_) => Ok(()),
        Err(()) => host_err(),
    }
}

/// `read_versions`: absent -> `None`; an untrusted directory/file -> `installation_record_untrusted`;
/// unreadable JSON or a record that is not v0.1.4-shaped -> `installation_record_invalid`.
fn read_record(roots: &Roots) -> Result<Option<Rec>> {
    let programs = roots.programs();
    if fs::symlink_metadata(&programs).is_err_and(|e| e.kind() == std::io::ErrorKind::NotFound) {
        return Ok(None);
    }
    let untrusted = || Refusal("installation_record_untrusted");
    let owner = owner_of(roots)?;
    if !matches!(private_walk(&programs, false, owner), Ok(true)) {
        return Err(untrusted());
    }
    let path = programs.join(RECORD);
    let meta = match fs::symlink_metadata(&path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(untrusted()),
        Ok(meta) => meta,
    };
    if !meta.file_type().is_file()
        || meta.nlink() != 1
        || meta.uid() != owner
        || meta.mode() & 0o7777 != 0o600
        || meta.len() > MAX_RECORD_BYTES
    {
        return Err(untrusted());
    }
    let bytes = read_checked(&path, &meta).ok_or_else(untrusted)?;
    let invalid = || Refusal("installation_record_invalid");
    let Ok(Value::Object(record)) = serde_json::from_slice::<Value>(&bytes) else {
        return Err(invalid());
    };
    if field(&record, "protocol") != Some(PROTOCOL) || !record_shaped(&record) {
        return Err(invalid());
    }
    Ok(Some(record))
}
/// The record file whose path metadata `checked` was admitted, read through one descriptor (lane
/// VL patch round 1, REVIEW-laneUI-astra VL-01): opened without following a link or waiting on a
/// FIFO (`O_NOFOLLOW | O_NONBLOCK`), and read only when the opened object itself is that same
/// single-link, owner-only regular file within the size bound, so a swap between the path check
/// and the open is refused, never waited on. `None`: anything else.
fn read_checked(path: &Path, checked: &fs::Metadata) -> Option<Vec<u8>> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .ok()?;
    let opened = file.metadata().ok()?;
    if !opened.file_type().is_file()
        || (opened.dev(), opened.ino()) != (checked.dev(), checked.ino())
        || opened.nlink() != 1
        || opened.uid() != checked.uid()
        || opened.mode() & 0o7777 != 0o600
        || opened.len() > MAX_RECORD_BYTES
    {
        return None;
    }
    let mut bytes = Vec::new();
    file.take(MAX_RECORD_BYTES + 1)
        .read_to_end(&mut bytes)
        .ok()?;
    (bytes.len() as u64 == opened.len()).then_some(bytes)
}
fn write_record(roots: &Roots, record: &Rec) -> Result<()> {
    let programs = roots.programs();
    private_dir(&programs, owner_of(roots)?)?;
    let bytes = serde_json::to_vec_pretty(record).map_err(|_| Refusal("installer_failed"))?;
    let temporary = programs.join(format!(".{RECORD}.{}.tmp", std::process::id()));
    remove_any(&temporary).map_err(io_err)?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)
        .map_err(io_err)?;
    file.write_all(&bytes).map_err(io_err)?;
    file.sync_all().map_err(io_err)?;
    fs::rename(&temporary, programs.join(RECORD)).map_err(io_err)
}

// ---- lock ----------------------------------------------------------------------------------------

/// Exclusive, non-blocking advisory lock; the OS drops it when the holder exits or is killed.
#[derive(Debug)]
pub struct LockGuard(Option<File>);
/// B12 (lane B): the lock is released explicitly. A `flock` belongs to the open file description,
/// which a child forked while the lock was held shares until its exec closes its copy: closing
/// only this descriptor would leave the lock held for that moment, and an install or rollback
/// right after would be refused `install_already_running`.
impl Drop for LockGuard {
    fn drop(&mut self) {
        if let Some(file) = &self.0
            && file.unlock().is_err()
        {
            eprintln!("install_lock_release_failed");
        }
    }
}
fn lock(roots: &Roots, create: bool) -> Result<LockGuard> {
    let programs = roots.programs();
    if !create && !programs.is_dir() {
        return Ok(LockGuard(None));
    }
    let owner = owner_of(roots)?;
    if create {
        private_dir(&programs, owner)?;
    } else if !matches!(private_walk(&programs, false, owner), Ok(true)) {
        return host_err();
    }
    let path = programs.join(LOCK);
    if fs::symlink_metadata(&path).is_ok_and(|m| !m.file_type().is_file()) {
        return host_err();
    }
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(&path)
        .or_else(|_| host_err())?;
    let meta = file.metadata().or_else(|_| host_err())?;
    if meta.uid() != owner || meta.mode() & 0o077 != 0 {
        return host_err();
    }
    match file.try_lock() {
        Ok(()) => Ok(LockGuard(Some(file))),
        Err(std::fs::TryLockError::WouldBlock) => refuse("install_already_running"),
        Err(std::fs::TryLockError::Error(_)) => host_err(),
    }
}
pub fn hold_lock(roots: &Roots) -> Result<LockGuard> {
    lock(roots, true)
}

// ---- verification (no writes) -----------------------------------------------------------------

struct Checked {
    version: String,
    payload_digest: String,
    rollback_floor: String,
    publisher_key_digest: String,
    publisher_pin_source: String,
    source_revision: String,
    developer_id_team: String,
    macos_version: String,
}
fn numeric(version: &str) -> Option<Vec<u64>> {
    version
        .split_once('-')
        .map_or(version, |(head, _)| head)
        .split('.')
        .map(|p| p.parse().ok())
        .collect()
}
/// `numeric()` raises `ValueError` in the oracle on a non-numeric version: `installer_failed`.
fn numeric_or_fail(version: &str) -> Result<Vec<u64>> {
    numeric(version).ok_or(Refusal("installer_failed"))
}
fn has_executable(payload: &Path) -> bool {
    EXECUTABLES.iter().any(|name| {
        // `Path.is_file()` follows links, as the oracle's check does.
        fs::metadata(payload.join("Contents/MacOS").join(name)).is_ok_and(|m| m.is_file())
    })
}
fn ours(roots: &Roots, entry: &Path) -> bool {
    fs::symlink_metadata(entry).is_ok_and(|m| m.file_type().is_symlink())
        && fs::read_link(entry).is_ok_and(|t| t.is_absolute() && t.starts_with(roots.programs()))
}
fn verify_bundle(bundle: &Path, seams: &Seams, installed: Option<&Rec>) -> Result<Checked> {
    let observed = (seams.observe)();
    let pin = installed.map(|r| {
        (
            field(r, "publisher_key_digest").unwrap_or_default(),
            field(r, "publisher_pin_source").unwrap_or_default(),
            field(r, "rollback_floor").unwrap_or_default(),
        )
    });
    let checked = docs::verify_documents(bundle, &observed, pin, (seams.now)())?;
    if let Some(record) = installed
        && numeric_or_fail(&checked.version)?
            < numeric_or_fail(field(record, "rollback_floor").unwrap_or_default())?
    {
        return refuse("package_downgrade_rejected");
    }
    let payload = bundle.join(PAYLOAD_DIRECTORY);
    if fs::symlink_metadata(&payload).is_ok_and(|m| m.file_type().is_symlink())
        || !has_executable(&payload)
    {
        return refuse("bundle_incomplete");
    }
    let digest = bundle_digest(&payload)?;
    if checked
        .expected_digests
        .iter()
        .any(|expected| *expected != digest)
    {
        return refuse("package_digest_mismatch");
    }
    Ok(Checked {
        version: checked.version,
        payload_digest: digest,
        rollback_floor: checked.rollback_floor,
        publisher_key_digest: checked.publisher_key_digest,
        publisher_pin_source: checked.publisher_pin_source,
        source_revision: checked.source_revision,
        developer_id_team: checked.developer_id_team,
        macos_version: observed.macos_version,
    })
}

// ---- install ---------------------------------------------------------------------------------------

fn remove_any(path: &Path) -> std::io::Result<()> {
    match fs::symlink_metadata(path) {
        Ok(m) if m.is_dir() => fs::remove_dir_all(path),
        Ok(_) => fs::remove_file(path),
        Err(_) => Ok(()),
    }
}
fn discard_staging(root: &Path) -> Result<()> {
    for child in fs::read_dir(root).map_err(io_err)? {
        let path = child.map_err(io_err)?.path();
        if path
            .file_name()
            .is_some_and(|n| n.to_string_lossy().ends_with(PARTIAL))
        {
            remove_any(&path).map_err(io_err)?;
        }
    }
    Ok(())
}
/// Digest of an installed payload, or `None` when there is no valid payload there.
fn installed_digest(version_directory: &Path) -> Option<String> {
    let payload = version_directory.join(PAYLOAD_DIRECTORY);
    let plain = |p: &Path| fs::symlink_metadata(p).is_ok_and(|m| m.is_dir());
    if !plain(version_directory) || !plain(&payload) || !has_executable(&payload) {
        return None;
    }
    bundle_digest(&payload).ok()
}
fn move_aside(seams: &Seams, target: &Path) -> Result<()> {
    let stamp = (seams.now)() as u64;
    let name = target
        .file_name()
        .ok_or(Refusal("install_io_error"))?
        .to_string_lossy()
        .into_owned();
    for attempt in 0..1000 {
        let suffix = if attempt == 0 {
            String::new()
        } else {
            format!("-{attempt}")
        };
        let aside = target.with_file_name(format!("{name}{STALE}{stamp}{suffix}"));
        if fs::symlink_metadata(&aside).is_err() {
            return fs::rename(target, aside).map_err(io_err);
        }
    }
    refuse("install_io_error")
}
/// `Some(quarantine cleared)` when the verified copy is already installed (the finalize hook runs
/// on it again, as in the oracle); `None` when `target` is free (absent, or moved aside).
fn settle_target(
    seams: &Seams,
    target: &Path,
    checked: &Checked,
    installed: Option<&Rec>,
) -> Result<Option<bool>> {
    if fs::symlink_metadata(target).is_err() {
        return Ok(None);
    }
    let digest = installed_digest(target);
    if digest.as_deref() == Some(checked.payload_digest.as_str()) {
        return Ok(Some((seams.clear_quarantine)(
            &target.join(PAYLOAD_DIRECTORY),
        )));
    }
    if digest.is_some() {
        let recorded = installed.is_some_and(|r| {
            [Some(current_of(r)), previous_of(r)]
                .into_iter()
                .flatten()
                .rfind(|e| version_of(e) == checked.version)
                .is_some_and(|e| {
                    field(e, "payload_digest") == Some(checked.payload_digest.as_str())
                })
        });
        if !recorded {
            return refuse("installed_version_conflict");
        }
    }
    move_aside(seams, target)?; // no valid payload (or damaged): kept for inspection, never deleted
    Ok(None)
}
fn stage(source: &Path, staging: &Path, expected: &str, seams: &Seams, owner: u32) -> Result<bool> {
    remove_any(staging).map_err(io_err)?;
    let result = (|| {
        private_dir(staging, owner)?;
        let payload = staging.join(PAYLOAD_DIRECTORY);
        if (seams.copy)(source, &payload).is_err() {
            return refuse("payload_copy_failed"); // the staging folder, payload included, is discarded by the caller
        }
        fs::set_permissions(staging, fs::Permissions::from_mode(0o700)).map_err(io_err)?;
        if bundle_digest(&payload)? != expected {
            return refuse("payload_copy_mismatch");
        }
        // Only now, on a copy that matches the signed digest: clear the download mark.
        Ok((seams.clear_quarantine)(&payload))
    })();
    if result.is_err() {
        remove_any(staging).map_err(io_err)?;
    }
    result
}
/// `register`: refuses a foreign entry (after the install, like the oracle), then points the link.
fn register(roots: &Roots, version_directory: &Path) -> Result<()> {
    let entry = roots.application_entry();
    if fs::symlink_metadata(&entry).is_ok() && !ours(roots, &entry) {
        return refuse("register_target_not_ours");
    }
    let parent = entry.parent().ok_or(Refusal("install_io_error"))?;
    fs::create_dir_all(parent).map_err(io_err)?;
    let temporary = parent.join(format!(".{APPLICATIONS_ENTRY}.tmp"));
    if let Ok(found) = fs::symlink_metadata(&temporary) {
        if found.is_dir() {
            return refuse("install_io_error"); // `Path.unlink()` on a directory
        }
        fs::remove_file(&temporary).map_err(io_err)?;
    }
    symlink(version_directory.join(PAYLOAD_DIRECTORY), &temporary).map_err(io_err)?;
    fs::rename(&temporary, &entry).map_err(io_err)
}

pub fn install(bundle: &Path, roots: &Roots, seams: &Seams) -> Result<Value> {
    let bundle = std::path::absolute(bundle).map_err(io_err)?;
    let mut installed = read_record(roots)?;
    let mut checked = verify_bundle(&bundle, seams, installed.as_ref())?; // nothing written before this
    let _guard = lock(roots, true)?;
    let again = read_record(roots)?;
    if again != installed {
        installed = again; // another installer finished between our read and the lock
        checked = verify_bundle(&bundle, seams, installed.as_ref())?;
    }
    let owner = owner_of(roots)?;
    let programs = roots.programs();
    discard_staging(&programs)?; // leftovers of a killed installer; safe: we hold the lock
    let target = roots.version_dir(&checked.version);
    let quarantine_cleared = match settle_target(seams, &target, &checked, installed.as_ref())? {
        Some(cleared) => cleared,
        None => {
            let staging = programs.join(format!("{}{PARTIAL}", checked.version));
            let cleared = stage(
                &bundle.join(PAYLOAD_DIRECTORY),
                &staging,
                &checked.payload_digest,
                seams,
                owner,
            )?;
            fs::rename(&staging, &target).map_err(io_err)?; // the whole version appears at once, verified
            cleared
        }
    };
    // v0.1.4: a same-version reinstall has no rollback target (previous is only set when the
    // version changes), and the old previous folder is pruned below.
    let previous = installed
        .as_ref()
        .filter(|r| version_of(current_of(r)) != checked.version)
        .map(|r| current_of(r).clone());
    let entry = json!({
        "version": checked.version, "payload_digest": checked.payload_digest,
        "rollback_floor": checked.rollback_floor, "source_revision": checked.source_revision,
    });
    let mut record = Rec::new();
    record.insert("protocol".into(), PROTOCOL.into());
    record.insert("current".into(), entry);
    record.insert(
        "previous".into(),
        previous.clone().map_or(Value::Null, Value::Object),
    );
    record.insert(
        "publisher_key_digest".into(),
        checked.publisher_key_digest.clone().into(),
    );
    record.insert(
        "publisher_pin_source".into(),
        checked.publisher_pin_source.clone().into(),
    );
    record.insert(
        "rollback_floor".into(),
        checked.rollback_floor.clone().into(),
    );
    record.insert("installed_at".into(), json!((seams.now)()));
    if let Some(old) = &installed {
        for (key, value) in old {
            record.entry(key.clone()).or_insert_with(|| value.clone()); // platform-owned facts survive
        }
    }
    write_record(roots, &record)?;
    let mut prune_failures = 0u32;
    let mut keep = vec![checked.version.clone()];
    keep.extend(previous.iter().map(|p| version_of(p).to_string()));
    for child in fs::read_dir(&programs).map_err(io_err)? {
        let child = child.map_err(io_err)?;
        let name = child.file_name().to_string_lossy().into_owned();
        if child.file_type().is_ok_and(|t| t.is_dir())
            && !keep.contains(&name)
            && !name.contains(STALE)
            && fs::remove_dir_all(child.path()).is_err()
        {
            prune_failures += 1; // at most current + previous are retained; a failure is reported
        }
    }
    register(roots, &target)?;
    private_dir(&roots.state(), owner)?; // owner-only when we make it; identity is created later by the app
    Ok(json!({
        "protocol": PROTOCOL, "action": "install", "version": checked.version,
        "previous": previous.as_ref().map(|p| version_of(p).to_string()),
        "publisher_key_digest": checked.publisher_key_digest,
        "publisher_pin_source": checked.publisher_pin_source,
        "developer_id_team": checked.developer_id_team, "macos_version": checked.macos_version,
        "quarantine_cleared": quarantine_cleared, "prune_failures": prune_failures,
        "identity_created": false, "unattended_task_registered": false,
    }))
}

// ---- rollback, uninstall, status ---------------------------------------------------------------------

pub fn rollback(roots: &Roots, seams: &Seams) -> Result<Value> {
    let _guard = lock(roots, false)?;
    let Some(installed) = read_record(roots)? else {
        return refuse("no_previous_version");
    };
    let Some(previous) = previous_of(&installed).cloned() else {
        return refuse("no_previous_version");
    };
    if numeric_or_fail(version_of(&previous))?
        < numeric_or_fail(field(&installed, "rollback_floor").unwrap_or_default())?
    {
        return refuse("package_downgrade_rejected");
    }
    let directory = roots.version_dir(version_of(&previous));
    match installed_digest(&directory) {
        None => return refuse("previous_version_missing"),
        Some(d) if Some(d.as_str()) != field(&previous, "payload_digest") => {
            return refuse("previous_version_tampered");
        }
        Some(_) => {}
    }
    let mut record = installed.clone();
    record.insert("current".into(), Value::Object(previous.clone()));
    record.insert("previous".into(), installed["current"].clone());
    record.insert("installed_at".into(), json!((seams.now)()));
    write_record(roots, &record)?;
    register(roots, &directory)?; // after the record, like the oracle
    Ok(json!({
        "protocol": PROTOCOL, "action": "rollback", "version": version_of(&previous),
        "previous": version_of(current_of(&installed)),
    }))
}

pub fn uninstall(roots: &Roots, seams: &Seams, delete_state: bool) -> Result<Value> {
    let _guard = lock(roots, false)?;
    // v0.1.4: a damaged or untrusted record refuses here, before anything is changed.
    let installed = read_record(roots)?;
    let entry = roots.application_entry();
    let mut removed = Vec::new();
    if ours(roots, &entry) {
        fs::remove_file(&entry).map_err(io_err)?;
        removed.push(APPLICATIONS_ENTRY);
    }
    let running = (seams.running_executable)();
    let programs = roots.programs();
    let mut deferred = false;
    if fs::metadata(&programs).is_ok() {
        let mut children: Vec<_> = fs::read_dir(&programs)
            .map_err(io_err)?
            .collect::<std::io::Result<Vec<_>>>()
            .map_err(io_err)?;
        children.sort_by_key(fs::DirEntry::file_name);
        for child in children {
            let path = child.path();
            if fs::metadata(&path).is_ok_and(|m| m.is_dir()) {
                if path
                    .canonicalize()
                    .is_ok_and(|real| running.starts_with(real))
                {
                    // The running executable lives here. POSIX lets its folder be unlinked while it
                    // runs: v0.1.4 removes the whole root right after (`remove_directory_after_exit`).
                    deferred = true;
                    continue;
                }
                if !fs::symlink_metadata(&path).is_ok_and(|m| m.file_type().is_symlink()) {
                    fs::remove_dir_all(&path).map_err(io_err)?;
                }
            } else {
                remove_any(&path).map_err(io_err)?; // includes the lock file; our descriptor stays valid
            }
        }
        remove_any(&programs).map_err(io_err)?;
    }
    let mut state = "retained";
    if delete_state {
        let state_root = roots.state();
        if let Ok(found) = fs::symlink_metadata(&state_root) {
            if !found.is_dir() {
                return refuse("install_io_error"); // a link or file there is never followed
            }
            fs::remove_dir_all(&state_root).map_err(io_err)?;
        }
        state = "deleted";
    }
    Ok(json!({
        "protocol": PROTOCOL, "action": "uninstall",
        "version": installed.as_ref().map(|r| version_of(current_of(r)).to_string()),
        "unattended_task": "absent", "state": state,
        "state_root": roots.state().to_string_lossy(),
        "running_version_removed_after_exit": deferred,
        "coordinator_revocation_claimed": false, "removed_entries": removed,
    }))
}

/// The source revision of the running build, for the page's version line (lane VL): the
/// `source_revision` the install record holds for its current version, only when that version
/// is this build's own ([`crate::bundle::VERSION`]) and `executable` resolves into that
/// version's installed copy (`programs/<version>/`, also when launched through the Applications
/// link). Read-only, no lock. Any doubt (no or untrusted record, another version, a copy run
/// from elsewhere, a revision that is not 40 lower-case hex digits) is `None`, never a guess.
pub fn running_source_revision(roots: &Roots, executable: &Path) -> Option<String> {
    let record = read_record(roots).ok()??;
    let current = current_of(&record);
    if version_of(current) != crate::bundle::VERSION {
        return None;
    }
    let installed = roots
        .version_dir(crate::bundle::VERSION)
        .canonicalize()
        .ok()?;
    if !executable.canonicalize().ok()?.starts_with(installed) {
        return None;
    }
    field(current, "source_revision")
        .filter(|r| r.len() == 40 && r.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')))
        .map(str::to_string)
}

pub fn status(roots: &Roots) -> Result<Value> {
    let installed = read_record(roots)?;
    Ok(json!({
        "protocol": PROTOCOL, "installed": installed.is_some(),
        "current": installed.as_ref().map(|r| version_of(current_of(r)).to_string()),
        "previous": installed.as_ref().and_then(previous_of).map(|p| version_of(p).to_string()),
        "publisher_key_digest": installed.as_ref().and_then(|r| field(r, "publisher_key_digest")),
    }))
}

#[cfg(test)]
mod record_tests {
    use super::*;
    use std::time::Duration;

    /// Lane VL patch round 1 (REVIEW-laneUI-astra VL-01): the record checked as an owner-only
    /// regular file by its path, then swapped for a FIFO with no writer before it is opened, is
    /// refused at once: the open neither follows a link nor waits on a FIFO, and the opened object
    /// itself is checked against what was admitted. An unchanged record still reads.
    #[test]
    fn a_record_swapped_for_a_fifo_after_its_check_is_refused_without_waiting() {
        let dir = std::env::temp_dir().canonicalize().unwrap().join(format!(
            "v02-vl-record-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join(RECORD);
        let write = |bytes: &[u8]| {
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&path)
                .unwrap()
                .write_all(bytes)
                .unwrap();
        };
        let read_within = |checked: fs::Metadata| {
            let (tx, rx) = std::sync::mpsc::channel();
            let path = path.clone();
            std::thread::spawn(move || {
                tx.send(read_checked(&path, &checked)).unwrap_or_default();
            });
            rx.recv_timeout(Duration::from_secs(10))
                .expect("the record read waited on what was opened")
        };
        write(b"{}");
        let checked = fs::symlink_metadata(&path).unwrap();
        assert_eq!(read_within(checked.clone()).as_deref(), Some(&b"{}"[..]));

        fs::remove_file(&path).unwrap();
        let made = Command::new("/usr/bin/mkfifo")
            .args(["-m", "600"])
            .arg(&path)
            .status()
            .unwrap();
        assert!(made.success());
        assert_eq!(read_within(checked.clone()), None, "a FIFO swapped in");

        // Another regular file of the same size and mode swapped in is not the one checked.
        fs::remove_file(&path).unwrap();
        write(b"[]");
        assert_eq!(
            read_within(checked.clone()),
            None,
            "another file swapped in"
        );

        // A link swapped in for the checked file is not followed.
        let target = dir.join("target.json");
        fs::rename(&path, &target).unwrap();
        symlink(&target, &path).unwrap();
        let target_checked = fs::symlink_metadata(&target).unwrap();
        assert_eq!(read_within(target_checked), None, "a link swapped in");
        fs::remove_dir_all(&dir).unwrap();
    }
}
