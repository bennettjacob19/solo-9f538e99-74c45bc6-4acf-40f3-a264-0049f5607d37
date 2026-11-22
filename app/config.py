"""Runtime configuration and content-addressed on-disk cache storage."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

CACHE_DIR = Path(os.environ.get("CCACHE_CACHE_DIR", "/data/cache")).resolve()
GCC_BIN = os.environ.get("CCACHE_GCC_BIN", "gcc")
GCC_TIMEOUT = int(os.environ.get("CCACHE_GCC_TIMEOUT", "30"))

_write_lock = threading.Lock()
_invalidation_stats: dict[str, int] = {}

# One asyncio lock per cache key: concurrent identical requests compile once,
# while requests for different keys still run in parallel.
_key_locks: dict[str, asyncio.Lock] = {}
_key_locks_guard = threading.Lock()


def key_lock(key: str) -> asyncio.Lock:
    with _key_locks_guard:
        lock = _key_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _key_locks[key] = lock
        return lock


def gcc_version() -> str:
    """Return the first line of ``gcc --version`` (cached per process)."""
    global _GCC_VERSION
    if _GCC_VERSION is None:
        out = subprocess.run(
            [GCC_BIN, "--version"], capture_output=True, text=True, timeout=10, check=True
        )
        _GCC_VERSION = out.stdout.splitlines()[0].strip()
    return _GCC_VERSION


_GCC_VERSION: str | None = None


def store_entry(key: str, object_bytes: bytes, meta: dict[str, Any]) -> Path:
    """Persist a compiled object and its metadata.

    Object files are content-addressed (``objects/<sha256>.o``) and never
    mutated, so a ``FileResponse`` streaming an older object cannot be
    interrupted by a recompile. ``meta.json`` is replaced atomically.
    """
    entry_dir = CACHE_DIR / key
    objects_dir = CACHE_DIR / "objects"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    objects_dir.mkdir(parents=True, exist_ok=True)
    object_hash = meta["object_sha256"]
    object_file = objects_dir / f"{object_hash}.o"

    with _write_lock:
        # Write the immutable object file via a temp + rename dance.
        if not object_file.exists():
            tmp_obj = objects_dir / f".{object_hash}.o.tmp"
            tmp_obj.write_bytes(object_bytes)
            os.replace(tmp_obj, object_file)

        tmp_meta = entry_dir.with_name(entry_dir.name + ".meta.tmp")
        tmp_meta.write_text(
            json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8"
        )
        entry_dir.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_meta, entry_dir / "meta.json")
        # Symlink keeps the per-entry object.o path stable for responses.
        link = entry_dir / "object.o"
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(os.path.relpath(object_file, entry_dir), link)
    return entry_dir


def record_invalidation(reason: str) -> None:
    with _write_lock:
        _invalidation_stats[reason] = _invalidation_stats.get(reason, 0) + 1


def invalidation_stats() -> dict[str, int]:
    with _write_lock:
        return dict(_invalidation_stats)
