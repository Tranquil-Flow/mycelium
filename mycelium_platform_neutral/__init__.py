"""A9 platform-neutral capability contracts (typed authoring modules).

The nine frozen contracts of spec §4, one module each:

===============================  =========================================
protocol                          module
===============================  =========================================
``mycelium.membership.peer_profile.v2``
    :mod:`mycelium_platform_neutral.peer_profile`
``mycelium.membership.capability_report.v2``
    :mod:`mycelium_platform_neutral.capability_report`
``mycelium.peer_class_qualification.v1``
    :mod:`mycelium_platform_neutral.class_qualification`
``mycelium.peer_eligibility.v1``
    :mod:`mycelium_platform_neutral.eligibility`
``mycelium.membership_contract_migration.v1``
    :mod:`mycelium_platform_neutral.migration`
``mycelium.runtime_acceleration_profile.v1``
    :mod:`mycelium_platform_neutral.acceleration_profile`
``mycelium.stage_performance_observation.v1``
    :mod:`mycelium_platform_neutral.stage_performance`
``mycelium.activation_codec_qualification.v1``
    :mod:`mycelium_platform_neutral.activation_codec`
``mycelium.runtime_efficiency_qualification.v1``
    :mod:`mycelium_platform_neutral.runtime_efficiency`
===============================  =========================================

Beside the nine contracts sits the *source* tranche for the runtime-efficiency
gap (spec §3.4), which is deliberately not a contract and claims nothing:

* :mod:`mycelium_platform_neutral.int8_execution` — bounded persistent int8
  weight execution with a monotonic materialization ledger plus a negative
  control for the rejected per-linear whole-weight cast.
* :mod:`mycelium_platform_neutral.activation_transport` — the reduced-precision
  (float16/bfloat16) activation frame codec: validation, measured byte
  reduction, the seven-class rejection battery, replay/cancellation fences.
* :mod:`mycelium_platform_neutral.efficiency_benchmark` — the honest local
  microbenchmark harness; it evaluates no frozen A9 threshold.  It is
  deliberately NOT imported here: it is a runnable harness
  (``python -m mycelium_platform_neutral.efficiency_benchmark``), not part of
  the package's authoring surface.

These are authoring modules: strict, deterministic validation and derivation
only.  Nothing here is qualified, eligible, placed, or ready — those remain
separate authorities (spec §2), and no physical round has been executed.  The
accelerated Windows x86_64 device blocker stays open and is raised explicitly.
"""

from __future__ import annotations

from . import (
    acceleration_profile,
    activation_codec,
    activation_transport,
    capability_report,
    class_qualification,
    eligibility,
    int8_execution,
    migration,
    peer_profile,
    primitives,
    runtime_efficiency,
    stage_performance,
)
from .primitives import (
    ACCELERATION_PROFILE_PROTOCOL,
    CAPABILITY_REPORT_PROTOCOL,
    CLASS_QUALIFICATION_PROTOCOL,
    CODEC_QUALIFICATION_PROTOCOL,
    EFFICIENCY_QUALIFICATION_PROTOCOL,
    ELIGIBILITY_PROTOCOL,
    FROZEN_A9_PROTOCOLS,
    MIGRATION_PROTOCOL,
    PEER_PROFILE_PROTOCOL,
    STAGE_OBSERVATION_PROTOCOL,
    PlatformNeutralContractError,
)

__all__ = [
    "ACCELERATION_PROFILE_PROTOCOL",
    "CAPABILITY_REPORT_PROTOCOL",
    "CLASS_QUALIFICATION_PROTOCOL",
    "CODEC_QUALIFICATION_PROTOCOL",
    "EFFICIENCY_QUALIFICATION_PROTOCOL",
    "ELIGIBILITY_PROTOCOL",
    "FROZEN_A9_PROTOCOLS",
    "MIGRATION_PROTOCOL",
    "PEER_PROFILE_PROTOCOL",
    "STAGE_OBSERVATION_PROTOCOL",
    "PlatformNeutralContractError",
    "acceleration_profile",
    "activation_codec",
    "activation_transport",
    "capability_report",
    "class_qualification",
    "eligibility",
    "int8_execution",
    "migration",
    "peer_profile",
    "primitives",
    "runtime_efficiency",
    "stage_performance",
]
