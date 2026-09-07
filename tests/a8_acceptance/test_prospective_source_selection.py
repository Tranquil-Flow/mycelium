"""Local production CLI composition; projection fixtures are not physical proof."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = "sha256:" + "1" * 64
CASE = "missing_or_stale_path_measurements"


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.PIPE,
    ).strip()


@pytest.fixture(scope="module")
def candidate(tmp_path_factory):
    # Local shared-object clone only; never a remote checkout or fleet call.
    parent = tmp_path_factory.mktemp("prospective-source").resolve()
    root = parent / "candidate"
    subprocess.run(
        ["git", "clone", "--shared", "--no-checkout", str(ROOT), str(root)],
        check=True, capture_output=True,
    )
    git(root, "checkout", "--detach", "HEAD")
    for relative in ("mycelium_internet/physical.py", "scripts/a8_run_physical_gate.py"):
        shutil.copy2(ROOT / relative, root / relative)
    git(root, "add", "mycelium_internet/physical.py", "scripts/a8_run_physical_gate.py")
    if git(root, "diff", "--cached", "--name-only"):
        git(root, "-c", "user.name=Tranquil-Flow", "-c",
            "user.email=tranquil_flow@protonmail.com", "commit", "-m",
            "Local prospective selection test fixture")
    commit = git(root, "rev-parse", "HEAD")
    paths = git(root, "ls-tree", "-r", "--name-only", commit).splitlines()
    pins = []
    for relative in paths:
        raw = (root / relative).read_bytes()
        pins.append({"path": relative, "sha256": digest(raw), "size_bytes": len(raw)})
    manifest = {"protocol": "mycelium.combined_candidate_source_manifest.v1",
                "base_commit": commit, "files": pins}
    return root, commit, manifest


def write_manifest(tmp_path, manifest):
    path = tmp_path.resolve() / "prospective.json"
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(raw)
    return path, digest(raw)


def run_candidate(candidate, tmp_path, *, explicit=True, mutate=None, timeout=60):
    root, commit, manifest = candidate
    path, expected = write_manifest(tmp_path, manifest)
    evidence = tmp_path / "records"
    evidence.mkdir(mode=0o700)
    argv = [sys.executable, "-B", str(root / "scripts/a8_run_physical_gate.py"),
            "run", CASE, "--origin", "https://seed.example.invalid",
            "--spec-digest", SPEC, "--source-digest", expected,
            "--seal", "--evidence-root", str(evidence)]
    if explicit:
        argv += ["--source-manifest", str(path), "--candidate-commit", commit]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(key, None)
    if mutate is not None:
        mutate(path, argv, env)
    return subprocess.run(argv, cwd=root, env=env, capture_output=True,
                          text=True, timeout=timeout), evidence, expected


def test_fixed_path_rejects_prospective_digest(candidate, tmp_path):
    result, records, _ = run_candidate(candidate, tmp_path, explicit=False)
    assert result.returncode == 2
    assert "source_binding_invalid" in result.stderr
    assert not list(records.iterdir())


def test_explicit_prospective_cli_executes_and_seals(candidate, tmp_path):
    result, records, expected = run_candidate(candidate, tmp_path)
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["source_digest"] == expected
    assert document["result"] == "passed"
    sealed = list(records.iterdir())
    assert len(sealed) == 1
    assert json.loads(sealed[0].read_bytes()) == document
    assert sealed[0].stat().st_mode & 0o777 == 0o400


def set_arg(argv, flag, value):
    argv[argv.index(flag) + 1] = value


@pytest.mark.parametrize("failure", [
    "missing", "invalid_json", "tampered", "digest_mismatch", "duplicate_key",
    "wrong_protocol", "missing_commit", "missing_path", "relative_path",
    "traversal_path", "manifest_symlink", "parent_symlink", "writable_manifest",
    "candidate_mismatch", "wrong_loaded_root", "deficient_closure", "unsafe_source",
    "duplicate_pin", "pin_hash", "pin_size", "arbitrary_root_field",
])
def test_explicit_cli_selection_fails_closed(candidate, tmp_path, failure):
    root, commit, _ = candidate

    def mutate(path, argv, env):
        document = json.loads(path.read_bytes())
        if failure == "missing":
            path.unlink()
        elif failure == "invalid_json":
            path.write_bytes(b"{")
            set_arg(argv, "--source-digest", digest(path.read_bytes()))
        elif failure == "tampered":
            path.write_bytes(path.read_bytes() + b" ")
        elif failure == "digest_mismatch":
            set_arg(argv, "--source-digest", SPEC)
        elif failure == "duplicate_key":
            path.write_bytes(path.read_bytes()[:-1] + b',"files":[]}')
            set_arg(argv, "--source-digest", digest(path.read_bytes()))
        elif failure in {"missing_commit", "missing_path"}:
            flag = "--candidate-commit" if failure == "missing_commit" else "--source-manifest"
            start = argv.index(flag)
            del argv[start:start + 2]
        elif failure == "relative_path":
            set_arg(argv, "--source-manifest", os.path.relpath(path, root))
        elif failure == "traversal_path":
            set_arg(argv, "--source-manifest", str(path.parent) + "/../" + path.parent.name + "/" + path.name)
        elif failure == "manifest_symlink":
            target = path.with_suffix(".target")
            path.rename(target)
            path.symlink_to(target)
        elif failure == "parent_symlink":
            link = path.parent / "link"
            link.symlink_to(path.parent, target_is_directory=True)
            set_arg(argv, "--source-manifest", str(link / path.name))
        elif failure == "writable_manifest":
            path.chmod(0o666)
        else:
            if failure == "wrong_protocol":
                document["protocol"] = "mycelium.a8_source_manifest.v1"
            elif failure == "candidate_mismatch":
                document["base_commit"] = "a" * 40
            elif failure == "wrong_loaded_root":
                # A real available Git commit plus its matching manifest digest
                # cannot select a different tree than the loaded code's HEAD.
                other = git(root, "rev-parse", "HEAD~1")
                document["base_commit"] = other
                set_arg(argv, "--candidate-commit", other)
                env["GIT_DIR"] = str(root / ".git")
            elif failure == "deficient_closure":
                document["files"] = [document["files"][0]]
            elif failure == "unsafe_source":
                document["files"][0]["path"] = "../outside.py"
            elif failure == "duplicate_pin":
                document["files"].insert(0, document["files"][0])
            elif failure == "pin_hash":
                document["files"][0]["sha256"] = SPEC
            elif failure == "pin_size":
                document["files"][0]["size_bytes"] += 1
            elif failure == "arbitrary_root_field":
                document["source_root"] = str(tmp_path)
            raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
            path.write_bytes(raw)
            set_arg(argv, "--source-digest", digest(raw))
        # Any accidental fallthrough must stop before opening peer inputs.
        argv += ["--bundle-file", str(tmp_path / "absent-peer-input.json")]

    result, records, _ = run_candidate(candidate, tmp_path, mutate=mutate)
    assert result.returncode == 2, result.stderr
    assert result.stderr.strip() == "gate rejected: source_binding_invalid"
    assert not result.stdout
    assert not list(records.iterdir())


@pytest.mark.parametrize("failure", ["dirty_bytes", "symlink", "directory_symlink", "mode"])
def test_explicit_consumer_rejects_source_filesystem_drift(candidate, tmp_path, failure):
    root, _, _ = candidate
    target = root / "README.md"
    raw, mode = target.read_bytes(), target.stat().st_mode & 0o777
    held_directory = root / "docs-held"
    try:
        if failure == "dirty_bytes":
            target.write_bytes(raw + b"drift")
        elif failure == "symlink":
            outside = tmp_path / "same-bytes"
            outside.write_bytes(raw)
            target.unlink()
            target.symlink_to(outside)
        elif failure == "directory_symlink":
            (root / "docs").rename(held_directory)
            (root / "docs").symlink_to(held_directory, target_is_directory=True)
        else:
            target.chmod(0o644 if mode & 0o111 else 0o755)
        result, records, _ = run_candidate(candidate, tmp_path)
        assert result.returncode == 2
        assert "source_binding_invalid" in result.stderr
        assert not list(records.iterdir())
    finally:
        if failure == "directory_symlink":
            (root / "docs").unlink()
            held_directory.rename(root / "docs")
        else:
            if target.is_symlink():
                target.unlink()
            target.write_bytes(raw)
            target.chmod(mode)


def test_nonregular_explicit_manifest_rejects_without_blocking(candidate, tmp_path):
    def mutate(path, argv, env):
        path.unlink()
        os.mkfifo(path, mode=0o600)
    result, records, _ = run_candidate(candidate, tmp_path, mutate=mutate, timeout=2)
    assert result.returncode == 2
    assert result.stderr.strip() == "gate rejected: source_binding_invalid"
    assert not list(records.iterdir())


def test_explicit_selection_ignores_git_environment_redirection(candidate, tmp_path):
    def mutate(path, argv, env):
        env.update(GIT_DIR=str(tmp_path / "nonexistent-git"),
                   GIT_WORK_TREE=str(tmp_path), GIT_INDEX_FILE=str(tmp_path / "index"))
    result, _, _ = run_candidate(candidate, tmp_path, mutate=mutate)
    assert result.returncode == 0, result.stderr


def test_historical_default_and_explicit_failure_do_not_share_cache(candidate, tmp_path):
    root, commit, _ = candidate
    code = '''
from pathlib import Path
import sys
from mycelium_internet import physical as p
kwargs = dict(origin="https://seed.example.invalid", evidence_root=None,
              spec_digest="sha256:" + "1"*64, source_digest=p._HISTORICAL_A8_SOURCE_DIGEST)
doc = p.execute_case("missing_or_stale_path_measurements", **kwargs)
assert doc["result"] == "passed"
assert p._HISTORICAL_A8_SOURCE_DIGEST in p._VERIFIED_HISTORICAL_SOURCE_DIGESTS
for selected in (None, Path(sys.argv[1])):
    try:
        p.execute_case("missing_or_stale_path_measurements", **kwargs,
                       source_manifest=selected, candidate_commit=sys.argv[2])
    except p.PhysicalGateError as exc:
        assert exc.code == "source_binding_invalid"
    else:
        raise AssertionError("explicit failure used historical cache")
print("historical default passed; explicit failures rejected")
'''
    result = subprocess.run([sys.executable, "-B", "-c", code,
                             str(tmp_path / "missing"), commit],
                            cwd=root, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "historical default passed" in result.stdout


@pytest.mark.parametrize("drift", ["before_seal", "during_seal"])
def test_explicit_sealer_rechecks_without_fallback(candidate, tmp_path, drift):
    root, commit, manifest = candidate
    path, expected = write_manifest(tmp_path, manifest)
    records = tmp_path / "records"
    records.mkdir(mode=0o700)
    code = '''
from pathlib import Path
import sys
from mycelium_internet import physical as p
manifest, root = Path(sys.argv[1]), Path(sys.argv[2])
selection = dict(source_manifest=manifest, candidate_commit=sys.argv[3])
doc = p.execute_case("missing_or_stale_path_measurements",
    origin="https://seed.example.invalid", evidence_root=None,
    spec_digest="sha256:"+"1"*64, source_digest=sys.argv[4], **selection)
if sys.argv[5] == "before_seal":
    manifest.write_bytes(b"tampered")
else:
    original = p.os.fsync
    def change_after_write(fd):
        original(fd)
        manifest.write_bytes(b"tampered")
    p.os.fsync = change_after_write
try:
    p.seal_qualification(doc, evidence_root=root, **selection)
except p.PhysicalGateError as exc:
    assert exc.code == "source_binding_invalid"
else:
    raise AssertionError("sealer accepted changed explicit manifest")
assert not list(root.iterdir())
print("source drift rejected; no sealed record remains")
'''
    result = subprocess.run([sys.executable, "-B", "-c", code, str(path),
                             str(records), commit, expected, drift], cwd=root,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
