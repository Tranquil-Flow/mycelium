# SPDX-License-Identifier: AGPL-3.0-only
"""Requirements-based gate harness (owner direction 2026-10-13).

Gates verify FEATURES on whatever eligible devices are present.  A gate is
described by a versioned, digest-pinned spec of *roles* and *requirements*
(``spec``); eligible members are discovered from the seed membership plus each
member's latest verified, signed capability/link report and bound to roles
deterministically (``binding``); evidence names device classes and capability
digests, never host names, addresses, users or paths (``evidence``); fault
injection is a signed, gate-scoped control verb that the member's own agent
executes (``control``).
"""
