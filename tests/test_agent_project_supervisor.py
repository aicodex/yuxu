from __future__ import annotations

import asyncio
import textwrap

import pytest

from yuxu.bundled.project_supervisor.handler import ProjectSupervisor
from yuxu.core.bus import Bus
from yuxu.core.loader import Loader

pytestmark = pytest.mark.asyncio


def _write_agent(root, name, *, fm, init_src):
    import yaml as _yaml
    d = root / name
    d.mkdir(parents=True)
    (d / "AGENT.md").write_text(f"---\n{_yaml.safe_dump(fm, sort_keys=False).strip()}\n---\n")
    (d / "__init__.py").write_text(textwrap.dedent(init_src))
    return d


async def _yield(n=6):
    for _ in range(n):
        await asyncio.sleep(0)


# -- helpers: a flaky-then-stable persistent agent ----------------


def _write_flaky_agent(root, name, crash_counter_file, crash_count=2):
    """Agent that crashes `crash_count` times then stabilizes.

    Crash happens inside start() AFTER bus.ready() so the loader task
    transitions through ready → failed, which the supervisor subscribes to.
    """
    _write_agent(root, name, fm={"run_mode": "persistent"}, init_src=f"""
        from pathlib import Path
        _CRASH = Path({str(crash_counter_file)!r})

        async def start(ctx):
            n = int(_CRASH.read_text() or "0") if _CRASH.exists() else 0
            _CRASH.write_text(str(n + 1))
            async def handler(msg):
                return {{"ok": True, "n": n + 1}}
            ctx.bus.register({name!r}, handler)
            await ctx.ready()
            if n < {crash_count}:
                raise RuntimeError("flaky crash #" + str(n + 1))
    """)


# -- unit tests --------------------------------------------------


async def test_supervisor_restarts_failed_persistent_agent(tmp_path):
    counter = tmp_path / "count.txt"
    _write_flaky_agent(tmp_path, "flaky", counter)

    bus = Bus()
    loader = Loader(bus, dirs=[str(tmp_path)])
    await loader.scan()

    # Install supervisor BEFORE first start so it sees the initial crash.
    supervisor = ProjectSupervisor(bus, loader, restart_delay=0, max_restarts=5)
    supervisor.install()

    restarts = []
    bus.subscribe("project_supervisor.restarted",
                  lambda ev: restarts.append(ev["payload"]))

    # First start: crashes after bus.ready(); loader reports ready then failed.
    try:
        await loader.ensure_running("flaky")
    except Exception:
        pass  # start raised after ready; that's the flaky behaviour

    # Wait for the restart cycle to stabilize
    for _ in range(50):
        if bus.query_status("flaky") == "ready" and len(restarts) >= 2:
            break
        await asyncio.sleep(0.05)

    assert bus.query_status("flaky") == "ready"
    assert len(restarts) >= 2
    r = await bus.request("flaky", None, timeout=1.0)
    assert r["ok"] is True


async def test_supervisor_ignores_non_persistent(tmp_path):
    _write_agent(tmp_path, "ephemeral", fm={"run_mode": "one_shot"}, init_src="""
        async def start(ctx):
            await ctx.ready()
    """)
    bus = Bus()
    loader = Loader(bus, dirs=[str(tmp_path)])
    await loader.scan()
    supervisor = ProjectSupervisor(bus, loader, restart_delay=0)
    supervisor.install()

    # Simulate failure of a one_shot agent
    await bus.publish_status("ephemeral", "failed")
    await _yield()
    assert "ephemeral" not in supervisor._restarts


async def test_supervisor_ignores_self(tmp_path):
    bus = Bus()
    loader = Loader(bus, dirs=[str(tmp_path)])
    supervisor = ProjectSupervisor(bus, loader)
    supervisor.install()
    await bus.publish_status("project_supervisor", "failed")
    await _yield()
    assert "project_supervisor" not in supervisor._restarts


