from mycelium_live.health import (
    LIVE_QUALIFICATION_REFRESH_AFTER_MS,
    RouteHealthSource,
)
from mycelium_live.route import FakeLiveRoute


class StubQualification:
    route_ready = True

    def __init__(self, issued_at_unix_ms: int = 1_000) -> None:
        self.issued_at_unix_ms = issued_at_unix_ms


def test_current_is_none_before_publish():
    route = FakeLiveRoute(scripted_tokens=(1,))
    route.open()
    source = RouteHealthSource(route=route)
    assert source.current() is None


def test_current_returns_published_record_while_route_alive():
    route = FakeLiveRoute(scripted_tokens=(1,))
    route.open()
    source = RouteHealthSource(route=route)
    record = StubQualification()
    source.publish(record)
    assert source.current() is record


def test_current_returns_none_once_route_dies():
    route = FakeLiveRoute(scripted_tokens=(1,))
    route.open()
    source = RouteHealthSource(route=route)
    source.publish(StubQualification())
    route.close()
    assert source.current() is None


def test_drop_is_permanent():
    route = FakeLiveRoute(scripted_tokens=(1,))
    route.open()
    source = RouteHealthSource(route=route)
    source.publish(StubQualification())
    source.drop()
    assert source.current() is None
    source.publish(StubQualification())
    assert source.current() is None


def test_current_renews_qualification_before_browser_freshness_expires():
    route = FakeLiveRoute(scripted_tokens=(1,))
    route.open()
    original = StubQualification()
    renewed = StubQualification(1_000 + LIVE_QUALIFICATION_REFRESH_AFTER_MS)
    refreshes = []
    source = RouteHealthSource(
        route=route,
        refresh=lambda: refreshes.append("refresh") or renewed,
        clock_unix_ms=lambda: renewed.issued_at_unix_ms,
    )
    source.publish(original)

    assert source.current() is renewed
    assert refreshes == ["refresh"]


def test_current_never_renews_while_request_router_is_not_idle():
    route = FakeLiveRoute(scripted_tokens=(1,))
    route.open()
    original = StubQualification()
    refreshes = []
    source = RouteHealthSource(
        route=route,
        refresh=lambda: refreshes.append("refresh") or StubQualification(),
        refresh_allowed=lambda: False,
        clock_unix_ms=lambda: 1_000 + LIVE_QUALIFICATION_REFRESH_AFTER_MS,
    )
    source.publish(original)

    assert source.current() is original
    assert refreshes == []


def test_failed_renewal_preserves_retryable_previous_record():
    route = FakeLiveRoute(scripted_tokens=(1,))
    route.open()
    original = StubQualification()

    def fail_refresh():
        raise RuntimeError("renewal failed")

    source = RouteHealthSource(
        route=route,
        refresh=fail_refresh,
        clock_unix_ms=lambda: 1_000 + LIVE_QUALIFICATION_REFRESH_AFTER_MS,
    )
    source.publish(original)

    assert source.current() is original
