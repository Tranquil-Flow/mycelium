// SPDX-License-Identifier: AGPL-3.0-only
fn best_effort() {
    if let Err(error) = publish_metric() { record_error("METRIC_FAILED", &error, "telemetry"); }
    match publish_metric() { Ok(()) => (), Err(error) => record_error("METRIC_FAILED", &error, "telemetry") }
}
