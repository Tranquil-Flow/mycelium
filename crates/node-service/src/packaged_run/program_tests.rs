// SPDX-License-Identifier: AGPL-3.0-only
//! Constructor-only, model-free compiler admission witnesses. No native worker is executed.
use super::*;
use api::{compiled_handoff::Pin, native_compiler::NativeProgram};
use owner_run::{owner_chain::ChainPorts, steps::compile::BuildPoll};
use std::os::unix::fs::PermissionsExt;

struct Scratch(PathBuf);
impl Scratch {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "s4cb1-program-{}",
            crate::service::random_id().unwrap()
        ));
        std::fs::create_dir(&root).unwrap();
        Self(root)
    }
    fn program(&self) -> NativeProgram {
        let binary = self.0.join("mycelium");
        let bytes = b"model-free executable identity fixture; never executed\n";
        std::fs::write(&binary, bytes).unwrap();
        std::fs::set_permissions(&binary, std::fs::Permissions::from_mode(0o700)).unwrap();
        NativeProgram {
            binary,
            expected: Pin {
                bytes: bytes.len() as u64,
                sha256: protocol::canonical::sha256_prefixed(bytes)[7..].to_owned(),
            },
        }
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        if let Err(error) = std::fs::remove_dir_all(&self.0) {
            eprintln!("s4cb1_program_cleanup_failed: {error}");
        }
    }
}
struct NoNative;
impl crate::service::Driver for NoNative {
    fn run(&self, _: &mut crate::service::Job) -> Result<(), PublicError> {
        panic!("model-free admission witness must not execute native code")
    }
}
fn compiler_verdict(root: &Path, program: Option<NativeProgram>) -> String {
    let state = root.join("state");
    let authority = Arc::new(Mutex::new(Authority::load(state.clone()).unwrap()));
    let node = NodeService::new(
        Arc::new(NoNative),
        "model-free-native-program".into(),
        crate::native::development_qualification().unwrap(),
    );
    let host = PackagedRun::new(
        authority,
        Network::new(&state, crate::packaged_network::NetworkConfig::default()),
        node,
        ModelHooks {
            acquisition: Box::new(|| None),
            resource_check: Box::new(|| None),
            reverify: Box::new(|| panic!("no model in admission witness")),
        },
        None,
    );
    let mut ports = PackagedChainPorts::new(&host, None, program);
    match ports.ports().compile.unwrap().poll_build() {
        BuildPoll::Done(Err(error)) => error.code_or("missing_code").into(),
        _ => panic!("an admission-only compiler must not run or manufacture a proof"),
    }
}
#[test]
fn packaged_compiler_selection_is_explicit_with_missing_or_hostile_environment() {
    if std::env::var_os("S4CB1_PACKAGED_ENV_CHILD").is_some() {
        let scratch = Scratch::new();
        assert_eq!(compiler_verdict(&scratch.0, None), "compiler_not_started");
        assert_eq!(
            compiler_verdict(&scratch.0, Some(scratch.program())),
            "compiler_not_started"
        );
        let mut mismatch = scratch.program();
        mismatch.expected.sha256 = "0".repeat(64);
        assert_eq!(
            compiler_verdict(&scratch.0, Some(mismatch)),
            "native_binary_changed"
        );
        return;
    }
    for hostile in [false, true] {
        let mut child = std::process::Command::new(std::env::current_exe().unwrap());
        child.args(["--exact", "packaged_run::program_tests::packaged_compiler_selection_is_explicit_with_missing_or_hostile_environment", "--nocapture"])
            .env("S4CB1_PACKAGED_ENV_CHILD", "1")
            .env_remove("MYCELIUM_COMPILER_NATIVE_BINARY")
            .env_remove("MYCELIUM_COMPILER_NATIVE_SHA256");
        if hostile {
            child
                .env(
                    "MYCELIUM_COMPILER_NATIVE_BINARY",
                    "/missing-untrusted-program",
                )
                .env("MYCELIUM_COMPILER_NATIVE_SHA256", "bad-pin");
        }
        let output = child.output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
}

#[test]
fn startup_manifest_identity_is_bound_to_the_verified_executable() {
    use desktop::package::{Entry, Kind, Manifest, admit_bundle};
    let scratch = Scratch::new();
    let mut entries = Vec::new();
    for (path, kind) in [
        ("Contents/MacOS/mycelium", Kind::Executable),
        ("Contents/Frameworks/libmlx.dylib", Kind::Dylib),
        ("Contents/Frameworks/libmlxc.dylib", Kind::Dylib),
        ("Contents/Frameworks/mlx.metallib", Kind::Metal),
    ] {
        let file = scratch.0.join(path);
        std::fs::create_dir_all(file.parent().unwrap()).unwrap();
        let bytes = b"model-free package admission fixture, never loaded";
        std::fs::write(&file, bytes).unwrap();
        std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o700)).unwrap();
        entries.push(Entry {
            path: path.into(),
            kind,
            bytes: bytes.len() as u64,
            sha256: protocol::canonical::sha256_prefixed(bytes)[7..].into(),
        });
    }
    let manifest = Manifest { schema: 1, entries };
    let executable = scratch.0.join("Contents/MacOS/mycelium");
    let verified = admit_bundle(&executable, &manifest, "macos", "aarch64", "26.5.1").unwrap();
    let entry = &manifest.entries[0];
    let program = crate::packaged::admitted_native_program(&verified, entry, &executable).unwrap();
    assert_eq!(
        compiler_verdict(&scratch.0, Some(program)),
        "compiler_not_started"
    );
    assert_eq!(
        crate::packaged::admitted_native_program(&verified, entry, &scratch.program().binary)
            .unwrap_err(),
        "native_executable_admission_mismatch"
    );
    // Do not measure new expected bytes after admission: the retained manifest
    // must reject a same-length mutation even though the path remains executable.
    std::fs::write(&executable, vec![b'z'; entry.bytes as usize]).unwrap();
    assert_eq!(
        crate::packaged::admitted_native_program(&verified, entry, &executable).unwrap_err(),
        "native_binary_changed"
    );
}

#[test]
fn explicit_expected_identity_reaches_the_packaged_compiler() {
    let scratch = Scratch::new();
    let mut program = scratch.program();
    program.expected.bytes += 1;
    assert_eq!(
        compiler_verdict(&scratch.0, Some(program)),
        "native_binary_changed",
        "the packaged compiler must check the supplied admission, not select an environment program"
    );
}
