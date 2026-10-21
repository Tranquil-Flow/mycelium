# SPDX-License-Identifier: AGPL-3.0-only
"""Platform-neutral A13 packaging core shared by the Windows and Linux tranches.

* :mod:`.manifest` -- the closed, Ed25519-signed
  ``mycelium.installation_package_manifest.v1`` (envelope, the twelve frozen
  bindings, embedded-material scan, signing and the signature/expiry/publisher
  pin/rollback-floor checks), parameterised by a :class:`.manifest.ManifestProfile`
  for what differs per platform (platform block, extra publisher fields, the
  service descriptor's name field and install modes);
* :mod:`.install_source` -- the shared install-source checks;
* :mod:`.build` -- the deterministic build helper (source closure, staging,
  archives, payload tree digest, signer loading);
* :mod:`.installer_core` -- the per-user verify-then-install state machine.

The platform modules (``mycelium_a13_windows``, ``mycelium_a13_linux``) keep
their public names, signatures and refusal codes as thin wrappers.
"""
