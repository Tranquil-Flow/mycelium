# SPDX-License-Identifier: AGPL-3.0-only
"""Shared doubles for the demo-watchdog suites.

Everything here is in-process and scripted: no network, no hardware, no child
process, no listen socket.  The watchdog's whole contact surface with the world
is three probes, one observer runner and the recovery driver's command runner,
so those five objects are the only things a test has to replace.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence

import pytest

from mycelium_demo_watchdog import probes as probes_module


class Completes:
    """A ``subprocess.run``-shaped callable driven by a scripted plan."""

    def __init__(self, plan: Mapping[str, object] | Callable[[Sequence[str]], object] | None = None):
        self.calls: list[tuple[str, ...]] = []
        self._callable = plan if callable(plan) else None
        self.plan = {} if self._callable is not None else dict(plan or {})

    def __call__(self, argv, **_kwargs):
        argv = tuple(str(item) for item in argv)
        self.calls.append(argv)
        scripted = self._callable(argv) if self._callable else self._lookup(argv)
        if isinstance(scripted, BaseException):
            raise scripted
        if scripted is None:
            return subprocess.CompletedProcess(argv, 0, "", "")
        code, stdout, stderr = scripted
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    def _lookup(self, argv: tuple[str, ...]):
        for key, value in self.plan.items():
            if key in " ".join(argv):
                return value
        return None

    @property
    def key_calls(self) -> list[str]:
        return [argv[-1] for argv in self.calls]


@dataclass
class ScriptedProbe:
    """A probe double returning a fixed, or per-call, ``ProbeResult``."""

    results: list[probes_module.ProbeResult] = field(default_factory=list)
    default: probes_module.ProbeResult | None = None
    raises: BaseException | None = None
    calls: int = 0

    def run(self) -> probes_module.ProbeResult:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        if self.results:
            return self.results.pop(0)
        if self.default is not None:
            return self.default
        raise AssertionError("ScriptedProbe called without a scripted result")


class ScriptedObserver:
    def __init__(self, outcomes: Iterable[bool] = (), default: bool = True):
        self.outcomes = list(outcomes)
        self.default = default
        self.calls = 0

    def run(self) -> bool:
        self.calls += 1
        if self.outcomes:
            return self.outcomes.pop(0)
        return self.default


def ok(detail: Mapping[str, object] | None = None) -> probes_module.ProbeResult:
    return probes_module.ProbeResult(True, dict(detail or {}), None)


def bad(error: str = "probe_failed") -> probes_module.ProbeResult:
    return probes_module.ProbeResult(False, {}, error)


def unknown(error: str = "probe_unknown") -> probes_module.ProbeResult:
    return probes_module.ProbeResult(None, {}, error)


@pytest.fixture
def completer():
    return Completes()
