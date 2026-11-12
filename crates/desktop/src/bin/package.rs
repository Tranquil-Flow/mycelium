// SPDX-License-Identifier: AGPL-3.0-only
//! Offline packaging utility: stages and describes the local unsigned bundle and runs the
//! per-user installer against EXPLICIT roots only (no default home). Not a release publisher.
use desktop::{
    bundle, docs, install,
    package::{self, Manifest},
};
use std::{path::Path, process::ExitCode};
const USAGE: &str = "usage: desktop-package <verify|stage|sign-plan> MANIFEST [SOURCE] [DESTINATION] | pins NATIVE_INPUTS | bundle EXECUTABLE NATIVE_INPUTS ICON OUT_DIR [NATIVE_INPUTS_SHA256] [--resource-probe FILE] [--relay-client FILE --relay-license FILE] [--iroh-sidecar FILE] [--sign-adhoc] | admit APP | audit-no-python APP | finalize BUNDLE_DIR VERSION ROLLBACK_FLOOR SOURCE_REVISION SIGNING_KEY_FILE | install HOME APPLICATIONS BUNDLE_DIR | rollback HOME APPLICATIONS | uninstall HOME APPLICATIONS [--delete-identity-and-state] | status HOME APPLICATIONS";
fn read_manifest(path: &str) -> Result<Manifest, String> {
    let bytes = std::fs::read(path).map_err(|e| format!("manifest_read:{:?}", e.kind()))?;
    serde_json::from_slice(&bytes).map_err(|_| "manifest_invalid".to_string())
}
/// `bundle EXECUTABLE NATIVE_INPUTS ICON OUT_DIR [NATIVE_INPUTS_SHA256] [--resource-probe FILE]
/// [--sign-adhoc]`: the probe is admitted by the compile-time helper pin; `--sign-adhoc` seals the
/// assembled bundle at the v0.1.4 trust level and records `SIGN-RECEIPT.json` beside it, after
/// which the content manifest and `APP-FILES.sha256` describe the SIGNED bytes.
fn bundle_command(args: &[String]) -> Result<(), String> {
    let mut positional = Vec::new();
    let mut probe = None;
    let (mut relay, mut relay_license) = (None, None);
    let mut sidecar = None;
    let mut sign = false;
    let mut rest = args.iter();
    while let Some(arg) = rest.next() {
        match arg.as_str() {
            "--resource-probe" if probe.is_none() => probe = Some(rest.next().ok_or(USAGE)?),
            "--relay-client" if relay.is_none() => relay = Some(rest.next().ok_or(USAGE)?),
            "--relay-license" if relay_license.is_none() => {
                relay_license = Some(rest.next().ok_or(USAGE)?);
            }
            "--iroh-sidecar" if sidecar.is_none() => sidecar = Some(rest.next().ok_or(USAGE)?),
            "--sign-adhoc" if !sign => sign = true,
            flag if flag.starts_with("--") => {
                return Err(format!("{USAGE}\nunknown or repeated {flag}"));
            }
            _ => positional.push(arg),
        }
    }
    let (executable, native_inputs, icon, out_dir, pin) = match positional.as_slice() {
        [a, b, c, d] => (*a, *b, *c, *d, None),
        [a, b, c, d, pin] => (*a, *b, *c, *d, Some(pin.as_str())),
        _ => return Err(USAGE.into()),
    };
    let helper_pins = package::helper_pins().map_err(|e| format!("helper_pins:{e:?}"))?;
    let mut helpers: Vec<(&Path, &package::Entry)> = match probe {
        Some(probe) => helper_pins
            .iter()
            .filter(|p| p.path == package::RESOURCE_PROBE_PATH)
            .map(|p| (Path::new(probe.as_str()), p))
            .collect(),
        None => Vec::new(),
    };
    if probe.is_some() && helpers.len() != 1 {
        return Err("helper_pins:resource_probe_pin_missing".into());
    }
    // The relay client and its licence travel together (v0.1.4 refuses to build without both:
    // `cloudflared_build_dependency_and_license_required`, build_a13_macos.py:287-288).
    let license_pin = package::relay_license_pin();
    match (relay, relay_license) {
        (Some(client), Some(license)) => {
            let pin = helper_pins
                .iter()
                .find(|p| p.path == package::RELAY_CLIENT_PATH)
                .ok_or("helper_pins:relay_client_pin_missing")?;
            helpers.push((Path::new(client.as_str()), pin));
            helpers.push((Path::new(license.as_str()), &license_pin));
        }
        (None, None) => {}
        _ => return Err("cloudflared_build_dependency_and_license_required".into()),
    }
    // The native iroh transport travels with the app: without it every packaged run stops at
    // "joined Macs report in" (`native_sidecar_installation_invalid`), and admission refuses an
    // unpinned copy dropped in after install.
    if let Some(source) = sidecar {
        let pin = helper_pins
            .iter()
            .find(|p| p.path == package::IROH_SIDECAR_PATH)
            .ok_or("helper_pins:iroh_sidecar_pin_missing")?;
        helpers.push((Path::new(source), pin));
    }
    let mut built = bundle::build_with_helpers(
        &bundle::Spec {
            executable: Path::new(executable),
            native_inputs: Path::new(native_inputs),
            icon: Path::new(icon),
            out_dir: Path::new(out_dir),
            expected_native_inputs_sha256: pin,
        },
        &helpers,
    )
    .map_err(|e| format!("bundle:{e:?}"))?;
    let out = Path::new(out_dir);
    if sign {
        let receipt = match desktop::sign::adhoc(&built.app, &built.manifest.entries) {
            Ok(receipt) => receipt,
            Err(error) => {
                // An unsealed or half-signed bundle is not left behind as if it were usable.
                std::fs::remove_dir_all(&built.app)
                    .map_err(|e| format!("sign:{} (removal failed: {:?})", error.0, e.kind()))?;
                return Err(format!("sign:{}", error.0));
            }
        };
        let measured = |relative: &str| {
            bundle::measure(&built.app.join(relative)).map_err(|e| format!("sign_measure:{e:?}"))
        };
        for entry in &mut built.manifest.entries {
            if entry.kind == package::Kind::Executable {
                (entry.bytes, entry.sha256) = measured(&entry.path)?;
            }
        }
        let (bytes, sha256) = measured(package::CODE_RESOURCES_PATH)?;
        built.manifest.entries.push(package::Entry {
            path: package::CODE_RESOURCES_PATH.into(),
            kind: package::Kind::Resource,
            bytes,
            sha256,
        });
        built.manifest.entries.sort_by(|a, b| a.path.cmp(&b.path));
        package::verify(&built.app, &built.manifest).map_err(|e| format!("sign_verify:{e:?}"))?;
        std::fs::write(
            out.join("SIGN-RECEIPT.json"),
            serde_json::to_vec_pretty(&receipt).map_err(|_| "receipt_encode")?,
        )
        .map_err(|e| format!("receipt_write:{:?}", e.kind()))?;
        built.signature = Some(receipt);
    }
    let content = bundle::content_manifest(&built).map_err(|e| format!("content:{e:?}"))?;
    let listing = bundle::app_files_listing(&built.app).map_err(|e| format!("listing:{e:?}"))?;
    std::fs::write(
        out.join("CONTENT-MANIFEST.json"),
        serde_json::to_vec_pretty(&content).map_err(|_| "content_encode")?,
    )
    .map_err(|e| format!("content_write:{:?}", e.kind()))?;
    std::fs::write(out.join("APP-FILES.sha256"), listing)
        .map_err(|e| format!("listing_write:{:?}", e.kind()))?;
    println!(
        "{} {}",
        if sign {
            "bundle_built_adhoc_signed_not_installed"
        } else {
            "bundle_built_unsigned_not_installed"
        },
        built.app.display()
    );
    Ok(())
}
/// `admit APP`: the packaged executable's own startup admission, run against `APP` from outside:
/// the compile-time native pins and helper pins plus the bundle's measured executable.
fn admit_command(app: &str) -> Result<(), String> {
    let mut pins: Vec<package::Entry> =
        serde_json::from_str(include_str!("../../package-pins.json"))
            .map_err(|_| "pins_invalid")?;
    pins.extend(package::helper_pins().map_err(|e| format!("helper_pins:{e:?}"))?);
    let app = Path::new(app);
    let manifest = package::with_measured_executable(pins, &app.join("Contents/MacOS/mycelium"))
        .map_err(|e| format!("admit:{e:?}"))?;
    package::verify_closed(app, &manifest).map_err(|e| format!("admit:{e:?}"))?;
    println!(
        "{}",
        serde_json::json!({"admitted": true, "entries": manifest.entries.len(),
            "paths": manifest.entries.iter().map(|e| e.path.clone()).collect::<Vec<_>>()})
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
        ("bundle", _) => bundle_command(&args[1..])?,
        ("admit", 2) => admit_command(&args[1])?,
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
