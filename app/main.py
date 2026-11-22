"""FastAPI service: GCC-based C compilation cache.

Workflow per request:

1. The client posts a ``.c`` source, flat groups of ``.h`` headers (one
   directory per group) and the directory search order.
2. Upload bytes are read and validated per request, while the GCC-facing
   header bodies are materialised through a process-wide content-addressed
   store: identical ``(group, file, content)`` uploads share one blob via
   hard links, different versions get distinct blobs, and each request
   releases its leases when preparation fails or compilation ends.
3. GCC compiles the source with ``-MMD -MP`` so the emitted ``.d``
   dependency file records *every transitively included* header that was
   actually used, including headers selected through ``#if``/``#ifdef`` on
   macro values.
4. Successful results are cached.  A later request with the same source and
   search order is served straight from the cache **without starting GCC** as
   long as every previously used header still exists byte-identically and no
   earlier directory now shadows it with a same-named header.  Unused headers
   can change freely without invalidating the entry.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from . import config
from .shared_store import Lease, SharedHeaderStore, store as shared_store


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> Any:
    # The shared header area is created lazily; remove it on shutdown.
    try:
        yield
    finally:
        shared_store.close()


app = FastAPI(
    title="C Compilation Cache",
    version="1.1.0",
    description=(
        "Compile C sources with GCC and serve cached ``.o`` files. Headers are "
        "supplied per request in flat directory groups; only the headers "
        "actually used by the preprocessor are part of the cache key. "
        "Identical header uploads from parallel requests share a single GCC "
        "temporary input body inside the instance. "
        "Every response carries an ``X-Cache: HIT|MISS`` header."
    ),
    lifespan=lifespan,
)

STATS = {"hits": 0, "misses": 0, "failures": 0, "prep_failures": 0}


class WorkspacePrepError(Exception):
    """Raised when the per-request workspace cannot be prepared.

    Validation problems still surface as ``422`` via :class:`HTTPException`;
    this type is reserved for I/O failures whose *reason* must be reported
    back to the caller.
    """


# ---------------------------------------------------------------------------
# Parsing / validation helpers
# ---------------------------------------------------------------------------

def _read_upload(upload: UploadFile) -> bytes:
    data = upload.file.read()
    if not isinstance(data, bytes):  # pragma: no cover - defensive
        data = data.encode("utf-8")
    return data


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_component(name: str) -> bool:
    """Reject path traversal / nested paths in header file names."""
    return bool(name) and "/" not in name and "\\" not in name and name not in (".", "..")


def _safe_group(name: str) -> bool:
    """Group names become real directories, so keep them simple."""
    return bool(re.fullmatch(r"[A-Za-z0-9_.\-]+", name))


def _parse_header_groups(raw: str | None) -> list[str]:
    if not raw:
        return []
    seen: set[str] = set()
    groups: list[str] = []
    for item in raw.split(","):
        name = item.strip()
        if not name:
            continue
        if not _safe_group(name):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid group name {name!r}: only letters, digits, '_', "
                    "'.' and '-' are allowed."
                ),
            )
        if name in seen:
            raise HTTPException(status_code=422, detail=f"Duplicate group {name!r}.")
        seen.add(name)
        groups.append(name)
    return groups


def _parse_search_order(raw: str | None, known: list[str]) -> list[str]:
    if not raw or not raw.strip():
        raise HTTPException(status_code=422, detail="search_order is required.")
    order = [part.strip() for part in raw.split(",") if part.strip()]
    if not order:
        raise HTTPException(status_code=422, detail="search_order is empty.")
    known_set = set(known)
    # A '.' entry means "no header directories" for sources without includes.
    if order == ["."]:
        return []
    unknown = [g for g in order if g not in known_set]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"search_order references unknown groups: {unknown}.",
        )
    if len(set(order)) != len(order):
        raise HTTPException(status_code=422, detail="search_order contains duplicates.")
    return order


def _parse_multi(raw: str | None) -> list[str]:
    """Parse comma/newline separated macro or flag tokens."""
    if not raw:
        return []
    return [tok.strip() for tok in re.split(r"[,\n]", raw) if tok.strip()]


@contextlib.contextmanager
def _temp_workdir() -> Any:
    path = Path(tempfile.mkdtemp(prefix="ccache-req-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _materialise_headers(
    workdir: Path,
    groups: list[str],
    files: list[UploadFile],
    leases: list[Lease],
) -> dict[str, dict[str, bytes]]:
    """Stage uploaded headers and place GCC inputs via the shared store.

    Upload bytes are still read and validated per request (each request has
    its own staging semantics and its own ``contents`` map), but the body
    GCC sees at ``workdir/<group>/<file>`` is a hard link to one shared
    blob per distinct ``(group, file, content)`` triple.  Parallel requests
    uploading byte-identical headers therefore write the body only once;
    different versions of a same-named header get distinct blobs and run
    side by side.

    Every acquired lease is appended to ``leases`` so the endpoint can
    release this request's holds in its ``finally`` block -- including when
    preparation fails midway.

    Returns ``{group: {file_name: content_bytes}}``.
    """
    contents: dict[str, dict[str, bytes]] = {g: {} for g in groups}
    for up in files:
        # Upload name is "<group>/<header.h>".
        normalized = (up.filename or "").replace("\\", "/")
        parts = [p for p in normalized.split("/") if p]
        if len(parts) != 2 or parts[0] not in contents:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Header {up.filename!r} must be uploaded as '<group>/<file>' "
                    "with that group declared in the 'groups' field."
                ),
            )
        group, fname = parts
        if not _safe_component(fname) or not fname.lower().endswith((".h", ".hpp", ".hh")):
            raise HTTPException(
                status_code=422, detail=f"Invalid header file name: {fname!r}."
            )
        if fname in contents[group]:
            raise HTTPException(
                status_code=422, detail=f"Duplicate header {group}/{fname}."
            )
        # Per-request upload staging: bytes are read for this request alone.
        data = _read_upload(up)
        contents[group][fname] = data

        destination = workdir / group / fname
        try:
            lease = shared_store.acquire(group, fname, data)
            leases.append(lease)
            _place_shared_header(lease, destination)
        except OSError as exc:
            raise WorkspacePrepError(
                f"failed to prepare shared header {group}/{fname}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
    return contents


def _place_shared_header(lease: Lease, destination: Path) -> None:
    """Indirection point so tests can simulate a workspace I/O failure."""
    shared_store.link_into(lease, destination)


# ---------------------------------------------------------------------------
# Dependency file / cache metadata
# ---------------------------------------------------------------------------

def _parse_depfile(dep_path: Path, workdir: Path) -> list[str]:
    """Parse a GCC ``.d`` file (handles backslash line continuations).

    ``-MP`` emits phony rules such as ``util.h:`` for every prerequisite;
    those lines end with a colon and must be ignored. Only the main
    ``object.o: header.h ...`` rule carries the real dependency list.
    """
    if not dep_path.exists():
        return []
    text = dep_path.read_text(encoding="utf-8", errors="replace")
    # Join backslash-continued logical lines.
    logical_lines = [
        line.strip() for line in text.replace("\\\n", " ").splitlines() if line.strip()
    ]
    result: list[str] = []
    for line in logical_lines:
        if line.endswith(":"):  # -MP phony target, e.g. "util.h:"
            continue
        if ":" not in line:
            continue
        _, rhs = line.split(":", 1)
        for tok in rhs.split():
            tok = tok.replace("\\ ", " ")  # escaped spaces in paths
            resolved = Path(tok)
            if not resolved.is_absolute():
                resolved = (workdir / tok).resolve()
            resolved_str = str(resolved)
            if resolved_str not in result:
                result.append(resolved_str)
    return result


def _cache_key(
    source_hash: str,
    order: list[str],
    defines: list[str],
    extra_flags: list[str],
    gcc_version: str,
) -> str:
    payload = repr(
        {
            "src": source_hash,
            "order": order,
            "defines": defines,
            "flags": extra_flags,
            "gcc": gcc_version,
        }
    ).encode("utf-8")
    return _sha256(payload)[:32]


def _build_meta(
    workdir: Path,
    groups: list[str],
    order: list[str],
    contents: dict[str, dict[str, bytes]],
    dep_headers: list[str],
    gcc_version: str,
    defines: list[str],
    extra_flags: list[str],
    object_hash: str,
) -> dict[str, Any]:
    root = workdir.resolve()
    used: list[dict[str, Any]] = []
    for abs_path in dep_headers:
        p = Path(abs_path)
        try:
            rel = p.resolve().relative_to(root)
        except ValueError:
            # System header outside the sandbox: not client controlled.
            continue
        rel_parts = rel.parts
        if len(rel_parts) != 2 or rel_parts[0] not in groups:
            continue
        group, fname = rel_parts
        data = contents[group].get(fname)
        if data is None:
            data = p.read_bytes()
        used.append(
            {
                "group": group,
                "dir_index": order.index(group),
                "name": fname,
                "sha256": _sha256(data),
            }
        )
    return {
        "version": 1,
        "gcc_version": gcc_version,
        "defines": defines,
        "extra_flags": extra_flags,
        "groups": groups,
        "search_order": order,
        "used_headers": used,
        "object_sha256": object_hash,
    }


def _entry_is_valid(
    meta: dict[str, Any],
    order: list[str],
    contents: dict[str, dict[str, bytes]],
    gcc_version: str,
    defines: list[str],
    extra_flags: list[str],
) -> tuple[bool, str | None]:
    """Validate a cached entry against the current request (no GCC run)."""
    if meta.get("gcc_version") != gcc_version:
        return False, "gcc_version_changed"
    if meta.get("defines") != defines or meta.get("extra_flags") != extra_flags:
        return False, "flags_changed"
    if meta.get("search_order") != order:
        return False, "search_order_changed"

    for hdr in meta["used_headers"]:
        group: str = hdr["group"]
        name: str = hdr["name"]
        used_index: int = hdr["dir_index"]

        # 1) The used header must still exist with identical content.
        current = contents.get(group, {}).get(name)
        if current is None:
            return False, f"used_header_deleted:{group}/{name}"
        if _sha256(current) != hdr["sha256"]:
            return False, f"used_header_modified:{group}/{name}"

        # 2) No earlier directory may now contain a same-named header that
        #    would shadow the one that was actually compiled against.
        for earlier_group in order[:used_index]:
            if name in contents.get(earlier_group, {}):
                return (
                    False,
                    f"shadowed:{earlier_group}/{name} ahead of {group}/{name}",
                )
    return True, None


def _object_name(src_name: str) -> str:
    return Path(src_name).stem + ".o"


def _lookup_cached_response(
    key: str,
    order: list[str],
    contents: dict[str, dict[str, bytes]],
    gcc_version: str,
    defines: list[str],
    extra_flags: list[str],
    download_name: str,
) -> FileResponse | None:
    """Return a HIT response when the stored entry is still valid, else None."""
    entry_dir = config.CACHE_DIR / key
    meta_path = entry_dir / "meta.json"
    object_path = entry_dir / "object.o"
    if not (meta_path.is_file() and object_path.is_file()):
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    valid, reason = _entry_is_valid(
        meta, order, contents, gcc_version, defines, extra_flags
    )
    if not valid:
        config.record_invalidation(reason or "unknown")
        return None
    return FileResponse(
        object_path,
        media_type="application/x-object",
        filename=download_name,
        headers={"X-Cache": "HIT", "X-Cache-Key": key},
    )


def _run_gcc(
    workdir: Path,
    order: list[str],
    define_list: list[str],
    flag_list: list[str],
) -> tuple[subprocess.CompletedProcess[str], list[str], float]:
    src_path = workdir / "source.c"
    obj_path = workdir / "source.o"
    dep_path = workdir / "source.d"

    include_args: list[str] = []
    for g in order:
        include_args.extend(["-I", str(workdir / g)])
    define_args = [f"-D{d}" for d in define_list]

    cmd = [
        config.GCC_BIN,
        "-c",
        str(src_path),
        "-o",
        str(obj_path),
        "-MMD",
        "-MP",
        "-MF",
        str(dep_path),
        *include_args,
        *define_args,
        *flag_list,
    ]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.GCC_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        proc = subprocess.CompletedProcess(cmd, returncode=-1, stdout="", stderr=str(exc))
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    return proc, cmd, elapsed_ms


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, Any]:
    try:
        ver = subprocess.run(
            [config.GCC_BIN, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.splitlines()[0]
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        ver = f"unavailable ({exc})"
    return {"status": "ok", "gcc": ver, "cache_dir": str(config.CACHE_DIR)}


@app.get("/stats")
def stats() -> dict[str, Any]:
    total = STATS["hits"] + STATS["misses"]
    return {
        **STATS,
        "hit_rate": round(STATS["hits"] / total, 4) if total else 0.0,
        "invalidation_reasons": config.invalidation_stats(),
        "cached_entries": len(list(config.CACHE_DIR.glob("*/meta.json")))
        if config.CACHE_DIR.exists()
        else 0,
        "shared_headers": {
            "active_blobs": shared_store.active_count(),
            **shared_store.stats(),
        },
    }


@app.post("/compile", response_class=FileResponse)
async def compile_endpoint(
    source: UploadFile = File(..., description="The .c source file."),
    groups: str = Form(
        "", description="Comma-separated header directory (group) names."
    ),
    search_order: str = Form(
        ..., description="Comma-separated group names, highest priority first."
    ),
    defines: str = Form(
        "", description="Comma/newline separated macros, e.g. 'DEBUG,FEATURE=2'."
    ),
    extra_flags: str = Form(
        "", description="Comma/newline separated extra GCC flags."
    ),
    headers: list[UploadFile] = File(
        default_factory=list,
        description="Header files uploaded as '<group>/<header.h>'.",
    ),
) -> Any:
    """Compile a source against flat header groups with response caching.

    Parallel requests that upload the same header ``(group, file name,
    content)`` share one GCC temporary input body (hard links inside each
    request's own work directory); their upload staging stays independent.
    Different cache keys still compile in parallel and different versions
    of a same-named header coexist.  Each request keeps its own directory
    search order, so dependency records and cache validation are unchanged.
    A request releases every shared-header hold when preparation fails or
    once compilation finishes; the shared blob is deleted only after the
    last user exits.
    """
    group_list = _parse_header_groups(groups)
    order = _parse_search_order(search_order, group_list)
    define_list = _parse_multi(defines)
    flag_list = _parse_multi(extra_flags)

    if not (source.filename or "").lower().endswith(".c"):
        raise HTTPException(status_code=422, detail="source must be a .c file.")
    source_bytes = _read_upload(source)
    if not source_bytes:
        raise HTTPException(status_code=422, detail="source file is empty.")

    gcc_version = config.gcc_version()
    source_hash = _sha256(source_bytes)
    key = _cache_key(source_hash, order, define_list, flag_list, gcc_version)

    download_name = _object_name(source.filename or "source.c")

    # Leases acquired for this request; released unconditionally at the end
    # (preparation failure or finished compilation). The shared blob itself
    # disappears only when the last holder releases it.
    leases: list[Lease] = []
    try:
        with _temp_workdir() as workdir:
            try:
                contents = _materialise_headers(
                    workdir, group_list, headers, leases
                )
            except WorkspacePrepError as exc:
                STATS["prep_failures"] += 1
                return JSONResponse(
                    status_code=500,
                    headers={"X-Cache": "MISS", "X-Cache-Reason": "workspace_prep_failed"},
                    content={"detail": "Workspace preparation failed.", "reason": str(exc)},
                )

            # ---- Fast path: cache lookup, GCC is never started ---------------
            cached = _lookup_cached_response(
                key, order, contents, gcc_version, define_list, flag_list, download_name
            )
            if cached is not None:
                STATS["hits"] += 1
                return cached

            # ---- Miss: serialise identical keys so concurrent requests only
            # pay for one GCC invocation and never stream a half-written file.
            async with config.key_lock(key):
                # Double-check: another request may have compiled it meanwhile.
                cached = _lookup_cached_response(
                    key, order, contents, gcc_version, define_list, flag_list,
                    download_name,
                )
                if cached is not None:
                    STATS["hits"] += 1
                    return cached

                STATS["misses"] += 1
                try:
                    (workdir / "source.c").write_bytes(source_bytes)
                except OSError as exc:
                    STATS["prep_failures"] += 1
                    return JSONResponse(
                        status_code=500,
                        headers={
                            "X-Cache": "MISS",
                            "X-Cache-Reason": "workspace_prep_failed",
                        },
                        content={
                            "detail": "Workspace preparation failed.",
                            "reason": f"failed to stage source.c: {type(exc).__name__}: {exc}",
                        },
                    )
                proc, cmd, elapsed_ms = await asyncio.to_thread(
                    _run_gcc, workdir, order, define_list, flag_list
                )

                if proc.returncode == -1:
                    STATS["failures"] += 1
                    return JSONResponse(
                        status_code=504,
                        headers={"X-Cache": "MISS"},
                        content={
                            "detail": "GCC compilation timed out.",
                            "command": cmd,
                        },
                    )

                if proc.returncode != 0:
                    # Failures are never cached. Reverting the inputs restores a
                    # HIT on the previously stored object.
                    STATS["failures"] += 1
                    return JSONResponse(
                        status_code=422,
                        headers={
                            "X-Cache": "MISS",
                            "X-Cache-Reason": "compilation_failed",
                        },
                        content={
                            "detail": "Compilation failed.",
                            "command": cmd,
                            "returncode": proc.returncode,
                            "stderr": proc.stderr,
                            "stdout": proc.stdout,
                            "elapsed_ms": elapsed_ms,
                        },
                    )

                obj_path = workdir / "source.o"
                dep_path = workdir / "source.d"
                object_bytes = obj_path.read_bytes()
                dep_headers = _parse_depfile(dep_path, workdir)
                meta = _build_meta(
                    workdir,
                    group_list,
                    order,
                    contents,
                    dep_headers,
                    gcc_version,
                    define_list,
                    flag_list,
                    _sha256(object_bytes),
                )
                # Objects are content-addressed and immutable, so the response can
                # stream the durable cached file safely even under recompiles.
                stored = config.store_entry(key, object_bytes, meta)
                used_names = ",".join(
                    f"{h['group']}/{h['name']}" for h in meta["used_headers"]
                )
                return FileResponse(
                    stored / "object.o",
                    media_type="application/x-object",
                    filename=download_name,
                    headers={
                        "X-Cache": "MISS",
                        "X-Cache-Reason": "compiled",
                        "X-Cache-Key": key,
                        "X-Compile-Ms": str(elapsed_ms),
                        "X-Used-Headers": used_names,
                    },
                )
    finally:
        # Release this request's holds no matter how it ended. The shared
        # blob itself is removed by the store only when the last user exits.
        for lease in leases:
            lease.release()
