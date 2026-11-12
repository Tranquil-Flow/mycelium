// SPDX-License-Identifier: AGPL-3.0-only
//! `xtask release check-bundle <check> --manifest M --manifest-sha256 S [--repo DIR]`: the release
//! cells a tool can check, produced BY the tool against the frozen candidate (lane RELF item 4,
//! review F2). Before and after the check the frozen executable and bundle are re-hashed
//! (`Manifest::verify_bytes`); the check reads the bundle at the manifest's own `bundle_dir`, never
//! a path given on the command line, and `--manifest-sha256` must be the manifest file's sha256, so
//! the record's argv names exactly which candidate was checked. The last line is
//! `release check-bundle <check>: PASS manifest_sha256 <S> ...` (exit 0) or `...: FAIL <why>`
//! (exit 1); a usage or manifest error exits 2.
//!
//! * `adhoc-signature` (CL-L11-06): `codesign --verify --deep --strict -vvv` passes and the seal is
//!   ad-hoc for `org.mycelium.app` with no team (v0.1.4's level, `build_a13_macos.py:167-168`).
//! * `bundle-admission` (CL-L13-01): the startup admission rules re-implemented here against the
//!   SOURCE tree's pins (`crates/desktop/{package,helper}-pins.json` at the candidate commit): every
//!   pinned file has its pinned size and sha256 (the Metal entry may be exactly the
//!   `../Resources/mlx.metallib` link), no link on any other pinned path, and nothing unpinned in
//!   `Contents/MacOS` or `Contents/Frameworks` except the self-measured executable. Pinned Helpers
//!   must also be executable regular files, as startup admission requires.
//! * `relocation` (CL-L11-02): a clone of the bundle under a deeper path with a space has the same
//!   bundle digest, still verifies and is still admitted; the copy is removed.
//! * `release-build` (CL-L12-01): the recipe's outputs beside the bundle describe exactly these
//!   bytes: `APP-FILES.sha256` lists every regular file with its sha256, `CONTENT-MANIFEST.json`
//!   names v0.2's identity and its entries match the files, `SIGN-RECEIPT.json` names the frozen
//!   executable, and `Info.plist` carries `org.mycelium.app`.
use crate::release::{self, Manifest, fail, io_text, usage};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    process::Command,
};

pub const CHECKS: [&str; 4] = [
    "release-build",
    "adhoc-signature",
    "bundle-admission",
    "relocation",
];
const USAGE: &str = "xtask release check-bundle <release-build|adhoc-signature|bundle-admission|relocation> --manifest M --manifest-sha256 S [--repo DIR]";
const CODESIGN: &str = "/usr/bin/codesign";
const BUNDLE_ID: &str = "org.mycelium.app";
const EXECUTABLE: &str = "Contents/MacOS/mycelium";
const METAL_PATH: &str = "Contents/Frameworks/mlx.metallib";
const METAL_LINK_TARGET: &str = "../Resources/mlx.metallib";
const METAL_STORE_PATH: &str = "Contents/Resources/mlx.metallib";
const CODE_DIRECTORIES: [&str; 2] = ["Contents/MacOS", "Contents/Frameworks"];

type Checked = Result<Vec<String>, String>;

fn sha256_file(path: &Path) -> Result<(String, u64), String> {
    let bytes = fs::read(path).map_err(|e| format!("{}: {}", path.display(), io_text(&e)))?;
    Ok((format!("{:x}", Sha256::digest(&bytes)), bytes.len() as u64))
}
fn is_link(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink())
}

fn codesign(args: &[&str], bundle: &Path) -> Result<(bool, String), String> {
    let output = Command::new(CODESIGN)
        .args(args)
        .arg(bundle)
        .output()
        .map_err(|e| format!("codesign unavailable: {}", io_text(&e)))?;
    let shown = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )
    .replace(&bundle.display().to_string(), "<bundle>");
    Ok((output.status.success(), shown))
}

