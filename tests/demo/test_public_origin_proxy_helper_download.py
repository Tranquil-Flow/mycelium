# SPDX-License-Identifier: AGPL-3.0-only
"""The helper zip served by the /swarms proxy, and the optional '/' -> '/swarms/' redirect.

FAKES (labelled): the product and directory upstreams, and a fixture file standing in for the helper
zip (no real package is used).  Everything else is the shipped proxy.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from mycelium_demo import public_origin_proxy as proxy
from _proxy_world import CAPABILITY, IP, call, world

NAME = "Mycelium-helper-mac.zip"
URL = "/swarms/download/" + NAME
PAYLOAD = b"PK\x03\x04 fixture, not a real package " * 50
SHA = hashlib.sha256(PAYLOAD).hexdigest()
CONFIG = {"directory": "https://mycelium.now", "download": {"available": False, "url": None, "sha256": None,
                                                            "version": "0.1.0-preview.2", "notarized": False}}


@pytest.fixture()
def zip_path(tmp_path: Path) -> Path:
    path = tmp_path / NAME
    path.write_bytes(PAYLOAD)
    path.chmod(0o600)
    return path


def _config(port: int) -> dict:
    status, _, body = call(port, "GET", "/swarms/api/v1/config", headers=IP)
    assert status == 200
    return json.loads(body)


# -- (1) the download --------------------------------------------------------------------------------

def test_served_file_is_the_fixture_with_download_headers_and_never_reaches_an_upstream(zip_path: Path) -> None:
    with world(directory_reply=CONFIG, helper_zip_path=str(zip_path)) as (public, product, directory):
        status, headers, body = call(public.server_port, "GET", URL)
        assert (status, body) == (200, PAYLOAD)
        assert headers["content-type"] == "application/zip"
        assert headers["content-length"] == str(len(PAYLOAD))
        assert headers["content-disposition"] == f'attachment; filename="{NAME}"'
        assert headers["x-content-type-options"] == "nosniff"
        status, headers, body = call(public.server_port, "HEAD", URL)
        assert (status, body) == (200, b"") and headers["content-length"] == str(len(PAYLOAD))
        assert product.requests == [] and directory.requests == []


def test_config_advertises_the_download_with_the_startup_sha_when_the_file_hashes(zip_path: Path) -> None:
    with world(directory_reply=CONFIG, helper_zip_path=str(zip_path), helper_version="9.9.9") as (public, _, _):
        document = _config(public.server_port)
        assert document["directory"] == "https://mycelium.now"
        assert document["download"] == {"available": True, "url": URL, "sha256": SHA, "version": "9.9.9",
                                        "notarized": False, "size": len(PAYLOAD)}


def test_unconfigured_proxy_leaves_the_directory_config_alone_and_serves_nothing() -> None:
    with world(directory_reply=CONFIG) as (public, product, directory):
        assert _config(public.server_port) == CONFIG
        assert call(public.server_port, "GET", URL)[0] == 404
        assert product.requests == []
        assert [r["path"] for r in directory.requests] == ["/swarms/api/v1/config"]


def _assert_not_published(port: int, *, download_status: int) -> None:
    document = _config(port)
    assert document["download"]["available"] is False
    assert document["download"]["url"] is None and document["download"]["sha256"] is None
    assert document["download"]["notarized"] is False
    assert call(port, "GET", URL)[0] == download_status


def test_missing_file_stays_not_published(tmp_path: Path) -> None:
    with world(directory_reply=CONFIG, helper_zip_path=str(tmp_path / NAME)) as (public, product, directory):
        _assert_not_published(public.server_port, download_status=404)
        assert product.requests == []


def test_a_directory_that_claims_a_download_is_not_trusted_over_a_missing_file(tmp_path: Path) -> None:
    claim = {**CONFIG, "download": {"available": True, "url": "/x.zip", "sha256": "0" * 64, "version": "1",
                                    "notarized": True}}
    with world(directory_reply=claim, helper_zip_path=str(tmp_path / NAME)) as (public, _, _):
        assert _config(public.server_port)["download"]["available"] is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_unreadable_file_stays_not_published(zip_path: Path) -> None:
    zip_path.chmod(0)
    with world(directory_reply=CONFIG, helper_zip_path=str(zip_path)) as (public, _, _):
        _assert_not_published(public.server_port, download_status=404)


def test_empty_file_symlink_and_directory_stay_not_published(tmp_path: Path, zip_path: Path) -> None:
    empty = tmp_path / "empty.zip"
    empty.write_bytes(b"")
    link = tmp_path / "link.zip"
    link.symlink_to(zip_path)
    folder = tmp_path / "dir.zip"
    folder.mkdir()
    for candidate in (empty, link, folder):
        with world(directory_reply=CONFIG, helper_zip_path=str(candidate)) as (public, _, _):
            assert _config(public.server_port)["download"]["available"] is False, candidate
            assert call(public.server_port, "GET", "/swarms/download/" + candidate.name)[0] == 404


def test_file_over_the_size_cap_stays_not_published(zip_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy, "_HELPER_ZIP_MAX_BYTES", len(PAYLOAD) - 1)
    with world(directory_reply=CONFIG, helper_zip_path=str(zip_path)) as (public, _, _):
        _assert_not_published(public.server_port, download_status=404)


def test_file_that_changes_after_startup_is_withdrawn_not_served_under_the_old_sha(zip_path: Path) -> None:
    with world(directory_reply=CONFIG, helper_zip_path=str(zip_path)) as (public, _, _):
        assert call(public.server_port, "GET", URL)[2] == PAYLOAD
        zip_path.write_bytes(b"something else entirely")
        status, _, body = call(public.server_port, "GET", URL)
        assert status == 503 and json.loads(body)["error"]["code"] == "download_changed"
        assert _config(public.server_port)["download"]["available"] is False


def test_file_deleted_after_startup_is_withdrawn(zip_path: Path) -> None:
    with world(directory_reply=CONFIG, helper_zip_path=str(zip_path)) as (public, _, _):
        zip_path.unlink()
        assert call(public.server_port, "GET", URL)[0] == 503
        assert _config(public.server_port)["download"]["available"] is False


def test_only_get_and_head_serve_it_and_nothing_else_is_forwarded(zip_path: Path) -> None:
    with world(directory_reply=CONFIG, helper_zip_path=str(zip_path)) as (public, product, directory):
        port = public.server_port
        assert call(port, "POST", URL, body=b"{}", headers={**IP, "Content-Type": "application/json"})[0] == 404
        assert call(port, "DELETE", URL)[0] == 404
        assert call(port, "PUT", URL)[0] == 403
        for path in ("/swarms/download/", "/swarms/download", "/swarms/download/other.zip",
                     "/swarms/download/../" + NAME, URL + "?x=1", URL + "/", "/swarms/download/%2e%2e/" + NAME):
            assert call(port, "GET", path)[0] == 404, path
        assert product.requests == [] and directory.requests == []


def test_a_proxy_without_the_directory_mount_cannot_be_given_a_zip(zip_path: Path) -> None:
    with pytest.raises(proxy.PublicOriginProxyError) as raised:
        proxy.ProxyConfig(listen_host="127.0.0.1", listen_port=4350, upstream_host="127.0.0.1", upstream_port=1,
                          public_host="mycelium.now", capability=CAPABILITY, helper_zip_path=str(zip_path))
    assert raised.value.code == "helper_zip_requires_directory"


@pytest.mark.parametrize("bad", ["relative/path.zip", "", "/tmp/not-a-zip.txt", "/tmp/we ird.zip", "/tmp/a" + "b" * 200 + ".zip"])
def test_bad_zip_paths_are_a_configuration_error(bad: str) -> None:
    with pytest.raises(proxy.PublicOriginProxyError) as raised:
        proxy.ProxyConfig(listen_host="127.0.0.1", listen_port=4350, upstream_host="127.0.0.1", upstream_port=1,
                          public_host="mycelium.now", capability=CAPABILITY, directory_host="127.0.0.1",
                          directory_port=2, helper_zip_path=bad)
    assert raised.value.code == "invalid_helper_zip_path"


def test_cli_flags(tmp_path: Path, zip_path: Path) -> None:
    capability = tmp_path / "cap"
    capability.write_text(CAPABILITY + "\n")
    capability.chmod(0o600)
    base = ["--upstream", "http://127.0.0.1:4351", "--trusted-proxy-capability-file", str(capability),
            "--directory-upstream", "http://127.0.0.1:4360"]
    config = proxy.build_config_from_args(base + ["--helper-zip", str(zip_path), "--helper-version", "1.2.3"])
    assert (config.helper_zip_path, config.helper_version) == (str(zip_path), "1.2.3")
    assert config.redirect_root_to_swarms is True
    assert proxy.build_config_from_args(base).helper_zip_path is None
    assert proxy.build_config_from_args(base + ["--no-redirect-root-to-swarms"]).redirect_root_to_swarms is False


# -- (2) the '/' redirect ------------------------------------------------------------------------------

def test_root_redirects_to_swarms_by_default_when_the_mount_is_on() -> None:
    with world() as (public, product, directory):
        for method in ("GET", "HEAD"):
            status, headers, body = call(public.server_port, method, "/")
            assert (status, headers["location"], body) == (302, "/swarms/", b"")
            assert headers["cache-control"] == "no-store"
        assert call(public.server_port, "GET", "/?utm=x")[1]["location"] == "/swarms/"
        assert product.requests == [] and directory.requests == []


def test_everything_but_root_reaches_the_product_bundle_and_api_only_with_product_routes_enabled() -> None:
    with world(enable_product_routes=True) as (public, product, _):
        for path in ("/index.html", "/assets/app.js", "/swarmsfoo", "/api/v1/bootstrap"):
            assert call(public.server_port, "GET", path)[0] == 200, path
        assert [r["path"] for r in product.requests] == ["/index.html", "/assets/app.js", "/swarmsfoo", "/api/v1/bootstrap"]
        assert call(public.server_port, "POST", "/")[0] == 404 and call(public.server_port, "PUT", "/")[0] == 403


def test_the_flag_turns_the_redirect_off() -> None:
    with world(redirect_root_to_swarms=False, enable_product_routes=True) as (public, product, _):
        assert call(public.server_port, "GET", "/")[0] == 200
        assert [r["path"] for r in product.requests] == ["/"]
    with world(redirect_root_to_swarms=False) as (public, product, _):  # product routes closed: nothing at the root
        assert call(public.server_port, "GET", "/")[0] == 404
        assert product.requests == []


def test_without_the_directory_mount_root_is_the_product_even_with_the_default_flag() -> None:
    with world(mount=False) as (public, product, _):
        assert call(public.server_port, "GET", "/")[0] == 200
        assert [r["path"] for r in product.requests] == ["/"]
