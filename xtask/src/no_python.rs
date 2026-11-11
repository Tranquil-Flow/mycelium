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
//! * NP005 Python source in a file without an extension (and without a shebang): a line that is a
//!   Python `import`/`from ... import`, `def f(...):`, `class C(...):` statement or the
//!   `__name__ == "__main__"` guard (lane RELF, review F3);
//! * NP006 a shell script (a `#!` file or `.sh .bash .zsh .ksh .command`) that runs an interpreter
//!   (`python`, `python3`, `python3.N` as a command word outside comments: `python3 - <<EOF`,
//!   `exec /usr/bin/env python3`).
//!
//! NP001 also names notebooks (`.ipynb`) and Python project files (`pyproject.toml`, `setup.cfg`,
//! `requirements*.txt`, `Pipfile`, `poetry.lock`, `tox.ini`, `.python-version`). NP003 also lists
//! the members of gzip/bzip2/xz-compressed or plain tar archives (`/usr/bin/tar -tf`); a compressed
//! stream that is not a tar is decompressed and scanned like a file. A tree or bundle with no file
//! at all is an error, never a pass.
//!
//! Repository mode skips exactly `.git` and `target` at the root (VCS data and build output); it
//! reads names, the first 64 KiB of every file, the tail of every file (a ZIP's end record, so a
//! prefixed ZIP is found) and all of an archive-shaped file, so prose that merely mentions Python
//! (this file included) is not a finding. Bundle mode reads every byte.
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

const SUFFIXES: [&str; 12] = [
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
    ".ipynb",
];
/// Python project and environment files (lower-case names).
const PROJECT_FILES: [&str; 8] = [
    "pyproject.toml",
    "setup.cfg",
    "pipfile",
    "pipfile.lock",
    "poetry.lock",
    "tox.ini",
    ".python-version",
    "conda-env.yml",
];
/// How much of every file repository mode reads from the front (text analysis, tar magic).
const HEAD_BYTES: usize = 64 * 1024;
/// A ZIP's end-of-central-directory record sits within the last 65 557 bytes of the file.
const ZIP_TAIL_BYTES: u64 = 65_557;
/// The largest decompressed stream scanned (a bigger one is an error, not a pass).
const DECOMPRESSED_MAX_BYTES: usize = 256 * 1024 * 1024;
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
        || PROJECT_FILES.contains(&lower.as_str())
        || (lower.starts_with("requirements") && lower.ends_with(".txt"))
}

fn dotted_name(text: &str) -> bool {
    !text.is_empty()
        && text.split('.').all(|part| {
            !part.is_empty() && part.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
        })
}
fn identifier(text: &str) -> bool {
    !text.is_empty()
        && !text.as_bytes()[0].is_ascii_digit()
        && text.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
}
/// `name` or `name as alias` (an import list item).
fn import_item(item: &str, dotted: bool) -> bool {
    let words: Vec<&str> = item.split_whitespace().collect();
    let name_ok = |w: &str| {
        if dotted {
            dotted_name(w)
        } else {
            identifier(w)
        }
    };
    match words.as_slice() {
        [name] => name_ok(name),
        [name, "as", alias] => name_ok(name) && identifier(alias),
        _ => false,
    }
}
/// One line that only Python reads this way: `import a.b[, c as d]`, `from a import b[, c]`,
/// `def f(...):`, `class C[(...)]:`, or the `__name__ == "__main__"` guard.
pub fn python_statement(line: &str) -> bool {
    let line = line.trim();
    if line.contains("__name__ == \"__main__\"") || line.contains("__name__ == '__main__'") {
        return true;
    }
    if let Some(rest) = line.strip_prefix("import ") {
        return rest.split(',').all(|item| import_item(item, true));
    }
    if let Some(rest) = line.strip_prefix("from ")
        && let Some((module, names)) = rest.split_once(" import ")
    {
        let module = module.trim_start_matches('.');
        let names = names.trim().trim_start_matches('(').trim_end_matches(')');
        return (module.is_empty() || dotted_name(module))
            && (names == "*" || names.split(',').all(|i| import_item(i, false)));
    }
    if let Some(rest) = line.strip_prefix("def ") {
        return rest.ends_with(':')
            && rest
                .split_once('(')
                .is_some_and(|(name, _)| identifier(name.trim()));
    }
    if let Some(rest) = line.strip_prefix("class ") {
        let head = rest.strip_suffix(':').unwrap_or_default();
        let name = head.split_once('(').map_or(head, |(n, _)| n);
        return rest.ends_with(':')
            && identifier(name.trim())
            && (head.contains('(') == head.ends_with(')'));
    }
    false
}
/// A shell line that runs a Python interpreter as a command word (comments ignored).
pub fn python_invocation(line: &str) -> bool {
    let line = line.trim_start();
    if line.starts_with('#') {
        return false;
    }
    // Each simple command (split at `; | & ( ) $ \``); its command word is the first word after
    // assignments and the prefixes that run their argument (`exec`, `env`, `command`, ...).
    line.split(|c: char| ";|&()`$".contains(c)).any(|segment| {
        let mut words = segment.split_whitespace().peekable();
        while let Some(word) = words.peek() {
            let base = word.rsplit('/').next().unwrap_or(word);
            let prefix = matches!(
                base,
                "exec"
                    | "env"
                    | "command"
                    | "nohup"
                    | "time"
                    | "then"
                    | "do"
                    | "else"
                    | "if"
                    | "while"
                    | "until"
                    | "!"
                    | "sudo"
                    | "arch"
            );
            let assignment = word.split_once('=').is_some_and(|(k, _)| identifier(k));
            let option = word.starts_with('-');
            if prefix || assignment || option {
                words.next();
            } else {
                break;
            }
        }
        words.next().is_some_and(|word| {
            let base = word.rsplit('/').next().unwrap_or(word);
            base == "python"
                || base == "python3"
                || base
                    .strip_prefix("python3.")
                    .is_some_and(|v| !v.is_empty() && v.bytes().all(|b| b.is_ascii_digit()))
        })
    })
}
fn extensionless(name: &str) -> bool {
    !name.trim_start_matches('.').contains('.')
}
fn shell_script(name: &str, head: &[u8]) -> bool {
    let lower = name.to_ascii_lowercase();
    head.starts_with(b"#!")
        || [".sh", ".bash", ".zsh", ".ksh", ".command"]
            .iter()
            .any(|s| lower.ends_with(s))
}
/// Gzip, bzip2, xz or a POSIX tar header.
fn archive_magic(head: &[u8]) -> bool {
    head.starts_with(&[0x1f, 0x8b])
        || head.starts_with(b"BZh")
        || head.starts_with(&[0xfd, b'7', b'z', b'X', b'Z', 0])
        || head.get(257..262) == Some(b"ustar")
}

