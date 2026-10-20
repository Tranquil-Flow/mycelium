"""PV9: the owner's job queue - per-request priority, bounded, durable, results delivered later."""

from __future__ import annotations

import json
import os

import pytest

from mycelium_a13_macos.job_queue import JobQueue, JobQueueError


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def make(tmp_path, **kw):
    clock = Clock()
    return JobQueue(tmp_path / "jobs", clock=clock, **kw), clock


def submit(queue, clock, prompt, priority="batch", **kw):
    clock.now += 1
    return queue.submit(prompt, 16, priority=priority, **kw)["id"]


def test_interactive_jumps_every_queued_batch_job_and_order_is_fifo_within_a_class(tmp_path):
    queue, clock = make(tmp_path)
    b1 = submit(queue, clock, "b1")
    b2 = submit(queue, clock, "b2")
    i1 = submit(queue, clock, "i1", priority="interactive")
    b3 = submit(queue, clock, "b3")
    i2 = submit(queue, clock, "i2", priority="interactive")
    order = []
    while True:
        job = queue.next_runnable(batch_allowed=True, interactive_busy=False, in_flight=0, concurrency=1)
        if job is None:
            break
        order.append(job["id"])
        queue.mark_running(job["id"])
        queue.complete(job["id"], "ok")
    assert order == [i1, i2, b1, b2, b3]


def test_batch_jobs_wait_while_sharing_is_not_allowed_but_interactive_ones_do_not(tmp_path):
    queue, clock = make(tmp_path)
    b = submit(queue, clock, "b")
    assert queue.next_runnable(batch_allowed=False, interactive_busy=False, in_flight=0, concurrency=1) is None
    i = submit(queue, clock, "i", priority="interactive")
    job = queue.next_runnable(batch_allowed=False, interactive_busy=False, in_flight=0, concurrency=1)
    assert job["id"] == i
    queue.mark_running(i)
    queue.complete(i, "done")
    assert queue.next_runnable(batch_allowed=False, interactive_busy=False, in_flight=0, concurrency=1) is None
    assert queue.next_runnable(batch_allowed=True, interactive_busy=False, in_flight=0, concurrency=1)["id"] == b


def test_no_batch_job_starts_while_an_interactive_request_is_busy_or_queued(tmp_path):
    queue, clock = make(tmp_path)
    b = submit(queue, clock, "b")
    assert queue.next_runnable(batch_allowed=True, interactive_busy=True, in_flight=0, concurrency=1) is None
    i = submit(queue, clock, "i", priority="interactive")
    assert queue.next_runnable(batch_allowed=True, interactive_busy=False, in_flight=0, concurrency=1)["id"] == i
    queue.mark_running(i)
    # an interactive job is running: the queued batch job does not start beside it
    assert queue.next_runnable(batch_allowed=True, interactive_busy=False, in_flight=1, concurrency=2) is None
    queue.complete(i, "x")
    assert queue.next_runnable(batch_allowed=True, interactive_busy=False, in_flight=0, concurrency=1)["id"] == b


def test_concurrency_limit_is_respected(tmp_path):
    queue, clock = make(tmp_path)
    ids = [submit(queue, clock, f"b{n}") for n in range(3)]
    assert queue.next_runnable(batch_allowed=True, interactive_busy=False, in_flight=1, concurrency=1) is None
    first = queue.next_runnable(batch_allowed=True, interactive_busy=False, in_flight=0, concurrency=2)
    assert first["id"] == ids[0]
    queue.mark_running(first["id"])
    second = queue.next_runnable(batch_allowed=True, interactive_busy=False, in_flight=1, concurrency=2)
    assert second["id"] == ids[1]


