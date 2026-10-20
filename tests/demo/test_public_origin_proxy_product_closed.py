# SPDX-License-Identifier: AGPL-3.0-only
"""M1: with the swarm directory mounted, the legacy product-gateway surface is closed on the public origin.

FAKES (labelled): the product and directory upstreams are recording stubs.  Everything else is the shipped proxy.
Review: R3 F1 (the product routes escaped every swarm guard rail and stamped every visitor as the owner).
"""
from __future__ import annotations

import pytest

from mycelium_demo import public_origin_proxy as proxy
from _proxy_world import CAPABILITY, IP, call, world

PRODUCT_ROUTES = (
    ("POST", "/api/v1/inference"),
    ("GET", "/api/v1/bootstrap"),
    ("GET", "/api/v1/product/export"),
    ("GET", "/api/v1/observatory/events"),
    ("GET", "/api/v1/swarm/status"),
    ("GET", "/api/v1/inference/abc/events"),
    ("DELETE", "/api/v1/inference/abc/cancel"),
    ("GET", "/anything"),
    ("GET", "/assets/app.js"),
    ("HEAD", "/index.html"),
    ("GET", "/swarmsfoo"),
)


@pytest.mark.parametrize("method,path", PRODUCT_ROUTES)
def test_product_routes_are_404_with_zero_upstream_connections_when_the_directory_is_mounted(method: str, path: str) -> None:
    with world() as (public, product, directory):
        status, _headers, _body = call(public.server_port, method, path, body=b"{}" if method == "POST" else b"",
                                       headers={**IP, "Content-Type": "application/json"})
        assert status == 404, (method, path)
        assert product.requests == [] and directory.requests == []


def test_the_swarms_surface_and_the_root_redirect_still_work_with_product_routes_closed() -> None:
    with world() as (public, product, directory):
        status, headers, _ = call(public.server_port, "GET", "/")
        assert status == 302 and headers["location"] == "/swarms/"
        assert call(public.server_port, "GET", "/swarms", headers=IP)[0] == 200
        assert call(public.server_port, "GET", "/swarms/api/v1/config", headers=IP)[0] == 200
        assert [r["path"] for r in directory.requests] == ["/swarms", "/swarms/api/v1/config"]
        assert product.requests == []


def test_closed_even_when_the_root_redirect_is_off() -> None:
    with world(redirect_root_to_swarms=False) as (public, product, _directory):
        assert call(public.server_port, "GET", "/")[0] == 404
        assert call(public.server_port, "GET", "/api/v1/bootstrap")[0] == 404
        assert product.requests == []


def test_blocked_prefixes_stay_unreachable_with_product_routes_closed() -> None:
    with world() as (public, product, _directory):
        for method, path in (("GET", "/__mycelium/live-status"), ("GET", "/api/v1/swarm/invites"),
                             ("POST", "/api/v1/swarm/join"), ("POST", "/v1/inference")):
            assert call(public.server_port, method, path, headers=IP)[0] in {403, 404}, path
        assert product.requests == []


def test_explicit_flag_restores_the_product_routes_unchanged() -> None:
    with world(redirect_root_to_swarms=False, enable_product_routes=True) as (public, product, directory):
        port = public.server_port
        assert call(port, "GET", "/")[0] == 200
        assert call(port, "GET", "/api/v1/bootstrap")[0] == 200
        assert call(port, "POST", "/api/v1/inference", body=b"{}", headers={"Content-Type": "application/json"})[0] == 200
        assert call(port, "GET", "/api/v1/secret")[0] == 404
        assert [r["path"] for r in product.requests] == ["/", "/api/v1/bootstrap", "/api/v1/inference"]
        assert directory.requests == []


def test_flag_off_is_the_default_and_the_cli_exposes_it(tmp_path) -> None:
    base = dict(listen_host="127.0.0.1", listen_port=4350, upstream_host="127.0.0.1", upstream_port=4351,
                public_host="mycelium.now", capability=CAPABILITY)
    assert proxy.ProxyConfig(**base).enable_product_routes is False
    cap = tmp_path / "cap"
    cap.write_text(CAPABILITY)
    cap.chmod(0o600)
    argv = ["--upstream", "http://127.0.0.1:4351", "--trusted-proxy-capability-file", str(cap),
            "--directory-upstream", "http://127.0.0.1:4360"]
    assert proxy.build_config_from_args(argv).enable_product_routes is False
    assert proxy.build_config_from_args([*argv, "--enable-product-routes"]).enable_product_routes is True
    with pytest.raises(proxy.PublicOriginProxyError, match="invalid_enable_product_routes"):
        proxy.ProxyConfig(**base, enable_product_routes="yes")


def test_without_a_mounted_directory_the_product_proxy_is_unchanged() -> None:
    with world(mount=False) as (public, product, _directory):
        assert call(public.server_port, "GET", "/api/v1/bootstrap")[0] == 200
        assert [r["path"] for r in product.requests] == ["/api/v1/bootstrap"]