fn adhoc_signature(bundle: &Path) -> Checked {
    let mut report = Vec::new();
    let (verified, shown) = codesign(&["--verify", "--deep", "--strict", "-vvv"], bundle)?;
    report.push(format!(
        "codesign --verify --deep --strict -vvv: exit {}",
        if verified { 0 } else { 1 }
    ));
    report.extend(shown.lines().map(|l| format!("  {l}")));
    if !verified {
        return Err(format!(
            "signature_invalid: codesign --verify --deep --strict failed: {}",
            shown.lines().last().unwrap_or_default()
        ));
    }
    let (displayed, shown) = codesign(&["-d", "-vvv"], bundle)?;
    let lines: Vec<&str> = shown.lines().map(str::trim).collect();
    for want in [
        "Signature=adhoc".to_string(),
        format!("Identifier={BUNDLE_ID}"),
        "TeamIdentifier=not set".to_string(),
    ] {
        if !displayed || !lines.contains(&want.as_str()) {
            return Err(format!(
                "signature_not_v014_level: codesign -d lacks {want}"
            ));
        }
        report.push(format!("codesign -d: {want}"));
    }
    Ok(report)
}

#[derive(Debug)]
struct Pin {
    path: String,
    kind: String,
    bytes: u64,
    sha256: String,
}
fn read_pins(repo: &Path) -> Result<Vec<Pin>, String> {
    let mut pins = Vec::new();
    for name in ["package-pins.json", "helper-pins.json"] {
        let path = repo.join("crates/desktop").join(name);
        let text = fs::read_to_string(&path)
            .map_err(|e| format!("pins_unreadable: {}: {}", path.display(), io_text(&e)))?;
        let rows: Vec<Value> = serde_json::from_str(&text)
            .map_err(|e| format!("pins_invalid: {}: {e}", path.display()))?;
        for row in rows {
            let field = |key: &str| row.get(key).and_then(Value::as_str).map(str::to_string);
            let (Some(path), Some(kind), Some(sha256), Some(bytes)) = (
                field("path"),
                field("kind"),
                field("sha256"),
                row.get("bytes").and_then(Value::as_u64),
            ) else {
                return Err(format!(
                    "pins_invalid: {name}: a row lacks path/kind/bytes/sha256"
                ));
            };
            if !path.starts_with("Contents/") || path.split('/').any(|c| c.is_empty() || c == "..")
            {
                return Err(format!("pins_invalid: {name}: {path}"));
            }
            pins.push(Pin {
                path,
                kind,
                bytes,
                sha256,
            });
        }
    }
    Ok(pins)
}
/// No link on any component below the bundle root.
fn no_link_on(bundle: &Path, relative: &str) -> bool {
    let mut path = bundle.to_path_buf();
    relative.split('/').all(|component| {
        path.push(component);
        !is_link(&path)
    })
}

