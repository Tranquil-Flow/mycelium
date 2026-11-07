// SPDX-License-Identifier: AGPL-3.0-only
//! Static origin-binding contract, not behavioral acquisition proof. Equal-valued
//! policies are not interchangeable; the executed acquisition/manifest replays
//! separately check unchanged behavior.
#[test]
fn acquisition_and_manifest_use_their_own_registered_origins() {
    let acquisition = include_str!("../src/acquisition.rs");
    let manifest = include_str!("../src/local_manifest.rs");
    for id in [
        "AcquisitionMaxRepositoryFiles",
        "AcquisitionMaxPathParts",
        "AcquisitionStreamChunkBytes",
        "AcquisitionCloseJoinSlack",
    ] {
        assert!(
            acquisition.contains(&format!("BudgetId::{id}")),
            "missing acquisition origin {id}"
        );
    }
    for id in [
        "PhysicalDeploymentRelativePathMaxChars",
        "PhysicalDeploymentRelativePathMaxParts",
    ] {
        assert!(
            manifest.contains(&format!("BudgetId::{id}")),
            "missing manifest origin {id}"
        );
    }
    assert!(!acquisition.contains("BudgetId::PhysicalDeploymentRelativePathMaxParts"));
    assert!(!manifest.contains("BudgetId::AcquisitionMaxPathParts"));
    // This equal-valued HTTP framing bound is NOT the acquisition chunk origin.
    assert!(acquisition.contains("const MAX_REDIRECT_HEAD: usize = 64 * 1024;"));
}
