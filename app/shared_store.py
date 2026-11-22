"""Process-wide shared store for GCC temporary header inputs.

A firmware batch typically compiles many sources in parallel, and every
request uploads the same board headers (same group, same file name, same
bytes).  Without sharing, each request would write its own physical copy
into its private temporary work directory.  This module keeps **one body
on disk per distinct header content** and lets every request place a hard
link to it inside its own ``workdir/<group>/<file>`` layout.

Design points
-------------

* The store is keyed by ``(group, file_name, sha256)``, so different
  versions of a same-named header get distinct blobs and coexist while
  their requests run in parallel.  GCC compilation for different cache
  keys is never serialised here; only the first on-disk write of a new
  blob takes the registry lock, guaranteeing one body per content.
* The GCC-facing path stays ``workdir/<group>/<file>``: hard links look
  like ordinary files, so GCC's ``-MMD -MP`` dependency output, the
  search-order resolution and the cache metadata keep recording the
  original group and file name exactly as before.
* A reference count tracks in-flight users.  Releasing the *last* user
  deletes the shared blob; per-request work directories (the upload
  staging area is still read per request) are cleaned independently.
* The blob directory is created lazily under the system temp root, i.e.
  on the same file system as the request work directories, so
  ``os.link`` works and falls back to a symlink/copy only if a platform
  refuses it.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Lease:
    """One request's hold on a shared header blob.

    ``release`` is idempotent: the endpoint's ``finally`` block and the
    context-manager exit can both run safely.
    """

    store: "SharedHeaderStore"
    key: tuple[str, str, str]
    blob_path: Path
    released: bool = field(default=False)

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.store.release(self.key)

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


class SharedHeaderStore:
    """Reference-counted, content-addressed store for shared header bodies."""

    def __init__(self, root: Path | None = None) -> None:
        self._root: Path | None = root
        self._lock = threading.Lock()
        # key -> [blob_path, refcount]
        self._entries: dict[tuple[str, str, str], list[Any]] = {}
        self._stats = {
            "acquires": 0,
            "writes": 0,
            "dedup_hits": 0,
            "link_fallbacks": 0,
        }

    # ------------------------------------------------------------------
    # Lifecycle / introspection
    # ------------------------------------------------------------------

    @property
    def root(self) -> Path:
        if self._root is None:
            self._root = Path(
                tempfile.mkdtemp(prefix="ccache-shared-headers-")
            )
        return self._root

    def close(self) -> None:
        """Remove the whole shared area (used on test teardown / shutdown)."""
        with self._lock:
            if self._root is not None and self._root.exists():
                shutil.rmtree(self._root, ignore_errors=True)
            self._entries.clear()
            self._root = None
            for key in self._stats:
                self._stats[key] = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def active_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def blob_paths(self) -> set[Path]:
        with self._lock:
            return {entry[0] for entry in self._entries.values()}

    # ------------------------------------------------------------------
    # Acquire / release
    # ------------------------------------------------------------------

    def acquire(self, group: str, name: str, data: bytes) -> Lease:
        """Return a lease for the shared blob holding ``data``.

        The first caller for a given ``(group, name, sha256)`` writes the
        single body; parallel callers with identical content reuse it.  The
        first-write path is serialised per store so that two requests racing
        on a brand-new key can never produce two blob bodies.
        """
        digest = hashlib.sha256(data).hexdigest()
        key = (group, name, digest)
        with self._lock:
            self._stats["acquires"] += 1
            entry = self._entries.get(key)
            if entry is not None:
                entry[1] += 1
                self._stats["dedup_hits"] += 1
                return Lease(self, key, entry[0])

            blob = self._blob_path(group, name, digest)
            blob.parent.mkdir(parents=True, exist_ok=True)
            self._entries[key] = [blob, 1]
            # Serialise only first writes: distinct keys write distinct blob
            # paths, so this only ever contends on genuinely identical
            # uploads, which are exactly the ones meant to share one body.
            tmp = blob.with_name(
                blob.name + f".tmp-{os.getpid()}-{threading.get_ident()}"
            )
            try:
                tmp.write_bytes(data)
                os.replace(tmp, blob)
            except OSError:
                self._rollback_locked(key)
                with contextlib.suppress(OSError):
                    tmp.unlink()
                raise
            self._stats["writes"] += 1
            return Lease(self, key, blob)

    def release(self, key: tuple[str, str, str]) -> None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:  # pragma: no cover - defensive
                return
            entry[1] -= 1
            if entry[1] > 0:
                return
            blob = entry[0]
            del self._entries[key]
            # Unlink inside the lock so an acquire racing the last release
            # cannot observe a stale registry entry while the blob disappears.
            try:
                blob.unlink()
            except FileNotFoundError:
                pass

    def _rollback_locked(self, key: tuple[str, str, str]) -> None:
        entry = self._entries.get(key)
        if entry is not None:
            entry[1] -= 1
            if entry[1] <= 0:
                del self._entries[key]

    def _blob_path(self, group: str, name: str, digest: str) -> Path:
        safe_name = name.replace(os.sep, "_")
        return self.root / group / f"{safe_name}.{digest}"

    # ------------------------------------------------------------------
    # GCC-facing materialisation
    # ------------------------------------------------------------------

    def link_into(self, lease: Lease, destination: Path) -> None:
        """Place the shared body at ``destination`` inside a request workdir.

        Hard link first (zero extra bytes, behaves as a regular file for
        GCC), then symlink, then a full copy on exotic file systems.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(lease.blob_path, destination)
            return
        except OSError:
            pass
        try:
            os.symlink(lease.blob_path, destination)
        except OSError:
            shutil.copyfile(lease.blob_path, destination)
        with self._lock:
            self._stats["link_fallbacks"] += 1


# Process-wide singleton used by the HTTP layer.
store = SharedHeaderStore()
