// SPDX-License-Identifier: AGPL-3.0-only
//! Relocation judgement on recorded dyld output (format observed from a real
//! `DYLD_PRINT_LIBRARIES=1 mycelium --install-status` run of a scratch bundle) plus the launch
//! composition through an injected transport. No process is spawned here.
use budgets::Span;
use device_harness::{
    Result,
    effects::{Command, Output, Transport},
    relocation::{self, FRAMEWORKS_RPATH},
};
use std::path::Path;

const APP: &str = "/scratch/dir a/Mycelium A13 Demo.app";
fn dyld(lines: &[&str]) -> String {
    lines
        .iter()
        .enumerate()
        .map(|(i, p)| format!("dyld[4242]: <E202CB42-72C7-3559-8B13-016BDBD4DF{i:02X}> {p}\n"))
        .collect()
}
fn good() -> Vec<String> {
    vec![
        format!("{APP}/Contents/MacOS/mycelium"),
        format!("{APP}/Contents/Frameworks/libmlxc.dylib"),
        format!("{APP}/Contents/Frameworks/libmlx.dylib"),
        "/usr/lib/libSystem.B.dylib".into(),
        "/System/Library/Frameworks/Metal.framework/Versions/A/Metal".into(),
    ]
}
fn output(images: &[String], rc: i32) -> Output {
    let refs: Vec<&str> = images.iter().map(String::as_str).collect();
    Output {
        rc,
        stdout: String::new(),
        stderr: format!(
            "dyld[4242]: move loaded to delayed: XPCSupport\n{}dyld[4242]: not a path\n",
            dyld(&refs)
        ),
    }
}
fn rp() -> Vec<String> {
    vec![FRAMEWORKS_RPATH.into()]
}
fn judge(images: &[String], rc: i32, rpaths: &[String]) -> Result<relocation::Receipt> {
    relocation::judge(Path::new(APP), &output(images, rc), 0, rpaths, &[])
}

#[test]
fn dyld_lines_keep_paths_with_spaces_and_skip_everything_that_is_not_an_image() {
    let parsed = relocation::parse_loaded_images(&output(&good(), 0).stderr);
    assert_eq!(parsed, good());
    assert!(relocation::parse_loaded_images("noise\ndyld[1]: <x> relative/path\n").is_empty());
}

#[test]
fn a_launch_from_the_copy_with_bundle_only_native_images_is_accepted() {
    let r = judge(&good(), 0, &rp()).unwrap();
    assert_eq!(r.verdict, "loaded_from_relocated_bundle");
    assert_eq!(
        (r.images_total, r.images_from_bundle, r.images_from_system),
        (5, 3, 2)
    );
    assert_eq!(r.required_images.len(), 2);
    assert!(r.not_claimed.contains(&"gui_launch"));
}

#[test]
fn every_way_of_loading_from_elsewhere_or_not_at_all_is_refused() {
    let code = |r: Result<relocation::Receipt>| r.unwrap_err().code;
    let with = |edit: &dyn Fn(&mut Vec<String>)| {
        let mut v = good();
        edit(&mut v);
        v
    };
    // Library from a dev/build tree instead of the copy.
    let dev = with(&|v| v[2] = "/dev/install/lib/libmlx.dylib".into());
    assert_eq!(
        code(judge(&dev, 0, &rp())),
        "relocation_image_outside_bundle"
    );
    // Library from the ORIGINAL location (the bundle was copied, not moved).
    let original = with(&|v| {
        v[1] = "/original/Mycelium A13 Demo.app/Contents/Frameworks/libmlxc.dylib".into()
    });
    assert_eq!(
        code(judge(&original, 0, &rp())),
        "relocation_image_outside_bundle"
    );
    // Extra non-system image from the copy's neighbour directory.
    let extra = with(&|v| v.push("/scratch/dir a/libmlx.dylib".into()));
    assert_eq!(
        code(judge(&extra, 0, &rp())),
        "relocation_image_outside_bundle"
    );
    // Required image never loaded / loaded twice.
    let missing = with(&|v| {
        v.remove(2);
    });
    assert_eq!(
        code(judge(&missing, 0, &rp())),
        "relocation_required_image_not_loaded_from_bundle"
    );
    let twice = with(&|v| v.push(format!("{APP}/Contents/Frameworks/libmlx.dylib")));
    assert_eq!(
        code(judge(&twice, 0, &rp())),
        "relocation_required_image_not_loaded_from_bundle"
    );
    // Prefix lookalike: a sibling bundle whose name starts with the same text.
    let lookalike = with(&|v| v[1] = format!("{APP}2/Contents/Frameworks/libmlxc.dylib"));
    assert_eq!(
        code(judge(&lookalike, 0, &rp())),
        "relocation_image_outside_bundle"
    );
    // Exit code, rpaths, silence.
    assert_eq!(code(judge(&good(), 134, &rp())), "relocation_exit_code");
    assert_eq!(
        code(judge(&good(), 0, &["/abs/lib".into()])),
        "relocation_rpath_not_bundle_relative"
    );
    assert_eq!(
        code(judge(&good(), 0, &[])),
        "relocation_rpath_not_bundle_relative"
    );
    assert_eq!(
        code(judge(
            &good(),
            0,
            &[FRAMEWORKS_RPATH.into(), "/abs/lib".into()]
        )),
        "relocation_rpath_not_bundle_relative"
    );
    let silent = Output {
        rc: 0,
        stdout: String::new(),
        stderr: "no dyld lines".into(),
    };
    assert_eq!(
        code(relocation::judge(Path::new(APP), &silent, 0, &rp(), &[])),
        "relocation_no_dyld_output"
    );
}

