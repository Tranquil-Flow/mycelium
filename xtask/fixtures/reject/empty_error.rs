// SPDX-License-Identifier: AGPL-3.0-only
fn run() {
    match worker.join() { Ok(()) => (), Err(_) => {} } // expect: SE003
    match work() { Ok(()) => (), Err(e) => { /* ignored */ } } // expect: SE003
    if let Err(error) = work() {} // expect: SE003
    if let Err(..) = work() { /* ignored */ } // expect: SE003
}
