"""Topic C end-to-end: real loader + memory_curator + mocked HTTP transport.

Complements the unit-level tests:
- test_core_loader.py covers loader publishing session.ended on stop/crash.
- test_agent_memory_curator.py covers curator handling a crafted session.ended.

This one boots the full bundled ring (memory_curator, llm_driver, llm_service,
rate_limit_service, approval_queue, ...) plus a user agent, then stops the
user agent and asserts a draft file lands on disk — the whole chain.
"""
from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path

import httpx
import pytest

from yuxu.core.main import boot

pytestmark = pytest.mark.asyncio


CURATE_RESPONSE = {
    "improvements": ["e2e canary improvement"],
    "memory_edits": [
        {
            "action": "add",
            "target": "feedback_e2e_canary.md",
            "title": "e2e canary",
            "memory_type": "feedback",
            "body": (
                "---\n"
                "name: e2e canary\n"
                "description: Topic C canary\n"
                "type: feedback\n"
                "---\n\n"
                "Topic C end-to-end fired successfully.\n"
            ),
            "rationale": "composition test canary",
        }
    ],
    "summary": "Topic C lit up.",
}


def _write_rate_config(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "rate.yaml"
    cfg.write_text(
        "minimax:\n"
        "  max_concurrent: 2\n"
        "  rpm: 60\n"
        "  accounts:\n"
        "    - id: k1\n"
        "      api_key: test-key\n"
        "      base_url: http://mock/v1\n"
    )
    monkeypatch.setenv("RATE_LIMITS_CONFIG", str(cfg))
    monkeypatch.setenv("CURATOR_POOL", "minimax")
    monkeypatch.setenv("CURATOR_MODEL", "test-model")


def _write_chatty_agent(agents_dir: Path) -> None:
    ad = agents_dir / "chatty"
    ad.mkdir()
    (ad / "AGENT.md").write_text(
        "---\ndriver: python\nrun_mode: persistent\nscope: user\n---\n"
    )
    (ad / "__init__.py").write_text(textwrap.dedent("""
        import asyncio
        async def start(ctx):
            async def _loop():
                await ctx.ready()
                await asyncio.sleep(60)
            asyncio.create_task(_loop())
    """))


def _pad_session_jsonl(path: Path) -> None:
    """Append synthetic message lines to clear MIN_SOURCE_CHARS (200)."""
    lines = []
    for i in range(30):
        lines.append(json.dumps({
            "ts": 1714000000.0 + i,
            "event": "message",
            "role": "user" if i % 2 == 0 else "assistant",
            "content": (
                f"synthetic turn {i} padding content "
                "to satisfy curator transcript threshold"
            ),
        }))
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


async def test_topic_c_end_to_end(tmp_path, monkeypatch, bundled_dir):
    (tmp_path / "yuxu.json").write_text("{}\n")
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _write_chatty_agent(agents_dir)
    _write_rate_config(tmp_path, monkeypatch)
    # memory_curator's memory_root resolution for a bundled agent falls back
    # to Path.cwd()/data/memory when no yuxu.json is reachable from the
    # bundled install path — chdir pins that fallback to our tmp project.
    monkeypatch.chdir(tmp_path)

    def route(req: httpx.Request):
        body = json.loads(req.content)
        sys_prompt = ""
        for m in body.get("messages", []):
            if m.get("role") == "system":
                sys_prompt = str(m.get("content", ""))
                break
        if "memory curator" in sys_prompt:
            return httpx.Response(200, json={
                "choices": [{
                    "message": {"content": json.dumps(CURATE_RESPONSE)},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 40, "completion_tokens": 60},
            })
        return httpx.Response(200, json={
            "choices": [{
                "message": {"content": "ok"}, "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        })

    bus, loader = await boot(
        dirs=[bundled_dir, str(agents_dir)],
        autostart_persistent=True,
    )

    llm_service = loader.get_handle("llm_service")
    llm_service._client = httpx.AsyncClient(
        transport=httpx.MockTransport(route),
    )
    llm_service._owned_client = True

    for name in ("memory_curator", "chatty"):
        assert bus.query_status(name) == "ready", f"{name} not ready"

    jsonl = tmp_path / "data" / "sessions" / "chatty.jsonl"
    assert jsonl.exists(), f"expected session jsonl at {jsonl}"
    _pad_session_jsonl(jsonl)

    curated: list[dict] = []
    bus.subscribe(
        "memory_curator.curated",
        lambda e: curated.append(e.get("payload") or {}),
    )

    await loader.stop("chatty", reason="e2e test")

    for _ in range(200):
        await asyncio.sleep(0.05)
        if curated:
            break
    assert curated, "memory_curator never emitted memory_curator.curated"

    drafts_dir = tmp_path / "data" / "memory" / "_drafts"
    assert drafts_dir.exists(), f"drafts dir missing: {drafts_dir}"
    drafts = sorted(drafts_dir.glob("curator_*.md"))
    assert drafts, f"no curator draft files under {drafts_dir}"

    draft_text = drafts[0].read_text(encoding="utf-8")
    assert "Topic C end-to-end fired successfully" in draft_text

    log_path = tmp_path / "data" / "memory" / "_improvement_log.md"
    assert log_path.exists()
    assert "e2e canary improvement" in log_path.read_text(encoding="utf-8")
