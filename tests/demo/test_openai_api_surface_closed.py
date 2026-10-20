# SPDX-License-Identifier: AGPL-3.0-only
"""PV22: Profile A stays closed.  The owner's OpenAI-compatible API never appears on the public directory origin.

FAKES (labelled): product and directory upstreams are recording stubs; the proxy and the surface probe are shipped code.
"""
from _proxy_world import IP, call, world

from mycelium_swarm_directory import surface_probe

KEY = {"Authorization": "Bearer myc-sk-" + "a" * 43, "Content-Type": "application/json"}


def test_v1_api_paths_answer_404_on_the_public_origin_and_reach_no_upstream():
    with world() as (public, product, directory):
        for method, path, body in (("GET", "/v1/models", b""), ("GET", "/v1/models/Qwen/Qwen3-0.6B", b""),
                                   ("POST", "/v1/chat/completions", b'{"model":"x","messages":[]}'),
                                   ("POST", "/swarms/v1/chat/completions", b"{}"), ("GET", "/swarms/v1/models", b"")):
            status, _headers, _raw = call(public.server_port, method, path, body=body, headers={**IP, **KEY})
            assert status == 404, (method, path, status)
        assert product.requests == []
        assert [r["path"] for r in directory.requests if "/v1/" in r["path"] and "/api/v1/" not in r["path"]] == []


def test_the_surface_probe_still_passes_with_the_api_code_present():
    with world() as (public, product, _directory):
        result = surface_probe.probe("http://127.0.0.1:%d" % public.server_port, client_ip="203.0.113.7")
        assert result["verdict"] == "closed_and_cookie_free", result
        assert product.requests == []
