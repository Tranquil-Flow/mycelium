// SPDX-License-Identifier: AGPL-3.0-only
//! The actual socket worker refuses proof requests before any resident image.
#![cfg(feature = "metal")]
use node_service::worker_link::Link;
use std::{
    os::{fd::OwnedFd, unix::net::UnixStream},
    process::{Child, Command, Stdio},
};
struct Owned(Child);
impl Drop for Owned {
    fn drop(&mut self) {
        if self.0.try_wait().expect("worker status").is_none() {
            self.0.kill().expect("kill owned test worker");
        }
        self.0.wait().expect("reap owned test worker");
    }
}
#[test]
#[ignore = "requires explicitly built Metal native worker"]
fn real_native_worker_refuses_resident_proof_before_prepare() {
    let (client, server) = UnixStream::pair().unwrap();
    let _child = Owned(
        Command::new(env!("CARGO_BIN_EXE_mycelium-node"))
            .arg("native-worker")
            .stdin(Stdio::null())
            .stdout(Stdio::from(OwnedFd::from(server)))
            .spawn()
            .unwrap(),
    );
    let wait = budgets::BudgetId::MemberChosenWall
        .span(budgets::Entries::ZERO)
        .unwrap();
    let mut link = Link::new(client, std::time::Duration::from_millis(wait.millis())).unwrap();
    link.send(&serde_json::json!({"command":"resident_proof_v1",
        "protocol":"mycelium.native_resident_request.v1", "nonce":"00112233445566778899aabbccddeeff"}), &[]).unwrap();
    let (reply, body) = link.receive().unwrap();
    assert!(body.is_empty());
    assert_eq!(reply, serde_json::json!({"error":"worker_not_prepared"}));
}
