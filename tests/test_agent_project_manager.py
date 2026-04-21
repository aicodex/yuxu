from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from yuxu.bundled.project_manager.handler import ProjectManager
from yuxu.core.bus import Bus
from yuxu.core.loader import Loader

pytestmark = pytest.mark.asyncio


@pytest.fixture
def yuxu_home(tmp_path, monkeypatch):
    """Redirect ~/.yuxu to a temp dir so tests don't touch real user home."""
    home = tmp_path / "_yuxu_home"
    monkeypatch.setenv("YUXU_HOME", str(home))
    return home


# -- create_project --------------------------------------------


async def test_create_project_scaffolds_expected_layout(tmp_path, yuxu_home):
    project = tmp_path / "myproj"
    p = ProjectManager.create_project(project)
    assert p == project.resolve()
    assert (p / "yuxu.json").exists()
    for d in ("agents", "skills", "_system", "config",
              "data/checkpoints", "data/logs", "data/memory", "data/sessions",
              ".yuxu"):
        assert (p / d).is_dir(), f"missing {d}"
    for f in (".gitignore", "config/rate_limits.yaml", "config/skills_enabled.yaml",
              ".yuxu/version", ".yuxu/manifest.json"):
        assert (p / f).exists(), f"missing {f}"


async def test_create_project_extracts_all_bundled(tmp_path, yuxu_home):
    p = ProjectManager.create_project(tmp_path / "proj")
    system = p / "_system"
    # every bundled agent appears
    names = sorted(d.name for d in system.iterdir() if d.is_dir())
    for expected in ("checkpoint_store", "rate_limit_service", "llm_service",
                     "llm_driver", "project_supervisor", "recovery_agent",
                     "resource_guardian", "skill_picker", "project_manager"):
        assert expected in names, f"{expected} not extracted"


async def test_create_project_manifest(tmp_path, yuxu_home):
    p = ProjectManager.create_project(tmp_path / "proj")
    manifest = json.loads((p / ".yuxu" / "manifest.json").read_text())
    names = {item["name"] for item in manifest["bundled"]}
    assert "checkpoint_store" in names
    # every entry has a sha (since all bundled agents have AGENT.md)
    assert all(item.get("agent_md_sha12") for item in manifest["bundled"])


async def test_create_project_refuses_existing_without_force(tmp_path, yuxu_home):
    p = tmp_path / "proj"
    ProjectManager.create_project(p)
    with pytest.raises(FileExistsError):
        ProjectManager.create_project(p)
    # force works
    ProjectManager.create_project(p, force=True)


async def test_create_project_registers_in_home(tmp_path, yuxu_home):
    p1 = ProjectManager.create_project(tmp_path / "p1")
    p2 = ProjectManager.create_project(tmp_path / "p2")
    listed = ProjectManager.list_projects()
    paths = [item["path"] for item in listed]
    assert str(p1) in paths
    assert str(p2) in paths


# -- create_agent ----------------------------------------------


async def test_create_agent_copies_template(tmp_path, yuxu_home):
    p = ProjectManager.create_project(tmp_path / "proj")
    agent = ProjectManager.create_agent(p, "my_bot")
    assert (agent / "AGENT.md").exists()
    assert (agent / "__init__.py").exists()
    # name substitution happened
    init_src = (agent / "__init__.py").read_text(encoding="utf-8")
    assert 'NAME = "my_bot"' in init_src
    assert 'NAME = "my_agent"' not in init_src


async def test_create_agent_rejects_duplicate(tmp_path, yuxu_home):
    p = ProjectManager.create_project(tmp_path / "proj")
    ProjectManager.create_agent(p, "dup")
    with pytest.raises(FileExistsError):
        ProjectManager.create_agent(p, "dup")


async def test_create_agent_rejects_missing_project(tmp_path, yuxu_home):
    with pytest.raises(FileNotFoundError):
        ProjectManager.create_agent(tmp_path / "nope", "x")


async def test_create_agent_unknown_template_fails(tmp_path, yuxu_home):
    p = ProjectManager.create_project(tmp_path / "proj")
    with pytest.raises(FileNotFoundError):
        ProjectManager.create_agent(p, "x", template="nonexistent")


