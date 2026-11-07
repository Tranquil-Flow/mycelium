// SPDX-License-Identifier: AGPL-3.0-only
//! Minimum-OS receipt for the packaged bundle. It computes the static floor from the bundle's
//! own Mach-O load commands and `Info.plist`, and consumes (by digest, never by trust in a
//! path) the two proofs only another host can produce: L8's load+compute proof of the pinned
//! libraries on macOS 15.6.1, and L11's run of the packaged executable on that OS. A proof that
//! is absent is `NOT_RUN`; nothing is inferred from the host this process runs on.
use crate::bundle_admission::AdmittedFile;
use crate::effects::{Command, CommandKind, Transport};
use crate::evidence_inputs::Content;
use crate::{Fault, FaultKind, Result};
use budgets::Span;
use serde::Serialize;
use sha2::{Digest, Sha256};

pub const RECEIPT_SCHEMA: &str = "mycelium.l12.minimum-os.v1";
/// The OS the proofs must name.
pub const MINIMUM_OS: &str = "15.6.1";
pub const MINIMUM_OS_BUILD: &str = "24G90";

fn fault(code: &'static str) -> Fault {
    Fault::new(code, FaultKind::Admission)
}

/// `minos` of the `LC_BUILD_VERSION` command in an `otool -l` dump.
pub fn parse_minos(otool_l: &str) -> Option<String> {
    let mut in_build = false;
    for line in otool_l.lines().map(str::trim) {
        if line.starts_with("cmd ") {
            in_build = line == "cmd LC_BUILD_VERSION";
        } else if in_build && let Some(rest) = line.strip_prefix("minos ") {
            return Some(rest.trim().to_owned());
        }
    }
    None
}

/// `otool -l` one Mach-O of the bundle through the transport. A tool that exits non-zero, cannot be
/// run, or prints no `LC_BUILD_VERSION` fails closed: it never yields a `minos`.
pub fn macho_minos<T: Transport>(
    io: &mut T,
    app: &std::path::Path,
    relative: &str,
    bound: Span,
) -> Result<MachO> {
    let out = io.execute(
        &Command {
            kind: CommandKind::Local,
            program: "/usr/bin/otool".into(),
            args: vec!["-l".into(), app.join(relative).display().to_string()],
        },
        bound,
    )?;
    if out.rc != 0 {
        return Err(fault("minos_otool_failed"));
    }
    let minos = parse_minos(&out.stdout).ok_or(fault("minos_macho_unparsed"))?;
    Ok(MachO {
        path: relative.into(),
        minos,
    })
}

/// `1.2.3` -> `[1,2,3]`; anything non-numeric is refused.
pub fn version(text: &str) -> Option<Vec<u32>> {
    let parts: Option<Vec<u32>> = text.split('.').map(|p| p.parse().ok()).collect();
    parts.filter(|p| !p.is_empty())
}
fn at_most(a: &[u32], b: &[u32]) -> bool {
    let n = a.len().max(b.len());
    let get = |v: &[u32], i: usize| v.get(i).copied().unwrap_or(0);
    (0..n)
        .map(|i| (get(a, i), get(b, i)))
        .find(|(x, y)| x != y)
        .is_none_or(|(x, y)| x < y)
}

/// `LSMinimumSystemVersion` of an Info.plist text.
pub fn plist_minimum(plist: &str) -> Option<String> {
    let mut lines = plist.lines().map(str::trim);
    lines.find(|l| *l == "<key>LSMinimumSystemVersion</key>")?;
    let value = lines.next()?;
    value
        .strip_prefix("<string>")?
        .strip_suffix("</string>")
        .map(str::to_owned)
}