fn bundle_admission(bundle: &Path, repo: &Path) -> Checked {
    let pins = read_pins(repo)?;
    let mut report = vec![format!(
        "pins: {} from {}/crates/desktop/{{package,helper}}-pins.json",
        pins.len(),
        repo.display()
    )];
    for pin in &pins {
        let file =
            if pin.kind == "Metal" && pin.path == METAL_PATH && is_link(&bundle.join(METAL_PATH)) {
                let target = fs::read_link(bundle.join(METAL_PATH)).map_err(|e| io_text(&e))?;
                if target != Path::new(METAL_LINK_TARGET) {
                    return Err(format!(
                        "pinned_link_elsewhere: {} -> {}",
                        pin.path,
                        target.display()
                    ));
                }
                METAL_STORE_PATH
            } else {
                pin.path.as_str()
            };
        if !no_link_on(bundle, file) {
            return Err(format!("pinned_path_has_link: {}", pin.path));
        }
        // RELF3/F18: agree with startup's verify_closed, not only its byte pins.
        if pin.kind == "Helper" {
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let meta = fs::symlink_metadata(bundle.join(file))
                    .map_err(|e| format!("pinned_helper_missing: {}: {}", pin.path, io_text(&e)))?;
                if !meta.is_file() || meta.permissions().mode() & 0o111 == 0 {
                    return Err(format!("pinned_helper_not_executable: {}", pin.path));
                }
            }
            #[cfg(not(unix))]
            return Err(format!(
                "pinned_helper_mode_check_unavailable: {}",
                pin.path
            ));
        }
        let (sha, bytes) = match sha256_file(&bundle.join(file)) {
            Ok(found) => found,
            Err(e) => return Err(format!("pinned_file_missing: {}: {e}", pin.path)),
        };
        if sha != pin.sha256 || bytes != pin.bytes {
            return Err(format!(
                "pinned_file_mismatch: {} is {sha} ({bytes} B), pinned {} ({} B)",
                pin.path, pin.sha256, pin.bytes
            ));
        }
        report.push(format!("pinned ok: {} {} {sha}", pin.kind, pin.path));
    }
    if !no_link_on(bundle, EXECUTABLE) || !bundle.join(EXECUTABLE).is_file() {
        return Err(format!("executable_missing: {EXECUTABLE}"));
    }
    fn walk(bundle: &Path, dir: &Path, out: &mut Vec<(String, bool)>) -> Result<(), String> {
        let mut children = fs::read_dir(dir)
            .map_err(|e| format!("{}: {}", dir.display(), io_text(&e)))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("{}: {}", dir.display(), io_text(&e)))?;
        children.sort_by_key(fs::DirEntry::file_name);
        for child in children {
            let path = child.path();
            let kind = child.file_type().map_err(|e| io_text(&e))?;
            if kind.is_dir() {
                walk(bundle, &path, out)?;
            } else {
                let relative = path
                    .strip_prefix(bundle)
                    .map_err(|e| e.to_string())?
                    .to_string_lossy()
                    .into_owned();
                out.push((relative, kind.is_symlink()));
            }
        }
        Ok(())
    }
    let mut found = Vec::new();
    for directory in CODE_DIRECTORIES {
        let path = bundle.join(directory);
        if is_link(&path) {
            return Err(format!("code_directory_is_link: {directory}"));
        }
        if !path.exists() {
            continue; // nothing there to execute; a pinned file in it was already missing above
        }
        walk(bundle, &path, &mut found)?;
    }
    for (relative, link) in &found {
        let pinned = pins.iter().find(|p| &p.path == relative);
        let allowed = match pinned {
            Some(pin) => !link || pin.kind == "Metal",
            None => relative == EXECUTABLE && !link,
        };
        if !allowed {
            return Err(format!(
                "unpinned {} in a code directory: {relative}",
                if *link { "link" } else { "file" }
            ));
        }
    }
    report.push(format!(
        "closed: {} entries in Contents/MacOS + Contents/Frameworks, all pinned or the self-measured executable",
        found.len()
    ));
    Ok(report)
}

fn relocation(bundle: &Path, repo: &Path, manifest: &Manifest) -> Checked {
    let name = bundle
        .file_name()
        .ok_or_else(|| "bundle_name_missing".to_string())?;
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|e| e.to_string())?
        .as_nanos();
    let root =
        std::env::temp_dir().join(format!("xtask-relocation-{}-{nanos}", std::process::id()));
    let parent = root.join("relocated copy").join("deeper level");
    fs::create_dir_all(&parent).map_err(|e| format!("relocation_dir: {}", io_text(&e)))?;
    let copy = parent.join(name);
    let result = (|| -> Checked {
        let cloned = Command::new("/bin/cp")
            .arg("-Rc")
            .arg(bundle)
            .arg(&copy)
            .output()
            .map_err(|e| format!("cp unavailable: {}", io_text(&e)))?;
        if !cloned.status.success() {
            return Err(format!(
                "relocation_copy_failed: {}",
                String::from_utf8_lossy(&cloned.stderr).trim()
            ));
        }
        let mut report = vec!["copy: <tmp>/relocated copy/deeper level/<bundle> (cp -Rc)".into()];
        let digest = release::bundle_digest(&copy)?;
        if digest != manifest.candidate.bundle_sha256 {
            return Err(format!(
                "relocation_bytes_changed: the copy lists as {digest}, frozen {}",
                manifest.candidate.bundle_sha256
            ));
        }
        report.push(format!("copy bundle digest = frozen {digest}"));
        report.extend(
            adhoc_signature(&copy)?
                .into_iter()
                .map(|l| format!("copy: {l}")),
        );
        report.extend(
            bundle_admission(&copy, repo)?
                .into_iter()
                .map(|l| format!("copy: {l}")),
        );
        Ok(report)
    })();
    fs::remove_dir_all(&root).map_err(|e| format!("relocation_cleanup: {}", io_text(&e)))?;
    result
}

