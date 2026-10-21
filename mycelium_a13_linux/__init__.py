# SPDX-License-Identifier: AGPL-3.0-only
"""Linux-member half of the A13 cross-platform onboarding tranche.

SOURCE ONLY.  Nothing in this package builds, signs, publishes, installs,
launches, or qualifies a package, and nothing here reads the host's identity:
every platform fact is a caller-supplied observation.  The committed macOS slice
in ``mycelium_a13_macos`` is the interface this package matches; it is never
modified from here.

Scope of this tranche
---------------------
* ``platform_admission`` -- declared Linux distribution/architecture/systemd
  admission, mirroring ``mycelium_a13_macos.app.preflight``.
* ``package_manifest`` -- the closed, signed
  ``mycelium.installation_package_manifest.v1`` shape and its fail-closed
  verification (platform, signature, expiry, publisher pin, rollback floor,
  lifecycle support, embedded-material refusal).
* ``service_lifecycle`` -- the hardened systemd member of the repository's
  existing deterministic service package, plus bounded lifecycle and uninstall
  outcomes.
* ``onboarding`` -- target-side pairing request, recipient-only handoff
  decryption, seed-pin check, and one atomic local join.
* ``progress`` -- the privacy-reduced ``mycelium.onboarding_progress.v1`` ladder.
* ``install_source`` -- the normal-user install source (no expert tools) and the
  verify-then-identity install plan.

Not claimed here: a signed package, a physical clean-device run, Windows
x86_64 evidence, platform signing keys, and any qualification level.
"""

__version__ = "0.1.0"
