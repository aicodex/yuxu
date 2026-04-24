"""skill_index — catalog of yuxu skills + agents, progressive disclosure."""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuxu.bundled.skill_index.handler import (
    DEFAULT_CHAR_BUDGET,
    DIRECTIVE_TEMPLATE,
    NAME,
    _discover_from_fs,
    _filter_entries,
    _render_compact,
    _render_full,
    _render_truncated,
    _render_with_budget,
    _xml_escape,
    build_directive,
    execute,
)
from yuxu.core.bus import Bus

pytestmark = pytest.mark.asyncio


# -- helpers -------------------------------------------------


def _make_skill(tmp_path: Path, dirname: str, *,
                   name: str = None,
                   description: str = "a test skill",
                   scope: str = None) -> Path:
    d = tmp_path / dirname
    d.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name or dirname}",
              f"description: {description}", "version: 0.1.0"]
    if scope:
        lines.append(f"scope: {scope}")
    lines.append("---")
    lines.append(f"# {name or dirname}")
    (d / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
    return d


def _make_agent(tmp_path: Path, dirname: str, *,
                   name: str = None,
                   description: str = "a test agent",
                   scope: str = None) -> Path:
    d = tmp_path / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "__init__.py").write_text("", encoding="utf-8")
    lines = ["---", f"name: {name or dirname}",
              f"description: {description}", "driver: python"]
    if scope:
        lines.append(f"scope: {scope}")
    lines.append("---")
    lines.append(f"# {name or dirname}")
    (d / "AGENT.md").write_text("\n".join(lines), encoding="utf-8")
    return d


def _project_ctx(project_root: Path) -> SimpleNamespace:
    """Fake ctx inside a project skeleton so walk-up finds yuxu.json."""
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "yuxu.json").write_text('{"name":"fake"}',
                                              encoding="utf-8")
    agent_dir = project_root / "agents" / "x"
    agent_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(bus=Bus(), agent_dir=agent_dir,
                            name="x", loader=None)


SAMPLE_ENTRIES = [
    {"name": "alpha", "kind": "skill", "description": "first skill",
     "location": "/x/alpha/SKILL.md", "scope": "user", "source": "bundled"},
    {"name": "beta", "kind": "agent", "description": "beta agent",
     "location": "/x/beta/AGENT.md", "scope": "system", "source": "bundled"},
    {"name": "gamma", "kind": "skill", "description": "third skill",
     "location": "/x/gamma/SKILL.md", "scope": "user", "source": "project"},
]


# -- primitives ----------------------------------------------


async def test_xml_escape_basics():
    assert _xml_escape("foo") == "foo"
    assert _xml_escape("a<b>c") == "a&lt;b&gt;c"
    assert _xml_escape("x&y") == "x&amp;y"
    assert _xml_escape('"quoted"') == "&quot;quoted&quot;"


async def test_render_full_includes_all_fields():
    xml = _render_full(SAMPLE_ENTRIES[:1])
    assert "<available_skills>" in xml and "</available_skills>" in xml
    assert "<name>alpha</name>" in xml
    assert "<kind>skill</kind>" in xml
    assert "<description>first skill</description>" in xml
    assert "<location>/x/alpha/SKILL.md</location>" in xml


async def test_render_compact_drops_descriptions():
    xml = _render_compact(SAMPLE_ENTRIES)
    for e in SAMPLE_ENTRIES:
        assert f"<name>{e['name']}</name>" in xml
        assert f"<kind>{e['kind']}</kind>" in xml
        assert f"<location>{e['location']}</location>" in xml
    # no descriptions
    assert "first skill" not in xml


async def test_render_with_budget_full_when_fits():
    xml, compact, omitted = _render_with_budget(SAMPLE_ENTRIES, 10_000)
    assert compact is False
    assert omitted == 0
    assert "first skill" in xml  # descriptions kept


async def test_render_with_budget_falls_back_to_compact():
    # Budget large enough for compact (~260 chars) but not full (~600+)
    xml, compact, omitted = _render_with_budget(SAMPLE_ENTRIES, 400)
    assert compact is True
    assert omitted == 0
    assert "first skill" not in xml  # descriptions dropped


