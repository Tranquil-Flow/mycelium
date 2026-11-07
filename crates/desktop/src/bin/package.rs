// SPDX-License-Identifier: AGPL-3.0-only
//! Offline packaging utility: stages and describes the local unsigned bundle and runs the
//! per-user installer against EXPLICIT roots only (no default home). Not a release publisher.
use desktop::{
    bundle, docs, install,
    package::{self, Manifest},
};
use std::{path::Path, process::ExitCode};
const USAGE: &str = "usage: desktop-package <verify|stage|sign-plan> MANIFEST [SOURCE] [DESTINATION] | pins NATIVE_INPUTS | bundle EXECUTABLE NATIVE_INPUTS ICON OUT_DIR [NATIVE_INPUTS_SHA256] | audit-no-python APP | finalize BUNDLE_DIR VERSION ROLLBACK_FLOOR SOURCE_REVISION SIGNING_KEY_FILE | install HOME APPLICATIONS BUNDLE_DIR | rollback HOME APPLICATIONS | uninstall HOME APPLICATIONS [--delete-identity-and-state] | status HOME APPLICATIONS";
fn read_manifest(path: &str) -> Result<Manifest, String> {
    let bytes = std::fs::read(path).map_err(|e| format!("manifest_read:{:?}", e.kind()))?;
    serde_json::from_slice(&bytes).map_err(|_| "manifest_invalid".to_string())
}
fn bundle_command(args: &[String]) -> Result<(), String> {
    let (executable, native_inputs, icon, out_dir, pin) = match args {
        [a, b, c, d] => (a, b, c, d, None),
        [a, b, c, d, pin] => (a, b, c, d, Some(pin.as_str())),
        _ => return Err(USAGE.into()),
    };
    let built = bundle::build(&bundle::Spec {
        executable: Path::new(executable),
        native_inputs: Path::new(native_inputs),
        icon: Path::new(icon),
        out_dir: Path::new(out_dir),
        expected_native_inputs_sha256: pin,
    })
    .map_err(|e| format!("bundle:{e:?}"))?;
    let content = bundle::content_manifest(&built).map_err(|e| format!("content:{e:?}"))?;
    let listing = bundle::app_files_listing(&built.app).map_err(|e| format!("listing:{e:?}"))?;
    let out = Path::new(out_dir);
    std::fs::write(
        out.join("CONTENT-MANIFEST.json"),
        serde_json::to_vec_pretty(&content).map_err(|_| "content_encode")?,
    )
    .map_err(|e| format!("content_write:{:?}", e.kind()))?;
    std::fs::write(out.join("APP-FILES.sha256"), listing)
        .map_err(|e| format!("listing_write:{:?}", e.kind()))?;
    println!(
        "bundle_built_unsigned_not_installed {}",
        built.app.display()
    );
    Ok(())
}
fn run() -> Result<(), String> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.len() < 2 {
        return Err(USAGE.into());
    }
    match (args[0].as_str(), args.len()) {
        ("verify", 3) => {
            package::verify(Path::new(&args[2]), &read_manifest(&args[1])?)
                .map_err(|e| format!("verify:{e:?}"))?;
            println!("payload_identity_verified");
        }
        ("stage", 4) => {
            package::assemble(
                Path::new(&args[2]),
                Path::new(&args[3]),
                &read_manifest(&args[1])?,
            )
            .map_err(|e| format!("stage:{e:?}"))?;
            println!("staged_not_installed_or_signed");
        }
        ("sign-plan", 2) => {
            for relative in package::signing_plan(&read_manifest(&args[1])?)
                .map_err(|e| format!("sign_plan:{e:?}"))?
            {
                // Arguments, not a shell line. Explicitly ad-hoc, no timestamp,
                // no --deep signing and no notarization implied or performed.
                println!(
                    "{}",
                    serde_json::to_string(&[
                        "/usr/bin/codesign",
                        "--force",
                        "--sign",
                        "-",
                        "--timestamp=none",
                        relative.as_str()
                    ])
                    .map_err(|_| "plan_encode")?
                );
            }
        }
        ("pins", 2) => {
            let bytes =
                std::fs::read(&args[1]).map_err(|e| format!("native_read:{:?}", e.kind()))?;
            let inputs = bundle::parse_native_inputs(&bytes).map_err(|e| format!("pins:{e:?}"))?;
            let pins = bundle::native_pins(&inputs).map_err(|e| format!("pins:{e:?}"))?;
            println!(
                "{}",
                serde_json::to_string_pretty(&pins).map_err(|_| "pins_encode")?
            );
        }
        ("bundle", 5 | 6) => bundle_command(&args[1..])?,
        ("finalize", 6) => {
            // Signs with a key FILE (32 raw bytes, never printed): local fixtures use a throw-away
            // test key; the production signer is the owner's release tool, not this one.
            let signer = docs::Signer::from_key_file(Path::new(&args[5]), docs::SIGNER_ENDPOINT_ID)
                .map_err(|e| format!("finalize:{}", e.0))?;
            let issued = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map_or(0.0, |d| d.as_secs_f64());
            let digest = docs::finalize(
                Path::new(&args[1]),
                &signer,
                &docs::FinalizeSpec {
                    version: &args[2],
                    rollback_floor: &args[3],
                    source_revision: &args[4],
                    issued_at: issued.floor(),
                    minimum_os: bundle::MINIMUM_OS,
                },
            )
            .map_err(|e| format!("finalize:{}", e.0))?;
            println!("package_finalized_signed {digest}");
        }
        ("audit-no-python", 2) => {
            let findings =
                bundle::audit_no_python(Path::new(&args[1])).map_err(|e| format!("audit:{e:?}"))?;
            for (path, reason) in &findings {
                println!("{path}: {reason}");
            }
            if !findings.is_empty() {
                return Err(format!("python_indicators:{}", findings.len()));
            }
            println!("no_python_indicators");
        }
        _ => return Err("arguments_rejected".into()),
    }
    Ok(())
}
fn installer(args: &[String]) -> ExitCode {
    let refuse = |code: &str| {
        eprintln!(
            "{}",
            serde_json::json!({"protocol": install::PROTOCOL, "refused": code})
        );
        ExitCode::from(2)
    };
    let result = (|| {
        let (command, rest) = args.split_first().ok_or("usage")?;
        let (home, applications) = match rest {
            [home, applications, ..] => (Path::new(home), Path::new(applications)),
            _ => return Err("usage"),
        };
        let roots = install::Roots::new(home, applications).map_err(|e| e.0)?;
        let seams = install::Seams::host();
        match (command.as_str(), &rest[2..]) {
            ("install", [bundle]) => install::install(Path::new(bundle), &roots, &seams),
            ("rollback", []) => install::rollback(&roots, &seams),
            ("uninstall", []) => install::uninstall(&roots, &seams, false),
            ("uninstall", [flag]) if flag == "--delete-identity-and-state" => {
                install::uninstall(&roots, &seams, true)
            }
            ("status", []) => install::status(&roots),
            _ => return Err("usage"),
        }
        .map_err(|e| e.0)
    })();
    match result {
        Ok(report) => {
            println!(
                "{}",
                serde_json::to_string_pretty(&report).unwrap_or_default()
            );
            ExitCode::SUCCESS
        }
        Err("usage") => {
            eprintln!("{USAGE}");
            ExitCode::FAILURE
        }
        Err(code) => refuse(code),
    }
}
fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if matches!(
        args.first().map(String::as_str),
        Some("install" | "rollback" | "uninstall" | "status")
    ) {
        return installer(&args);
    }
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("{e}");
            ExitCode::FAILURE
        }
    }
}
