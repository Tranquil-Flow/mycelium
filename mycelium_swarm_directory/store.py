# SPDX-License-Identifier: AGPL-3.0-only
"""Four-method KV storage behind the directory (``get``/``put``/``delete``/``scan``).

The same shape maps onto a Durable Object's storage API (get/put/delete/list
with a prefix), so the core needs no SQL.  ``SqliteStore`` is the durable
implementation for the self-hosted directory; ``MemoryStore`` is for tests.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator

TABLES = frozenset({'swarm', 'tomb', 'request', 'grant', 'offer', 'nonce'})


class MemoryStore:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {name: {} for name in TABLES}

    def get(self, table: str, key: str) -> dict[str, Any] | None:
        value = self._data[table].get(key)
        return None if value is None else json.loads(value)

    def put(self, table: str, key: str, value: dict[str, Any]) -> None:
        self._data[table][key] = json.dumps(value, sort_keys=True)

    def delete(self, table: str, key: str) -> None:
        self._data[table].pop(key, None)

    def scan(self, table: str, prefix: str = '') -> list[tuple[str, dict[str, Any]]]:
        return [(k, json.loads(v)) for k, v in sorted(self._data[table].items()) if k.startswith(prefix)]

    def close(self) -> None:
        pass


class SqliteStore:
    def __init__(self, path: Path | str) -> None:
        path = Path(path)
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            os.close(fd)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.execute('PRAGMA synchronous=NORMAL')
        self._db.execute('CREATE TABLE IF NOT EXISTS kv (tbl TEXT NOT NULL, k TEXT NOT NULL, v TEXT NOT NULL, '
                         'PRIMARY KEY (tbl, k)) WITHOUT ROWID')
        for suffix in ('-wal', '-shm'):
            side = Path(str(path) + suffix)
            if side.exists():
                os.chmod(side, 0o600)

    def get(self, table: str, key: str) -> dict[str, Any] | None:
        _table(table)
        with self._lock:
            row = self._db.execute('SELECT v FROM kv WHERE tbl=? AND k=?', (table, key)).fetchone()
        return None if row is None else json.loads(row[0])

    def put(self, table: str, key: str, value: dict[str, Any]) -> None:
        _table(table)
        with self._lock:
            self._db.execute('INSERT OR REPLACE INTO kv (tbl, k, v) VALUES (?, ?, ?)',
                             (table, key, json.dumps(value, sort_keys=True)))

    def delete(self, table: str, key: str) -> None:
        _table(table)
        with self._lock:
            self._db.execute('DELETE FROM kv WHERE tbl=? AND k=?', (table, key))

    def scan(self, table: str, prefix: str = '') -> list[tuple[str, dict[str, Any]]]:
        _table(table)
        with self._lock:
            rows = self._db.execute('SELECT k, v FROM kv WHERE tbl=? AND substr(k, 1, ?)=? ORDER BY k',
                                    (table, len(prefix), prefix)).fetchall()
        return [(k, json.loads(v)) for k, v in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _table(name: str) -> None:
    if name not in TABLES:
        raise ValueError('store_table_invalid')


def iter_values(store: Any, table: str, prefix: str = '') -> Iterator[dict[str, Any]]:
    for _key, value in store.scan(table, prefix):
        yield value
