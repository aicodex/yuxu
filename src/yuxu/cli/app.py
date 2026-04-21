"""`yuxu` CLI — thin wrapper around the `project_manager` bundled agent.

Most of the heavy lifting lives in `yuxu.bundled.project_manager.handler`;
this module just parses argv, calls the right static method, and prints.
Same logic is reachable via `bus.request("project_manager", ...)` at
runtime (for future shell / chat-based creation flows).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..bundled.project_manager.handler import ProjectManager
from .bootstrap import ensure_home, home_dir
from .serve import run_serve


# -- command impls ----------------------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    try:
        p = ProjectManager.create_project(args.dir or ".", force=args.force)
    except FileExistsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"[yuxu] Initialized project at {p}")
    print("[yuxu] Next steps:")
    print(f"  cd {p}")
    print("  # edit config/rate_limits.yaml to add your LLM API key")
    print("  yuxu new agent <name>      # scaffold a business agent")
    print("  yuxu serve                 # run the daemon")
    return 0


def _cmd_new_agent(args: argparse.Namespace) -> int:
    project_dir = Path(args.project or ".").expanduser().resolve()
    try:
        p = ProjectManager.create_agent(project_dir, args.name, template=args.template)
    except (FileExistsError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"[yuxu] Created agent at {p}")
    print(f"[yuxu] Edit {p}/AGENT.md + {p}/handler.py, then `yuxu serve` picks it up.")
    return 0


def _cmd_list_projects(args: argparse.Namespace) -> int:
    projects = ProjectManager.list_projects()
    if not projects:
        print("(no projects registered; run `yuxu init <dir>` to create one)")
        return 0
    for p in projects:
        flag = "✓" if p.get("exists") else "✗"
        name = p.get("name") or "?"
        ver = p.get("yuxu_version") or "?"
        print(f"{flag} {name:<30} [yuxu {ver:<8}] {p['path']}")
    return 0


def _cmd_list_agents(args: argparse.Namespace) -> int:
    project_dir = Path(args.project or ".").expanduser().resolve()
    try:
        agents = ProjectManager.list_agents(project_dir)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if not agents:
        print("(no agents in this project)")
        return 0
    for a in agents:
        tag = "[system]" if a["source"] == "bundled" else "[user]  "
        print(f"{tag} {a['name']:<25} {a['path']}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    target = Path(args.dir or ".").resolve()
    if not (target / "yuxu.json").exists():
        print(f"error: no yuxu.json at {target}. Run `yuxu init {target}` first.",
              file=sys.stderr)
        return 1
    run_serve(target, extra_agents=args.agent or None, log_level=args.log_level)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    import yaml
    home = home_dir()
    print(f"yuxu home: {home}")
    if not home.exists():
        print("  (not initialized; any CLI command will create it)")
        return 0
    projects = ProjectManager.list_projects()
    print(f"known projects ({len(projects)}):")
    for p in projects:
        flag = "✓" if p.get("exists") else "✗"
        print(f"  {flag} {p['path']}")
    return 0


def _cmd_version(args: argparse.Namespace) -> int:
    from .. import __version__
    print(__version__)
    return 0


# -- parser -----------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yuxu",
        description="Yuxu (玉虚) — long-running agent creation and supervision framework.",
    )
    subs = p.add_subparsers(dest="cmd")

    # init
    p_init = subs.add_parser("init", help="Scaffold a new project directory.")
    p_init.add_argument("dir", nargs="?", default=None,
                        help="Project directory (default: cwd).")
    p_init.add_argument("--force", action="store_true",
                        help="Overwrite existing yuxu.json.")
    p_init.set_defaults(func=_cmd_init)

    # new
    p_new = subs.add_parser("new", help="Scaffold an agent / skill from a template.")
    new_subs = p_new.add_subparsers(dest="new_cmd", required=True)
    p_new_agent = new_subs.add_parser("agent", help="Create a new agent.")
    p_new_agent.add_argument("name", help="Agent name (= folder name).")
    p_new_agent.add_argument("--project", default=None,
                             help="Project dir (default: cwd).")
    p_new_agent.add_argument("--template", default="default",
                             help="Template to use (default: 'default').")
    p_new_agent.set_defaults(func=_cmd_new_agent)

    # list
    p_list = subs.add_parser("list", help="List projects or agents.")
    list_subs = p_list.add_subparsers(dest="list_cmd", required=True)
    p_list_p = list_subs.add_parser("projects", help="Projects registered in ~/.yuxu.")
    p_list_p.set_defaults(func=_cmd_list_projects)
    p_list_a = list_subs.add_parser("agents", help="Agents in a project (bundled + user).")
    p_list_a.add_argument("--project", default=None, help="Project dir (default: cwd).")
    p_list_a.set_defaults(func=_cmd_list_agents)

    # serve
    p_serve = subs.add_parser("serve", help="Run the daemon.")
    p_serve.add_argument("dir", nargs="?", default=None,
                         help="Project directory (default: cwd).")
    p_serve.add_argument("--agent", action="append",
                         help="Additional agent to start after persistent ones. Repeatable.")
    p_serve.add_argument("--log-level", default="INFO",
                         choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p_serve.set_defaults(func=_cmd_serve)

    # status
    p_status = subs.add_parser("status", help="Show yuxu home + known projects.")
    p_status.set_defaults(func=_cmd_status)

    # version
    p_ver = subs.add_parser("version", help="Print yuxu version.")
    p_ver.set_defaults(func=_cmd_version)

    return p


def main(argv: list[str] | None = None) -> int:
    # First-run bootstrap runs on EVERY invocation (idempotent after first run).
    ensure_home(verbose=True)

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
