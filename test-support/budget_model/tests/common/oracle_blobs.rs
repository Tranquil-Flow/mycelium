// SPDX-License-Identifier: AGPL-3.0-only
//! Read-only access to pinned oracle blobs for re-derivation tests (never executes the oracle).
use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::path::PathBuf;

pub const ORACLE: &str = "f4e366e98a1f705dfaf182897044500d290dbd7f";
/// Environment variable that points at the read-only v0.1.4 oracle checkout.
pub const ORACLE_DIR_ENV: &str = "MYCELIUM_ORACLE_DIR";

/// Where the read-only v0.1.4 oracle checkout lives on this host: `$MYCELIUM_ORACLE_DIR` when set, else
/// `$HOME/Projects/mycelium-v014`. It only locates the checkout; it does not check that it exists.
pub fn oracle_dir() -> PathBuf {
    match std::env::var_os(ORACLE_DIR_ENV) {
        Some(dir) => PathBuf::from(dir),
        None => {
            let home = std::env::var_os("HOME").unwrap_or_else(|| {
                panic!("HOME is not set; set {ORACLE_DIR_ENV} to the v0.1.4 oracle checkout")
            });
            PathBuf::from(home).join("Projects").join("mycelium-v014")
        }
    }
}

pub fn oracle_present() -> bool {
    oracle_dir().exists()
}

/// The oracle checkout, or a panic naming the path and `MYCELIUM_ORACLE_DIR`. A test that needs the oracle calls
/// this instead of skipping when the checkout is absent: a missing oracle must fail, never pass vacuously.
pub fn oracle_dir_or_panic() -> PathBuf {
    let dir = oracle_dir();
    assert!(
        dir.exists(),
        "the pinned v0.1.4 oracle checkout is required but {} does not exist; set {ORACLE_DIR_ENV} to a read-only checkout of v0.1.4 (pin {ORACLE})",
        dir.display()
    );
    dir
}

pub fn sha256_hex(data: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    Sha256::digest(data)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// Exact blobs at the oracle pin through one `git cat-file --batch` (read-only).
pub fn blobs(paths: &[String]) -> BTreeMap<String, String> {
    let mut child = std::process::Command::new("git")
        .arg("-C")
        .arg(oracle_dir_or_panic())
        .args(["cat-file", "--batch"])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .expect("git cat-file");
    let mut stdin = child.stdin.take().expect("stdin");
    let request: String = paths.iter().map(|p| format!("{ORACLE}:{p}\n")).collect();
    let writer = std::thread::spawn(move || {
        stdin.write_all(request.as_bytes()).expect("request");
    });
    let mut out = Vec::new();
    child
        .stdout
        .take()
        .expect("stdout")
        .read_to_end(&mut out)
        .expect("read");
    writer.join().expect("writer");
    assert!(child.wait().expect("wait").success());
    let mut result = BTreeMap::new();
    let mut at = 0;
    for path in paths {
        let end = at + out[at..].iter().position(|b| *b == b'\n').expect("header");
        let header = std::str::from_utf8(&out[at..end]).expect("header utf8");
        let fields: Vec<_> = header.split(' ').collect();
        assert_eq!(fields.len(), 3, "{path}: {header}");
        assert_eq!(fields[1], "blob", "{path}: {header}");
        let size: usize = fields[2].parse().expect("size");
        let body = &out[end + 1..end + 1 + size];
        result.insert(
            path.clone(),
            String::from_utf8(body.to_vec()).expect("source utf8"),
        );
        at = end + 1 + size + 1;
    }
    result
}

/// Line `n` (one-based) split on `\n` only, without a trailing `\r`.
pub fn line_of(text: &str, n: usize) -> Option<&str> {
    text.split('\n')
        .nth(n.checked_sub(1)?)
        .map(|l| l.strip_suffix('\r').unwrap_or(l))
}
