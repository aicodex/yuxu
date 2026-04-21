from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from yuxu.bundled.skill_picker.registry import (
    SkillRegistry,
    SkillScope,
    default_scopes,
)

pytestmark = pytest.mark.asyncio


def _write_skill(root: Path, name: str, *, fm: dict, body: str = "",
                 with_handler: bool = False) -> Path:
    d = root / name
    d.mkdir(parents=True)
    fm_text = yaml.safe_dump(fm, sort_keys=False).strip()
    (d / "SKILL.md").write_text(f"---\n{fm_text}\n---\n{body}\n")
    if with_handler:
        (d / "handler.py").write_text("async def execute(i, c): return None\n")
    return d


def _write_enabled(path: Path, names: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"enabled": names}))


# -- SkillScope validation --------------------------------------

async def test_scope_validation():
    with pytest.raises(ValueError):
        SkillScope(skills_root=Path("/x"), enable_file=Path("/x.yaml"),
                   scope="sideways")
    with pytest.raises(ValueError):
        SkillScope(skills_root=Path("/x"), enable_file=Path("/x.yaml"),
                   scope="global", owner="x")  # global can't have owner
    with pytest.raises(ValueError):
        SkillScope(skills_root=Path("/x"), enable_file=Path("/x.yaml"),
                   scope="agent")  # agent must have owner


async def test_scope_constructors(tmp_path):
    g = SkillScope.global_scope(tmp_path / "bundled", tmp_path / "enabled.yaml")
    assert g.scope == "global" and g.owner is None

    p = SkillScope.project(tmp_path / "proj_x", "proj_x")
    assert p.scope == "project" and p.owner == "proj_x"
    assert p.skills_root == tmp_path / "proj_x" / "skills"
    assert p.enable_file == tmp_path / "proj_x" / "skills_enabled.yaml"

    a = SkillScope.agent(tmp_path / "agt_y", "agt_y")
    assert a.scope == "agent" and a.owner == "agt_y"
    assert a.skills_root == tmp_path / "agt_y" / "skills"
    assert a.enable_file == tmp_path / "agt_y" / "skills_enabled.yaml"


# -- scan / install semantics -----------------------------------

async def test_scan_empty(tmp_path):
    reg = SkillRegistry()
    reg.scan([SkillScope.global_scope(tmp_path / "b", tmp_path / "e.yaml")])
    assert reg.skills == {}


async def test_scan_finds_installed_but_disabled(tmp_path):
    root = tmp_path / "bundled"
    _write_skill(root, "alpha", fm={"description": "A"})
    reg = SkillRegistry()
    reg.scan([SkillScope.global_scope(root, tmp_path / "enabled.yaml")])
    spec = reg.skills[("global", None, "alpha")]
    assert spec.enabled is False
    # Not in default (enabled-only) catalog
    assert reg.catalog() == []
    # Visible when only_enabled=False
    assert len(reg.catalog(only_enabled=False)) == 1


async def test_enabled_list_marks_enabled(tmp_path):
    root = tmp_path / "bundled"
    enabled = tmp_path / "enabled.yaml"
    _write_skill(root, "a", fm={"description": "A"})
    _write_skill(root, "b", fm={"description": "B"})
    _write_enabled(enabled, ["a"])

    reg = SkillRegistry()
    reg.scan([SkillScope.global_scope(root, enabled)])
    assert reg.skills[("global", None, "a")].enabled is True
    assert reg.skills[("global", None, "b")].enabled is False
    names = [c["name"] for c in reg.catalog()]
    assert names == ["a"]


async def test_enabled_as_bare_list(tmp_path):
    # Accept either {enabled: [...]} or plain [...]
    root = tmp_path / "b"
    enabled = tmp_path / "e.yaml"
    _write_skill(root, "z", fm={"description": "z"})
    enabled.write_text(yaml.safe_dump(["z"]))
    reg = SkillRegistry()
    reg.scan([SkillScope.global_scope(root, enabled)])
    assert reg.is_enabled("z", scope="global")


async def test_enabled_file_malformed_is_lenient(tmp_path):
    root = tmp_path / "b"
    enabled = tmp_path / "e.yaml"
    _write_skill(root, "z", fm={"description": "z"})
    enabled.write_text("::: not yaml :::\n  - broken")
    reg = SkillRegistry()
    reg.scan([SkillScope.global_scope(root, enabled)])
    assert reg.is_enabled("z", scope="global") is False


# -- enable / disable -------------------------------------------

async def test_enable_writes_file(tmp_path):
    root = tmp_path / "b"
    enabled = tmp_path / "e.yaml"
    _write_skill(root, "foo", fm={"description": "F"})
    reg = SkillRegistry()
    sc = SkillScope.global_scope(root, enabled)
    reg.scan([sc])

    reg.enable("foo", scope="global")
    assert reg.is_enabled("foo", scope="global")
    assert enabled.exists()
    data = yaml.safe_load(enabled.read_text())
    assert data["enabled"] == ["foo"]


