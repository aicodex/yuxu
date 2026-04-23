"""Tests for the creation-time principles loader."""
from __future__ import annotations

import pytest

from yuxu.core import principles

pytestmark = pytest.mark.asyncio


def test_extract_section_found():
    text = (
        "# Title\n\nintro\n\n"
        "## Foo\nfoo body\nfoo line 2\n\n"
        "## Bar\nbar body\n"
    )
    out = principles._extract_section(text, "Foo")
    assert out.startswith("## Foo")
    assert "foo body" in out
    assert "Bar" not in out


def test_extract_section_missing():
    text = "# Title\n\n## Foo\nfoo body\n"
    assert principles._extract_section(text, "NotThere") == ""


def test_extract_section_last_section_to_eof():
    text = "# Title\n\n## Foo\nfoo body\nmore body"
    out = principles._extract_section(text, "Foo")
    assert "more body" in out


def test_load_architecture_reads_real_doc():
    # Clear cache so this reads from disk (other tests may have populated it).
    principles._clear_cache()
    text = principles.load_architecture()
    # The installed doc must have the canonical identity line
    assert "Name:" in text and "yuxu" in text
    # And at least one invariant heading
    assert "I1." in text
    assert "Everything is an agent" in text


def test_load_architecture_cached():
    principles._clear_cache()
    first = principles.load_architecture()
    assert first  # non-empty
    second = principles.load_architecture()
    assert first is second  # identity, not just equality → cache hit


def test_load_guide_principles_reads_section():
    principles._clear_cache()
    text = principles.load_guide_principles()
    # The section header + at least one principle keyword should be present
    assert text.startswith("## Principles (read before creating)")
    assert "Dogfood" in text
    assert "Reuse before invent" in text


def test_load_creation_context_combines_both():
    principles._clear_cache()
    combined = principles.load_creation_context()
    assert "yuxu Architecture" in combined or "## Identity" in combined
    assert "Principles (read before creating)" in combined
    # Separator between arch and guide
    assert "\n---\n" in combined


def test_load_creation_context_empty_when_files_missing(monkeypatch, tmp_path):
    # Point the module paths at a non-existent location and clear cache.
    ghost = tmp_path / "nope.md"
    monkeypatch.setattr(principles, "_ARCH_PATH", ghost, raising=True)
    monkeypatch.setattr(principles, "_GUIDE_PATH", ghost, raising=True)
    principles._clear_cache()
    assert principles.load_creation_context() == ""


async def test_generate_agent_md_prompt_injects_principles():
    from yuxu.bundled.generate_agent_md.handler import _build_system_prompt

    principles._clear_cache()
    prompt = _build_system_prompt(
        name="sample_bot", description="a bot that does X",
        run_mode="one_shot", driver="python", scope="user",
        depends_on=[], extra_hints="",
    )
    # Original prompt still present
    assert "AGENT.md author" in prompt
    # Creation context appended
    assert "yuxu framework context" in prompt
    assert "Everything is an agent" in prompt
    # Operational principles section from AGENT_GUIDE
    assert "Dogfood drives" in prompt


async def test_classify_intent_prompt_injects_principles():
    from yuxu.bundled.classify_intent.handler import _system_prompt_with_context

    principles._clear_cache()
    prompt = _system_prompt_with_context()
    assert "agent-creation classifier" in prompt
    assert "yuxu framework context" in prompt
    assert "Everything is an agent" in prompt
    assert "Dogfood drives" in prompt
