// SPDX-License-Identifier: AGPL-3.0-only
//! Install / upgrade / rollback / uninstall and user-state preservation, ported from the
//! v0.1.4 macOS installer (`mycelium_a13_packaging/installer_core.py`, `mycelium_a13_macos/
//! installer.py` at f4e366e9), exercised against a scratch fake install root only.
//! Nothing here touches the real home, `/Applications`, `~/Applications` or user state.
use desktop::{
    docs::{self, FinalizeSpec, Signer},
    install::{self, Observed, Roots, Seams},
};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    os::unix::fs::{MetadataExt, PermissionsExt, symlink},
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
};

static SEQ: AtomicUsize = AtomicUsize::new(0);
struct Tree(PathBuf);
impl Tree {
    fn new() -> Self {
        // Canonical base: the installer refuses a path that crosses a link (as v0.1.4's does).
        let p = std::env::temp_dir().canonicalize().unwrap().join(format!(
            "v02-l11-install-{}-{}",
            std::process::id(),
            SEQ.fetch_add(1, Ordering::SeqCst)
        ));
        fs::create_dir(&p).unwrap();
        fs::create_dir(p.join("home")).unwrap();
        fs::create_dir(p.join("apps")).unwrap();
        Self(p)
    }
    fn path(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
    fn roots(&self) -> Roots {
        Roots::new(&self.path("home"), &self.path("apps")).unwrap()
    }
}
impl Drop for Tree {
    fn drop(&mut self) {
        // Test-owned scratch only. Fixtures may hold read-only files.
        fn unlock(path: &Path) {
            if let Ok(meta) = fs::symlink_metadata(path)
                && meta.is_dir()
            {
                fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
                for child in fs::read_dir(path).unwrap().flatten() {
                    unlock(&child.path());
                }
            }
        }
        unlock(&self.0);
        fs::remove_dir_all(&self.0).unwrap();
    }
}

/// Installer runs hold a flock on a CLOEXEC descriptor; a `fork` in a sibling test thread (ditto,
/// xattr) would inherit it until that child's `exec`, and a quick follow-up run in this test could
/// then see `install_already_running`. Harness users therefore take turns (re-entrant per thread).
static GATE: Mutex<()> = Mutex::new(());
thread_local!(static HELD: std::cell::Cell<bool> = const { std::cell::Cell::new(false) });
struct Gate(Option<std::sync::MutexGuard<'static, ()>>);
fn gate() -> Gate {
    if HELD.get() {
        return Gate(None);
    }
    let guard = GATE
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    HELD.set(true);
    Gate(Some(guard))
}
impl Drop for Gate {
    fn drop(&mut self) {
        if self.0.is_some() {
            HELD.set(false);
        }
    }
}
struct Harness {
    seams: Seams,
    cleared: Arc<Mutex<Vec<PathBuf>>>,
    _gate: Gate,
}
fn harness(observed: Observed) -> Harness {
    let gate = gate();
    let cleared = Arc::new(Mutex::new(Vec::new()));
    let clock = Arc::new(Mutex::new(1_790_000_000.0));
    let (seen, ticks) = (cleared.clone(), clock.clone());
    let mut seams = Seams::host();
    seams.observe = Box::new(move || observed.clone());
    seams.now = Box::new(move || {
        let mut now = ticks.lock().unwrap();
        *now += 1.0;
        *now
    });
    seams.clear_quarantine = Box::new(move |path| {
        seen.lock().unwrap().push(path.to_path_buf());
        true
    });
    Harness {
        seams,
        cleared,
        _gate: gate,
    }
}
fn mac() -> Observed {
    Observed {
        system: "Darwin".into(),
        machine: "arm64".into(),
        macos_version: "26.5.1".into(),
    }
}
fn ok() -> Harness {
    harness(mac())
}

struct Pkg<'a> {
    label: &'a str,
    version: &'a str,
    /// v0.1.4's PyInstaller executable name differs from the v0.2 executable name.
    exe: &'a str,
    floor: &'a str,
    /// Test signing key identity (a fixed throw-away seed, never a production key).
    seed: u8,
    body: &'a str,
    python_shaped: bool,
}
impl<'a> Pkg<'a> {
    fn new(label: &'a str, version: &'a str) -> Self {
        Self {
            label,
            version,
            exe: "mycelium",
            floor: "0.1.0",
            seed: 1,
            body: "body",
            python_shaped: false,
        }
    }
    fn v014(label: &'a str, version: &'a str) -> Self {
        let mut pkg = Self::new(label, version);
        pkg.exe = "Mycelium A13 Demo";
        pkg.python_shaped = true;
        pkg
    }
}
const ISSUED: f64 = 1_790_000_000.0;
fn signer(seed: u8) -> Signer {
    Signer::from_seed([seed; 32], docs::SIGNER_ENDPOINT_ID)
}
fn key_digest(seed: u8) -> String {
    signer(seed).verification_key_digest()
}
fn put(path: &Path, bytes: &[u8], mode: u32) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, bytes).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
}
/// `<tree>/<label>/` holding the app payload and the install-package document.
fn make(tree: &Tree, pkg: &Pkg) -> PathBuf {
    let bundle = tree.path(pkg.label);
    let app = bundle.join(install::PAYLOAD_DIRECTORY);
    put(
        &app.join("Contents/MacOS").join(pkg.exe),
        format!("exe {} {}", pkg.version, pkg.body).as_bytes(),
        0o755,
    );
    put(&app.join("Contents/Info.plist"), b"<plist/>", 0o644);
    put(
        &app.join("Contents/Resources/data.bin"),
        format!("data {} {}", pkg.version, pkg.body).as_bytes(),
        0o644,
    );
    symlink("data.bin", app.join("Contents/Resources/current.bin")).unwrap();
    if pkg.python_shaped {
        put(
            &app.join("Contents/Frameworks/Python.framework/Versions/3.14/Python"),
            b"frozen interpreter stand-in",
            0o755,
        );
        symlink(
            "3.14",
            app.join("Contents/Frameworks/Python.framework/Versions/Current"),
        )
        .unwrap();
        symlink(
            "Versions/Current/Python",
            app.join("Contents/Frameworks/Python.framework/Python"),
        )
        .unwrap();
    }
    docs::finalize(
        &bundle,
        &signer(pkg.seed),
        &FinalizeSpec {
            version: pkg.version,
            rollback_floor: pkg.floor,
            source_revision: "0123456789abcdef0123456789abcdef01234567",
            issued_at: ISSUED,
            minimum_os: "15.6.1",
        },
    )
    .unwrap();
    bundle
}

/// Everything observable about a tree: kind, mode, size, content hash, link target, mtime.
fn snapshot(root: &Path) -> BTreeMap<String, String> {
    fn walk(root: &Path, dir: &Path, out: &mut BTreeMap<String, String>) {
        let mut children: Vec<_> = fs::read_dir(dir).unwrap().flatten().collect();
        children.sort_by_key(std::fs::DirEntry::file_name);
        for child in children {
            let path = child.path();
            let meta = fs::symlink_metadata(&path).unwrap();
            let relative = path.strip_prefix(root).unwrap().display().to_string();
            let facts = if meta.file_type().is_symlink() {
                format!("link {}", fs::read_link(&path).unwrap().display())
            } else if meta.is_dir() {
                format!(
                    "dir {:o} mtime={}.{}",
                    meta.mode(),
                    meta.mtime(),
                    meta.mtime_nsec()
                )
            } else {
                format!(
                    "file {:o} {} {:x} mtime={}.{}",
                    meta.mode(),
                    meta.len(),
                    Sha256::digest(fs::read(&path).unwrap()),
                    meta.mtime(),
                    meta.mtime_nsec()
                )
            };
            out.insert(relative, facts);
            if meta.is_dir() {
                walk(root, &path, out);
            }
        }
    }
    let mut out = BTreeMap::new();
    walk(root, root, &mut out);
    out
}

/// A state tree shaped like the one v0.1.4 keeps in `~/Library/Application Support/Mycelium
/// A13 macOS Demo` (names taken from `mycelium_a13_macos/*.py` at f4e366e9: `identity/`,
/// `authority.json`, `membership.state.json`, `consent.json`, `network.json`, `run-model.json`,
/// `fast-mode.json`, `resource-policy.json`, `managed/`, `serving/`, `tls/`, `application.lock`...).
/// Contents are deterministic synthetic bytes: no real user data, no secrets.
const STATE_FIXTURE: &[(&str, u32)] = &[
    ("identity/sidecar.key", 0o600),
    ("identity/public.json", 0o644),
    ("authority.json", 0o600),
    ("membership.state.json", 0o600),
    ("membership.disposition.json", 0o600),
    ("consent.json", 0o600),
    ("network.json", 0o600),
    ("run-model.json", 0o600),
    ("fast-mode.json", 0o600),
    ("resource-policy.json", 0o600),
    ("endpoint-recovery.json", 0o600),
    ("managed/native-control.json", 0o600),
    ("managed-control/installation-role.json", 0o600),
    ("serving/run-0001/status.json", 0o600),
    ("serving-runs/run-0001.json", 0o600),
    ("preparations/a9-route/progress.json", 0o600),
    ("tls/cert.pem", 0o600),
    ("seed/models/Qwen2.5-0.5B-Instruct/weights.bin", 0o644),
    ("application.lock", 0o600),
];
fn seed_state(state: &Path) {
    fs::create_dir_all(state).unwrap();
    fs::set_permissions(state, fs::Permissions::from_mode(0o700)).unwrap();
    for (index, (relative, mode)) in STATE_FIXTURE.iter().enumerate() {
        let body: Vec<u8> = format!("synthetic v0.1.4-shaped state {index} {relative}\n")
            .into_bytes()
            .into_iter()
            .cycle()
            .take(1024 + index * 97)
            .collect();
        put(&state.join(relative), &body, *mode);
    }
    symlink("identity/public.json", state.join("current-identity")).unwrap();
}

