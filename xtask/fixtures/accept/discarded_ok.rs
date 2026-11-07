// SPDX-License-Identifier: AGPL-3.0-only
fn collect() -> Option<()> { stop().ok() }
fn run() { let optional = work().ok(); consume(optional); }