async def test_render_with_budget_truncates_when_compact_still_too_big():
    many = [
        {"name": f"skill_{i:03d}", "kind": "skill",
         "description": "x",
         "location": f"/x/skill_{i:03d}/SKILL.md",
         "scope": None, "source": "bundled"}
        for i in range(200)
    ]
    xml, compact, omitted = _render_with_budget(many, 2000)
    assert compact is True
    assert omitted > 0
    assert "entries omitted" in xml


async def test_render_truncated_binary_search_stable():
    xml, omitted = _render_truncated(SAMPLE_ENTRIES, 200, compact=True)
    assert omitted >= 0
    assert "entries omitted" in xml or omitted == 0


# -- filter --------------------------------------------------


async def test_filter_by_kind():
    out = _filter_entries(SAMPLE_ENTRIES, "skill", None, include_self=True)
    assert [e["name"] for e in out] == ["alpha", "gamma"]


async def test_filter_by_scope():
    out = _filter_entries(SAMPLE_ENTRIES, None, "system", include_self=True)
    assert [e["name"] for e in out] == ["beta"]


async def test_filter_excludes_self_when_asked():
    with_self = SAMPLE_ENTRIES + [
        {"name": NAME, "kind": "skill", "description": "self",
         "location": "/x/skill_index/SKILL.md",
         "scope": "user", "source": "bundled"}
    ]
    out = _filter_entries(with_self, None, None, include_self=False)
    assert NAME not in [e["name"] for e in out]


# -- discover (filesystem) -----------------------------------