fn code<T: std::fmt::Debug>(result: install::Result<T>) -> &'static str {
    result.expect_err("a refusal was expected").0
}
fn version_of(value: &serde_json::Value, key: &str) -> serde_json::Value {
    value[key].clone()
}
fn record(tree: &Tree) -> serde_json::Value {
    let path = tree.roots().programs().join("versions.json");
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}
fn link_target(tree: &Tree) -> PathBuf {
    fs::read_link(tree.roots().application_entry()).unwrap()
}

// ---- layout and the safety rail -------------------------------------------------------

#[test]
fn layout_is_v02s_own_layout_under_the_given_root_ruling25() {
    let tree = Tree::new();
    let roots = tree.roots();
    let support = tree.path("home/Library/Application Support");
    assert_eq!(roots.programs(), support.join("org.mycelium.app/programs"));
    assert_eq!(roots.state(), support.join("org.mycelium.app/state"));
    assert_eq!(roots.application_entry(), tree.path("apps/Mycelium.app"));
    assert_eq!(
        roots.version_dir("0.2.0"),
        support.join("org.mycelium.app/programs/0.2.0")
    );
}

#[test]
fn the_real_home_and_real_applications_folders_are_refused_as_install_roots() {
    let tree = Tree::new();
    let real_home = PathBuf::from(std::env::var("HOME").unwrap());
    assert_eq!(
        code(Roots::new(&real_home, &tree.path("apps"))),
        "install_root_is_real_home"
    );
    assert_eq!(
        code(Roots::new(&tree.path("home"), Path::new("/Applications"))),
        "install_root_is_real_applications"
    );
    assert_eq!(
        code(Roots::new(
            &tree.path("home"),
            &real_home.join("Applications")
        )),
        "install_root_is_real_applications"
    );
    assert_eq!(
        code(Roots::new(Path::new("relative/home"), &tree.path("apps"))),
        "install_root_invalid"
    );
    assert_eq!(
        code(Roots::new(&tree.path("missing"), &tree.path("apps"))),
        "install_root_invalid"
    );
}

// ---- fresh install ---------------------------------------------------------------------

#[test]
fn fresh_install_is_versioned_verified_linked_recorded_and_leaves_state_empty() {
    let tree = Tree::new();
    let bundle = make(&tree, &Pkg::new("b020", "0.2.0"));
    let h = ok();
    let report = install::install(&bundle, &tree.roots(), &h.seams).unwrap();
    let roots = tree.roots();

    assert_eq!(report["action"], "install");
    assert_eq!(report["protocol"], install::PROTOCOL);
    assert_eq!(report["version"], "0.2.0");
    assert_eq!(report["previous"], serde_json::Value::Null);
    assert_eq!(report["identity_created"], false);
    assert_eq!(report["unattended_task_registered"], false);
    assert_eq!(report["quarantine_cleared"], true);
    assert_eq!(report["publisher_pin_source"], install::FIRST_USE_PIN);

    let installed = roots.version_dir("0.2.0").join(install::PAYLOAD_DIRECTORY);
    assert_eq!(
        install::bundle_digest(&installed).unwrap(),
        install::bundle_digest(&bundle.join(install::PAYLOAD_DIRECTORY)).unwrap()
    );
    assert!(
        fs::metadata(installed.join("Contents/MacOS/mycelium"))
            .unwrap()
            .permissions()
            .mode()
            & 0o111
            != 0
    );
    assert_eq!(
        fs::read_link(installed.join("Contents/Resources/current.bin")).unwrap(),
        PathBuf::from("data.bin")
    );
    assert_eq!(link_target(&tree), installed);

    let rec = record(&tree);
    assert_eq!(rec["protocol"], install::PROTOCOL);
    assert_eq!(rec["current"]["version"], "0.2.0");
    assert_eq!(rec["previous"], serde_json::Value::Null);
    assert_eq!(rec["rollback_floor"], "0.1.0");
    assert_eq!(rec["publisher_pin_source"], install::FIRST_USE_PIN);
    assert_eq!(
        rec["current"]["payload_digest"],
        install::bundle_digest(&installed).unwrap()
    );
    assert!(rec["installed_at"].is_number());

    // Owner-only record and folders; the state root exists but the installer put nothing in it.
    let mode = |p: &Path| fs::metadata(p).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode(&roots.programs()), 0o700);
    assert_eq!(mode(&roots.programs().join("versions.json")), 0o600);
    assert_eq!(mode(&roots.state()), 0o700);
    assert_eq!(fs::read_dir(roots.state()).unwrap().count(), 0);

    // No staging left; quarantine cleared on the verified staged copy only, never on the source.
    let names: Vec<_> = fs::read_dir(roots.programs())
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    assert!(names.iter().all(|n| !n.ends_with(".partial")), "{names:?}");
    let cleared = h.cleared.lock().unwrap();
    assert_eq!(cleared.len(), 1);
    assert!(cleared[0].starts_with(roots.programs()), "{cleared:?}");
    assert!(!cleared[0].starts_with(&bundle));
}

#[test]
fn every_check_refuses_before_any_write() {
    type Case = (
        &'static str,
        Box<dyn Fn(&Tree, &Pkg) -> PathBuf>,
        Observed,
        &'static str,
    );
    let cases: Vec<Case> = vec![
        (
            "payload bytes changed after the package was described",
            Box::new(|tree, pkg| {
                let bundle = make(tree, pkg);
                let exe = bundle.join("Mycelium.app/Contents/MacOS/mycelium");
                fs::write(exe, b"tampered").unwrap();
                bundle
            }),
            mac(),
            "package_digest_mismatch",
        ),
        (
            "an added file the description does not list",
            Box::new(|tree, pkg| {
                let bundle = make(tree, pkg);
                put(&bundle.join("Mycelium.app/Contents/extra"), b"x", 0o644);
                bundle
            }),
            mac(),
            "package_digest_mismatch",
        ),
        (
            "package document missing",
            Box::new(|tree, pkg| {
                let bundle = make(tree, pkg);
                fs::remove_file(bundle.join(docs::MANIFEST_NAME)).unwrap();
                bundle
            }),
            mac(),
            "bundle_incomplete",
        ),
        (
            "package document malformed",
            Box::new(|tree, pkg| {
                let bundle = make(tree, pkg);
                fs::write(bundle.join(docs::MANIFEST_NAME), b"{not json").unwrap();
                bundle
            }),
            mac(),
            "bundle_incomplete",
        ),
        (
            "payload executable missing",
            Box::new(|tree, pkg| {
                let bundle = make(tree, pkg);
                fs::remove_file(bundle.join("Mycelium.app/Contents/MacOS/mycelium")).unwrap();
                bundle
            }),
            mac(),
            "bundle_incomplete",
        ),
        (
            "an absolute link in the payload",
            Box::new(|tree, pkg| {
                let bundle = make(tree, pkg);
                symlink("/etc/hosts", bundle.join("Mycelium.app/Contents/abs")).unwrap();
                bundle
            }),
            mac(),
            "payload_link_absolute",
        ),
        (
            "a link that escapes the bundle",
            Box::new(|tree, pkg| {
                let bundle = make(tree, pkg);
                symlink("../../../x", bundle.join("Mycelium.app/Contents/esc")).unwrap();
                bundle
            }),
            mac(),
            "payload_link_escapes_bundle",
        ),
        (
            "a dangling link",
            Box::new(|tree, pkg| {
                let bundle = make(tree, pkg);
                symlink("nowhere", bundle.join("Mycelium.app/Contents/dangling")).unwrap();
                bundle
            }),
            mac(),
            "payload_link_dangling",
        ),
        (
            "Intel Mac",
            Box::new(make),
            Observed {
                machine: "x86_64".into(),
                ..mac()
            },
            "platform_unsupported",
        ),
        (
            "not macOS",
            Box::new(make),
            Observed {
                system: "Linux".into(),
                ..mac()
            },
            "platform_unsupported",
        ),
        (
            "macOS version the app does not admit (v0.1.4 admits exactly 26.5.1 and 15.6.1)",
            Box::new(make),
            Observed {
                macos_version: "15.6.0".into(),
                ..mac()
            },
            "package_platform_unsupported",
        ),
    ];
    for (name, build, observed, expected) in cases {
        let tree = Tree::new();
        let bundle = build(&tree, &Pkg::new("b020", "0.2.0"));
        let before = snapshot(&tree.path("home"));
        let apps_before = snapshot(&tree.path("apps"));
        let h = harness(observed);
        assert_eq!(
            code(install::install(&bundle, &tree.roots(), &h.seams)),
            expected,
            "{name}"
        );
        assert_eq!(snapshot(&tree.path("home")), before, "{name}: home changed");
        assert_eq!(
            snapshot(&tree.path("apps")),
            apps_before,
            "{name}: applications changed"
        );
        assert!(
            h.cleared.lock().unwrap().is_empty(),
            "{name}: quarantine touched"
        );
    }
}

