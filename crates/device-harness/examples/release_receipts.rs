// SPDX-License-Identifier: AGPL-3.0-only
//! Release-qualification receipts for a built v0.2 bundle (content admission, relocation,
//! minimum-OS). Operator tool: every path is explicit, every write goes under the given
//! scratch output directory, and the only processes it starts are `cp -c`, `otool` and the
//! bundle's NON-GUI `--install-status` path with a scratch `HOME`, all through the confined
//! native transport. It never launches the GUI, never touches an install root, and never
//! writes outside `OUT`.
//!
//! usage: release_receipts REPO OUT APP BUILT_EXE INFO_PLIST_SHA256 L8_PROOF L8_PROOF_SHA256 [L11_PROOF_LOG L11_COPY_LIST]
use budgets::Span;
use device_harness::{
    Fault, FaultKind, Result,
    bundle_admission::{self, Expected, Statics},
    effects::{Command, CommandKind, Transport},
    evidence_inputs::Content,
    minimum_os,
    native::NativeTransport,
    relocation,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};

const COMMAND_MS: u64 = 600_000;
const POLL_MS: u64 = 50;
const MAX_OUTPUT: u64 = 4 << 20;
const NATIVE_MACHO: [&str; 4] = [
    "Contents/MacOS/mycelium",
    "Contents/Frameworks/libmlxc.dylib",
    "Contents/Frameworks/libmlx.dylib",
    "Contents/Frameworks/libjaccl.dylib",
];

fn input(code: &'static str) -> Fault {
    Fault::new(code, FaultKind::Input)
}
fn read(path: &Path) -> Result<Vec<u8>> {
    std::fs::read(path).map_err(|_| input("receipt_input_unreadable"))
}
fn content(bytes: &[u8]) -> Content {
    Content {
        size_bytes: bytes.len() as u64,
        sha256: format!("{:x}", Sha256::digest(bytes)),
    }
}
fn write(out: &Path, name: &str, value: &Value) -> Result<()> {
    std::fs::write(
        out.join(name),
        serde_json::to_vec_pretty(value).map_err(|_| input("receipt_encoding_failed"))?,
    )
    .map_err(|_| input("receipt_write_failed"))
}
fn run<T: Transport>(
    io: &mut T,
    program: &str,
    args: &[&str],
) -> Result<device_harness::effects::Output> {
    io.execute(
        &Command {
            kind: CommandKind::Local,
            program: program.into(),
            args: args.iter().map(|a| (*a).into()).collect(),
        },
        Span::from_millis(COMMAND_MS),
    )
}
fn clone_tree<T: Transport>(io: &mut T, from: &Path, to_parent: &Path) -> Result<PathBuf> {
    std::fs::create_dir_all(to_parent).map_err(|_| input("receipt_scratch_failed"))?;
    let out = run(
        io,
        "/bin/cp",
        &[
            "-cR",
            &from.display().to_string(),
            &to_parent.display().to_string(),
        ],
    )?;
    if out.rc != 0 {
        return Err(input("receipt_copy_failed"));
    }
    Ok(to_parent.join(from.file_name().ok_or(input("receipt_path_invalid"))?))
}

fn main() -> std::process::ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match drive(&args) {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(fault) => {
            eprintln!("{fault}");
            std::process::ExitCode::from(1)
        }
    }
}

