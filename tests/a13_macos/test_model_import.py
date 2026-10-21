# SPDX-License-Identifier: AGPL-3.0-only
"""Import a model from a local folder (USB stick, shared drive) into the cache.

Every file is checked against the pinned manifest before anything is installed:
a wrong byte, a wrong size, a missing or an unexpected file, or a link refuses
the whole import with a plain-English message and leaves the cache untouched.
Pure filesystem tests; no network, no sockets.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from mycelium_a13_macos.acquisition import SelectedModelAcquisition
from mycelium_a13_macos.model_import import ModelImportError, import_model_folder
from mycelium_a13_macos.serving import CANDIDATE_MODEL
from mycelium_demo import cli
from mycelium_platform_neutral.offline import OFFLINE_ENV
from test_acquisition import _consent, _injected_manifest, _repository_files

MODEL = CANDIDATE_MODEL


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(OFFLINE_ENV, raising=False)
    monkeypatch.delenv("MYCELIUM_NETWORK_MODE", raising=False)


@pytest.fixture
def stick(tmp_path):
    files = _repository_files()
    folder = tmp_path / "stick"
    folder.mkdir()
    for name, data in files.items():
        (folder / name).write_bytes(data)
    cache = tmp_path / "cache"
    return folder, cache, files, _injected_manifest(files)


def _snapshot(cache: Path) -> Path:
    return (cache / ("models--" + MODEL.model_id.replace("/", "--")) / "snapshots"
            / MODEL.resolved_commit)


def _untouched(cache: Path) -> bool:
    return not cache.exists() or not any(cache.rglob("*"))


def _refused(stick, code: str, fragment: str, **override):
    folder, cache, _, manifest = stick
    with pytest.raises(ModelImportError) as caught:
        import_model_folder(folder, manifest=override.get("manifest", manifest), model=MODEL,
                            cache_root=cache)
    assert caught.value.code == code
    assert fragment in str(caught.value)
    assert not _snapshot(cache).exists()
    assert not any(path.name.endswith(".acquiring") for path in cache.rglob("*"))
    return caught.value


def test_a_verified_folder_is_installed_read_only_and_usable(stick):
    folder, cache, files, manifest = stick
    result = import_model_folder(folder, manifest=manifest, model=MODEL, cache_root=cache)
    root = _snapshot(cache)
    assert result["files"] == len(files) and result["bytes"] == manifest["total_bytes"]
    assert Path(result["snapshot"]) == root
    for name, data in files.items():
        installed = root / name
        assert installed.read_bytes() == data
        assert not stat.S_IMODE(installed.stat().st_mode) & 0o222
    # The very next download of this model finds it and fetches nothing.
    acquisition = SelectedModelAcquisition(cache, _manifest=manifest)
    try:
        status = acquisition._run_blocking(_consent(manifest["total_bytes"]))
        assert status["state"] == "prepared", status
    finally:
        acquisition.close()


def test_a_flipped_byte_with_the_same_size_is_refused(stick):
    folder, _, files, _ = stick
    target = folder / "model.safetensors"
    data = bytearray(files["model.safetensors"])
    data[-1] ^= 0xFF
    target.write_bytes(bytes(data))
    error = _refused(stick, "import_hash_mismatch", "model.safetensors")
    assert "does not match" in str(error)


def test_a_wrong_size_is_refused(stick):
    folder, _, files, _ = stick
    (folder / "tokenizer.json").write_bytes(files["tokenizer.json"] + b" ")
    _refused(stick, "import_size_mismatch", "tokenizer.json")


def test_an_unexpected_file_is_refused(stick):
    folder, *_ = stick
    (folder / "notes.txt").write_bytes(b"not part of the pinned model")
    _refused(stick, "import_unexpected_file", "notes.txt")


def test_an_unexpected_file_in_a_subfolder_is_refused(stick):
    folder, *_ = stick
    (folder / "extra").mkdir()
    (folder / "extra" / "weights.bin").write_bytes(b"x")
    _refused(stick, "import_unexpected_file", "extra/weights.bin")


def test_a_missing_file_is_refused(stick):
    folder, *_ = stick
    (folder / "config.json").unlink()
    _refused(stick, "import_file_missing", "config.json")


def test_a_link_in_the_folder_is_refused(stick, tmp_path):
    folder, _, files, _ = stick
    outside = tmp_path / "outside.json"
    outside.write_bytes(files["tokenizer.json"])
    (folder / "tokenizer.json").unlink()
    (folder / "tokenizer.json").symlink_to(outside)
    _refused(stick, "import_link_refused", "tokenizer.json")


def test_every_problem_is_listed_not_only_the_first(stick):
    folder, _, files, _ = stick
    (folder / "config.json").unlink()
    (folder / "tokenizer.json").write_bytes(files["tokenizer.json"] + b" ")
    (folder / "notes.txt").write_bytes(b"x")
    error = _refused(stick, "import_folder_not_the_pinned_model", "config.json")
    for fragment in ("tokenizer.json", "notes.txt"):
        assert fragment in str(error)


def test_a_manifest_for_another_commit_is_refused(stick):
    _, _, _, manifest = stick
    other = {**manifest, "resolved_commit": "c" * 40}
    _refused(stick, "model_manifest_commit_mismatch", "commit", manifest=other)


def test_an_already_installed_model_is_not_overwritten(stick):
    folder, cache, _, manifest = stick
    import_model_folder(folder, manifest=manifest, model=MODEL, cache_root=cache)
    with pytest.raises(ModelImportError) as caught:
        import_model_folder(folder, manifest=manifest, model=MODEL, cache_root=cache)
    assert caught.value.code == "import_already_installed"
    assert (_snapshot(cache) / "config.json").exists()


def test_a_missing_folder_is_refused_in_plain_english(stick):
    _, cache, _, manifest = stick
    with pytest.raises(ModelImportError) as caught:
        import_model_folder(cache.parent / "nope", manifest=manifest, model=MODEL, cache_root=cache)
    assert caught.value.code == "import_folder_missing"
    assert "nope" in str(caught.value)


# ------------------------------------------------------------------ the CLI


def _write_manifest(tmp_path: Path, manifest) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def _argv(stick, tmp_path, *extra):
    folder, cache, _, manifest = stick
    return ["models", "import", str(folder), "--model", MODEL.model_id,
            "--commit", MODEL.resolved_commit, "--cache-root", str(cache), *extra]


def test_cli_imports_a_folder_with_a_manifest_file(stick, tmp_path, capsys):
    manifest = stick[3]
    code = cli.main(_argv(stick, tmp_path, "--manifest", str(_write_manifest(tmp_path, manifest))),
                    environ={})
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["files"] == len(manifest["files"])
    assert (_snapshot(stick[1]) / "config.json").exists()


def test_cli_refuses_a_bad_folder_with_a_plain_message(stick, tmp_path, capsys):
    folder, cache, files, manifest = stick
    (folder / "tokenizer.json").write_bytes(b"[]")
    code = cli.main(_argv(stick, tmp_path, "--manifest", str(_write_manifest(tmp_path, manifest))),
                    environ={})
    captured = capsys.readouterr()
    assert code == 1 and captured.out == ""
    assert "tokenizer.json" in captured.err and "Traceback" not in captured.err
    assert not _snapshot(cache).exists()


def test_cli_offline_needs_a_manifest_and_never_contacts_hugging_face(stick, tmp_path, capsys):
    def resolver(model):
        raise AssertionError("Hugging Face must not be contacted")

    code = cli.main(_argv(stick, tmp_path, "--offline"), environ={},
                    model_manifest_resolver=resolver)
    captured = capsys.readouterr()
    assert code == 1
    assert "--manifest" in captured.err and "offline" in captured.err.lower()
    assert _untouched(stick[1])


def test_cli_lan_mode_implies_offline_for_the_import(stick, tmp_path, capsys):
    code = cli.main(_argv(stick, tmp_path, "--network-mode", "lan"), environ={},
                    model_manifest_resolver=lambda model: pytest.fail("no Hugging Face"))
    assert code == 1 and "--manifest" in capsys.readouterr().err


def test_cli_online_without_a_manifest_file_resolves_the_pinned_manifest(stick, tmp_path, capsys):
    manifest = stick[3]
    asked = []
    code = cli.main(_argv(stick, tmp_path), environ={},
                    model_manifest_resolver=lambda model: asked.append(model) or manifest)
    assert code == 0 and asked == [MODEL]
    assert capsys.readouterr().out.strip().startswith("{")


def test_cli_refuses_an_unregistered_model_in_plain_english(stick, tmp_path, capsys):
    folder, cache, _, manifest = stick
    code = cli.main(["models", "import", str(folder), "--model", "someone/else",
                     "--commit", "a" * 40, "--cache-root", str(cache),
                     "--manifest", str(_write_manifest(tmp_path, manifest))], environ={})
    captured = capsys.readouterr()
    assert code == 1 and "someone/else" in captured.err and "supported" in captured.err
    assert _untouched(cache)


def test_cli_rejects_a_malformed_manifest_file(stick, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    code = cli.main(_argv(stick, tmp_path, "--manifest", str(bad)), environ={})
    assert code == 1 and "manifest" in capsys.readouterr().err.lower()
    assert os.path.exists(bad)


# ------------------------------------------- fetching the manifest file once


def _fetch_argv(tmp_path, *extra):
    return ["models", "fetch-manifest", "--model", MODEL.model_id, "--commit",
            MODEL.resolved_commit, "--out", str(tmp_path / "pinned.manifest.json"), *extra]


def test_cli_fetch_manifest_writes_the_pinned_manifest_that_import_accepts(stick, tmp_path, capsys):
    manifest = stick[3]
    code = cli.main(_fetch_argv(tmp_path), environ={},
                    model_manifest_resolver=lambda model: manifest)
    assert code == 0 and "pinned.manifest.json" in capsys.readouterr().out
    written = json.loads((tmp_path / "pinned.manifest.json").read_text())
    assert written == {**manifest, "files": sorted(manifest["files"], key=lambda row: row["path"])}
    code = cli.main(_argv(stick, tmp_path, "--manifest", str(tmp_path / "pinned.manifest.json")),
                    environ={})
    assert code == 0


def test_cli_fetch_manifest_is_refused_offline_without_contacting_hugging_face(tmp_path, capsys):
    code = cli.main(_fetch_argv(tmp_path, "--offline"), environ={},
                    model_manifest_resolver=lambda model: pytest.fail("no Hugging Face"))
    captured = capsys.readouterr()
    assert code == 1 and "offline" in captured.err.lower() and "--no-offline" in captured.err
    assert not (tmp_path / "pinned.manifest.json").exists()


def test_cli_fetch_manifest_does_not_overwrite_a_file(stick, tmp_path, capsys):
    (tmp_path / "pinned.manifest.json").write_text("keep")
    code = cli.main(_fetch_argv(tmp_path), environ={},
                    model_manifest_resolver=lambda model: stick[3])
    assert code == 1 and "already exists" in capsys.readouterr().err
    assert (tmp_path / "pinned.manifest.json").read_text() == "keep"


def test_cli_fetch_manifest_refuses_a_manifest_for_another_commit(stick, tmp_path, capsys):
    other = {**stick[3], "resolved_commit": "b" * 40}
    code = cli.main(_fetch_argv(tmp_path), environ={},
                    model_manifest_resolver=lambda model: other)
    assert code == 1 and "commit" in capsys.readouterr().err
    assert not (tmp_path / "pinned.manifest.json").exists()
