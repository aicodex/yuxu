"""memory skill — 2-layer progressive disclosure tests.

Seeds a memory_root with real-looking entries (frontmatter + body), then
verifies:
- `list` returns compact index from frontmatter only (no bodies)
- `get` returns full body + frontmatter for one entry
- Index payload size is much smaller than the sum of bodies (the actual
  point of progressive disclosure)
- `_drafts/` and `_improvement_log.md` are skipped from the index
- `types` filter narrows results
- `get` rejects paths that escape memory_root
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuxu.bundled.memory.handler import execute

pytestmark = pytest.mark.asyncio


def _write_entry(root: Path, path: str, *, name: str, description: str,
                  type_: str, body_lines: int = 20) -> int:
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(
        f"line {i}: lorem ipsum body content padding for a realistic entry"
        for i in range(body_lines)
    )
    text = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"type: {type_}\n"
        "---\n\n"
        f"# {name}\n\n{body}\n"
    )
    p.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def _ctx(memory_dir: Path):
    # memory skill walks up from ctx.agent_dir for yuxu.json; pass a child of
    # the project to exercise that resolution path.
    project = memory_dir.parent.parent  # data/memory → project root
    return SimpleNamespace(agent_dir=str(project / "agents" / "fake"))


async def test_list_returns_index_only_no_bodies(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)

    total_body_bytes = 0
    total_body_bytes += _write_entry(mem, "feedback_terse.md",
                                       name="Terse", description="Prefer short replies",
                                       type_="feedback", body_lines=40)
    total_body_bytes += _write_entry(mem, "project_goal.md",
                                       name="Goal", description="Ship the memory skill",
                                       type_="project", body_lines=30)
    total_body_bytes += _write_entry(mem, "reference_docs.md",
                                       name="Docs", description="Where the guides live",
                                       type_="reference", body_lines=60)

    ctx = _ctx(mem)
    result = await execute({"op": "list"}, ctx)
    assert result["ok"] is True
    assert result["memory_root"] == str(mem)
    entries = result["entries"]
    assert len(entries) == 3

    names = {e["name"] for e in entries}
    assert names == {"Terse", "Goal", "Docs"}

    # Index rows carry only summary fields — no body leak.
    for e in entries:
        assert set(e.keys()) == {"path", "name", "description", "type", "bytes"}
        assert "lorem ipsum" not in json.dumps(e)

    # Progressive disclosure ROI: the serialized index is much smaller than
    # the sum of file bytes. Use a generous ratio so padding / filesystem
    # variance doesn't make the test flaky.
    index_payload = json.dumps(result)
    assert len(index_payload) * 3 < total_body_bytes, (
        f"index {len(index_payload)}B should be << total bodies "
        f"{total_body_bytes}B for meaningful progressive disclosure"
    )


async def test_list_skips_drafts_and_improvement_log(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)

    _write_entry(mem, "feedback_real.md", name="Real",
                  description="A legit entry", type_="feedback")
    # curator staging area — must be hidden from the index
    _write_entry(mem, "_drafts/curator_abc_001.md", name="Draft",
                  description="Draft entry", type_="feedback")
    # append-only improvement log — not an indexable entry
    (mem / "_improvement_log.md").write_text(
        "- 2026-04-24 improvement\n- 2026-04-24 another\n",
        encoding="utf-8",
    )
    # dotfile — hidden
    (mem / ".hidden.md").write_text(
        "---\nname: Hidden\ndescription: no\ntype: feedback\n---\n\nnope\n",
        encoding="utf-8",
    )

    result = await execute({"op": "list"}, _ctx(mem))
    assert result["ok"] is True
    names = {e["name"] for e in result["entries"]}
    assert names == {"Real"}


async def test_list_filter_by_type(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)

    _write_entry(mem, "a.md", name="A", description="a", type_="feedback")
    _write_entry(mem, "b.md", name="B", description="b", type_="project")
    _write_entry(mem, "c.md", name="C", description="c", type_="reference")

    r = await execute({"op": "list", "types": ["feedback", "project"]},
                        _ctx(mem))
    assert r["ok"] is True
    names = {e["name"] for e in r["entries"]}
    assert names == {"A", "B"}


async def test_get_returns_full_body_and_frontmatter(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry(mem, "feedback_terse.md", name="Terse",
                  description="Prefer short replies", type_="feedback",
                  body_lines=10)

    r = await execute({"op": "get", "path": "feedback_terse.md"}, _ctx(mem))
    assert r["ok"] is True
    assert r["path"] == "feedback_terse.md"
    assert r["frontmatter"]["name"] == "Terse"
    assert r["frontmatter"]["type"] == "feedback"
    assert "lorem ipsum" in r["body"]
    assert "# Terse" in r["body"]


async def test_get_rejects_path_escape(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    outside = project / "secret.md"
    outside.write_text("---\nname: s\ndescription: s\ntype: x\n---\nsecret\n",
                         encoding="utf-8")

    r = await execute({"op": "get", "path": "../secret.md"}, _ctx(mem))
    assert r["ok"] is False
    assert "escapes memory_root" in r["error"]


async def test_get_missing_file(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)

    r = await execute({"op": "get", "path": "nope.md"}, _ctx(mem))
    assert r["ok"] is False
    assert "not a file" in r["error"]


async def test_unknown_op(tmp_path):
    r = await execute({"op": "search"}, _ctx(tmp_path / "data" / "memory"))
    assert r["ok"] is False
    assert "unknown op" in r["error"]


async def test_via_bus_request_roundtrip(tmp_path, monkeypatch, bundled_dir):
    """Install the skill via loader and invoke it over the bus — verifying
    it's picked up as a proper bundled skill (not just directly callable)."""
    from yuxu.core.main import boot

    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry(mem, "feedback_terse.md", name="Terse",
                  description="Prefer short replies", type_="feedback")
    _write_entry(mem, "project_goal.md", name="Goal",
                  description="ship it", type_="project")

    monkeypatch.chdir(project)  # skill falls back to cwd if agent_dir walk misses

    bus, loader = await boot(
        dirs=[bundled_dir],
        autostart_persistent=False,  # skip persistents — skill register is synchronous
    )
    # Force-register the skill (autostart_persistent=False skipped the skills
    # loop that would normally register passive handlers).
    await loader.ensure_running("memory")

    list_r = await bus.request("memory", {"op": "list"}, timeout=2.0)
    assert list_r["ok"] is True
    names = {e["name"] for e in list_r["entries"]}
    assert names == {"Terse", "Goal"}

    get_r = await bus.request(
        "memory", {"op": "get", "path": "feedback_terse.md"}, timeout=2.0,
    )
    assert get_r["ok"] is True
    assert get_r["frontmatter"]["name"] == "Terse"
