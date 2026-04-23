from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from yuxu.core import session_log

pytestmark = pytest.mark.asyncio


def _make_project(root: Path, agent_name: str = "alpha") -> Path:
    """Build a tmp yuxu project; return the agent dir path."""
    (root / "yuxu.json").write_text("{}\n")
    agents = root / "agents"
    agents.mkdir()
    agent_dir = agents / agent_name
    agent_dir.mkdir()
    return agent_dir


async def test_find_project_root_walks_up(tmp_path):
    agent_dir = _make_project(tmp_path, "alpha")
    assert session_log.find_project_root(agent_dir) == tmp_path.resolve()


async def test_find_project_root_none_when_missing(tmp_path):
    d = tmp_path / "stray"
    d.mkdir()
    assert session_log.find_project_root(d) is None


async def test_append_writes_jsonl(tmp_path):
    agent_dir = _make_project(tmp_path, "alpha")
    path = await session_log.append(agent_dir, "alpha",
                                     {"event": "lifecycle", "state": "ready"})
    assert path is not None
    assert path == tmp_path.resolve() / "data" / "sessions" / "alpha.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["event"] == "lifecycle"
    assert obj["state"] == "ready"
    assert isinstance(obj["ts"], (int, float))


async def test_append_is_no_op_without_yuxu_json(tmp_path):
    d = tmp_path / "loose"
    d.mkdir()
    path = await session_log.append(d, "alpha", {"event": "lifecycle"})
    assert path is None


async def test_append_appends_in_order(tmp_path):
    agent_dir = _make_project(tmp_path, "alpha")
    for i in range(5):
        await session_log.append(agent_dir, "alpha",
                                  {"event": "message", "role": "user",
                                   "content": f"msg-{i}"})
    path = tmp_path.resolve() / "data" / "sessions" / "alpha.jsonl"
    objs = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    assert [o["content"] for o in objs] == [f"msg-{i}" for i in range(5)]


async def test_append_concurrent_same_file_no_corruption(tmp_path):
    agent_dir = _make_project(tmp_path, "alpha")
    # Large-ish payload to stress the lock; tail-of-line should not interleave.
    big_body = "X" * 4000

    async def writer(i: int) -> None:
        await session_log.append(agent_dir, "alpha",
                                  {"event": "message", "role": "assistant",
                                   "content": f"{i}:{big_body}"})

    await asyncio.gather(*[writer(i) for i in range(20)])
    path = tmp_path.resolve() / "data" / "sessions" / "alpha.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 20
    # every line must parse cleanly
    for ln in lines:
        obj = json.loads(ln)
        assert obj["content"].endswith(big_body)


async def test_resolve_transcript_path_none_when_no_project(tmp_path):
    d = tmp_path / "nope"
    d.mkdir()
    assert session_log.resolve_transcript_path(d, "alpha") is None


async def test_resolve_transcript_path_returns_expected(tmp_path):
    agent_dir = _make_project(tmp_path, "alpha")
    p = session_log.resolve_transcript_path(agent_dir, "alpha")
    assert p == tmp_path.resolve() / "data" / "sessions" / "alpha.jsonl"
