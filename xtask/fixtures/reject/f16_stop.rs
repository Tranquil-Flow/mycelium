// SPDX-License-Identifier: AGPL-3.0-only
// MODEL negative controls; not execution of the historical product.
// L13-ROLLSTOP.md:15 records the pre-fix swallowed stop failure / 30-second joins.
// Fixed seam: mycelium_a13_macos/demo_chain.py:1350-1378 @ f5bfb709.
// Join/error-before-success: mycelium_a13_macos/chain_compile.py:415-443 @ f5bfb709.
// Retained legacy override: mycelium_a13_macos/chain_compile.py:480 @ f5bfb709.
fn retire_route() {
    let _ = stop(route); // expect: SE001
    match route.join() { Ok(()) => (), Err(_) => {} } // expect: SE003
    let stop_wait = Duration::from_secs(30); // expect: TM001
}
