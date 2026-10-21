# SPDX-License-Identifier: AGPL-3.0-only
"""`mycelium_demo doctor --lan`: plain-English LAN readiness, probes injected."""
from __future__ import annotations

import json

import pytest

from mycelium_demo import cli
from mycelium_platform_neutral import lan_doctor
from mycelium_platform_neutral.lan_doctor import render_report, run_lan_doctor

GOOD_ENV = {"MYCELIUM_NETWORK_MODE": "lan"}


def good_address() -> str:
    return "192.168.7.20"


def all_free(port: int) -> bool:
    return True


def run(environ=None, **overrides):
    arguments = dict(
        environ=GOOD_ENV if environ is None else environ,
        lan_address=good_address,
        port_available=all_free,
    )
    arguments.update(overrides)
    return run_lan_doctor(**arguments)


def check(report, name):
    matches = [item for item in report["checks"] if item["name"] == name]
    assert len(matches) == 1, [item["name"] for item in report["checks"]]
    return matches[0]


def test_a_ready_lan_host_passes_every_check() -> None:
    report = run()
    assert report["ok"] is True
    assert [item["name"] for item in report["checks"]] == [
        "network_mode", "private_address", "ports", "hosted_endpoints", "offline",
    ]
    assert all(item["ok"] for item in report["checks"])
    assert all(item["fix"] is None for item in report["checks"])


def test_default_internet_mode_fails_with_the_one_line_fix() -> None:
    report = run(environ={})
    item = check(report, "network_mode")
    assert report["ok"] is False and item["ok"] is False
    assert "MYCELIUM_NETWORK_MODE=lan" in item["fix"] and "--network-mode lan" in item["fix"]


def test_cli_mode_beats_the_environment() -> None:
    report = run(environ={}, network_mode="lan")
    assert check(report, "network_mode")["ok"] is True


def test_local_mode_is_not_enough_for_other_devices() -> None:
    item = check(run(environ={"MYCELIUM_NETWORK_MODE": "local"}), "network_mode")
    assert item["ok"] is False and "loopback" in item["detail"]


def test_unknown_mode_is_reported_not_raised() -> None:
    report = run(environ={"MYCELIUM_NETWORK_MODE": "wan"})
    item = check(report, "network_mode")
    assert item["ok"] is False and "internet, lan or local" in item["fix"]


@pytest.mark.parametrize("address", [None, "8.8.8.8", "127.0.0.1", "169.254.1.2"])
def test_missing_or_non_private_address_fails_with_network_advice(address) -> None:
    item = check(run(lan_address=lambda: address), "private_address")
    assert item["ok"] is False
    assert "Wi-Fi" in item["fix"] and "--bind" in item["fix"]


def test_private_address_is_named_when_found() -> None:
    assert "192.168.7.20" in check(run(), "private_address")["detail"]


def test_ports_are_probed_through_the_loopback_probe_only() -> None:
    seen: list[int] = []

    def probe(port: int) -> bool:
        seen.append(port)
        return True

    run(port_available=probe, ports=(8765, 8766, 4360))
    assert seen == [8765, 8766, 4360]


def test_default_ports_are_the_seed_and_the_node_advertise_port() -> None:
    seen: list[int] = []
    run(port_available=lambda port: seen.append(port) or True)
    assert seen == [8765, 8766]


def test_a_busy_port_is_named_with_a_fix() -> None:
    item = check(run(port_available=lambda port: port != 8766), "ports")
    assert item["ok"] is False
    assert "8766" in item["detail"] and "8765" not in item["detail"]
    assert "--advertise-port" in item["fix"] and "--port" in item["fix"]


@pytest.mark.parametrize("name,value,reason", [
    ("MYCELIUM_PUBLIC_ORIGIN", "https://quiet-river.trycloudflare.com", "Cloudflare tunnel"),
    ("MYCELIUM_DIRECTORY_ORIGIN", "https://mycelium.now", "hosted Mycelium"),
    ("IROH_RELAY_URL", "https://use1-1.relay.n0.iroh.link", "n0"),
    ("HF_ENDPOINT", "https://huggingface.co", "public HTTPS"),
    ("MYCELIUM_ENTRY_ORIGIN", "https://some-host.example.org/entry", "public HTTPS"),
])
def test_hosted_endpoints_in_the_environment_fail(name, value, reason) -> None:
    item = check(run(environ={**GOOD_ENV, name: value}), "hosted_endpoints")
    assert item["ok"] is False
    assert name in item["detail"] and reason in item["detail"]
    assert name in item["fix"]


