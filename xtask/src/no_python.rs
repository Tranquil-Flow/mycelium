// SPDX-License-Identifier: AGPL-3.0-only
//! `xtask no-python` (CL-L12-05, CL-DOD-11): the final no-Python inventory of the repository tree
//! and of built bundles. v0.2's rule is zero Python in the repository, its tests, CI and the
//! shipped app; this check is the machine-readable proof, run on a tree and on a `.app`.
//!
//! Findings (`path: CODE: detail`), each an independent indicator:
//! * NP001 a Python-named path component: `.py .pyc .pyo .pyw .pyi .pyd .pyz .pth`, `__pycache__`,
//!   `site-packages`, `*.dist-info`, `*.egg-info`, `pyvenv.cfg`, `Python.framework`, `libpython*`,
//!   an interpreter name (`python`, `python3`, `python3.N`), or a symlink whose target is one;
//! * NP002 a Python shebang (`#!` first line naming python);
//! * NP003 an embedded Python archive: a ZIP (also a shebang-prefixed zipapp) holding `*.py`,
//!   `*.pyc`, `*.pyo` or `__main__.py`, or a PyInstaller archive cookie;
//! * NP004 (bundles only) Python runtime bytes inside a file: `libpython`, `Python.framework`,
//!   `Py_Initialize`, `PyConfig_`, `PYTHONHOME`, the PyInstaller cookie.
//!
//! Repository mode skips exactly `.git` and `target` at the root (VCS data and build output); it
//! reads names, the first line of every file and the ZIP directory of ZIP-shaped files, so prose
//! that merely mentions Python (this file included) is not a finding. Bundle mode reads every byte.
use std::{
    fs::{self, File},
    io::Read,
    path::{Path, PathBuf},
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Mode {
    Repository,
    Bundle,
}
#[derive(Clone, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub struct Finding {
    pub path: String,
    pub code: &'static str,
    pub detail: String,
}
#[derive(Debug, Default)]
pub struct Report {
    pub files: u64,
    pub bytes_read: u64,
    pub findings: Vec<Finding>,
}

const SUFFIXES: [&str; 11] = [
    ".py",
    ".pyc",
    ".pyo",
    ".pyw",
    ".pyi",
    ".pyd",
    ".pyz",
    ".pth",
    ".dist-info",
    ".egg-info",
    ".pyx",
];
/// The PyInstaller CArchive cookie magic (`MEI\014\013\012\013\016`).
const PYINSTALLER_COOKIE: &[u8] = b"MEI\x0c\x0b\x0a\x0b\x0e";
const RUNTIME_NEEDLES: [&[u8]; 6] = [
    b"libpython",
    b"Python.framework",
    b"Py_Initialize",
    b"PyConfig_",
    b"PYTHONHOME",
    PYINSTALLER_COOKIE,
];

/// Whether one path component (or a symlink target's last component) names Python.
pub fn python_name(component: &str) -> bool {
    let lower = component.to_ascii_lowercase();
    let interpreter = lower == "python"
        || lower == "python3"
        || lower
            .strip_prefix("python3.")
            .is_some_and(|v| !v.is_empty() && v.bytes().all(|b| b.is_ascii_digit()));
    SUFFIXES.iter().any(|s| lower.ends_with(s))
        || interpreter
        || matches!(
            lower.as_str(),
            "__pycache__" | "site-packages" | "pyvenv.cfg" | "python.framework"
        )
        || lower.starts_with("libpython")
        || lower.contains(".cpython-")
}

/// A `#!` first line that names python (`#!/usr/bin/env python3`, `#!/usr/bin/python`, ...).
pub fn python_shebang(head: &[u8]) -> Option<String> {
    let rest = head.strip_prefix(b"#!")?;
    let line = rest.split(|b| *b == b'\n').next().unwrap_or_default();
    let text = String::from_utf8_lossy(line).to_ascii_lowercase();
    text.contains("python").then(|| format!("#!{}", text.trim()))
}

/// Names of ZIP central-directory members anywhere in `bytes` (handles prefixed zipapps).
pub fn zip_members(bytes: &[u8]) -> Vec<String> {
    let mut names = Vec::new();
    let mut at = 0;
    while let Some(offset) = find(&bytes[at..], b"PK\x01\x02") {
        let start = at + offset;
        let field = |o: usize| -> Option<usize> {
            let b = bytes.get(start + o..start + o + 2)?;
            Some(usize::from(u16::from_le_bytes([b[0], b[1]])))
        };
        if let Some(length) = field(28)
            && let Some(name) = bytes.get(start + 46..start + 46 + length)
        {
            names.push(String::from_utf8_lossy(name).into_owned());
        }
        at = start + 4;
    }
    names
}
fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}
fn python_member(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    let last = lower.rsplit('/').next().unwrap_or(&lower);
    last == "__main__.py"
        || [".py", ".pyc", ".pyo"].iter().any(|s| last.ends_with(s))
}

