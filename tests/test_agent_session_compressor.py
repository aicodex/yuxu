"""session_compressor — raw JSONL → compressed memory entry."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuxu.bundled.session_compressor.handler import (
    ARCHIVED_TOPIC,
    COMPRESSED_TOPIC,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MIN_TOKENS,
    NAME,
    SessionCompressor,
    _derive_target_tokens,
    _extract_description,
    _extract_session_id,
    _pick_date,
)
from yuxu.core.bus import Bus
from yuxu.core.frontmatter import parse_frontmatter

pytestmark = pytest.mark.asyncio


# -- helpers -----------------------------------------------------


def _make_jsonl(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for entry in lines:
            f.write(json.dumps(entry) + "\n")
    return path


def _sample_jsonl(path: Path) -> Path:
    # Pad content to exceed the summary size so compression_ratio is
    # positive — real-world sessions are always much bigger than the
    # summary, this just mirrors that.
    padding = "Filler context for realistic compression ratio. " * 100
    return _make_jsonl(path, [
        {"type": "user", "message": {"role": "user",
                                       "content": f"Fix the memory bug {padding}"}},
        {"type": "assistant", "message": {"role": "assistant",
                                           "content": f"reading the code... {padding}"}},
        {"type": "user", "message": {"role": "user",
                                       "content": f"also add tests {padding}"}},
    ])


def _mk_project(tmp_path: Path) -> Path:
    """Create a fake yuxu project skeleton."""
    (tmp_path / "yuxu.json").write_text('{"name":"fake"}', encoding="utf-8")
    return tmp_path


def _ctx(bus, project: Path) -> SimpleNamespace:
    """agent_dir is inside project so walk-up finds yuxu.json."""
    agent_dir = project / "agents" / "session_compressor"
    agent_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(bus=bus, agent_dir=agent_dir,
                            name="session_compressor", loader=None)


def _register_fake_compressor(bus: Bus, body: str, *,
                                 ok: bool = True,
                                 fallback_used: bool = False,
                                 raises: BaseException | None = None):
    calls: list[dict] = []

    async def handler(msg):
        calls.append(dict(msg.payload))
        if raises:
            raise raises
        if not ok:
            return {"ok": False, "error": "forced failure"}
        return {
            "ok": True,
            "merged_summary": body,
            "per_document": [],
            "total_tokens_before": 1000,
            "total_tokens_after": len(body) // 4,
            "savings_ratio": 0.9,
            "fallback_used": fallback_used,
            "skipped": False,
        }
    bus.register("context_compressor", handler)
    return calls


SAMPLE_SUMMARY = """1. Primary Request and Intent:
   User wanted to fix a bug in the memory indexer. The fix required
   adding a new regex pattern.

2. Key Technical Concepts:
   - Regex pattern for UUIDs
   - Frontmatter parsing

3. Files and Code Sections:
   - src/yuxu/bundled/memory/handler.py

4. Errors and Fixes:
   - Initial pattern failed on mixed-case input. Switched to re.IGNORECASE.

5. All User Messages:
    - "Fix the memory bug"
    - "also add tests"

6. Pending Tasks:
   - Write unit tests

7. Current Work:
   Added regex fix; writing tests now.

8. Optional Next Step:
   Run the test suite with "pytest".
