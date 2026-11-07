// SPDX-License-Identifier: AGPL-3.0-only
fn run() {
    std::thread::spawn(worker); // expect: SE004
    thread::spawn(worker); // expect: SE004
    tokio::spawn(async { work().await }); // expect: SE004
}
