# SPDX-License-Identifier: AGPL-3.0-only
"""A9 migration-executor lifecycle suite (promoted from RED-by-design).

This directory used to pin the unimplemented migration executor seams with
``scaffold_red`` placeholders.  The executor, checkpoint restart, rollback
before commit, serialized concurrent resume, the recorded cutover, the v1
write gate and migration-only reader removal now run real private filesystem
transactions, so the suite is GREEN and collected by default.

It still proves only the local private transaction.  It does not prove a live
seed cutover, a physical member migration or A9 closure; those are the A9 gate
driver's physical steps (``scripts/run_a9_gate.py``).
"""
