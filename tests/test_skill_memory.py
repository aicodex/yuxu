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
                  type_: str, body_lines: int = 20,
                  scope: str | None = None,
                  evidence_level: str | None = None,
                  status: str | None = None,
                  tags: list[str] | None = None,
                  probation: bool = False) -> int:
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(
        f"line {i}: lorem ipsum body content padding for a realistic entry"
        for i in range(body_lines)
    )
    fm_lines = [
        "---",
        f"name: {name}",
        f"description: {description}",
        f"type: {type_}",
    ]
    if scope is not None:
        fm_lines.append(f"scope: {scope}")
    if evidence_level is not None:
        fm_lines.append(f"evidence_level: {evidence_level}")
    if status is not None:
        fm_lines.append(f"status: {status}")
    if tags:
        fm_lines.append(f"tags: [{', '.join(tags)}]")
    if probation:
        fm_lines.append("probation: true")
    fm_lines.append("---")
    text = "\n".join(fm_lines) + f"\n\n# {name}\n\n{body}\n"
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
    expected_keys = {
        "path", "name", "description", "type", "bytes",
        "scope", "evidence_level", "status", "tags", "probation", "updated",
    }
    for e in entries:
        assert set(e.keys()) == expected_keys
        assert "lorem ipsum" not in json.dumps(e, default=str)

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
    r = await execute({"op": "nope"}, _ctx(tmp_path / "data" / "memory"))
    assert r["ok"] is False
    assert "unknown op" in r["error"]


# ---------- stats (L0) ----------


async def test_stats_aggregates_by_dimensions(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)

    _write_entry(mem, "a.md", name="A", description="a",
                  type_="feedback", scope="global",
                  evidence_level="consensus", status="current",
                  tags=["architectural", "mandatory"])
    _write_entry(mem, "b.md", name="B", description="b",
                  type_="feedback", scope="project",
                  evidence_level="observed", status="current")
    _write_entry(mem, "c.md", name="C", description="c",
                  type_="project", scope="global",
                  evidence_level="consensus", status="archived")
    _write_entry(mem, "d.md", name="D", description="d",
                  type_="reference", scope="project",
                  evidence_level="speculative", status="current",
                  probation=True)

    r = await execute({"op": "stats"}, _ctx(mem))
    assert r["ok"] is True
    assert r["total"] == 4
    assert r["by_type"] == {"feedback": 2, "project": 1, "reference": 1}
    assert r["by_scope"] == {"global": 2, "project": 2}
    assert r["by_status"] == {"current": 3, "archived": 1}
    assert r["by_evidence_level"] == {
        "consensus": 2, "observed": 1, "speculative": 1,
    }
    assert r["probation_count"] == 1
    assert r["mandatory_count"] == 1


