// SPDX-License-Identifier: AGPL-3.0-only
//! Explicit gate over TWO REAL built bundles (never in the default run): the real installer
//! installs the probe bundle as 0.2.0, upgrades to the default bundle as 0.2.1, rolls back and
//! forward again, in a scratch `HOME` with the TEST publisher key (the production key is never
//! read). It is the real-executable counterpart of `install.rs`'s toy-bundle upgrade/rollback
//! tests (CL-L11-01 without the production key): each installed version is the bundle it was built
//! from (executable and whole-tree digest), `previous` really keeps the old version until the
//! rollback swaps it, and a v0.1.4-shaped state tree (identity, authority, consent, network,
//! models, ...) is byte-for-byte, mode-for-mode and mtime-for-mtime unchanged at every step.
//! Nothing is launched; no GPU, no network, no installed application.
//!
//! ```text
//! MYCELIUM_BUNDLE_APP="/path/default/Mycelium.app" \
//! MYCELIUM_PROBE_BUNDLE_APP="/path/probe/Mycelium.app" \
//!   cargo test --offline --locked -j 2 -p desktop --test bundle_upgrade -- --ignored --test-threads=1
//! ```
#![cfg(target_os = "macos")]
use desktop::{
    docs::{FinalizeSpec, SIGNER_ENDPOINT_ID, Signer},
    install::{self, Roots, Seams},
};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    os::unix::fs::{MetadataExt, PermissionsExt, symlink},
    path::{Path, PathBuf},
    process::Command,
};

/// Names of a v0.1.4 state tree (`install.rs::STATE_FIXTURE`, from `mycelium_a13_macos/*.py`).
const STATE: &[(&str, u32)] = &[
    ("identity/sidecar.key", 0o600),
    ("identity/public.json", 0o644),
    ("authority.json", 0o600),
    ("membership.state.json", 0o600),
    ("consent.json", 0o600),
    ("network.json", 0o600),
    ("run-model.json", 0o600),
    ("managed/native-control.json", 0o600),
    ("serving-runs/run-0001.json", 0o600),
    ("tls/cert.pem", 0o600),
    ("seed/models/Qwen2.5-0.5B-Instruct/weights.bin", 0o644),
    ("application.lock", 0o600),
];

fn bundle_from_env(name: &str) -> PathBuf {
    let app = PathBuf::from(
        std::env::var(name).unwrap_or_else(|_| panic!("{name} must name a built .app")),
    );
    assert!(
        app.starts_with(std::env::var("HOME").unwrap())
            && app.to_string_lossy().contains("/.hermes/cache/scratch/"),
        "only a lane scratch bundle may be installed: {}",
        app.display()
    );
    assert!(
        app.join("Contents/MacOS/mycelium").is_file(),
        "{}",
        app.display()
    );
    app
}
fn sha256_file(path: &Path) -> String {
    format!("{:x}", Sha256::digest(fs::read(path).unwrap()))
}
/// Every entry of `root` with its kind, mode, size, content digest and mtime.
fn snapshot(root: &Path) -> BTreeMap<String, String> {
    fn walk(root: &Path, dir: &Path, out: &mut BTreeMap<String, String>) {
        for child in fs::read_dir(dir).unwrap().flatten() {
            let path = child.path();
            let meta = fs::symlink_metadata(&path).unwrap();
            let key = path.strip_prefix(root).unwrap().display().to_string();
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
                    "file {:o} {} {} mtime={}.{}",
                    meta.mode(),
                    meta.len(),
                    sha256_file(&path),
                    meta.mtime(),
                    meta.mtime_nsec()
                )
            };
            out.insert(key, facts);
            if meta.is_dir() {
                walk(root, &path, out);
            }
        }
    }
    let mut out = BTreeMap::new();
    walk(root, root, &mut out);
    out
}
fn stage(scratch: &Path, label: &str, app: &Path, version: &str) -> PathBuf {
    let staged = scratch.join(label);
    fs::create_dir_all(&staged).unwrap();
    assert!(
        Command::new("/usr/bin/ditto")
            .arg(app)
            .arg(staged.join("Mycelium.app"))
            .status()
            .unwrap()
            .success()
    );
    desktop::docs::finalize(
        &staged,
        &Signer::from_seed([0x66; 32], SIGNER_ENDPOINT_ID),
        &FinalizeSpec {
            version,
            rollback_floor: "0.1.0",
            source_revision: "0123456789abcdef0123456789abcdef01234567",
            issued_at: std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_secs_f64()
                .floor(),
            minimum_os: "15.6.1",
        },
    )
    .unwrap();
    staged
}

