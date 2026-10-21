# SPDX-License-Identifier: AGPL-3.0-only
"""Windows half of the A13 cross-platform onboarding tranche (source prep).

SOURCE ONLY.  Nothing in this package builds, signs, publishes, installs,
launches, or qualifies a Windows installer, and nothing here reads the host's
identity: every platform fact is a caller-supplied observation.  The committed
macOS slice (``mycelium_a13_macos``) and the landed Linux slice
(``mycelium_a13_linux``) are the interfaces this package matches; neither is
modified from here.

Scope of this tranche
---------------------
* ``platform_admission`` -- declared Windows release/build/architecture/service
  admission, mirroring the landed Linux slice's ``admit_linux_platform``.
* ``package_manifest`` -- the closed, signed
  ``mycelium.installation_package_manifest.v1`` shape for a signed Windows
  installer, with fail-closed verification and a pinned Ed25519 publisher
  identity; per owner ruling 2026-09-27 #4 the pilot ``owner_private_media``
  channel carries no Authenticode identity (an explicit unsigned sentinel
  instead), while ``signed_update_channel`` still requires one.
* ``install_source`` -- the normal-user install source (no expert tools) and the
  verify-then-identity install plan for an MSI-class artifact.
* ``service_lifecycle`` -- the least-privilege Windows service declaration, its
  bounded lifecycle outcomes, and the refusal to fabricate a Windows member from
  the repository's launchd/systemd-only shared renderer.
* ``onboarding`` -- target-side pairing request, recipient-only handoff
  decryption, seed-pin check, owner-only ACL requirement, and one atomic local
  join for the ``windows_native_iroh`` peer class.
* ``progress`` -- the frozen, platform-neutral ``mycelium.onboarding_progress.v1``
  ladder, reused from the landed Linux tranche rather than forked.

Not claimed here: a signed Windows installer, a Windows service installation, a
clean-device physical run, Windows driver/provider qualification, and any
eligibility or acceleration claim.
"""

__version__ = "0.1.0"
