# SPDX-License-Identifier: AGPL-3.0-only
"""Fresh compilation derives executable source closure, not fleet templates."""
from scripts import build_qwen_live_route as builder

def test_runtime_closure_follows_project_imports_without_extra_files(tmp_path,monkeypatch):
    monkeypatch.setattr(builder,'_RUNTIME_MODULE_CLOSURE',())
    monkeypatch.setattr(builder,'_RUNTIME_PACKAGE_CLOSURE',())
    (tmp_path/'physical_inference_node.py').write_text('import model_adapters\n')
    (tmp_path/'weight_quantization.py').write_text('# declared unit fixture\n')
    (tmp_path/'model_adapters.py').write_text('from model_manifest import Model\n')
    (tmp_path/'model_manifest.py').write_text('# declared unit fixture\n')
    (tmp_path/'unrelated.py').write_text('# must not be copied\n')
    result=builder.runtime_source_paths(tmp_path)
    assert set(result)=={'physical_inference_node.py','weight_quantization.py','model_adapters.py','model_manifest.py'}
    output=tmp_path/'bundle';output.mkdir()
    builder._copy_runtime_closure(tmp_path,output,{'controller':{'transfer_manifest':{'files':[]}}})
    assert {p.name for p in output.iterdir()}==set(result)
