# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV29 L2: wire dtype negotiation over declared backends (the route-compiler seam)."""

from __future__ import annotations

import pytest

from mycelium_platform_neutral.primitives import PlatformNeutralContractError
from mycelium_platform_neutral.stage_capabilities import (
    DECLARED_BACKEND_WIRE_DTYPES,
    negotiate_route_wire_dtype_for_backends,
)

_ENABLED_ALL = ("float32", "float16", "bfloat16")


def _code(raised: pytest.ExceptionInfo[PlatformNeutralContractError]) -> str:
    return raised.value.code


def test_declared_ceilings_match_the_backend_rules() -> None:
    assert DECLARED_BACKEND_WIRE_DTYPES["numpy"] == frozenset({"float32"})
    assert DECLARED_BACKEND_WIRE_DTYPES["mobile-rust-qwen2"] == frozenset({"float32"})
    assert DECLARED_BACKEND_WIRE_DTYPES["mlx"] == frozenset({"float32", "float16", "bfloat16"})
    assert set(DECLARED_BACKEND_WIRE_DTYPES) == {"mlx", "numpy", "llama_stage", "mobile-rust-qwen2"}


def test_mac_plus_numpy_negotiates_float32() -> None:
    assert negotiate_route_wire_dtype_for_backends(("mlx", "numpy")) == "float32"
    assert negotiate_route_wire_dtype_for_backends(("mlx", "mlx")) == "float32"


def test_mixed_backends_never_get_reduced_precision() -> None:
    with pytest.raises(PlatformNeutralContractError) as raised:
        negotiate_route_wire_dtype_for_backends(
            ("mlx", "numpy"), requested="float16", enabled=_ENABLED_ALL
        )
    assert _code(raised) == "mixed_backend_route_requires_float32"
    # Same-backend numpy cannot do float16 at all.
    with pytest.raises(PlatformNeutralContractError) as raised:
        negotiate_route_wire_dtype_for_backends(
            ("numpy", "numpy"), requested="float16", enabled=_ENABLED_ALL
        )
    assert _code(raised) == "wire_dtype_negotiation_failed"


def test_reduced_precision_is_not_enabled_by_default() -> None:
    with pytest.raises(PlatformNeutralContractError) as raised:
        negotiate_route_wire_dtype_for_backends(("mlx", "mlx"), requested="bfloat16")
    assert _code(raised) == "wire_dtype_not_enabled"
    assert (
        negotiate_route_wire_dtype_for_backends(
            ("mlx", "mlx"), requested="bfloat16", enabled=_ENABLED_ALL
        )
        == "bfloat16"
    )


def test_unregistered_and_fenced_backends_are_refused() -> None:
    for backends in ((), ("mlx", "cuda"), ("llama_http",), "mlx", None):
        with pytest.raises(PlatformNeutralContractError):
            negotiate_route_wire_dtype_for_backends(backends)  # type: ignore[arg-type]
    with pytest.raises(PlatformNeutralContractError) as raised:
        negotiate_route_wire_dtype_for_backends(("mlx", "llama_stage"))
    assert _code(raised) == "fenced_stage_backend_unavailable"
    assert (
        negotiate_route_wire_dtype_for_backends(
            ("mlx", "llama_stage"), runtime_present=frozenset({"llama_stage"})
        )
        == "float32"
    )
