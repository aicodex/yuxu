"""Unix-socket daemon that fronts the yuxu `memory` skill.

Designed so an external process (e.g. the user's Claude Code session)
can query yuxu memory over a local socket without paying the
bus+loader bootstrap on every call.

Protocol
--------
One JSON request per connection, one JSON response, then close.

    Request:  {"op": "list"|"get"|"search"|"stats", ...args}
    Response: whatever `memory.execute()` returns, unchanged.

No truncation here — truncation is an LLM-budget concern; the shell
client (`bin/yuxu-memory`) applies it.

TODO(yuxu/memory-#5): this daemon runs bare — it doesn't load
performance_ranker, so retrievals via `yuxu-memory` don't bump
`score.applied` in memory frontmatter. For that, the daemon would need
to publish `memory.retrieved` events to the running yuxu process (or
load performance_ranker in-process). Fine for now: daemon is an
observation tool, not a canonical retrieval path. When daemon-sourced
retrievals need to count toward promotion, wire bus.publish here.

Environment
-----------
    YUXU_MEMORY_SOCK   socket path        (default /tmp/yuxu_memory.sock)
    YUXU_MEMORY_ROOT   memory directory   (default Claude Code auto-memory
                                           path for theme-flow-engine)
    YUXU_MEMORY_QLOG   query log path     (default <memory_root>/_queries.jsonl)
                                           Set to empty string to disable.
    LOG_LEVEL          log level          (default INFO)

Usage
-----
    python examples/memory_daemon.py &          # background
    echo '{"op":"stats"}' | nc -U /tmp/yuxu_memory.sock
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from typing import Optional

from yuxu.bundled.memory.handler import execute as memory_execute
from yuxu.core.bus import Bus

log = logging.getLogger("memory_daemon")

DEFAULT_SOCKET = os.environ.get("YUXU_MEMORY_SOCK", "/tmp/yuxu_memory.sock")
DEFAULT_MEMORY_ROOT = os.environ.get(
    "YUXU_MEMORY_ROOT",
    str(Path.home() / ".claude" / "projects"
         / "-home-xzp-project-theme-flow-engine" / "memory"),
)
# YUXU_MEMORY_QLOG: explicit path overrides. `""` (empty env) disables
# logging; `None` (env unset) → default under memory_root.
_ENV_QLOG = os.environ.get("YUXU_MEMORY_QLOG", None)


def _result_summary(op: Optional[str], resp: dict) -> dict:
    """Extract a compact result fingerprint for the query log.

    Deliberately small: just enough to let a later validation pass
    spot patterns like 'searched N times, 0 hits' or 'asked for section
    X which didn't exist'. Full request args and full response stay in
    the daemon memory only; we don't bloat the log with them.
    """
    out: dict = {}
    if not isinstance(resp, dict):
        return out
    if op == "search":
        entries = resp.get("entries") or []
        out["hit_count"] = len(entries)
        out["top_paths"] = [e.get("path") for e in entries[:3] if isinstance(e, dict)]
    elif op == "list":
        entries = resp.get("entries") or []
        out["hit_count"] = len(entries)
        out["mode"] = resp.get("mode")
    elif op == "get":
        out["path"] = resp.get("path")
        if resp.get("section") is not None:
            out["section"] = resp.get("section")
            out["section_hit"] = resp.get("section_body") is not None
        out["bytes"] = resp.get("bytes")
    elif op == "stats":
        out["total"] = resp.get("total")
    return out


def _write_qlog(path: Optional[Path], *, op: Optional[str], req: dict,
                  resp: dict, elapsed_ms: float) -> None:
    """Append one JSONL line. Best-effort: log-write failure never
    impacts the request response."""
    if path is None:
        return
    try:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            "op": op,
            "query": req.get("query") if op == "search" else None,
            "path": req.get("path") if op == "get" else None,
            "mode": req.get("mode"),
            "ok": bool(resp.get("ok")),
            "elapsed_ms": round(elapsed_ms, 2),
            "error": resp.get("error") if not resp.get("ok") else None,
            "result": _result_summary(op, resp),
        }
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        log.exception("qlog write failed")


async def _handle_client(reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter,
                          *, memory_root: str, ctx,
                          qlog_path: Optional[Path]) -> None:
    peer = writer.get_extra_info("peername") or "?"
    req: dict = {}
    op: Optional[str] = None
    t0 = time.monotonic()
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
        if not raw:
            return
        try:
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("top-level JSON must be object")
            req = parsed
            op = req.get("op")
        except (json.JSONDecodeError, ValueError) as e:
            resp = {"ok": False, "error": f"bad request: {e}"}
        else:
            req.setdefault("memory_root", memory_root)
            try:
                resp = await memory_execute(req, ctx)
            except Exception as e:  # noqa: BLE001
                log.exception("memory.execute raised")
                resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        writer.write((json.dumps(resp, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
        await writer.drain()
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        log.info("req op=%s from=%s ok=%s elapsed=%.1fms",
                  op, peer, resp.get("ok"), elapsed_ms)
        _write_qlog(qlog_path, op=op, req=req, resp=resp, elapsed_ms=elapsed_ms)
    except asyncio.TimeoutError:
        log.warning("client %s timed out reading request", peer)
    except Exception:  # noqa: BLE001
        log.exception("client %s crashed", peer)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def _run() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sock_path = DEFAULT_SOCKET
    memory_root = str(Path(DEFAULT_MEMORY_ROOT).expanduser().resolve())

    if not Path(memory_root).exists():
        log.error("memory_root does not exist: %s", memory_root)
        return 1

    # Resolve query-log path.
    # - env unset  → default to <memory_root>/_queries.jsonl
    # - env == ""  → explicitly disabled
    # - env set    → use verbatim
    qlog_path: Optional[Path]
    if _ENV_QLOG is None:
        qlog_path = Path(memory_root) / "_queries.jsonl"
    elif _ENV_QLOG == "":
        qlog_path = None
    else:
        qlog_path = Path(_ENV_QLOG).expanduser().resolve()

    # Minimal ctx: memory skill touches ctx.bus (best-effort publish) and
    # ctx.agent_dir (only when memory_root isn't passed — we always pass it).
    bus = Bus()
    ctx = SimpleNamespace(bus=bus, agent_dir=Path(memory_root).parent)

    # Clean stale socket (previous crash).
    if os.path.exists(sock_path):
        try:
            os.unlink(sock_path)
        except OSError as e:
            log.error("can't remove stale socket %s: %s", sock_path, e)
            return 1

    server = await asyncio.start_unix_server(
        lambda r, w: _handle_client(r, w, memory_root=memory_root,
                                      ctx=ctx, qlog_path=qlog_path),
        path=sock_path,
    )
    os.chmod(sock_path, 0o600)  # owner-only

    banner = (f"[memory_daemon] listening on {sock_path}\n"
               f"[memory_daemon] memory_root = {memory_root}\n"
               f"[memory_daemon] qlog        = {qlog_path or '(disabled)'}")
    log.info(banner.replace("\n", " | "))
    print(banner, flush=True)

    stop = asyncio.Event()

    def _stop(*_):
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    async with server:
        serve_task = asyncio.create_task(server.serve_forever())
        stop_task = asyncio.create_task(stop.wait())
        done, pending = await asyncio.wait(
            {serve_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()

    try:
        os.unlink(sock_path)
    except OSError:
        pass
    log.info("memory_daemon exited")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_run()))
    except KeyboardInterrupt:
        sys.exit(130)
