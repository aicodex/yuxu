"""ProjectManager — runtime supervisor surface for the loader.

After the skill extraction (project_manager → skills_bundled.{create_project,
create_agent, list_projects, list_agents}), this agent only owns the dynamic
ops (start/stop/restart/get_state). Static scaffolding lives in skill tests.
"""
from __future__ import annotations

import pytest

from yuxu.bundled.project_manager.handler import ProjectManager
from yuxu.core.bus import Bus
from yuxu.core.loader import Loader

pytestmark = pytest.mark.asyncio


# -- dynamic ops (with fake loader) ----------------------------


class _FakeLoader:
    def __init__(self):
        self.calls = []

    async def ensure_running(self, name):
        self.calls.append(("start", name))
        return "ready"

    async def stop(self, name, cascade=False):
        self.calls.append(("stop", name, cascade))

    async def restart(self, name):
        self.calls.append(("restart", name))
        return "ready"

    def get_state(self, name=None):
        return {"name": name, "status": "ready"} if name else {"x": "ready"}


async def test_dynamic_ops_require_loader():
    pm = ProjectManager(loader=None)
    r = await pm.start_agent("x")
    assert r["ok"] is False
    assert "not running" in r["error"]


async def test_start_agent_routes_to_loader():
    loader = _FakeLoader()
    pm = ProjectManager(loader=loader)
    r = await pm.start_agent("foo")
    assert r == {"ok": True, "status": "ready"}
    assert loader.calls == [("start", "foo")]


async def test_stop_agent_routes_to_loader():
    loader = _FakeLoader()
    pm = ProjectManager(loader=loader)
    r = await pm.stop_agent("foo", cascade=True)
    assert r == {"ok": True}
    assert loader.calls == [("stop", "foo", True)]


async def test_restart_agent_routes_to_loader():
    loader = _FakeLoader()
    pm = ProjectManager(loader=loader)
    r = await pm.restart_agent("foo")
    assert r == {"ok": True, "status": "ready"}


async def test_get_state_routes_to_loader():
    loader = _FakeLoader()
    pm = ProjectManager(loader=loader)
    assert pm.get_state() == {"ok": True, "state": {"x": "ready"}}
    assert pm.get_state("foo") == {"ok": True, "state": {"name": "foo", "status": "ready"}}


# -- handle() dispatcher ---------------------------------------


class _Msg:
    def __init__(self, payload):
        self.payload = payload


async def test_handle_dynamic_op_requires_loader():
    pm = ProjectManager(loader=None)
    r = await pm.handle(_Msg({"op": "start_agent", "name": "x"}))
    assert r["ok"] is False


async def test_handle_unknown_op():
    pm = ProjectManager()
    r = await pm.handle(_Msg({"op": "weird"}))
    assert r["ok"] is False
    assert "unknown op" in r["error"]


async def test_handle_missing_field():
    pm = ProjectManager(loader=_FakeLoader())
    r = await pm.handle(_Msg({"op": "start_agent"}))
    assert r["ok"] is False
    assert "missing field: name" in r["error"]


async def test_handle_static_op_no_longer_recognized(tmp_path):
    """create_project / list_projects etc. were extracted to skills; the
    agent should no longer respond to those op names."""
    pm = ProjectManager()
    for op in ("create_project", "create_agent", "list_projects", "list_agents"):
        r = await pm.handle(_Msg({"op": op}))
        assert r["ok"] is False
        assert "unknown op" in r["error"]


# -- full loader integration -----------------------------------


async def test_project_manager_starts_via_loader_and_exposes_handle(bundled_dir):
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("project_manager")
    assert bus.query_status("project_manager") == "ready"

    pm = loader.get_handle("project_manager")
    assert pm is not None
    # dynamic op works because agent has loader
    r = await bus.request("project_manager",
                           {"op": "get_state", "name": "project_manager"},
                           timeout=2.0)
    assert r["ok"] is True
