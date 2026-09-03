from __future__ import annotations

import argparse

from scripts import run_a5_replica_loss_gate as gate


def test_resolve_sidecar_pid_uses_configured_socket(monkeypatch):
    observed: list[str] = []

    def fake_ssh(_args, remote: str, *, timeout: float = 30.0) -> str:
        observed.append(remote)
        return "14557"

    monkeypatch.setattr(gate, "_ssh", fake_ssh)
    args = argparse.Namespace(
        sidecar_socket_path="/Users/evinova/qualified/socket/i.sock"
    )

    assert gate._resolve_sidecar_pid(args) == 14557
    assert observed == ["lsof -t /Users/evinova/qualified/socket/i.sock"]