#[test]
fn hostile_artifact_names_and_path_tricks_are_refused_at_the_document_check() {
    // v0.1.4 derives the version from the install source's artifact name
    // (`^mycelium-(\d+\.\d+\.\d+(?:-[a-z0-9.]+)?)-macos-arm64$`); the name is not in the signed
    // manifest, so only the install-source checks stand between a hostile name and a path.
    for (name, expected) in [
        ("mycelium--macos-arm64", "artifact_name_invalid"),
        ("mycelium-0.2-macos-arm64", "artifact_name_invalid"),
        ("mycelium-v0.2.0-macos-arm64", "artifact_name_invalid"),
        ("mycelium-0.2.0-RC-macos-arm64", "artifact_name_invalid"),
        ("mycelium-0.2.0--macos-arm64", "artifact_name_invalid"),
        ("mycelium-01.2.0.9-macos-arm64", "artifact_name_invalid"),
        ("mycelium-0.2.0-macos-x86_64", "artifact_name_invalid"),
        ("evil-0.2.0-macos-arm64", "artifact_name_invalid"),
        (
            "mycelium-../0.2.0-macos-arm64",
            "install_source_artifact_invalid",
        ),
        (
            "mycelium-0.2.0/x-macos-arm64",
            "install_source_artifact_invalid",
        ),
        (
            "mycelium-0.2.0 -macos-arm64",
            "install_source_artifact_invalid",
        ),
        ("", "install_source_artifact_invalid"),
    ] {
        let tree = Tree::new();
        let bundle = make(&tree, &Pkg::new("b", "0.2.0"));
        let path = bundle.join(docs::SOURCE_NAME);
        let mut doc: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        doc["artifact_name"] = name.into();
        fs::write(&path, serde_json::to_vec(&doc).unwrap()).unwrap();
        let before = snapshot(&tree.path("home"));
        assert_eq!(
            code(install::install(&bundle, &tree.roots(), &ok().seams)),
            expected,
            "{name:?}"
        );
        assert_eq!(snapshot(&tree.path("home")), before);
    }
}

// ---- upgrade ---------------------------------------------------------------------------

#[test]
fn upgrade_keeps_previous_for_rollback_and_prunes_older_versions() {
    let tree = Tree::new();
    let h = ok();
    let roots = tree.roots();
    for (label, version) in [("b1", "0.1.4"), ("b2", "0.2.0"), ("b3", "0.2.1")] {
        let bundle = make(&tree, &Pkg::new(label, version));
        install::install(&bundle, &roots, &h.seams).unwrap();
    }
    let rec = record(&tree);
    assert_eq!(rec["current"]["version"], "0.2.1");
    assert_eq!(rec["previous"]["version"], "0.2.0");
    assert!(roots.version_dir("0.2.1").is_dir() && roots.version_dir("0.2.0").is_dir());
    assert!(
        !roots.version_dir("0.1.4").exists(),
        "at most current + previous are retained"
    );
    assert_eq!(
        link_target(&tree),
        roots.version_dir("0.2.1").join(install::PAYLOAD_DIRECTORY)
    );
    assert_eq!(rec["publisher_pin_source"], install::FIRST_USE_PIN);
}

#[test]
fn upgrade_from_a_v014_installed_tree_keeps_its_record_pin_and_unknown_fields() {
    let tree = Tree::new();
    let roots = tree.roots();
    let h = ok();
    // Lay down what the v0.1.4 installer leaves: a PyInstaller-shaped payload, `versions.json`
    // in its exact shape (extra platform-owned field included), the Applications link.
    let old = make(&tree, &Pkg::v014("old", "0.1.4"));
    let old_digest = install::bundle_digest(&old.join(install::PAYLOAD_DIRECTORY)).unwrap();
    let target = roots.version_dir("0.1.4");
    fs::create_dir_all(roots.programs()).unwrap();
    fs::set_permissions(roots.programs(), fs::Permissions::from_mode(0o700)).unwrap();
    let copy = |from: &Path, to: &Path| (h.seams.copy)(from, to).unwrap();
    fs::create_dir(&target).unwrap();
    copy(
        &old.join(install::PAYLOAD_DIRECTORY),
        &target.join(install::PAYLOAD_DIRECTORY),
    );
    let legacy_key = key_digest(1);
    let legacy_key = legacy_key.as_str();
    let legacy = serde_json::json!({
        "protocol": "mycelium.a13_macos_installer.v1",
        "current": {"version": "0.1.4", "payload_digest": old_digest, "rollback_floor": "0.1.0",
                    "source_revision": "f4e366e98a1f705dfaf182897044500d290dbd7f"},
        "previous": null,
        "publisher_key_digest": legacy_key,
        "publisher_pin_source": "first_use_owner_private_media",
        "rollback_floor": "0.1.0",
        "installed_at": 1_790_000_000.25,
        "platform_owned_fact": {"kept": true}
    });
    put(
        &roots.programs().join("versions.json"),
        &serde_json::to_vec_pretty(&legacy).unwrap(),
        0o600,
    );
    fs::create_dir_all(roots.application_entry().parent().unwrap()).unwrap();
    symlink(
        target.join(install::PAYLOAD_DIRECTORY),
        roots.application_entry(),
    )
    .unwrap();
    seed_state(&roots.state());
    let state_before = snapshot(&roots.state());

    // Upgrade with the same publisher key: accepted. A different key: refused (pinned).
    let mut pkg = Pkg::new("new", "0.2.0");
    pkg.seed = 1;
    let bundle = make(&tree, &pkg);
    let report = install::install(&bundle, &roots, &h.seams).unwrap();
    assert_eq!(report["previous"], "0.1.4");
    let rec = record(&tree);
    assert_eq!(rec["current"]["version"], "0.2.0");
    assert_eq!(rec["previous"]["version"], "0.1.4");
    assert_eq!(rec["previous"]["payload_digest"], old_digest);
    assert_eq!(rec["publisher_key_digest"], legacy_key);
    assert_eq!(
        rec["platform_owned_fact"],
        serde_json::json!({"kept": true})
    );
    assert_eq!(
        snapshot(&roots.state()),
        state_before,
        "state changed during upgrade"
    );

    // And back to v0.1.4: its PyInstaller payload (different executable name) is accepted.
    let rolled = install::rollback(&roots, &h.seams).unwrap();
    assert_eq!(rolled["version"], "0.1.4");
    assert_eq!(
        snapshot(&roots.state()),
        state_before,
        "state changed during rollback"
    );
    assert_eq!(
        link_target(&tree),
        roots.version_dir("0.1.4").join(install::PAYLOAD_DIRECTORY)
    );
}

#[test]
fn a_different_publisher_key_than_the_pinned_one_is_refused_without_writes() {
    let tree = Tree::new();
    let h = ok();
    install::install(
        &make(&tree, &Pkg::new("b1", "0.2.0")),
        &tree.roots(),
        &h.seams,
    )
    .unwrap();
    let mut other = Pkg::new("b2", "0.2.1");
    other.seed = 2;
    let bundle = make(&tree, &other);
    let before = snapshot(&tree.path("home"));
    assert_eq!(
        code(install::install(&bundle, &tree.roots(), &h.seams)),
        "package_publisher_mismatch"
    );
    assert_eq!(snapshot(&tree.path("home")), before);
}

#[test]
fn a_version_below_the_recorded_rollback_floor_is_refused() {
    let tree = Tree::new();
    let h = ok();
    let mut first = Pkg::new("b1", "0.2.0");
    first.floor = "0.2.0";
    install::install(&make(&tree, &first), &tree.roots(), &h.seams).unwrap();
    let mut old = Pkg::new("b2", "0.1.9");
    old.floor = "0.0.0";
    let bundle = make(&tree, &old);
    let before = snapshot(&tree.path("home"));
    assert_eq!(
        code(install::install(&bundle, &tree.roots(), &h.seams)),
        "package_downgrade_rejected"
    );
    assert_eq!(snapshot(&tree.path("home")), before);
}