async def test_discover_from_fs_finds_project_skills(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    _make_skill(project / "skills", "foo_skill",
                description="a foo skill")
    _make_agent(project / "agents", "bar_agent",
                description="a bar agent")
    ctx = _project_ctx(project)
    entries = _discover_from_fs(ctx)
    names = {e["name"] for e in entries}
    assert "foo_skill" in names
    assert "bar_agent" in names
    # kind classification
    by_name = {e["name"]: e for e in entries}
    assert by_name["foo_skill"]["kind"] == "skill"
    assert by_name["bar_agent"]["kind"] == "agent"


async def test_discover_skips_dirs_without_required_frontmatter(
    tmp_path: Path,
):
    project = tmp_path / "proj"
    project.mkdir()
    d = project / "skills" / "bad"
    d.mkdir(parents=True)
    # Skip — no frontmatter
    (d / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
    ctx = _project_ctx(project)
    entries = _discover_from_fs(ctx)
    assert all(e["name"] != "bad" for e in entries)


async def test_discover_from_fs_finds_bundled():
    """Real bundled discovery — sanity check we see the ones we shipped."""
    ctx = SimpleNamespace(agent_dir=Path("/tmp"))
    entries = _discover_from_fs(ctx)
    names = {e["name"] for e in entries}
    # Known bundled entries
    for expected in ("memory", "admission_gate", "context_compressor",
                      "llm_judge", "session_compressor", "memory_curator"):
        assert expected in names


# -- op end-to-end -------------------------------------------


async def test_list_op_returns_xml_block(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    _make_skill(project / "skills", "alpha_skill",
                description="alpha description here")
    _make_agent(project / "agents", "beta_agent",
                description="beta description here")
    ctx = _project_ctx(project)

    r = await execute({"op": "list", "scope": None}, ctx)
    assert r["ok"] is True
    assert "<available_skills>" in r["xml_block"]
    names = {e["name"] for e in r["entries"]}
    assert "alpha_skill" in names and "beta_agent" in names
    # rendered_chars matches
    assert r["rendered_chars"] == len(r["xml_block"])


async def test_list_op_kind_filter(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    _make_skill(project / "skills", "just_skill")
    _make_agent(project / "agents", "just_agent")
    ctx = _project_ctx(project)

    only_agents = await execute({"op": "list", "kind": "agent"}, ctx)
    only_skills = await execute({"op": "list", "kind": "skill"}, ctx)
    assert any(e["name"] == "just_agent" for e in only_agents["entries"])
    assert not any(e["name"] == "just_skill" for e in only_agents["entries"])
    assert any(e["name"] == "just_skill" for e in only_skills["entries"])


async def test_list_op_char_budget_triggers_compact(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    for i in range(30):
        _make_skill(project / "skills", f"lots_{i:02d}",
                    description=("x" * 300))
    ctx = _project_ctx(project)

    r = await execute({"op": "list",
                        "char_budget": 2000,
                        "kind": "skill"}, ctx)
    assert r["ok"] is True
    assert r["compact_used"] is True


async def test_list_op_excludes_self_when_asked(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    _make_skill(project / "skills", NAME, description="test")
    ctx = _project_ctx(project)
    r1 = await execute({"op": "list", "include_self": True,
                         "scope": None}, ctx)
    r2 = await execute({"op": "list", "include_self": False,
                         "scope": None}, ctx)
    n1 = [e["name"] for e in r1["entries"]]
    n2 = [e["name"] for e in r2["entries"]]
    assert NAME in n1
    assert NAME not in n2


async def test_stats_op_returns_counts(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    _make_skill(project / "skills", "s1", scope="user")
    _make_skill(project / "skills", "s2", scope="user")
    _make_agent(project / "agents", "a1", scope="system")
    ctx = _project_ctx(project)

    r = await execute({"op": "stats"}, ctx)
    assert r["ok"] is True
    # kind counts for project-level entries
    proj_entries_kinds = {
        e["kind"] for e in _discover_from_fs(ctx) if e["source"] == "project"
    }
    assert proj_entries_kinds == {"skill", "agent"}


async def test_read_op_returns_body(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    _make_skill(project / "skills", "readme_target",
                description="a readable skill")
    ctx = _project_ctx(project)

    r = await execute({"op": "read", "name": "readme_target"}, ctx)
    assert r["ok"] is True
    assert r["name"] == "readme_target"
    assert r["kind"] == "skill"
    assert r["frontmatter"]["description"] == "a readable skill"
    assert "# readme_target" in r["body"]


async def test_read_op_missing_name_errors(tmp_path: Path):
    ctx = _project_ctx(tmp_path / "proj")
    r = await execute({"op": "read"}, ctx)
    assert r["ok"] is False


async def test_read_op_unknown_name_errors(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    ctx = _project_ctx(project)
    r = await execute({"op": "read", "name": "nonexistent"}, ctx)
    assert r["ok"] is False
    assert "not found" in r["error"]


async def test_unknown_op_errors(tmp_path: Path):
    ctx = _project_ctx(tmp_path / "proj")
    r = await execute({"op": "frobnicate"}, ctx)
    assert r["ok"] is False


# -- directive helper -----------------------------------------


async def test_build_directive_contains_scan_instruction():
    block = "<available_skills>\n</available_skills>"
    out = build_directive(block)
    assert "## Available Skills (mandatory)" in out
    assert "Before replying: scan" in out
    assert "never invoke more than one skill up front" in out
    assert block in out


async def test_directive_template_points_to_invoke_skill_tool():
    assert "invoke_skill" in DIRECTIVE_TEMPLATE
    assert '"name"' in DIRECTIVE_TEMPLATE
    # No longer tells the LLM to call skill_index directly.
    assert '{"op": "read"' not in DIRECTIVE_TEMPLATE


# -- loader discovery path -----------------------------------


async def test_loader_discovers_skill_index():
    from yuxu.core.loader import Loader
    import yuxu as _y

    bundled_dir = str(Path(_y.__file__).parent / "bundled")
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    assert NAME in loader.specs


async def test_list_uses_loader_specs_when_available():
    """When ctx.loader.specs is populated, _discover_from_loader should
    short-circuit the filesystem walk."""
    from yuxu.core.loader import Loader
    import yuxu as _y

    bundled_dir = str(Path(_y.__file__).parent / "bundled")
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()

    ctx = SimpleNamespace(bus=bus, agent_dir=Path(bundled_dir),
                            name="test", loader=loader)
    r = await execute({"op": "list"}, ctx)
    assert r["ok"] is True
    names = {e["name"] for e in r["entries"]}
    # A few things we know must be there
    for expected in ("memory", "admission_gate", "llm_judge",
                      "context_compressor"):
        assert expected in names
