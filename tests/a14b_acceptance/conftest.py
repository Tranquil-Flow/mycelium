# SPDX-License-Identifier: AGPL-3.0-only
"""A14b coarse-region acceptance suite: promoted to default collection.

This suite was RED by design (marked `scaffold_red`) until A14b executed. The
coarse-region contract, reduction and projection widening landed in 810442b,
operator-declared publication in b29a630 and the optional globe renderer in
`ui/web/src/features/routeExplorer/{globe.ts,RegionGlobe.tsx}`, so every test
here now runs in ordinary and L2 collection. Passing it is deterministic
evidence only: it is not the browser, physical, privacy-in-a-live-window or
completion-checklist acceptance that spec §8 still requires.

See docs/operations/red-by-design-suites.md.
"""
