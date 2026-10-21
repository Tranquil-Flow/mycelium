# SPDX-License-Identifier: AGPL-3.0-only
"""Grant-gated A6 controlled-fault receiver mounted by the ordinary product.

The browser harness (``ui/web/scripts/a6-product-browser-e2e.mjs``) and the
physical gate send one ``execute_controlled_source_failure`` request per case to
a local-only fault-control origin beside the product origin. Until now that
receiver existed only in explicit test compositions; the product serve process
never mounted it, so every live control request could only be refused.

This module is the product mount, and only that:

* it exists only while the serve process holds the exact fleet grant in its
  own environment, and every request must present the same grant again;
* it listens on its own direct IPv4 loopback socket (never the product HTTP
  handler, a proxy or a public origin), so the peer the receiver checks is the
  real TCP peer and cannot be rewritten by forwarding middleware;
* it refuses to start when the product is served behind a trusted HTTPS proxy
  or a public origin, where loopback provenance no longer identifies the owner;
* it dispatches only to the seams the product composition injects; with no
  seam for a case the mechanism refuses that case and nothing is recorded.

A mounted receiver is a control surface, not evidence. Its status never claims
a qualification, a physical fault, or a passing case.
"""
from __future__ import annotations

import hmac
import os
import time
from typing import Any, Callable, Mapping

from mycelium_a6_direct_socket import A6DirectSocketServer
from mycelium_a6_fault_control import EXACT_FLEET_GRANT, FaultSeam
from mycelium_a6_http_fault_control import A6LocalFaultReceiver

GRANT_ENVIRONMENT = "MYCELIUM_A6_FLEET_GRANT"
CONTROL_PATH = "/a6/controlled-fault"
STATUS_PROTOCOL = "mycelium.a6_product_fault_control.v1"


class ProductFaultControlRefused(ValueError):
    """The product refuses to mount the controlled-fault receiver."""


def exact_grant(value: object) -> bool:
    return isinstance(value, str) and hmac.compare_digest(
        value.encode("utf-8"), EXACT_FLEET_GRANT.encode("ascii")
    )


def grant_from_environment(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the exact grant from the serve process environment, else None."""

    value = (os.environ if environ is None else environ).get(GRANT_ENVIRONMENT)
    return value if exact_grant(value) else None


class ProductFaultControl:
    """One product-owned receiver lifetime on one direct loopback listener."""

    def __init__(
        self,
        *,
        grant: str | None,
        seams: Mapping[str, FaultSeam],
        product_port: int,
        port: int = 0,
        public_origin: str | None = None,
        trusted_https_proxy: bool = False,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not exact_grant(grant):
            raise ProductFaultControlRefused("explicit_fleet_grant_required")
        if public_origin is not None or trusted_https_proxy is not False:
            # Behind a proxy every caller arrives from loopback: the direct-peer
            # check would no longer distinguish the owner from the public.
            raise ProductFaultControlRefused(
                "a6_fault_control_requires_direct_loopback_product"
            )
        if type(product_port) is not int or not 1 <= product_port <= 65_535:
            raise ProductFaultControlRefused("a6_fault_control_product_port_invalid")
        if type(port) is not int or (port != 0 and not 1_024 <= port <= 65_535):
            raise ProductFaultControlRefused("a6_fault_control_port_invalid")
        if port == product_port:
            raise ProductFaultControlRefused("a6_fault_control_port_is_product_origin")
        if not isinstance(seams, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, FaultSeam)
            or value.seam_id != key
            for key, value in seams.items()
        ):
            raise ProductFaultControlRefused("a6_fault_control_seams_invalid")
        assert grant is not None
        self._receiver = A6LocalFaultReceiver(
            seams=dict(seams),
            clock_ms=clock_ms or (lambda: int(time.time() * 1_000)),
            path=CONTROL_PATH,
            product_grant=grant,
        )
        self._server = A6DirectSocketServer(
            receiver=self._receiver,
            bind_host="127.0.0.1",
            port=port,
            product_grant=grant,
        )
        self._seam_ids = tuple(sorted(seams))
        self._started = False
        self._closed = False

    def __enter__(self) -> "ProductFaultControl":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def start(self) -> None:
        if self._started or self._closed:
            raise ProductFaultControlRefused("a6_fault_control_lifecycle_invalid")
        self._server.__enter__()
        self._started = True

    def close(self) -> None:
        if self._started and not self._closed:
            self._server.__exit__(None, None, None)
        self._closed = True

    @property
    def port(self) -> int:
        if not self._started or self._closed:
            raise ProductFaultControlRefused("a6_fault_control_not_listening")
        return self._server.port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{CONTROL_PATH}"

    @property
    def executed_case_ids(self) -> tuple[str, ...]:
        return self._receiver.executed_case_ids

    def status(self) -> dict[str, Any]:
        """Privacy-reduced mount status; never a qualification or case result."""

        listening = self._started and not self._closed
        return {
            "protocol": STATUS_PROTOCOL,
            "mounted": listening,
            "url": self.url if listening else None,
            "lifetime_id": self._server.lifetime_id if self._started else None,
            "seam_ids": list(self._seam_ids),
            "executed_case_ids": list(self._receiver.executed_case_ids),
            "grant_required_per_request": True,
            "qualification_claim": False,
            "physical_evidence": False,
        }


__all__ = [
    "CONTROL_PATH",
    "GRANT_ENVIRONMENT",
    "ProductFaultControl",
    "ProductFaultControlRefused",
    "STATUS_PROTOCOL",
    "exact_grant",
    "grant_from_environment",
]
