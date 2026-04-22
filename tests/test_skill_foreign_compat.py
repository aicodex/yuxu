"""Cross-ecosystem skill compatibility tests.

Precondition for a future skill_converter agent: yuxu's loader must read
OpenClaw- and Claude-Code-flavored SKILL.md files without data loss. We
don't require them to *execute* — execution is a separate layer — but
scanning, catalog, and `load()` should surface their metadata faithfully.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import yaml

from yuxu.bundled.skill_picker.registry import SkillRegistry, SkillScope


def _write_skill(root: Path, name: str, fm_body: str,
                 handler_filename: str | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(fm_body, encoding="utf-8")
    if handler_filename:
        (d / handler_filename).write_text("# handler\n", encoding="utf-8")
    return d


def _scope(root: Path, enable_file: Path, enabled: list[str]) -> SkillScope:
    enable_file.write_text(yaml.safe_dump({"enabled": enabled}),
                           encoding="utf-8")
    return SkillScope.global_scope(skills_root=root, enable_file=enable_file)


def test_openclaw_style_skill_loads_all_metadata(tmp_path):
    """OpenClaw SKILL.md has name/description/version/author/type/tags/
    homepage/license. yuxu should surface all of them."""
    _write_skill(tmp_path / "sk", "self-improving-agent", dedent("""\
        ---
        name: self-improving-agent
        description: Self-improving agent system that analyzes quality.
        version: "1.0.0"
        author: xiucheng
        type: skill
        tags: [self-improvement, learning, reflection]
        homepage: https://github.com/xiucheng/self-improving-agent
        license: MIT
        ---
        # Self-Improving Agent

        Some docs.
        """))
    reg = SkillRegistry()
    reg.scan([_scope(tmp_path / "sk",
                     tmp_path / "skills_enabled.yaml",
                     enabled=["self-improving-agent"])])
    spec = reg.resolve("self-improving-agent")
    assert spec is not None
    assert spec.version == "1.0.0"
    assert spec.author == "xiucheng"
    assert spec.license == "MIT"
    assert spec.tags == ["self-improvement", "learning", "reflection"]
    assert spec.homepage.startswith("https://")
    # triggers absent in OpenClaw → empty list, not crash
    assert spec.triggers == []


def test_openclaw_style_skill_catalog_surfaces_version_tags(tmp_path):
    _write_skill(tmp_path / "sk", "oc", dedent("""\
        ---
        name: oc
        description: d
        version: "2.0"
        tags: [t1, t2]
        ---
        body
        """))
    reg = SkillRegistry()
    reg.scan([_scope(tmp_path / "sk",
                     tmp_path / "skills_enabled.yaml",
                     enabled=["oc"])])
    cat = reg.catalog(only_enabled=True)
    assert cat[0]["version"] == "2.0"
    assert cat[0]["tags"] == ["t1", "t2"]


def test_cc_style_kebab_case_allowed_tools_read(tmp_path):
    """Claude Code uses kebab-case `allowed-tools`; we accept both forms."""
    _write_skill(tmp_path / "sk", "cc_kebab", dedent("""\
        ---
        name: cc_kebab
        description: cc skill
        version: "1.0.0"
        triggers: [show learnings]
        allowed-tools:
          - Bash
          - Read
          - Grep
        ---
        body
        """))
    reg = SkillRegistry()
    reg.scan([_scope(tmp_path / "sk",
                     tmp_path / "skills_enabled.yaml",
                     enabled=["cc_kebab"])])
    spec = reg.resolve("cc_kebab")
    assert spec.allowed_tools == ["Bash", "Read", "Grep"]


def test_snake_case_allowed_tools_also_read(tmp_path):
    """Snake-case `allowed_tools` (yuxu-native) should work identically."""
    _write_skill(tmp_path / "sk", "native", dedent("""\
        ---
        name: native
        description: d
        allowed_tools: [Bash, Write]
        ---
        body
        """))
    reg = SkillRegistry()
    reg.scan([_scope(tmp_path / "sk",
                     tmp_path / "skills_enabled.yaml",
                     enabled=["native"])])
    assert reg.resolve("native").allowed_tools == ["Bash", "Write"]


def test_cc_style_model_and_context_hints_preserved(tmp_path):
    _write_skill(tmp_path / "sk", "cc_hints", dedent("""\
        ---
        name: cc_hints
        description: d
        model: haiku
        context: fork
        ---
        body
        """))
    reg = SkillRegistry()
    reg.scan([_scope(tmp_path / "sk",
                     tmp_path / "skills_enabled.yaml",
                     enabled=["cc_hints"])])
    spec = reg.resolve("cc_hints")
    assert spec.model == "haiku"
    assert spec.skill_context == "fork"


def test_custom_handler_filename_via_frontmatter(tmp_path):
    """OpenClaw ships arbitrary .py filenames; yuxu accepts a `handler:`
    frontmatter key that overrides the default `handler.py`."""
    _write_skill(tmp_path / "sk", "custom", dedent("""\
        ---
        name: custom
        description: d
        handler: self_improving.py
        ---
        body
        """), handler_filename="self_improving.py")
    reg = SkillRegistry()
    reg.scan([_scope(tmp_path / "sk",
                     tmp_path / "skills_enabled.yaml",
                     enabled=["custom"])])
    spec = reg.resolve("custom")
    assert spec.handler_filename == "self_improving.py"
    assert spec.has_handler is True


def test_default_handler_filename_is_handler_py(tmp_path):
    _write_skill(tmp_path / "sk", "default", dedent("""\
        ---
        name: default
        description: d
        ---
        body
        """), handler_filename="handler.py")
    reg = SkillRegistry()
    reg.scan([_scope(tmp_path / "sk",
                     tmp_path / "skills_enabled.yaml",
                     enabled=["default"])])
    spec = reg.resolve("default")
    assert spec.handler_filename == "handler.py"
    assert spec.has_handler is True


def test_unknown_foreign_fields_preserved_in_frontmatter(tmp_path):
    """Fields we don't recognize should NOT crash the loader; they land
    in `spec.frontmatter` for a converter or a later yuxu version to use."""
    _write_skill(tmp_path / "sk", "exotic", dedent("""\
        ---
        name: exotic
        description: d
        preamble-tier: 2
        effort: medium
        sparkles: shine
        ---
        body
        """))
    reg = SkillRegistry()
    reg.scan([_scope(tmp_path / "sk",
                     tmp_path / "skills_enabled.yaml",
                     enabled=["exotic"])])
    spec = reg.resolve("exotic")
    assert spec is not None
    # Kebab-case + unknown fields preserved verbatim
    assert spec.frontmatter.get("preamble-tier") == 2
    assert spec.frontmatter.get("effort") == "medium"
    assert spec.frontmatter.get("sparkles") == "shine"


def test_load_returns_all_new_fields(tmp_path):
    _write_skill(tmp_path / "sk", "full", dedent("""\
        ---
        name: full
        description: every field
        version: "3.0"
        author: tester
        license: Apache-2.0
        tags: [a, b]
        homepage: https://example.com
        allowed-tools: [Bash]
        model: sonnet
        context: inline
        handler: foo.py
        ---
        body
        """), handler_filename="foo.py")
    reg = SkillRegistry()
    reg.scan([_scope(tmp_path / "sk",
                     tmp_path / "skills_enabled.yaml",
                     enabled=["full"])])
    loaded = reg.load("full")
    assert loaded["version"] == "3.0"
    assert loaded["author"] == "tester"
    assert loaded["license"] == "Apache-2.0"
    assert loaded["tags"] == ["a", "b"]
    assert loaded["homepage"] == "https://example.com"
    assert loaded["allowed_tools"] == ["Bash"]
    assert loaded["model"] == "sonnet"
    assert loaded["context"] == "inline"
    assert loaded["handler_filename"] == "foo.py"
    assert loaded["has_handler"] is True
