# SPDX-License-Identifier: AGPL-3.0-only
"""Criterion 3's acceptance vehicle must be ssh-free BY CONSTRUCTION.

PLAN.md section 3 criterion 3 needs an acceptance run with no SSH. Reading the code shows the
acceptance step already qualifies: `round14_stream.py` drives the product API and the SSE
stream over plain HTTP against the owner's own registry (`owner(...)` -> localhost, then
`http://127.0.0.1:{port}`), and never touches the ssh transport.

That is the property worth pinning. A future edit that reaches for `target(...)` - the only
ssh-based RPC - or that shells out would silently move the acceptance back onto ssh, and the
loss would be invisible until someone tried to re-earn the criterion. These tests make the
regression loud and local. They are structural, not behavioural: they assert what the module
CAN do, not what a particular run happened to do (that is `ssh_audit`'s job).
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
STREAM = ROOT / 'scripts/a13_operator/round14_stream.py'
RPC = ROOT / 'scripts/a13_operator/round14_rpc.py'


def _module(tree):
    """String constants and imported module names for a parsed module."""
    consts, modules, names = set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            consts.add(node.value)
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            modules.add(node.module or '')
        if isinstance(node, ast.Name):
            names.add(node.id)
    return consts, modules, names


def test_the_acceptance_step_never_reaches_for_the_ssh_rpc():
    consts, modules, names = _module(ast.parse(STREAM.read_text()))
    # `target` is the ssh transport. Not merely unused -- not named at all.
    assert 'target' not in names
    assert not any('scp' in value for value in consts)
    assert not any('ssh' in value for value in consts)


def test_the_acceptance_step_cannot_shell_out():
    _consts, modules, _names = _module(ast.parse(STREAM.read_text()))
    assert 'subprocess' not in modules
    assert 'subprocess' not in STREAM.read_text()


def test_the_acceptance_step_imports_the_local_ssh_free_transport():
    source = STREAM.read_text()
    assert 'from round14_rpc import owner' in source
    assert 'target' not in source.split('from round14_rpc import')[1].split('\n')[0]


def _functions(path):
    tree = ast.parse(path.read_text())
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            consts = {c.value for c in ast.walk(node)
                      if isinstance(c, ast.Constant) and isinstance(c.value, str)}
            out[node.name] = consts
    return out


def test_owner_transport_is_local_and_target_transport_is_the_ssh_one():
    fns = _functions(RPC)
    assert 'owner' in fns and 'target' in fns
    # The boundary, pinned on both sides.
    assert 'ssh' not in fns['owner'] and 'scp' not in fns['owner']
    assert 'ssh' in fns['_target_once'] and 'scp' in fns['_target_once']


def test_the_ssh_transport_is_opt_in_not_a_default():
    # `owner` must not delegate to the ssh helper; if it did, every acceptance step would ssh.
    owner_src = None
    tree = ast.parse(RPC.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'owner':
            owner_src = ast.unparse(node)
    assert owner_src is not None
    assert '_target_once' not in owner_src
    assert 'require_local' in owner_src