async def test_supervisor_gives_up_after_max_restarts(tmp_path):
    # An always-failing persistent agent
    _write_agent(tmp_path, "broken", fm={"run_mode": "persistent"}, init_src="""
        async def start(ctx):
            raise RuntimeError("always broken")
    """)
    bus = Bus()
    loader = Loader(bus, dirs=[str(tmp_path)])
    await loader.scan()
    # Don't ensure_running — force failed status directly
    await bus.publish_status("broken", "failed")

    supervisor = ProjectSupervisor(bus, loader, restart_delay=0, max_restarts=2)
    supervisor.install()

    giveups = []
    bus.subscribe("project_supervisor.giveup",
                  lambda ev: giveups.append(ev["payload"]))

    # Trigger multiple failures; each try will restart (and fail) → count grows
    for _ in range(5):
        await bus.publish_status("broken", "failed")
        # allow the scheduled restart task to run
        for _ in range(20):
            await asyncio.sleep(0.01)
    assert len(giveups) >= 1
    assert giveups[0]["agent"] == "broken"


async def test_supervisor_restart_failure_emits_event(tmp_path):
    _write_agent(tmp_path, "bad", fm={"run_mode": "persistent"}, init_src="""
        async def start(ctx):
            raise RuntimeError("broken")
    """)
    bus = Bus()
    loader = Loader(bus, dirs=[str(tmp_path)])
    await loader.scan()
    await bus.publish_status("bad", "failed")  # pre-mark failed

    supervisor = ProjectSupervisor(bus, loader, restart_delay=0, max_restarts=5)
    supervisor.install()

    fails = []
    bus.subscribe("project_supervisor.restart_failed",
                  lambda ev: fails.append(ev["payload"]))
    await supervisor._attempt_restart("bad")
    await _yield()
    assert fails and fails[0]["agent"] == "bad"


async def test_scan_and_heal_fixes_already_failed(tmp_path):
    counter = tmp_path / "count.txt"
    _write_flaky_agent(tmp_path, "failing_then_ok", counter)

    bus = Bus()
    loader = Loader(bus, dirs=[str(tmp_path)])
    await loader.scan()
    # Start without a supervisor so nobody restarts it; confirm it ends failed.
    try:
        await loader.ensure_running("failing_then_ok")
    except Exception:
        pass
    for _ in range(20):
        if bus.query_status("failing_then_ok") == "failed":
            break
        await asyncio.sleep(0.02)
    assert bus.query_status("failing_then_ok") == "failed"

    supervisor = ProjectSupervisor(bus, loader, restart_delay=0, max_restarts=5)
    supervisor.install()
    await supervisor.scan_and_heal()

    for _ in range(50):
        if bus.query_status("failing_then_ok") == "ready":
            break
        await asyncio.sleep(0.05)
    assert bus.query_status("failing_then_ok") == "ready"


async def test_handle_report_and_reset(tmp_path):
    from collections import deque
    bus = Bus()
    loader = Loader(bus, dirs=[str(tmp_path)])
    supervisor = ProjectSupervisor(bus, loader)
    import time
    supervisor._restarts["x"] = deque([time.monotonic()])
    supervisor._give_ups.append({"agent": "y"})

    class _M: payload = {"op": "report"}
    r = await supervisor.handle(_M())
    assert r["ok"] is True
    assert "x" in r["restarts"]
    assert r["give_ups"]

    class _R: payload = {"op": "reset"}
    r = await supervisor.handle(_R())
    assert r["ok"] is True
    assert supervisor._restarts == {}
    assert supervisor._give_ups == []


async def test_handle_unknown_op(tmp_path):
    bus = Bus()
    loader = Loader(bus, dirs=[str(tmp_path)])
    supervisor = ProjectSupervisor(bus, loader)
    class _M: payload = {"op": "foo"}
    r = await supervisor.handle(_M())
    assert r["ok"] is False


# -- bundled integration -----------------------------------------

async def test_integration_via_loader(bundled_dir):
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("project_supervisor")
    assert bus.query_status("project_supervisor") == "ready"
    r = await bus.request("project_supervisor", {"op": "report"}, timeout=2.0)
    assert r["ok"] is True
    assert r["max_restarts"] == 5
