"""admission_gate — write-admission 3-stage quality check."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from yuxu.bundled.admission_gate.handler import (
    _golden_replay, _jaccard, _noop_baseline, _trigrams,
    execute,
)
from yuxu.core.bus import Bus

pytestmark = pytest.mark.asyncio


# -- helpers ----------------------------------------------------


ENTRY_OK = """---
name: Kernel Invariants
description: 架子铁律 可靠大于简单大于高效
type: feedback
---
body: kernel must never crash
"""

ENTRY_NO_FM = "no frontmatter here at all\n"

ENTRY_WITH_SESSION = """---
name: Something Cited
description: cites a session
type: project
originSessionId: abcd1234-5678-90ef-cafe-0000000000aa
---
body
"""


def _mk_memory_root(tmp_path: Path, entries: list[tuple[str, str]]) -> Path:
    root = tmp_path / "mem"
    root.mkdir()
    for name, body in entries:
        (root / name).write_text(body, encoding="utf-8")
    return root


class _StubBus:
    """Stand-in for ctx.bus that answers admission_gate's llm_driver
    request with a programmable reply."""

    def __init__(self, reply=None, raises=None):
        self.reply = reply
        self.raises = raises
        self.calls: list[tuple[str, dict]] = []

    async def request(self, target: str, payload: dict, timeout: float = 5.0):
        self.calls.append((target, payload))
        if self.raises is not None:
            raise self.raises
        return self.reply


def _ctx(bus) -> SimpleNamespace:
    return SimpleNamespace(bus=bus)


# -- small helpers ----------------------------------------------


async def test_trigrams_small_strings():
    assert _trigrams("") == set()
    assert _trigrams("ab") == {"ab"}
    assert "abc" in _trigrams("abcd")
    # whitespace-insensitive
    assert _trigrams("a b c") == _trigrams("abc")


async def test_jaccard_edge_cases():
    assert _jaccard(set(), set()) == 0.0
    assert _jaccard({"a"}, {"a"}) == 1.0
    assert _jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)


# -- stage 2: golden_replay -------------------------------------


async def test_golden_replay_no_citation_passes(tmp_path: Path):
    r = _golden_replay({"name": "x", "description": "y"},
                        memory_root=tmp_path, session_root_override=None)
    assert r["pass"] is True
    assert "no session" in r["reason"].lower()


async def test_golden_replay_citation_missing_session_root_fails(tmp_path: Path):
    r = _golden_replay({"originSessionId": "abcd1234-aaaa"},
                        memory_root=tmp_path, session_root_override=None)
    assert r["pass"] is False
    assert "no session_root" in r["reason"]


async def test_golden_replay_found(tmp_path: Path):
    sess = tmp_path / "sessions"
    sess.mkdir()
    (sess / "2026-04-24-abcd1234.jsonl").write_text("{}\n", encoding="utf-8")
    r = _golden_replay({"originSessionId": "abcd1234-5678-ffff"},
                        memory_root=tmp_path,
                        session_root_override=str(sess))
    assert r["pass"] is True
    assert "abcd1234" in r["reason"]


async def test_golden_replay_missing_file_fails(tmp_path: Path):
    sess = tmp_path / "sessions"
    sess.mkdir()
    r = _golden_replay({"originSessionId": "abcd1234-aaaa-bbbb"},
                        memory_root=tmp_path,
                        session_root_override=str(sess))
    assert r["pass"] is False
    assert "not found" in r["reason"]


# -- stage 3: noop_baseline -------------------------------------


async def test_noop_baseline_empty_root_passes(tmp_path: Path):
    root = _mk_memory_root(tmp_path, [])
    r = _noop_baseline({"name": "fresh", "description": "brand new idea"},
                        memory_root=root, target_path=None,
                        dedup_threshold=0.6)
    assert r["pass"] is True


async def test_noop_baseline_name_collision_fails(tmp_path: Path):
    root = _mk_memory_root(tmp_path, [
        ("existing.md", """---
name: Kernel Invariants
description: something entirely different
type: feedback
---
""")
    ])
    r = _noop_baseline({"name": "Kernel Invariants",
                         "description": "different words"},
                        memory_root=root, target_path=None,
                        dedup_threshold=0.6)
    assert r["pass"] is False
    assert "name collision" in r["reason"]
    assert r["match_path"].endswith("existing.md")


async def test_noop_baseline_high_similarity_fails(tmp_path: Path):
    root = _mk_memory_root(tmp_path, [
        ("old.md", """---
name: Old Entry
description: the memory system stores counts and tags
type: project
---
""")
    ])
    r = _noop_baseline({"name": "New Entry",
                         "description": "the memory system stores counts and tags"},
                        memory_root=root, target_path=None,
                        dedup_threshold=0.5)
    assert r["pass"] is False
    assert "Jaccard" in r["reason"]


async def test_noop_baseline_skips_self(tmp_path: Path):
    root = _mk_memory_root(tmp_path, [
        ("same.md", """---
