"""ModelShape built from config.json + safetensors headers (model_shape_builder.py).

Golden: synthetic config/header fixtures laid out like the Hugging Face Qwen
checkpoints (tensor names and shapes derived from the config numbers of each
pinned revision, no weights, no network) must rebuild the five hand-written
``MODEL_SHAPES`` rows exactly.
"""
import copy
import json
import struct
from dataclasses import replace
from pathlib import Path

import pytest

from mycelium_a13_macos import model_resources as R
from mycelium_a13_macos.layer_split import model_layer_profile
from mycelium_a13_macos.model_shape_builder import (
    ShapeRefused, build_model_shape, shape_from_snapshot, shape_literal)
from mycelium_a13_macos.serving import (
    A9_ROUTE_MODEL, CANDIDATE_MODEL, QWEN3_0_6B_MODEL, QWEN3_8B_MODEL, QWEN3_14B_MODEL)

# config.json numbers of the five pinned revisions (HF) plus the layout facts a
# config does not carry: bias tensors, q/k norms, a stored tied lm_head.
QWEN2 = {'model_type': 'qwen2', 'tie_word_embeddings': False}
CONFIGS = {
    CANDIDATE_MODEL: dict(QWEN2, num_hidden_layers=28, hidden_size=3584, intermediate_size=18944,
                          num_attention_heads=28, num_key_value_heads=4, vocab_size=152064),
    A9_ROUTE_MODEL: dict(QWEN2, tie_word_embeddings=True, num_hidden_layers=24, hidden_size=896,
                         intermediate_size=4864, num_attention_heads=14, num_key_value_heads=2,
                         vocab_size=151936),
    QWEN3_0_6B_MODEL: {'model_type': 'qwen3', 'tie_word_embeddings': True, 'head_dim': 128,
                       'num_hidden_layers': 28, 'hidden_size': 1024, 'intermediate_size': 3072,
                       'num_attention_heads': 16, 'num_key_value_heads': 8, 'vocab_size': 151936},
    QWEN3_8B_MODEL: {'model_type': 'qwen3', 'tie_word_embeddings': False, 'head_dim': 128,
                     'num_hidden_layers': 36, 'hidden_size': 4096, 'intermediate_size': 12288,
                     'num_attention_heads': 32, 'num_key_value_heads': 8, 'vocab_size': 151936},
    QWEN3_14B_MODEL: {'model_type': 'qwen3', 'tie_word_embeddings': False, 'head_dim': 128,
                      'num_hidden_layers': 40, 'hidden_size': 5120, 'intermediate_size': 17408,
                      'num_attention_heads': 40, 'num_key_value_heads': 8, 'vocab_size': 151936},
}
#: (attention bias, q/k norm, stores a tied lm_head) per model, as the checkpoints are laid out.
LAYOUT = {CANDIDATE_MODEL: (True, False, False), A9_ROUTE_MODEL: (True, False, False),
          QWEN3_0_6B_MODEL: (False, True, True), QWEN3_8B_MODEL: (False, True, False),
          QWEN3_14B_MODEL: (False, True, False)}