#[test]
fn configured_build_roots_are_refused_even_inside_the_bundle_prefix() {
    let r = relocation::judge(
        Path::new(APP),
        &output(&good(), 0),
        0,
        &rp(),
        &[Path::new(APP)],
    );
    assert_eq!(r.unwrap_err().code, "relocation_image_outside_bundle");
}

#[test]
fn rpaths_are_read_from_lc_rpath_commands_only() {
    let dump = "Load command 9\n          cmd LC_RPATH\n      cmdsize 48\n         path @executable_path/../Frameworks (offset 12)\nLoad command 10\n          cmd LC_LOAD_DYLIB\n         name @rpath/libmlxc.dylib (offset 24)\n      path decoy (offset 1)\nLoad command 11\n          cmd LC_RPATH\n      cmdsize 32\n         path /abs (offset 12)\n";
    assert_eq!(
        relocation::parse_rpaths(dump),
        vec![FRAMEWORKS_RPATH.to_owned(), "/abs".to_owned()]
    );
}

struct Fake {
    seen: Vec<(String, Vec<String>)>,
    otool: String,
    run: Output,
}
impl Transport for Fake {
    fn execute(&mut self, command: &Command, _: Span) -> Result<Output> {
        self.seen
            .push((command.program.clone(), command.args.clone()));
        if command.program == "/usr/bin/otool" {
            return Ok(Output {
                rc: 0,
                stdout: self.otool.clone(),
                stderr: String::new(),
            });
        }
        Ok(Output {
            rc: self.run.rc,
            stdout: String::new(),
            stderr: self.run.stderr.clone(),
        })
    }
}

#[test]
fn run_inspects_the_rpath_first_then_launches_through_env_with_home_and_dyld_print() {
    let mut io = Fake {
        seen: vec![],
        otool: "cmd LC_RPATH\n path @executable_path/../Frameworks (offset 12)\n".into(),
        run: output(&good(), 0),
    };
    let r = relocation::run(
        &mut io,
        Path::new(APP),
        Path::new("/scratch/home"),
        &["--install-status"],
        0,
        &[],
        Span::from_millis(1),
    )
    .unwrap();
    assert_eq!(r.location, APP);
    assert_eq!(io.seen[0].0, "/usr/bin/otool");
    assert_eq!(io.seen[1].0, "/usr/bin/env");
    assert_eq!(
        io.seen[1].1,
        vec![
            "HOME=/scratch/home".to_owned(),
            "DYLD_PRINT_LIBRARIES=1".into(),
            format!("{APP}/Contents/MacOS/mycelium"),
            "--install-status".into(),
        ]
    );
}

#[test]
fn two_receipts_prove_relocation_only_from_distinct_non_nested_locations() {
    let a = judge(&good(), 0, &rp()).unwrap();
    let b = judge(&good(), 0, &rp()).unwrap();
    assert_eq!(
        relocation::distinct_locations(&a, &b).unwrap_err().code,
        "relocation_locations_not_distinct"
    );
    let other = "/scratch/other/Mycelium A13 Demo.app";
    let images: Vec<String> = good().into_iter().map(|p| p.replace(APP, other)).collect();
    let c = relocation::judge(Path::new(other), &output(&images, 0), 0, &rp(), &[]).unwrap();
    relocation::distinct_locations(&a, &c).unwrap();
    let nested = "/scratch/dir a/Mycelium A13 Demo.app/inner/Mycelium A13 Demo.app";
    let nested_images: Vec<String> = good().into_iter().map(|p| p.replace(APP, nested)).collect();
    let n =
        relocation::judge(Path::new(nested), &output(&nested_images, 0), 0, &rp(), &[]).unwrap();
    assert!(relocation::distinct_locations(&a, &n).is_err());
}

struct Failing<'a>(&'a mut Fake, i32);
impl Transport for Failing<'_> {
    fn execute(&mut self, c: &Command, b: Span) -> Result<Output> {
        let mut o = self.0.execute(c, b)?;
        if c.program == "/usr/bin/otool" {
            o.rc = self.1;
        }
        Ok(o)
    }
}

#[test]
fn an_otool_that_fails_or_prints_nothing_usable_never_yields_a_passing_receipt() {
    let attempt = |rc: i32, otool: &str| {
        let mut io = Fake {
            seen: vec![],
            otool: otool.into(),
            run: output(&good(), 0),
        };
        relocation::run(
            &mut Failing(&mut io, rc),
            Path::new(APP),
            Path::new("/h"),
            &["--install-status"],
            0,
            &[],
            Span::from_millis(1),
        )
        .map(|_| ())
        .map_err(|f| f.code)
    };
    let valid = "cmd LC_RPATH\n path @executable_path/../Frameworks (offset 12)\n";
    assert_eq!(attempt(0, valid), Ok(()));
    // Non-zero exit with perfectly plausible stdout must still fail.
    assert_eq!(attempt(1, valid), Err("relocation_otool_failed"));
    assert_eq!(attempt(-1, valid), Err("relocation_otool_failed"));
    // Exit 0 but nothing parseable -> no rpath -> refused.
    assert_eq!(attempt(0, ""), Err("relocation_rpath_not_bundle_relative"));
    assert_eq!(
        attempt(0, "garbage"),
        Err("relocation_rpath_not_bundle_relative")
    );
}
