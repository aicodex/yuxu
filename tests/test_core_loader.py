from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from yuxu.core.bus import Bus
from yuxu.core.loader import Loader

pytestmark = pytest.mark.asyncio


def _write_agent(root: Path, name: str, *, fm: dict | None = None,
                 init_src: str | None = None, body: str = "") -> Path:
    d = root / name
    d.mkdir(parents=True)
    if fm is not None or body:
        import yaml as _yaml
        fm_text = _yaml.safe_dump(fm or {}, sort_keys=False).strip()
        (d / "AGENT.md").write_text(f"---\n{fm_text}\n---\n{body}\n")
    if init_src is not None:
        (d / "__init__.py").write_text(textwrap.dedent(init_src))
    return d


async def test_scan_finds_agents(tmp_path):
    _write_agent(tmp_path, "alpha", fm={"run_mode": "persistent"}, init_src="")
    _write_agent(tmp_path, "beta", fm={"driver": "llm", "run_mode": "one_shot"})
    # ignored: no AGENT.md and no __init__.py
    (tmp_path / "junk").mkdir()
    (tmp_path / "junk" / "README.md").write_text("nope")

    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    await loader.scan()
    assert set(loader.specs.keys()) == {"alpha", "beta"}
    assert loader.specs["alpha"].run_mode == "persistent"
    assert loader.specs["alpha"].driver == "python"  # has __init__
    assert loader.specs["beta"].driver == "llm"


async def test_scan_override_user_over_bundled(tmp_path, caplog):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    _write_agent(bundled, "shared", fm={"run_mode": "persistent", "scope": "system"}, init_src="")
    _write_agent(user, "shared", fm={"run_mode": "persistent", "scope": "user"}, init_src="")
    bus = Bus()
    loader = Loader(bus, [str(bundled), str(user)])
    await loader.scan()
    assert loader.specs["shared"].scope == "user"


async def test_build_dep_graph_ok(tmp_path):
    _write_agent(tmp_path, "a", fm={"depends_on": ["b"]}, init_src="")
    _write_agent(tmp_path, "b", fm={"depends_on": ["c"]}, init_src="")
    _write_agent(tmp_path, "c", fm={}, init_src="")
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    await loader.scan()
    order = loader.build_dep_graph()
    assert order.index("c") < order.index("b") < order.index("a")


async def test_build_dep_graph_cycle(tmp_path):
    _write_agent(tmp_path, "a", fm={"depends_on": ["b"]}, init_src="")
    _write_agent(tmp_path, "b", fm={"depends_on": ["a"]}, init_src="")
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    await loader.scan()
    with pytest.raises(RuntimeError, match="circular"):
        loader.build_dep_graph()


async def test_build_dep_graph_unknown_dep(tmp_path):
    _write_agent(tmp_path, "a", fm={"depends_on": ["ghost"]}, init_src="")
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    await loader.scan()
    with pytest.raises(RuntimeError, match="unknown agent: ghost"):
        loader.build_dep_graph()


async def test_ensure_running_simple(tmp_path):
    init = """
        async def start(ctx):
            async def handler(msg):
                return {"ok": True}
            ctx.bus.register("alpha", handler)
            await ctx.ready()
    """
    _write_agent(tmp_path, "alpha", fm={"run_mode": "persistent"}, init_src=init)
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    await loader.scan()
    status = await loader.ensure_running("alpha")
    assert status == "ready"
    reply = await bus.request("alpha", "hello", timeout=1.0)
    assert reply == {"ok": True}


async def test_ensure_running_recurses_deps(tmp_path):
    order = []
    # Share state via a module-level file; use file-backed marker.
    marker = tmp_path / "start_order.log"
    marker.write_text("")
    for name, deps in [("a", ["b"]), ("b", ["c"]), ("c", [])]:
        init = f"""
            async def start(ctx):
                from pathlib import Path
                Path({str(marker)!r}).open("a").write({name!r} + "\\n")
                await ctx.ready()
        """
        _write_agent(tmp_path, name, fm={"run_mode": "persistent", "depends_on": deps}, init_src=init)
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    await loader.scan()
    await loader.ensure_running("a")
    seq = marker.read_text().split()
    assert seq == ["c", "b", "a"]
    for n in ("a", "b", "c"):
        assert bus.query_status(n) == "ready"


async def test_ensure_running_idempotent(tmp_path):
    init = """
        _calls = [0]
        async def start(ctx):
            _calls[0] += 1
            await ctx.ready()
    """
    _write_agent(tmp_path, "x", fm={"run_mode": "persistent"}, init_src=init)
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    await loader.scan()
    await loader.ensure_running("x")
    await loader.ensure_running("x")
    # module's _calls; second ensure_running must not re-import/start
    mod = loader.modules["x"]
    assert mod._calls[0] == 1


async def test_ensure_running_persistent_with_background_task(tmp_path):
    init = """
        import asyncio

        async def start(ctx):
            async def _loop():
                await ctx.ready()
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    raise
            asyncio.create_task(_loop())
    """
    _write_agent(tmp_path, "svc", fm={"run_mode": "persistent"}, init_src=init)
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    await loader.scan()
    status = await loader.ensure_running("svc")
    assert status == "ready"


async def test_ensure_running_timeout_marks_failed(tmp_path):
    init = """
        async def start(ctx):
            # never calls ctx.ready()
            import asyncio
            await asyncio.sleep(10)
    """
    _write_agent(tmp_path, "hang", fm={"run_mode": "one_shot", "ready_timeout": 0.1}, init_src=init)
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    await loader.scan()
    with pytest.raises(Exception):
        await loader.ensure_running("hang")
    assert bus.query_status("hang") == "failed"