def _header(config, *, bias, qk_norm, lm_head, dtype='BF16'):
    """Safetensors header records for a dense Qwen checkpoint of this config."""
    per = {'BF16': 2, 'F16': 2, 'F32': 4}[dtype]
    h, inter, vocab = config['hidden_size'], config['intermediate_size'], config['vocab_size']
    heads, kv = config['num_attention_heads'], config['num_key_value_heads']
    hd = config.get('head_dim', h // heads)
    tensors = {'model.embed_tokens.weight': (vocab, h), 'model.norm.weight': (h,)}
    if lm_head:
        tensors['lm_head.weight'] = (vocab, h)
    for i in range(config['num_hidden_layers']):
        p = f'model.layers.{i}.'
        tensors.update({p + 'self_attn.q_proj.weight': (heads * hd, h), p + 'self_attn.k_proj.weight': (kv * hd, h),
                        p + 'self_attn.v_proj.weight': (kv * hd, h), p + 'self_attn.o_proj.weight': (h, heads * hd),
                        p + 'mlp.gate_proj.weight': (inter, h), p + 'mlp.up_proj.weight': (inter, h),
                        p + 'mlp.down_proj.weight': (h, inter), p + 'input_layernorm.weight': (h,),
                        p + 'post_attention_layernorm.weight': (h,)})
        if bias:
            tensors.update({p + 'self_attn.q_proj.bias': (heads * hd,), p + 'self_attn.k_proj.bias': (kv * hd,),
                            p + 'self_attn.v_proj.bias': (kv * hd,)})
        if qk_norm:
            tensors.update({p + 'self_attn.q_norm.weight': (hd,), p + 'self_attn.k_norm.weight': (hd,)})
    records, offset = {}, 0
    for name, shape in tensors.items():
        size = per
        for d in shape:
            size *= d
        records[name] = {'dtype': dtype, 'shape': list(shape), 'data_offsets': [offset, offset + size]}
        offset += size
    return records


def _files(shape_row):
    """The HF tree listing of the pinned revision: weight files + the rest."""
    return {'model.safetensors': shape_row.snapshot_bytes - shape_row.non_weight_bytes,
            'tokenizer.json': shape_row.non_weight_bytes}


def _fixture(model, **changes):
    bias, qk_norm, lm_head = LAYOUT[model]
    row = R.model_shape(model)
    config = dict(CONFIGS[model])
    headers = _header(config, bias=bias, qk_norm=qk_norm, lm_head=lm_head or not config['tie_word_embeddings'])
    return row, config, headers, _files(row)


@pytest.mark.parametrize('model', list(CONFIGS), ids=lambda m: R.model_shape(m).label)
def test_header_built_shape_equals_the_hand_written_table_row(model):
    row, config, headers, files = _fixture(model)
    built = build_model_shape(row.label, config, headers, files)
    assert built == row  # every field, including layers / kv heads / head dim / biases / tied head
    assert built.layer_profile() == row.layer_profile()
    assert built.weight_source_bytes == row.weight_source_bytes


def test_five_registered_rows_are_covered():
    assert {R.model_shape(m) for m in CONFIGS} == set(R.MODEL_SHAPES.values())


def test_derives_head_dim_and_kv_heads_when_the_config_omits_them():
    row, config, headers, files = _fixture(CANDIDATE_MODEL)
    config.pop('num_key_value_heads')  # defaults to the attention heads
    with pytest.raises(ShapeRefused, match='model_config_mismatch'):  # k_proj says 4 kv heads
        build_model_shape(row.label, config, headers, files)
    assert build_model_shape(row.label, dict(CONFIGS[CANDIDATE_MODEL]), headers, files).head_dim == 128


def test_dtype_sets_bytes_per_parameter():
    row, config, _, _ = _fixture(A9_ROUTE_MODEL)
    for dtype, per in (('F16', 2), ('F32', 4)):
        headers = _header(config, bias=True, qk_norm=False, lm_head=False, dtype=dtype)
        shape = build_model_shape('x', config, headers, {'model.safetensors': 1_000_000})
        assert shape.source_bytes_per_parameter == per
        assert shape.weight_source_bytes == row.weight_source_bytes * per // 2


def test_mixed_dtypes_are_refused():
    row, config, headers, files = _fixture(A9_ROUTE_MODEL)
    size = 4 * config['hidden_size']
    headers['model.norm.weight'] = {'dtype': 'F32', 'shape': [config['hidden_size']], 'data_offsets': [0, size]}
    with pytest.raises(ShapeRefused, match='model_shape_dtype_unsupported'):
        build_model_shape(row.label, config, headers, files)


def test_snapshot_totals_come_from_the_file_listing():
    row, config, headers, _ = _fixture(QWEN3_8B_MODEL)
    shape = build_model_shape('Qwen3-8B', config, headers, {'a.safetensors': 100, 'b.safetensors': 50,
                                                           'config.json': 7, 'tokenizer.json': 3})
    assert (shape.snapshot_bytes, shape.non_weight_bytes) == (160, 10)


@pytest.mark.parametrize('mutate,code', [
    (lambda c, h: c.update(model_type='llama'), 'model_architecture_unsupported'),
    (lambda c, h: c.update(num_local_experts=8), 'model_architecture_unsupported'),
    (lambda c, h: c.update(num_hidden_layers=27), 'model_layer_out_of_range'),
    (lambda c, h: c.update(num_hidden_layers=29), 'model_header_invalid'),
    (lambda c, h: c.update(hidden_size=3000), 'model_config_invalid'),
    (lambda c, h: c.update(hidden_size=3640), 'model_config_mismatch'),
    (lambda c, h: c.update(vocab_size=1), 'model_config_mismatch'),
    (lambda c, h: c.update(intermediate_size=10), 'model_config_mismatch'),
    (lambda c, h: c.update(tie_word_embeddings='yes'), 'model_config_invalid'),
    (lambda c, h: h.__setitem__('model.layers.3.mlp.experts.0.w', h['model.norm.weight']), 'model_tensor_unsupported'),
    (lambda c, h: h.__setitem__('vision.tower', h['model.norm.weight']), 'model_tensor_unsupported'),
    (lambda c, h: h.pop('model.layers.5.mlp.up_proj.weight'), 'model_header_invalid'),
    (lambda c, h: h.pop('model.embed_tokens.weight'), 'model_header_invalid'),
    (lambda c, h: h.pop('lm_head.weight'), 'model_header_invalid'),
    (lambda c, h: h['model.layers.2.self_attn.q_proj.bias'].update(shape=[1], data_offsets=[0, 2]),
     'model_config_mismatch'),
    (lambda c, h: h['model.norm.weight'].update(data_offsets=[0, 3]), 'model_header_invalid'),
])
def test_unsupported_or_inconsistent_snapshots_are_refused_by_closed_code(mutate, code):
    row, config, headers, files = _fixture(CANDIDATE_MODEL)
    headers = copy.deepcopy(headers)
    headers['lm_head.weight'] = headers.get('lm_head.weight') or dict(headers['model.embed_tokens.weight'])
    mutate(config, headers)
    with pytest.raises(ShapeRefused, match=code):
        build_model_shape(row.label, config, headers, files)


def test_layers_must_share_one_tensor_set():
    row, config, headers, files = _fixture(QWEN3_0_6B_MODEL)
    headers.pop('model.layers.7.self_attn.q_norm.weight')
    with pytest.raises(ShapeRefused, match='model_header_invalid'):
        build_model_shape(row.label, config, headers, files)


def test_empty_label_or_files_are_refused():
    row, config, headers, files = _fixture(A9_ROUTE_MODEL)
    with pytest.raises(ShapeRefused, match='model_shape_label_invalid'):
        build_model_shape('', config, headers, files)
    with pytest.raises(ShapeRefused, match='model_files_invalid'):
        build_model_shape(row.label, config, headers, {})
    with pytest.raises(ShapeRefused, match='model_files_invalid'):
        build_model_shape(row.label, config, headers, {'tokenizer.json': 5})


def _write_snapshot(root: Path, config, headers, extra_files=None, shards=2):
    """A tiny on-disk snapshot: real safetensors headers, sparse tensor bodies."""
    names = list(headers)
    weight_map = {}
    for shard in range(shards):
        part = {n: headers[n] for n in names[shard::shards]}
        offset, rebased = 0, {}
        for n, r in part.items():
            size = r['data_offsets'][1] - r['data_offsets'][0]
            rebased[n] = dict(r, data_offsets=[offset, offset + size])
            offset += size
        body = json.dumps(rebased).encode()
        name = f'model-{shard + 1:05d}-of-{shards:05d}.safetensors'
        with (root / name).open('wb') as handle:
            handle.write(struct.pack('<Q', len(body)) + body)
            handle.truncate(8 + len(body) + offset)  # sparse tensor bytes
        weight_map.update({n: name for n in part})
    (root / 'model.safetensors.index.json').write_text(json.dumps({'weight_map': weight_map}))
    (root / 'config.json').write_text(json.dumps(config))
    for name, text in (extra_files or {}).items():
        (root / name).write_text(text)


TINY = {'model_type': 'qwen3', 'tie_word_embeddings': True, 'head_dim': 8, 'num_hidden_layers': 3,
        'hidden_size': 16, 'intermediate_size': 24, 'num_attention_heads': 4, 'num_key_value_heads': 2,
        'vocab_size': 40}


def test_shape_from_a_snapshot_directory_reads_headers_and_file_sizes(tmp_path):
    headers = _header(TINY, bias=False, qk_norm=True, lm_head=False)
    _write_snapshot(tmp_path, TINY, headers, {'tokenizer.json': '{}' * 50})
    shape = shape_from_snapshot('Tiny', tmp_path)
    assert (shape.num_layers, shape.hidden_size, shape.num_key_value_heads, shape.head_dim) == (3, 16, 2, 8)
    assert shape.qk_norm and not shape.attention_bias and shape.tie_word_embeddings
    files = {p.name: p.stat().st_size for p in tmp_path.iterdir()}
    assert shape.snapshot_bytes == sum(files.values())
    assert shape.non_weight_bytes == sum(s for n, s in files.items() if not n.endswith('.safetensors'))
    # The same profile the layer planner reads from the headers on disk.
    assert shape.layer_profile() == model_layer_profile(tmp_path)


def test_shape_literal_round_trips_through_eval():
    row = R.model_shape(QWEN3_0_6B_MODEL)
    assert eval(shape_literal(row), {'ModelShape': R.ModelShape}) == row
    plain = R.model_shape(CANDIDATE_MODEL)
    assert eval(shape_literal(plain), {'ModelShape': R.ModelShape}) == plain


def _local_snapshot(model):
    path = Path.home() / '.cache/huggingface/hub' / ('models--' + model.model_id.replace('/', '--')) \
        / 'snapshots' / model.resolved_commit
    return path if (path / 'config.json').is_file() else None


@pytest.mark.parametrize('model', [QWEN3_0_6B_MODEL, QWEN3_14B_MODEL], ids=lambda m: m.model_id)
def test_real_local_snapshot_headers_rebuild_the_table_row(model):
    path = _local_snapshot(model)
    if path is None:
        pytest.skip('pinned snapshot not in the local HF cache')
    row = R.model_shape(model)
    built = shape_from_snapshot(row.label, path)
    # The local copy may omit README/.gitattributes: compare the header-derived fields only.
    assert replace(built, snapshot_bytes=row.snapshot_bytes, non_weight_bytes=row.non_weight_bytes) == row
    assert built.layer_profile() == model_layer_profile(path)


def _extra_model():
    from mycelium_a13_macos.serving import ModelContract
    return ModelContract('Qwen/Qwen3-1.7B', 'f' * 40, 'bfloat16-safetensors', 'mlx', 'float32', 'int8-weight-only')


def test_a_derived_shape_registers_without_a_table_row():
    model = _extra_model()
    assert R.model_shape(model) is None
    config = {'model_type': 'qwen3', 'tie_word_embeddings': True, 'head_dim': 128, 'num_hidden_layers': 28,
              'hidden_size': 2048, 'intermediate_size': 6144, 'num_attention_heads': 16,
              'num_key_value_heads': 8, 'vocab_size': 151936}
    shape = build_model_shape('Qwen3-1.7B', config, _header(config, bias=False, qk_norm=True, lm_head=False),
                              {'model.safetensors': 4_000_000_000, 'tokenizer.json': 20_000_000})
    try:
        R.register_shape(model, shape)
        assert R.model_shape(model) is shape
        R.register_shape(model, shape)  # idempotent
        assert R.model_resource_proposal(model).network_download_bytes == shape.snapshot_bytes
        assert R.owner_storage_requirement(model) == -(-110 * shape.snapshot_bytes // 100)
        with pytest.raises(R.ResourceRefused, match='model_shape_conflict'):
            R.register_shape(model, replace(shape, num_layers=29))
    finally:
        R.unregister_shape(model)
    assert R.model_shape(model) is None


def test_a_hand_written_row_cannot_be_replaced_by_a_different_derived_shape():
    row = R.model_shape(QWEN3_8B_MODEL)
    R.register_shape(QWEN3_8B_MODEL, row)  # the same shape is a no-op
    with pytest.raises(R.ResourceRefused, match='model_shape_conflict'):
        R.register_shape(QWEN3_8B_MODEL, replace(row, num_key_value_heads=4))
    assert R.model_shape(QWEN3_8B_MODEL) is row
