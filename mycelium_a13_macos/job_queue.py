# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV9 owner job queue: per-request priority, bounded, durable, results delivered later.

Two priorities: ``interactive`` always runs before any queued ``batch`` job (FIFO inside a class).
A batch job starts only when the caller says batch work is allowed (Batch mode, sharing allowed, route
ready) and no interactive request is running or waiting.  Each job is one owner-only JSON file, so a
restart keeps the queue and results; a job that was running when the app stopped is queued again.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable

PRIORITIES = ("interactive", "batch")
STATES = ("queued", "running", "done", "failed", "cancelled")
MAX_PROMPT_CHARS = 20_000
MAX_NEW_TOKENS = 1024
MAX_RESULT_CHARS = 200_000
_CODE = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_ID = re.compile(r"[A-Za-z0-9_-]{16,64}\Z")


class JobQueueError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class JobQueue:
    def __init__(self, directory: str | os.PathLike[str], *, clock: Callable[[], float] = time.time,
                 max_queued: int = 200, max_kept: int = 200) -> None:
        self._dir = Path(directory)
        self._clock = clock
        self._max_queued = max_queued
        self._max_kept = max_kept
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._seq = 0
        self._load()

    # ---------------------------------------------------------------- persistence
    def _ensure_dir(self) -> None:
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _path(self, job_id: str) -> Path:
        return self._dir / f"{job_id}.json"

    def _save(self, job: dict[str, Any]) -> None:
        self._ensure_dir()
        path = self._path(job["id"])
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(job, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load(self) -> None:
        try:
            names = sorted(os.listdir(self._dir))
        except OSError:
            return
        for name in names:
            if not name.endswith(".json") or name.startswith("."):
                continue
            path = self._dir / name
            try:
                info = os.lstat(path)
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 2_000_000:
                    continue
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (not isinstance(job, dict) or job.get("state") not in STATES
                    or job.get("priority") not in PRIORITIES or not _ID.fullmatch(str(job.get("id", "")))):
                continue
            if job["state"] == "running":
                job["state"] = "queued"
                job["tokens"] = 0
            self._jobs[job["id"]] = job
            self._seq = max(self._seq, int(job.get("seq", 0)))

    def _prune(self) -> None:
        finished = sorted((j for j in self._jobs.values() if j["state"] in ("done", "failed", "cancelled")),
                          key=lambda j: j["seq"])
        for job in finished[:max(0, len(finished) - self._max_kept)]:
            self._jobs.pop(job["id"], None)
            self._path(job["id"]).unlink(missing_ok=True)

    # ---------------------------------------------------------------- API
    def submit(self, prompt: Any, max_new_tokens: Any, *, priority: Any = "batch") -> dict[str, Any]:
        if type(prompt) is not str or not prompt.strip():
            raise JobQueueError("batch_prompt_invalid")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise JobQueueError("batch_prompt_too_long")
        if type(max_new_tokens) is not int or not 1 <= max_new_tokens <= MAX_NEW_TOKENS:
            raise JobQueueError("batch_max_new_tokens_invalid")
        if type(priority) is not str or priority not in PRIORITIES:
            raise JobQueueError("batch_priority_invalid")
        with self._lock:
            if priority == "batch" and sum(
                    1 for j in self._jobs.values() if j["state"] == "queued" and j["priority"] == "batch"
            ) >= self._max_queued:
                raise JobQueueError("batch_queue_full")
            self._seq += 1
            job = {"id": secrets.token_urlsafe(18), "seq": self._seq, "priority": priority, "state": "queued",
                   "prompt": prompt, "max_new_tokens": max_new_tokens, "submitted_at": self._clock(),
                   "started_at": None, "finished_at": None, "tokens": 0, "result": None, "error": None}
            self._jobs[job["id"]] = job
            self._save(job)
            return self._row(job)

    def next_runnable(self, *, batch_allowed: bool, interactive_busy: bool, in_flight: int,
                      concurrency: int) -> dict[str, Any] | None:
        """The next job to start, or ``None``.  Interactive first; batch only when nothing interactive competes."""
        with self._lock:
            queued = sorted((j for j in self._jobs.values() if j["state"] == "queued"),
                            key=lambda j: (PRIORITIES.index(j["priority"]), j["seq"]))
            if not queued or in_flight >= max(1, concurrency):
                return None
            head = queued[0]
            if head["priority"] == "interactive":
                return dict(head)
            interactive_running = any(j["state"] == "running" and j["priority"] == "interactive"
                                      for j in self._jobs.values())
            if not batch_allowed or interactive_busy or interactive_running:
                return None
            return dict(head)

    def _get(self, job_id: Any) -> dict[str, Any]:
        job = self._jobs.get(job_id) if type(job_id) is str else None
        if job is None:
            raise JobQueueError("batch_job_unknown")
        return job

    def mark_running(self, job_id: str) -> None:
        with self._lock:
            job = self._get(job_id)
            job["state"], job["started_at"] = "running", self._clock()
            self._save(job)

    def append_tokens(self, job_id: str, pieces: list[str]) -> None:
        with self._lock:
            job = self._get(job_id)
            job["tokens"] += len(pieces)
            job["partial"] = (job.get("partial", "") + "".join(pieces))[:MAX_RESULT_CHARS]

    def complete(self, job_id: str, result: str) -> None:
        with self._lock:
            job = self._get(job_id)
            if job["state"] == "cancelled":
                return
            job.pop("partial", None)
            job.update(state="done", result=str(result)[:MAX_RESULT_CHARS], finished_at=self._clock())
            self._save(job)
            self._prune()

    def fail(self, job_id: str, code: Any) -> None:
        with self._lock:
            job = self._get(job_id)
            if job["state"] == "cancelled":
                return
            job.pop("partial", None)
            job.update(state="failed", finished_at=self._clock(),
                       error=code if isinstance(code, str) and _CODE.fullmatch(code) else "batch_job_failed")
            self._save(job)
            self._prune()

    def cancel(self, job_id: Any) -> str:
        with self._lock:
            job = self._get(job_id)
            if job["state"] == "cancelled":
                return "cancelled"
            if job["state"] not in ("queued", "running"):
                raise JobQueueError("batch_job_not_cancellable")
            job.update(state="cancelled", finished_at=self._clock())
            self._save(job)
            self._prune()
            return "cancelled"

    def get(self, job_id: Any) -> dict[str, Any]:
        with self._lock:
            return dict(self._get(job_id))

    def counts(self) -> dict[str, int]:
        with self._lock:
            counts = {state: 0 for state in STATES}
            for job in self._jobs.values():
                counts[job["state"]] += 1
            return counts

    def in_flight(self) -> int:
        return self.counts()["running"]

    @staticmethod
    def _row(job: dict[str, Any]) -> dict[str, Any]:
        row = {k: job[k] for k in ("id", "priority", "state", "max_new_tokens", "submitted_at",
                                   "started_at", "finished_at", "tokens")}
        if job.get("error"):
            row["error"] = job["error"]
        return row

    def list(self, limit: int = 50) -> dict[str, Any]:
        with self._lock:
            rows = [self._row(j) for j in sorted(self._jobs.values(), key=lambda j: j["seq"])]
            return {"counts": self.counts(), "jobs": rows[-limit:]}
