"""Tests for the shared GCC-temporary header store and its HTTP integration.

Covers the batch-compile optimisation:

* identical ``(group, file, content)`` uploads share one on-disk body even
  under parallel requests;
* different versions of a same-named header coexist and compile in parallel;
* different cache keys never block each other;
* a failing request releases its holds while surviving ones keep theirs,
  and the shared blob is deleted only after the last user exits;
* workspace preparation failures report their reason and leak no lease.
"""

from __future__ import annotations

import io
import threading

import pytest

from app import main
from app.shared_store import SharedHeaderStore


# ---------------------------------------------------------------------------
# Unit tests for the reference-counted store itself
# ---------------------------------------------------------------------------

def test_store_writes_one_body_per_content_and_deletes_after_last_release(tmp_path):
    store = SharedHeaderStore(root=tmp_path / "shared")
    data = b"#define VALUE 2\n"

    lease_a = store.acquire("board", "util.h", data)
    lease_b = store.acquire("board", "util.h", data)

    # Same key -> one blob, one physical write, two users.
    assert lease_a.blob_path == lease_b.blob_path
    assert lease_a.blob_path.is_file()
    assert store.stats()["writes"] == 1
    assert store.stats()["dedup_hits"] == 1
    assert store.active_count() == 1

    lease_a.release()
    # The other user is still holding it.
    assert lease_b.blob_path.is_file()
    assert store.active_count() == 1

    lease_b.release()
    assert not lease_b.blob_path.exists()
    assert store.active_count() == 0

    # Releasing twice must not raise (idempotent finally blocks).
    lease_b.release()


def test_store_keeps_distinct_versions_of_same_named_header(tmp_path):
    store = SharedHeaderStore(root=tmp_path / "shared")
    v1 = b"#define VALUE 1\n"
    v2 = b"#define VALUE 2\n"

    l1 = store.acquire("board", "util.h", v1)
    l2 = store.acquire("board", "util.h", v2)

    # Different content -> different blobs, both alive simultaneously.
    assert l1.blob_path != l2.blob_path
    assert l1.blob_path.is_file() and l2.blob_path.is_file()
    assert store.active_count() == 2
    assert l1.blob_path.read_bytes() == v1
    assert l2.blob_path.read_bytes() == v2

    l1.release()
    assert not l1.blob_path.exists()
    assert l2.blob_path.is_file()
    l2.release()
    assert store.active_count() == 0


def test_store_rolls_back_refcount_when_blob_write_fails(tmp_path, monkeypatch):
    store = SharedHeaderStore(root=tmp_path / "shared")

    def boom(self, data, *args, **kwargs):  # noqa: ANN001
        raise OSError("simulated disk full")

    monkeypatch.setattr("pathlib.Path.write_bytes", boom)
    with pytest.raises(OSError):
        store.acquire("board", "util.h", b"x\n")

    # The failed acquire must not leave a phantom entry behind.
    assert store.active_count() == 0
    monkeypatch.undo()

    # A later acquire of the same key works normally.
    lease = store.acquire("board", "util.h", b"x\n")
    assert lease.blob_path.is_file()
    lease.release()
    assert store.active_count() == 0


def test_link_into_hard_links_shared_body(tmp_path):
    store = SharedHeaderStore(root=tmp_path / "shared")
    lease = store.acquire("board", "util.h", b"#define VALUE 2\n")
    workdir = tmp_path / "req" / "board"
    destination = workdir / "util.h"

    store.link_into(lease, destination)

    assert destination.read_bytes() == b"#define VALUE 2\n"
    assert destination.stat().st_ino == lease.blob_path.stat().st_ino
    assert destination.stat().st_dev == lease.blob_path.stat().st_dev
    lease.release()


def test_parallel_first_acquires_write_exactly_one_body(tmp_path):
    store = SharedHeaderStore(root=tmp_path / "shared")
    data = b"#define RACE 1\n"
    leases: list = []
    barrier = threading.Barrier(16)

    def worker() -> None:
        barrier.wait()
        leases.append(store.acquire("board", "race.h", data))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(leases) == 16
    assert len({l.blob_path for l in leases}) == 1
    assert store.stats()["writes"] == 1
    assert store.stats()["dedup_hits"] == 15
    for lease in leases:
        lease.release()
    assert store.active_count() == 0