fn release_build(bundle: &Path, manifest: &Manifest) -> Checked {
    let out = bundle
        .parent()
        .ok_or_else(|| "bundle_parent_missing".to_string())?;
    let mut report = Vec::new();
    // Every regular file of the bundle, by its sha256.
    let mut files = BTreeMap::new();
    fn walk(bundle: &Path, dir: &Path, out: &mut BTreeMap<String, String>) -> Result<(), String> {
        for child in fs::read_dir(dir)
            .map_err(|e| format!("{}: {}", dir.display(), io_text(&e)))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("{}: {}", dir.display(), io_text(&e)))?
        {
            let path = child.path();
            let kind = child.file_type().map_err(|e| io_text(&e))?;
            if kind.is_dir() {
                walk(bundle, &path, out)?;
            } else if kind.is_file() {
                let relative = path
                    .strip_prefix(bundle)
                    .map_err(|e| e.to_string())?
                    .to_string_lossy()
                    .into_owned();
                out.insert(relative, sha256_file(&path)?.0);
            }
        }
        Ok(())
    }
    walk(bundle, bundle, &mut files)?;
    let listing_path = out.join("APP-FILES.sha256");
    let listing = fs::read_to_string(&listing_path)
        .map_err(|e| format!("build_output_missing: APP-FILES.sha256: {}", io_text(&e)))?;
    let mut listed = BTreeMap::new();
    for line in listing.lines().filter(|l| !l.trim().is_empty()) {
        let Some((sha, relative)) = line.split_once("  ") else {
            return Err(format!(
                "build_output_invalid: APP-FILES.sha256 line {line:?}"
            ));
        };
        listed.insert(relative.to_string(), sha.to_string());
    }
    for (relative, sha) in &files {
        match listed.get(relative) {
            Some(want) if want == sha => {}
            Some(want) => {
                return Err(format!(
                    "build_output_disagrees: APP-FILES.sha256 lists {relative} as {want}, the bundle has {sha}"
                ));
            }
            None => {
                return Err(format!(
                    "build_output_disagrees: {relative} is not listed in APP-FILES.sha256"
                ));
            }
        }
    }
    if let Some(extra) = listed.keys().find(|k| !files.contains_key(*k)) {
        return Err(format!(
            "build_output_disagrees: APP-FILES.sha256 lists {extra}, absent from the bundle"
        ));
    }
    report.push(format!(
        "APP-FILES.sha256: {} files, all equal",
        files.len()
    ));
    let read_json = |name: &str| -> Result<Value, String> {
        let text = fs::read_to_string(out.join(name))
            .map_err(|e| format!("build_output_missing: {name}: {}", io_text(&e)))?;
        serde_json::from_str(&text).map_err(|e| format!("build_output_invalid: {name}: {e}"))
    };
    let content = read_json("CONTENT-MANIFEST.json")?;
    if content["identity"]["bundle_identifier"] != BUNDLE_ID {
        return Err(format!(
            "build_identity_wrong: CONTENT-MANIFEST.json identity {}",
            content["identity"]
        ));
    }
    let entries = content["entries"]
        .as_array()
        .filter(|e| !e.is_empty())
        .ok_or_else(|| "build_output_invalid: CONTENT-MANIFEST.json has no entries".to_string())?;
    for entry in entries {
        let (Some(path), Some(want)) = (entry["path"].as_str(), entry["sha256"].as_str()) else {
            return Err("build_output_invalid: a CONTENT-MANIFEST entry lacks path/sha256".into());
        };
        let stored = if path == METAL_PATH && is_link(&bundle.join(METAL_PATH)) {
            METAL_STORE_PATH
        } else {
            path
        };
        match files.get(stored) {
            Some(have) if have == want => {}
            other => {
                return Err(format!(
                    "build_output_disagrees: CONTENT-MANIFEST.json {path} {want}, the bundle has {other:?}"
                ));
            }
        }
    }
    report.push(format!(
        "CONTENT-MANIFEST.json: {BUNDLE_ID}, {} entries equal",
        entries.len()
    ));
    let receipt = read_json("SIGN-RECEIPT.json")?;
    if receipt["executable"]["sha256_after"] != manifest.candidate.exe_sha256.as_str() {
        return Err(format!(
            "build_output_disagrees: SIGN-RECEIPT.json executable {}, frozen {}",
            receipt["executable"]["sha256_after"], manifest.candidate.exe_sha256
        ));
    }
    report.push("SIGN-RECEIPT.json: executable = frozen exe".into());
    let plist = fs::read_to_string(bundle.join("Contents/Info.plist"))
        .map_err(|e| format!("build_output_missing: Info.plist: {}", io_text(&e)))?;
    if !plist.contains(&format!("<string>{BUNDLE_ID}</string>")) {
        return Err(format!(
            "build_identity_wrong: Info.plist lacks {BUNDLE_ID}"
        ));
    }
    report.push(format!("Info.plist: {BUNDLE_ID}"));
    Ok(report)
}

