// SPDX-License-Identifier: AGPL-3.0-only
//! Minimum-OS receipt logic: static floor from Mach-O load commands and the plist, and the two
//! cited cross-host proofs bound to exact bytes. Fixture texts only; the real L8/L11 documents
//! are consumed by the receipt driver.
use device_harness::{
    bundle_admission::AdmittedFile,
    evidence_inputs::Content,
    minimum_os::{self, Citation, ExecutableProof, MachO},
};
use sha2::{Digest, Sha256};

fn pin(bytes: &[u8]) -> Content {
    Content {
        size_bytes: bytes.len() as u64,
        sha256: format!("{:x}", Sha256::digest(bytes)),
    }
}
const LIBS: [&str; 4] = [
    "8117d43fcd12f4af9a76b948e792803b151f8f46c28a2042a5bdff3fd0e4247d",
    "e70c48ea5da63b86d4e11408f83cc5396d6228fd84ae4857d383d7480654dac3",
    "645a45cd177916b5c71890dd7ebb270ec1a5254eed72821ede1540dd36a4d2a4",
    "d70ab213d4cfa9f2673e4db2c2a4474c86ad9cc7674564bec4dd4ac9409da675",
];
fn l8_text() -> String {
    format!(
        "**PASS (scoped).** macOS 15.6.1 (24G90) arm64 host; outputs bit-identical. {}",
        LIBS.iter()
            .map(|s| format!("{}...", &s[..8]))
            .collect::<Vec<_>>()
            .join(" ")
    )
}
fn macho(items: &[(&str, &str)]) -> Vec<MachO> {
    items
        .iter()
        .map(|(p, m)| MachO {
            path: (*p).into(),
            minos: (*m).into(),
        })
        .collect()
}
const EXE_SHA: &str = "bf2d81ff0ec5d6e94e9b627f75a6b99071745ebd8e8e63ee2c44cdd31978e2b4";

#[test]
fn minos_and_plist_minimum_are_read_from_the_right_commands() {
    let dump = "cmd LC_SEGMENT_64\n minos 99.0\ncmd LC_BUILD_VERSION\n cmdsize 32\n platform 1\n    minos 14.0\n      sdk 26.5\ncmd LC_SOURCE_VERSION\n minos 1.0\n";
    assert_eq!(minimum_os::parse_minos(dump).as_deref(), Some("14.0"));
    assert_eq!(
        minimum_os::parse_minos("cmd LC_SEGMENT_64\n minos 9\n"),
        None
    );
    let plist = "\t<key>LSMinimumSystemVersion</key>\n\t<string>15.6.1</string>\n";
    assert_eq!(minimum_os::plist_minimum(plist).as_deref(), Some("15.6.1"));
    assert_eq!(
        minimum_os::plist_minimum("<key>Other</key>\n<string>1</string>"),
        None
    );
}

#[test]
fn the_static_floor_is_the_highest_minos_and_must_not_exceed_the_declared_minimum() {
    let ok = macho(&[("a", "14.0"), ("exe", "15.6"), ("b", "14.0")]);
    let r = minimum_os::receipt(ok, "15.6.1", None, None).unwrap();
    assert_eq!(r.static_floor, "15.6");
    assert_eq!(r.packaged_executable_on_minimum_os, "NOT_RUN");
    assert!(!r.minimum_os_receipt_complete && !r.release_qualified);
    let code = |m, plist: &str| minimum_os::receipt(m, plist, None, None).unwrap_err().code;
    assert_eq!(
        code(macho(&[("exe", "26.0")]), "15.6.1"),
        "minos_floor_above_declared_minimum"
    );
    assert_eq!(
        code(macho(&[("exe", "15.7")]), "15.6.1"),
        "minos_floor_above_declared_minimum"
    );
    assert_eq!(
        code(macho(&[("exe", "15.6")]), "15.6"),
        "minos_declared_minimum_mismatch"
    );
    assert_eq!(
        code(macho(&[("exe", "x.y")]), "15.6.1"),
        "minos_macho_unparsed"
    );
    assert_eq!(code(vec![], "15.6.1"), "minos_macho_unparsed");
    // 15.6 < 15.6.1 and 15.6.1 itself both pass; 15.6.2 does not.
    assert!(minimum_os::receipt(macho(&[("e", "15.6.1")]), "15.6.1", None, None).is_ok());
    assert!(minimum_os::receipt(macho(&[("e", "15.6.2")]), "15.6.1", None, None).is_err());
}

