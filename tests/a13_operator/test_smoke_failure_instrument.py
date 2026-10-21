# SPDX-License-Identifier: AGPL-3.0-only
"""The smoke-refusal path must capture the app's own admission status while the app is alive.

Round p81r1 spent a full round and left BOTH `roundp81r1-admission-status-{pre,post}-soak.json` missing:
the chain ties those two captures to the soak's boundaries, and the smoke gate ends the round before the
soak ever starts. The round that most needs the coordinator's own view therefore left none -- the p78r1
lesson repeating.

This is a structural test over the newest operator chain, in the same spirit as the soak's structural gate:
it reads the chain's embedded Python and asserts that
  1. the capture helper is DEFINED before the smoke gate (otherwise the refusal path cannot call it), and
  2. the smoke-refusal branch actually CALLS it before the branch ends the round, and
  3. the capture uses its own stage label rather than reusing a soak boundary's,
so a future round cannot silently go back to leaving no view of the app.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CHAIN_DIR = Path(__file__).resolve().parents[2] / "scripts" / "a13_operator"
FAILURE_BRANCH = "if not smoke.get('succeeded'):"
DEFINITION = "def _capture_admission_status("
SOAK_STAGES = ("pre-soak", "post-soak")


def _newest_chain() -> Path:
    candidates = []
    for path in CHAIN_DIR.glob("chain-preview*.sh"):
        match = re.search(r"chain-preview(\d+)e?\.sh$", path.name)
        if match:
            candidates.append((int(match.group(1)), path.name, path))
    if not candidates:
        pytest.skip("the operator chain launchers are not part of the published tree")
    candidates.sort()
    return candidates[-1][2]


def _embedded_python(path: Path) -> list[str]:
    lines = path.read_text("utf-8").splitlines()
    # A heredoc is *invoked* by a line like `... -B - <<'PY'`; the chain also mentions the marker inside a
    # comment explaining shell quoting, so filter out comment lines explicitly.
    starts = [
        i for i, line in enumerate(lines)
        if "<<'PY'" in line and not line.lstrip().startswith("#")
    ]
    assert len(starts) == 1, f"expected exactly one embedded python block in {path.name}, found {starts}"
    start = starts[0]
    for index in range(start + 1, len(lines)):
        if lines[index].strip() == "PY":
            return lines[start:index]
    raise AssertionError(f"embedded python block in {path.name} is unterminated")


def test_the_capture_helper_is_defined_before_the_smoke_gate():
    chain = _newest_chain()
    body = _embedded_python(chain)
    definition = next(
        i for i, line in enumerate(body) if line.startswith(DEFINITION)
    )
    smoke = next(i for i, line in enumerate(body) if line.startswith(FAILURE_BRANCH))
    assert definition < smoke, (
        f"{chain.name}: {DEFINITION} is defined AFTER the smoke gate, so the smoke-refusal path "
        "cannot capture the app's own view at all (this is the p81r1 defect)"
    )


def _failure_branch_body(body: list[str]) -> list[str]:
    """The indented body of the smoke-refusal branch -- and nothing after it.

    A fixed window is wrong: once the instrument block sits above the smoke (the fix), the legitimate
    `pre-soak` call follows the branch and would be misread as a refusal-path capture.
    """
    start = next(i for i, line in enumerate(body) if line.startswith(FAILURE_BRANCH))
    collected = []
    for line in body[start + 1:]:
        stripped = line.strip()
        if not stripped:
            collected.append(line)
            continue
        if not line[:1].isspace():
            break
        collected.append(line)
    return collected


def test_the_smoke_refusal_branch_captures_the_admission_status():
    chain = _newest_chain()
    body = _embedded_python(chain)
    calls = [
        line.strip() for line in _failure_branch_body(body)
        if "_capture_admission_status(" in line and "def " not in line
    ]
    assert calls, (
        f"{chain.name}: the smoke-refusal branch ends the round without capturing the admission status, "
        "so a refused round leaves no view of the app (p81r1 left both boundary captures missing)"
    )
    assert any("smoke" in line for line in calls), (
        f"{chain.name}: the refusal-path capture must use its own stage label, not a soak boundary's: {calls}"
    )


def test_the_refusal_capture_does_not_reuse_a_soak_boundary_label():
    chain = _newest_chain()
    body = _embedded_python(chain)
    calls = [
        line.strip() for line in _failure_branch_body(body)
        if "_capture_admission_status(" in line and "def " not in line
    ]
    for call in calls:
        for stage in SOAK_STAGES:
            assert f"'{stage}'" not in call, (
                f"{chain.name}: the refusal-path capture overwrites the {stage} boundary file: {call}"
            )