/// `xtask release check-bundle ...` (arguments after `check-bundle`).
pub fn cli(args: &[String]) -> Result<u8, String> {
    let Some((check, rest)) = args.split_first() else {
        return Err(usage(USAGE));
    };
    if !CHECKS.contains(&check.as_str()) {
        return Err(usage(&format!("unknown check {check}; {USAGE}")));
    }
    let mut values = BTreeMap::new();
    let mut it = rest.iter();
    while let Some(key) = it.next() {
        if !["--manifest", "--manifest-sha256", "--repo"].contains(&key.as_str()) {
            return Err(usage(&format!("unknown option {key}; {USAGE}")));
        }
        let value = it
            .next()
            .ok_or_else(|| usage(&format!("{key} needs a value")))?;
        if values.insert(key.as_str(), value.clone()).is_some() {
            return Err(usage(&format!("repeated option {key}")));
        }
    }
    let need = |key: &str| {
        values
            .get(key)
            .cloned()
            .ok_or_else(|| usage(&format!("{key} is required; {USAGE}")))
    };
    let manifest = Manifest::load(Path::new(&need("--manifest")?))?;
    let named = need("--manifest-sha256")?;
    if manifest.sha256 != named {
        return Err(fail(
            "check_manifest_mismatch",
            format!(
                "the manifest file hashes to {}, the command names {named}",
                manifest.sha256
            ),
        ));
    }
    let repo = match values.get("--repo") {
        Some(repo) => PathBuf::from(repo),
        None => PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .ok_or_else(|| fail("check_repo_unknown", "no workspace parent"))?
            .to_path_buf(),
    };
    let outcome = (|| -> Checked {
        manifest.verify_bytes()?;
        let bundle = fs::canonicalize(&manifest.bundle_dir)
            .map_err(|e| format!("bundle_unreadable: {}", io_text(&e)))?;
        let exe = fs::canonicalize(&manifest.exe_path)
            .map_err(|e| format!("exe_unreadable: {}", io_text(&e)))?;
        if exe != bundle.join(EXECUTABLE) {
            return Err(format!(
                "check_exe_outside_bundle: the frozen exe {} is not <bundle>/{EXECUTABLE}",
                exe.display()
            ));
        }
        let mut report = vec![format!(
            "frozen bytes re-hashed: exe {} bundle {}",
            manifest.candidate.exe_sha256, manifest.candidate.bundle_sha256
        )];
        report.extend(match check.as_str() {
            "adhoc-signature" => adhoc_signature(&bundle)?,
            "bundle-admission" => bundle_admission(&bundle, &repo)?,
            "relocation" => relocation(&bundle, &repo, &manifest)?,
            _ => release_build(&bundle, &manifest)?,
        });
        manifest.verify_bytes()?;
        report.push("frozen bytes re-hashed after the check: unchanged".into());
        Ok(report)
    })();
    match outcome {
        Ok(report) => {
            for line in report {
                println!("{line}");
            }
            println!(
                "release check-bundle {check}: PASS manifest_sha256 {} exe_sha256 {} bundle_sha256 {}",
                manifest.sha256, manifest.candidate.exe_sha256, manifest.candidate.bundle_sha256
            );
            Ok(0)
        }
        Err(why) => {
            println!("release check-bundle {check}: FAIL {why}");
            Ok(1)
        }
    }
}
