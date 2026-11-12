// SPDX-License-Identifier: AGPL-3.0-only
//! Ad-hoc code signing of an assembled bundle at the v0.1.4 trust level (CL-L11-06).
//!
//! v0.1.4 signs with `codesign --force --sign -` (`scripts/build_a13_macos.py:167-168` @
//! f4e366e9): ad-hoc, no Developer ID, no timestamp, no notarization, no keychain identity, no
//! network. Inside-out, never `--deep` for signing:
//!
//! 1. Pinned code (the native dylibs and the resource probe) is NOT re-signed: the packaged
//!    executable admits those files by their pinned sha256 at every start, and `codesign --force`
//!    rewrites a Mach-O's bytes. Their existing signature must already be a valid ad-hoc one
//!    (the linker signs every arm64 image ad-hoc); anything else is refused.
//! 2. The executable is signed ad-hoc (its admission entry is self-measured, so this is safe).
//! 3. The bundle is sealed ad-hoc (`Contents/_CodeSignature/CodeResources`).
//! 4. `codesign --verify --deep --strict -vvv` must pass, and every pinned file must still hash to
//!    its pin afterwards.
//!
//! The receipt names every command and result with paths relative to the bundle, so the same
//! bytes give the same receipt wherever the bundle was built.
use crate::package::{Entry, Kind};
use serde_json::{Value, json};
use std::{
    path::Path,
    process::{Command, Output},
};

pub const CODESIGN: &str = "/usr/bin/codesign";
pub const TRUST_LEVEL: &str = "ad-hoc (codesign --sign -, v0.1.4 level): no Developer ID, no timestamp, no notarization, no keychain identity";

#[derive(Debug, Eq, PartialEq)]
pub struct SignError(pub String);
fn refuse(code: &str, detail: impl std::fmt::Display) -> SignError {
    SignError(format!("{code}: {detail}"))
}

fn run(args: &[&str], app: &Path) -> Result<(Output, Value), SignError> {
    let output = Command::new(CODESIGN)
        .args(args)
        .output()
        .map_err(|e| refuse("codesign_unavailable", format!("{:?}", e.kind())))?;
    let app_text = app.display().to_string();
    let relative = |text: &[u8]| -> Vec<String> {
        String::from_utf8_lossy(text)
            .lines()
            .map(|l| l.replace(&app_text, "<app>"))
            .collect()
    };
    let record = json!({
        "argv": std::iter::once(CODESIGN.to_string())
            .chain(args.iter().map(|a| a.replace(&app_text, "<app>")))
            .collect::<Vec<_>>(),
        "exit": output.status.code(),
        "stdout": relative(&output.stdout),
        "stderr": relative(&output.stderr),
    });
    Ok((output, record))
}

/// `Signature=adhoc` in `codesign -d -v` output (written on stderr).
pub fn is_adhoc_display(stderr: &str) -> bool {
    stderr.lines().any(|l| l.trim() == "Signature=adhoc")
}

/// `codesign --verify --deep` validates nested code concurrently, so its `--prepared:` and
/// `--validated:` lines arrive in another order on each run; the receipt keeps the transcript
/// sorted, so two builds of the same bytes write the same receipt (lane RELF, two-build check).
fn canonical_order(lines: &mut Value) {
    if let Some(lines) = lines.as_array_mut() {
        lines.sort_by(|a, b| a.as_str().cmp(&b.as_str()));
    }
}

