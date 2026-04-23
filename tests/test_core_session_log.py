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


# -- format_jsonl_transcript ------------------------------------


async def test_format_transcript_empty_when_file_missing(tmp_path):
    p = tmp_path / "nope.jsonl"
    assert session_log.format_jsonl_transcript(p) == ""


async def test_format_transcript_lifecycle_horizontal_rule(tmp_path):
    p = tmp_path / "run.jsonl"
    p.write_text(
        json.dumps({"ts": 1714000000.0, "event": "lifecycle", "state": "ready"}) + "\n"
        + json.dumps({"ts": 1714000001.0, "event": "lifecycle",
                      "state": "stopped", "reason": "normal"}) + "\n",
        encoding="utf-8",
    )
    text = session_log.format_jsonl_transcript(p)
    assert "lifecycle: ready" in text
    assert "lifecycle: stopped — normal" in text
    # Each lifecycle gets horizontal-rule framing
    assert "---" in text


async def test_format_transcript_message_bodies_rendered(tmp_path):
    p = tmp_path / "run.jsonl"
    p.write_text(
        json.dumps({"ts": 1714000000.0, "event": "message",
                    "role": "user", "content": "Hello"}) + "\n"
        + json.dumps({"ts": 1714000001.0, "event": "message",
                      "role": "assistant", "content": "Hi back",
                      "iteration": 1}) + "\n",
        encoding="utf-8",
    )
    text = session_log.format_jsonl_transcript(p)
    assert "USER" in text
    assert "Hello" in text
    assert "ASSISTANT" in text
    assert "Hi back" in text
    assert "iter=1" in text


async def test_format_transcript_reasoning_kind(tmp_path):
    p = tmp_path / "run.jsonl"
    p.write_text(
        json.dumps({"ts": 1714000000.0, "event": "message",
                    "role": "assistant", "kind": "reasoning",
                    "content": "thinking step-by-step", "iteration": 1}) + "\n"
        + json.dumps({"ts": 1714000001.0, "event": "message",
                      "role": "assistant", "content": "final answer",
                      "iteration": 1}) + "\n",
        encoding="utf-8",
    )
    text = session_log.format_jsonl_transcript(p)
    # Reasoning gets its own labeled header so curator's LLM can see it
    assert "ASSISTANT reasoning" in text
    assert "thinking step-by-step" in text
    # Final assistant still shown separately
    assert "final answer" in text
    # Reasoning must come before the final assistant reply in output order
    assert text.index("thinking step-by-step") < text.index("final answer")


async def test_format_transcript_tool_result(tmp_path):
    p = tmp_path / "run.jsonl"
    p.write_text(
        json.dumps({"ts": 1714000000.0, "event": "message",
                    "role": "tool", "tool_name": "get_price",
                    "tool_call_id": "c1", "content": '{"price": 42}',
                    "iteration": 1}) + "\n",
        encoding="utf-8",
    )
    text = session_log.format_jsonl_transcript(p)
    assert "TOOL_RESULT get_price" in text
    assert "price" in text


async def test_format_transcript_skips_malformed_lines(tmp_path, caplog):
    p = tmp_path / "run.jsonl"
    p.write_text(
        '{"ts": 0, "event": "lifecycle", "state": "ready"}\n'
        'this is not json\n'
        '{"ts": 1, "event": "lifecycle", "state": "stopped"}\n',
        encoding="utf-8",
    )
    text = session_log.format_jsonl_transcript(p)
    # Both valid lines should still render; the bad one gets skipped
    assert "ready" in text
    assert "stopped" in text


async def test_format_transcript_truncates_tail_with_marker(tmp_path):
    p = tmp_path / "run.jsonl"
    big_content = "A" * 500
    lines = []
    for i in range(50):
        lines.append(json.dumps({
            "ts": 1714000000.0 + i, "event": "message",
            "role": "user", "content": f"{i}: {big_content}",
        }))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    text = session_log.format_jsonl_transcript(p, max_chars=2_000)
    assert len(text) <= 2_200  # marker adds a bit of slack
    assert "earlier session history truncated" in text
    # Tail preserved: the LAST message should still be visible
    assert "49:" in text


async def test_format_entry_body_cap_applies_per_entry(tmp_path):
    # A single massive entry shouldn't blow past the per-entry cap even
    # without a max_chars. Useful to keep one runaway tool output from
    # starving the rest.
    p = tmp_path / "run.jsonl"
    p.write_text(
        json.dumps({"ts": 1714000000.0, "event": "message",
                    "role": "tool", "tool_name": "dump", "iteration": 1,
                    "content": "X" * 20_000}) + "\n",
        encoding="utf-8",
    )
    text = session_log.format_jsonl_transcript(p)
    assert "chars truncated" in text