#[derive(Clone, Debug, Serialize)]
pub struct MachO {
    pub path: String,
    pub minos: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct Citation {
    pub path: String,
    pub bytes: u64,
    pub sha256: String,
    pub status: &'static str,
}

fn cite(path: &str, bytes: &[u8], pin: Option<&Content>, status: &'static str) -> Result<Citation> {
    let sha256 = format!("{:x}", Sha256::digest(bytes));
    if let Some(pin) = pin {
        pin.verify(bytes)?;
    }
    Ok(Citation {
        path: path.into(),
        bytes: bytes.len() as u64,
        sha256,
        status,
    })
}

/// L8's report. The caller's `pin` binds the exact bytes; the text must name the host OS and
/// build, the scoped PASS, bit-identical outputs, and every pinned library digest prefix.
pub fn consume_l8(
    path: &str,
    bytes: &[u8],
    pin: &Content,
    library_sha256: &[&str],
) -> Result<Citation> {
    let citation = cite(path, bytes, Some(pin), "cited_pass_scoped")?;
    let text = std::str::from_utf8(bytes).map_err(|_| fault("minos_l8_proof_invalid"))?;
    let named = |token: &str| text.contains(token);
    let all_pins = library_sha256.len() == 4
        && library_sha256
            .iter()
            .all(|s| s.len() == 64 && named(&s[..8]));
    if !(named("**PASS (scoped).**")
        && named(&format!("macOS {MINIMUM_OS} ({MINIMUM_OS_BUILD})"))
        && named("bit-identical")
        && all_pins)
    {
        return Err(fault("minos_l8_proof_unbound"));
    }
    Ok(citation)
}

/// L11's run of the packaged executable on the minimum-OS host, plus the sha256 list of the exact
/// copy it ran. Absent -> `NOT_RUN`. Present -> the log must name the executable's full sha256, the
/// OS and build, the non-GUI `--install-status` run exiting 0 and the missing-library control
/// aborting in dyld (134); and the copied tree must equal the admitted bundle file for file (so the
/// proof is about THIS bundle, not a similar one).
pub struct ExecutableProof<'a> {
    pub path: &'a str,
    pub log: &'a [u8],
    /// `shasum -a 256` listing of the copy that ran (`<sha>  ./<path>`).
    pub copy_list: &'a str,
}
const L11_TOKENS: [&str; 5] = [
    "run B-install-status",
    "exit=0",
    "run D-control-libmlxc-missing",
    "exit=134",
    "minos 15.6",
];
pub fn consume_l11(
    proof: Option<&ExecutableProof>,
    admitted: &[AdmittedFile],
) -> Result<Option<Citation>> {
    let Some(proof) = proof else {
        return Ok(None);
    };
    let executable = admitted
        .iter()
        .find(|f| f.path == crate::bundle_admission::EXECUTABLE)
        .ok_or(fault("minos_executable_proof_unbound"))?;
    let text =
        std::str::from_utf8(proof.log).map_err(|_| fault("minos_executable_proof_invalid"))?;
    if !(text.contains(&executable.sha256)
        && text.contains(&format!("macOS {MINIMUM_OS} {MINIMUM_OS_BUILD}"))
        && L11_TOKENS.iter().all(|t| text.contains(t)))
    {
        return Err(fault("minos_executable_proof_unbound"));
    }
    let mut copied = std::collections::BTreeMap::new();
    for line in proof.copy_list.lines().filter(|l| !l.trim().is_empty()) {
        let (sha, path) = line
            .split_once("  ./")
            .ok_or(fault("minos_executable_proof_invalid"))?;
        if copied.insert(path, sha).is_some() {
            return Err(fault("minos_executable_proof_invalid"));
        }
    }
    let same = copied.len() == admitted.len()
        && admitted
            .iter()
            .all(|f| copied.get(f.path.as_str()) == Some(&f.sha256.as_str()));
    if !same {
        return Err(fault("minos_executable_proof_other_bundle"));
    }
    cite(proof.path, proof.log, None, "cited_present").map(Some)
}

#[derive(Debug, Serialize)]
pub struct Receipt {
    pub schema: &'static str,
    pub declared_minimum_os: &'static str,
    pub static_floor: String,
    pub macho: Vec<MachO>,
    pub libraries_on_minimum_os: Option<Citation>,
    pub packaged_executable_on_minimum_os: String,
    pub packaged_executable_citation: Option<Citation>,
    pub minimum_os_receipt_complete: bool,
    pub release_qualified: bool,
    pub not_claimed: [&'static str; 3],
}

/// Combine. Static floor = highest `minos` of any Mach-O in the bundle; it must not exceed the
/// declared minimum (the plist's `LSMinimumSystemVersion` must equal [`MINIMUM_OS`]).
pub fn receipt(
    macho: Vec<MachO>,
    plist_minimum_os: &str,
    libraries: Option<Citation>,
    executable: Option<Citation>,
) -> Result<Receipt> {
    if plist_minimum_os != MINIMUM_OS {
        return Err(fault("minos_declared_minimum_mismatch"));
    }
    let declared = version(MINIMUM_OS).ok_or(fault("minos_declared_minimum_mismatch"))?;
    let mut floor: Vec<u32> = Vec::new();
    let mut floor_text = String::new();
    for m in &macho {
        let v = version(&m.minos).ok_or(fault("minos_macho_unparsed"))?;
        if !at_most(&v, &floor) {
            floor = v;
            floor_text = m.minos.clone();
        }
    }
    if floor_text.is_empty() {
        return Err(fault("minos_macho_unparsed"));
    }
    if !at_most(&floor, &declared) {
        return Err(fault("minos_floor_above_declared_minimum"));
    }
    let complete = libraries.is_some() && executable.is_some();
    Ok(Receipt {
        schema: RECEIPT_SCHEMA,
        declared_minimum_os: MINIMUM_OS,
        static_floor: floor_text,
        macho,
        libraries_on_minimum_os: libraries,
        packaged_executable_on_minimum_os: if executable.is_some() {
            "cited".into()
        } else {
            "NOT_RUN".into()
        },
        packaged_executable_citation: executable,
        minimum_os_receipt_complete: complete,
        release_qualified: false,
        not_claimed: [
            "macos_14_x",
            "other_chips_than_proofs",
            "signature_or_gatekeeper",
        ],
    })
}
