"""Skill discovery, enable state, and scoped catalog.

This module lives under `skill_picker/` because skills are NOT part of the
core kernel contract — skill loading is a specialized agent capability. It
happens to be pure file-IO (no bus, no asyncio), so it's a plain library the
skill_picker agent uses.

See `docs/CORE_INTERFACE.md` for what belongs in `src/core/` (and why this
doesn't).

Design (OpenClaw-flavored):

- **Install** = a skill folder exists under one of the three scope roots
- **Enable**  = skill name is listed in that scope's `skills_enabled.yaml`
- **Visibility** = determined by caller identity, not a property of the skill

Three scopes & precedence (narrower wins on same name):

| Scope    | Skills root                                    | Enable file (sibling)                         |
|----------|-----------------------------------------------|-----------------------------------------------|
| global   | `src/skills_bundled/`                         | `config/skills_enabled.yaml`                  |
| project  | `data/projects/{project}/skills/`             | `data/projects/{project}/skills_enabled.yaml` |
| agent    | `{agent_dir}/skills/`                         | `{agent_dir}/skills_enabled.yaml`             |

Catalog visibility for a caller (agent X, project P):
- all enabled global skills
- enabled project skills where owner == P
- enabled agent skills where owner == X
- NEVER other agents' private skills or other projects'
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml

from yuxu.core.frontmatter import parse_frontmatter

log = logging.getLogger(__name__)

VALID_SCOPES = ("global", "project", "agent")
_PRECEDENCE = {"global": 0, "project": 1, "agent": 2}


def installed_skills_bundled_root() -> Path:
    """Where shipped skills live in the installed yuxu package.

    Used as the default for global-scope discovery so an editable-install or
    pip-installed yuxu finds its bundled skills regardless of caller cwd.
    """
    import yuxu
    return Path(yuxu.__file__).parent / "skills_bundled"


@dataclass
class SkillScope:
    """A (skills_root, enable_file, scope, owner) bundle.

    Use `global_scope()` / `project()` / `agent()` constructors for the
    standard layout; construct directly for non-standard arrangements.
    """
    skills_root: Path
    enable_file: Path
    scope: str
    owner: Optional[str] = None

    def __post_init__(self) -> None:
        if self.scope not in VALID_SCOPES:
            raise ValueError(f"invalid scope: {self.scope!r}")
        if self.scope == "global" and self.owner is not None:
            raise ValueError("global scope cannot have an owner")
        if self.scope != "global" and not self.owner:
            raise ValueError(f"{self.scope} scope requires an owner")

    @classmethod
    def global_scope(cls,
                     skills_root: Path | str | None = None,
                     enable_file: Path | str = "config/skills_enabled.yaml") -> "SkillScope":
        """Default skills_root is the installed yuxu package's
        `skills_bundled/` directory; pass an explicit path to point elsewhere
        (e.g. tests, alternate distributions)."""
        return cls(
            skills_root=Path(skills_root) if skills_root is not None
                        else installed_skills_bundled_root(),
            enable_file=Path(enable_file),
            scope="global",
            owner=None,
        )

    @classmethod
    def project(cls, project_dir: Path | str, project_id: str) -> "SkillScope":
        pd = Path(project_dir)
        return cls(
            skills_root=pd / "skills",
            enable_file=pd / "skills_enabled.yaml",
            scope="project",
            owner=project_id,
        )

    @classmethod
    def agent(cls, agent_dir: Path | str, agent_name: str) -> "SkillScope":
        ad = Path(agent_dir)
        return cls(
            skills_root=ad / "skills",
            enable_file=ad / "skills_enabled.yaml",
            scope="agent",
            owner=agent_name,
        )

    def read_enabled(self) -> set[str]:
        if not self.enable_file.exists():
            return set()
        try:
            data = yaml.safe_load(self.enable_file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            log.warning("skills: bad YAML in %s, treating as empty", self.enable_file)
            return set()
        if isinstance(data, list):
            names = data
        elif isinstance(data, dict):
            names = data.get("enabled", [])
        else:
            names = []
        if not isinstance(names, list):
            log.warning("skills: %s 'enabled' is not a list, ignoring", self.enable_file)
            return set()
        return {str(n) for n in names}

    def write_enabled(self, names: Iterable[str]) -> None:
        self.enable_file.parent.mkdir(parents=True, exist_ok=True)
        data = {"enabled": sorted(set(names))}
        self.enable_file.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )


@dataclass
class SkillSpec:
    name: str
    path: Path
    scope: str
    owner: Optional[str]
    description: str = ""
    triggers: list[str] = field(default_factory=list)
    parameters: Optional[dict] = None
    depends_on: list[str] = field(default_factory=list)
    rate_limit_pool: Optional[str] = None
    edit_warning: bool = False
    # Cross-ecosystem compatibility fields — not used by yuxu's own execution
    # path but preserved so a future converter agent can round-trip skills
    # from OpenClaw / Claude Code and yuxu agents can surface metadata.
    version: Optional[str] = None
    author: Optional[str] = None
    license: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    homepage: Optional[str] = None
    handler_filename: str = "handler.py"   # override via frontmatter `handler`
    allowed_tools: list[str] = field(default_factory=list)  # CC compat
    model: Optional[str] = None            # CC compat (sonnet/haiku hint)
    skill_context: Optional[str] = None    # CC compat: "inline" | "fork"
    frontmatter: dict = field(default_factory=dict)
    enabled: bool = False

    @property
    def has_handler(self) -> bool:
        return (self.path / self.handler_filename).exists()

    def read_body(self) -> str:
        md = self.path / "SKILL.md"
        if not md.exists():
            return ""
        _, body = parse_frontmatter(md.read_text(encoding="utf-8"))
        return body.strip("\n")


class SkillRegistry:
    """In-memory index of installed skills across all known scopes."""

    def __init__(self) -> None:
        # Key = (scope, owner, name). Same-name skills across scopes coexist.
        self.skills: dict[tuple[str, Optional[str], str], SkillSpec] = {}
        self.scopes: dict[tuple[str, Optional[str]], SkillScope] = {}

    # -- discovery --------------------------------------------------

    def scan(self, scopes: Iterable[SkillScope]) -> None:
        self.skills.clear()
        self.scopes.clear()
        for sc in scopes:
            self.scopes[(sc.scope, sc.owner)] = sc
            enabled_names = sc.read_enabled()
            if not sc.skills_root.exists():
                continue
            for skill_dir in sorted(sc.skills_root.iterdir()):
                if not skill_dir.is_dir() or skill_dir.name.startswith((".", "_")):
                    continue
                spec = self._load_spec(skill_dir, sc)
                if spec is None:
                    continue
                spec.enabled = spec.name in enabled_names
                self.skills[(sc.scope, sc.owner, spec.name)] = spec

    def _load_spec(self, skill_dir: Path, sc: SkillScope) -> Optional[SkillSpec]:
        md = skill_dir / "SKILL.md"
        if not md.exists():
            log.warning("skills: %s has no SKILL.md, skipping", skill_dir)
            return None
        fm, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
        fm_name = fm.get("name")
        if fm_name and fm_name != skill_dir.name:
            log.warning("skills: %s SKILL.md name=%r disagrees with folder; using folder",
                        skill_dir, fm_name)
        description = str(fm.get("description") or "").strip()
        if not description:
            log.warning("skills: %s/%s has empty description", sc.scope, skill_dir.name)
        # Cross-ecosystem field reads: accept both snake_case and kebab-case
        # (CC uses kebab for `allowed-tools`; OpenClaw/yuxu snake). The later
        # form wins if both appear.
        def _get(k_snake: str, k_kebab: Optional[str] = None,
                 default=None):
            if k_kebab and k_kebab in fm:
                return fm[k_kebab]
            return fm.get(k_snake, default)

        return SkillSpec(
            name=skill_dir.name,
            path=skill_dir,
            scope=sc.scope,
            owner=sc.owner,
            description=description,
            triggers=list(fm.get("triggers") or []),
            parameters=fm.get("parameters"),
            depends_on=list(fm.get("depends_on") or []),
            rate_limit_pool=fm.get("rate_limit_pool"),
            edit_warning=bool(fm.get("edit_warning", False)),
            version=fm.get("version"),
            author=fm.get("author"),
            license=fm.get("license"),
            tags=list(fm.get("tags") or []),
            homepage=fm.get("homepage"),
            handler_filename=str(fm.get("handler") or "handler.py"),
            allowed_tools=list(_get("allowed_tools", "allowed-tools") or []),
            model=fm.get("model"),
            skill_context=fm.get("context"),
            frontmatter=fm,
        )

    # -- visibility & catalog ---------------------------------------

    def _visible_candidates(self, name: Optional[str],
                            for_agent: Optional[str],
                            for_project: Optional[str]) -> list[SkillSpec]:
        out: list[SkillSpec] = []
        for (scope, owner, n), spec in self.skills.items():
            if name is not None and n != name:
                continue
            if scope == "agent" and owner != for_agent:
                continue
            if scope == "project" and owner != for_project:
                continue
            out.append(spec)
        return out

    def catalog(self, *,
                for_agent: Optional[str] = None,
                for_project: Optional[str] = None,
                only_enabled: bool = True,
                triggers_any: Optional[Iterable[str]] = None) -> list[dict]:
        """Return entries the caller can see. Narrower scope wins on name clash."""
        trig_filter = set(triggers_any) if triggers_any else None
        by_name: dict[str, tuple[int, SkillSpec]] = {}
        for spec in self._visible_candidates(None, for_agent, for_project):
            if only_enabled and not spec.enabled:
                continue
            if trig_filter and not (trig_filter & set(spec.triggers)):
                continue
            prec = _PRECEDENCE[spec.scope]
            existing = by_name.get(spec.name)
            if existing is None or prec > existing[0]:
                by_name[spec.name] = (prec, spec)
        return [
            {
                "name": s.name,
                "description": s.description,
                "scope": s.scope,
                "owner": s.owner,
                "triggers": list(s.triggers),
                "has_handler": s.has_handler,
                "enabled": s.enabled,
                "version": s.version,
                "tags": list(s.tags),
            }
            for _, s in sorted(by_name.values(), key=lambda x: x[1].name)
        ]

    def resolve(self, name: str, *,
                for_agent: Optional[str] = None,
                for_project: Optional[str] = None,
                only_enabled: bool = True) -> Optional[SkillSpec]:
        """Pick the highest-precedence visible spec by name."""
        best: Optional[tuple[int, SkillSpec]] = None
        for spec in self._visible_candidates(name, for_agent, for_project):
            if only_enabled and not spec.enabled:
                continue
            prec = _PRECEDENCE[spec.scope]
            if best is None or prec > best[0]:
                best = (prec, spec)
        return best[1] if best else None

    def load(self, name: str, *,
             for_agent: Optional[str] = None,
             for_project: Optional[str] = None,
             only_enabled: bool = True) -> dict:
        spec = self.resolve(name, for_agent=for_agent, for_project=for_project,
                            only_enabled=only_enabled)
        if spec is None:
            raise KeyError(f"skill {name!r} not visible or not enabled")
        return {
            "name": spec.name,
            "description": spec.description,
            "scope": spec.scope,
            "owner": spec.owner,
            "enabled": spec.enabled,
            "triggers": list(spec.triggers),
            "parameters": spec.parameters,
            "depends_on": list(spec.depends_on),
            "rate_limit_pool": spec.rate_limit_pool,
            "edit_warning": spec.edit_warning,
            "version": spec.version,
            "author": spec.author,
            "license": spec.license,
            "tags": list(spec.tags),
            "homepage": spec.homepage,
            "handler_filename": spec.handler_filename,
            "allowed_tools": list(spec.allowed_tools),
            "model": spec.model,
            "context": spec.skill_context,
            "path": str(spec.path),
            "has_handler": spec.has_handler,
            "body": spec.read_body(),
            "frontmatter": dict(spec.frontmatter),
        }

    # -- enable / disable -------------------------------------------

    def _get_scope(self, scope: str, owner: Optional[str]) -> SkillScope:
        sc = self.scopes.get((scope, owner))
        if sc is None:
            raise KeyError(f"scope not registered: {scope}/{owner}")
        return sc

    def enable(self, name: str, *, scope: str, owner: Optional[str] = None) -> None:
        sc = self._get_scope(scope, owner)
        key = (scope, owner, name)
        if key not in self.skills:
            raise KeyError(
                f"skill {name!r} not installed in {scope}/{owner or '*'}; "
                "drop the folder first"
            )
        current = sc.read_enabled()
        current.add(name)
        sc.write_enabled(current)
        self.skills[key].enabled = True

    def disable(self, name: str, *, scope: str, owner: Optional[str] = None) -> None:
        sc = self._get_scope(scope, owner)
        current = sc.read_enabled()
        current.discard(name)
        sc.write_enabled(current)
        key = (scope, owner, name)
        if key in self.skills:
            self.skills[key].enabled = False

    def is_enabled(self, name: str, *, scope: str, owner: Optional[str] = None) -> bool:
        spec = self.skills.get((scope, owner, name))
        return bool(spec and spec.enabled)

    # -- introspection ----------------------------------------------

    def list_all(self) -> list[dict]:
        """Admin view: every installed skill regardless of scope/enabled.

        Includes `path`, `handler_filename`, `frontmatter` so runtime
        components (e.g. skill_executor) can import / render without making
        a second round-trip per skill via `load`."""
        out = []
        for (scope, owner, name), spec in sorted(self.skills.items()):
            out.append({
                "name": name,
                "scope": scope,
                "owner": owner,
                "enabled": spec.enabled,
                "description": spec.description,
                "has_handler": spec.has_handler,
                "path": str(spec.path),
                "handler_filename": spec.handler_filename,
                "version": spec.version,
                "frontmatter": dict(spec.frontmatter),
            })
        return out


def default_scopes(*,
                   bundled_root: Path | str | None = None,
                   global_enable_file: Path | str = "config/skills_enabled.yaml",
                   projects: Optional[Iterable[tuple[Path | str, str]]] = None,
                   agents: Optional[Iterable[tuple[Path | str, str]]] = None,
                   ) -> list[SkillScope]:
    """Build the standard SkillScope list.

    `bundled_root` defaults to the installed yuxu package's `skills_bundled/`.
    `projects` items are `(project_dir, project_id)`;
    `agents` items are `(agent_dir, agent_name)`.
    """
    scopes: list[SkillScope] = [
        SkillScope.global_scope(bundled_root, global_enable_file),
    ]
    for pdir, pid in projects or []:
        scopes.append(SkillScope.project(pdir, pid))
    for adir, aname in agents or []:
        scopes.append(SkillScope.agent(adir, aname))
    return scopes