async def test_enable_unknown_skill_raises(tmp_path):
    root = tmp_path / "b"
    reg = SkillRegistry()
    reg.scan([SkillScope.global_scope(root, tmp_path / "e.yaml")])
    with pytest.raises(KeyError):
        reg.enable("ghost", scope="global")


async def test_enable_unregistered_scope_raises(tmp_path):
    reg = SkillRegistry()
    with pytest.raises(KeyError):
        reg.enable("foo", scope="global")


async def test_disable_removes_from_file(tmp_path):
    root = tmp_path / "b"
    enabled = tmp_path / "e.yaml"
    _write_skill(root, "foo", fm={"description": "F"})
    _write_skill(root, "bar", fm={"description": "B"})
    _write_enabled(enabled, ["foo", "bar"])

    reg = SkillRegistry()
    reg.scan([SkillScope.global_scope(root, enabled)])
    reg.disable("foo", scope="global")
    data = yaml.safe_load(enabled.read_text())
    assert data["enabled"] == ["bar"]
    assert reg.is_enabled("foo", scope="global") is False


async def test_enable_persists_across_rescan(tmp_path):
    root = tmp_path / "b"
    enabled = tmp_path / "e.yaml"
    _write_skill(root, "x", fm={"description": "x"})
    reg1 = SkillRegistry()
    sc1 = SkillScope.global_scope(root, enabled)
    reg1.scan([sc1])
    reg1.enable("x", scope="global")

    reg2 = SkillRegistry()
    reg2.scan([SkillScope.global_scope(root, enabled)])
    assert reg2.is_enabled("x", scope="global")


# -- visibility --------------------------------------------------

async def test_agent_cannot_see_other_agent_skills(tmp_path):
    # two agent scopes: alice and bob
    a_root = tmp_path / "alice"
    b_root = tmp_path / "bob"
    _write_skill(a_root / "skills", "alice_private", fm={"description": "A"})
    _write_skill(b_root / "skills", "bob_private", fm={"description": "B"})
    _write_enabled(a_root / "skills_enabled.yaml", ["alice_private"])
    _write_enabled(b_root / "skills_enabled.yaml", ["bob_private"])

    reg = SkillRegistry()
    reg.scan([
        SkillScope.agent(a_root, "alice"),
        SkillScope.agent(b_root, "bob"),
    ])
    assert [c["name"] for c in reg.catalog(for_agent="alice")] == ["alice_private"]
    assert [c["name"] for c in reg.catalog(for_agent="bob")] == ["bob_private"]
    # stranger sees nothing from agent scope
    assert reg.catalog(for_agent="stranger") == []


async def test_project_visibility(tmp_path):
    p1 = tmp_path / "proj1"
    p2 = tmp_path / "proj2"
    _write_skill(p1 / "skills", "s1", fm={"description": "p1 skill"})
    _write_skill(p2 / "skills", "s2", fm={"description": "p2 skill"})
    _write_enabled(p1 / "skills_enabled.yaml", ["s1"])
    _write_enabled(p2 / "skills_enabled.yaml", ["s2"])

    reg = SkillRegistry()
    reg.scan([
        SkillScope.project(p1, "proj1"),
        SkillScope.project(p2, "proj2"),
    ])
    assert [c["name"] for c in reg.catalog(for_project="proj1")] == ["s1"]
    assert [c["name"] for c in reg.catalog(for_project="proj2")] == ["s2"]


async def test_global_visible_to_everyone(tmp_path):
    root = tmp_path / "g"
    _write_skill(root, "global_skill", fm={"description": "g"})
    _write_enabled(tmp_path / "enabled.yaml", ["global_skill"])

    reg = SkillRegistry()
    reg.scan([SkillScope.global_scope(root, tmp_path / "enabled.yaml")])
    # Everyone sees global
    for ident in [{}, {"for_agent": "x"}, {"for_project": "p"}]:
        assert [c["name"] for c in reg.catalog(**ident)] == ["global_skill"]


async def test_precedence_agent_over_project_over_global(tmp_path):
    g_root = tmp_path / "g"
    p_root = tmp_path / "p"
    a_root = tmp_path / "a"
    _write_skill(g_root, "echo", fm={"description": "global"})
    _write_skill(p_root / "skills", "echo", fm={"description": "project"})
    _write_skill(a_root / "skills", "echo", fm={"description": "agent"})
    _write_enabled(tmp_path / "ge.yaml", ["echo"])
    _write_enabled(p_root / "skills_enabled.yaml", ["echo"])
    _write_enabled(a_root / "skills_enabled.yaml", ["echo"])

    reg = SkillRegistry()
    reg.scan([
        SkillScope.global_scope(g_root, tmp_path / "ge.yaml"),
        SkillScope.project(p_root, "p1"),
        SkillScope.agent(a_root, "a1"),
    ])
    cat = reg.catalog(for_agent="a1", for_project="p1")
    assert len(cat) == 1
    assert cat[0]["description"] == "agent"

    # Without agent identity, project wins
    cat = reg.catalog(for_project="p1")
    assert cat[0]["description"] == "project"

    # Without project/agent identity, global wins
    cat = reg.catalog()
    assert cat[0]["description"] == "global"


