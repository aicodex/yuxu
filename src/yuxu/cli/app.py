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


def _cmd_examples_list(args: argparse.Namespace) -> int:
    """List shipped example agents."""
    import yuxu.examples
    root = Path(yuxu.examples.__file__).parent
    names = sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and not d.name.startswith((".", "_"))
    )
    if not names:
        print("(no examples)")
        return 0
    for n in names:
        agent_md = root / n / "AGENT.md"
        summary = ""
        if agent_md.exists():
            # Take the first non-frontmatter, non-header line as summary.
            in_fm = False
            for line in agent_md.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s == "---":
                    in_fm = not in_fm
                    continue
                if in_fm or not s or s.startswith("#"):
                    continue
                summary = s
                break
        print(f"  {n:<15} {summary}")
    return 0


def _cmd_examples_install(args: argparse.Namespace) -> int:
    """Copy an example agent into <project>/agents/."""
    import shutil

    import yuxu.examples
    examples_root = Path(yuxu.examples.__file__).parent
    src = examples_root / args.name
    if not src.is_dir():
        available = sorted(
            d.name for d in examples_root.iterdir()
            if d.is_dir() and not d.name.startswith((".", "_"))
        )
        print(f"error: no such example: {args.name!r}. "
              f"Available: {', '.join(available) or '(none)'}",
              file=sys.stderr)
        return 1

    project = Path(args.project or ".").expanduser().resolve()
    if not (project / "yuxu.json").exists():
        print(f"error: {project} is not a yuxu project (no yuxu.json). "
              f"Run `yuxu init` first.", file=sys.stderr)
        return 1

    dest = project / "agents" / args.name
    if dest.exists() and not args.force:
        print(f"error: {dest} already exists. Use --force to overwrite.",
              file=sys.stderr)
        return 1
    if dest.exists():
        shutil.rmtree(dest)

    shutil.copytree(src, dest,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # prune .gitkeep placeholder from agents/
    gk = project / "agents" / ".gitkeep"
    if gk.exists():
        gk.unlink()
    print(f"[yuxu] Installed example {args.name!r} → {dest}")
    print("[yuxu] Next: cd to the project and `yuxu serve`.")
    return 0


def _cmd_feishu_register(args: argparse.Namespace) -> int:
    """Scan-to-create Feishu/Lark onboarding. Saves credentials to
    <project>/config/secrets/feishu.yaml unless --no-save."""
    from ..bundled.gateway.adapters.feishu_onboard import register_feishu

    domain = "lark" if args.lark else "feishu"
    print(f"[yuxu] Starting Feishu/Lark ({domain}) onboarding...")
    creds = register_feishu(initial_domain=domain, timeout_seconds=args.timeout)
    if not creds:
        print("error: onboarding failed (denied, timed out, or network error).",
              file=sys.stderr)
        return 1

    print("[yuxu] ✓ Registered.")
    print(f"        app_id:       {creds['app_id']}")
    print(f"        domain:       {creds['domain']}")
    if creds.get("bot_name"):
        print(f"        bot_name:     {creds['bot_name']}")
    if creds.get("bot_open_id"):
        print(f"        bot_open_id:  {creds['bot_open_id']}")
    if creds.get("open_id"):
        print(f"        your open_id: {creds['open_id']}")

    if args.no_save:
        base = ("larksuite.com" if creds["domain"] == "lark"
                else "feishu.cn")
        print("\n[yuxu] --no-save: not writing credentials. Export yourself:")
        print(f"  export FEISHU_APP_ID='{creds['app_id']}'")
        print(f"  export FEISHU_APP_SECRET='{creds['app_secret']}'")
        print(f"  export FEISHU_API_BASE='https://open.{base}'")
        return 0

    project = Path(args.project or ".").expanduser().resolve()
    if not (project / "yuxu.json").exists():
        print(f"error: {project} is not a yuxu project (no yuxu.json). "
              f"Run `yuxu init` first, or use --no-save.",
              file=sys.stderr)
        return 1

    import yaml as _yaml
    secrets_dir = project / "config" / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    target = secrets_dir / "feishu.yaml"
    target.write_text(
        _yaml.safe_dump({
            "app_id":       creds["app_id"],
            "app_secret":   creds["app_secret"],
            "domain":       creds["domain"],
            "open_id":      creds.get("open_id"),
            "bot_name":     creds.get("bot_name"),
            "bot_open_id":  creds.get("bot_open_id"),
        }, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    print(f"\n[yuxu] Credentials saved to {target}")
    print("[yuxu] `yuxu serve` will pick them up automatically next run.")
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

    # examples
    p_ex = subs.add_parser("examples",
                            help="Shipped example agents.")
    ex_subs = p_ex.add_subparsers(dest="examples_cmd", required=True)
    p_ex_list = ex_subs.add_parser("list", help="List available examples.")
    p_ex_list.set_defaults(func=_cmd_examples_list)
    p_ex_install = ex_subs.add_parser(
        "install",
        help="Copy an example agent into <project>/agents/.",
    )
    p_ex_install.add_argument("name", help="Example folder name, e.g. echo_bot.")
    p_ex_install.add_argument("--project", default=None,
                               help="Project dir (default: cwd).")
    p_ex_install.add_argument("--force", action="store_true",
                               help="Overwrite if already present.")
    p_ex_install.set_defaults(func=_cmd_examples_install)

    # feishu
    p_fs = subs.add_parser("feishu",
                            help="Feishu / Lark onboarding + bot management.")
    fs_subs = p_fs.add_subparsers(dest="feishu_cmd", required=True)
    p_fs_reg = fs_subs.add_parser(
        "register",
        help="Scan-to-create: show QR, user scans in Feishu app, "
             "we get app_id+app_secret.",
    )
    p_fs_reg.add_argument("--project", default=None,
                          help="Project dir to save credentials into (default: cwd).")
    p_fs_reg.add_argument("--lark", action="store_true",
                          help="Use Lark (international) instead of Feishu.")
    p_fs_reg.add_argument("--timeout", type=int, default=600,
                          help="Max seconds to wait for QR scan (default 600).")
    p_fs_reg.add_argument("--no-save", action="store_true",
                          help="Don't write to config/secrets/feishu.yaml; print env vars instead.")
    p_fs_reg.set_defaults(func=_cmd_feishu_register)

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