/// A `#!` first line that names python (`#!/usr/bin/env python3`, `#!/usr/bin/python`, ...).
pub fn python_shebang(head: &[u8]) -> Option<String> {
    let rest = head.strip_prefix(b"#!")?;
    let line = rest.split(|b| *b == b'\n').next().unwrap_or_default();
    let text = String::from_utf8_lossy(line).to_ascii_lowercase();
    text.contains("python")
        .then(|| format!("#!{}", text.trim()))
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
    last == "__main__.py" || [".py", ".pyc", ".pyo"].iter().any(|s| last.ends_with(s))
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

/// Every Python indicator below `root` (a directory). Errors are IO failures, never findings.
pub fn audit(root: &Path, mode: Mode) -> Result<Report, String> {
    let mut report = Report::default();
    let meta = fs::symlink_metadata(root).map_err(|e| io(root, &e))?;
    if !meta.is_dir() {
        return Err(format!("NO-PYTHON: {} is not a directory", root.display()));
    }
    let mut pending: Vec<PathBuf> = vec![root.to_path_buf()];
    while let Some(dir) = pending.pop() {
        let mut children: Vec<_> = fs::read_dir(&dir)
            .map_err(|e| io(&dir, &e))?
            .collect::<Result<_, _>>()
            .map_err(|e| io(&dir, &e))?;
        children.sort_by_key(fs::DirEntry::file_name);
        for child in children {
            let path = child.path();
            let name = child.file_name().to_string_lossy().into_owned();
            if mode == Mode::Repository && dir == root && (name == ".git" || name == "target") {
                continue;
            }
            let rel = relative(root, &path);
            let kind = child.file_type().map_err(|e| io(&path, &e))?;
            if python_name(&name) {
                report.findings.push(Finding {
                    path: rel.clone(),
                    code: "NP001",
                    detail: format!("python-named component {name}"),
                });
            }
            if kind.is_symlink() {
                let target = fs::read_link(&path).map_err(|e| io(&path, &e))?;
                if target
                    .components()
                    .any(|c| python_name(&c.as_os_str().to_string_lossy()))
                {
                    report.findings.push(Finding {
                        path: rel,
                        code: "NP001",
                        detail: format!("symlink to {}", target.display()),
                    });
                }
            } else if kind.is_dir() {
                pending.push(path);
            } else if kind.is_file() {
                report.files += 1;
                scan_file(&path, &rel, mode, &mut report)?;
            }
        }
    }
    if report.files == 0 {
        return Err(format!(
            "NO-PYTHON: {} holds no files: an empty tree is not evidence",
            root.display()
        ));
    }
    report.findings.sort();
    report.findings.dedup();
    Ok(report)
}

/// The text-level indicators of one file's bytes (NP002 shebang, NP005 source, NP006 invocation).
fn scan_text(name: &str, rel: &str, bytes: &[u8], report: &mut Report) {
    if let Some(line) = python_shebang(bytes) {
        report.findings.push(Finding {
            path: rel.into(),
            code: "NP002",
            detail: format!("python shebang {line}"),
        });
    }
    if bytes.contains(&0) {
        return; // binary: no text indicators
    }
    let text = String::from_utf8_lossy(bytes);
    if extensionless(name) && !bytes.starts_with(b"#!") {
        if let Some(line) = text.lines().find(|l| python_statement(l)) {
            report.findings.push(Finding {
                path: rel.into(),
                code: "NP005",
                detail: format!("python source statement {:?}", line.trim()),
            });
        }
    }
    if shell_script(name, bytes) && python_shebang(bytes).is_none() {
        if let Some(line) = text.lines().find(|l| python_invocation(l)) {
            report.findings.push(Finding {
                path: rel.into(),
                code: "NP006",
                detail: format!("script runs python: {:?}", line.trim()),
            });
        }
    }
}

/// Members of a (compressed) tar listed by the system `tar`; `None` when it is not a tar.
fn tar_members(path: &Path) -> Result<Option<Vec<String>>, String> {
    let out = std::process::Command::new("/usr/bin/tar")
        .arg("-tf")
        .arg(path)
        .output()
        .map_err(|e| io(path, &e))?;
    if !out.status.success() {
        return Ok(None);
    }
    Ok(Some(
        String::from_utf8_lossy(&out.stdout)
            .lines()
            .map(str::to_string)
            .collect(),
    ))
}
/// The decompressed bytes of a gzip/bzip2/xz stream (`/usr/bin/gzip|bzip2|xz -dc`), bounded.
fn decompressed(path: &Path, head: &[u8]) -> Result<Vec<u8>, String> {
    let tool = if head.starts_with(&[0x1f, 0x8b]) {
        "/usr/bin/gzip"
    } else if head.starts_with(b"BZh") {
        "/usr/bin/bzip2"
    } else {
        "/usr/bin/xz"
    };
    let out = std::process::Command::new(tool)
        .arg("-dc")
        .arg(path)
        .output()
        .map_err(|e| io(path, &e))?;
    if !out.status.success() || out.stdout.len() > DECOMPRESSED_MAX_BYTES {
        return Err(format!(
            "NO-PYTHON: {}: cannot decompress for inspection ({tool} exit {:?}, {} bytes)",
            path.display(),
            out.status.code(),
            out.stdout.len()
        ));
    }
    Ok(out.stdout)
}

fn scan_file(path: &Path, rel: &str, mode: Mode, report: &mut Report) -> Result<(), String> {
    use std::io::{Seek, SeekFrom};
    let name = path
        .file_name()
        .map_or_else(String::new, |n| n.to_string_lossy().into_owned());
    let mut file = File::open(path).map_err(|e| io(path, &e))?;
    let length = file.metadata().map_err(|e| io(path, &e))?.len();
    let mut head = Vec::with_capacity(HEAD_BYTES);
    (&mut file)
        .take(HEAD_BYTES as u64)
        .read_to_end(&mut head)
        .map_err(|e| io(path, &e))?;
    report.bytes_read += head.len() as u64;
    // Read the whole file when it can hold an archive (ZIP or tar magic, a script prefix, a ZIP
    // end record in its tail) or in bundle mode, where every byte of the shipped app is inspected.
    let mut whole = mode == Mode::Bundle
        || head.starts_with(b"PK\x03\x04")
        || head.starts_with(b"#!")
        || archive_magic(&head)
        || shell_script(&name, &head)
        || head.len() as u64 == length;
    if !whole {
        let from = length.saturating_sub(ZIP_TAIL_BYTES).max(head.len() as u64);
        file.seek(SeekFrom::Start(from)).map_err(|e| io(path, &e))?;
        let mut tail = Vec::new();
        file.read_to_end(&mut tail).map_err(|e| io(path, &e))?;
        report.bytes_read += tail.len() as u64;
        whole = find(&tail, b"PK\x05\x06").is_some();
    }
    let mut bytes = head;
    if whole && bytes.len() as u64 != length {
        file.seek(SeekFrom::Start(bytes.len() as u64))
            .map_err(|e| io(path, &e))?;
        let before = bytes.len();
        file.read_to_end(&mut bytes).map_err(|e| io(path, &e))?;
        report.bytes_read += (bytes.len() - before) as u64;
    }
    scan_text(&name, rel, &bytes, report);
    for member in zip_members(&bytes) {
        if python_member(&member) {
            report.findings.push(Finding {
                path: rel.into(),
                code: "NP003",
                detail: format!("archive member {member}"),
            });
        }
    }
    if archive_magic(&bytes) {
        match tar_members(path)? {
            Some(members) => {
                for member in members
                    .iter()
                    .filter(|m| python_member(m) || m.split('/').any(python_name))
                {
                    report.findings.push(Finding {
                        path: rel.into(),
                        code: "NP003",
                        detail: format!("archive member {member}"),
                    });
                }
            }
            None if bytes.get(257..262) != Some(b"ustar") => {
                let inner = decompressed(path, &bytes)?;
                let before = report.findings.len();
                scan_text("", rel, &inner, report);
                for member in zip_members(&inner) {
                    if python_member(&member) {
                        report.findings.push(Finding {
                            path: rel.into(),
                            code: "NP003",
                            detail: format!("compressed archive member {member}"),
                        });
                    }
                }
                for finding in &mut report.findings[before..] {
                    finding.detail = format!("inside the compressed stream: {}", finding.detail);
                }
            }
            None => {}
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
                    detail: format!("python runtime bytes {:?}", String::from_utf8_lossy(needle)),
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