async def test_resolve_picks_highest_visible(tmp_path):
    g_root = tmp_path / "g"
    a_root = tmp_path / "a"
    _write_skill(g_root, "x", fm={"description": "g"})
    _write_skill(a_root / "skills", "x", fm={"description": "a"})
    _write_enabled(tmp_path / "ge.yaml", ["x"])
    _write_enabled(a_root / "skills_enabled.yaml", ["x"])

    reg = SkillRegistry()
    reg.scan([
        SkillScope.global_scope(g_root, tmp_path / "ge.yaml"),
        SkillScope.agent(a_root, "a1"),
    ])
    # as agent a1 → agent version
    spec = reg.resolve("x", for_agent="a1")
    assert spec.scope == "agent"
    # as other agent → global fallback
    spec = reg.resolve("x", for_agent="someone_else")
    assert spec.scope == "global"


# -- load -------------------------------------------------------

async def test_load_body_lazy(tmp_path):
    root = tmp_path / "b"
    body = "## When to use\nWhen you need x."
    _write_skill(root, "foo", fm={"description": "F"}, body=body, with_handler=True)
    _write_enabled(tmp_path / "e.yaml", ["foo"])

    reg = SkillRegistry()
    reg.scan([SkillScope.global_scope(root, tmp_path / "e.yaml")])

    # Catalog does not carry body
    assert "body" not in reg.catalog()[0]
    # Load does
    r = reg.load("foo")
    assert r["body"].startswith("## When to use")
    assert r["has_handler"] is True


async def test_load_disabled_by_default_raises(tmp_path):
    root = tmp_path / "b"
    _write_skill(root, "foo", fm={"description": "F"})
    reg = SkillRegistry()
    reg.scan([SkillScope.global_scope(root, tmp_path / "e.yaml")])
    # installed but not enabled
    with pytest.raises(KeyError):
        reg.load("foo")
    # Bypass for admin tools
    assert reg.load("foo", only_enabled=False)["enabled"] is False


async def test_load_invisible_raises(tmp_path):
    a_root = tmp_path / "a"
    _write_skill(a_root / "skills", "secret", fm={"description": "s"})
    _write_enabled(a_root / "skills_enabled.yaml", ["secret"])
    reg = SkillRegistry()
    reg.scan([SkillScope.agent(a_root, "alice")])
    # Loading as bob → invisible
    with pytest.raises(KeyError):
        reg.load("secret", for_agent="bob")
    # As alice → OK
    assert reg.load("secret", for_agent="alice")["name"] == "secret"


# -- triggers / admin -------------------------------------------

async def test_triggers_filter(tmp_path):
    root = tmp_path / "b"
    _write_skill(root, "a", fm={"description": "x", "triggers": ["price"]})
    _write_skill(root, "b", fm={"description": "x", "triggers": ["news"]})
    _write_enabled(tmp_path / "e.yaml", ["a", "b"])
    reg = SkillRegistry()
    reg.scan([SkillScope.global_scope(root, tmp_path / "e.yaml")])
    names = [c["name"] for c in reg.catalog(triggers_any=["price"])]
    assert names == ["a"]


async def test_list_all_admin_view(tmp_path):
    g_root = tmp_path / "g"
    a_root = tmp_path / "a"
    _write_skill(g_root, "gx", fm={"description": "x"})
    _write_skill(a_root / "skills", "ax", fm={"description": "y"})
    _write_enabled(a_root / "skills_enabled.yaml", ["ax"])
    reg = SkillRegistry()
    reg.scan([
        SkillScope.global_scope(g_root, tmp_path / "ge.yaml"),
        SkillScope.agent(a_root, "alice"),
    ])
    out = {item["name"]: item for item in reg.list_all()}
    assert out["gx"]["enabled"] is False
    assert out["ax"]["enabled"] is True
    assert out["ax"]["owner"] == "alice"


async def test_default_scopes_order(tmp_path):
    scopes = default_scopes(
        bundled_root=tmp_path / "b",
        global_enable_file=tmp_path / "e.yaml",
        projects=[(tmp_path / "p1", "p1")],
        agents=[(tmp_path / "a1", "a1")],
    )
    assert scopes[0].scope == "global"
    assert scopes[1].scope == "project" and scopes[1].owner == "p1"
    assert scopes[2].scope == "agent" and scopes[2].owner == "a1"
