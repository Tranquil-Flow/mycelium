// SPDX-License-Identifier: AGPL-3.0-only
fn run() {
    stop().ok(); // expect: SE002
    (worker.join().ok()); // expect: SE002
}