#[test]
fn reinstalling_the_current_version_drops_the_rollback_target_exactly_as_v014_does() {
    // installer_core.install: `previous` is only set when the installed current differs from the
    // version being installed, so a same-version reinstall records `previous: null` and prunes
    // the old previous folder (integrator ruling 2).
    let tree = Tree::new();
    let h = ok();
    let roots = tree.roots();
    let one = make(&tree, &Pkg::new("b1", "0.2.0"));
    let two = make(&tree, &Pkg::new("b2", "0.2.1"));
    install::install(&one, &roots, &h.seams).unwrap();
    install::install(&two, &roots, &h.seams).unwrap();
    assert_eq!(record(&tree)["previous"]["version"], "0.2.0");
    let report = install::install(&two, &roots, &h.seams).unwrap();
    let rec = record(&tree);
    assert_eq!(rec["current"]["version"], "0.2.1");
    assert_eq!(rec["previous"], serde_json::Value::Null);
    assert_eq!(report["previous"], serde_json::Value::Null);
    assert!(!roots.version_dir("0.2.0").exists(), "old previous pruned");
    assert_eq!(version_of(&rec, "rollback_floor"), "0.1.0");
    assert_eq!(
        code(install::rollback(&roots, &h.seams)),
        "no_previous_version"
    );
    // Already installed: the verified copy is kept, the finalize hook runs on it again.
    assert_eq!(report["quarantine_cleared"], true);
    let cleared = h.cleared.lock().unwrap();
    assert_eq!(
        cleared.last().unwrap(),
        &roots.version_dir("0.2.1").join(install::PAYLOAD_DIRECTORY)
    );
}

// ---- staging integrity ---------------------------------------------------------------------

#[test]
fn leftovers_of_a_killed_installer_are_cleaned_by_the_next_run() {
    let tree = Tree::new();
    let roots = tree.roots();
    private_programs(&roots);
    fs::create_dir_all(roots.programs().join("0.2.0.partial/Mycelium.app")).unwrap();
    fs::write(
        roots.programs().join("0.2.0.partial/Mycelium.app/half"),
        b"x",
    )
    .unwrap();
    fs::create_dir_all(roots.programs().join("9.9.9.partial")).unwrap();
    let bundle = make(&tree, &Pkg::new("b", "0.2.0"));
    install::install(&bundle, &roots, &ok().seams).unwrap();
    let names: Vec<_> = fs::read_dir(roots.programs())
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    assert!(names.iter().all(|n| !n.ends_with(".partial")), "{names:?}");
}

#[test]
fn a_failed_copy_leaves_nothing_installed_recorded_or_unquarantined() {
    let tree = Tree::new();
    let bundle = make(&tree, &Pkg::new("b", "0.2.0"));
    let mut h = ok();
    h.seams.copy = Box::new(|_from, to| {
        fs::create_dir_all(to)?;
        fs::write(to.join("half-copied"), b"x")?;
        Err(std::io::Error::other("disk full"))
    });
    assert_eq!(
        code(install::install(&bundle, &tree.roots(), &h.seams)),
        "payload_copy_failed"
    );
    let roots = tree.roots();
    assert!(!roots.programs().join("versions.json").exists());
    assert!(!roots.version_dir("0.2.0").exists());
    assert!(!roots.programs().join("0.2.0.partial").exists());
    assert!(fs::symlink_metadata(roots.application_entry()).is_err());
    assert!(h.cleared.lock().unwrap().is_empty());
}

#[test]
fn a_copy_that_does_not_match_the_signed_digest_is_discarded_before_quarantine_is_touched() {
    let tree = Tree::new();
    let bundle = make(&tree, &Pkg::new("b", "0.2.0"));
    let mut h = ok();
    h.seams.copy = Box::new(|from, to| {
        let native = install::ditto_copy(from, to);
        native?;
        fs::write(
            to.join("Contents/Resources/data.bin"),
            b"flipped in transit",
        )
    });
    assert_eq!(
        code(install::install(&bundle, &tree.roots(), &h.seams)),
        "payload_copy_mismatch"
    );
    let roots = tree.roots();
    assert!(!roots.version_dir("0.2.0").exists());
    assert!(!roots.programs().join("0.2.0.partial").exists());
    assert!(
        h.cleared.lock().unwrap().is_empty(),
        "quarantine cleared on an unverified copy"
    );
}

#[test]
fn a_version_directory_without_a_valid_payload_is_moved_aside_never_deleted() {
    let tree = Tree::new();
    let roots = tree.roots();
    let stale = roots.version_dir("0.2.0");
    private_programs(&roots);
    fs::create_dir_all(&stale).unwrap();
    fs::write(stale.join("evidence.txt"), b"keep me").unwrap();
    let h = ok();
    install::install(&make(&tree, &Pkg::new("b", "0.2.0")), &roots, &h.seams).unwrap();
    let asides: Vec<_> = fs::read_dir(roots.programs())
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| {
            p.file_name()
                .unwrap()
                .to_string_lossy()
                .starts_with("0.2.0.stale-")
        })
        .collect();
    assert_eq!(asides.len(), 1);
    assert_eq!(
        fs::read(asides[0].join("evidence.txt")).unwrap(),
        b"keep me"
    );
    assert!(
        roots
            .version_dir("0.2.0")
            .join(install::PAYLOAD_DIRECTORY)
            .is_dir()
    );
}

#[test]
fn a_valid_but_different_payload_under_an_unrecorded_version_is_a_conflict() {
    let tree = Tree::new();
    let roots = tree.roots();
    let h = ok();
    let squatter = make(&tree, &Pkg::new("squat", "0.2.0"));
    let target = roots.version_dir("0.2.0");
    private_programs(&roots);
    fs::create_dir_all(&target).unwrap();
    (h.seams.copy)(
        &squatter.join(install::PAYLOAD_DIRECTORY),
        &target.join(install::PAYLOAD_DIRECTORY),
    )
    .unwrap();
    let mut other = Pkg::new("real", "0.2.0");
    other.body = "different";
    let bundle = make(&tree, &other);
    let before = snapshot(&tree.path("home"));
    assert_eq!(
        code(install::install(&bundle, &roots, &h.seams)),
        "installed_version_conflict"
    );
    assert_eq!(snapshot(&tree.path("home")), before);
}

#[test]
fn a_recorded_version_damaged_on_disk_is_repaired_from_the_verified_bundle_keeping_the_damaged_copy()
 {
    let tree = Tree::new();
    let roots = tree.roots();
    let h = ok();
    let bundle = make(&tree, &Pkg::new("b", "0.2.0"));
    install::install(&bundle, &roots, &h.seams).unwrap();
    let damaged = roots
        .version_dir("0.2.0")
        .join("Mycelium.app/Contents/Resources/data.bin");
    fs::write(&damaged, b"bit rot").unwrap();
    install::install(&bundle, &roots, &h.seams).unwrap();
    assert_eq!(
        install::bundle_digest(&roots.version_dir("0.2.0").join(install::PAYLOAD_DIRECTORY))
            .unwrap(),
        install::bundle_digest(&bundle.join(install::PAYLOAD_DIRECTORY)).unwrap()
    );
    let aside = fs::read_dir(roots.programs())
        .unwrap()
        .map(|e| e.unwrap().path())
        .find(|p| p.file_name().unwrap().to_string_lossy().contains(".stale-"))
        .expect("damaged copy kept for inspection");
    assert_eq!(
        fs::read(aside.join("Mycelium.app/Contents/Resources/data.bin")).unwrap(),
        b"bit rot"
    );
}

// ---- locking --------------------------------------------------------------------------------

#[test]
fn a_second_installer_refuses_while_the_lock_is_held_and_works_again_once_it_is_released() {
    let tree = Tree::new();
    let roots = tree.roots();
    let h = ok();
    let bundle = make(&tree, &Pkg::new("b", "0.2.0"));
    install::install(&bundle, &roots, &h.seams).unwrap();
    let before = snapshot(&tree.path("home"));
    {
        let held = install::hold_lock(&roots).unwrap();
        assert_eq!(
            code(install::install(&bundle, &roots, &h.seams)),
            "install_already_running"
        );
        assert_eq!(
            code(install::rollback(&roots, &h.seams)),
            "install_already_running"
        );
        assert_eq!(
            code(install::uninstall(&roots, &h.seams, false)),
            "install_already_running"
        );
        drop(held);
    }
    assert_eq!(
        snapshot(&tree.path("home")),
        before,
        "a refused run changed something"
    );
    install::install(&bundle, &roots, &h.seams).unwrap();
}

// ---- the Applications entry ------------------------------------------------------------------

