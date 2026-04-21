"""`yuxu init <dir>` — scaffold a project directory.

Layout created:
    <dir>/
    ├── yuxu.json                 project config
    ├── agents/                   user agents (private to this project)
    │   └── .gitkeep
    ├── skills/                   project-level skills
    │   └── .gitkeep
    ├── _system/                  extracted bundled agents (gitignore)
    │   └── {each bundled agent}/
    ├── config/
    │   ├── rate_limits.yaml
    │   └── skills_enabled.yaml
    ├── data/{checkpoints,logs,memory,sessions}/
    ├── .yuxu/                    internal metadata (gitignore)
    │   ├── version
    │   └── manifest.json         {extracted agents + their sha}
    └── .gitignore
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Iterable

from .bootstrap import register_project


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
    "name": "",            # filled in at init time
    "yuxu_version": "",    # filled in at init time
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
# See yuxu.bundled.skill_picker for details.
enabled: []
"""


def _bundled_source() -> Path:
    """Locate the installed yuxu.bundled directory."""
    import yuxu.bundled
    return Path(yuxu.bundled.__file__).parent


def _copy_bundled(into: Path) -> list[dict]:
    """Copy every bundled agent folder into `into/`. Returns a manifest."""
    src = _bundled_source()
    manifest: list[dict] = []
    for entry in sorted(src.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        dest = into / entry.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(entry, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # sha over AGENT.md if present (coarse change detection for `yuxu upgrade`)
        agent_md = dest / "AGENT.md"
        sha = None
        if agent_md.exists():
            sha = hashlib.sha256(agent_md.read_bytes()).hexdigest()[:12]
        manifest.append({"name": entry.name, "agent_md_sha12": sha})
    return manifest


def init_project(target: Path, *, force: bool = False) -> Path:
    """Create the project scaffold at `target`.

    Returns the resolved absolute path. Raises FileExistsError if target
    already contains a yuxu.json and `force=False`.
    """
    from .. import __version__ as ver

    target = target.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)

    yuxu_json = target / "yuxu.json"
    if yuxu_json.exists() and not force:
        raise FileExistsError(
            f"{yuxu_json} already exists. Use --force to overwrite."
        )

    # Standard directories
    for rel in ["agents", "skills", "_system", "config",
                "data/checkpoints", "data/logs", "data/memory", "data/sessions",
                ".yuxu"]:
        (target / rel).mkdir(parents=True, exist_ok=True)

    # Placeholder keepers so empty dirs survive git
    for rel in ["agents/.gitkeep", "skills/.gitkeep"]:
        p = target / rel
        if not p.exists():
            p.write_text("")

    # Project config
    cfg = dict(DEFAULT_YUXU_JSON)
    cfg["name"] = target.name
    cfg["yuxu_version"] = ver
    yuxu_json.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")

    # User-editable config files
    rl = target / "config" / "rate_limits.yaml"
    if not rl.exists():
        rl.write_text(DEFAULT_RATE_LIMITS, encoding="utf-8")
    se = target / "config" / "skills_enabled.yaml"
    if not se.exists():
        se.write_text(DEFAULT_SKILLS_ENABLED, encoding="utf-8")

    # .gitignore
    gi = target / ".gitignore"
    if not gi.exists():
        gi.write_text(PROJECT_GITIGNORE, encoding="utf-8")

    # Extract bundled agents → _system/
    manifest = _copy_bundled(target / "_system")

    # .yuxu metadata
    (target / ".yuxu" / "version").write_text(ver + "\n", encoding="utf-8")
    (target / ".yuxu" / "manifest.json").write_text(
        json.dumps({"yuxu_version": ver, "bundled": manifest}, indent=2,
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    register_project(target)
    return target


def print_init_summary(target: Path) -> None:
    print(f"[yuxu] Initialized project at {target}")
    print("[yuxu] Next steps:")
    print(f"  cd {target}")
    print("  # edit config/rate_limits.yaml to add your LLM API key")
    print("  yuxu serve")
