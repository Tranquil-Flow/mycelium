// SPDX-License-Identifier: AGPL-3.0-only
use super::*;
use std::io::{BufRead, BufReader, Read, Write};
use std::process::{Child, Command, Stdio};
struct Owned(Child);
impl std::ops::Deref for Owned {
    type Target = Child;
    fn deref(&self) -> &Child {
        &self.0
    }
}
impl std::ops::DerefMut for Owned {
    fn deref_mut(&mut self) -> &mut Child {
        &mut self.0
    }
}
impl Drop for Owned {
    fn drop(&mut self) {
        match self.0.try_wait() {
            Ok(Some(_)) => {}
            other => {
                eprintln!("owned child cleanup {:?}", other);
                if let Err(e) = self.0.kill() {
                    eprintln!("owned kill: {e}")
                };
                if let Err(e) = self.0.wait() {
                    eprintln!("owned reap: {e}")
                }
            }
        }
    }
}

#[test]
fn owned_process_lock_and_crash_reopen() {
    if let Ok(path) = std::env::var("MYCELIUM_OWNER_CHILD_ROOT") {
        let mode = std::env::var("MYCELIUM_OWNER_CHILD_MODE").unwrap();
        println!("OWNED-READY {}", std::process::id());
        std::io::stdout().flush().unwrap();
        let mut byte = [0];
        std::io::stdin().read_exact(&mut byte).unwrap();
        let result = ManagedControlOwner::open(
            PathBuf::from(path).as_path(),
            &signer(),
            "swarm",
            "owner",
            Limits::registered().unwrap(),
        );
        if mode == "locked" {
            assert_eq!(result.err().unwrap().0, "managed_owner_already_running");
            return;
        }
        let mut owner = result.expect("child owner");
        owner
            .prepare_enqueue(&signer(), &binding(), &command(), meta())
            .unwrap();
        // Deliberate finite crash: skip destructors after the durable write.
        std::process::exit(42);
    }
    let r = Root::new();
    let o = open(&r);
    run_child(&r, "locked", 0);
    drop(o);
    run_child(&r, "crash", 42);
    let saved = disk(&r);
    assert_eq!(saved, case("uncertain_enqueue")["journal"]);
    let mut o = open(&r);
    let prepared = o
        .prepare_enqueue(&signer(), &binding(), &command(), meta())
        .unwrap();
    assert_eq!(prepared, saved["commands"]["hello-1"]["enqueue_request"]);
    assert_eq!(
        o.prepare_result(&signer(), &binding(), &command(), meta())
            .unwrap()["envelope"]["statement"]["sequence"],
        128
    );
}
fn run_child(r: &Root, mode: &str, exit: i32) {
    let mut child = Owned(
        Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "process::owned_process_lock_and_crash_reopen",
                "--nocapture",
            ])
            .env("MYCELIUM_OWNER_CHILD_ROOT", &r.0)
            .env("MYCELIUM_OWNER_CHILD_MODE", mode)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap(),
    );
    let pid = child.id();
    let stdout = child.stdout.take().unwrap();
    let mut reader = BufReader::new(stdout);
    let mut line = String::new();
    loop {
        line.clear();
        assert!(
            reader.read_line(&mut line).unwrap() > 0,
            "child startup failed"
        );
        if line.starts_with("OWNED-READY ") {
            break;
        }
    }
    assert_eq!(line.trim(), format!("OWNED-READY {pid}"));
    let birth = Command::new("/bin/ps")
        .args(["-p", &pid.to_string(), "-o", "lstart="])
        .output()
        .unwrap();
    assert!(birth.status.success());
    assert!(!birth.stdout.is_empty());
    println!(
        "OWNED pid={pid} birth={} mode={mode}",
        String::from_utf8_lossy(&birth.stdout).trim()
    );
    child.stdin.take().unwrap().write_all(b"x").unwrap();
    let mut output = String::new();
    reader.read_to_string(&mut output).unwrap();
    let status = child.wait().unwrap();
    assert_eq!(status.code(), Some(exit), "{output}");
    assert!(
        child.try_wait().unwrap().is_some(),
        "exact owned child reaped"
    );
    println!("REAPED pid={pid} exit={exit}");
}
