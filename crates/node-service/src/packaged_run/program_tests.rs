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