name: Self Entry
description: identical to the one being updated
type: feedback
---
""")
    ])
    target = root / "same.md"
    r = _noop_baseline({"name": "Self Entry",
                         "description": "identical to the one being updated"},
                        memory_root=root, target_path=target.resolve(),
                        dedup_threshold=0.5)
    assert r["pass"] is True


async def test_noop_baseline_ignores_archive_and_drafts(tmp_path: Path):
    root = _mk_memory_root(tmp_path, [])
    # craft an archive copy that looks like an old collision
    (root / "_archive").mkdir()
    (root / "_archive" / "rejected").mkdir()
    (root / "_archive" / "rejected" / "old.md").write_text("""---
name: Foo
description: archive should be ignored
type: feedback
---
""", encoding="utf-8")
    r = _noop_baseline({"name": "Foo", "description": "new rule"},
                        memory_root=root, target_path=None,
                        dedup_threshold=0.3)
    assert r["pass"] is True


# -- stage 1: surface_check (via the full check op) -------------


async def test_check_surface_pass(tmp_path: Path):
    bus = _StubBus(reply={"ok": True, "content": '{"pass": true, "reason": "ok"}'})
    r = await execute({
        "op": "check",
        "entry_body": ENTRY_OK,
        "memory_root": str(tmp_path),
    }, _ctx(bus))
    assert r["ok"] is True
    assert r["pass"] is True
    assert r["stages"]["surface_check"]["pass"] is True
    assert "PASS" in r["verdict"]


async def test_check_surface_fails(tmp_path: Path):
    bus = _StubBus(reply={"ok": True,
                           "content": '{"pass": false, "reason": "verbose-obvious"}'})
    r = await execute({
        "op": "check",
        "entry_body": ENTRY_OK,
        "memory_root": str(tmp_path),
    }, _ctx(bus))
    assert r["pass"] is False
    assert r["stages"]["surface_check"]["pass"] is False
    assert "verbose-obvious" in r["stages"]["surface_check"]["reason"]


async def test_check_llm_missing_is_lenient(tmp_path: Path):
    bus = _StubBus(raises=LookupError("llm_driver"))
    r = await execute({
        "op": "check",
        "entry_body": ENTRY_OK,
        "memory_root": str(tmp_path),
    }, _ctx(bus))
    assert r["stages"]["surface_check"]["pass"] is True
    assert r["stages"]["surface_check"].get("skipped") == "llm_driver_not_loaded"


async def test_check_llm_unparseable_is_lenient(tmp_path: Path):
    bus = _StubBus(reply={"ok": True, "content": "totally not json"})
    r = await execute({
        "op": "check",
        "entry_body": ENTRY_OK,
        "memory_root": str(tmp_path),
    }, _ctx(bus))
    assert r["stages"]["surface_check"]["pass"] is True
    assert r["stages"]["surface_check"].get("skipped") == "verdict_unparseable"


async def test_check_requires_entry_body():
    bus = _StubBus(reply={"ok": True, "content": '{"pass": true}'})
    r = await execute({"op": "check"}, _ctx(bus))
    assert r["ok"] is False


async def test_check_rejects_entry_without_frontmatter(tmp_path: Path):
    bus = _StubBus(reply={"ok": True, "content": '{"pass": true}'})
    r = await execute({
        "op": "check",
        "entry_body": ENTRY_NO_FM,
        "memory_root": str(tmp_path),
    }, _ctx(bus))
    assert r["ok"] is True
    assert r["pass"] is False
    assert "no-frontmatter" in r["verdict"]


async def test_check_unknown_op_errors():
    bus = _StubBus()
    r = await execute({"op": "frobnicate"}, _ctx(bus))
    assert r["ok"] is False


async def test_check_combines_stages_AND(tmp_path: Path):
    # surface passes but noop_baseline fails → overall fail
    root = _mk_memory_root(tmp_path, [
        ("existing.md", """---
name: Kernel Invariants
description: something else
type: feedback
---
""")
    ])
    bus = _StubBus(reply={"ok": True, "content": '{"pass": true, "reason": "ok"}'})
    r = await execute({
        "op": "check",
        "entry_body": ENTRY_OK,
        "memory_root": str(root),
    }, _ctx(bus))
    assert r["pass"] is False
    assert r["stages"]["surface_check"]["pass"] is True
    assert r["stages"]["noop_baseline"]["pass"] is False


# -- integration via Loader -------------------------------------


async def test_loaded_as_skill(tmp_path: Path):
    """admission_gate must be discoverable and callable via bus.request."""
    from yuxu.core.loader import Loader
    import yuxu as _y

    bundled_dir = str(Path(_y.__file__).parent / "bundled")
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    assert "admission_gate" in loader.specs
    # Skill; no lifecycle to start. Bus routes request to the execute fn
    # by skill name — same as memory.
