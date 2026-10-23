# SPDX-License-Identifier: AGPL-3.0-only
"""Source-level, model-free evidence for the A11 strict-KV repair.

Every numerical object here is a labelled stdlib DOUBLE (float32 rounding emulated with
struct).  Nothing below is MLX evidence: the doubles show (a) the failure SHAPE the native
discriminator must look for and (b) that the overlay code, executed through fakes, feeds
rows exactly like serial decode.  Native parity stays UNPROVEN until the parent's run.
"""
import ast
import subprocess
import hashlib
import struct
import random
import unittest
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
BASE_COMMIT = 'a398eaa4352cf795f0d9855457ba323beb7c4697'
OVERLAY = REPO / 'mycelium_router/mlx_runtime.py'   # the repaired source now lives at the repo path
F_SHA = '66734c263824545e18ea9b1a99dd1a3ddeb08a8c5d351dd6abc94766dd0d7e3c'
NATIVE = FIXTURES / 'a11_diagnostic06_native_result.json'   # pre-repair diagnostic06 native evidence (RED)


def base_source():
    """Pre-repair mlx_runtime.py bytes, read from the frozen base commit (a398eaa4) in git history."""
    try:
        return subprocess.run(['git', '-C', str(REPO), 'show', BASE_COMMIT + ':mycelium_router/mlx_runtime.py'],
                              check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:   # shallow clone / no git: cannot reconstruct base
        raise unittest.SkipTest('base commit %s not available: %s' % (BASE_COMMIT, exc))


def tree(path):
    if path == 'BASE':
        return ast.parse(base_source().decode())
    return ast.parse(Path(path).read_text())


def top(t, name):
    return next(n for n in t.body if getattr(n, 'name', None) == name)


def method(t, cls, name):
    return next(n for n in top(t, cls).body if getattr(n, 'name', None) == name)


def dump(n):
    return ast.dump(n)


def f32(x):
    return struct.unpack('f', struct.pack('f', x))[0]


def bits(x):
    return struct.pack('f', x)


class Reject(Exception):
    pass


def reject(code, detail=''):
    raise Reject(code)


def load_helpers():
    t = tree(OVERLAY)
    ns = {'Any': object, '_reject': reject, 'INT8_EXECUTION_PERSISTENT_PACKED': 'persistent_packed_int8', 'mx': None}
    wanted = []
    for n in t.body:
        if isinstance(n, ast.Assign) and any(getattr(x, 'id', '').startswith(('VERIFY_ARITH', '_VERIFY_ARITH')) for x in n.targets):
            wanted.append(n)
        if isinstance(n, ast.FunctionDef) and n.name in ('_verify_arithmetic_mode', '_verify_rows'):
            wanted.append(n)
    exec(compile(ast.Module(wanted, []), 'overlay-helpers', 'exec'), ns)
    return ns


# ---- float32 kernel doubles (NOT MLX) -------------------------------------------------
def dot_serial(p, v):
    """single-row 'gemv' double: left-to-right float32 accumulation over exactly len(p) keys."""
    acc = 0.0
    for a, b in zip(p, v):
        acc = f32(acc + f32(a * b))
    return acc


def dot_span(p_padded, v_padded):
    """multi-row 'tiled' double: 4 interleaved lanes over the PADDED key axis, then combined."""
    lanes = [0.0] * 4
    for i, (a, b) in enumerate(zip(p_padded, v_padded)):
        lanes[i % 4] = f32(lanes[i % 4] + f32(a * b))
    return f32(f32(lanes[0] + lanes[1]) + f32(lanes[2] + lanes[3]))


def make_case(seed=7, prefix=4, span=4):
    r = random.Random(seed)
    n = prefix + span
    probs = [[f32(r.random()) for _ in range(i + prefix + 1)] for i in range(span)]
    values = [f32((r.random() - .5) * 8) for _ in range(n)]
    return probs, values, prefix, span


def find_seed():
    """First seed in a fixed range whose span-kernel double differs from serial in bits (deterministic)."""
    for seed in range(1, 200):
        probs, values, prefix, span = make_case(seed)
        s, b = serial_rows(probs, values), batched_span(probs, values, prefix, span)
        if s and any(bits(x) != bits(y) for x, y in zip(s, b)):
            return seed
    raise AssertionError('no_double_divergence_seed')


def serial_rows(probs, values):
    return [dot_serial(p, values[:len(p)]) for p in probs]


def batched_span(probs, values, prefix, span):
    n = prefix + span
    out = []
    for p in probs:
        padded = list(p) + [0.0] * (n - len(p))   # causal mask: exact zeros
        out.append(dot_span(padded, values))
    return out


class Rows:
    """Fake mx array: one value per span row; slices apply to the first non-trivial slice."""
    def __init__(self, data):
        self.data = list(data)

    @property
    def shape(self):
        n = len(self.data)
        return (1, n, n, n)

    def __getitem__(self, key):
        for part in key:
            if isinstance(part, slice) and part != slice(None):
                return Rows(self.data[part])
        return self


class FakeMx:
    @staticmethod
    def concatenate(parts, axis=0):
        return Rows([x for p in parts for x in p.data])

    @staticmethod
    def eval(*a):
        return None


class MlxSourceContract(unittest.TestCase):
    def test_base_is_exact_frozen_source_and_lacks_repair(self):
        raw = base_source()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), F_SHA)
        text = raw.decode()
        for name in ('_verify_rows', '_qwen2_attend_rows', 'verify_arithmetic'):
            self.assertNotIn(name, text)                  # BASE RED control: no repair in F

    def test_overlay_changes_are_confined(self):
        a, b = tree('BASE'), tree(OVERLAY)
        self.assertEqual(dump(method(a, 'MLXRuntimePort', '_execute_bound')), dump(method(b, 'MLXRuntimePort', '_execute_bound')))
        self.assertEqual(dump(method(a, 'MLXRuntimePort', '_runtime_result')), dump(method(b, 'MLXRuntimePort', '_runtime_result')))
        self.assertEqual(dump(top(a, '_qwen2_attend')), dump(top(b, '_qwen2_attend')))
        self.assertEqual(dump(top(a, '_qwen2_linear')), dump(top(b, '_qwen2_linear')))
        changed = set()
        for n in b.body:
            name = getattr(n, 'name', None)
            if name and name != 'MLXRuntimePort':
                old = next((x for x in a.body if getattr(x, 'name', None) == name), None)
                if old is None or dump(old) != dump(n):
                    changed.add(name)
        for n in top(b, 'MLXRuntimePort').body:
            name = getattr(n, 'name', None)
            old = next((x for x in top(a, 'MLXRuntimePort').body if getattr(x, 'name', None) == name), None)
            if name and (old is None or dump(old) != dump(n)):
                changed.add('MLXRuntimePort.' + name)
        # v0.1.2 intentionally adds stage materialization without changing the
        # A11 arithmetic kernels: the property fixes the mode for a port's
        # lifetime; kv_snapshot names the measured work unit; _finish_stage_work
        # materializes/fences the stage; the hybrid executor uses that same
        # stage boundary. Keep an EXACT set, not a subset or a blanket class
        # exemption. __init__ and _execute_stage_with_kv were already admitted.
        self.assertEqual(changed, {'_verify_arithmetic_mode', '_verify_rows', '_qwen2_attend_rows', '_qwen2_block_with_kv',
                                   'MLXRuntimePort.__init__', 'MLXRuntimePort._execute_stage_with_kv',
                                   'MLXRuntimePort.materialization', 'MLXRuntimePort.kv_snapshot',
                                   'MLXRuntimePort._finish_stage_work', 'MLXRuntimePort._execute_qwen35_stage'})

    def test_call_contract_positions_and_slices(self):
        src = OVERLAY.read_text()
        body = ast.unparse(top(tree(OVERLAY), '_qwen2_attend_rows'))
        for text in ('query[:, :, row:row + 1, :]', 'key[:, :, row:row + 1, :]', 'value[:, :, row:row + 1, :]', 'position + row'):
            self.assertIn(text, body)
        stage = ast.unparse(method(tree(OVERLAY), 'MLXRuntimePort', '_execute_stage_with_kv'))
        for text in ('position=position + row', 'all_positions=False', 'token_ids[:, row:row + 1]',
                     'hidden_states[:, row:row + 1, :]', 'row_serial_attention=verify_mode == VERIFY_ARITHMETIC_ROW_ATTENTION'):
            self.assertIn(text, stage)
        self.assertIn('self.verify_arithmetic = VERIFY_ARITHMETIC_AUTO', src)
        # mode gate is only entered for VERIFY (all_positions) spans longer than one row
        self.assertIn('if all_positions:', stage)
        self.assertIn('if span > 1:', stage)


