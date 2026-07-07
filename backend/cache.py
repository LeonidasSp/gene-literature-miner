"""
Tiny async-friendly key/value cache with TTL.

Gene→protein and protein→cluster lookups are stable for long stretches, so
caching them cuts repeat-query latency and load on NCBI/UniProt/OrthoDB (and
softens Hugging Face cold starts). Backed by SQLite for persistence across
restarts, with an in-process dict in front; if the SQLite file can't be opened
(e.g. a read-only host) it degrades silently to memory-only.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from typing import Any, Optional

CACHE_PATH = os.environ.get(
    "CACHE_PATH", os.path.join(os.environ.get("TMPDIR", "/tmp"), "glm_cache.sqlite")
)
DEFAULT_TTL = float(os.environ.get("CACHE_TTL", str(14 * 24 * 3600)))  # 14 days


class Cache:
    def __init__(self, path: str = CACHE_PATH) -> None:
        self._mem: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()
        self._db: Optional[sqlite3.Connection] = None
        try:
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, exp REAL)"
            )
            self._db.commit()
        except (sqlite3.Error, OSError):
            self._db = None  # memory-only fallback

    @staticmethod
    def _key(namespace: str, key: str) -> str:
        return f"{namespace}:{key}"

    async def get(self, namespace: str, key: str) -> Optional[Any]:
        k = self._key(namespace, key)
        now = time.time()
        hit = self._mem.get(k)
        if hit and hit[0] > now:
            return hit[1]
        if hit:
            self._mem.pop(k, None)
        if self._db is None:
            return None
        async with self._lock:
            row = await asyncio.to_thread(
                lambda: self._db.execute(
                    "SELECT v, exp FROM kv WHERE k=?", (k,)
                ).fetchone()
            )
        if not row:
            return None
        v, exp = row
        if exp <= now:
            return None
        try:
            val = json.loads(v)
        except (ValueError, TypeError):
            return None
        self._mem[k] = (exp, val)
        return val

    async def set(
        self, namespace: str, key: str, value: Any, ttl: float = DEFAULT_TTL
    ) -> None:
        k = self._key(namespace, key)
        exp = time.time() + ttl
        self._mem[k] = (exp, value)
        if self._db is None:
            return
        try:
            payload = json.dumps(value)
        except (TypeError, ValueError):
            return
        async with self._lock:
            await asyncio.to_thread(self._commit, k, payload, exp)

    def _commit(self, k: str, payload: str, exp: float) -> None:
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO kv (k, v, exp) VALUES (?, ?, ?)",
                (k, payload, exp),
            )
            self._db.commit()
        except sqlite3.Error:
            pass

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except sqlite3.Error:
                pass
