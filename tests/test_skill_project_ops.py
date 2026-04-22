"""Skill coverage for the four project-scaffolding skills extracted from
project_manager: create_project / create_agent / list_projects / list_agents.

Tests both the sync library entry (used by the CLI) and the async `execute`
skill-protocol entry, plus a Loader discovery sanity check."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from yuxu.bundled.create_agent.handler import (
    create_agent,
    execute as create_agent_execute,
)
from yuxu.bundled.create_project.handler import (
    create_project,
    execute as create_project_execute,
)
from yuxu.bundled.list_agents.handler import (
    execute as list_agents_execute,
    list_agents,
)
from yuxu.bundled.list_projects.handler import (
    execute as list_projects_execute,
    list_projects,
)


@pytest.fixture
def yuxu_home(tmp_path, monkeypatch):
    home = tmp_path / "_yuxu_home"
    monkeypatch.setenv("YUXU_HOME", str(home))
    return home


# -- create_project ---------------------------------------------


def test_create_project_scaffolds_expected_layout(tmp_path, yuxu_home):
    project = tmp_path / "myproj"
    p = create_project(project)
    assert p == project.resolve()
    assert (p / "yuxu.json").exists()
    for d in ("agents", "skills", "_system", "config",
              "data/checkpoints", "data/logs", "data/memory", "data/sessions",
              ".yuxu"):
        assert (p / d).is_dir(), f"missing {d}"
    for f in (".gitignore", "config/rate_limits.yaml", "config/skills_enabled.yaml",
              ".yuxu/version", ".yuxu/manifest.json"):
        assert (p / f).exists(), f"missing {f}"


def test_create_project_extracts_all_bundled(tmp_path, yuxu_home):
    p = create_project(tmp_path / "proj")
    system = p / "_system"
    names = sorted(d.name for d in system.iterdir() if d.is_dir())
    for expected in ("checkpoint_store", "rate_limit_service", "llm_service",
                     "llm_driver", "project_supervisor", "recovery_agent",
                     "resource_guardian", "project_manager"):
        assert expected in names, f"{expected} not extracted"


def test_create_project_manifest(tmp_path, yuxu_home):
    p = create_project(tmp_path / "proj")
    manifest = json.loads((p / ".yuxu" / "manifest.json").read_text())
    names = {item["name"] for item in manifest["bundled"]}
    assert "checkpoint_store" in names
    assert all(item.get("md_sha12") for item in manifest["bundled"])
    kinds = {item["name"]: item["kind"] for item in manifest["bundled"]}
    assert kinds["checkpoint_store"] == "agent"
    assert kinds["classify_intent"] == "skill"


def test_create_project_refuses_existing_without_force(tmp_path, yuxu_home):
    p = tmp_path / "proj"
    create_project(p)
    with pytest.raises(FileExistsError):
        create_project(p)
    create_project(p, force=True)


def test_create_project_registers_in_home(tmp_path, yuxu_home):
    p1 = create_project(tmp_path / "p1")
    p2 = create_project(tmp_path / "p2")
    listed = list_projects()
    paths = [item["path"] for item in listed]
    assert str(p1) in paths
    assert str(p2) in paths


@pytest.mark.asyncio
async def test_create_project_execute_returns_ok(tmp_path, yuxu_home):
    r = await create_project_execute({"dir": str(tmp_path / "proj")}, ctx=None)
    assert r["ok"] is True
    assert Path(r["path"]).is_dir()


@pytest.mark.asyncio
async def test_create_project_execute_missing_dir(yuxu_home):
    r = await create_project_execute({}, ctx=None)
    assert r["ok"] is False
    assert "missing field" in r["error"]


@pytest.mark.asyncio
async def test_create_project_execute_already_exists(tmp_path, yuxu_home):
    create_project(tmp_path / "proj")
    r = await create_project_execute({"dir": str(tmp_path / "proj")}, ctx=None)
    assert r["ok"] is False
    assert "already exists" in r["error"]


# -- create_agent -----------------------------------------------


def test_create_agent_copies_template(tmp_path, yuxu_home):
    p = create_project(tmp_path / "proj")
    agent = create_agent(p, "my_bot")
    assert (agent / "AGENT.md").exists()
    assert (agent / "__init__.py").exists()
    init_src = (agent / "__init__.py").read_text(encoding="utf-8")
    assert 'NAME = "my_bot"' in init_src
    assert 'NAME = "my_agent"' not in init_src


def test_create_agent_rejects_duplicate(tmp_path, yuxu_home):
    p = create_project(tmp_path / "proj")
    create_agent(p, "dup")
    with pytest.raises(FileExistsError):
        create_agent(p, "dup")


def test_create_agent_rejects_missing_project(tmp_path, yuxu_home):
    with pytest.raises(FileNotFoundError):
        create_agent(tmp_path / "nope", "x")


def test_create_agent_unknown_template_fails(tmp_path, yuxu_home):
    p = create_project(tmp_path / "proj")
    with pytest.raises(FileNotFoundError):
        create_agent(p, "x", template="nonexistent")


@pytest.mark.asyncio
async def test_create_agent_execute_ok(tmp_path, yuxu_home):
    p = create_project(tmp_path / "proj")
    r = await create_agent_execute(
        {"project_dir": str(p), "name": "bob"}, ctx=None,
    )
    assert r["ok"] is True
    assert Path(r["path"]).name == "bob"


@pytest.mark.asyncio
async def test_create_agent_execute_already_exists(tmp_path, yuxu_home):
    p = create_project(tmp_path / "proj")
    create_agent(p, "dup")
    r = await create_agent_execute(
        {"project_dir": str(p), "name": "dup"}, ctx=None,
    )
    assert r["ok"] is False
    assert "already exists" in r["error"]


# -- list_projects ----------------------------------------------


def test_list_projects_empty(yuxu_home):
    assert list_projects() == []


def test_list_projects_shows_version_and_name(tmp_path, yuxu_home):
    p = create_project(tmp_path / "alpha")
    listed = list_projects()
    found = [item for item in listed if item["path"] == str(p)]
    assert len(found) == 1
    assert found[0]["name"] == "alpha"
    assert found[0]["exists"] is True
    assert found[0].get("yuxu_version")


def test_list_projects_marks_missing(tmp_path, yuxu_home):
    yuxu_home.mkdir()
    (yuxu_home / "projects.yaml").write_text(
        yaml.safe_dump({"projects": ["/nonexistent/path"]})
    )
    listed = list_projects()
    assert listed[0]["path"] == "/nonexistent/path"
    assert listed[0]["exists"] is False


@pytest.mark.asyncio
async def test_list_projects_execute(tmp_path, yuxu_home):
    create_project(tmp_path / "p1")
    r = await list_projects_execute({}, ctx=None)
    assert r["ok"] is True
    assert any(item["name"] == "p1" for item in r["projects"])


# -- list_agents ------------------------------------------------


def test_list_agents_shows_bundled_and_user(tmp_path, yuxu_home):
    p = create_project(tmp_path / "proj")
    create_agent(p, "user_bot")
    agents = list_agents(p)
    bundled_names = {a["name"] for a in agents if a["source"] == "bundled"}
    user_names = {a["name"] for a in agents if a["source"] == "user"}
    assert "llm_service" in bundled_names
    assert "user_bot" in user_names


def test_list_agents_rejects_missing_project(tmp_path, yuxu_home):
    with pytest.raises(FileNotFoundError):
        list_agents(tmp_path / "nope")


@pytest.mark.asyncio
async def test_list_agents_execute_missing_field(yuxu_home):
    r = await list_agents_execute({}, ctx=None)
    assert r["ok"] is False
    assert "missing field" in r["error"]


@pytest.mark.asyncio
async def test_list_agents_execute_ok(tmp_path, yuxu_home):
    p = create_project(tmp_path / "proj")
    r = await list_agents_execute({"project_dir": str(p)}, ctx=None)
    assert r["ok"] is True
    assert any(a["name"] == "llm_service" for a in r["agents"])


# -- loader discovery -------------------------------------------


@pytest.mark.asyncio
async def test_loader_discovers_all_four_bundled_skills():
    """The four scaffolding skills must show up under kind=skill when Loader
    scans the installed bundled root."""
    import yuxu as _y
    from yuxu.core.bus import Bus
    from yuxu.core.loader import Loader

    bundled_root = Path(_y.__file__).parent / "bundled"
    bus = Bus()
    loader = Loader(bus, [str(bundled_root)])
    await loader.scan()
    skills = {s.name for s in loader.filter(kind="skill")}
    for expected in ("create_project", "create_agent", "list_projects", "list_agents"):
        assert expected in skills, f"{expected} not in skills: {sorted(skills)}"


@pytest.mark.asyncio
async def test_loader_skips_underscored_helpers():
    """`_shared.py` at the bundled root must not be picked up as a skill."""
    import yuxu as _y
    from yuxu.core.bus import Bus
    from yuxu.core.loader import Loader

    bundled_root = Path(_y.__file__).parent / "bundled"
    bus = Bus()
    loader = Loader(bus, [str(bundled_root)])
    await loader.scan()
    names = set(loader.specs)
    assert "_shared" not in names
    assert "_shared.py" not in names
