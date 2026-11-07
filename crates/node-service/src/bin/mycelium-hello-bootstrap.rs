// SPDX-License-Identifier: AGPL-3.0-only
#[path = "../hello_bootstrap.rs"]
mod hello_bootstrap;
fn main() {
    if let Err(code) = hello_bootstrap::run() {
        eprintln!("{code}");
        std::process::exit(1);
    }
}
