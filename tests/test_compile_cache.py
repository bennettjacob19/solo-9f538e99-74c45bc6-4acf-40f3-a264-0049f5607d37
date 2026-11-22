"""End-to-end tests for the C compilation cache service."""

from __future__ import annotations

import io
import threading


def _post(
    client,
    source: bytes,
    headers: dict[str, bytes] | None = None,
    groups: str = "",
    search_order: str = "",
    defines: str = "",
    extra_flags: str = "",
    source_name: str = "main.c",
):
    """POST /compile. ``headers`` maps '<group>/<file>.h' -> content bytes."""
    data = {
        "groups": groups,
        "search_order": search_order,
        "defines": defines,
        "extra_flags": extra_flags,
    }
    files = [("source", (source_name, io.BytesIO(source), "text/x-c"))]
    for name, content in (headers or {}).items():
        files.append(("headers", (name, io.BytesIO(content), "text/x-chdr")))
    return client.post("/compile", data=data, files=files)


# ---------------------------------------------------------------------------
# Basic compilation and cache hit/miss headers
# ---------------------------------------------------------------------------

SIMPLE_SRC = b"""
#include <util.h>
int add(int a, int b) { return a + b; }
int answer(void) { return add(40, VALUE); }
"""

UTIL_H = b"#define VALUE 2\n"
UTIL_H_V2 = b"#define VALUE 3\n"
UNUSED_H = b"/* not included by anyone */\n#define UNUSED 99\n"


