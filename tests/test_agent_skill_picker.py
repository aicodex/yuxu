"""SkillPicker (bus-facing agent) integration tests.

Registry-level tests are in test_agent_skill_picker_registry.py.
This file exercises the bus ops.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from yuxu.bundled.skill_picker.handler import SkillPicker
from yuxu.bundled.skill_picker.registry import SkillScope
from yuxu.core.bus import Bus
from yuxu.core.loader import Loader

pytestmark = pytest.mark.asyncio


def _write_skill(root: Path, name: str, fm: dict, body: str = "") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\n{yaml.safe_dump(fm, sort_keys=False).strip()}\n---\n{body}\n"
    )
    return d


def _write_agent(root: Path, name: str, fm: dict, init_src: str = "") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "AGENT.md").write_text(
        f"---\n{yaml.safe_dump(fm, sort_keys=False).strip()}\n---\n"
    )
    (d / "__init__.py").write_text(textwrap.dedent(init_src or f"""
        async def start(bus, loader):
            await bus.ready({name!r})
    """))
    return d


async def test_picker_autodiscovers_agent_skills(tmp_path):
    # Set up a fake agent with a private skill folder
    agents_dir = tmp_path / "agents"
    agent_path = _write_agent(agents_dir, "alice", {"run_mode": "one_shot"})
    _write_skill(agent_path / "skills", "greet", {"description": "say hi"})

    bus = Bus()
    loader = Loader(bus, dirs=[str(agents_dir)])
    await loader.scan()
    picker = SkillPicker(
        bus, loader,
        global_root=tmp_path / "no_global",  # empty
        global_enable_file=tmp_path / "no_enable.yaml",
    )
    # alice's skills/greet auto-registered
    assert ("agent", "alice", "greet") in picker.registry.skills


async def test_picker_catalog_honors_enable_file(tmp_path):
    agents_dir = tmp_path / "agents"
    agent_path = _write_agent(agents_dir, "alice", {"run_mode": "one_shot"})
    _write_skill(agent_path / "skills", "greet", {"description": "say hi"})
    _write_skill(agent_path / "skills", "secret", {"description": "hidden"})
    (agent_path / "skills_enabled.yaml").write_text(yaml.safe_dump({"enabled": ["greet"]}))

    bus = Bus()
    loader = Loader(bus, dirs=[str(agents_dir)])
    await loader.scan()
    picker = SkillPicker(
        bus, loader,
        global_root=tmp_path / "no_global",
        global_enable_file=tmp_path / "no_enable.yaml",
    )
    class _M: payload = {"op": "catalog", "for_agent": "alice"}
    r = await picker.handle(_M())
    names = [s["name"] for s in r["skills"]]
    assert names == ["greet"]


async def test_picker_load_returns_body(tmp_path):
    agents_dir = tmp_path / "agents"
    agent_path = _write_agent(agents_dir, "alice", {"run_mode": "one_shot"})
    _write_skill(agent_path / "skills", "greet",
                 {"description": "say hi"},
                 body="## Use me for greeting")
    (agent_path / "skills_enabled.yaml").write_text(yaml.safe_dump({"enabled": ["greet"]}))

    bus = Bus()
    loader = Loader(bus, dirs=[str(agents_dir)])
    await loader.scan()
    picker = SkillPicker(
        bus, loader,
        global_root=tmp_path / "no_global",
        global_enable_file=tmp_path / "no_enable.yaml",
    )
    class _M: payload = {"op": "load", "name": "greet", "for_agent": "alice"}
    r = await picker.handle(_M())
    assert r["ok"] is True
    assert "Use me for greeting" in r["body"]


async def test_picker_enable_via_bus(tmp_path):
    # Install a global skill, disabled
    global_root = tmp_path / "global_skills"
    enable_file = tmp_path / "global_enabled.yaml"
    _write_skill(global_root, "bash", {"description": "shell"})

    bus = Bus()
    loader = Loader(bus, dirs=[])
    picker = SkillPicker(
        bus, loader,
        global_root=global_root,
        global_enable_file=enable_file,
    )
    class _E:
        payload = {"op": "enable", "name": "bash", "scope": "global"}
    r = await picker.handle(_E())
    assert r["ok"] is True
    assert enable_file.exists()
    assert yaml.safe_load(enable_file.read_text())["enabled"] == ["bash"]

    # Now catalog shows it
    class _C: payload = {"op": "catalog"}
    r = await picker.handle(_C())
    assert [s["name"] for s in r["skills"]] == ["bash"]


async def test_picker_disable_via_bus(tmp_path):
    global_root = tmp_path / "g"
    enable_file = tmp_path / "e.yaml"
    _write_skill(global_root, "bash", {"description": "shell"})
    enable_file.write_text(yaml.safe_dump({"enabled": ["bash"]}))

    bus = Bus()
    loader = Loader(bus, dirs=[])
    picker = SkillPicker(bus, loader, global_root=global_root, global_enable_file=enable_file)

    class _D:
        payload = {"op": "disable", "name": "bash", "scope": "global"}
    r = await picker.handle(_D())
    assert r["ok"] is True
    assert yaml.safe_load(enable_file.read_text())["enabled"] == []


async def test_picker_rescan_with_extra_projects(tmp_path):
    global_root = tmp_path / "g"
    enable_file = tmp_path / "e.yaml"
    global_root.mkdir()

    # project skill
    proj_dir = tmp_path / "proj_trading"
    _write_skill(proj_dir / "skills", "buy", {"description": "buy order"})
    (proj_dir / "skills_enabled.yaml").write_text(yaml.safe_dump({"enabled": ["buy"]}))

    bus = Bus()
    loader = Loader(bus, dirs=[])
    picker = SkillPicker(bus, loader, global_root=global_root, global_enable_file=enable_file)

    class _R:
        payload = {"op": "rescan", "extra_projects": [[str(proj_dir), "trading"]]}
    r = await picker.handle(_R())
    assert r["ok"] is True
    assert r["count"] >= 1

    class _C:
        payload = {"op": "catalog", "for_project": "trading"}
    cat = await picker.handle(_C())
    assert [s["name"] for s in cat["skills"]] == ["buy"]


async def test_picker_unknown_op(tmp_path):
    bus = Bus()
    loader = Loader(bus, dirs=[])
    picker = SkillPicker(bus, loader, global_root=tmp_path / "g", global_enable_file=tmp_path / "e")
    class _M: payload = {"op": "nope"}
    r = await picker.handle(_M())
    assert r["ok"] is False


async def test_picker_load_missing_name(tmp_path):
    bus = Bus()
    loader = Loader(bus, dirs=[])
    picker = SkillPicker(bus, loader, global_root=tmp_path / "g", global_enable_file=tmp_path / "e")
    class _M: payload = {"op": "load"}
    r = await picker.handle(_M())
    assert r["ok"] is False
    assert "name required" in r["error"]


async def test_picker_load_hidden_from_stranger(tmp_path):
    agents_dir = tmp_path / "agents"
    apath = _write_agent(agents_dir, "alice", {"run_mode": "one_shot"})
    _write_skill(apath / "skills", "secret", {"description": "s"})
    (apath / "skills_enabled.yaml").write_text(yaml.safe_dump({"enabled": ["secret"]}))

    bus = Bus()
    loader = Loader(bus, dirs=[str(agents_dir)])
    await loader.scan()
    picker = SkillPicker(
        bus, loader,
        global_root=tmp_path / "g",
        global_enable_file=tmp_path / "e.yaml",
    )
    class _M: payload = {"op": "load", "name": "secret", "for_agent": "bob"}
    r = await picker.handle(_M())
    assert r["ok"] is False  # invisible to bob


# -- full boot integration --------------------------------------

async def test_picker_starts_via_loader(bundled_dir):
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    await loader.ensure_running("skill_picker")
    assert bus.query_status("skill_picker") == "ready"

    r = await bus.request("skill_picker", {"op": "catalog"}, timeout=2.0)
    assert r["ok"] is True
    # No skills installed yet → empty catalog
    assert r["skills"] == []
