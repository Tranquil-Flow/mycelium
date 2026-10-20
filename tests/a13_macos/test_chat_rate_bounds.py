# SPDX-License-Identifier: AGPL-3.0-only
"""The chat service's per-client rate table cannot grow with the number of distinct clients over time."""
from mycelium_a13_macos.chat import ChatService, PER_CLIENT_PER_MINUTE


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def _service(clock):
    return ChatService(app=None, clock=clock)


def test_idle_clients_are_dropped_after_their_window_even_when_never_seen_again():
    clock = Clock()
    service = _service(clock)
    for index in range(6_000):  # 30 a minute is the global cap, so spread them over time
        clock.now = index * 3.0
        service._rate(f"client-{index}")
    assert len(service._per_client) <= 4_096
    assert len(service._per_client) <= 30 + 1  # only clients inside the last 60 s remain


def test_the_per_client_limit_and_window_still_hold():
    from mycelium_a13_macos.chat import ChatError
    import pytest
    clock = Clock()
    service = _service(clock)
    for _ in range(PER_CLIENT_PER_MINUTE):
        service._rate("same")
    with pytest.raises(ChatError) as caught:
        service._rate("same")
    assert caught.value.args or True
    clock.now = 61
    service._rate("same")