def test_results_are_kept_for_later_and_listed_without_prompts(tmp_path):
    queue, clock = make(tmp_path)
    job = submit(queue, clock, "secret prompt text")
    queue.mark_running(job)
    queue.append_tokens(job, ["he", "llo"])
    queue.complete(job, "hello")
    listed = queue.list()
    assert listed["counts"] == {"queued": 0, "running": 0, "done": 1, "failed": 0, "cancelled": 0}
    row = listed["jobs"][0]
    assert row["state"] == "done" and row["priority"] == "batch" and row["id"] == job
    assert "prompt" not in row and "result" not in row
    full = queue.get(job)
    assert full["result"] == "hello" and full["prompt"] == "secret prompt text"
    assert "secret prompt text" not in json.dumps(listed)


def test_jobs_survive_a_restart_and_running_jobs_requeue(tmp_path):
    queue, clock = make(tmp_path)
    a = submit(queue, clock, "a")
    b = submit(queue, clock, "b")
    queue.mark_running(a)
    del queue
    again, _ = make(tmp_path)
    states = {row["id"]: row["state"] for row in again.list()["jobs"]}
    assert states == {a: "queued", b: "queued"}
    for name in os.listdir(tmp_path / "jobs"):
        assert (tmp_path / "jobs" / name).stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "jobs").stat().st_mode & 0o777 == 0o700
    assert again.next_runnable(batch_allowed=True, interactive_busy=False, in_flight=0, concurrency=1)["id"] == a


def test_cancel_only_queued_or_running_jobs(tmp_path):
    queue, clock = make(tmp_path)
    a = submit(queue, clock, "a")
    assert queue.cancel(a) == "cancelled"
    assert queue.cancel(a) == "cancelled"  # idempotent
    assert queue.next_runnable(batch_allowed=True, interactive_busy=False, in_flight=0, concurrency=1) is None
    done = submit(queue, clock, "d")
    queue.mark_running(done)
    queue.complete(done, "x")
    with pytest.raises(JobQueueError, match="batch_job_not_cancellable"):
        queue.cancel(done)
    with pytest.raises(JobQueueError, match="batch_job_unknown"):
        queue.cancel("nope")


def test_validation_and_bounds(tmp_path):
    queue, clock = make(tmp_path, max_queued=2, max_kept=3)
    for bad in ("", "   ", 5, None):
        with pytest.raises(JobQueueError, match="batch_prompt_invalid"):
            queue.submit(bad, 16)
    with pytest.raises(JobQueueError, match="batch_prompt_too_long"):
        queue.submit("x" * 20001, 16)
    for bad in (0, -1, True, "5", 5000):
        with pytest.raises(JobQueueError, match="batch_max_new_tokens_invalid"):
            queue.submit("ok", bad)
    with pytest.raises(JobQueueError, match="batch_priority_invalid"):
        queue.submit("ok", 16, priority="urgent")
    submit(queue, clock, "a")
    submit(queue, clock, "b")
    with pytest.raises(JobQueueError, match="batch_queue_full"):
        queue.submit("c", 16)
    # interactive-priority jobs are not blocked by a queue full of batch jobs
    submit(queue, clock, "i", priority="interactive")


def test_finished_jobs_beyond_max_kept_are_pruned_oldest_first_never_queued_ones(tmp_path):
    queue, clock = make(tmp_path, max_queued=50, max_kept=3)
    done = []
    for n in range(5):
        job = submit(queue, clock, f"d{n}")
        queue.mark_running(job)
        queue.complete(job, "r")
        done.append(job)
    waiting = submit(queue, clock, "waiting")
    listed = queue.list()
    kept = {row["id"] for row in listed["jobs"]}
    assert kept == set(done[-3:]) | {waiting}
    assert len(os.listdir(tmp_path / "jobs")) == 4


def test_failed_job_records_a_bounded_code_not_text(tmp_path):
    queue, clock = make(tmp_path)
    job = submit(queue, clock, "a")
    queue.mark_running(job)
    queue.fail(job, "chat_route_not_qualified")
    row = queue.list()["jobs"][0]
    assert row["state"] == "failed" and row["error"] == "chat_route_not_qualified"
    other = submit(queue, clock, "b")
    queue.mark_running(other)
    queue.fail(other, "Some long free text that is not a code!")
    assert queue.get(other)["error"] == "batch_job_failed"