"""


# -- primitives --------------------------------------------------


async def test_extract_session_id_from_date_prefix_stem():
    p = Path("2026-04-24-023fac6d.jsonl")
    assert _extract_session_id(p) == "023fac6d"


async def test_extract_session_id_from_full_uuid_stem():
    p = Path("023fac6d-08cb-4e76-9459-bc650b217663.jsonl")
    assert _extract_session_id(p) == "023fac6d-08cb-4e76-9459-bc650b217663"


async def test_extract_session_id_fallback_unknown():
    p = Path("no-uuid-here.jsonl")
    assert _extract_session_id(p) is None


async def test_pick_date_prefers_filename():
    p = Path("2026-03-15-abcd1234.jsonl")
    assert _pick_date(p, "") == __import__("datetime").date(2026, 3, 15)


async def test_derive_target_tokens_respects_bounds():
    # ratio 10% of 100_000 → 10_000
    assert _derive_target_tokens(100_000, ratio=0.1,
                                   floor=5000, ceiling=50_000) == 10_000
    # below floor
    assert _derive_target_tokens(10_000, ratio=0.1,
                                   floor=5000, ceiling=50_000) == 5000
    # above ceiling
    assert _derive_target_tokens(10_000_000, ratio=0.1,
                                   floor=5000, ceiling=50_000) == 50_000


async def test_extract_description_finds_primary_request():
    desc = _extract_description(SAMPLE_SUMMARY)
    # Should pull first sentence of the Primary Request section
    assert "memory bug" in desc.lower() or "memory indexer" in desc.lower()
    assert len(desc) <= 200


async def test_extract_description_fallback_on_no_section():
    body = "just some ordinary content\n\nwithout the expected structure"
    desc = _extract_description(body)
    assert "just some ordinary content" in desc


# -- compress_jsonl op end-to-end -----------------------------


async def test_compress_jsonl_writes_memory_entry(tmp_path: Path):
    project = _mk_project(tmp_path)
    jsonl = _sample_jsonl(project / "data" / "sessions_raw"
                            / "2026-04-24-abcd1234.jsonl")
    bus = Bus()
    _register_fake_compressor(bus, SAMPLE_SUMMARY)

    agent = SessionCompressor(_ctx(bus, project))
    await agent.install()
    try:
        r = await agent.handle(SimpleNamespace(payload={
            "op": "compress_jsonl",
            "jsonl_path": str(jsonl),
        }))
    finally:
        await agent.uninstall()

    assert r["ok"] is True
    entry = Path(r["memory_entry_path"])
    assert entry.exists()
    # lives under data/memory/sessions/
    rel = entry.relative_to(project / "data" / "memory")
    assert rel.parts[0] == "sessions"
    assert rel.name == "2026-04-24-abcd1234.md"

    fm, body = parse_frontmatter(entry.read_text(encoding="utf-8"))
    assert fm["type"] == "session"
    assert fm["scope"] == "project"
    assert fm["evidence_level"] == "observed"
    assert fm["status"] == "current"
    assert fm["tags"] == ["session"]
    assert fm["originSessionId"] == "abcd1234"
    assert fm["source_bytes"] > 0
    assert fm["compressed_bytes"] > 0
    assert 0.0 < fm["compression_ratio"] < 1.0
    assert "Primary Request" in body


async def test_compress_jsonl_publishes_event(tmp_path: Path):
    project = _mk_project(tmp_path)
    jsonl = _sample_jsonl(project / "archives"
                            / "2026-04-24-aaaaaaaa.jsonl")
    bus = Bus()
    _register_fake_compressor(bus, SAMPLE_SUMMARY)
    seen: list[dict] = []

    async def _h(e):
        seen.append(e.get("payload") or {})
    bus.subscribe(COMPRESSED_TOPIC, _h)

    agent = SessionCompressor(_ctx(bus, project))
    await agent.install()
    try:
        r = await agent.handle(SimpleNamespace(payload={
            "op": "compress_jsonl", "jsonl_path": str(jsonl),
        }))
    finally:
        await agent.uninstall()

    assert r["ok"]
    for _ in range(20):
        if seen:
            break
        await asyncio.sleep(0.005)
    assert seen
    assert seen[0]["originSessionId"] == "aaaaaaaa"
    assert seen[0]["memory_entry_path"]


async def test_on_archived_auto_triggers_compression(tmp_path: Path):
    """bus.publish(session.archived) must auto-fire compress."""
    project = _mk_project(tmp_path)
    jsonl = _sample_jsonl(project / "archives"
                            / "2026-04-24-deadbeef.jsonl")
    bus = Bus()
    _register_fake_compressor(bus, SAMPLE_SUMMARY)

    agent = SessionCompressor(_ctx(bus, project))
    await agent.install()
    try:
        await bus.publish(ARCHIVED_TOPIC, {"jsonl_path": str(jsonl)})
        # Wait for the subscriber to run.
        for _ in range(40):
            entry = (project / "data" / "memory" / "sessions"
                     / "2026-04-24-deadbeef.md")
            if entry.exists():
                break
            await asyncio.sleep(0.01)
    finally:
        await agent.uninstall()

    entry = project / "data" / "memory" / "sessions" / "2026-04-24-deadbeef.md"
    assert entry.exists()


async def test_compress_jsonl_missing_file_errors(tmp_path: Path):
    project = _mk_project(tmp_path)
    bus = Bus()
    _register_fake_compressor(bus, SAMPLE_SUMMARY)
    agent = SessionCompressor(_ctx(bus, project))
    r = await agent.handle(SimpleNamespace(payload={
        "op": "compress_jsonl",
        "jsonl_path": str(tmp_path / "nope.jsonl"),
    }))
    assert r["ok"] is False


async def test_compress_jsonl_requires_path():
    bus = Bus()
    agent = SessionCompressor(SimpleNamespace(bus=bus, agent_dir=Path("/tmp")))
    r = await agent.handle(SimpleNamespace(payload={"op": "compress_jsonl"}))
    assert r["ok"] is False


async def test_unknown_op_errors(tmp_path: Path):
    agent = SessionCompressor(SimpleNamespace(bus=Bus(), agent_dir=tmp_path))
    r = await agent.handle(SimpleNamespace(payload={"op": "frobnicate"}))
    assert r["ok"] is False


async def test_compressor_failure_propagates(tmp_path: Path):
    project = _mk_project(tmp_path)
    jsonl = _sample_jsonl(project / "archives" / "2026-04-24-bbbbbbbb.jsonl")
    bus = Bus()
    _register_fake_compressor(bus, "", ok=False)
    agent = SessionCompressor(_ctx(bus, project))
    r = await agent.handle(SimpleNamespace(payload={
        "op": "compress_jsonl", "jsonl_path": str(jsonl),
    }))
    assert r["ok"] is False
    assert "context_compressor" in r["error"]


async def test_idempotent_overwrite(tmp_path: Path):
    project = _mk_project(tmp_path)
    jsonl = _sample_jsonl(project / "archives" / "2026-04-24-cafecafe.jsonl")
    bus = Bus()
    _register_fake_compressor(bus, SAMPLE_SUMMARY)
    agent = SessionCompressor(_ctx(bus, project))

    r1 = await agent.handle(SimpleNamespace(payload={
        "op": "compress_jsonl", "jsonl_path": str(jsonl),
    }))
    r2 = await agent.handle(SimpleNamespace(payload={
        "op": "compress_jsonl", "jsonl_path": str(jsonl),
    }))
    assert r1["ok"] and r2["ok"]
    assert r1["memory_entry_path"] == r2["memory_entry_path"]


async def test_memory_skill_finds_the_session_entry(tmp_path: Path):
    """After compression, the memory skill's list op should surface
    the new entry — proving progressive disclosure works end-to-end."""
    from yuxu.bundled.memory.handler import execute as memory_execute

    project = _mk_project(tmp_path)
    jsonl = _sample_jsonl(project / "archives" / "2026-04-24-11111111.jsonl")
    bus = Bus()
    _register_fake_compressor(bus, SAMPLE_SUMMARY)
    agent = SessionCompressor(_ctx(bus, project))

    r = await agent.handle(SimpleNamespace(payload={
        "op": "compress_jsonl", "jsonl_path": str(jsonl),
    }))
    assert r["ok"]

    # Now query memory skill with type=session filter
    mem_root = project / "data" / "memory"
    fake_ctx = SimpleNamespace(
        bus=bus,
        agent_dir=project / "agents" / "consumer",
    )
    (project / "agents" / "consumer").mkdir(parents=True, exist_ok=True)
    lst = await memory_execute({
        "op": "list",
        "mode": "reflect",            # include everything
        "memory_root": str(mem_root),
        "type": "session",
    }, fake_ctx)
    assert lst["ok"]
    entries = lst["entries"]
    assert len(entries) == 1
    e = entries[0]
    assert e["type"] == "session"
    assert "session" in e["tags"]
    # description surfaced for L1 consumption
    assert e["description"]


async def test_loader_discovers_agent():
    from yuxu.core.loader import Loader
    import yuxu as _y

    bundled_dir = str(Path(_y.__file__).parent / "bundled")
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    assert NAME in loader.specs
    # depends_on includes context_compressor
    assert "context_compressor" in loader.specs[NAME].depends_on
