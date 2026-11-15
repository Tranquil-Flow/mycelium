// SPDX-License-Identifier: AGPL-3.0-only
//! Lane BND (REPORT-laneTEN D2, REVIEW-laneTEN-astra F2): the stage worker a joined member's
//! custody launches is the program the composition ADMITTED, never the development environment's
//! `MYCELIUM_COMPILER_NATIVE_BINARY` / `_SHA256`. Each case runs in a child of this test binary
//! with those variables naming a different executable, so the environment is present and must
//! have no effect. The "workers" are `/bin/sh` scripts that append their own name to a marker file
//! and exit: which one ran is read from the marker, no model or Metal is involved. They live
//! outside the pair's root (the shard sandbox denies reads under the forbidden source).
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
};

pub const CASE: &str = "MYCELIUM_BND_STAGE_CASE";
pub const DIR: &str = "MYCELIUM_BND_STAGE_DIR";

pub fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "bnd-stage-{name}-{}-{}",
        std::process::id(),
        api::app_host::hex(&api::app_host::random(6).unwrap())
    ));
    fs::create_dir(&dir).unwrap();
    dir.canonicalize().unwrap()
}
pub fn marker(dir: &Path) -> PathBuf {
    dir.join("launched.txt")
}
/// An executable that records `name` in the marker file and exits non-zero.
pub fn script(dir: &Path, name: &str) -> PathBuf {
    let path = dir.join(name);
    fs::write(
        &path,
        format!(
            "#!/bin/sh\necho {name} >> '{}'\nexit 3\n",
            marker(dir).display()
        ),
    )
    .unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
    path
}
pub fn sha256(path: &Path) -> String {
    protocol::canonical::sha256_prefixed(&fs::read(path).unwrap())[7..].to_owned()
}
/// The identity a composition captures at admission: path and the bytes it had then.
pub fn admitted(path: &Path) -> api::native_compiler::NativeProgram {
    api::native_compiler::NativeProgram {
        binary: path.to_owned(),
        expected: api::compiled_handoff::Pin {
            bytes: fs::metadata(path).unwrap().len(),
            sha256: sha256(path),
        },
    }
}
/// Runs `child` (an ignored test of this binary) with the case and the development variables
/// naming `environment`, a valid pin included. Returns the marker's lines.
pub fn run_child(child: &str, case: &str, dir: &Path, environment: &Path) -> Vec<String> {
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", child, "--ignored", "--nocapture"])
        .env(CASE, case)
        .env(DIR, dir)
        .env("MYCELIUM_COMPILER_NATIVE_BINARY", environment)
        .env("MYCELIUM_COMPILER_NATIVE_SHA256", sha256(environment))
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&output.stdout).into_owned()
        + &String::from_utf8_lossy(&output.stderr);
    assert!(output.status.success(), "{case}: {text}");
    assert!(text.contains("1 passed"), "{case}: {text}");
    fs::read_to_string(marker(dir))
        .unwrap_or_default()
        .lines()
        .map(str::to_owned)
        .collect()
}
