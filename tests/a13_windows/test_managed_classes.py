# SPDX-License-Identifier: AGPL-3.0-only
"""Which peer classes may run as A13 managed hosts, and with which stage runtime."""

from __future__ import annotations

import pytest

from mycelium_a13_macos.managed_classes import (
    MANAGED_PEER_RUNTIMES,
    is_managed_peer_class,
    managed_runtime_backend,
)
from mycelium_membership.contracts import _PEER_RUNTIME_CAPABILITIES


def test_managed_classes_are_exact_and_match_the_membership_capability():
    assert MANAGED_PEER_RUNTIMES == {"mac_mlx_iroh": "mlx", "windows_native_iroh": "numpy",
                                     "linux_native_iroh": "numpy"}
    for peer_class, backend in MANAGED_PEER_RUNTIMES.items():
        assert _PEER_RUNTIME_CAPABILITIES[peer_class]["runtime_backend"] == backend
        assert managed_runtime_backend(peer_class) == backend


@pytest.mark.parametrize("value", ["linux_numpy_iroh", "pixel_http", "windows", None, 3, ["mac_mlx_iroh"]])
def test_other_classes_are_not_managed_hosts(value):
    assert not is_managed_peer_class(value)
    with pytest.raises(ValueError, match="managed_peer_class_invalid"):
        managed_runtime_backend(value)
