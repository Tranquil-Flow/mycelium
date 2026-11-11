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

/// Sign `app` ad-hoc (RED stub: not implemented yet).
pub fn adhoc(app: &Path, pinned: &[Entry]) -> Result<Value, SignError> {
    let _unused = (app, pinned, json!({}), Kind::Helper, run as fn(&[&str], &Path) -> _);
    Err(refuse("sign_not_implemented", "RED"))
}
