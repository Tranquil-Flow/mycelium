# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV25: idle weight unload with lazy warm reload, plugged-in-only sharing support, energy estimate.

Platform-neutral core.  Nothing here imports the A13 apps, the live route or the native node; those
reach this package through narrow hooks so the same interfaces serve macOS, Linux and Windows.
"""
