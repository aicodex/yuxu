"""HarnessProMax v0 — agent-creator agent."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest

from yuxu.bundled.harness_pro_max.handler import (
    COMMAND,
    HarnessProMax,
    _find_project_root,
)
from yuxu.core.bus import Bus

pytestmark = pytest.mark.asyncio


# -- pure helpers -----------------------------------------------


def test_find_project_root_walks_up(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "yuxu.json").write_text("{}")
    deep = root / "_system" / "harness_pro_max"
    deep.mkdir(parents=True)
    assert _find_project_root(deep) == root


def test_find_project_root_returns_none_when_absent(tmp_path):
    assert _find_project_root(tmp_path) is None


# -- fixtures + fakes -------------------------------------------


_GOOD_AGENT_MD = dedent("""\
    ---
    driver: llm
    run_mode: one_shot
    scope: user
    depends_on: [llm_driver]
    ready_timeout: 30
    ---
    # weather_bot

    Summarizes the morning weather.
""")


class _FakeLoader:
    """Tracks scan / ensure_running calls and returns canned status."""
    def __init__(self, ensure_status: str = "ready",
                 ensure_raises: Exception | None = None) -> None:
        self.scan_calls = 0
        self.ensure_calls: list[str] = []
        self._status = ensure_status
        self._raise = ensure_raises

    async def scan(self) -> None:
        self.scan_calls += 1

    async def ensure_running(self, name: str) -> str:
        self.ensure_calls.append(name)
        if self._raise is not None:
            raise self._raise
        return self._status


def _make_project(tmp_path: Path) -> Path:
    """Cheap project root: just a dir with yuxu.json + agents/."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "yuxu.json").write_text("{}")
    (root / "agents").mkdir()
    return root


def _make_ctx(tmp_path: Path, bus: Bus,
              loader: _FakeLoader | None = None) -> SimpleNamespace:
    """Mimic AgentContext just enough for HarnessProMax."""
    project_root = _make_project(tmp_path)
    agent_dir = project_root / "_system" / "harness_pro_max"
    agent_dir.mkdir(parents=True)
    return SimpleNamespace(
        bus=bus,
        loader=loader or _FakeLoader(),
        agent_dir=agent_dir,
        name="harness_pro_max",
    )


def _wire_llm_driver(bus: Bus, *, classify: dict, generate_text: str | None,
                     classify_ok: bool = True, generate_ok: bool = True):
    """Register a fake llm_driver that switches behavior based on the
    system_prompt content (classify vs generate)."""
    seen: list[dict] = []

    async def handler(msg):
        payload = dict(msg.payload) if isinstance(msg.payload, dict) else {}
        seen.append(payload)
        sys = payload.get("system_prompt", "")
        if "classifier for the yuxu framework" in sys:
            if not classify_ok:
                return {"ok": False, "error": "classify_simulated_failure"}
            return {"ok": True, "content": json.dumps(classify),
                    "stop_reason": "complete", "usage": {}}
        if "AGENT.md author for the yuxu agent framework" in sys:
            if not generate_ok:
                return {"ok": False, "error": "generate_simulated_failure"}
            return {"ok": True, "content": generate_text or "",
                    "stop_reason": "complete", "usage": {}}
        return {"ok": False, "error": f"unexpected system_prompt: {sys[:60]}"}

    bus.register("llm_driver", handler)
    return seen


def _classify_payload(name: str = "weather_bot",
                      driver: str = "llm",
                      depends_on: list[str] | None = None) -> dict:
    return {
        "agent_type": "default",
        "suggested_name": name,
        "run_mode": "one_shot",
        "depends_on": depends_on if depends_on is not None else ["llm_driver"],
        "driver": driver,
        "reasoning": "morning summary task",
    }


# -- happy path -------------------------------------------------