#[test]
fn an_app_the_user_put_in_applications_is_never_replaced_and_v014s_residue_is_characterized() {
    // Integrator ruling 4 (V03 characterization): v0.1.4 checks the Applications entry only in
    // `register`, AFTER the version is installed and the record written; the refusal code is the
    // same `register_target_not_ours` and the user's app is never touched. An early check would
    // change which code a bundle with several defects reports, so the oracle's order is kept: a
    // refused run leaves the verified version and the record behind (not linked), and the next run
    // succeeds once the user's app is out of the way.
    for make_theirs in [
        (|p: &Path| fs::create_dir_all(p.join("Contents")).unwrap()) as fn(&Path),
        |p: &Path| symlink("/usr/bin", p).unwrap(),
    ] {
        let tree = Tree::new();
        let roots = tree.roots();
        make_theirs(&roots.application_entry());
        let bundle = make(&tree, &Pkg::new("b", "0.2.0"));
        let apps = snapshot(&tree.path("apps"));
        assert_eq!(
            code(install::install(&bundle, &roots, &ok().seams)),
            "register_target_not_ours"
        );
        assert_eq!(
            snapshot(&tree.path("apps")),
            apps,
            "the user's app is untouched"
        );
        assert!(
            roots
                .version_dir("0.2.0")
                .join(install::PAYLOAD_DIRECTORY)
                .is_dir()
        );
        assert_eq!(record(&tree)["current"]["version"], "0.2.0");
        // A bundle with another defect still reports that defect first (no early register check).
        let tampered = make(&tree, &Pkg::new("bad", "0.2.1"));
        fs::write(tampered.join("Mycelium.app/Contents/MacOS/mycelium"), b"x").unwrap();
        assert_eq!(
            code(install::install(&tampered, &roots, &ok().seams)),
            "package_digest_mismatch"
        );
        // Out of the way: the retry installs and links.
        fs::remove_dir_all(roots.application_entry())
            .or_else(|_| fs::remove_file(roots.application_entry()))
            .unwrap();
        install::install(&bundle, &roots, &ok().seams).unwrap();
        assert_eq!(
            link_target(&tree),
            roots.version_dir("0.2.0").join(install::PAYLOAD_DIRECTORY)
        );
    }
}

// ---- rollback ----------------------------------------------------------------------------------

fn two_versions(tree: &Tree, h: &Harness) {
    for (label, version) in [("b1", "0.2.0"), ("b2", "0.2.1")] {
        install::install(
            &make(tree, &Pkg::new(label, version)),
            &tree.roots(),
            &h.seams,
        )
        .unwrap();
    }
}

#[test]
fn rollback_swaps_current_and_previous_relinks_and_rolls_forward_again() {
    let tree = Tree::new();
    let h = ok();
    two_versions(&tree, &h);
    let roots = tree.roots();
    let report = install::rollback(&roots, &h.seams).unwrap();
    assert_eq!(
        (
            report["action"].as_str(),
            report["version"].as_str(),
            report["previous"].as_str()
        ),
        (Some("rollback"), Some("0.2.0"), Some("0.2.1"))
    );
    let rec = record(&tree);
    assert_eq!(rec["current"]["version"], "0.2.0");
    assert_eq!(rec["previous"]["version"], "0.2.1");
    assert_eq!(
        link_target(&tree),
        roots.version_dir("0.2.0").join(install::PAYLOAD_DIRECTORY)
    );
    install::rollback(&roots, &h.seams).unwrap();
    assert_eq!(record(&tree)["current"]["version"], "0.2.1");
    assert_eq!(
        link_target(&tree),
        roots.version_dir("0.2.1").join(install::PAYLOAD_DIRECTORY)
    );
}

/// Proves: B12 (lane B), the desktop flake `install::rollback_refusals_change_nothing`
/// (`install_already_running` where `no_previous_version` was expected right after an install;
/// REPORT-laneA-release.md p2-desktop, laneTEN p2-r1). A `flock` belongs to the lock file's open
/// file description, and a child that another test's thread forked while the install held its lock
/// shares that description until its exec closes its copy (installs themselves spawn `ditto`,
/// `xattr`, `sw_vers`); closing the guard's descriptor then leaves the lock held, and the next
/// install or rollback right after is refused. Here a child is forked while the lock is held and
/// kept before its exec (a `pre_exec` that waits); the guard is dropped and the lock taken again.
#[test]
#[allow(unsafe_code)]
fn the_install_lock_is_free_once_its_guard_dropped_although_a_child_forked_while_it_was_held() {
    use std::io::{Read, Write};
    use std::os::{
        fd::{AsRawFd, FromRawFd},
        unix::process::CommandExt,
    };
    let tree = Tree::new();
    let roots = tree.roots();
    let held = install::hold_lock(&roots).unwrap();
    let (mut entered, entered_w) = std::io::pipe().unwrap();
    let (release_r, mut release_w) = std::io::pipe().unwrap();
    let (signal, wait) = (entered_w.as_raw_fd(), release_r.as_raw_fd());
    let spawner = std::thread::spawn(move || {
        let mut child = std::process::Command::new("/usr/bin/true");
        // SAFETY: between fork and exec only one `write` and one `read` run, on two pipe
        // descriptors this test keeps open until the spawn returned; neither allocates.
        unsafe {
            child.pre_exec(move || {
                let mut out = std::mem::ManuallyDrop::new(std::fs::File::from_raw_fd(signal));
                let mut input = std::mem::ManuallyDrop::new(std::fs::File::from_raw_fd(wait));
                let mut byte = [1u8];
                out.write_all(&byte)?;
                input.read_exact(&mut byte)?;
                Ok(())
            });
        }
        let spawned = child.spawn().and_then(|mut c| c.wait());
        drop((entered_w, release_r));
        spawned
    });
    let mut byte = [0u8];
    entered.read_exact(&mut byte).unwrap();
    drop(held);
    let again = install::hold_lock(&roots).map(drop).map_err(|e| e.0);
    release_w.write_all(&[1]).unwrap();
    assert!(spawner.join().unwrap().is_ok(), "the held child ran");
    assert_eq!(
        again,
        Ok(()),
        "the install lock stayed held by a child forked while it was held"
    );
}

#[test]
fn rollback_refusals_change_nothing() {
    // No previous version.
    let tree = Tree::new();
    let h = ok();
    assert_eq!(
        code(install::rollback(&tree.roots(), &h.seams)),
        "no_previous_version"
    );
    install::install(
        &make(&tree, &Pkg::new("b1", "0.2.0")),
        &tree.roots(),
        &h.seams,
    )
    .unwrap();
    assert_eq!(
        code(install::rollback(&tree.roots(), &h.seams)),
        "no_previous_version"
    );

    for (name, damage, expected) in [
        (
            "previous removed",
            (|dir: &Path| fs::remove_dir_all(dir).unwrap()) as fn(&Path),
            "previous_version_missing",
        ),
        (
            "previous tampered",
            |dir: &Path| {
                fs::write(
                    dir.join("Mycelium.app/Contents/Resources/data.bin"),
                    b"evil",
                )
                .unwrap()
            },
            "previous_version_tampered",
        ),
    ] {
        let tree = Tree::new();
        let h = ok();
        two_versions(&tree, &h);
        damage(&tree.roots().version_dir("0.2.0"));
        let before = snapshot(&tree.path("home"));
        let apps = snapshot(&tree.path("apps"));
        assert_eq!(
            code(install::rollback(&tree.roots(), &h.seams)),
            expected,
            "{name}"
        );
        assert_eq!(snapshot(&tree.path("home")), before, "{name}");
        assert_eq!(snapshot(&tree.path("apps")), apps, "{name}");
    }

    // Previous below the floor.
    let tree = Tree::new();
    let h = ok();
    two_versions(&tree, &h);
    let path = tree.roots().programs().join("versions.json");
    let mut rec = record(&tree);
    rec["rollback_floor"] = "0.2.1".into();
    fs::write(&path, serde_json::to_vec(&rec).unwrap()).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    assert_eq!(
        code(install::rollback(&tree.roots(), &h.seams)),
        "package_downgrade_rejected"
    );
}

#[test]
fn a_damaged_or_foreign_installation_record_is_refused_not_trusted() {
    for (name, bytes, mode, expected) in [
        ("not json", b"{{{".to_vec(), 0o600, "installation_record_invalid"),
        (
            "wrong protocol",
            br#"{"protocol":"other","current":{"version":"0.2.0","payload_digest":"x"},"previous":null,"publisher_key_digest":"a","publisher_pin_source":"b","rollback_floor":"0.0.0"}"#.to_vec(),
            0o600,
            "installation_record_invalid",
        ),
        (
            "missing fields",
            br#"{"protocol":"mycelium.a13_macos_installer.v1","current":{}}"#.to_vec(),
            0o600,
            "installation_record_invalid",
        ),
        ("readable by others", b"{}".to_vec(), 0o644, "installation_record_untrusted"),
    ] {
        let tree = Tree::new();
        let roots = tree.roots();
    private_programs(&roots);
        fs::create_dir_all(roots.programs()).unwrap();
        fs::set_permissions(roots.programs(), fs::Permissions::from_mode(0o700)).unwrap();
        put(&roots.programs().join("versions.json"), &bytes, mode);
        let h = ok();
        let bundle = make(&tree, &Pkg::new("b", "0.2.0"));
        let before = snapshot(&tree.path("home"));
        assert_eq!(code(install::install(&bundle, &roots, &h.seams)), expected, "{name}");
        assert_eq!(code(install::rollback(&roots, &h.seams)), expected, "{name}");
        assert_eq!(code(install::status(&roots)), expected, "{name}");
        assert_eq!(snapshot(&tree.path("home")), before, "{name}");
        // Integrator ruling 3: v0.1.4's uninstall reads the record first, so a damaged or untrusted
        // record refuses with the same code and changes nothing (nothing is removed, no link,
        // no state). The refusal text's "uninstall, then install again" is not actionable there.
        seed_state(&roots.state());
        let state = snapshot(&roots.state());
        let before_uninstall = snapshot(&tree.path("home"));
        assert_eq!(code(install::uninstall(&roots, &h.seams, false)), expected, "{name}");
        assert_eq!(code(install::uninstall(&roots, &h.seams, true)), expected, "{name}");
        assert_eq!(snapshot(&tree.path("home")), before_uninstall, "{name}: uninstall changed something");
        assert_eq!(snapshot(&roots.state()), state, "{name}");
    }
}

