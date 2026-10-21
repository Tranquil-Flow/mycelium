# SPDX-License-Identifier: AGPL-3.0-only
"""Host-independent rendering of the POSIX service members.

``build_service_package`` renders launchd/systemd members, which always run on a
POSIX host.  The A13 Windows ``finalize`` step runs it on a Windows build host,
where judging ``/usr/bin/python3`` with ``ntpath`` refused every package
(``service_executable_invalid``).  The members' paths are now judged as POSIX
paths on every host; on POSIX this is exactly the old rule.
"""

from __future__ import annotations

import os

import pytest

import mycelium_service_package as sp


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("/usr/bin/python3", True),
        ("/var/lib/mycelium", True),
        ("/", True),
        ("/a/b/../c", False),
        # Like the POSIX host rule: the normalised spellings agree, ".." does not.
        ("/a//b", True),
        ("/a/b/", True),
        ("/a/./b", True),
        ("relative", False),
        ("C:\\Python\\python.exe", False),
        ("", False),
    ],
)
def test_the_posix_member_rule_is_host_independent(value: str, accepted: bool) -> None:
    def outcome(check):
        try:
            return check(value, "refused")
        except sp.ServicePackageError as exc:
            return str(exc)

    assert outcome(sp._posix_absolute) == (value if accepted else "refused")
    if os.name != "nt":  # on a POSIX host it is exactly the host rule
        assert outcome(sp._posix_absolute) == outcome(sp._absolute)


def test_posix_members_render_on_any_host(tmp_path) -> None:
    config = {
        "protocol": sp.PROTOCOL,
        "package_version": "v0-1-0",
        "service_id": "mycelium-node",
        "role": "node",
        "argv": ["/usr/bin/python3", "-m", "mycelium_node.cli"],
        "working_directory": "/var/lib/mycelium",
        "state_directory": "/var/lib/mycelium/state",
        "log_directory": "/var/log/mycelium",
        "environment": {"MYCELIUM_ROLE": "node"},
        "health_url": "http://127.0.0.1:14001/health",
        "restart_limit": 5,
        "restart_window_seconds": 60,
        "restart_delay_seconds": 5,
        "stop_timeout_seconds": 30,
    }
    package = sp.build_service_package(config, package_root=tmp_path / "package")
    assert package["protocol"] == sp.PROTOCOL
    with pytest.raises(sp.ServicePackageError, match="service_executable_invalid"):
        sp.build_service_package(
            {**config, "argv": ["python3"]}, package_root=tmp_path / "package"
        )
