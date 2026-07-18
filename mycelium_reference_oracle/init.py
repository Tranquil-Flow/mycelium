"""Public surface for the bounded Mycelium numerical reference oracle."""

from .gpt2 import (
    ABSOLUTE_TOLERANCE,
    IMPLEMENTATION_VERSION,
    RELATIVE_TOLERANCE,
    DecodeStep,
    FixtureIdentity,
    ForwardPass,
    GPT2FixtureOracle,
    Generation,
    OracleValidationError,
    expected_tensor_shapes,
    load_gpt2_fixture,
    prompt_digest,
)
from .report import REPORT_PROTOCOL, build_report, canonical_report_json

__all__ = [
    "ABSOLUTE_TOLERANCE",
    "IMPLEMENTATION_VERSION",
    "RELATIVE_TOLERANCE",
    "DecodeStep",
    "FixtureIdentity",
    "ForwardPass",
    "GPT2FixtureOracle",
    "Generation",
    "OracleValidationError",
    "REPORT_PROTOCOL",
    "build_report",
    "canonical_report_json",
    "expected_tensor_shapes",
    "load_gpt2_fixture",
    "prompt_digest",
]
