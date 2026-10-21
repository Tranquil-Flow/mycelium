# SPDX-License-Identifier: AGPL-3.0-only
"""Private (RFC 1918) directory/entry origins are accepted only when opted in (lan mode)."""
from __future__ import annotations

import json

import pytest

from mycelium_a13_macos.swarm_publish import DirectoryClient, SwarmPublishError, SwarmPublisher
from mycelium_platform_neutral.network_mode import NetworkMode
from mycelium_swarm_directory import docs, server
from mycelium_swarm_directory.crypto import Ed25519Signer, verify
from mycelium_swarm_directory.directory import Directory
from mycelium_swarm_directory.store import MemoryStore
from mycelium_swarm_directory.wire import WireError

PRIVATE_OK = [
    "http://192.168.1.5:4360",
    "https://192.168.1.5:4360",
    "http://10.0.0.5:4350",
    "http://172.16.3.4:4350",
    "http://172.31.255.254",
    "https://10.1.2.3",
]
PRIVATE_BAD = [
    "http://8.8.8.8:4360",            # public literal
    "http://172.32.0.1:4360",         # just outside 172.16/12
    "http://169.254.1.1:4360",        # link-local
    "http://127.0.0.1:4360",          # loopback stays its own opt-in
    "http://0.0.0.0:4360",
    "http://192.168.1.5:4360/",       # trailing path
    "http://192.168.1.5:4360/x",
    "http://user@192.168.1.5:4360",
    "http://192.168.001.005:4360",    # non-canonical octets
    "http://192.168.1.5:0",
    "http://192.168.1.5:99999",
    "ftp://192.168.1.5:4360",
    "http://printer.local:4360",      # names are not private literals
    "http://[fd00::1]:4360",
    "HTTP://192.168.1.5:4360",
    "http://192.168.1.5:4360 ",
]


@pytest.mark.parametrize("value", PRIVATE_OK + PRIVATE_BAD)
def test_default_policy_rejects_every_private_origin(value: str) -> None:
    if value in {"http://127.0.0.1:4360"}:
        pytest.skip("loopback has its own flag")
    with pytest.raises(WireError):
        docs.origin(value, allow_loopback=False)
    with pytest.raises(WireError):
        docs.origin(value, allow_loopback=True)  # loopback opt-in must not admit LAN


@pytest.mark.parametrize("value", PRIVATE_OK)
def test_opt_in_accepts_private_origins(value: str) -> None:
    assert docs.origin(value, allow_loopback=False, allow_private=True) == value


@pytest.mark.parametrize("value", PRIVATE_BAD)
def test_opt_in_still_rejects_everything_else(value: str) -> None:
    allow_loopback = value == "http://127.0.0.1:4360"
    if allow_loopback:
        assert docs.origin(value, allow_loopback=True, allow_private=True) == value
        with pytest.raises(WireError):
            docs.origin(value, allow_loopback=False, allow_private=True)
        return
    with pytest.raises(WireError):
        docs.origin(value, allow_loopback=False, allow_private=True)


def test_opt_in_leaves_the_public_https_rule_intact() -> None:
    assert docs.origin("https://quiet-river.trycloudflare.com", allow_loopback=False, allow_private=True)
    with pytest.raises(WireError):
        docs.origin("http://evil.example", allow_loopback=False, allow_private=True)


def signed_record(owner, origin):
    return docs.swarm_record(
        owner, swarm_id="sw-aaaaaaaaaaaaaaaaaaaa", seq=1, name="Home", model="Qwen/Qwen3-0.6B",
        description="", access_mode="request", open_quota=None,
        entry={"key": owner.public_key, "origin": origin},
    )


def test_record_entry_origin_and_swarm_link_follow_the_policy() -> None:
    owner = Ed25519Signer.generate()
    record = signed_record(owner, "http://192.168.1.5:4350")
    with pytest.raises(WireError):
        docs.check_record(record, verify)
    assert docs.check_record(record, verify, allow_private=True)["entry"]["origin"] == "http://192.168.1.5:4350"
    link = docs.swarm_link("http://192.168.1.9:4360", "sw-aaaaaaaaaaaaaaaaaaaa", owner.public_key)
    with pytest.raises(WireError):
        docs.parse_swarm_link(link)
    assert docs.parse_swarm_link(link, allow_private=True)["directory"] == "http://192.168.1.9:4360"


