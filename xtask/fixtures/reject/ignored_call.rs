// SPDX-License-Identifier: AGPL-3.0-only
fn run() {
    let _ = stop(); // expect: SE001
    let _: Result<(), E> = worker.join(); // expect: SE001
    let _ = (client.close().await); // expect: SE001
}