# ---------------------------------------------------------------------------
# End-to-end: parallel /compile requests through FastAPI
# ---------------------------------------------------------------------------

SRC = b"""
#include <util.h>
int answer(void) { return VALUE; }
"""
UTIL_V1 = b"#define VALUE 10\n"
UTIL_V2 = b"#define VALUE 20\n"


def _post(client, source, headers, groups, search_order, defines=""):
    data = {
        "groups": groups,
        "search_order": search_order,
        "defines": defines,
    }
    files = [("source", ("main.c", io.BytesIO(source), "text/x-c"))]
    for name, content in headers.items():
        files.append(("headers", (name, io.BytesIO(content), "text/x-chdr")))
    return client.post("/compile", data=data, files=files)


def test_parallel_identical_headers_share_one_gcc_input_body(client):
    n = 8
    barrier = threading.Barrier(n)
    results: list[tuple[int, bytes, str]] = []
    errors: list[Exception] = []
    seen_inodes: list[int] = []
    inode_lock = threading.Lock()
    original_link = main._place_shared_header

    def capturing_link(lease, destination):
        original_link(lease, destination)
        with inode_lock:
            seen_inodes.append(destination.stat().st_ino)

    main._place_shared_header = capturing_link
    try:
        def worker() -> None:
            try:
                barrier.wait()
                r = _post(
                    client, SRC, {"board/util.h": UTIL_V1},
                    groups="board", search_order="board",
                )
                results.append((r.status_code, r.content, r.headers["X-Cache"]))
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        main._place_shared_header = original_link

    assert not errors
    assert len(results) == n
    assert all(code == 200 for code, _, _ in results)
    # Every request resolved util.h to the same physical blob (hard link).
    assert len(set(seen_inodes)) == 1
    # Exactly one body was written; the other requests reused it.
    stats = client.get("/stats").json()["shared_headers"]
    assert stats["writes"] == 1
    assert stats["dedup_hits"] == n - 1
    assert stats["active_blobs"] == 0  # all leases released at request end
    # The first request compiles, the rest hit the cache.
    assert sum(1 for _, _, tag in results if tag == "MISS") == 1
    assert len({body for _, body, _ in results}) == 1


def test_parallel_different_versions_coexist_and_compile(client):
    barrier = threading.Barrier(2)
    outcomes: dict[str, tuple[int, bytes, str]] = {}
    errors: list[Exception] = []
    seen: dict[str, int] = {}
    seen_lock = threading.Lock()
    original_link = main._place_shared_header

    def capturing_link(lease, destination):
        original_link(lease, destination)
        with seen_lock:
            seen[lease.key] = destination.stat().st_ino

    main._place_shared_header = capturing_link
    try:
        def worker(version: str, header: bytes, define: str) -> None:
            try:
                barrier.wait()
                r = _post(
                    client, SRC, {"board/util.h": header},
                    groups="board", search_order="board", defines=define,
                )
                outcomes[version] = (r.status_code, r.content, r.headers["X-Cache-Key"])
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=("v1", UTIL_V1, "V1")),
            threading.Thread(target=worker, args=("v2", UTIL_V2, "V2")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        main._place_shared_header = original_link

    assert not errors
    assert set(outcomes) == {"v1", "v2"}
    assert all(code == 200 for code, _, _ in outcomes.values())
    # Separate cache keys (different defines) -> parallel, distinct objects.
    assert outcomes["v1"][2] != outcomes["v2"][2]
    assert outcomes["v1"][1] != outcomes["v2"][1]
    # Two different blobs for the same group/name, alive at the same time.
    assert len(seen) == 2
    assert len(set(seen.values())) == 2
    stats = client.get("/stats").json()["shared_headers"]
    assert stats["writes"] == 2
    assert stats["active_blobs"] == 0


def test_one_failing_request_releases_its_hold_while_others_survive(client):
    good_src = b"#include <util.h>\nint good(void){return VALUE;}\n"
    bad_src = b"#include <util.h>\nint broken( { return ; }\n"

    # Establish the shared header with a successful compile.
    r1 = _post(
        client, good_src, {"board/util.h": UTIL_V1},
        groups="board", search_order="board",
    )
    assert r1.status_code == 200

    # A parallel request uploads the same header but its source is invalid.
    barrier = threading.Barrier(2)
    results: dict[str, tuple[int, str]] = {}
    errors: list[Exception] = []

    def worker(name: str, source: bytes) -> None:
        try:
            barrier.wait()
            r = _post(
                client, source, {"board/util.h": UTIL_V1},
                groups="board", search_order="board",
            )
            results[name] = (r.status_code, r.headers.get("X-Cache-Reason", ""))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("bad", bad_src)),
        threading.Thread(
            target=worker,
            args=("good", good_src),
        ),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert results["bad"] == (422, "compilation_failed")
    assert results["good"][0] == 200
    # Both requests released their holds; nothing leaks.
    stats = client.get("/stats").json()
    assert stats["shared_headers"]["active_blobs"] == 0
    # And the good input still compiles / hits afterwards.
    r_again = _post(
        client, good_src, {"board/util.h": UTIL_V1},
        groups="board", search_order="board",
    )
    assert r_again.status_code == 200
    assert r_again.headers["X-Cache"] == "HIT"


