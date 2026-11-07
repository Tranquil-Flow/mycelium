// SPDX-License-Identifier: AGPL-3.0-only
//! Relocation receipt for an admitted bundle: the bundle is copied to a different path, its
//! non-GUI installer path is launched from there with `DYLD_PRINT_LIBRARIES=1`, and every
//! non-system image the process loaded must come from that copy's own `Contents/`.
//!
//! The launch goes through the injected [`Transport`] (the confined native transport in a
//! real run), so no process is spawned here. Judgement is pure and tested on recorded dyld
//! output; the real runs are in the receipt driver. It proves nothing about the GUI path,
//! signing, Gatekeeper, or other macOS versions.
use crate::effects::{Command, CommandKind, Output, Transport};
use crate::{Fault, FaultKind, Result};
use budgets::Span;
use serde::Serialize;
use std::path::Path;

pub const RECEIPT_SCHEMA: &str = "mycelium.l12.relocation.v1";
pub const FRAMEWORKS_RPATH: &str = "@executable_path/../Frameworks";
/// Libraries that must be loaded, and loaded from the copy (libjaccl is not loaded at
/// startup: libmlx no longer references it).
pub const REQUIRED_IMAGES: [&str; 2] = ["libmlxc.dylib", "libmlx.dylib"];
const SYSTEM_PREFIXES: [&str; 2] = ["/usr/lib/", "/System/"];

fn fault(code: &'static str) -> Fault {
    Fault::new(code, FaultKind::Admission)
}

/// `/usr/bin/env HOME=<home> DYLD_PRINT_LIBRARIES=1 <app>/Contents/MacOS/mycelium <args>`.
/// The environment is set by `env` so the target (not a system launcher) sees it.
pub fn launch(app: &Path, home: &Path, args: &[&str]) -> Command {
    let mut argv = vec![
        format!("HOME={}", home.display()),
        "DYLD_PRINT_LIBRARIES=1".to_owned(),
        app.join(crate::bundle_admission::EXECUTABLE)
            .display()
            .to_string(),
    ];
    argv.extend(args.iter().map(|a| (*a).to_owned()));
    Command {
        kind: CommandKind::Local,
        program: "/usr/bin/env".into(),
        args: argv,
    }
}

/// Image paths from `dyld[<pid>]: [<UUID> ]<path>` lines; paths may contain spaces.
pub fn parse_loaded_images(stderr: &str) -> Vec<String> {
    stderr
        .lines()
        .filter_map(|line| {
            let rest = line.strip_prefix("dyld[")?.split_once("]: ")?.1;
            let path = match rest.strip_prefix('<') {
                Some(after) => after.split_once("> ")?.1,
                None => rest,
            };
            path.starts_with('/').then(|| path.to_owned())
        })
        .collect()
}

/// `LC_RPATH` values of an `otool -l` dump.
pub fn parse_rpaths(otool_l: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut in_rpath = false;
    for line in otool_l.lines().map(str::trim) {
        if line.starts_with("cmd ") {
            in_rpath = line == "cmd LC_RPATH";
        } else if in_rpath && let Some(rest) = line.strip_prefix("path ") {
            out.push(
                rest.rsplit_once(" (offset ")
                    .map_or(rest, |x| x.0)
                    .to_owned(),
            );
            in_rpath = false;
        }
    }
    out
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct LoadedFromBundle {
    pub image: &'static str,
    pub path: String,
}

#[derive(Debug, Serialize)]
pub struct Receipt {
    pub schema: &'static str,
    pub verdict: &'static str,
    pub location: String,
    pub exit_code: i32,
    pub rpaths: Vec<String>,
    pub required_images: Vec<LoadedFromBundle>,
    pub images_total: usize,
    pub images_from_bundle: usize,
    pub images_from_system: usize,
    pub not_claimed: [&'static str; 4],
}

/// Judge one launch. `app` must be canonical (the real path dyld reports). `rpaths` are the
/// executable's `LC_RPATH` entries from `otool -l`.
pub fn judge(
    app: &Path,
    run: &Output,
    expected_rc: i32,
    rpaths: &[String],
    build_roots: &[&Path],
) -> Result<Receipt> {
    if run.rc != expected_rc {
        return Err(fault("relocation_exit_code"));
    }
    if rpaths != [FRAMEWORKS_RPATH] {
        return Err(fault("relocation_rpath_not_bundle_relative"));
    }
    let contents = format!("{}/Contents/", app.display());
    let images = parse_loaded_images(&run.stderr);
    if images.is_empty() {
        return Err(fault("relocation_no_dyld_output"));
    }
    let (mut bundle, mut system) = (0, 0);
    for image in &images {
        if image.starts_with(&contents) {
            bundle += 1;
        } else if SYSTEM_PREFIXES.iter().any(|p| image.starts_with(p)) {
            system += 1;
        } else {
            // A dev/build-tree library, the original location, or anything else.
            return Err(fault("relocation_image_outside_bundle"));
        }
        if build_roots.iter().any(|r| Path::new(image).starts_with(r)) {
            return Err(fault("relocation_image_outside_bundle"));
        }
    }
    let mut required = Vec::new();
    for leaf in REQUIRED_IMAGES {
        let wanted = format!("{contents}Frameworks/{leaf}");
        let hits = images.iter().filter(|i| **i == wanted).count();
        if hits != 1 {
            return Err(fault("relocation_required_image_not_loaded_from_bundle"));
        }
        required.push(LoadedFromBundle {
            image: leaf,
            path: wanted,
        });
    }
    Ok(Receipt {
        schema: RECEIPT_SCHEMA,
        verdict: "loaded_from_relocated_bundle",
        location: app.display().to_string(),
        exit_code: run.rc,
        rpaths: rpaths.to_vec(),
        required_images: required,
        images_total: images.len(),
        images_from_bundle: bundle,
        images_from_system: system,
        not_claimed: [
            "gui_launch",
            "signature_or_gatekeeper",
            "other_macos_versions",
            "metal_compute_after_relocation",
        ],
    })
}

/// `otool -l` the executable through the transport and return its rpaths.
pub fn executable_rpaths<T: Transport>(io: &mut T, app: &Path, bound: Span) -> Result<Vec<String>> {
    let out = io.execute(
        &Command {
            kind: CommandKind::Local,
            program: "/usr/bin/otool".into(),
            args: vec![
                "-l".into(),
                app.join(crate::bundle_admission::EXECUTABLE)
                    .display()
                    .to_string(),
            ],
        },
        bound,
    )?;
    if out.rc != 0 {
        return Err(fault("relocation_otool_failed"));
    }
    Ok(parse_rpaths(&out.stdout))
}

/// Launch the non-GUI path from `app` and judge it.
pub fn run<T: Transport>(
    io: &mut T,
    app: &Path,
    home: &Path,
    args: &[&str],
    expected_rc: i32,
    build_roots: &[&Path],
    bound: Span,
) -> Result<Receipt> {
    let rpaths = executable_rpaths(io, app, bound)?;
    let output = io.execute(&launch(app, home, args), bound)?;
    judge(app, &output, expected_rc, &rpaths, build_roots)
}

/// Two receipts only count as a relocation proof when they are from two different, non-nested
/// locations.
pub fn distinct_locations(a: &Receipt, b: &Receipt) -> Result<()> {
    let (pa, pb) = (Path::new(&a.location), Path::new(&b.location));
    if pa == pb || pa.starts_with(pb) || pb.starts_with(pa) {
        return Err(fault("relocation_locations_not_distinct"));
    }
    Ok(())
}