@pytest.mark.parametrize("flag", ["--force-relay", "--sidecar-force-relay", "--relay-url=https://r.example"])
def test_relay_flags_fail_in_lan_mode(flag) -> None:
    item = check(run(flags=(flag,)), "hosted_endpoints")
    assert item["ok"] is False and flag.split("=")[0] in item["detail"]


def test_a_public_https_directory_flag_fails() -> None:
    item = check(run(flags=("--directory-origin", "https://dir.example.org")), "hosted_endpoints")
    assert item["ok"] is False and "--directory-origin" in item["detail"]


def test_private_and_loopback_origins_are_not_hosted() -> None:
    environ = {
        **GOOD_ENV,
        "MYCELIUM_DIRECTORY_ORIGIN": "http://192.168.7.20:4360",
        "MYCELIUM_ENTRY_ORIGIN": "http://127.0.0.1:4350",
        "HF_ENDPOINT": "http://10.0.0.5:8080",
    }
    assert check(run(environ=environ), "hosted_endpoints")["ok"] is True


def test_report_never_echoes_secrets_or_url_credentials() -> None:
    environ = {
        **GOOD_ENV,
        "MYCELIUM_DEMO_REQUEST_BEARER_TOKEN": "tok-secret-123",
        "MYCELIUM_A10_FLEET_GRANT": "grant-secret-456",
        "MYCELIUM_PUBLIC_ORIGIN": "https://user:pw-secret-789@tunnel.example.com/path?k=q-secret",
    }
    report = run(environ=environ)
    text = json.dumps(report) + render_report(report)
    for secret in ("tok-secret-123", "grant-secret-456", "pw-secret-789", "q-secret"):
        assert secret not in text


def test_lan_mode_implies_offline_and_passes() -> None:
    assert check(run(), "offline")["ok"] is True


def test_explicit_offline_off_in_lan_mode_fails() -> None:
    item = check(run(environ={**GOOD_ENV, "MYCELIUM_OFFLINE": "0"}), "offline")
    assert item["ok"] is False
    assert "Hugging Face" in item["detail"] and "MYCELIUM_OFFLINE" in item["fix"]


def test_cli_offline_value_is_honoured_and_unknown_env_is_reported() -> None:
    assert check(run(environ={**GOOD_ENV, "MYCELIUM_OFFLINE": "0"}, offline=True), "offline")["ok"]
    item = check(run(environ={**GOOD_ENV, "MYCELIUM_OFFLINE": "maybe"}), "offline")
    assert item["ok"] is False and "1 or 0" in item["fix"]


def test_render_gives_one_plain_english_fix_per_failing_item() -> None:
    report = run(environ={"MYCELIUM_PUBLIC_ORIGIN": "https://a.trycloudflare.com"},
                 lan_address=lambda: None, port_available=lambda port: False)
    text = render_report(report)
    failing = [item for item in report["checks"] if not item["ok"]]
    assert len(failing) == 5
    assert text.count("Fix:") == len(failing)
    assert "NOT READY" in text
    assert "READY" in render_report(run()) and "NOT READY" not in render_report(run())


def test_probes_default_to_the_real_ones() -> None:
    assert lan_doctor.DEFAULT_PORTS == (8765, 8766)


# -- the CLI ---------------------------------------------------------------------

def cli_run(argv, capsys, environ=None, **overrides):
    code = cli.main(
        argv,
        environ=GOOD_ENV if environ is None else environ,
        lan_address=good_address,
        port_available=all_free,
        **overrides,
    )
    return code, capsys.readouterr().out


def test_cli_doctor_lan_needs_no_state_dir_and_prints_ready(capsys) -> None:
    code, out = cli_run(["doctor", "--lan"], capsys)
    assert code == 0 and "READY" in out and "NOT READY" not in out


def test_cli_doctor_lan_json_and_exit_code_on_failure(capsys) -> None:
    code, out = cli_run(["doctor", "--lan", "--json"], capsys, environ={})
    report = json.loads(out)
    assert code == 1 and report["ok"] is False


def test_cli_doctor_lan_accepts_mode_offline_and_port_flags(capsys) -> None:
    seen: list[int] = []
    code = cli.main(
        ["doctor", "--lan", "--network-mode", "lan", "--no-offline", "--port", "9000", "--json"],
        environ={},
        lan_address=good_address,
        port_available=lambda port: seen.append(port) or True,
    )
    report = json.loads(capsys.readouterr().out)
    assert seen == [9000] and code == 1
    assert check(report, "offline")["ok"] is False
    assert check(report, "network_mode")["ok"] is True


def test_cli_doctor_without_lan_still_requires_state_dir(capsys) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(["doctor"])
    assert caught.value.code == 2
    assert "--state-dir" in capsys.readouterr().err
