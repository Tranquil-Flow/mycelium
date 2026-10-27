# SPDX-License-Identifier: AGPL-3.0-only
"""Cancellation overtakes an already-produced prefix at the real TLS boundary."""
import threading

from test_inflight_route_stop import stack
from test_inflight_progress_stop import native_progress_transport


def test_stop_after_owner_progress_close_still_collects_native_cleanup(tmp_path, monkeypatch):
    pending_http = threading.Event()
    release_http = threading.Event()
    stream_closed = threading.Event()
    native_results = []

    def configured(world, route, worker):
        cursor = native_progress_transport(world, route, worker)
        send_progress = world['target_client'].token_progress
        close_progress = world['mailbox'].close_token_progress
        native_send = worker.send

        def held_progress(**kwargs):
            if kwargs['receipt']['statement']['sequence'] == 1:
                pending_http.set()
                assert release_http.wait(5)
            return send_progress(**kwargs)

        def observed_close(*args, **kwargs):
            close_progress(*args, **kwargs)
            stream_closed.set()

        def observed_native(**kwargs):
            result = native_send(**kwargs)
            native_results.append((kwargs['command'], result))
            return result

        monkeypatch.setattr(world['target_client'], 'token_progress', held_progress)
        monkeypatch.setattr(world['mailbox'], 'close_token_progress', observed_close)
        worker.send = observed_native
        return cursor

    with stack(tmp_path, monkeypatch, held_command='streaming',
               progress_factory=configured, restarting_owner=True) as t:
        subscription = t.service.subscribe(t.request_id, last_event_id=None)
        try:
            tokens = []
            while len(tokens) < 8:
                event = subscription.next_event(timeout=3)
                assert event is not None, t.errors
                subscription.ack(event.sequence)
                if event.kind == 'token':
                    tokens.append(event)
                assert event.kind not in {'failed', 'cancelled', 'completed'}
            assert pending_http.wait(3)
            pending = t.router._pending[t.request_id]
            assert t.router.cancel(t.request_id)
            # L15 released immediately after scheduling cancel; this barrier
            # forces the real owner finally/stream removal to win the race.
            assert stream_closed.wait(3)
            assert not t.world['mailbox']._token_progress_streams
            release_http.set()
            t.service.close()
            try:
                t.router.close()
            except RuntimeError:
                print('pump failures:', [(str(e), getattr(e, 'cause_code', None)) for e in t.errors])
                print('native results:', [(c, r.get('ok'), r.get('error')) for c, r in native_results])
                raise
            assert not t.errors
            assert t.worker.cancelled.is_set()
            assert t.worker.commands.count('infer_start') == 1
            assert not any(thread.is_alive() for thread in t.router._worker_pool._threads)
            assert not t.router._pending
            assert pending.terminal_status == 'CANCELLED'
            assert any(row['observation']['event'] == 'inference_cancelled'
                       and row['observation']['details']['request_cleanup']['complete'] is True
                       for row in t.route._signed_observations)
            assert any(command == 'infer_start' and result['ok'] is False
                       and result['error']['code'] == 'stale_infer_decode_generation'
                       for command, result in native_results)
        finally:
            release_http.set()
            subscription.close()