def test_workspace_preparation_failure_returns_reason_and_releases_hold(
    client, monkeypatch
):
    original_link = main._place_shared_header

    def failing_link(lease, destination):
        raise OSError("simulated ENOSPC")

    monkeypatch.setattr(main, "_place_shared_header", failing_link)
    r = _post(
        client, SRC, {"board/util.h": UTIL_V1},
        groups="board", search_order="board",
    )
    assert r.status_code == 500
    body = r.json()
    assert body["detail"] == "Workspace preparation failed."
    assert "simulated ENOSPC" in body["reason"]
    assert r.headers["X-Cache-Reason"] == "workspace_prep_failed"
    monkeypatch.undo()

    # The failed request rolled its lease back: no blob remains, and a
    # follow-up request with the same input works from a clean state.
    assert main.shared_store.active_count() == 0
    r_ok = _post(
        client, SRC, {"board/util.h": UTIL_V1},
        groups="board", search_order="board",
    )
    assert r_ok.status_code == 200
    assert r_ok.headers["X-Cache"] == "MISS"
    assert r_ok.headers["X-Used-Headers"] == "board/util.h"


def test_different_cache_keys_compile_in_parallel_without_blocking(client):
    n = 6
    barrier = threading.Barrier(n)
    results: list[tuple[int, str]] = []
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            barrier.wait()
            # Distinct source per thread -> distinct cache key.
            src = f"int parallel_fn_{i}(void){{return {i};}}\n".encode()
            r = _post(
                client, src, {"board/util.h": UTIL_V1},
                groups="board", search_order="board",
            )
            results.append((r.status_code, r.headers["X-Cache"]))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == n
    assert all(code == 200 for code, _ in results)
    # Each distinct key compiles once; the shared header body is reused.
    assert sum(1 for _, tag in results if tag == "MISS") == n
    stats = client.get("/stats").json()["shared_headers"]
    assert stats["writes"] == 1
    assert stats["active_blobs"] == 0


def test_shared_body_disappears_only_after_last_user_exits(client):
    """Reference-count semantics at the store boundary.

    The HTTP layer always releases its leases in a ``finally`` block
    (covered by every endpoint test); here we verify directly that a blob
    stays alive while any user holds it and is deleted exactly when the
    last user leaves.
    """
    store = main.shared_store
    lease_1 = store.acquire("board", "util.h", UTIL_V1)
    lease_2 = store.acquire("board", "util.h", UTIL_V1)
    assert lease_1.blob_path == lease_2.blob_path
    blob = lease_1.blob_path
    assert blob.is_file()

    # A third user comes and goes: the blob must survive.
    lease_3 = store.acquire("board", "util.h", UTIL_V1)
    lease_3.release()
    assert blob.is_file()
    assert store.active_count() == 1

    lease_1.release()
    assert blob.is_file(), "blob deleted while a user still holds it"

    lease_2.release()
    assert not blob.exists()
    assert store.active_count() == 0