// ---- user state --------------------------------------------------------------------------------

#[test]
fn user_state_survives_install_upgrade_rollback_refusals_and_uninstall_byte_for_byte() {
    let tree = Tree::new();
    let roots = tree.roots();
    let h = ok();
    seed_state(&roots.state());
    let original = snapshot(&roots.state());
    assert!(
        original.len() > 20,
        "the fixture must be a real tree: {}",
        original.len()
    );
    let same = |step: &str| {
        assert_eq!(
            snapshot(&roots.state()),
            original,
            "state changed at: {step}"
        )
    };

    install::install(&make(&tree, &Pkg::v014("b1", "0.1.4")), &roots, &h.seams).unwrap();
    same("fresh install over existing state");
    install::install(&make(&tree, &Pkg::new("b2", "0.2.0")), &roots, &h.seams).unwrap();
    same("upgrade");
    install::rollback(&roots, &h.seams).unwrap();
    same("rollback");
    install::rollback(&roots, &h.seams).unwrap();
    same("roll forward");
    install::install(&make(&tree, &Pkg::new("b3", "0.2.1")), &roots, &h.seams).unwrap();
    same("second upgrade, oldest version pruned");

    // Refused runs of every kind.
    let mut hostile = Pkg::new("bad", "0.2.2");
    hostile.seed = 3;
    assert!(install::install(&make(&tree, &hostile), &roots, &h.seams).is_err());
    same("refused upgrade (publisher)");
    let tampered = make(&tree, &Pkg::new("bad2", "0.2.3"));
    fs::write(tampered.join("Mycelium.app/Contents/MacOS/mycelium"), b"x").unwrap();
    assert!(install::install(&tampered, &roots, &h.seams).is_err());
    same("refused upgrade (tampered)");
    let mut failing = ok();
    failing.seams.copy = Box::new(|_, to| {
        fs::create_dir_all(to)?;
        Err(std::io::Error::other("injected"))
    });
    assert!(
        install::install(
            &make(&tree, &Pkg::new("bad3", "0.2.4")),
            &roots,
            &failing.seams
        )
        .is_err()
    );
    same("failed copy");

    // Uninstall keeps state unless deletion is explicit.
    let report = install::uninstall(&roots, &h.seams, false).unwrap();
    assert_eq!(report["state"], "retained");
    assert_eq!(report["coordinator_revocation_claimed"], false);
    same("uninstall");
    assert!(!roots.programs().exists());
    assert!(fs::symlink_metadata(roots.application_entry()).is_err());

    // A reinstall after uninstall finds the same state.
    install::install(&make(&tree, &Pkg::new("b4", "0.2.0")), &roots, &h.seams).unwrap();
    same("reinstall after uninstall");

    let report = install::uninstall(&roots, &h.seams, true).unwrap();
    assert_eq!(report["state"], "deleted");
    assert!(!roots.state().exists(), "explicit deletion deletes");
}

#[test]
fn the_installer_never_writes_inside_the_state_root_and_never_creates_identity() {
    let tree = Tree::new();
    let h = ok();
    let roots = tree.roots();
    install::install(&make(&tree, &Pkg::new("b1", "0.2.0")), &roots, &h.seams).unwrap();
    assert!(snapshot(&roots.state()).is_empty());
    // v0.1.4 ends an install with `private_directory(state_root, create=True)`: an existing state
    // root that is not owner-only 0700 fails there (`installer_failed`) after the install, and is
    // never changed. The installer still writes nothing inside it.
    fs::set_permissions(roots.state(), fs::Permissions::from_mode(0o755)).unwrap();
    let before = snapshot(&roots.state());
    assert_eq!(
        code(install::install(
            &make(&tree, &Pkg::new("b2", "0.2.1")),
            &roots,
            &h.seams
        )),
        "installer_failed"
    );
    assert_eq!(snapshot(&roots.state()), before);
    assert_eq!(
        fs::metadata(roots.state()).unwrap().permissions().mode() & 0o777,
        0o755
    );
}

// ---- status and clocks ------------------------------------------------------------------------

#[test]
fn status_reports_not_installed_then_current_and_previous() {
    let tree = Tree::new();
    let h = ok();
    let none = install::status(&tree.roots()).unwrap();
    assert_eq!(none["installed"], false);
    assert_eq!(none["current"], serde_json::Value::Null);
    two_versions(&tree, &h);
    let status = install::status(&tree.roots()).unwrap();
    assert_eq!(
        (
            status["installed"].as_bool(),
            status["current"].as_str(),
            status["previous"].as_str()
        ),
        (Some(true), Some("0.2.1"), Some("0.2.0"))
    );
}

// ---- digest compatibility with the v0.1.4 Python installer -------------------------------------

#[test]
fn bundle_digest_matches_the_python_oracle_golden_values() {
    // `mycelium_a13_packaging.build.bundle_digest` at f4e366e9 on this exact tree, computed once
    // outside the repository (links, a non-ASCII name and a quote in a name included).
    let tree = Tree::new();
    let app = tree.path("golden.app");
    put(&app.join("Contents/MacOS/Mycelium A13 Demo"), b"exe", 0o755);
    put(&app.join("Contents/Resources/a\"b.txt"), b"quoted", 0o644);
    put(
        &app.join("Contents/Resources/caf\u{e9}.txt"),
        "caf\u{e9}".as_bytes(),
        0o644,
    );
    put(
        &app.join("Contents/Resources/emoji-\u{1f344}.txt"),
        b"fungus",
        0o644,
    );
    put(&app.join("Contents/Frameworks/V/3.14/Py"), b"py", 0o644);
    symlink("3.14", app.join("Contents/Frameworks/V/Current")).unwrap();
    symlink("V/Current/Py", app.join("Contents/Frameworks/Py")).unwrap();
    fs::create_dir_all(app.join("Contents/Empty")).unwrap();
    assert_eq!(
        install::bundle_digest(&app).unwrap(),
        "sha256:56fdb242a6139323f1714cbf53f251304ffdc0af7b83ff7a2d95dc88170062f0"
    );
}

