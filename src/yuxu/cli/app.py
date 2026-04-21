"""`yuxu` CLI entrypoint.

Commands:
    yuxu init [DIR]       scaffold a project directory (default: cwd)
    yuxu serve [DIR]      run the framework in a project directory (default: cwd)
    yuxu status           list known projects + yuxu home info
    yuxu version          print yuxu version

On every invocation, ensures `~/.yuxu/` exists (first-run bootstrap).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .bootstrap import ensure_home, home_dir
from .project_init import init_project, print_init_summary
from .serve import run_serve


def _cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.dir or ".").resolve()
    try:
        project = init_project(target, force=args.force)
    except FileExistsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print_init_summary(project)
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
        print("  (not initialized; will be created on first CLI invocation)")
        return 0
    proj_file = home / "projects.yaml"
    if proj_file.exists():
        data = yaml.safe_load(proj_file.read_text(encoding="utf-8")) or {}
        projects = data.get("projects") or []
        print(f"known projects ({len(projects)}):")
        for p in projects:
            pp = Path(p)
            alive = "✓" if (pp / "yuxu.json").exists() else "✗ (missing yuxu.json)"
            print(f"  {alive} {p}")
    else:
        print("no projects.yaml")
    return 0


def _cmd_version(args: argparse.Namespace) -> int:
    from .. import __version__
    print(__version__)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yuxu",
        description="Yuxu (玉虚) — long-running agent creation and supervision framework.",
    )
    subs = p.add_subparsers(dest="cmd", required=False)

    p_init = subs.add_parser("init", help="Scaffold a new project directory.")
    p_init.add_argument("dir", nargs="?", default=None,
                        help="Project directory (default: cwd).")
    p_init.add_argument("--force", action="store_true",
                        help="Overwrite existing yuxu.json.")
    p_init.set_defaults(func=_cmd_init)

    p_serve = subs.add_parser("serve", help="Run the daemon.")
    p_serve.add_argument("dir", nargs="?", default=None,
                         help="Project directory (default: cwd).")
    p_serve.add_argument("--agent", action="append",
                         help="Additional agent to start after persistent ones. Repeatable.")
    p_serve.add_argument("--log-level", default="INFO",
                         choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p_serve.set_defaults(func=_cmd_serve)

    p_status = subs.add_parser("status", help="Show yuxu home + known projects.")
    p_status.set_defaults(func=_cmd_status)

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
