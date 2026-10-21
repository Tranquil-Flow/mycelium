// SPDX-License-Identifier: AGPL-3.0-only
// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    mycelium_node_app_lib::run();
}