async def test_stats_empty_root(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    r = await execute({"op": "stats"}, _ctx(mem))
    assert r["ok"] is True
    assert r["total"] == 0
    assert r["by_type"] == {}


# ---------- search (cross-cut) ----------


async def test_search_matches_name_and_description(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry(mem, "a.md", name="Kernel Invariants",
                  description="reliability over simplicity",
                  type_="feedback", evidence_level="consensus")
    _write_entry(mem, "b.md", name="Session Archive",
                  description="session JSONL pattern",
                  type_="reference", evidence_level="consensus")
    _write_entry(mem, "c.md", name="Random",
                  description="nothing relevant here",
                  type_="feedback", evidence_level="consensus")

    r = await execute({"op": "search", "query": "kernel"}, _ctx(mem))
    assert r["ok"] is True
    names = [e["name"] for e in r["entries"]]
    assert names == ["Kernel Invariants"]


async def test_search_ranks_name_hits_above_description(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    # Both contain "memory" — but only first has it in the NAME
    _write_entry(mem, "a.md", name="Memory Designs",
                  description="overview of systems",
                  type_="reference", evidence_level="consensus")
    _write_entry(mem, "b.md", name="Terse replies",
                  description="notes about memory usage",
                  type_="feedback", evidence_level="consensus")

    r = await execute({"op": "search", "query": "memory", "limit": 5},
                        _ctx(mem))
    assert r["ok"] is True
    assert [e["name"] for e in r["entries"]] == [
        "Memory Designs", "Terse replies",
    ]


async def test_search_honors_limit(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    for i in range(5):
        _write_entry(mem, f"e{i}.md", name=f"Test entry {i}",
                      description="test test test",
                      type_="feedback", evidence_level="consensus")
    r = await execute(
        {"op": "search", "query": "test", "limit": 2}, _ctx(mem),
    )
    assert r["ok"] is True
    assert len(r["entries"]) == 2


async def test_search_respects_mode_filter(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry(mem, "a.md", name="Exploration pattern",
                  description="observed lesson",
                  type_="feedback", evidence_level="observed",
                  status="current")
    _write_entry(mem, "b.md", name="Exploration hypothesis",
                  description="speculative idea",
                  type_="feedback", evidence_level="speculative",
                  status="current")

    # execute mode excludes speculative
    r = await execute({"op": "search", "query": "exploration",
                       "mode": "execute"}, _ctx(mem))
    assert [e["name"] for e in r["entries"]] == ["Exploration pattern"]

    # reflect mode includes both
    r = await execute({"op": "search", "query": "exploration",
                       "mode": "reflect"}, _ctx(mem))
    assert {e["name"] for e in r["entries"]} == {
        "Exploration pattern", "Exploration hypothesis",
    }


# ---------- list with modes ----------


async def test_list_execute_mode_excludes_speculative_archived_probation(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry(mem, "validated.md", name="V", description="v",
                  type_="feedback", evidence_level="validated",
                  status="current")
    _write_entry(mem, "consensus.md", name="C", description="c",
                  type_="feedback", evidence_level="consensus",
                  status="current")
    _write_entry(mem, "observed.md", name="O", description="o",
                  type_="feedback", evidence_level="observed",
                  status="current")
    _write_entry(mem, "speculative.md", name="S", description="s",
                  type_="feedback", evidence_level="speculative",
                  status="current")
    _write_entry(mem, "archived.md", name="A", description="a",
                  type_="feedback", evidence_level="consensus",
                  status="archived")
    _write_entry(mem, "probation.md", name="P", description="p",
                  type_="feedback", evidence_level="consensus",
                  status="current", probation=True)

    # default mode = execute
    r = await execute({"op": "list"}, _ctx(mem))
    assert r["mode"] == "execute"
    names = {e["name"] for e in r["entries"]}
    # Keeps validated/consensus/observed at status=current, not in probation
    assert names == {"V", "C", "O"}


async def test_list_reflect_mode_includes_everything(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry(mem, "a.md", name="Validated", description="v",
                  type_="feedback", evidence_level="validated",
                  status="current")
    _write_entry(mem, "b.md", name="Archived", description="a",
                  type_="feedback", evidence_level="consensus",
                  status="archived")
    _write_entry(mem, "c.md", name="Probation", description="p",
                  type_="feedback", evidence_level="observed",
                  status="current", probation=True)

    r = await execute({"op": "list", "mode": "reflect"}, _ctx(mem))
    names = {e["name"] for e in r["entries"]}
    assert names == {"Validated", "Archived", "Probation"}


async def test_list_blank_and_explore_modes_return_only_mandatory(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry(mem, "safety.md", name="Safety", description="kernel",
                  type_="feedback", evidence_level="consensus",
                  tags=["architectural", "mandatory"])
    _write_entry(mem, "advisory.md", name="Advisory", description="not mandatory",
                  type_="feedback", evidence_level="consensus",
                  tags=["discipline"])

    for mode in ("blank", "explore"):
        r = await execute({"op": "list", "mode": mode}, _ctx(mem))
        names = {e["name"] for e in r["entries"]}
        assert names == {"Safety"}, f"mode={mode} returned {names}"


async def test_list_debug_mode_observed_archived(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry(mem, "a.md", name="ObservedArchived", description="d",
                  type_="feedback", evidence_level="observed",
                  status="archived")
    _write_entry(mem, "b.md", name="ObservedCurrent", description="d",
                  type_="feedback", evidence_level="observed",
                  status="current")
    _write_entry(mem, "c.md", name="ConsensusArchived", description="d",
                  type_="feedback", evidence_level="consensus",
                  status="archived")

    r = await execute({"op": "list", "mode": "debug"}, _ctx(mem))
    names = {e["name"] for e in r["entries"]}
    # Only observed + archived combo hits
    assert names == {"ObservedArchived"}


async def test_user_filter_overrides_mode_default(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry(mem, "a.md", name="Validated", description="v",
                  type_="feedback", evidence_level="validated",
                  status="current")
    _write_entry(mem, "b.md", name="Speculative", description="s",
                  type_="feedback", evidence_level="speculative",
                  status="current")

    # execute mode normally excludes speculative — user override adds it back
    r = await execute({
        "op": "list", "mode": "execute",
        "evidence_level": ["validated", "speculative"],
    }, _ctx(mem))
    names = {e["name"] for e in r["entries"]}
    assert names == {"Validated", "Speculative"}


async def test_tags_filter_requires_all(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry(mem, "a.md", name="A", description="d",
                  type_="feedback", evidence_level="consensus",
                  tags=["architectural", "kernel"])
    _write_entry(mem, "b.md", name="B", description="d",
                  type_="feedback", evidence_level="consensus",
                  tags=["architectural"])
    _write_entry(mem, "c.md", name="C", description="d",
                  type_="feedback", evidence_level="consensus",
                  tags=["kernel"])

    r = await execute({
        "op": "list", "mode": "reflect",
        "tags": ["architectural", "kernel"],
    }, _ctx(mem))
    names = {e["name"] for e in r["entries"]}
    assert names == {"A"}


async def test_list_unknown_mode_errors(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    r = await execute({"op": "list", "mode": "bogus"}, _ctx(mem))
    assert r["ok"] is False
    assert "unknown mode" in r["error"]


async def test_summary_carries_new_fields(tmp_path):
    """Phase 1 canary: list output exposes the I6 extended fields."""
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry(mem, "entry.md", name="Canary",
                  description="I6 schema smoke test",
                  type_="feedback", scope="global",
                  evidence_level="consensus", status="current",
                  tags=["architectural", "mandatory"])

    r = await execute({"op": "list", "mode": "reflect"}, _ctx(mem))
    entry = r["entries"][0]
    assert entry["scope"] == "global"
    assert entry["evidence_level"] == "consensus"
    assert entry["status"] == "current"
    assert set(entry["tags"]) == {"architectural", "mandatory"}
    assert entry["probation"] is False


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


# ---------- Phase 4: memory.retrieved event emission ----------


async def test_retrieved_event_on_list(tmp_path):
    from yuxu.bundled.memory.handler import RETRIEVED_TOPIC
    from yuxu.core.bus import Bus
    bus = Bus()
    seen: list[dict] = []

    async def cap(event):
        seen.append(event.get("payload") or {})

    bus.subscribe(RETRIEVED_TOPIC, cap)

    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry(mem, "a.md", name="A", description="d",
                  type_="feedback", evidence_level="consensus")
    _write_entry(mem, "b.md", name="B", description="d",
                  type_="feedback", evidence_level="consensus")

    ctx = SimpleNamespace(bus=bus, agent_dir=str(project / "agents" / "fake"))
    r = await execute({"op": "list", "memory_root": str(mem)}, ctx)
    assert r["ok"] is True
    # Yield once so subscribers run
    import asyncio as _a
    await _a.sleep(0)
    assert seen, "memory.retrieved should fire on list"
    p = seen[0]
    assert p["op"] == "list"
    assert set(p["paths"]) == {"a.md", "b.md"}
    assert p["mode"] == "execute"


async def test_retrieved_event_on_get(tmp_path):
    from yuxu.bundled.memory.handler import RETRIEVED_TOPIC
    from yuxu.core.bus import Bus
    bus = Bus()
    seen: list[dict] = []
    bus.subscribe(RETRIEVED_TOPIC, lambda e: seen.append(e.get("payload") or {}))

    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry(mem, "a.md", name="A", description="d",
                  type_="feedback", evidence_level="consensus")

    ctx = SimpleNamespace(bus=bus, agent_dir=str(project / "agents" / "fake"))
    r = await execute({"op": "get", "path": "a.md",
                       "memory_root": str(mem)}, ctx)
    assert r["ok"] is True
    import asyncio as _a
    await _a.sleep(0)
    assert seen and seen[0]["op"] == "get"
    assert seen[0]["paths"] == ["a.md"]


async def test_retrieved_event_on_search(tmp_path):
    from yuxu.bundled.memory.handler import RETRIEVED_TOPIC
    from yuxu.core.bus import Bus
    bus = Bus()
    seen: list[dict] = []
    bus.subscribe(RETRIEVED_TOPIC, lambda e: seen.append(e.get("payload") or {}))

    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry(mem, "a.md", name="Kernel",
                  description="core invariants",
                  type_="feedback", evidence_level="consensus")

    ctx = SimpleNamespace(bus=bus, agent_dir=str(project / "agents" / "fake"))
    r = await execute({"op": "search", "query": "kernel",
                       "memory_root": str(mem)}, ctx)
    assert r["ok"] is True
    import asyncio as _a
    await _a.sleep(0)
    assert seen and seen[0]["op"] == "search"
    assert seen[0]["query"] == "kernel"
    assert seen[0]["paths"] == ["a.md"]


async def test_no_retrieved_event_when_empty_result(tmp_path):
    """When a query returns zero hits, no event fires (nothing was retrieved)."""
    from yuxu.bundled.memory.handler import RETRIEVED_TOPIC
    from yuxu.core.bus import Bus
    bus = Bus()
    seen: list[dict] = []
    bus.subscribe(RETRIEVED_TOPIC, lambda e: seen.append(e.get("payload") or {}))

    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry(mem, "a.md", name="A", description="d",
                  type_="feedback", evidence_level="consensus")
    ctx = SimpleNamespace(bus=bus, agent_dir=str(project / "agents" / "fake"))

    r = await execute({"op": "search", "query": "nonmatching term",
                       "memory_root": str(mem)}, ctx)
    assert r["ok"] is True
    assert r["entries"] == []
    import asyncio as _a
    await _a.sleep(0)
    assert not seen


async def test_retrieval_gracefully_handles_no_bus(tmp_path):
    """ctx without a bus → retrieval still works, just no event fired."""
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry(mem, "a.md", name="A", description="d",
                  type_="feedback", evidence_level="consensus")
    # No bus on ctx
    ctx = SimpleNamespace(agent_dir=str(project / "agents" / "fake"))
    r = await execute({"op": "list", "memory_root": str(mem)}, ctx)
    assert r["ok"] is True
    assert len(r["entries"]) == 1


# ----------------------------------------------------------------------------
# Body FTS  (search across body text, not just name + description)
# ----------------------------------------------------------------------------


def _write_entry_raw(root: Path, path: str, *, name: str, description: str,
                       type_: str, body: str,
                       evidence_level: str = "consensus",
                       status: str | None = "current") -> None:
    """Like _write_entry, but lets the test inject a custom body verbatim."""
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = [
        "---",
        f"name: {name}",
        f"description: {description}",
        f"type: {type_}",
        f"evidence_level: {evidence_level}",
    ]
    if status:
        fm.append(f"status: {status}")
    fm.append("---")
    p.write_text("\n".join(fm) + f"\n\n# {name}\n\n{body}\n", encoding="utf-8")


async def test_search_matches_body_when_name_and_desc_miss(tmp_path):
    """Body contains the keyword, name+desc don't — must still hit."""
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry_raw(
        mem, "a.md", name="Memory Body FTS", description="body search test",
        type_="reference",
        body="This note mentions invoke_skill fishing behavior seen in demo 1.",
    )
    _write_entry_raw(
        mem, "b.md", name="Unrelated", description="nothing relevant here",
        type_="reference",
        body="line 1\nline 2\nline 3",
    )
    r = await execute({"op": "search", "query": "fishing"}, _ctx(mem))
    assert r["ok"] is True
    names = [e["name"] for e in r["entries"]]
    assert names == ["Memory Body FTS"]


async def test_search_returns_body_snippet_around_hit(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry_raw(
        mem, "a.md", name="Plain", description="plain",
        type_="reference",
        body="alpha beta gamma INVOKE_SKILL delta epsilon zeta",
    )
    r = await execute({"op": "search", "query": "invoke_skill"}, _ctx(mem))
    assert r["ok"] is True
    entry = r["entries"][0]
    snip = entry.get("body_snippet") or ""
    assert "INVOKE_SKILL" in snip
    # snippet is a short single-line context, not the whole body
    assert "\n" not in snip
    assert len(snip) < 400


async def test_search_body_disabled_regresses_to_name_desc_only(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry_raw(
        mem, "a.md", name="BodyOnly", description="irrelevant",
        type_="reference",
        body="the secret token is frobnicate",
    )
    # With body FTS (default): hits
    r_on = await execute({"op": "search", "query": "frobnicate"}, _ctx(mem))
    assert [e["name"] for e in r_on["entries"]] == ["BodyOnly"]
    # Without body FTS: misses
    r_off = await execute(
        {"op": "search", "query": "frobnicate", "search_body": False},
        _ctx(mem),
    )
    assert r_off["entries"] == []


async def test_search_name_desc_still_outrank_body(tmp_path):
    """Ordering invariant: a name match still ranks above a body-only
    match (protects against over-weighting body hits). This is the
    regression test for the body-FTS addition — without capping, a body
    with many keyword repeats could outrank a clean name hit."""
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    # Body-only match: name / desc have no "kernel", body repeats it heavily.
    _write_entry_raw(
        mem, "a.md", name="Unrelated Talk", description="xyz",
        type_="reference",
        body="this body mentions kernel many many many many times kernel kernel kernel",
    )
    # Metadata match: name has "kernel", body doesn't.
    _write_entry_raw(
        mem, "b.md", name="All about Kernel Invariants", description="core rules",
        type_="feedback",
        body="no match text",
    )
    r = await execute({"op": "search", "query": "kernel"}, _ctx(mem))
    assert r["ok"] is True
    names = [e["name"] for e in r["entries"]]
    assert names[0] == "All about Kernel Invariants"
    assert names[1] == "Unrelated Talk"


# ----------------------------------------------------------------------------
# section-aware get  (**<label>:** paragraphs per memory_section_convention)
# ----------------------------------------------------------------------------


async def test_get_section_returns_why_paragraph(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    body = (
        "Lead sentence stating the rule.\n\n"
        "**Why:** the reason this matters, a few words.\n"
        "**How to apply:** when and where this kicks in.\n"
        "**Evidence:** observed in demo X on 2026-04-24.\n"
    )
    _write_entry_raw(mem, "a.md", name="With Sections",
                      description="has labeled sections",
                      type_="feedback", body=body)
    r = await execute({"op": "get", "path": "a.md", "section": "why"},
                       _ctx(mem))
    assert r["ok"] is True
    assert r["section"] == "why"
    assert r["section_body"] is not None
    assert "reason this matters" in r["section_body"]
    # Must NOT bleed into the next label's content
    assert "when and where" not in r["section_body"]


async def test_get_section_accepts_underscored_form(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    body = (
        "lead\n\n**Why:** w\n**How to apply:** h\n**Evidence:** e\n"
    )
    _write_entry_raw(mem, "a.md", name="X", description="d",
                      type_="feedback", body=body)
    r1 = await execute({"op": "get", "path": "a.md",
                         "section": "how_to_apply"}, _ctx(mem))
    r2 = await execute({"op": "get", "path": "a.md",
                         "section": "How To Apply"}, _ctx(mem))
    assert r1["section_body"] == "h"
    assert r2["section_body"] == "h"


async def test_get_section_not_found_lists_available(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    body = "lead\n\n**Why:** w\n**Evidence:** e\n"
    _write_entry_raw(mem, "a.md", name="X", description="d",
                      type_="feedback", body=body)
    r = await execute({"op": "get", "path": "a.md",
                       "section": "how_to_apply"}, _ctx(mem))
    # Still ok=True (caller chose the wrong section, not an error); but
    # section_body is None and available_sections lists what IS present.
    assert r["ok"] is True
    assert r["section_body"] is None
    assert set(r["available_sections"]) == {"Why", "Evidence"}


async def test_get_without_section_returns_full_body_unchanged(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    body = "lead\n\n**Why:** w\n"
    _write_entry_raw(mem, "a.md", name="X", description="d",
                      type_="feedback", body=body)
    r = await execute({"op": "get", "path": "a.md"}, _ctx(mem))
    assert r["ok"] is True
    assert "section" not in r
    assert "section_body" not in r
    assert "**Why:**" in r["body"]


async def test_get_section_rejects_empty_string(tmp_path):
    project = tmp_path
    (project / "yuxu.json").write_text("{}\n")
    mem = project / "data" / "memory"
    mem.mkdir(parents=True)
    _write_entry_raw(mem, "a.md", name="X", description="d",
                      type_="feedback", body="b")
    r = await execute({"op": "get", "path": "a.md", "section": "   "},
                       _ctx(mem))
    assert r["ok"] is False
    assert "section" in r["error"]
