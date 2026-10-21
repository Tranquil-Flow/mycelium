# SPDX-License-Identifier: AGPL-3.0-only
"""Criterion 6's arithmetic must be right or the published claim sheet is fiction.

The round's own events carry no timestamps, so timing comes from the RECEIVING end -- which means the
maths here is the entire measurement. These tests pin the definitions, including the case that would
otherwise publish a lie: a single-token stream has NO inter-token interval, so TPOT must be None
rather than 0.0 (0.0 would read as an impossibly fast measurement), and an incomplete stream must
report failure rather than a number.
"""
import importlib.util
import pathlib
import sys

import pytest

OPERATOR = pathlib.Path(__file__).resolve().parents[2] / 'scripts/a13_operator'
_spec = importlib.util.spec_from_file_location('wp8_05_measure_stream',
                                               OPERATOR / 'wp8_05_measure_stream.py')
measure = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(measure)


def _stream(*, accepted=0.0, token_times=(), terminal_at=None):
    samples = [({'type': 'accepted'}, accepted)]
    for index, when in enumerate(token_times):
        samples.append(({'type': 'token', 'text': 't%d' % index, 'token_index': index}, when))
    if terminal_at is not None:
        samples.append(({'type': 'completed'}, terminal_at))
    return samples


def test_ttft_is_accept_to_first_token():
    result = measure.summarize(_stream(accepted=10.0, token_times=(12.5, 13.0, 13.5),
                                       terminal_at=13.6))
    assert result['ttft_s'] == pytest.approx(2.5)


def test_tpot_is_the_mean_inter_token_interval_after_the_first():
    # 3 tokens at 12.5, 13.0, 13.5 -> (13.5 - 12.5) / (3 - 1) = 0.5
    result = measure.summarize(_stream(accepted=10.0, token_times=(12.5, 13.0, 13.5),
                                       terminal_at=13.6))
    assert result['tpot_s'] == pytest.approx(0.5)


def test_a_single_token_stream_reports_no_tpot_rather_than_zero():
    """The case that would otherwise publish an impossible number."""
    result = measure.summarize(_stream(accepted=0.0, token_times=(9.0,), terminal_at=9.1))
    assert result['ok'] is True
    assert result['tokens'] == 1
    assert result['tpot_s'] is None
    assert result['ttft_s'] == pytest.approx(9.0)


def test_an_incomplete_stream_fails_rather_than_reporting_a_time():
    assert measure.summarize(_stream(accepted=0.0, token_times=()))['ok'] is False
    # Tokens with NO accept event: built by hand, because _stream() always emits an accepted event
    # (accepted defaults to 0.0). The first draft of this test passed accepted=0.0 by default and so
    # asserted nothing about the missing-accept case -- a fixture bug, fixed here rather than by
    # loosening the implementation to match it.
    tokens_without_accept = [({'type': 'token', 'text': 't0', 'token_index': 0}, 1.0),
                             ({'type': 'completed'}, 1.1)]
    assert measure.summarize(tokens_without_accept)['ok'] is False


def test_the_terminal_type_is_carried_through():
    """A cancelled or failed terminal must never be summarised as a successful measurement."""
    for kind in ('completed', 'cancelled', 'failed'):
        samples = _stream(accepted=0.0, token_times=(1.0, 2.0))
        samples.append(({'type': kind}, 2.1))
        assert measure.summarize(samples)['terminal'] == kind


def test_the_text_is_reassembled_in_order():
    result = measure.summarize(_stream(accepted=0.0, token_times=(1.0, 2.0, 3.0),
                                       terminal_at=3.1))
    assert result['text'] == 't0t1t2'


def test_timing_never_reads_a_wall_clock_it_was_not_given():
    """A clock is injected, not sampled: the summary must be reproducible from the samples alone."""
    samples = _stream(accepted=100.0, token_times=(101.0, 102.0), terminal_at=102.5)
    first = measure.summarize(list(samples))
    second = measure.summarize(list(samples))
    assert first == second
