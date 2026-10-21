# SPDX-License-Identifier: AGPL-3.0-only
"""O3 is a deterministic informational report, never a qualification decision."""
import copy
import json
from pathlib import Path

import pytest

from scripts import run_a11_route_benchmark as runner


def fixture_document():
    records, windows = [], []
    for mode in runner.WINDOW_ORDER:
        window_records = []
        for shape in runner.window_workload():
            wall = (1000 + shape.index) * (0.8 if mode == 'speculative' else 1)
            window_records.append({
                'shape': shape.key, 'mode': mode, 'outcome': 'completed',
                'tokens': shape.max_new_tokens, 'ttft_ms': 10.0, 'tpot_ms': 1.0,
                'wall_ms': wall, 'output_digest': 'sha256:' + 'a' * 64, 'speculation': None,
            })
        records.extend(window_records)
        windows.append({
            'mode': mode, 'completed_requests': len(window_records),
            'target_committed_output_tokens': sum(r['tokens'] for r in window_records),
            'prompt_length_buckets': 2, 'output_length_buckets': 2,
            'wall_clock_ms': round(sum(r['wall_ms'] for r in window_records), 3),
            'ttft_p95_ms': 10.0, 'error_timeout_cancellation_fallback_count': 0,
        })
    return {
        'protocol': runner.PROTOCOL, 'evidence_class': 'product_route',
        'windows': windows, 'requests': records, 'parity_failures': 0,
        'benchmark': runner.decide(windows, plan_digest='sha256:' + 'b' * 64),
    }


def test_request_bootstrap_is_reproducible_and_never_changes_decision():
    from scripts.report_a11_request_bootstrap import report
    doc = fixture_document()
    before = copy.deepcopy(doc)
    first = report(doc)
    assert first == report(doc)
    assert doc == before
    assert first['informational_only'] is True
    assert first['request_pairs'] == 36
    assert first['resamples'] == 10_000
    assert first['seed'] == runner.BOOTSTRAP_SEED
    assert first['observed_reduction_fraction'] == pytest.approx(1 - 0.8)
    assert first['lower_bound_fraction'] == pytest.approx(1 - 0.8)
    assert first['benchmark_digest'] == doc['benchmark']['benchmark_digest']
    assert 'decision' not in first and 'qualified_enabled' not in first


@pytest.mark.parametrize('change', [
    'quick', 'duplicate_shape', 'wrong_mode', 'incomplete', 'unequal_tokens',
    'parity', 'nonfinite', 'zero_wall', 'aggregate_mismatch', 'tampered_seal',
])
def test_invalid_or_unpaired_request_records_are_refused(change):
    from scripts.report_a11_request_bootstrap import report, ReportRefused
    doc = fixture_document()
    if change == 'quick':
        doc['requests'] = doc['requests'][:24]
    elif change == 'duplicate_shape':
        doc['requests'][1]['shape'] = doc['requests'][0]['shape']
    elif change == 'wrong_mode':
        doc['requests'][0]['mode'] = 'speculative'
    elif change == 'incomplete':
        doc['requests'][0]['outcome'] = 'failed'
    elif change == 'unequal_tokens':
        doc['requests'][0]['tokens'] -= 1
    elif change == 'parity':
        doc['requests'][12]['output_digest'] = 'sha256:' + 'c' * 64
    elif change == 'nonfinite':
        doc['requests'][0]['wall_ms'] = float('nan')
    elif change == 'zero_wall':
        doc['requests'][0]['wall_ms'] = 0
    elif change == 'aggregate_mismatch':
        doc['requests'][0]['wall_ms'] += 100
    elif change == 'tampered_seal':
        doc['benchmark']['decision']['state'] = 'tampered'
    with pytest.raises(ReportRefused):
        report(doc)


def test_cli_preserves_input_and_refuses_duplicate_json_keys(tmp_path: Path, capsys):
    from scripts.report_a11_request_bootstrap import main
    source = tmp_path / 'benchmark.json'
    out = tmp_path / 'informational.json'
    source.write_text(json.dumps(fixture_document()))
    before = source.read_bytes()
    assert main(['--input', str(source), '--output', str(out)]) == 0
    assert source.read_bytes() == before
    result = json.loads(out.read_text())
    assert result['informational_only']
    assert result['input_sha256'].startswith('sha256:')
    source.write_text('{"requests": [], "requests": []}')
    assert main(['--input', str(source), '--output', str(tmp_path/'invalid.json')]) != 0
    assert not (tmp_path/'invalid.json').exists()


@pytest.mark.parametrize('destination', ['input', 'existing', 'symlink'])
def test_cli_never_overwrites_input_or_existing_output(tmp_path: Path, destination):
    from scripts.report_a11_request_bootstrap import main
    source = tmp_path / 'benchmark.json'
    source.write_text(json.dumps(fixture_document()))
    sentinel = tmp_path / 'retained.json'
    sentinel.write_text('retained evidence\n')
    if destination == 'input':
        output = source
    elif destination == 'existing':
        output = sentinel
    else:
        output = tmp_path / 'output-link.json'
        output.symlink_to(sentinel)
    before = source.read_bytes()
    assert main(['--input', str(source), '--output', str(output)]) == 2
    assert source.read_bytes() == before
    assert sentinel.read_text() == 'retained evidence\n'
    if destination == 'symlink':
        assert output.is_symlink()


def test_cli_output_is_private_and_omits_raw_inputs(tmp_path: Path):
    import stat
    from scripts.report_a11_request_bootstrap import main
    source = tmp_path / 'benchmark.json'
    output = tmp_path / 'report.json'
    document = fixture_document()
    document['private_debug'] = 'PRIVATE_TEST_SENTINEL'
    source.write_text(json.dumps(document))
    assert main(['--input', str(source), '--output', str(output)]) == 0
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert 'PRIVATE_TEST_SENTINEL' not in output.read_text()
    assert str(tmp_path) not in output.read_text()


def test_direct_cli_works_outside_repository(tmp_path: Path):
    import os
    import subprocess
    import sys
    source = tmp_path / 'benchmark.json'
    output = tmp_path / 'report.json'
    source.write_text(json.dumps(fixture_document()))
    script = Path(__file__).resolve().parents[2] / 'scripts/report_a11_request_bootstrap.py'
    environment = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
    result = subprocess.run(
        [sys.executable, '-B', str(script), '--input', str(source), '--output', str(output)],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['informational_only'] is True
    assert json.loads(output.read_text())['request_pairs'] == 36
