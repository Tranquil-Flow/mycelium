// SPDX-License-Identifier: AGPL-3.0-only
//! Model-free identity checks. Executable fixtures are data and never run.
use super::*;
use std::os::unix::fs::{PermissionsExt, symlink};
struct Fixture {
    root: PathBuf,
    program: NativeProgram,
}
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "s4cb1-pin-{}",
            crate::app_host::hex(&crate::app_host::random(16).unwrap())
        ));
        fs::create_dir(&root).unwrap();
        let binary = root.join("worker");
        let bytes = vec![b'x'; 3 * 64 * 1024];
        fs::write(&binary, &bytes).unwrap();
        fs::set_permissions(&binary, fs::Permissions::from_mode(0o700)).unwrap();
        Self {
            root,
            program: NativeProgram {
                binary,
                expected: Pin {
                    bytes: bytes.len() as u64,
                    sha256: format!("{:x}", Sha256::digest(&bytes)),
                },
            },
        }
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        if let Err(error) = fs::remove_dir_all(&self.root) {
            eprintln!("native_program_fixture_cleanup_failed: {error}");
        }
    }
}
#[test]
fn explicit_unavailable_does_not_select_the_development_environment() {
    if std::env::var_os("S4CB1_ENV_CHILD").is_some() {
        assert!(
            ProgramSelection::Explicit(None)
                .resolve(&|| false)
                .unwrap()
                .is_none()
        );
        assert!(ProgramSelection::Environment.resolve(&|| false).is_err());
        let f = Fixture::new();
        assert!(
            ProgramSelection::Explicit(Some(f.program.clone()))
                .resolve(&|| false)
                .unwrap()
                .is_some()
        );
        return;
    }
    for pin in [None, Some("malicious-pin")] {
        let mut child = std::process::Command::new(std::env::current_exe().unwrap());
        child.args(["--exact", "native_compiler::program_tests::explicit_unavailable_does_not_select_the_development_environment", "--nocapture"])
            .env("S4CB1_ENV_CHILD", "1")
            .env("MYCELIUM_COMPILER_NATIVE_BINARY", "/missing-untrusted-native-worker")
            .env_remove("MYCELIUM_COMPILER_NATIVE_SHA256");
        if let Some(pin) = pin {
            child.env("MYCELIUM_COMPILER_NATIVE_SHA256", pin);
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
fn chunk_hashing_observes_stop_and_accepts_the_system_temp_alias() {
    let f = Fixture::new();
    assert_eq!(
        f.program.clone().admit(&|| false).unwrap().binary,
        f.program.binary.canonicalize().unwrap()
    );
    let calls = std::cell::Cell::new(0);
    let stopped = || {
        calls.set(calls.get() + 1);
        calls.get() == 3
    };
    assert_eq!(
        f.program.clone().admit(&stopped).unwrap_err().0,
        "compiler_stopped"
    );
    assert_eq!(calls.get(), 3, "Stop was checked during the chunk scan");
}
#[test]
fn malformed_pin_permissions_links_and_fifo_are_refused_without_execution() {
    let f = Fixture::new();
    let mut malformed = f.program.clone();
    malformed.expected.sha256 = "X".repeat(64);
    assert_eq!(
        malformed.admit(&|| false).unwrap_err().0,
        "native_path_invalid"
    );
    fs::set_permissions(&f.program.binary, fs::Permissions::from_mode(0o600)).unwrap();
    assert_eq!(
        f.program.clone().admit(&|| false).unwrap_err().0,
        "native_path_invalid"
    );
    let real = f.root.join("original");
    fs::rename(&f.program.binary, &real).unwrap();
    symlink(&real, &f.program.binary).unwrap();
    assert_eq!(
        f.program.clone().admit(&|| false).unwrap_err().0,
        "native_path_invalid"
    );
    fs::remove_file(&f.program.binary).unwrap();
    // A FIFO with no writer used to hang a read-only open. The test's registered
    // nextest deadline is a backstop; no thread is left blocked in this fixture.
    assert!(
        std::process::Command::new("/usr/bin/mkfifo")
            .arg(&f.program.binary)
            .status()
            .unwrap()
            .success()
    );
    assert_eq!(
        f.program.clone().admit(&|| false).unwrap_err().0,
        "native_path_invalid"
    );
}
#[test]
fn each_spawn_rechecks_captured_bytes_and_refuses_a_removed_or_swapped_path() {
    for change in ["digest", "size", "missing", "symlink", "parent"] {
        let f = Fixture::new();
        let config = ProgramSelection::Explicit(Some(f.program.clone()))
            .resolve(&|| false)
            .unwrap()
            .unwrap();
        match change {
            "digest" => fs::write(&config.binary, vec![b'y'; config.pin.bytes as usize]).unwrap(),
            "size" => fs::write(&config.binary, b"different").unwrap(),
            "missing" => fs::remove_file(&config.binary).unwrap(),
            "symlink" => {
                let other = f.root.join("other");
                fs::rename(&config.binary, &other).unwrap();
                symlink(other, &config.binary).unwrap();
            }
            "parent" => {
                // Swap a directory BELOW the admitted trusted root for a link.
                let nested = f.root.join("nested");
                fs::create_dir(&nested).unwrap();
                let bytes = fs::read(&config.binary).unwrap();
                let mut nested_program = f.program.clone();
                nested_program.binary = nested.join("worker");
                fs::write(&nested_program.binary, bytes).unwrap();
                fs::set_permissions(&nested_program.binary, fs::Permissions::from_mode(0o700))
                    .unwrap();
                let nested_config = ProgramSelection::Explicit(Some(nested_program))
                    .resolve(&|| false)
                    .unwrap()
                    .unwrap();
                let moved = f.root.join("moved");
                fs::rename(&nested, &moved).unwrap();
                symlink(moved, nested).unwrap();
                assert_eq!(nested_config.verify().unwrap_err().0, "native_path_invalid");
                continue;
            }
            _ => unreachable!(),
        }
        let error = match worker::Worker::start(
            &config,
            &json!({"command":"prepare"}),
            Arc::new(AtomicBool::new(false)),
            &f.root,
        ) {
            Err(error) => error,
            Ok(_) => panic!("a changed program was executed"),
        };
        assert!(
            matches!(
                error.0,
                "native_binary_changed" | "native_file_unavailable" | "native_path_invalid"
            ),
            "{change}: {error:?}"
        );
    }
}