#[test]
#[ignore = "reads a real extracted v0.1.x release bundle; set MYCELIUM_V01X_BUNDLE"]
fn bundle_digest_equals_the_signed_package_digest_of_a_real_v01x_release() {
    let bundle = PathBuf::from(std::env::var("MYCELIUM_V01X_BUNDLE").unwrap());
    let manifest: serde_json::Value = serde_json::from_slice(
        &fs::read(bundle.join("installation-package-manifest.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(
        install::bundle_digest(&bundle.join(install::PAYLOAD_DIRECTORY)).unwrap(),
        manifest["package_digest"].as_str().unwrap()
    );
}

/// What a real v0.1.4 install leaves: an owner-only programs folder holding its lock file.
fn private_programs(roots: &Roots) {
    fs::create_dir_all(roots.programs()).unwrap();
    fs::set_permissions(roots.programs(), fs::Permissions::from_mode(0o700)).unwrap();
    put(&roots.programs().join(".install.lock"), b"", 0o600);
}

// ---- exactly v0.1.4: strict private modes, deferred self-removal, ditto ------------------------------

#[test]
fn a_programs_folder_or_record_that_is_not_exactly_owner_only_is_untrusted() {
    // `private_directory` requires mode exactly 0700 and `read_file` exactly 0600 (not merely
    // "no group/other bits"): a read-only 0500/0400 pair is refused too.
    for (name, dir_mode, file_mode) in [
        ("directory 0750", 0o750, 0o600),
        ("directory 0500", 0o500, 0o600),
        ("record 0400", 0o700, 0o400),
        ("record 0640", 0o700, 0o640),
    ] {
        let tree = Tree::new();
        let roots = tree.roots();
        install::install(&make(&tree, &Pkg::new("b", "0.2.0")), &roots, &ok().seams).unwrap();
        fs::set_permissions(
            roots.programs().join("versions.json"),
            fs::Permissions::from_mode(file_mode),
        )
        .unwrap();
        fs::set_permissions(roots.programs(), fs::Permissions::from_mode(dir_mode)).unwrap();
        let outcome = install::status(&roots);
        fs::set_permissions(roots.programs(), fs::Permissions::from_mode(0o700)).unwrap();
        assert_eq!(code(outcome), "installation_record_untrusted", "{name}");
    }
}

#[test]
fn a_link_anywhere_on_the_programs_path_makes_the_record_untrusted() {
    let tree = Tree::new();
    let roots = tree.roots();
    install::install(&make(&tree, &Pkg::new("b", "0.2.0")), &roots, &ok().seams).unwrap();
    // programs itself a link
    let moved = tree.path("moved-programs");
    fs::rename(roots.programs(), &moved).unwrap();
    symlink(&moved, roots.programs()).unwrap();
    assert_eq!(
        code(install::status(&roots)),
        "installation_record_untrusted"
    );
    fs::remove_file(roots.programs()).unwrap();
    fs::rename(&moved, roots.programs()).unwrap();
    // an ancestor a link
    let support = tree.path("home/Library/Application Support");
    let relocated = tree.path("relocated-support");
    fs::rename(&support, &relocated).unwrap();
    symlink(&relocated, &support).unwrap();
    assert_eq!(
        code(install::status(&roots)),
        "installation_record_untrusted"
    );
    fs::remove_file(&support).unwrap();
    fs::rename(&relocated, &support).unwrap();
    assert!(install::status(&roots).is_ok());
}

#[test]
fn missing_parents_are_created_owner_only_like_private_directory_create() {
    let tree = Tree::new();
    install::install(
        &make(&tree, &Pkg::new("b", "0.2.0")),
        &tree.roots(),
        &ok().seams,
    )
    .unwrap();
    let mode = |p: &str| fs::metadata(tree.path(p)).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode("home/Library"), 0o700);
    assert_eq!(mode("home/Library/Application Support"), 0o700);
    assert_eq!(
        mode("home/Library/Application Support/org.mycelium.app"),
        0o700
    );
    assert_eq!(
        mode("home/Library/Application Support/org.mycelium.app/state"),
        0o700
    );
}

#[test]
fn a_lock_file_that_others_can_read_is_a_host_failure_not_a_lock() {
    let tree = Tree::new();
    let roots = tree.roots();
    private_programs(&roots);
    fs::set_permissions(
        roots.programs().join(".install.lock"),
        fs::Permissions::from_mode(0o644),
    )
    .unwrap();
    assert_eq!(
        code(install::install(
            &make(&tree, &Pkg::new("b", "0.2.0")),
            &roots,
            &ok().seams
        )),
        "installer_failed"
    );
}

#[test]
fn uninstall_run_from_the_installed_version_unlinks_it_while_running_like_v014() {
    // `installer_core.uninstall`: the child that holds the running executable is noted as deferred
    // and the whole programs root is removed right after (POSIX unlinks a running bundle's
    // folder; v0.1.4's `remove_directory_after_exit` is `rmtree`). The report says so.
    let tree = Tree::new();
    let roots = tree.roots();
    let mut h = ok();
    install::install(&make(&tree, &Pkg::new("b1", "0.2.0")), &roots, &h.seams).unwrap();
    install::install(&make(&tree, &Pkg::new("b2", "0.2.1")), &roots, &h.seams).unwrap();
    let running = roots
        .version_dir("0.2.1")
        .join("Mycelium.app/Contents/MacOS/mycelium")
        .canonicalize()
        .unwrap();
    h.seams.running_executable = Box::new(move || running.clone());
    seed_state(&roots.state());
    let state = snapshot(&roots.state());
    let report = install::uninstall(&roots, &h.seams, false).unwrap();
    assert_eq!(report["running_version_removed_after_exit"], true);
    assert_eq!(report["version"], "0.2.1");
    assert_eq!(
        report["removed_entries"],
        serde_json::json!(["Mycelium.app"])
    );
    assert!(
        !roots.programs().exists(),
        "everything below programs is gone, running version included"
    );
    assert!(fs::symlink_metadata(roots.application_entry()).is_err());
    assert_eq!(snapshot(&roots.state()), state);

    // Run from outside the install: nothing deferred.
    let tree = Tree::new();
    let roots = tree.roots();
    let h = ok();
    install::install(&make(&tree, &Pkg::new("b1", "0.2.0")), &roots, &h.seams).unwrap();
    let report = install::uninstall(&roots, &h.seams, false).unwrap();
    assert_eq!(report["running_version_removed_after_exit"], false);
    assert!(!roots.programs().exists());
}

#[test]
fn uninstall_of_nothing_is_a_report_not_an_error() {
    let tree = Tree::new();
    let roots = tree.roots();
    let report = install::uninstall(&roots, &ok().seams, false).unwrap();
    assert_eq!(report["version"], serde_json::Value::Null);
    assert_eq!(report["removed_entries"], serde_json::json!([]));
    assert_eq!(report["state"], "retained");
}

fn xattr(args: &[&str], path: &Path) -> Option<String> {
    let out = std::process::Command::new("/usr/bin/xattr")
        .args(args)
        .arg(path)
        .output()
        .unwrap();
    out.status
        .success()
        .then(|| String::from_utf8_lossy(&out.stdout).trim().to_string())
}

#[test]
fn the_default_copy_is_ditto_and_keeps_links_modes_mtimes_and_extended_attributes() {
    // Integrator ruling 6: call /usr/bin/ditto exactly as v0.1.4 did. Proof on a real bundle tree
    // carrying attributes (a non-quarantine attribute; no real quarantine is ever set), links,
    // modes and old mtimes: the installed payload is identical to a direct ditto of the source.
    let tree = Tree::new();
    let bundle = make(&tree, &Pkg::new("b", "0.2.0"));
    let app = bundle.join(install::PAYLOAD_DIRECTORY);
    let marked = [
        app.join("Contents/Resources/data.bin"),
        app.join("Contents/MacOS/mycelium"),
        app.join("Contents/Resources"),
    ];
    for path in &marked {
        assert!(xattr(&["-w", "org.mycelium.l11-test", "kept"], path).is_some());
    }
    assert!(
        std::process::Command::new("/usr/bin/touch")
            .args(["-t", "201707140240.00"])
            .arg(app.join("Contents/Resources/data.bin"))
            .status()
            .unwrap()
            .success()
    );
    let source_mtime = fs::metadata(app.join("Contents/Resources/data.bin"))
        .unwrap()
        .mtime();
    assert!(
        source_mtime < 1_600_000_000,
        "an old mtime, so a copy that stamps 'now' is caught"
    );
    let reference = tree.path("reference.app");
    let status = std::process::Command::new("/usr/bin/ditto")
        .arg(&app)
        .arg(&reference)
        .status()
        .unwrap();
    assert!(status.success());
    let roots = tree.roots();
    // Default seams: the real ditto copy (only observe/now/quarantine are replaced).
    let h = ok();
    install::install(&bundle, &roots, &h.seams).unwrap();
    let installed = roots.version_dir("0.2.0").join(install::PAYLOAD_DIRECTORY);
    assert_eq!(
        snapshot(&installed),
        snapshot(&reference),
        "tree, modes, links, sizes, hashes, mtimes"
    );
    for path in &marked {
        let relative = path.strip_prefix(&app).unwrap();
        assert_eq!(
            xattr(&["-p", "org.mycelium.l11-test"], &installed.join(relative)).as_deref(),
            Some("kept"),
            "{relative:?}"
        );
    }
    assert_eq!(
        fs::symlink_metadata(installed.join("Contents/Resources/data.bin"))
            .unwrap()
            .mtime(),
        source_mtime
    );
}

// ---- a bad signature is refused before any write (ruling 1) -------------------------------------------

/// The manifest's own statement, for forging signatures that differ from the genuine one only in
/// the cryptography (every claimed field, digest and length is consistent).
fn statement_of(manifest: &serde_json::Value) -> serde_json::Value {
    let mut statement = manifest.clone();
    statement.as_object_mut().unwrap().remove("signature");
    statement
}
fn forge(kind: &str, manifest: &mut serde_json::Value) {
    match kind {
        // A valid-looking signature object by a different scratch test key: claims the pinned
        // key's digest and the true statement digest; only the Ed25519 check can notice.
        "other_key_signature" => {
            let mut forged = signer(9).sign(&statement_of(manifest)).unwrap();
            forged["verification_key_digest"] = key_digest(1).into();
            manifest["signature"] = forged;
        }
        // The genuine signature with one base64 character of the first bytes changed.
        "flipped_signature_byte" => {
            let text = manifest["signature"]["signature"]
                .as_str()
                .unwrap()
                .to_string();
            let first = if text.starts_with('A') { "B" } else { "A" };
            manifest["signature"]["signature"] = format!("{first}{}", &text[1..]).into();
        }
        "missing_signature" => {
            manifest.as_object_mut().unwrap().remove("signature");
        }
        "null_signature" => manifest["signature"] = serde_json::Value::Null,
        other => panic!("{other}"),
    }
}

#[test]
fn a_package_with_a_valid_digest_but_a_bad_signature_is_refused_before_any_write() {
    for (kind, expected) in [
        ("other_key_signature", "package_signature_invalid"),
        ("flipped_signature_byte", "package_signature_invalid"),
        ("missing_signature", "package_manifest_shape_invalid"),
        ("null_signature", "package_signature_invalid"),
    ] {
        for already_installed in [false, true] {
            let tree = Tree::new();
            let roots = tree.roots();
            let h = ok();
            seed_state(&roots.state());
            if already_installed {
                install::install(&make(&tree, &Pkg::new("good", "0.2.0")), &roots, &h.seams)
                    .unwrap();
            }
            let bundle = make(&tree, &Pkg::new("bad", "0.2.1"));
            // The payload digest is genuine and matches both documents; only the signature is bad.
            let path = bundle.join(docs::MANIFEST_NAME);
            let mut manifest: serde_json::Value =
                serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
            forge(kind, &mut manifest);
            fs::write(&path, serde_json::to_vec_pretty(&manifest).unwrap()).unwrap();
            let digest = install::bundle_digest(&bundle.join(install::PAYLOAD_DIRECTORY)).unwrap();
            assert_eq!(
                manifest.get("package_digest").and_then(|d| d.as_str()),
                Some(digest.as_str())
            );
            let (home, apps) = (snapshot(&tree.path("home")), snapshot(&tree.path("apps")));
            let state = snapshot(&roots.state());
            let cleared = h.cleared.lock().unwrap().len();
            assert_eq!(
                code(install::install(&bundle, &roots, &h.seams)),
                expected,
                "{kind} (already installed: {already_installed})"
            );
            assert_eq!(
                snapshot(&tree.path("home")),
                home,
                "{kind}: install root changed"
            );
            assert_eq!(
                snapshot(&tree.path("apps")),
                apps,
                "{kind}: applications changed"
            );
            assert_eq!(snapshot(&roots.state()), state, "{kind}: state changed");
            assert_eq!(
                h.cleared.lock().unwrap().len(),
                cleared,
                "{kind}: quarantine touched"
            );
        }
    }
}

// ---- ruling 20/25: v0.2 sits beside an installed v0.1.4 and never touches it ------------

/// A HOME holding a complete v0.1.4 install, laid out by v0.1.4 itself (@ f4e366e9):
/// `~/Applications/Mycelium A13.app` -> `Application Support/Mycelium/programs/<v>/Mycelium A13
/// Demo.app` (installer.py:117,121), `programs/versions.json` + `.install.lock`, its state
/// `Application Support/Mycelium A13 macOS Demo` (__main__.py:67), its service package
/// `Application Support/Mycelium/service` (finalize_a13_macos.py:135) and the operator folder
/// `Application Support/Mycelium A13 Demo`. Marker bytes only.
fn seed_v014_install(home: &Path, apps: &Path) {
    let support = home.join("Library/Application Support");
    let programs = support.join("Mycelium/programs");
    let app = programs.join("0.1.4/Mycelium A13 Demo.app");
    put(
        &app.join("Contents/MacOS/Mycelium A13 Demo"),
        b"v0.1.4 exe marker",
        0o755,
    );
    put(
        &app.join("Contents/Info.plist"),
        b"<plist>org.mycelium.a13-macos-demo</plist>",
        0o644,
    );
    put(
        &programs.join("versions.json"),
        b"{\"current\":{\"version\":\"0.1.4\"}}",
        0o600,
    );
    put(&programs.join(".install.lock"), b"", 0o600);
    put(
        &support.join("Mycelium/service/marker"),
        b"v0.1.4 service marker",
        0o600,
    );
    put(
        &support.join("Mycelium A13 Demo/qualification-marker"),
        b"v0.1.4 operator marker",
        0o600,
    );
    seed_state(&support.join("Mycelium A13 macOS Demo"));
    fs::create_dir_all(apps).unwrap();
    symlink(&app, apps.join("Mycelium A13.app")).unwrap();
}

/// Ruling 20/25, what this test proves (and only that): the real `install::install` (fresh, then an
/// upgrade), `install::rollback`, `install::uninstall` (state kept, then `--delete-identity-and-state`)
/// and the launch ROUTING of the installed executable (`entry::route`, a pure function) leave every
/// v0.1.4 path byte-, mode- and mtime-identical. It runs no launch code and no Quit: the state the
/// packaged core creates first (`node_service::packaged::prepare_dirs`,
/// `<Roots::state()>/v02-node/{private,evidence}`) is SIMULATED by the test making those folders.
/// The real model-free launch and Quit beside a seeded v0.1.4 tree is the smoke receipt
/// (`qualification/laneRELF-*/smoke/`, ruling 17), not this test.
#[test]
fn ruling20_v02_install_upgrade_rollback_uninstall_and_launch_routing_beside_v014_touch_no_v014_path()
 {
    let tree = Tree::new();
    let (home, apps) = (tree.path("home"), tree.path("apps"));
    seed_v014_install(&home, &apps);
    let v014_paths = [
        "home/Library/Application Support/Mycelium",
        "home/Library/Application Support/Mycelium A13 macOS Demo",
        "home/Library/Application Support/Mycelium A13 Demo",
        "apps/Mycelium A13.app",
    ];
    let snap = |tree: &Tree| -> BTreeMap<String, BTreeMap<String, String>> {
        v014_paths
            .iter()
            .map(|p| {
                let path = tree.path(p);
                let meta = fs::symlink_metadata(&path).unwrap();
                let mut facts = if meta.is_dir() {
                    snapshot(&path)
                } else {
                    BTreeMap::new()
                };
                facts.insert(
                    ".".into(),
                    format!(
                        "{:?} {:o} mtime={}.{}",
                        meta.file_type(),
                        meta.mode(),
                        meta.mtime(),
                        meta.mtime_nsec()
                    ),
                );
                ((*p).to_string(), facts)
            })
            .collect()
    };
    let before = snap(&tree);
    let names_before: Vec<_> = fs::read_dir(home.join("Library/Application Support"))
        .unwrap()
        .map(|e| e.unwrap().file_name())
        .collect();
    let created_support_entries = || {
        let mut created: Vec<_> = fs::read_dir(home.join("Library/Application Support"))
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .filter(|n| !names_before.contains(n))
            .collect();
        created.sort();
        created
    };
    let applications = || {
        let mut entries: Vec<_> = fs::read_dir(&apps)
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .collect();
        entries.sort();
        entries
    };

    // Install v0.2, then the launch ROUTE of the installed executable; the first launch writes are
    // simulated (see the doc comment).
    let roots = tree.roots();
    let h = ok();
    install::install(&make(&tree, &Pkg::new("b020", "0.2.0")), &roots, &h.seams).unwrap();
    let installed = roots.version_dir("0.2.0").join(install::PAYLOAD_DIRECTORY);
    assert_eq!(fs::read_link(roots.application_entry()).unwrap(), installed);
    let exe = installed.join("Contents/MacOS/mycelium");
    assert!(matches!(
        desktop::entry::route(&["mycelium".to_string()], &exe, &home),
        desktop::entry::Route::Run
    ));
    for dir in ["v02-node/private", "v02-node/evidence"] {
        fs::create_dir_all(roots.state().join(dir)).unwrap();
    }
    assert_eq!(snap(&tree), before, "install: a v0.1.4 path changed");
    // v0.2 created exactly one new Application Support entry and one Applications entry.
    assert_eq!(created_support_entries(), [install::SUPPORT_FOLDER]);
    let own = home
        .join("Library/Application Support")
        .join(install::SUPPORT_FOLDER);
    assert!(roots.programs().starts_with(&own));
    assert!(roots.state().starts_with(&own));
    assert_eq!(applications(), ["Mycelium A13.app", "Mycelium.app"]);

    // Upgrade to a second v0.2 build, then roll back to the first.
    install::install(&make(&tree, &Pkg::new("b021", "0.2.1")), &roots, &h.seams).unwrap();
    assert_eq!(snap(&tree), before, "upgrade: a v0.1.4 path changed");
    let rolled = install::rollback(&roots, &h.seams).unwrap();
    assert_eq!(rolled["version"], "0.2.0");
    assert_eq!(fs::read_link(roots.application_entry()).unwrap(), installed);
    assert_eq!(snap(&tree), before, "rollback: a v0.1.4 path changed");
    assert_eq!(created_support_entries(), [install::SUPPORT_FOLDER]);

    // Uninstall keeping state, then uninstall deleting identity and state.
    let kept = install::uninstall(&roots, &h.seams, false).unwrap();
    assert_eq!(kept["state"], "retained");
    assert_eq!(snap(&tree), before, "uninstall: a v0.1.4 path changed");
    assert!(roots.state().is_dir());
    assert_eq!(applications(), ["Mycelium A13.app"]);
    let deleted = install::uninstall(&roots, &h.seams, true).unwrap();
    assert_eq!(deleted["state"], "deleted");
    assert!(!roots.state().exists());
    assert_eq!(
        snap(&tree),
        before,
        "uninstall --delete-identity-and-state: a v0.1.4 path changed"
    );
    assert_eq!(applications(), ["Mycelium A13.app"]);
}
