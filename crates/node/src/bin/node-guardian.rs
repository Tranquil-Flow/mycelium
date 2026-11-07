// SPDX-License-Identifier: AGPL-3.0-only
fn main() {
    let result = std::env::args()
        .nth(1)
        .and_then(|value| value.parse().ok())
        .ok_or(node::WorkerError::Refused("guardian_frame_length_required"))
        .and_then(node::guardian::run);
    if let Err(error) = result {
        eprintln!("guardian_failed: {error:?}");
        std::process::exit(1);
    }
}