def test_health_and_docs(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert "gcc" in r.json()
    docs = client.get("/docs")
    assert docs.status_code == 200


def test_first_compile_is_miss_and_returns_elf_object(client):
    r = _post(
        client, SIMPLE_SRC, {"board/util.h": UTIL_H},
        groups="board", search_order="board",
    )
    assert r.status_code == 200, r.text
    assert r.headers["X-Cache"] == "MISS"
    assert r.headers["X-Cache-Reason"] == "compiled"
    assert r.headers["X-Used-Headers"] == "board/util.h"
    # ELF relocatable object magic.
    assert r.content[:4] == b"\x7fELF"
    assert 'filename="main.o"' in r.headers["content-disposition"]


def test_identical_request_is_a_cache_hit(client):
    kw = dict(headers={"board/util.h": UTIL_H}, groups="board", search_order="board")
    r1 = _post(client, SIMPLE_SRC, **kw)
    assert r1.headers["X-Cache"] == "MISS"
    obj1 = r1.content

    r2 = _post(client, SIMPLE_SRC, **kw)
    assert r2.status_code == 200
    assert r2.headers["X-Cache"] == "HIT"
    assert r2.headers["X-Cache-Key"] == r1.headers["X-Cache-Key"]
    assert r2.content == obj1


def test_unused_header_change_keeps_cache_hit(client):
    kw = dict(
        headers={"board/util.h": UTIL_H, "board/noise.h": UNUSED_H},
        groups="board", search_order="board",
    )
    r1 = _post(client, SIMPLE_SRC, **kw)
    assert r1.headers["X-Cache"] == "MISS"

    # Modify (and add another) unused header: GCC must not be re-invoked.
    kw2 = dict(kw)
    kw2["headers"] = {
        "board/util.h": UTIL_H,
        "board/noise.h": b"#define UNUSED 100\n",
        "board/other.h": b"#define OTHER 1\n",
    }
    r2 = _post(client, SIMPLE_SRC, **kw2)
    assert r2.status_code == 200
    assert r2.headers["X-Cache"] == "HIT", r2.text
    assert r2.content == r1.content


def test_used_header_modification_invalidates(client):
    kw = dict(headers={"board/util.h": UTIL_H}, groups="board", search_order="board")
    r1 = _post(client, SIMPLE_SRC, **kw)
    assert r1.headers["X-Cache"] == "MISS"

    r2 = _post(client, SIMPLE_SRC, headers={"board/util.h": UTIL_H_V2},
               groups="board", search_order="board")
    assert r2.headers["X-Cache"] == "MISS"
    assert r2.headers["X-Cache-Reason"] == "compiled"
    # A genuinely new object file (the macro value changed the code).
    assert r2.content != r1.content


def test_used_header_deletion_invalidates_and_diagnostics(client):
    kw = dict(headers={"board/util.h": UTIL_H}, groups="board", search_order="board")
    r1 = _post(client, SIMPLE_SRC, **kw)
    assert r1.headers["X-Cache"] == "MISS"

    # util.h gone -> GCC cannot find it -> failure response, no cached object.
    r2 = _post(client, SIMPLE_SRC, groups="board", search_order="board")
    assert r2.status_code == 422
    assert r2.headers["X-Cache"] == "MISS"
    body = r2.json()
    assert body["detail"] == "Compilation failed."
    assert "util.h" in body["stderr"]
    assert body["returncode"] != 0

    # Reverting the inputs must hit the previously cached object again.
    r3 = _post(client, SIMPLE_SRC, **kw)
    assert r3.status_code == 200
    assert r3.headers["X-Cache"] == "HIT"
    assert r3.content == r1.content


# ---------------------------------------------------------------------------
# Search order / shadowing
# ---------------------------------------------------------------------------

SRC_SHADOW = b"#include <plat.h>\nint f(void){return PLAT_ID;}\n"


def test_same_named_header_in_earlier_dir_invalidates(client):
    # Start with the header resolved from the later 'board' directory.
    headers_late = {"board/plat.h": b"#define PLAT_ID 2\n"}
    r_late = _post(
        client, SRC_SHADOW, headers=headers_late,
        groups="arch,board", search_order="arch,board",
    )
    assert r_late.status_code == 200
    assert r_late.headers["X-Cache"] == "MISS"
    assert r_late.headers["X-Used-Headers"] == "board/plat.h"
    late_obj = r_late.content

    # An earlier directory now gains a same-named header: the cached object
    # can no longer be valid, so GCC must run again.
    r_shadow = _post(
        client, SRC_SHADOW,
        headers={
            "arch/plat.h": b"#define PLAT_ID 1\n",
            "board/plat.h": b"#define PLAT_ID 2\n",
        },
        groups="arch,board", search_order="arch,board",
    )
    assert r_shadow.status_code == 200
    assert r_shadow.headers["X-Cache"] == "MISS", r_shadow.text
    assert r_shadow.headers["X-Used-Headers"] == "arch/plat.h"
    assert r_shadow.content != late_obj

    # Removing the shadowing header: the stored object was overwritten by the
    # arch build, so this recompiles once ...
    r_again = _post(
        client, SRC_SHADOW, headers=headers_late,
        groups="arch,board", search_order="arch,board",
    )
    assert r_again.headers["X-Cache"] == "MISS"
    assert r_again.headers["X-Used-Headers"] == "board/plat.h"
    # ... and immediately settles back into a cache hit afterwards.
    r_stable = _post(
        client, SRC_SHADOW, headers=headers_late,
        groups="arch,board", search_order="arch,board",
    )
    assert r_stable.headers["X-Cache"] == "HIT"
    assert r_stable.content == r_again.content


def test_search_order_reversal_changes_resolution(client):
    h = {
        "a/plat.h": b"#define PLAT_ID 10\n",
        "b/plat.h": b"#define PLAT_ID 20\n",
    }
    r1 = _post(client, SRC_SHADOW, headers=h, groups="a,b", search_order="a,b")
    r2 = _post(client, SRC_SHADOW, headers=h, groups="a,b", search_order="b,a")
    # Different search order -> different key, both are fresh compiles.
    assert r1.headers["X-Cache-Key"] != r2.headers["X-Cache-Key"]
    assert r2.headers["X-Cache"] == "MISS"
    assert r2.headers["X-Used-Headers"] == "b/plat.h"


# ---------------------------------------------------------------------------
# Nested includes and macro-driven header selection
# ---------------------------------------------------------------------------

NESTED_SRC = b"""
#include <top.h>
int run(void) { return TOP + MID + LEAF; }
"""


def test_nested_transitive_includes_are_tracked(client):
    headers = {
        "inc/top.h": b"#include <mid.h>\n#define TOP 1\n",
        "inc/mid.h": b"#include <leaf.h>\n#define MID 10\n",
        "inc/leaf.h": b"#define LEAF 100\n",
    }
    kw = dict(headers=headers, groups="inc", search_order="inc")
    r1 = _post(client, NESTED_SRC, **kw)
    assert r1.status_code == 200, r1.text
    used = set(r1.headers["X-Used-Headers"].split(","))
    assert used == {"inc/top.h", "inc/mid.h", "inc/leaf.h"}

    # Touching the deepest leaf invalidates; changing nothing still hits.
    r_hit = _post(client, NESTED_SRC, **kw)
    assert r_hit.headers["X-Cache"] == "HIT"

    headers["inc/leaf.h"] = b"#define LEAF 200\n"
    r_miss = _post(client, NESTED_SRC, **kw)
    assert r_miss.headers["X-Cache"] == "MISS"


MACRO_SRC = b"""
#if defined(BOARD_A)
#include <cfg_a.h>
#elif defined(BOARD_B)
#include <cfg_b.h>
#else
#include <cfg_default.h>
#endif
int cfg(void){return CFG_VALUE;}
"""


def test_macro_value_selects_header_and_define_is_part_of_key(client):
    headers = {
        "cfg/cfg_a.h": b"#define CFG_VALUE 111\n",
        "cfg/cfg_b.h": b"#define CFG_VALUE 222\n",
        "cfg/cfg_default.h": b"#define CFG_VALUE 0\n",
    }
    base = dict(headers=headers, groups="cfg", search_order="cfg")

    r_def = _post(client, MACRO_SRC, **base)
    assert r_def.headers["X-Used-Headers"] == "cfg/cfg_default.h"

    r_a1 = _post(client, MACRO_SRC, defines="BOARD_A", **base)
    assert r_a1.status_code == 200
    assert r_a1.headers["X-Used-Headers"] == "cfg/cfg_a.h"

    # Same define again -> hit without GCC.
    r_a2 = _post(client, MACRO_SRC, defines="BOARD_A", **base)
    assert r_a2.headers["X-Cache"] == "HIT"
    assert r_a2.content == r_a1.content

    # A different macro selects a different header -> separate cache entry.
    r_b = _post(client, MACRO_SRC, defines="BOARD_B", **base)
    assert r_b.headers["X-Used-Headers"] == "cfg/cfg_b.h"
    assert r_b.headers["X-Cache-Key"] != r_a1.headers["X-Cache-Key"]

    # Changing the value of a used macro invalidates the BOARD_A entry.
    r_a_val = _post(client, MACRO_SRC, defines="BOARD_A=1", **base)
    assert r_a_val.headers["X-Cache-Key"] != r_a1.headers["X-Cache-Key"]


# ---------------------------------------------------------------------------
# Error handling / input validation
# ---------------------------------------------------------------------------

def test_syntax_error_returns_diagnostics_and_keeps_old_cache(client):
    good = b"int ok(void){return 0;}\n"
    bad = b"int broken( { return ; }\n"
    r1 = _post(client, good, groups="board", search_order="board")
    assert r1.headers["X-Cache"] == "MISS"

    r_bad = _post(client, bad, groups="board", search_order="board")
    assert r_bad.status_code == 422
    assert "error" in r_bad.json()["stderr"]

    r_good = _post(client, good, groups="board", search_order="board")
    assert r_good.headers["X-Cache"] == "HIT"
    assert r_good.content == r1.content


def test_validation_errors(client):
    # Unknown group in search order.
    r = _post(client, b"int x;\n", groups="a", search_order="b")
    assert r.status_code == 422

    # Path traversal in header name.
    r = _post(client, b"#include <x.h>\n",
              headers={"a/../x.h": b""}, groups="a", search_order="a")
    assert r.status_code == 422

    # Header uploaded for an undeclared group.
    r = _post(client, b"int x;\n",
              headers={"ghost/x.h": b""}, groups="a", search_order="a")
    assert r.status_code == 422

    # Non-.c source.
    r = client.post(
        "/compile",
        data={"search_order": "a", "groups": "a"},
        files=[("source", ("main.txt", io.BytesIO(b"x"), "text/plain"))],
    )
    assert r.status_code == 422


def test_headerless_source_uses_dot_search_order(client):
    src = b"int standalone(void){return 42;}\n"
    r1 = _post(client, src, groups="", search_order=".")
    assert r1.status_code == 200
    assert r1.content[:4] == b"\x7fELF"
    r2 = _post(client, src, groups="", search_order=".")
    assert r2.headers["X-Cache"] == "HIT"
    assert r2.content == r1.content


def test_concurrent_misses_compile_once_and_return_full_objects(client):
    src = b"int concurrent_fn(void){return 31337;}\n"
    results: list[tuple[int, bytes, str]] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            r = _post(client, src, groups="board", search_order="board")
            results.append((r.status_code, r.content, r.headers["X-Cache"]))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 12
    assert all(code == 200 for code, _, _ in results)
    # Every response carries a complete ELF object and they are identical.
    assert all(body[:4] == b"\x7fELF" and len(body) > 100 for _, body, _ in results)
    assert len({body for _, body, _ in results}) == 1
    assert {tag for _, _, tag in results} <= {"HIT", "MISS"}
    assert sum(1 for _, _, tag in results if tag == "MISS") == 1


def test_stats_endpoint(client):
    kw = dict(headers={"board/util.h": UTIL_H}, groups="board", search_order="board")
    _post(client, SIMPLE_SRC, **kw)
    _post(client, SIMPLE_SRC, **kw)
    s = client.get("/stats").json()
    assert s["hits"] >= 1 and s["misses"] >= 1
    assert s["cached_entries"] >= 1
