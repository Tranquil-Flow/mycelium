# SPDX-License-Identifier: AGPL-3.0-only
"""conftest for tests/openai_shim/ — prevent import-time collisions with sibling
test_server modules in tests/ that share basename.

The L2 host has tests/interactive/test_server.py and tests/openai_shim/test_server.py
on the same pytest collection; when both have __pycache__/__init__ shared via
sys.path, pytest's import hits the wrong file and raises an import-file-mismatch
collection error. Setting --no-caches is the proper fix, but to be safe at the
suite level we also drop the shim tests' __pycache__ before collection.
"""
import os
import shutil
import sys


def _purge_openai_shim_pycache(path):
    cache = os.path.join(path, "__pycache__")
    if os.path.isdir(cache):
        shutil.rmtree(cache, ignore_errors=True)


_purge_openai_shim_pycache(os.path.dirname(__file__))

# Ensure interactive's test_server.pyc can't shadow ours via earlier-loaded
# __pycache__. Strip any pre-loaded test_server modules so pytest reimports ours.
for mod in list(sys.modules):
    if mod == "test_server" or mod.startswith("test_server."):
        del sys.modules[mod]