async def test_create_agent_happy_path_writes_file_and_starts(tmp_path):
    bus = Bus()
    loader = _FakeLoader()
    ctx = _make_ctx(tmp_path, bus, loader)
    _wire_llm_driver(bus, classify=_classify_payload(),
                     generate_text=_GOOD_AGENT_MD)

    h = HarnessProMax(ctx)
    r = await h.create_agent_from_description("morning weather summary")

    assert r["ok"] is True
    assert r["name"] == "weather_bot"
    written = Path(r["path"])
    assert written.is_dir()
    assert (written / "AGENT.md").read_text(encoding="utf-8").startswith("---")
    assert loader.scan_calls == 1
    assert loader.ensure_calls == ["weather_bot"]
    assert r["status"] == "ready"
    assert r["warnings"] == []  # classifier picked driver=llm too


# -- driver downgrade warning -----------------------------------


async def test_python_driver_classification_is_downgraded_with_warning(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    _wire_llm_driver(bus, classify=_classify_payload(driver="python"),
                     generate_text=_GOOD_AGENT_MD)
    h = HarnessProMax(ctx)

    r = await h.create_agent_from_description("a python-flavored bot")

    assert r["ok"] is True
    assert any("driver=llm" in w and "python" in w for w in r["warnings"])


# -- failure stages ---------------------------------------------


async def test_classify_failure_short_circuits(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    _wire_llm_driver(bus, classify=_classify_payload(),
                     generate_text=None, classify_ok=False)
    h = HarnessProMax(ctx)

    r = await h.create_agent_from_description("anything")

    assert r["ok"] is False
    assert r["stage"] == "classify_intent"
    # No file should have been written and ensure_running not called
    assert not (Path(ctx.agent_dir).parent.parent / "agents" / "weather_bot").exists()


async def test_generate_failure_after_successful_classify(tmp_path):
    bus = Bus()
    loader = _FakeLoader()
    ctx = _make_ctx(tmp_path, bus, loader)
    _wire_llm_driver(bus, classify=_classify_payload(),
                     generate_text=None, generate_ok=False)
    h = HarnessProMax(ctx)

    r = await h.create_agent_from_description("anything")

    assert r["ok"] is False
    assert r["stage"] == "generate_agent_md"
    assert loader.scan_calls == 0
    assert not (Path(ctx.agent_dir).parent.parent / "agents" / "weather_bot").exists()


async def test_name_conflict_refused(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    project_root = Path(ctx.agent_dir).parent.parent
    (project_root / "agents" / "weather_bot").mkdir(parents=True)

    _wire_llm_driver(bus, classify=_classify_payload(),
                     generate_text=_GOOD_AGENT_MD)
    h = HarnessProMax(ctx)

    r = await h.create_agent_from_description("anything")

    assert r["ok"] is False
    assert r["stage"] == "conflict"
    assert "weather_bot" in r["error"]


async def test_missing_yuxu_json_short_circuits(tmp_path):
    bus = Bus()
    # Don't use _make_ctx; build a ctx whose agent_dir has no yuxu.json above it
    rogue_dir = tmp_path / "rogue" / "_system" / "harness"
    rogue_dir.mkdir(parents=True)
    ctx = SimpleNamespace(bus=bus, loader=_FakeLoader(), agent_dir=rogue_dir,
                          name="harness_pro_max")
    _wire_llm_driver(bus, classify=_classify_payload(),
                     generate_text=_GOOD_AGENT_MD)
    h = HarnessProMax(ctx)

    r = await h.create_agent_from_description("anything")

    assert r["ok"] is False
    assert r["stage"] == "find_project_root"


async def test_explicit_project_dir_override_used(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    other_root = tmp_path / "other_proj"
    other_root.mkdir()
    (other_root / "yuxu.json").write_text("{}")
    (other_root / "agents").mkdir()
    _wire_llm_driver(bus, classify=_classify_payload(),
                     generate_text=_GOOD_AGENT_MD)
    h = HarnessProMax(ctx)

    r = await h.create_agent_from_description("x", project_dir=other_root)

    assert r["ok"] is True
    assert Path(r["path"]).parent == other_root / "agents"


async def test_explicit_project_dir_rejected_if_not_yuxu_project(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    bogus = tmp_path / "not_a_proj"
    bogus.mkdir()
    _wire_llm_driver(bus, classify=_classify_payload(),
                     generate_text=_GOOD_AGENT_MD)
    h = HarnessProMax(ctx)

    r = await h.create_agent_from_description("x", project_dir=bogus)

    assert r["ok"] is False
    assert r["stage"] == "find_project_root"


async def test_ensure_running_failure_keeps_file_on_disk(tmp_path):
    bus = Bus()
    loader = _FakeLoader(ensure_raises=RuntimeError("boom"))
    ctx = _make_ctx(tmp_path, bus, loader)
    _wire_llm_driver(bus, classify=_classify_payload(),
                     generate_text=_GOOD_AGENT_MD)
    h = HarnessProMax(ctx)

    r = await h.create_agent_from_description("anything")

    assert r["ok"] is False
    assert r["stage"] == "ensure_running"
    assert r["agent_md_written"] is True
    project_root = Path(ctx.agent_dir).parent.parent
    assert (project_root / "agents" / "weather_bot" / "AGENT.md").exists()


# -- handle() bus surface ---------------------------------------


class _Msg:
    def __init__(self, payload):
        self.payload = payload


async def test_handle_create_agent_op(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    _wire_llm_driver(bus, classify=_classify_payload(),
                     generate_text=_GOOD_AGENT_MD)
    h = HarnessProMax(ctx)

    r = await h.handle(_Msg({"op": "create_agent", "description": "x"}))
    assert r["ok"] is True
    assert r["name"] == "weather_bot"


async def test_handle_unknown_op(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    h = HarnessProMax(ctx)
    r = await h.handle(_Msg({"op": "weird"}))
    assert r["ok"] is False
    assert "unknown op" in r["error"]


async def test_handle_missing_description(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    h = HarnessProMax(ctx)
    r = await h.handle(_Msg({"op": "create_agent"}))
    assert r["ok"] is False
    assert "missing field: description" in r["error"]


# -- gateway integration (slash-command path) -------------------


async def test_slash_command_triggers_create_and_replies(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    _wire_llm_driver(bus, classify=_classify_payload(),
                     generate_text=_GOOD_AGENT_MD)

    sent_replies: list[dict] = []
    register_calls: list[dict] = []

    async def fake_gateway(msg):
        payload = dict(msg.payload) if isinstance(msg.payload, dict) else {}
        op = payload.get("op")
        if op == "register_command":
            register_calls.append(payload)
            return {"ok": True}
        if op == "send":
            sent_replies.append(payload)
            return {"ok": True}
        if op == "unregister_command":
            return {"ok": True}
        return {"ok": False, "error": f"unhandled op: {op}"}

    bus.register("gateway", fake_gateway)

    h = HarnessProMax(ctx)
    await h.install()
    assert register_calls and register_calls[0]["command"] == COMMAND

    # Simulate gateway publishing a /new command event
    await bus.publish("gateway.command_invoked", {
        "command": COMMAND,
        "args": "morning weather summary",
        "session_key": "session-abc",
    })
    # let the subscriber + create flow finish
    for _ in range(20):
        await asyncio.sleep(0)
        if sent_replies:
            break

    assert sent_replies, "expected at least one reply via gateway"
    reply = sent_replies[-1]
    assert reply["session_key"] == "session-abc"
    assert "weather_bot" in reply["text"]
    assert reply["text"].startswith("✅")


async def test_slash_command_empty_args_prints_usage(tmp_path):
    bus = Bus()
    ctx = _make_ctx(tmp_path, bus)
    sent: list[dict] = []

    async def fake_gateway(msg):
        payload = dict(msg.payload) if isinstance(msg.payload, dict) else {}
        if payload.get("op") == "send":
            sent.append(payload)
        return {"ok": True}

    bus.register("gateway", fake_gateway)
    h = HarnessProMax(ctx)
    await h.install()

    await bus.publish("gateway.command_invoked", {
        "command": COMMAND, "args": "", "session_key": "k",
    })
    for _ in range(10):
        await asyncio.sleep(0)
        if sent:
            break

    assert sent and sent[0]["text"].startswith("Usage:")
