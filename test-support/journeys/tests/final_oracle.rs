// SPDX-License-Identifier: AGPL-3.0-only
use journey_tests::process::{ORACLE_EXE, ORACLE_SOURCE};

#[test]
fn final_certified_oracle_identity_is_explicit() {
    assert_eq!(ORACLE_SOURCE, "f4e366e98a1f705dfaf182897044500d290dbd7f");
    assert_eq!(
        ORACLE_EXE,
        "8fff41823d357949ffa55c00b3fb8dfb0b4cc541f66343a72f21e3d8dd89186f"
    );
}
