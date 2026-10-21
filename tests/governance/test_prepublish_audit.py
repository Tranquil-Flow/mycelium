# SPDX-License-Identifier: AGPL-3.0-only
"""RED-first tests for scripts/prepublish_audit.py.

Every secret-looking fixture is assembled at run time so this file never
contains a literal that a secret scanner (or the audit itself) would flag.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "prepublish_audit.py"

OWNER = "tranquil_flow@protonmail.com"
OR_LATER = "AGPL-3.0-" + "or-later"
HEADER = "# SPDX-License-Identifier: AGPL-3.0-only\n"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Tranquil-Flow",
    "GIT_AUTHOR_EMAIL": OWNER,
    "GIT_COMMITTER_NAME": "Tranquil-Flow",
    "GIT_COMMITTER_EMAIL": OWNER,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    full = {**os.environ, **GIT_ENV, **(env or {})}
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=full,
    )
    return done.stdout.strip()


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        _git(path, "init", "-q", "-b", "main")
        self.write("LICENSE", "GNU AFFERO GENERAL PUBLIC LICENSE\nVersion 3, 19 November 2007\n")
        self.write("mod.py", HEADER + "x = 1\n")
        self.commit("base")

    def write(self, rel: str, text: str) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message: str, env: dict[str, str] | None = None) -> str:
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-q", "--allow-empty", "-m", message, env=env)
        return _git(self.path, "rev-parse", "HEAD")

    def audit(self, *extra: str, rev: str | None = "--all-history") -> tuple[int, dict, str]:
        out = self.path.parent / f"{self.path.name}-report.json"
        args = [sys.executable, str(SCRIPT), "--root", str(self.path), "--output", str(out)]
        if rev == "--all-history":
            args += ["--rev-range", "HEAD"]
        elif rev is not None:
            args += ["--rev-range", rev]
        args += list(extra)
        done = subprocess.run(args, capture_output=True, text=True)
        report = json.loads(out.read_text()) if out.exists() else {}
        return done.returncode, report, done.stdout + done.stderr


@pytest.fixture()
def repo(tmp_path: Path) -> Repo:
    path = tmp_path / "r"
    path.mkdir()
    return Repo(path)


def _cats(report: dict) -> set[str]:
    return {f["category"] for f in report["findings"]}


def test_script_exists() -> None:
    assert SCRIPT.is_file()


def test_clean_repo_passes(repo: Repo) -> None:
    rc, report, _ = repo.audit()
    assert rc == 0, report
    assert report["findings"] == []
    assert report["summary"]["commits_scanned"] >= 1


SECRET_CASES = {
    "private_key": lambda: "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nabc\n-----END " + "OPENSSH PRIVATE KEY-----\n",
    "private_key_age": lambda: "AGE-SECRET-" + "KEY-1" + "Q" * 58 + "\n",
    "api_token_github": lambda: "token_value = '" + "ghp" + "_" + "A1b2C3d4" * 5 + "'\n",
    "api_token_github_pat": lambda: "x = '" + "github_pat" + "_" + "11ABCDEFG0" + "a1B2c3D4e5F6g7H8i9J0k1" + "'\n",
    "api_token_hf": lambda: "HF = '" + "hf" + "_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3f" + "'\n",
    "api_token_aws": lambda: "id = '" + "AKIA" + "ABCDEFGHIJKLMNOP" + "'\n",
    "generic_secret": lambda: "api_secret = \"" + "Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0Lm" + "\"\n",
    "cloudflared_credential": lambda: '{"AccountTag": "abc", "Tunnel' + 'Secret": "c2VjcmV0c2VjcmV0c2VjcmV0", "TunnelID": "x"}\n',
    "tailscale_ip": lambda: "peer = '100." + "101.20.30" + "'\n",
    "private_hostname": lambda: "host = 'mac-" + "node.local'\n",
    "home_path": lambda: "p = '/Users/" + "someperson/work'\n",
    "email": lambda: "contact = 'someone" + "@gmail.com'\n",
    "spdx_or_later": lambda: "# SPDX-License-Identifier: " + OR_LATER + "\n",
}


@pytest.mark.parametrize("category", sorted(SECRET_CASES))
def test_tree_finding_by_category(repo: Repo, category: str) -> None:
    body = SECRET_CASES[category]()
    expected = category.split("_github")[0].split("_hf")[0].split("_aws")[0]
    if category.startswith("api_token") or category.startswith("private_key"):
        expected = "api_token" if category.startswith("api_token") else "private_key"
    repo.write("notes/data.txt", body)
    repo.commit("add")
    rc, report, out = repo.audit()
    assert rc != 0
    assert expected in _cats(report), (expected, report["findings"])
    hit = next(f for f in report["findings"] if f["category"] == expected)
    assert hit["file"] == "notes/data.txt"
    assert hit["line"] >= 1
    assert hit["commit"] is None or len(hit["commit"]) == 40
    # never leak the matched value
    for needle in ("someperson", "hf_aB3", "AKIAABCDEF", "ghp_A1b2", "Zx9Qw8", "gmail.com", "OPENSSH"):
        assert needle not in json.dumps(report), needle
        assert needle not in out, needle


def test_secret_only_in_history_is_found_with_commit(repo: Repo) -> None:
    repo.write("leak.txt", SECRET_CASES["api_token_github"]())
    leaked = repo.commit("oops")
    (repo.path / "leak.txt").unlink()
    repo.commit("remove it")
    rc, report, _ = repo.audit()
    assert rc != 0
    hits = [f for f in report["findings"] if f["category"] == "api_token"]
    assert hits and hits[0]["commit"] == leaked
    assert hits[0]["scope"] == "history"
    assert hits[0]["file"] == "leak.txt"


def test_tree_only_mode_ignores_history(repo: Repo) -> None:
    repo.write("leak.txt", SECRET_CASES["api_token_github"]())
    repo.commit("oops")
    (repo.path / "leak.txt").unlink()
    repo.commit("remove it")
    rc, report, _ = repo.audit("--no-history")
    assert rc == 0, report["findings"]


def test_rev_range_limits_history(repo: Repo) -> None:
    repo.write("leak.txt", SECRET_CASES["api_token_github"]())
    repo.commit("oops")
    (repo.path / "leak.txt").unlink()
    cutoff = repo.commit("remove it")
    repo.write("ok.py", HEADER + "y = 2\n")
    repo.commit("fine")
    rc, report, _ = repo.audit(rev=f"{cutoff}..HEAD")
    assert rc == 0, report["findings"]


def test_deny_file_strings_found_but_never_printed(repo: Repo, tmp_path: Path) -> None:
    name = "Zebediah Quux-Frobnitz"
    deny = tmp_path / "deny.local"
    deny.write_text("# comment\n\n" + name + "\n")
    repo.write("AUTHORS.txt", "written by " + name.lower() + "\n")
    repo.commit("add")
    rc, report, out = repo.audit("--deny-file", str(deny))
    assert rc != 0
    assert "deny_string" in _cats(report)
    blob = json.dumps(report) + out
    assert "zebediah" not in blob.lower()
    assert "frobnitz" not in blob.lower()


def test_deny_string_in_history_commit_message_and_path(repo: Repo, tmp_path: Path) -> None:
    deny = tmp_path / "deny.local"
    deny.write_text("Secretname Person\n")
    repo.write("a.py", HEADER)
    repo.commit("by Secretname Person")
    rc, report, out = repo.audit("--deny-file", str(deny))
    assert rc != 0
    assert any(f["category"] == "deny_string" and f["file"] == "<commit-message>" for f in report["findings"])
    assert "secretname" not in (json.dumps(report) + out).lower()


def test_missing_deny_file_is_an_error(repo: Repo, tmp_path: Path) -> None:
    rc, _, out = repo.audit("--deny-file", str(tmp_path / "nope"))
    assert rc == 2
    assert "deny" in out.lower()


def test_email_policy(repo: Repo) -> None:
    repo.write("ok.txt", f"{OWNER}\nbot@users.noreply.github.com\nnoreply@example.org\nme@example.com\n")
    repo.commit("emails")
    rc, report, _ = repo.audit()
    assert rc == 0, report["findings"]
    repo.write("bad.txt", "mail ann" + "@corp-example.net\n")
    repo.commit("bad")
    rc, report, _ = repo.audit()
    assert rc != 0 and "email" in _cats(report)


def test_commit_author_email_is_checked(repo: Repo) -> None:
    repo.write("b.py", HEADER)
    repo.commit("by someone else", env={"GIT_AUTHOR_EMAIL": "x" + "@gmail.com"})
    rc, report, _ = repo.audit()
    assert rc != 0
    assert any(f["category"] == "email" and f["file"] == "<commit-author>" for f in report["findings"])


def test_license_missing(repo: Repo) -> None:
    _git(repo.path, "rm", "-q", "LICENSE")
    repo.commit("drop license")
    rc, report, _ = repo.audit("--no-history")
    assert rc != 0 and "license_missing" in _cats(report)


def test_license_text_must_be_agpl_v3(repo: Repo) -> None:
    repo.write("LICENSE", "MIT License\n")
    repo.commit("wrong license")
    rc, report, _ = repo.audit("--no-history")
    assert rc != 0 and "license_missing" in _cats(report)


def test_spdx_header_required_on_source_files(repo: Repo) -> None:
    repo.write("pkg/nohdr.py", "x = 1\n")
    repo.write("pkg/lib.rs", "fn main() {}\n")
    repo.write("pkg/empty.py", "")
    repo.commit("sources")
    rc, report, _ = repo.audit("--no-history")
    assert rc != 0
    missing = {f["file"] for f in report["findings"] if f["category"] == "spdx_missing"}
    assert missing == {"pkg/nohdr.py", "pkg/lib.rs"}


def test_spdx_other_identifier_is_inconsistent(repo: Repo) -> None:
    repo.write("pkg/other.py", "# SPDX-License-Identifier: MIT\n")
    repo.commit("mit")
    rc, report, _ = repo.audit("--no-history")
    assert rc != 0 and "spdx_inconsistent" in _cats(report)


def test_history_or_later_is_a_note_not_a_failure(repo: Repo) -> None:
    repo.write("old.py", "# SPDX-License-Identifier: " + OR_LATER + "\n")
    repo.commit("old header")
    repo.write("old.py", HEADER)
    repo.commit("relicense")
    rc, report, _ = repo.audit()
    assert rc == 0, report["findings"]
    notes = [f for f in report["findings"] if f["category"] == "spdx_or_later"]
    assert notes and all(f["severity"] == "note" and f["scope"] == "history" for f in notes)
    assert report["summary"]["errors"] == 0


def test_allowlist_requires_reason_and_suppresses(repo: Repo, tmp_path: Path) -> None:
    repo.write("pkg/fixture.txt", "p = '/Users/" + "fixtureuser/x'\n")
    repo.commit("fixture")
    rc, report, _ = repo.audit()
    assert rc != 0
    hit = next(f for f in report["findings"] if f["category"] == "home_path")

    bad = tmp_path / "allow-bad.json"
    bad.write_text(json.dumps({"entries": [{"category": "home_path", "file": hit["file"]}]}))
    rc, _, out = repo.audit("--allowlist", str(bad))
    assert rc == 2 and "reason" in out.lower()

    good = tmp_path / "allow.json"
    good.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "category": "home_path",
                        "file": hit["file"],
                        "fingerprint": hit["fingerprint"],
                        "reason": "reviewed: documentation placeholder",
                    }
                ]
            }
        )
    )
    rc, report, _ = repo.audit("--allowlist", str(good))
    assert rc == 0, report["findings"]
    assert report["summary"]["allowlisted"] >= 1


def test_allowlist_does_not_hide_a_different_value(repo: Repo, tmp_path: Path) -> None:
    repo.write("pkg/fixture.txt", "p = '/Users/" + "fixtureuser/x'\n")
    repo.commit("fixture")
    _, report, _ = repo.audit()
    hit = next(f for f in report["findings"] if f["category"] == "home_path")
    allow = tmp_path / "allow.json"
    allow.write_text(
        json.dumps({"entries": [{"category": "home_path", "file": hit["file"], "fingerprint": hit["fingerprint"], "reason": "r"}]})
    )
    repo.write("pkg/fixture.txt", "p = '/Users/" + "otheruser/x'\n")
    repo.commit("changed")
    rc, report, _ = repo.audit("--allowlist", str(allow), "--no-history")
    assert rc != 0


def test_placeholder_home_paths_and_example_values_are_not_flagged(repo: Repo) -> None:
    repo.write("assets.md", "AppIcon-20x20@2x.png git@github.com:o/r.git ann@peer.example\n")
    repo.write("docs.md", "cd /Users/<name>/proj\ncd /Users/you/x\ncd /Users/Shared\nTOKEN=<token>\npassword = None\n")
    repo.commit("docs")
    rc, report, _ = repo.audit()
    assert rc == 0, report["findings"]


def test_identifiers_near_secret_names_are_not_flagged(repo: Repo) -> None:
    repo.write(
        "pkg/ok.py",
        HEADER + 'token_hash = "sha256:' + "0123456789abcdef" * 4 + '"\nowner_token_path = "a_long_python_identifier_name_here"\n',
    )
    repo.commit("idents")
    rc, report, _ = repo.audit("--no-history")
    assert rc == 0, report["findings"]


def test_binary_files_do_not_crash(repo: Repo) -> None:
    (repo.path / "blob.bin").write_bytes(bytes(range(256)) * 40)
    repo.commit("bin")
    rc, report, _ = repo.audit()
    assert rc == 0, report["findings"]


def test_credential_file_names_are_flagged(repo: Repo) -> None:
    repo.write("deploy/cert.pem", "not really a key\n")
    repo.write("deploy/id_" + "ed25519", "x\n")
    repo.commit("creds")
    rc, report, _ = repo.audit("--no-history")
    assert rc != 0
    files = {f["file"] for f in report["findings"] if f["category"] in {"cloudflared_credential", "private_key"}}
    assert {"deploy/cert.pem", "deploy/id_ed25519"} <= files


def test_signing_grant_content(repo: Repo) -> None:
    repo.write("evidence/grant.json", json.dumps({"signing" + "_grant": "kJ3x9Qm2Zp7Wv1Rb5Tn8Yc4Hd6Lf0Sg3Ua9Ie2Ok5Pq7", "x": 1}))
    repo.commit("grant")
    rc, report, _ = repo.audit("--no-history")
    assert rc != 0 and "signing_grant" in _cats(report)


def test_default_range_without_origin_is_a_clear_error(repo: Repo) -> None:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(repo.path)],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 2
    assert "origin/main" in done.stdout + done.stderr


def test_report_is_valid_json_on_stdout_when_no_output_flag(repo: Repo) -> None:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(repo.path), "--rev-range", "HEAD"],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0
    assert json.loads(done.stdout)["summary"]["errors"] == 0


ASTRA = "Co-authored-by: " + "Astra.K <145781196+Astra-K@users." + "noreply.github.com>"


def test_any_trailer_is_a_commit_trailer_error_by_default(repo: Repo) -> None:
    repo.write("ok.py", HEADER + "y = 2\n")
    repo.commit("feat: add y\n\nBody text.\n\n" + ASTRA)
    rc, report, _ = repo.audit()
    assert rc == 1
    assert "commit_trailer" in _cats(report)


def test_the_exact_allowed_trailer_passes_once_per_commit(repo: Repo) -> None:
    repo.write("ok.py", HEADER + "y = 2\n")
    repo.commit("feat: add y\n\nBody text.\n\n" + ASTRA)
    rc, report, _ = repo.audit("--allowed-trailer", ASTRA)
    assert rc == 0, report["findings"]
    repo.write("ok2.py", HEADER + "z = 3\n")
    repo.commit("feat: add z\n\n" + ASTRA + "\n" + ASTRA)
    rc, report, _ = repo.audit("--allowed-trailer", ASTRA)
    assert rc == 1
    assert "commit_trailer" in _cats(report)


@pytest.mark.parametrize(
    "trailer",
    [
        "Co-authored-by: Someone Else <someone@example.com>",
        "Signed-off-by: Tranquil-Flow <" + OWNER + ">",
        "Co-Authored-By: " + ASTRA.split(": ", 1)[1],  # same person, different spelling
        ASTRA.replace(": ", ":  ", 1),  # extra space is not the exact line
        "Generated-by: a tool",
        "Reviewed-by: A Reviewer",
    ],
)
def test_every_other_trailer_still_fails_with_the_allow_flag(repo: Repo, trailer: str) -> None:
    repo.write("ok.py", HEADER + "y = 2\n")
    repo.commit("feat: add y\n\nBody text.\n\n" + trailer)
    rc, report, _ = repo.audit("--allowed-trailer", ASTRA)
    assert rc == 1, report["findings"]
    assert "commit_trailer" in _cats(report)


def test_allowed_trailer_plus_another_trailer_fails(repo: Repo) -> None:
    repo.write("ok.py", HEADER + "y = 2\n")
    repo.commit("feat: add y\n\nBody.\n\n" + ASTRA + "\nSigned-off-by: X <x@example.com>")
    rc, report, _ = repo.audit("--allowed-trailer", ASTRA)
    assert rc == 1
    assert "commit_trailer" in _cats(report)


def test_messages_without_trailers_pass(repo: Repo) -> None:
    repo.write("ok.py", HEADER + "y = 2\n")
    repo.commit("feat(x): add y\n\nThe body mentions a rule: nothing special here.\nSecond line.")
    rc, report, _ = repo.audit()
    assert rc == 0, report["findings"]
