// SPDX-License-Identifier: AGPL-3.0-only
fn cleanup() -> Result<(), E> {
    if let Err(error) = stop() { record_error("STOP_FAILED", &error, "stop"); }
    Ok(()) // expect: SE005
}
fn stopped() -> Result<(), E> {
    if let Err(error) = stop() { record_error("STOP_FAILED", &error, "stop"); return Ok(()); } // expect: SE005
    Ok(())
}
fn teardown() {
    match worker.join() { Ok(()) => (), Err(error) => record_error("JOIN_FAILED", &error, "join") }
    record_success(); // expect: SE005
}
fn close() -> Result<(), E> {
    if let Err(error) = worker.close() { if retry { return Err(error); } }
    return Ok(()); // expect: SE005
}