#[test]
#[ignore = "explicit gate over two real built bundles; scratch HOME, TEST key, nothing launched"]
fn two_real_bundles_install_upgrade_and_roll_back_with_state_and_each_version_intact() {
    let probe = bundle_from_env("MYCELIUM_PROBE_BUNDLE_APP");
    let default = bundle_from_env("MYCELIUM_BUNDLE_APP");
    let (exe_old, exe_new) = (
        sha256_file(&probe.join("Contents/MacOS/mycelium")),
        sha256_file(&default.join("Contents/MacOS/mycelium")),
    );
    assert_ne!(exe_old, exe_new, "the two bundles must be different builds");
    let (digest_old, digest_new) = (
        install::bundle_digest(&probe).unwrap(),
        install::bundle_digest(&default).unwrap(),
    );

    let scratch = std::env::temp_dir()
        .canonicalize()
        .unwrap()
        .join(format!("v02-l11-upgrade-{}", std::process::id()));
    let (home, apps) = (scratch.join("home"), scratch.join("apps"));
    fs::create_dir_all(&home).unwrap();
    fs::create_dir_all(&apps).unwrap();
    let outcome = std::panic::catch_unwind(|| {
        let roots = Roots::new(&home, &apps).unwrap();
        let seams = Seams::host();
        let state = roots.state();
        fs::create_dir_all(&state).unwrap();
        fs::set_permissions(&state, fs::Permissions::from_mode(0o700)).unwrap();
        for (index, (relative, mode)) in STATE.iter().enumerate() {
            let path = state.join(relative);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(
                &path,
                format!("synthetic state {index} {relative}\n").repeat(40),
            )
            .unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(*mode)).unwrap();
        }
        symlink("identity/public.json", state.join("current-identity")).unwrap();
        let original = snapshot(&state);
        assert!(original.len() > 15, "{}", original.len());
        let same = |step: &str| assert_eq!(snapshot(&state), original, "state changed at: {step}");

        let installed_exe = |version: &str| {
            roots
                .version_dir(version)
                .join(install::PAYLOAD_DIRECTORY)
                .join("Contents/MacOS/mycelium")
        };
        let old = stage(&scratch, "old", &probe, "0.2.0");
        let new = stage(&scratch, "new", &default, "0.2.1");

        // Fresh install of the probe bundle as 0.2.0.
        let report = install::install(&old, &roots, &seams).unwrap();
        assert_eq!(report["version"], "0.2.0", "{report}");
        let status = install::status(&roots).unwrap();
        assert_eq!(
            (status["current"].as_str(), status["previous"].as_str()),
            (Some("0.2.0"), None)
        );
        assert_eq!(sha256_file(&installed_exe("0.2.0")), exe_old);
        same("fresh install");

        // Upgrade to the default bundle as 0.2.1: the old version stays installed as `previous`.
        let report = install::install(&new, &roots, &seams).unwrap();
        assert_eq!(report["version"], "0.2.1", "{report}");
        let status = install::status(&roots).unwrap();
        assert_eq!(
            (status["current"].as_str(), status["previous"].as_str()),
            (Some("0.2.1"), Some("0.2.0"))
        );
        assert_eq!(sha256_file(&installed_exe("0.2.1")), exe_new);
        assert_eq!(
            sha256_file(&installed_exe("0.2.0")),
            exe_old,
            "previous was touched"
        );
        let payload = |version: &str| roots.version_dir(version).join(install::PAYLOAD_DIRECTORY);
        assert_eq!(
            install::bundle_digest(&payload("0.2.1")).unwrap(),
            digest_new
        );
        assert_eq!(
            install::bundle_digest(&payload("0.2.0")).unwrap(),
            digest_old
        );
        assert_eq!(
            fs::read_link(roots.application_entry()).unwrap(),
            payload("0.2.1")
        );
        same("upgrade");

        // Rollback: the entry points at the old build again, whole-tree digest equal to its source.
        let report = install::rollback(&roots, &seams).unwrap();
        assert_eq!(report["version"], "0.2.0", "{report}");
        let status = install::status(&roots).unwrap();
        assert_eq!(
            (status["current"].as_str(), status["previous"].as_str()),
            (Some("0.2.0"), Some("0.2.1"))
        );
        assert_eq!(
            fs::read_link(roots.application_entry()).unwrap(),
            payload("0.2.0")
        );
        assert_eq!(
            sha256_file(&roots.application_entry().join("Contents/MacOS/mycelium")),
            exe_old,
            "the application entry does not run the rolled-back build"
        );
        assert_eq!(
            install::bundle_digest(&payload("0.2.0")).unwrap(),
            digest_old
        );
        same("rollback");

        // Roll forward again: the new build, intact.
        install::rollback(&roots, &seams).unwrap();
        assert_eq!(
            sha256_file(&roots.application_entry().join("Contents/MacOS/mycelium")),
            exe_new
        );
        assert_eq!(
            install::bundle_digest(&payload("0.2.1")).unwrap(),
            digest_new
        );
        same("roll forward");
    });
    if let Err(error) = fs::remove_dir_all(&scratch) {
        eprintln!("cleanup_failed:{:?}", error.kind());
    }
    if let Err(panic) = outcome {
        std::panic::resume_unwind(panic);
    }
}