class ModeSelection(unittest.TestCase):
    def test_modes(self):
        ns = load_helpers()
        mode = ns['_verify_arithmetic_mode']
        packed = 'persistent_packed_int8'
        self.assertEqual(mode('auto', 'qwen2', packed), 'row_attention')
        self.assertEqual(mode('auto', 'qwen3', packed), 'row_attention')
        for arch, ex in (('qwen2', 'per_call_float32_materialization'), ('qwen2', 'none'), ('gpt2', packed), ('qwen3_5', packed)):
            self.assertEqual(mode('auto', arch, ex), 'sequential_rows')   # never assume unverified row independence
        self.assertEqual(mode('sequential_rows', 'qwen2', packed), 'sequential_rows')
        self.assertEqual(mode('row_attention', 'qwen2', 'none'), 'row_attention')
        with self.assertRaises(Reject):
            mode('epsilon', 'qwen2', packed)


class NumericalDoubles(unittest.TestCase):
    def test_batched_span_double_fails_strict_bits_serial_rows_do_not(self):
        probs, values, prefix, span = make_case(find_seed())
        serial = serial_rows(probs, values)
        batched = batched_span(probs, values, prefix, span)
        unequal = [i for i in range(span) if bits(serial[i]) != bits(batched[i])]
        self.assertTrue(unequal, 'control: the span-kernel double must reproduce a bit-level failure')
        self.assertTrue(all(abs(serial[i] - batched[i]) < 1e-5 for i in range(span)))   # small drift is still not parity
        # row 0 (the FIRST verify row, attending prompt+itself) also differs: matches native accepted_prefix data
        native = __import__('json').loads(NATIVE.read_text())
        acc = next(c for c in native['kv_comparisons'] if c['phase'] == 'accepted_prefix')
        self.assertFalse(acc['bitwise_equal'])
        layer0 = [t for t in acc['tensors'] if t['layer'] == 0]
        self.assertTrue(all(t['bitwise_equal'] for t in layer0))      # layer-0 K/V equal: pre-attention path already row-stable
        self.assertTrue(all(not t['bitwise_equal'] for t in acc['tensors'] if t['layer'] >= 1))

    def test_native_localisation_layer0_equal_layer1_up_unequal_all_phases(self):
        native = __import__('json').loads(NATIVE.read_text())
        self.assertEqual(len(native['kv_comparisons']), 4)
        for c in native['kv_comparisons']:
            self.assertEqual(len(c['tensors']), 48)
            self.assertTrue(all(t['bitwise_equal'] for t in c['tensors'] if t['layer'] == 0))
            self.assertTrue(all(t['unequal_elements'] > 0 for t in c['tensors'] if t['layer'] >= 1))
            self.assertAlmostEqual(c['max_abs_deviation'], 3.4809112548828125e-05)
        # correction/following steps first differ at layer 2: layer-1 K/V of the DECODE row still equals serial
        pc = next(c for c in native['kv_comparisons'] if c['phase'] == 'post_correction_position_7')
        pa = next(c for c in native['kv_comparisons'] if c['phase'] == 'accepted_prefix')
        l1 = lambda c: [t['unequal_elements'] for t in c['tensors'] if t['layer'] == 1]
        l2 = lambda c: [t['unequal_elements'] for t in c['tensors'] if t['layer'] == 2]
        self.assertEqual(l1(pc), l1(pa))
        self.assertGreater(min(l2(pc)), max(l2(pa)))

    def test_overlay_attention_rows_feed_exactly_like_serial(self):
        ns = load_helpers()
        t = tree(OVERLAY)
        calls = []
        probs, values, prefix, span = make_case(find_seed())
        serial = serial_rows(probs, values)

        def attend(q, k, v, past, position, config):   # double of serial single-row attention
            calls.append((len(q.data), len(k.data), len(v.data), 0 if past is None else len(past[0].data), position))
            row = q.data[0]
            return Rows([serial[row]]), Rows((past[0].data if past else []) + k.data), Rows((past[1].data if past else []) + v.data)

        ns.update(mx=FakeMx, _qwen2_attend=attend)
        exec(compile(ast.Module([top(t, '_qwen2_attend_rows')], []), 'overlay-rows', 'exec'), ns)
        past = (Rows(list(range(prefix))), Rows(list(range(prefix))))
        snapshot = (list(past[0].data), list(past[1].data))
        q = Rows(range(span)); k = Rows(range(100, 100 + span)); v = Rows(range(200, 200 + span))
        out, all_k, all_v = ns['_qwen2_attend_rows'](q, k, v, past, prefix, {})
        self.assertEqual([bits(x) for x in out.data], [bits(x) for x in serial])
        self.assertEqual(calls, [(1, 1, 1, prefix + i, prefix + i) for i in range(span)])   # serial shapes + positions
        self.assertEqual(all_k.data, list(range(prefix)) + list(range(100, 100 + span)))
        self.assertEqual((past[0].data, past[1].data), snapshot)                          # prefix never mutated

    def test_sequential_rows_branch_matches_serial_and_not_old_span(self):
        ns = load_helpers()
        t = tree(OVERLAY)
        probs, values, prefix, span = make_case(find_seed())
        serial = serial_rows(probs, values)
        old_span = batched_span(probs, values, prefix, span)
        self.assertNotEqual([bits(x) for x in old_span], [bits(x) for x in serial])
        seen = []

        class Port:
            verify_arithmetic = 'auto'
            int8_execution = 'per_call_float32_materialization'
            materialization = 'per_layer'

            def _checkpoint_path(self, path_id):
                checkpoints.append(path_id)

            def _tensors_for(self, loaded):
                return {}

            def _execute_stage_with_kv(self, **kw):
                if kw['all_positions']:                       # the BASE path: one multi-row call
                    return Rows(old_span), {'layers': 'span'}
                seen.append((kw['position'], list(kw['token_ids'].data), kw['all_positions'], dict(kw['past_layers'])))
                row = kw['position'] - prefix
                layers = dict(kw['past_layers']); layers[row] = 'row-%d' % row
                return Rows([serial[row]]), layers

        fn = method(t, 'MLXRuntimePort', '_execute_stage_with_kv')
        ns.update(mx=FakeMx, time=time, HYBRID_STATE_ARCHITECTURES=frozenset(), VERIFY_ARITHMETIC_SEQUENTIAL_ROWS='sequential_rows',
                  VERIFY_ARITHMETIC_ROW_ATTENTION='row_attention')
        exec(compile(ast.Module([fn], []), 'overlay-stage', 'exec'), ns)
        stage = type('S', (), {'component_roles': (), 'layer_range': type('R', (), {'start_layer': 0, 'end_layer_exclusive': 24})()})()
        prior = {'rolled': 'back'}
        for materialization in ('per_layer', 'per_stage'):
            with self.subTest(materialization=materialization):
                seen.clear()
                checkpoints = []
                port = Port()
                port.materialization = materialization
                out, layers = ns['_execute_stage_with_kv'](port, stage=stage, loaded=object(), runtime={'architecture': 'qwen2', 'model_config': {}},
                                                           token_ids=Rows([5, 6, 7, 8]), hidden_states=None, position=prefix,
                                                           past_layers=prior, path_id='p', produce_logits=True, all_positions=True)
                self.assertEqual(checkpoints, ['p'])  # actual entry fence is reached in both modes
                self.assertEqual([bits(x) for x in out.data], [bits(x) for x in serial])
                self.assertEqual([s[:3] for s in seen], [(prefix + i, [5 + i], False) for i in range(span)])
                self.assertEqual(seen[0][3], {'rolled': 'back'})
                self.assertEqual(set(layers), {'rolled', 0, 1, 2, 3})            # carried state accumulates row by row
                self.assertEqual(prior, {'rolled': 'back'})                       # caller's cache prefix untouched

    def test_rewind_prefix_preservation_double(self):
        probs, values, prefix, span = make_case(find_seed())
        serial = serial_rows(probs, values)
        cache = list(range(prefix + span))           # state.layers axis after VERIFY
        watermark = prefix + 2                       # accepted prefix length (frozen _execute_bound slices [:position])
        past_view = cache[:watermark]
        self.assertEqual(past_view, list(range(watermark)))
        self.assertEqual(len(cache), prefix + span)  # slicing a view does not mutate stored state until output validates
        # With exact rows the retained rows 0..1 ARE the serial rows, so no recompute/rewrite of accepted KV is needed.
        retained = serial[:2]
        self.assertEqual([bits(x) for x in retained], [bits(x) for x in serial_rows(probs[:2], values)])


if __name__ == '__main__':
    unittest.main()
