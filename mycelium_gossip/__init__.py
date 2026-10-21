# SPDX-License-Identifier: AGPL-3.0-only
"""Transport-neutral Mycelium gossip evidence contracts.

This package deliberately contains no router, allocator, planner, or simulator imports.
"""

from .schema import RecordEnvelope, RecordKind, SchemaError, build_record

__all__ = ["RecordEnvelope", "RecordKind", "SchemaError", "build_record"]
