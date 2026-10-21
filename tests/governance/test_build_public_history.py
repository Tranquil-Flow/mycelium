# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for scripts/release/build_public_history.py: the --co-author option."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release" / "build_public_history.py"
CO_AUTHOR = "Astra.K <145781196+Astra-K@users." + "noreply.github.com>"
TRAILER = "Co-authored-by: " + CO_AUTHOR
ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=ENV)
    return done.stdout.strip()


@pytest.fixture()
def fixture(tmp_path: Path) -> dict:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    origin = _git(repo, "rev-parse", "HEAD")
    (repo / "a").mkdir()
    (repo / "a" / "one.txt").write_text("one\n")
    (repo / "b.txt").write_text("two\n")
    (repo / "private.txt").write_text("private\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "source")
    source = _git(repo, "rev-parse", "HEAD")
    features = tmp_path / "features.json"
    features.write_text(
        json.dumps(
            {
                "schema": "mycelium.public-features.v1",
                "features": [
                    {"id": "first", "title": "feat(a): add the first thing", "body": "Adds a.", "paths": ["a/**"]},
                    {"id": "second", "title": "feat(b): add the second thing", "body": "Adds b.", "paths": ["b.txt"]},
                ],
            }
        )
    )
    excludes = tmp_path / "excludes.txt"
    excludes.write_text("private.txt\n")
    return {"repo": repo, "origin": origin, "source": source, "features": features, "excludes": excludes}


def _build(fx: dict, branch: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(fx["repo"]),
            "--origin-main",
            fx["origin"],
            "--source",
            fx["source"],
            "--features",
            str(fx["features"]),
            "--excludes",
            str(fx["excludes"]),
            "--branch",
            branch,
            *extra,
        ],
        capture_output=True,
        text=True,
        env=ENV,
    )


def _messages(fx: dict, branch: str) -> list[str]:
    raw = _git(fx["repo"], "log", "--format=%B%x00", f"{fx['origin']}..{branch}")
    return [m.strip("\n") for m in raw.split("\x00") if m.strip()]


def test_co_author_adds_exactly_one_trailer_after_a_blank_line(fixture: dict) -> None:
    done = _build(fixture, "pub", "--co-author", CO_AUTHOR)
    assert done.returncode == 0, done.stderr
    messages = _messages(fixture, "pub")
    assert len(messages) == 2
    for message in messages:
        assert message.endswith("\n\n" + TRAILER)
        assert message.count("Co-authored-by:") == 1
        assert message.count(TRAILER) == 1
    assert json.loads(done.stdout)["co_author_trailer"] == TRAILER
    # the tree is still the source minus the excludes
    assert _git(fixture["repo"], "rev-parse", "pub^{tree}") != _git(fixture["repo"], "rev-parse", fixture["source"] + "^{tree}")
    assert "private.txt" not in _git(fixture["repo"], "ls-tree", "-r", "--name-only", "pub")


def test_without_the_option_there_is_no_trailer(fixture: dict) -> None:
    done = _build(fixture, "pub")
    assert done.returncode == 0, done.stderr
    assert all("Co-authored-by" not in m for m in _messages(fixture, "pub"))


@pytest.mark.parametrize(
    "value",
    [
        "no email",
        "Name <not-an-email>",
        "Name <a@b>\nSigned-off-by: X <x@y.z>",
        " Name <a@b.c>",
        "Claude <a@b.c>",
        "",
    ],
)
def test_malformed_co_author_is_refused_and_creates_no_branch(fixture: dict, value: str) -> None:
    done = _build(fixture, "pub", "--co-author", value)
    assert done.returncode != 0
    assert subprocess.run(
        ["git", "-C", str(fixture["repo"]), "rev-parse", "--verify", "--quiet", "refs/heads/pub"], env=ENV
    ).returncode != 0