#[test]
fn l8_proof_is_bound_to_exact_bytes_host_and_all_four_library_digests() {
    let text = l8_text();
    let c = minimum_os::consume_l8(
        "L8/MINOS-LOAD-PROOF.md",
        text.as_bytes(),
        &pin(text.as_bytes()),
        &LIBS,
    )
    .unwrap();
    assert_eq!(c.status, "cited_pass_scoped");
    assert_eq!(c.sha256, pin(text.as_bytes()).sha256);
    let code = |bytes: &[u8], p: &Content, libs: &[&str]| {
        minimum_os::consume_l8("x", bytes, p, libs)
            .unwrap_err()
            .code
    };
    // Swapped bytes under the old pin.
    assert_eq!(
        code(b"other", &pin(text.as_bytes()), &LIBS),
        "evidence_content_mismatch"
    );
    for broken in [
        text.replace("**PASS (scoped).**", "FAIL"),
        text.replace("(24G90)", "(25F80)"),
        text.replace("15.6.1", "26.5.1"),
        text.replace("bit-identical", "different"),
        text.replace(&LIBS[3][..8], "00000000"),
    ] {
        assert_eq!(
            code(broken.as_bytes(), &pin(broken.as_bytes()), &LIBS),
            "minos_l8_proof_unbound",
            "{broken}"
        );
    }
    assert_eq!(
        code(text.as_bytes(), &pin(text.as_bytes()), &LIBS[..3]),
        "minos_l8_proof_unbound"
    );
}

fn admitted() -> Vec<AdmittedFile> {
    [
        ("Contents/Frameworks/libmlx.dylib", "e".repeat(64)),
        ("Contents/MacOS/mycelium", EXE_SHA.to_owned()),
    ]
    .into_iter()
    .map(|(p, s)| AdmittedFile {
        path: p.into(),
        bytes: 1,
        sha256: s,
    })
    .collect()
}
fn copy_list(files: &[AdmittedFile]) -> String {
    files
        .iter()
        .map(|f| format!("{}  ./{}\n", f.sha256, f.path))
        .collect()
}
fn log() -> String {
    format!(
        "== host: macOS 15.6.1 24G90 arm64\n== exe: {EXE_SHA}\n minos 15.6\n== run B-install-status: x\nexit=0\n== run D-control-libmlxc-missing: x\nexit=134\n"
    )
}