/// Sign `app` ad-hoc. `pinned` = the code entries admitted by pin (dylibs and helpers); every one
/// must exist in the bundle with its pinned bytes before and after.
pub fn adhoc(app: &Path, pinned: &[Entry]) -> Result<Value, SignError> {
    let mut steps = Vec::new();
    let path = |relative: &str| app.join(relative).display().to_string();
    let mut code: Vec<&Entry> = pinned
        .iter()
        .filter(|e| matches!(e.kind, Kind::Dylib | Kind::Helper))
        .collect();
    code.sort_by(|a, b| a.path.cmp(&b.path));
    if code.is_empty() {
        return Err(refuse("sign_no_pinned_code", "no dylib or helper pin"));
    }
    let check_pins = |when: &str| -> Result<Vec<Value>, SignError> {
        code.iter()
            .map(|entry| {
                let (bytes, sha256) =
                    crate::bundle::measure(&app.join(&entry.path)).map_err(|e| {
                        refuse("sign_pinned_unreadable", format!("{}: {e:?}", entry.path))
                    })?;
                if bytes != entry.bytes || sha256 != entry.sha256 {
                    return Err(refuse(
                        "sign_pinned_bytes_changed",
                        format!(
                            "{} {when}: {sha256} ({bytes} B), pinned {}",
                            entry.path, entry.sha256
                        ),
                    ));
                }
                Ok(json!({"path": entry.path, "sha256": sha256, "bytes": bytes}))
            })
            .collect()
    };
    let pinned_before = check_pins("before signing")?;
    for entry in &code {
        let file = path(&entry.path);
        let (verify, record) = run(&["--verify", "--strict", "-v", &file], app)?;
        steps.push(record);
        let (display, record) = run(&["-d", "-v", &file], app)?;
        steps.push(record);
        if !verify.status.success()
            || !display.status.success()
            || !is_adhoc_display(&String::from_utf8_lossy(&display.stderr))
        {
            return Err(refuse("pinned_code_not_adhoc_signed", &entry.path));
        }
    }
    let exe = format!("Contents/MacOS/{}", crate::bundle::EXECUTABLE);
    let exe_before = crate::bundle::measure(&app.join(&exe))
        .map_err(|e| refuse("sign_executable_unreadable", format!("{e:?}")))?;
    let app_text = app.display().to_string();
    for target in [path(&exe), app_text.clone()] {
        let (signed, record) = run(
            &["--force", "--sign", "-", "--timestamp=none", &target],
            app,
        )?;
        steps.push(record);
        if !signed.status.success() {
            return Err(refuse(
                "codesign_failed",
                target.replace(&app_text, "<app>"),
            ));
        }
    }
    let (verified, mut verify_record) =
        run(&["--verify", "--deep", "--strict", "-vvv", &app_text], app)?;
    canonical_order(&mut verify_record["stderr"]);
    steps.push(verify_record.clone());
    if !verified.status.success() {
        return Err(refuse(
            "codesign_verify_failed",
            verify_record["stderr"].to_string(),
        ));
    }
    let (display, record) = run(&["-d", "-vvv", &app_text], app)?;
    steps.push(record.clone());
    if !display.status.success() || !is_adhoc_display(&String::from_utf8_lossy(&display.stderr)) {
        return Err(refuse(
            "bundle_signature_not_adhoc",
            record["stderr"].to_string(),
        ));
    }
    let pinned_after = check_pins("after signing")?;
    let exe_after = crate::bundle::measure(&app.join(&exe))
        .map_err(|e| refuse("sign_executable_unreadable", format!("{e:?}")))?;
    let seal = crate::bundle::measure(&app.join(crate::package::CODE_RESOURCES_PATH))
        .map_err(|e| refuse("sign_seal_missing", format!("{e:?}")))?;
    Ok(json!({
        "schema": "mycelium.l11.adhoc-signature.v1",
        "spdx_license_identifier": "AGPL-3.0-only",
        "trust_level": TRUST_LEVEL,
        "oracle": "scripts/build_a13_macos.py:167-168 @ f4e366e98a1f705dfaf182897044500d290dbd7f",
        "policy": "inside-out: pinned code keeps its verified ad-hoc linker signature (its bytes are admitted by pin at every start); the executable, then the bundle are signed ad-hoc; no --deep signing",
        "pinned_code": pinned_before,
        "pinned_code_after": pinned_after,
        "executable": {"path": exe, "sha256_before": exe_before.1, "bytes_before": exe_before.0,
                       "sha256_after": exe_after.1, "bytes_after": exe_after.0},
        "code_resources": {"path": crate::package::CODE_RESOURCES_PATH, "sha256": seal.1, "bytes": seal.0},
        "verify": verify_record,
        "steps": steps,
    }))
}