fn io(path: &Path, error: &std::io::Error) -> String {
    format!("NO-PYTHON: {}: {:?}", path.display(), error.kind())
}
fn relative(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .into_owned()
}

/// Every Python indicator below `root` (RED stub: finds nothing yet).
pub fn audit(root: &Path, mode: Mode) -> Result<Report, String> {
    let _unused = (mode, relative as fn(&Path, &Path) -> String, io as fn(&Path, &std::io::Error) -> String);
    fs::symlink_metadata(root).map_err(|e| io(root, &e))?;
    Ok(Report::default())
}

#[allow(dead_code)]
fn scan_file(path: &Path, rel: &str, mode: Mode, report: &mut Report) -> Result<(), String> {
    let mut file = File::open(path).map_err(|e| io(path, &e))?;
    let mut head = [0_u8; 512];
    let n = file.read(&mut head).map_err(|e| io(path, &e))?;
    report.bytes_read += n as u64;
    let head = &head[..n];
    if let Some(line) = python_shebang(head) {
        report.findings.push(Finding {
            path: rel.into(),
            code: "NP002",
            detail: format!("python shebang {line}"),
        });
    }
    // Read the whole file only when it can hold an archive (ZIP magic or a script prefix) or in
    // bundle mode, where every byte of the shipped app is inspected.
    let whole = mode == Mode::Bundle || head.starts_with(b"PK\x03\x04") || head.starts_with(b"#!");
    if !whole {
        return Ok(());
    }
    let mut bytes = head.to_vec();
    file.read_to_end(&mut bytes).map_err(|e| io(path, &e))?;
    report.bytes_read += (bytes.len() - n) as u64;
    for member in zip_members(&bytes) {
        if python_member(&member) {
            report.findings.push(Finding {
                path: rel.into(),
                code: "NP003",
                detail: format!("archive member {member}"),
            });
        }
    }
    if find(&bytes, PYINSTALLER_COOKIE).is_some() {
        report.findings.push(Finding {
            path: rel.into(),
            code: "NP003",
            detail: "PyInstaller archive cookie".into(),
        });
    }
    if mode == Mode::Bundle {
        for needle in RUNTIME_NEEDLES {
            if needle != PYINSTALLER_COOKIE && find(&bytes, needle).is_some() {
                report.findings.push(Finding {
                    path: rel.into(),
                    code: "NP004",
                    detail: format!(
                        "python runtime bytes {:?}",
                        String::from_utf8_lossy(needle)
                    ),
                });
            }
        }
    }
    Ok(())
}

pub const USAGE: &str =
    "USAGE: xtask no-python [--root DIR] [--bundle APP]...  (exit 0 clean, 1 findings, 2 error)";

/// `xtask no-python`: the repository (default: this checkout) and every `--bundle`.
pub fn cli(args: &[String]) -> Result<u8, String> {
    let mut root: Option<PathBuf> = None;
    let mut bundles = Vec::new();
    let mut rest = args.iter();
    while let Some(arg) = rest.next() {
        match arg.as_str() {
            "--root" if root.is_none() => {
                root = Some(PathBuf::from(rest.next().ok_or(USAGE)?));
            }
            "--bundle" => bundles.push(PathBuf::from(rest.next().ok_or(USAGE)?)),
            _ => return Err(format!("{USAGE}\nunknown or repeated option {arg}")),
        }
    }
    let root = match root {
        Some(root) => root,
        None => PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .ok_or("NO-PYTHON: no workspace parent")?
            .to_path_buf(),
    };
    let mut total = 0;
    for (label, dir, mode) in std::iter::once(("repository", root, Mode::Repository))
        .chain(bundles.into_iter().map(|b| ("bundle", b, Mode::Bundle)))
    {
        let report = audit(&dir, mode)?;
        for finding in &report.findings {
            println!("{}: {}: {}", finding.path, finding.code, finding.detail);
        }
        println!(
            "no-python {label} {}: {} files, {} bytes read, {} findings",
            dir.display(),
            report.files,
            report.bytes_read,
            report.findings.len()
        );
        total += report.findings.len();
    }
    Ok(u8::from(total != 0))
}