def post(directory, record):
    return directory.handle("POST", "/records", json.dumps(record).encode(), "ip:1")


def test_directory_accepts_private_entries_only_when_constructed_for_lan() -> None:
    owner = Ed25519Signer.generate()
    record = signed_record(owner, "http://192.168.1.5:4350")
    assert post(Directory(MemoryStore(), verify), record) == (400, {"error": "entry_origin_invalid"})
    lan = Directory(MemoryStore(), verify, allow_private_entries=True)
    assert post(lan, record)[0] == 200
    assert post(lan, signed_record(owner, "http://8.8.8.8:4350"))[1] == {"error": "entry_origin_invalid"}


def test_directory_cli_enables_private_entries_from_the_network_mode(tmp_path) -> None:
    db = str(tmp_path / 'directory.sqlite3')
    parse = server.build_parser().parse_args
    assert server.build_directory(parse(["--db", db]), environ={}).allow_private is False
    assert server.build_directory(parse(["--db", db, "--network-mode", "lan"]), environ={}).allow_private is True
    assert server.build_directory(parse(["--db", db]), environ={"MYCELIUM_NETWORK_MODE": "lan"}).allow_private
    assert server.build_directory(
        parse(["--db", db, "--network-mode", "local"]), environ={}
    ).allow_private is False
    with pytest.raises(SystemExit):
        parse(["--db", db, "--network-mode", "wan"])


def test_directory_client_rejects_lan_origin_unless_opted_in() -> None:
    with pytest.raises(SwarmPublishError) as caught:
        DirectoryClient("http://192.168.1.9:4360")
    assert caught.value.code == "swarm_directory_origin_invalid"
    assert DirectoryClient("http://192.168.1.9:4360", allow_private=True).origin == "http://192.168.1.9:4360"
    assert DirectoryClient("https://swarms.example.org").origin == "https://swarms.example.org"
    with pytest.raises(SwarmPublishError):
        DirectoryClient("http://8.8.8.8:4360", allow_private=True)


class _Run:
    def status(self):
        return {"state": "serving", "route_ready": True, "registry_origin": "http://127.0.0.1:5",
                "participants": [{"node_id": "a"}, {"node_id": "b"}]}


class _App:
    installation_role = "owner"

    def __init__(self, root):
        self.root = root
        self.owner_run = _Run()

    def run_model_contract(self):
        return type("M", (), {"model_id": "Qwen/Qwen3-0.6B"})()


class _InProcess:
    directory = None

    def __init__(self, origin):
        assert origin == "http://192.168.1.9:4360"

    def get(self, path):
        status, value = self.directory.handle("GET", path, b"", "ip:1")
        assert status == 200
        return value

    def post(self, path, document):
        status, value = self.directory.handle("POST", path, json.dumps(document).encode(), "ip:1")
        if status != 200:
            raise SwarmPublishError("swarm_directory_" + value["error"])
        return value


def _publish(tmp_path, mode):
    _InProcess.directory = Directory(MemoryStore(), verify, allow_private_entries=True)
    pub = SwarmPublisher(
        _App(tmp_path), origin=lambda: "http://192.168.1.5:4350", client_factory=_InProcess,
        chat=lambda: None, network_mode=mode,
    )
    return pub.publish({"directory": "http://192.168.1.9:4360", "name": "Home", "access_mode": "request"})


def test_publisher_lan_mode_publishes_a_private_entry(tmp_path) -> None:
    state = _publish(tmp_path, NetworkMode.LAN)
    row, = _InProcess.directory.list_swarms()
    assert state["enabled"] and row["entry_origin"] == "http://192.168.1.5:4350"


@pytest.mark.parametrize("mode", [NetworkMode.INTERNET, NetworkMode.LOCAL])
def test_publisher_default_and_local_modes_refuse_a_private_entry(tmp_path, mode) -> None:
    with pytest.raises(SwarmPublishError) as caught:
        _publish(tmp_path, mode)
    assert caught.value.code == "swarm_entry_origin_invalid"


def test_publisher_default_mode_is_internet(tmp_path) -> None:
    pub = SwarmPublisher(_App(tmp_path), origin=lambda: None, client_factory=_InProcess, chat=lambda: None)
    assert pub.network_mode is NetworkMode.INTERNET