fn drive(args: &[String]) -> Result<()> {
    let [repo, out, app, exe, plist_sha, l8, l8_sha, rest @ ..] = args else {
        return Err(input("usage"));
    };
    let (repo, out, app, exe) = (
        Path::new(repo),
        Path::new(out),
        Path::new(app),
        Path::new(exe),
    );
    let app = app
        .canonicalize()
        .map_err(|_| input("receipt_path_invalid"))?;
    std::fs::create_dir_all(out).map_err(|_| input("receipt_scratch_failed"))?;
    let out = out
        .canonicalize()
        .map_err(|_| input("receipt_path_invalid"))?;
    let scratch = out.join("transport");
    std::fs::create_dir_all(&scratch).map_err(|_| input("receipt_scratch_failed"))?;
    std::fs::set_permissions(&scratch, std::fs::Permissions::from_mode(0o700))
        .map_err(|_| input("receipt_scratch_failed"))?;
    // The freshly created, 0700 directory is owned by this process's user.
    let uid = std::fs::metadata(&scratch)
        .map_err(|_| input("receipt_scratch_failed"))?
        .uid();
    let mut io = NativeTransport {
        scratch: scratch.clone(),
        uid,
        poll: Span::from_millis(POLL_MS),
        max_output_bytes: MAX_OUTPUT,
        diagnostics: vec![],
    };

    // Pins: the manifest digest comes from the seal (not from the manifest), the executable
    // from the build output, the icon from the committed asset.
    let manifest = read(&repo.join("evidence/foundation-exit/native-inputs.json"))?;
    let seal: Value =
        serde_json::from_slice(&read(&repo.join("evidence/foundation-exit/seal.json"))?)
            .map_err(|_| input("receipt_seal_invalid"))?;
    let sealed = seal["files"]
        .as_array()
        .or_else(|| seal.as_array())
        .and_then(|files| {
            files
                .iter()
                .find(|f| f["path"] == "evidence/foundation-exit/native-inputs.json")
        })
        .ok_or(input("receipt_seal_invalid"))?;
    let manifest_pin = Content {
        size_bytes: sealed["size_bytes"]
            .as_u64()
            .ok_or(input("receipt_seal_invalid"))?,
        sha256: sealed["sha256"]
            .as_str()
            .ok_or(input("receipt_seal_invalid"))?
            .into(),
    };
    let pins = read(&repo.join("crates/desktop/package-pins.json"))?;
    let statics = Statics {
        executable: content(&read(exe)?),
        info_plist: Content {
            size_bytes: std::fs::metadata(app.join(bundle_admission::INFO_PLIST))
                .map_err(|_| input("receipt_input_unreadable"))?
                .len(),
            sha256: plist_sha.clone(),
        },
        icon: content(&read(
            &repo.join("crates/desktop/icons/icon-windowed.icns"),
        )?),
    };
    let expected = Expected::from_documents(&manifest, &manifest_pin, &pins, &statics)?;

    // 1. Package admission of the built bundle, and RED controls on tampered clones.
    let admission = bundle_admission::admit(&app, &expected)?;
    write(
        &out,
        "admission.json",
        &serde_json::to_value(&admission).map_err(|_| input("receipt_encoding_failed"))?,
    )?;
    let mut controls = vec![];
    for (name, edit) in [
        ("flip_byte_in_libmlxc", "Contents/Frameworks/libmlxc.dylib"),
        ("flip_byte_in_metallib", "Contents/Frameworks/mlx.metallib"),
        ("flip_byte_in_executable", "Contents/MacOS/mycelium"),
        (
            "stale_old_layout_metallib",
            "Contents/Resources/metal/mlx.metallib",
        ),
        ("extra_file", "Contents/Resources/extra.txt"),
    ] {
        let parent = out.join(format!("tamper-{name}"));
        let copy = clone_tree(&mut io, &app, &parent)?;
        let target = copy.join(edit);
        if name.starts_with("flip") {
            let mut f = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .open(&target)
                .map_err(|_| input("receipt_tamper_failed"))?;
            let mut byte = [0u8; 1];
            std::io::Read::read_exact(&mut f, &mut byte)
                .map_err(|_| input("receipt_tamper_failed"))?;
            std::io::Seek::seek(&mut f, std::io::SeekFrom::Start(0))
                .map_err(|_| input("receipt_tamper_failed"))?;
            byte[0] ^= 1;
            std::io::Write::write_all(&mut f, &byte).map_err(|_| input("receipt_tamper_failed"))?;
        } else {
            std::fs::create_dir_all(target.parent().ok_or(input("receipt_tamper_failed"))?)
                .map_err(|_| input("receipt_tamper_failed"))?;
            let source = std::fs::read(app.join("Contents/Frameworks/mlx.metallib"))
                .map_err(|_| input("receipt_tamper_failed"))?;
            std::fs::write(
                &target,
                if name == "extra_file" {
                    b"x".to_vec()
                } else {
                    source
                },
            )
            .map_err(|_| input("receipt_tamper_failed"))?;
        }
        let refused = bundle_admission::admit(&copy, &expected);
        controls.push(json!({
            "control": name,
            "refused": refused.is_err(),
            "code": refused.err().map(|f| f.code),
        }));
        if run(&mut io, "/bin/rm", &["-rf", &parent.display().to_string()])?.rc != 0 {
            return Err(input("receipt_cleanup_failed"));
        }
    }
    write(&out, "admission-tamper-controls.json", &json!(controls))?;
    if controls.iter().any(|c| c["refused"] != true) {
        return Err(Fault::new("tamper_control_admitted", FaultKind::Admission));
    }

    // 2. Relocation from two distinct scratch locations (+ a negative control).
    let mut receipts = vec![];
    let locations = [
        out.join("location-a"),
        out.join("location b/with space/deeper"),
    ];
    for (index, location) in locations.iter().enumerate() {
        let copy = clone_tree(&mut io, &app, location)?
            .canonicalize()
            .map_err(|_| input("receipt_path_invalid"))?;
        let admitted = bundle_admission::admit(&copy, &expected)?;
        let home = out.join(format!("home-{index}"));
        std::fs::create_dir_all(&home).map_err(|_| input("receipt_scratch_failed"))?;
        std::fs::set_permissions(&home, std::fs::Permissions::from_mode(0o700))
            .map_err(|_| input("receipt_scratch_failed"))?;
        let receipt = relocation::run(
            &mut io,
            &copy,
            &home,
            &["--install-status"],
            0,
            &[
                Path::new("/Users/mycelium-dev/Projects/mycelium-native-macos14"),
                &app,
            ],
            Span::from_millis(COMMAND_MS),
        )?;
        let mut value =
            serde_json::to_value(&receipt).map_err(|_| input("receipt_encoding_failed"))?;
        value["admitted_files_at_location"] = json!(admitted.file_count);
        value["admitted_total_bytes"] = json!(admitted.total_bytes);
        value["home_after"] = json!(
            std::fs::read_dir(&home)
                .map_err(|_| input("receipt_scratch_failed"))?
                .count()
        );
        receipts.push((receipt, value));
    }
    relocation::distinct_locations(&receipts[0].0, &receipts[1].0)?;
    // Negative control: without libmlx.dylib in the copy the launch must fail (dyld), and the
    // judgement must refuse it - the passing runs are therefore loading from the bundle.
    let neg_parent = out.join("location-negative");
    let neg = clone_tree(&mut io, &app, &neg_parent)?
        .canonicalize()
        .map_err(|_| input("receipt_path_invalid"))?;
    std::fs::remove_file(neg.join("Contents/Frameworks/libmlx.dylib"))
        .map_err(|_| input("receipt_tamper_failed"))?;
    let neg_admission = bundle_admission::admit(&neg, &expected);
    let neg_home = out.join("home-negative");
    std::fs::create_dir_all(&neg_home).map_err(|_| input("receipt_scratch_failed"))?;
    std::fs::set_permissions(&neg_home, std::fs::Permissions::from_mode(0o700))
        .map_err(|_| input("receipt_scratch_failed"))?;
    let neg_launch = relocation::launch(&neg, &neg_home, &["--install-status"]);
    let neg_run = io.execute(&neg_launch, Span::from_millis(COMMAND_MS))?;
    let neg_judged = relocation::judge(
        &neg,
        &neg_run,
        0,
        &[relocation::FRAMEWORKS_RPATH.into()],
        &[],
    );
    let negative = json!({
        "control": "libmlx.dylib removed from the relocated copy",
        "admission_refused": neg_admission.as_ref().err().map(|f| f.code),
        "exit_code": neg_run.rc,
        "judgement_refused": neg_judged.as_ref().err().map(|f| f.code),
        "stderr_mentions_missing_library": neg_run.stderr.contains("Library not loaded") || neg_run.stderr.contains("libmlx.dylib"),
    });
    if neg_admission.is_ok() || neg_run.rc == 0 || neg_judged.is_ok() {
        return Err(Fault::new(
            "relocation_negative_control_passed",
            FaultKind::Admission,
        ));
    }
    write(
        &out,
        "relocation.json",
        &json!({
            "schema": "mycelium.l12.relocation-pair.v1",
            "launch_path": "mycelium --install-status (non-GUI installer path), HOME = scratch, DYLD_PRINT_LIBRARIES=1",
            "locations": receipts.iter().map(|r| r.1.clone()).collect::<Vec<_>>(),
            "negative_control": negative,
        }),
    )?;

    // 3. Minimum OS.
    let mut macho = vec![];
    for relative in NATIVE_MACHO {
        macho.push(minimum_os::macho_minos(
            &mut io,
            &app,
            relative,
            Span::from_millis(COMMAND_MS),
        )?);
    }
    let plist = String::from_utf8(read(&app.join(bundle_admission::INFO_PLIST))?)
        .map_err(|_| input("receipt_plist_invalid"))?;
    let l8_bytes = read(Path::new(l8))?;
    let l8_pin = Content {
        size_bytes: l8_bytes.len() as u64,
        sha256: l8_sha.clone(),
    };
    let natives = [
        "libmlxc.dylib",
        "libmlx.dylib",
        "libjaccl.dylib",
        "mlx.metallib",
    ];
    let digests: Vec<&str> = natives
        .iter()
        .filter_map(|n| {
            expected
                .files()
                .iter()
                .find(|(p, _)| p.ends_with(n))
                .map(|(_, f)| f.sha256.as_str())
        })
        .collect();
    let libraries = minimum_os::consume_l8(l8, &l8_bytes, &l8_pin, &digests)?;
    let l11_files = match rest {
        [log, list] => Some((log, read(Path::new(log))?, read(Path::new(list))?)),
        [] => None,
        _ => return Err(input("usage")),
    };
    let l11_list = l11_files
        .as_ref()
        .map(|f| String::from_utf8(f.2.clone()).map_err(|_| input("receipt_input_unreadable")))
        .transpose()?;
    let l11 = minimum_os::consume_l11(
        l11_files
            .as_ref()
            .zip(l11_list.as_deref())
            .map(|(f, list)| minimum_os::ExecutableProof {
                path: f.0,
                log: &f.1,
                copy_list: list,
            })
            .as_ref(),
        &admission.files,
    )?;
    let receipt = minimum_os::receipt(
        macho,
        &minimum_os::plist_minimum(&plist).ok_or(input("receipt_plist_invalid"))?,
        Some(libraries),
        l11,
    )?;
    write(
        &out,
        "minimum-os.json",
        &serde_json::to_value(&receipt).map_err(|_| input("receipt_encoding_failed"))?,
    )?;
    println!(
        "RECEIPTS_DONE admission={} relocation=2 minimum_os_complete={}",
        admission.file_count, receipt.minimum_os_receipt_complete
    );
    Ok(())
}
