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
client (`bin/yum`) applies it.

Environment
-----------
    YUXU_MEMORY_SOCK   socket path        (default /tmp/yuxu_memory.sock)
    YUXU_MEMORY_ROOT   memory directory   (default Claude Code auto-memory
                                           path for theme-flow-engine)
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
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yuxu.bundled.memory.handler import execute as memory_execute
from yuxu.core.bus import Bus

log = logging.getLogger("memory_daemon")

DEFAULT_SOCKET = os.environ.get("YUXU_MEMORY_SOCK", "/tmp/yuxu_memory.sock")
DEFAULT_MEMORY_ROOT = os.environ.get(
    "YUXU_MEMORY_ROOT",
    str(Path.home() / ".claude" / "projects"
         / "-home-xzp-project-theme-flow-engine" / "memory"),
)


async def _handle_client(reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter,
                          *, memory_root: str, ctx) -> None:
    peer = writer.get_extra_info("peername") or "?"
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
        if not raw:
            return
        try:
            req = json.loads(raw.decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError("top-level JSON must be object")
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
        log.info("req op=%s from=%s ok=%s",
                  (req.get("op") if isinstance(req, dict) else "?"),
                  peer, resp.get("ok"))
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
        lambda r, w: _handle_client(r, w, memory_root=memory_root, ctx=ctx),
        path=sock_path,
    )
    os.chmod(sock_path, 0o600)  # owner-only

    banner = (f"[memory_daemon] listening on {sock_path}\n"
               f"[memory_daemon] memory_root = {memory_root}")
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
