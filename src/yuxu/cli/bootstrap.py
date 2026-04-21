"""First-run ~/.yuxu/ bootstrap.

On any CLI invocation, ensure `~/.yuxu/` exists with a sensible default
layout. If it doesn't, create it and print a one-time welcome note.

Layout:
    ~/.yuxu/
    ├── config.yaml      global user config (api keys, defaults, token plan hint)
    ├── logs/            yuxu serve logs
    ├── projects.yaml    registry of projects this user has created (maintained by CLI)
    └── README.md        explains what lives here
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

YUXU_HOME_ENV = "YUXU_HOME"

README_BODY = """# ~/.yuxu

User-global state for yuxu. Created automatically on first run.

## Files

- `config.yaml` — global defaults (LLM keys, preferred model, token plan).
- `projects.yaml` — registry of projects you've created with `yuxu init`.
- `logs/` — daemon logs from `yuxu serve` (when not running inside a project).

Anything project-specific lives in **that project's directory**, not here.
"""

DEFAULT_CONFIG_YAML = """# yuxu global config
# Project-specific config lives in each project's yuxu.json, not here.

# LLM defaults used across projects (can be overridden per-project).
# llm:
#   default_pool: minimax
#   default_model: abab6.5s-chat

# Token plan (for token_budget agent, once it exists).
# token_plan:
#   monthly_tokens: 10000000
#   reset_day: 1

# Logging level for `yuxu serve`.
log_level: INFO
"""

DEFAULT_PROJECTS_YAML = """# Projects registry (updated by `yuxu init`).
projects: []
"""


def home_dir() -> Path:
    """Where ~/.yuxu lives. Overridable via YUXU_HOME env var (for tests/containers)."""
    import os
    override = os.environ.get(YUXU_HOME_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".yuxu"


def ensure_home(verbose: bool = True) -> tuple[Path, bool]:
    """Create ~/.yuxu/ with defaults on first run.

    Returns (home_path, created_now). `created_now` is True only on the very
    first run when the directory did not exist.
    """
    home = home_dir()
    if home.exists():
        return home, False

    home.mkdir(parents=True)
    (home / "logs").mkdir()
    (home / "README.md").write_text(README_BODY, encoding="utf-8")
    (home / "config.yaml").write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    (home / "projects.yaml").write_text(DEFAULT_PROJECTS_YAML, encoding="utf-8")

    if verbose:
        print(f"[yuxu] Initialized user home at {home}")
        print(f"[yuxu] See {home / 'README.md'} for what lives there.")
    return home, True


def register_project(project_dir: Path) -> None:
    """Append a project path to ~/.yuxu/projects.yaml (best effort)."""
    import yaml
    home, _ = ensure_home(verbose=False)
    path = home / "projects.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {"projects": []}
    except yaml.YAMLError:
        data = {"projects": []}
    projects = data.get("projects") or []
    proj_str = str(project_dir.resolve())
    if proj_str not in projects:
        projects.append(proj_str)
    data["projects"] = projects
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