#[test]
fn the_packaged_executable_proof_is_not_run_when_absent_and_bound_to_this_bundle_when_present() {
    let files = admitted();
    assert_eq!(minimum_os::consume_l11(None, &files).unwrap(), None);
    let (text, list) = (log(), copy_list(&files));
    let proof = |log: &str, list: &str| {
        minimum_os::consume_l11(
            Some(&ExecutableProof {
                path: "L11/proof.log",
                log: log.as_bytes(),
                copy_list: list,
            }),
            &files,
        )
    };
    let c = proof(&text, &list).unwrap().unwrap();
    assert_eq!(c.status, "cited_present");
    let code = |r: device_harness::Result<Option<Citation>>| r.unwrap_err().code;
    // Log of another executable, another OS/build, failed runs or a missing control.
    for bad in [
        text.replace(EXE_SHA, &"0".repeat(64)),
        text.replace("15.6.1 24G90", "26.5.1 25F80"),
        text.replace("exit=0", "exit=1"),
        text.replace("exit=134", "exit=0"),
        text.replace("run D-control-libmlxc-missing", "run D-other"),
        text.replace("minos 15.6", "minos 26.0"),
    ] {
        assert_eq!(
            code(proof(&bad, &list)),
            "minos_executable_proof_unbound",
            "{bad}"
        );
    }
    // The copy that ran differs from the admitted bundle: other digest, extra, missing file.
    let mut changed = files.clone();
    changed[0].sha256 = "f".repeat(64);
    assert_eq!(
        code(proof(&text, &copy_list(&changed))),
        "minos_executable_proof_other_bundle"
    );
    let mut extra = files.clone();
    extra.push(AdmittedFile {
        path: "Contents/x".into(),
        bytes: 1,
        sha256: "1".repeat(64),
    });
    assert_eq!(
        code(proof(&text, &copy_list(&extra))),
        "minos_executable_proof_other_bundle"
    );
    assert_eq!(
        code(proof(&text, &copy_list(&files[..1]))),
        "minos_executable_proof_other_bundle"
    );
    assert_eq!(
        code(proof(&text, "not a listing")),
        "minos_executable_proof_invalid"
    );
    assert_eq!(
        code(proof(&text, &format!("{list}{list}"))),
        "minos_executable_proof_invalid"
    );
}

#[test]
fn the_receipt_is_complete_only_with_both_cross_host_proofs_and_never_release_qualified() {
    let cite = |status| Citation {
        path: "p".into(),
        bytes: 1,
        sha256: "a".repeat(64),
        status,
    };
    let m = || macho(&[("exe", "15.6"), ("lib", "14.0")]);
    let lib_only =
        minimum_os::receipt(m(), "15.6.1", Some(cite("cited_pass_scoped")), None).unwrap();
    assert!(!lib_only.minimum_os_receipt_complete);
    let exe_only = minimum_os::receipt(m(), "15.6.1", None, Some(cite("cited_present"))).unwrap();
    assert!(!exe_only.minimum_os_receipt_complete);
    let both = minimum_os::receipt(
        m(),
        "15.6.1",
        Some(cite("cited_pass_scoped")),
        Some(cite("cited_present")),
    )
    .unwrap();
    assert!(both.minimum_os_receipt_complete && !both.release_qualified);
    assert_eq!(both.packaged_executable_on_minimum_os, "cited");
    assert!(both.not_claimed.contains(&"macos_14_x"));
}

struct Otool(i32, &'static str);
impl device_harness::effects::Transport for Otool {
    fn execute(
        &mut self,
        c: &device_harness::effects::Command,
        _: budgets::Span,
    ) -> device_harness::Result<device_harness::effects::Output> {
        assert_eq!(c.program, "/usr/bin/otool");
        assert_eq!(c.args[0], "-l");
        assert!(c.args[1].ends_with("/Contents/MacOS/mycelium"));
        Ok(device_harness::effects::Output {
            rc: self.0,
            stdout: self.1.into(),
            stderr: String::new(),
        })
    }
}

#[test]
fn a_failing_or_unparseable_otool_never_produces_a_minos() {
    let run = |mut io: Otool| {
        minimum_os::macho_minos(
            &mut io,
            std::path::Path::new("/app"),
            "Contents/MacOS/mycelium",
            budgets::Span::from_millis(1),
        )
        .map(|m| m.minos)
        .map_err(|f| f.code)
    };
    let dump = "cmd LC_BUILD_VERSION\n minos 15.6\n";
    assert_eq!(run(Otool(0, dump)), Ok("15.6".to_owned()));
    assert_eq!(run(Otool(1, dump)), Err("minos_otool_failed"));
    assert_eq!(run(Otool(-1, dump)), Err("minos_otool_failed"));
    assert_eq!(run(Otool(0, "")), Err("minos_macho_unparsed"));
    assert_eq!(
        run(Otool(0, "cmd LC_SEGMENT_64\n minos 14.0\n")),
        Err("minos_macho_unparsed")
    );
}