# -- list_projects ---------------------------------------------


async def test_list_projects_empty(yuxu_home):
    assert ProjectManager.list_projects() == []


async def test_list_projects_shows_version_and_name(tmp_path, yuxu_home):
    p = ProjectManager.create_project(tmp_path / "alpha")
    listed = ProjectManager.list_projects()
    found = [item for item in listed if item["path"] == str(p)]
    assert len(found) == 1
    assert found[0]["name"] == "alpha"
    assert found[0]["exists"] is True
    assert found[0].get("yuxu_version")


async def test_list_projects_marks_missing(tmp_path, yuxu_home):
    # Manually add a bogus path to projects.yaml
    yuxu_home.mkdir()
    (yuxu_home / "projects.yaml").write_text(
        yaml.safe_dump({"projects": ["/nonexistent/path"]})
    )
    listed = ProjectManager.list_projects()
    assert listed[0]["path"] == "/nonexistent/path"
    assert listed[0]["exists"] is False


# -- list_agents -----------------------------------------------


async def test_list_agents_shows_bundled_and_user(tmp_path, yuxu_home):
    p = ProjectManager.create_project(tmp_path / "proj")
    ProjectManager.create_agent(p, "user_bot")
    agents = ProjectManager.list_agents(p)
    bundled_names = {a["name"] for a in agents if a["source"] == "bundled"}
    user_names = {a["name"] for a in agents if a["source"] == "user"}
    assert "llm_service" in bundled_names
    assert "user_bot" in user_names


async def test_list_agents_rejects_missing_project(tmp_path, yuxu_home):
    with pytest.raises(FileNotFoundError):
        ProjectManager.list_agents(tmp_path / "nope")


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


async def test_handle_create_project_op(tmp_path, yuxu_home):
    pm = ProjectManager()
    r = await pm.handle(_Msg({"op": "create_project", "dir": str(tmp_path / "proj")}))
    assert r["ok"] is True
    assert Path(r["path"]).is_dir()


async def test_handle_create_agent_op(tmp_path, yuxu_home):
    pm = ProjectManager()
    p = ProjectManager.create_project(tmp_path / "proj")
    r = await pm.handle(_Msg({"op": "create_agent",
                               "project_dir": str(p),
                               "name": "bob"}))
    assert r["ok"] is True
    assert Path(r["path"]).name == "bob"


async def test_handle_list_projects_op(tmp_path, yuxu_home):
    ProjectManager.create_project(tmp_path / "proj")
    pm = ProjectManager()
    r = await pm.handle(_Msg({"op": "list_projects"}))
    assert r["ok"] is True
    assert len(r["projects"]) == 1


async def test_handle_list_agents_op(tmp_path, yuxu_home):
    p = ProjectManager.create_project(tmp_path / "proj")
    pm = ProjectManager()
    r = await pm.handle(_Msg({"op": "list_agents", "project_dir": str(p)}))
    assert r["ok"] is True
    assert any(a["name"] == "llm_service" for a in r["agents"])


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
    pm = ProjectManager()
    r = await pm.handle(_Msg({"op": "create_project"}))
    assert r["ok"] is False
    assert "missing field" in r["error"]


async def test_handle_already_exists(tmp_path, yuxu_home):
    p = ProjectManager.create_project(tmp_path / "proj")
    ProjectManager.create_agent(p, "dup")
    pm = ProjectManager()
    r = await pm.handle(_Msg({"op": "create_agent",
                               "project_dir": str(p),
                               "name": "dup"}))
    assert r["ok"] is False
    assert "already exists" in r["error"]


# -- full loader integration -----------------------------------


async def test_project_manager_starts_via_loader_and_exposes_handle(bundled_dir):
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("project_manager")
    assert bus.query_status("project_manager") == "ready"

    # handle exposed
    pm = loader.get_handle("project_manager")
    assert pm is not None
    # and bus op works
    r = await bus.request("project_manager", {"op": "list_projects"}, timeout=2.0)
    assert r["ok"] is True
    # dynamic op works because agent has loader
    r = await bus.request("project_manager",
                           {"op": "get_state", "name": "project_manager"},
                           timeout=2.0)
    assert r["ok"] is True
