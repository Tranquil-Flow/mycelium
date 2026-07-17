from __future__ import annotations

import importlib.util

import pytest

from mycelium_gossip.zenoh_transport import (
    ZenohTransport,
    ZenohTransportConfig,
    ZenohUnavailable,
    parse_liveness_key,
)


def test_module_and_config_work_without_optional_zenoh_dependency() -> None:
    config = ZenohTransportConfig()
    assert config.listen_endpoints == ("tcp/127.0.0.1:0",)
    assert config.multicast_enabled is False
    if importlib.util.find_spec("zenoh") is None:
        transport = ZenohTransport("swarm-a", "node-a", config=config)
        with pytest.raises(ZenohUnavailable, match="eclipse-zenoh"):
            transport.start()


def test_wildcard_listener_requires_explicit_opt_in() -> None:
    with pytest.raises(ValueError, match="wildcard"):
        ZenohTransportConfig(listen_endpoints=("tcp/0.0.0.0:0",))

    config = ZenohTransportConfig(
        listen_endpoints=("tcp/0.0.0.0:0",),
        allow_wildcard_listen=True,
    )
    assert config.listen_endpoints == ("tcp/0.0.0.0:0",)


def test_peer_config_is_explicit_and_lease_tuned() -> None:
    config = ZenohTransportConfig(
        listen_endpoints=("tcp/127.0.0.1:0",),
        connect_endpoints=("tcp/127.0.0.1:17447",),
        multicast_enabled=True,
        multicast_address="224.0.0.225:17446",
        multicast_interface="lo0",
        lease_ms=4_000,
        keep_alive=4,
    )

    value = config.to_dict()

    assert value["mode"] == "peer"
    assert value["listen"]["endpoints"] == ["tcp/127.0.0.1:0"]
    assert value["connect"]["endpoints"] == ["tcp/127.0.0.1:17447"]
    assert value["scouting"]["multicast"]["enabled"] is True
    assert value["scouting"]["multicast"]["interface"] == "lo0"
    assert value["transport"]["link"]["tx"] == {"lease": 4_000, "keep_alive": 4}


def test_liveness_key_round_trip_and_malformed_rejection() -> None:
    key = "mycelium/swarm-a/liveness/node-a/3/boot-a"
    assert parse_liveness_key(key, expected_swarm="swarm-a") == ("node-a", 3, "boot-a")

    with pytest.raises(ValueError, match="liveness key"):
        parse_liveness_key("mycelium/swarm-a/liveness/node-a/not-int/boot-a", expected_swarm="swarm-a")
    with pytest.raises(ValueError, match="swarm"):
        parse_liveness_key(key, expected_swarm="swarm-b")


def test_config_rejects_invalid_transport_limits() -> None:
    with pytest.raises(ValueError, match="lease"):
        ZenohTransportConfig(lease_ms=0)
    with pytest.raises(ValueError, match="query timeout"):
        ZenohTransportConfig(query_timeout_seconds=0)
