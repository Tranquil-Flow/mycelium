# SPDX-License-Identifier: AGPL-3.0-only
"""Bind boot_id onto the plan's peers -- inert by default, and only ever boot_id.

The measured failure (2026-09-19): soak03's compiled plan carried peer boot_id boot-af00774d... while the
node's live hello said boot-a0423521..., because plan_binding refreshes the plan's OFFERS against live
membership but never touches controller.peers, which is what becomes PeerIdentity(**peer) and what the
identity check compares. boot_id is not a membership field (nothing in mycelium_membership carries it), so
the live OBSERVATION is the only fresh source.

These tests pin the shape that makes the change safe, because the change sits on the qualification path:
  * inert with no observations -- existing callers cannot change behaviour;
  * only boot_id is replaced -- node_id and host_id are untouched, so the impostor case is still refused;
  * a node with no observation keeps the plan's value (fallback, not a guess).
"""
import ast
import pathlib
import textwrap

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = (REPO / 'mycelium_seed/plan_binding.py').read_text()
TREE = ast.parse(SRC)


def test_the_new_input_is_optional_so_every_existing_caller_is_unaffected():
    fn = next(n for n in ast.walk(TREE)
              if isinstance(n, ast.FunctionDef) and n.name == 'bind_operator_plan_document')
    # it is a KEYWORD-ONLY argument (the signature has a bare `*`), so it lives in kwonlyargs --
    # looking in args.args is what a sloppy version of this test does, and it would silently pass by
    # finding nothing to check.
    names = [a.arg for a in fn.args.kwonlyargs]
    assert 'observed_boot_ids' in names, 'input missing from keyword-only args'
    i = names.index('observed_boot_ids')
    default = fn.args.kw_defaults[i]
    assert isinstance(default, ast.Constant) and default.value is None, 'default must be None'


def test_the_refresh_is_guarded_and_assigns_only_boot_id():
    """The safety shape: guarded on presence, and nothing but boot_id is written."""
    i = SRC.index('if observed_boot_ids:')
    j = SRC.index('return result', i)
    block = SRC[i:j]
    assigns = [line.strip() for line in block.splitlines()
               if '[' in line and ']' in line and '=' in line and 'peer[' in line]
    assert assigns, 'no peer field is assigned at all'
    assert all('boot_id' in a for a in assigns), 'the block writes something other than boot_id: %r' % assigns
    assert 'peer["node_id"]' not in block and 'peer["host_id"]' not in block


def test_the_block_runs_before_the_return_so_no_early_path_skips_it():
    assert SRC.index('if observed_boot_ids:') < SRC.rindex('return result')


def test_behaviour_inert_without_observations_and_preferring_them_with(tmp_path):
    """Run the real block against a stub plan: unchanged when inert, refreshed when given."""
    src = SRC
    i = src.index('    if observed_boot_ids:')
    j = src.index('    return result', i)
    block = textwrap.dedent(src[i:j])
    def run(plan, observed):
        # the block operates on `result`, which is the plan document in the real function -- naming it
        # anything else here would test a copy of the logic rather than the logic
        ns = {'observed_boot_ids': observed, 'result': plan}
        exec(compile(block, '<block>', 'exec'), ns)
        return plan
    base = {'controller': {'peers': [{'node_id': 'n1', 'host_id': 'h1', 'boot_id': 'old'}]}}
    inert = run({'controller': {'peers': [dict(base['controller']['peers'][0])]}}, None)
    assert inert['controller']['peers'][0]['boot_id'] == 'old'
    refreshed = run({'controller': {'peers': [dict(base['controller']['peers'][0])]}}, {'n1': 'new'})
    peer = refreshed['controller']['peers'][0]
    assert peer['boot_id'] == 'new'
    assert peer['node_id'] == 'n1' and peer['host_id'] == 'h1'
    # Nothing may be ADDED to the controller: its field set is closed and
    # load_operator_plan refuses the document with plan_unknown_field (measured 2026-09-19, where the
    # audit field made ServingPreparationError("operator_plan_canonical_invalid") stop serving).
    assert 'boot_id_bindings' not in refreshed['controller']
    assert set(refreshed['controller']) == set(base['controller'])
    missing = run({'controller': {'peers': [{'node_id': 'n2', 'boot_id': 'kept'}]}}, {'n1': 'new'})
    assert missing['controller']['peers'][0]['boot_id'] == 'kept'
