"""Cross-skill helpers for project / agent scaffolding.

Lives at the bundled/ root so each scaffolding skill can import the
same template constants and manifest helpers without re-implementing them.
Loader.scan() skips files (and underscore-prefixed names) at the
scope root, so this module is invisible to the catalog.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import yaml


# -- frontmatter writer (shared between approval_applier + performance_ranker)

def dump_frontmatter(fm: dict) -> str:
    """Serialize a frontmatter dict to a `---`-bounded block matching the
    convention used by bundled memory writers:

    - JSON for dicts/lists (round-trippable through `yaml.safe_load`)
    - `true` / `false` for bools, `null` for None
    - plain repr for scalars
    - key order preserved from dict iteration

    Caller appends the entry body.
    """
    lines = ["---"]
    for k, v in fm.items():
        if isinstance(v, (dict, list)):
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif v is None:
            lines.append(f"{k}: null")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


# -- template text (used by create_project) ----------------------

PROJECT_GITIGNORE = """# yuxu project
_system/
.yuxu/
data/
config/secrets/
*.log

# Python
__pycache__/
*.py[cod]
.venv/
venv/
"""

DEFAULT_YUXU_JSON = {
    "name": "",
    "yuxu_version": "",
    "scan_order": ["_system", "agents"],
    "skills_dir": "skills",
    "data_dir": "data",
}

DEFAULT_RATE_LIMITS = """# Rate-limit pools for this project.
# See https://github.com/aicodex/yuxu for the schema.
#
# Example:
# minimax:
#   max_concurrent: 5
#   rpm: 60
#   accounts:
#     - id: key1
#       api_key: your-key-here
#       base_url: https://api.minimaxi.com/v1
"""

DEFAULT_SKILLS_ENABLED = """# Enabled global-scope skills for this project.
enabled: []
"""


# -- path helpers ------------------------------------------------


def bundled_source() -> Path:
    """Where shipped bundled agents live in the installed yuxu package."""
    import yuxu.bundled
    return Path(yuxu.bundled.__file__).parent


def templates_source() -> Path:
    """Where shipped templates live in the installed yuxu package."""
    import yuxu
    return Path(yuxu.__file__).parent / "templates"


def home_dir() -> Path:
    """~/.yuxu (or $YUXU_HOME). Mirrors cli.bootstrap.home_dir."""
    override = os.environ.get("YUXU_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".yuxu"


# -- registry helpers --------------------------------------------


def copy_bundled_into(into: Path) -> list[dict]:
    """Copy every bundled agent folder into `into/`. Returns a manifest entry per agent."""
    src = bundled_source()
    manifest: list[dict] = []
    into.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        dest = into / entry.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(entry, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        agent_md = dest / "AGENT.md"
        skill_md = dest / "SKILL.md"
        has_init = (dest / "__init__.py").exists()
        if has_init:
            kind, md_file = "agent", agent_md
        elif skill_md.exists():
            kind, md_file = "skill", skill_md
        else:
            kind, md_file = "agent", agent_md  # LLM-only agent
        sha = hashlib.sha256(md_file.read_bytes()).hexdigest()[:12] \
            if md_file.exists() else None
        manifest.append({"name": entry.name, "kind": kind, "md_sha12": sha})
    return manifest


def register_project_in_home(project_dir: Path) -> None:
    """Append project_dir to ~/.yuxu/projects.yaml, creating the file if needed."""
    home = home_dir()
    home.mkdir(parents=True, exist_ok=True)
    path = home / "projects.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() \
            else {"projects": []}
    except (yaml.YAMLError, FileNotFoundError):
        data = {"projects": []}
    if not isinstance(data, dict):
        data = {"projects": []}
    projects = data.get("projects") or []
    proj_str = str(project_dir.resolve())
    if proj_str not in projects:
        projects.append(proj_str)
    data["projects"] = projects
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")


def read_projects_yaml() -> list[str]:
    """Return raw project paths from ~/.yuxu/projects.yaml; empty if missing/garbled."""
    path = home_dir() / "projects.yaml"
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    paths = data.get("projects") or []
    return [str(p) for p in paths]


def hydrate_project_info(p: str) -> dict:
    """Inspect a registered project path and return its name/version/exists."""
    pp = Path(p)
    info: dict[str, Any] = {"path": str(pp), "exists": pp.exists()}
    cfg_path = pp / "yuxu.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            info["name"] = cfg.get("name")
            info["yuxu_version"] = cfg.get("yuxu_version")
        except json.JSONDecodeError:
            pass
    return info
