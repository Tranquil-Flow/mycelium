# SPDX-License-Identifier: AGPL-3.0-only
"""Native Linux A13 managed host (the ``managed_https`` target side).

The Linux twin of ``mycelium_a13_windows.managed_host`` under the owner parity
ruling (2026-10-07): the class ``linux_native_iroh`` runs the NumPy stage
runtime and is activation-eligible.  Linux is POSIX, so the macOS target
pieces run unchanged -- the canonical POSIX staging script, the worker's
process-group accounting, owner-only modes -- and this package only adds the
Linux class, capability and resource observations, the installed sidecar, the
installation descriptor and the host CLI.

Verified on the Mac's POSIX backend; not yet run on a Linux device.
"""