async def test_ensure_running_empty_init_auto_ready(tmp_path):
    """An __init__.py with no start() is valid — loader auto-marks ready."""
    _write_agent(tmp_path, "empty", fm={"run_mode": "persistent"},
                 init_src="# no start function\n")
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    await loader.scan()
    status = await loader.ensure_running("empty")
    assert status == "ready"


async def test_stop_cancels_task(tmp_path):
    init = """
        import asyncio
        async def start(ctx):
            async def _loop():
                await ctx.ready()
                await asyncio.sleep(10)
            asyncio.create_task(_loop())
            # return; loader sees 'ready' via wait_for_service
    """
    _write_agent(tmp_path, "svc", fm={"run_mode": "persistent"}, init_src=init)
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    await loader.scan()
    await loader.ensure_running("svc")
    await loader.stop("svc")
    assert bus.query_status("svc") == "stopped"


async def test_filter_by_run_mode(tmp_path):
    _write_agent(tmp_path, "p", fm={"run_mode": "persistent"}, init_src="")
    _write_agent(tmp_path, "o", fm={"run_mode": "one_shot"}, init_src="")
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    await loader.scan()
    assert [s.name for s in loader.filter(run_mode="persistent")] == ["p"]
    assert [s.name for s in loader.filter(run_mode="one_shot")] == ["o"]


async def test_start_receives_context_with_name_and_dir(tmp_path):
    init = """
        _captured = {}
        async def start(ctx):
            _captured["name"] = ctx.name
            _captured["dir"] = str(ctx.agent_dir)
            _captured["fm_keys"] = sorted(ctx.frontmatter.keys())
            await ctx.ready()
    """
    _write_agent(tmp_path, "introspect",
                 fm={"run_mode": "persistent", "version": "0.1.0"},
                 init_src=init)
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    await loader.scan()
    await loader.ensure_running("introspect")
    mod = loader.modules["introspect"]
    assert mod._captured["name"] == "introspect"
    assert "introspect" in mod._captured["dir"]
    assert "run_mode" in mod._captured["fm_keys"]


async def test_get_handle_returns_module_exposed_object(tmp_path):
    init = """
        _service = {"value": 42}
        async def start(ctx):
            await ctx.ready()
        def get_handle(ctx):
            return _service
    """
    _write_agent(tmp_path, "svc", fm={"run_mode": "persistent"}, init_src=init)
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    await loader.scan()
    await loader.ensure_running("svc")
    h = loader.get_handle("svc")
    assert h == {"value": 42}


async def test_get_handle_returns_none_for_unexposed(tmp_path):
    _write_agent(tmp_path, "plain", fm={"run_mode": "persistent"},
                 init_src="async def start(ctx): await ctx.ready()\n")
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    await loader.scan()
    await loader.ensure_running("plain")
    assert loader.get_handle("plain") is None


async def test_get_handle_returns_none_for_unloaded(tmp_path):
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    assert loader.get_handle("nope") is None


async def test_ctx_get_agent_cross_access(tmp_path):
    a_init = """
        _shared = {"from": "a"}
        async def start(ctx):
            await ctx.ready()
        def get_handle(ctx):
            return _shared
    """
    b_init = """
        _captured = {}
        async def start(ctx):
            _captured["handle"] = ctx.get_agent("agent_a")
            await ctx.ready()
    """
    _write_agent(tmp_path, "agent_a", fm={"run_mode": "persistent"}, init_src=a_init)
    _write_agent(tmp_path, "agent_b",
                 fm={"run_mode": "persistent", "depends_on": ["agent_a"]},
                 init_src=b_init)
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    await loader.scan()
    await loader.ensure_running("agent_b")
    mod = loader.modules["agent_b"]
    assert mod._captured["handle"] == {"from": "a"}


async def test_stop_hook_called(tmp_path):
    init = """
        _called = {"start": 0, "stop": 0}
        async def start(ctx):
            _called["start"] += 1
            await ctx.ready()
        async def stop(ctx):
            _called["stop"] += 1
    """
    _write_agent(tmp_path, "hooked", fm={"run_mode": "persistent"}, init_src=init)
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    await loader.scan()
    await loader.ensure_running("hooked")
    await loader.stop("hooked")
    assert loader.modules["hooked"]._called == {"start": 1, "stop": 1}


async def test_stop_hook_exception_does_not_break_stop(tmp_path):
    init = """
        async def start(ctx):
            await ctx.ready()
        async def stop(ctx):
            raise RuntimeError("flaky stop")
    """
    _write_agent(tmp_path, "bad_stop", fm={"run_mode": "persistent"}, init_src=init)
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    await loader.scan()
    await loader.ensure_running("bad_stop")
    # Must not raise; agent still moves to stopped.
    await loader.stop("bad_stop")
    assert bus.query_status("bad_stop") == "stopped"


async def test_stop_hook_timeout(tmp_path):
    init = """
        import asyncio
        async def start(ctx):
            await ctx.ready()
        async def stop(ctx):
            await asyncio.sleep(30)
    """
    _write_agent(tmp_path, "slow_stop", fm={"run_mode": "persistent"}, init_src=init)
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    loader.STOP_HOOK_TIMEOUT = 0.1
    await loader.scan()
    await loader.ensure_running("slow_stop")
    await loader.stop("slow_stop")  # must not hang
    assert bus.query_status("slow_stop") == "stopped"


async def test_get_dep_graph_and_state(tmp_path):
    _write_agent(tmp_path, "a", fm={"depends_on": ["b"]}, init_src="")
    _write_agent(tmp_path, "b", fm={}, init_src="")
    bus = Bus()
    loader = Loader(bus, [str(tmp_path)])
    await loader.scan()
    g = loader.get_dep_graph()
    assert g == {"a": ["b"], "b": []}
    st = loader.get_state()
    assert st == {"a": "unloaded", "b": "unloaded"}
