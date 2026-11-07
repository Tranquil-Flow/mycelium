// SPDX-License-Identifier: AGPL-3.0-only
fn run() {
    let wait = Duration::from_secs(30); // expect: TM001
    let wait = std::time::Duration::from_millis(100); // expect: TM001
    let wait = Duration::from_secs_f64(0.5); // expect: TM001
}
