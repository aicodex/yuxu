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
from typing import Optional

from ..bundled.create_agent.handler import create_agent as _skill_create_agent
from ..bundled.create_project.handler import create_project as _skill_create_project
from ..bundled.list_agents.handler import list_agents as _skill_list_agents
from ..bundled.list_projects.handler import list_projects as _skill_list_projects
from .bootstrap import ensure_home, home_dir
from .run import run_one_shot
from .serve import run_serve


# -- command impls ----------------------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    try:
        p = _skill_create_project(args.dir or ".", force=args.force)
    except FileExistsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"[yuxu] Initialized project at {p}")

    # Interactive chat-platform setup (skippable via --skip-setup or no-TTY).
    if not args.skip_setup and sys.stdin.isatty():
        from .setup_wizard import run_setup_wizard
        run_setup_wizard(p, interactive=True)

    print("[yuxu] Next steps:")
    print(f"  cd {p}")
    print("  # edit config/rate_limits.yaml to add your LLM API key")
    print("  yuxu new agent <name>      # scaffold a business agent")
    print("  yuxu serve                 # run the daemon")
    return 0


def _cmd_setup(args: argparse.Namespace) -> int:
    """Re-run the chat-platform setup wizard on an existing project."""
    from .setup_wizard import run_setup_wizard
    project = Path(args.project or ".").expanduser().resolve()
    interactive = (not args.non_interactive) and sys.stdin.isatty()
    return run_setup_wizard(project, interactive=interactive)


def _cmd_new_agent(args: argparse.Namespace) -> int:
    project_dir = Path(args.project or ".").expanduser().resolve()
    try:
        p = _skill_create_agent(project_dir, args.name, template=args.template)
    except (FileExistsError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"[yuxu] Created agent at {p}")
    print(f"[yuxu] Edit {p}/AGENT.md + {p}/handler.py, then `yuxu serve` picks it up.")
    return 0


def _cmd_list_projects(args: argparse.Namespace) -> int:
    projects = _skill_list_projects()
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
        agents = _skill_list_agents(project_dir)
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
    run_serve(target, extra_agents=args.agent or None,
              log_level=args.log_level,
              dev_mode=bool(getattr(args, "dev", False)))
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    """Refresh <project>/_system/ from the installed yuxu package.

    Needed because `yuxu init` takes a one-shot snapshot of bundled agents.
    After `pip install -U yuxu` or during dev iteration, run `yuxu sync`
    to bring `_system/` up to date. `agents/` and `data/` are untouched."""
    from ..bundled._shared import copy_bundled_into
    from .. import __version__ as installed_ver

    target = Path(args.project or ".").expanduser().resolve()
    if not (target / "yuxu.json").exists():
        print(f"error: {target} is not a yuxu project (no yuxu.json).",
              file=sys.stderr)
        return 1
    system_dir = target / "_system"
    version_file = target / ".yuxu" / "version"
    old_ver = version_file.read_text(encoding="utf-8").strip() \
        if version_file.exists() else "(unknown)"
    old_agents = ({d.name for d in system_dir.iterdir() if d.is_dir()}
                  if system_dir.exists() else set())

    manifest = copy_bundled_into(system_dir)
    new_agents = {e["name"] for e in manifest}

    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(installed_ver + "\n", encoding="utf-8")

    added = sorted(new_agents - old_agents)
    removed = sorted(old_agents - new_agents)
    print(f"[yuxu sync] {target}")
    print(f"  version: {old_ver} → {installed_ver}")
    print(f"  bundled agents: {len(new_agents)} total "
          f"({len(added)} added, {len(removed)} removed, "
          f"{len(new_agents & old_agents)} refreshed)")
    if added:
        print(f"  added:   {', '.join(added)}")
    if removed:
        print(f"  removed: {', '.join(removed)}")
    return 0


def _cmd_ps(args: argparse.Namespace) -> int:
    """List yuxu serves on this machine.

    Reads `~/.yuxu/runtime/*.json` written by each live serve, validates the
    recorded pid, prunes stale entries, prints a table."""
    import json as _json
    import os as _os
    from datetime import datetime

    runtime_dir = home_dir() / "runtime"
    if not runtime_dir.exists():
        print("(no ~/.yuxu/runtime/; no yuxu serve has run yet)")
        return 0
    entries: list[dict] = []
    for p in sorted(runtime_dir.glob("*.json")):
        try:
            data = _json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        pid = data.get("pid")
        alive = False
        if isinstance(pid, int):
            try:
                _os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            except PermissionError:
                alive = True   # pid exists but we can't signal it
        if not alive and not args.include_stale:
            try:
                p.unlink()
            except OSError:
                pass
            continue
        entries.append({**data, "_alive": alive})

    if not entries:
        print("(no live yuxu serves)")
        return 0

    print(f"{'PID':>7}  {'ALIVE':<5}  {'STARTED':<19}  PROJECT")
    for e in entries:
        pid = e.get("pid", "?")
        alive_s = "✓" if e.get("_alive") else "stale"
        ts = e.get("started_at", "")
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00")) \
                .astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
        proj = e.get("project_dir", "?")
        print(f"{str(pid):>7}  {alive_s:<5}  {ts:<19}  {proj}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    target = Path(args.dir or ".").expanduser().resolve()
    if not (target / "yuxu.json").exists():
        print(f"error: no yuxu.json at {target}. Run `yuxu init {target}` first.",
              file=sys.stderr)
        return 1
    return run_one_shot(target, args.agent, log_level=args.log_level)


def _cmd_status(args: argparse.Namespace) -> int:
    home = home_dir()
    print(f"yuxu home: {home}")
    if not home.exists():
        print("  (not initialized; any CLI command will create it)")
        return 0
    projects = _skill_list_projects()
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


def _pairing_registry(project: Path):
    from ..bundled.gateway.pairing import PairingRegistry
    return PairingRegistry(project / "config" / "secrets" / "pairings.yaml")


def _project_dir(args: argparse.Namespace) -> Optional["Path"]:
    target = Path(args.project or ".").expanduser().resolve()
    if not (target / "yuxu.json").exists():
        print(f"error: {target} is not a yuxu project (no yuxu.json). "
              f"Run `yuxu init` first, or pass --project DIR.",
              file=sys.stderr)
        return None
    return target


def _cmd_pair_list(args: argparse.Namespace) -> int:
    project = _project_dir(args)
    if project is None:
        return 1
    reg = _pairing_registry(project)
    platform = args.platform
    allowed = reg.list_allowed(platform)
    pending = reg.list_pending(platform)
    print(f"[pairing] file: {reg.path}")
    print(f"[pairing] allowed ({len(allowed)}):")
    for e in allowed:
        note = f" — {e.note}" if e.note else ""
        print(f"   ✓ {e.platform:<10} {e.user_id:<28} {e.approved_at}{note}")
    print(f"[pairing] pending ({len(pending)}):")
    for e in pending:
        snippet = e.first_message.replace("\n", " ")[:40]
        print(f"   ⏳ {e.platform:<10} {e.user_id:<28} {e.first_seen} "
              f'"{snippet}"')
    return 0


def _cmd_pair_approve(args: argparse.Namespace) -> int:
    project = _project_dir(args)
    if project is None:
        return 1
    reg = _pairing_registry(project)
    reg.approve_pending(args.platform, args.user_id, note=args.note or "")
    print(f"[pairing] ✓ approved {args.platform}:{args.user_id}")
    return 0


def _cmd_pair_reject(args: argparse.Namespace) -> int:
    project = _project_dir(args)
    if project is None:
        return 1
    reg = _pairing_registry(project)
    removed = reg.reject_pending(args.platform, args.user_id)
    if removed:
        print(f"[pairing] ✗ rejected {args.platform}:{args.user_id}")
    else:
        print(f"[pairing] (no pending record for {args.platform}:{args.user_id})")
    return 0


def _cmd_pair_revoke(args: argparse.Namespace) -> int:
    project = _project_dir(args)
    if project is None:
        return 1
    reg = _pairing_registry(project)
    removed = reg.revoke_allowed(args.platform, args.user_id)
    if removed:
        print(f"[pairing] ↺ revoked {args.platform}:{args.user_id}")
    else:
        print(f"[pairing] (not currently allowed)")
    return 0


def _cmd_feishu_inject_event(args: argparse.Namespace) -> int:
    """POST a synthetic Feishu event to the local webhook for testing.

    Lets you verify the full inbound path (webhook → parse → gateway →
    agent) without needing a public HTTPS URL.
    """
    import asyncio
    import json
    import time as _time

    if args.file:
        try:
            payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    elif args.text is not None:
        # Synthesize a minimal im.message.receive_v1 event
        payload = {
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": args.user_id or "ou_tester"}},
                "message": {
                    "message_id": "om_test_" + str(int(_time.time() * 1000)),
                    "message_type": "text",
                    "chat_id": args.chat_id or "oc_test",
                    "chat_type": args.chat_type,
                    "content": json.dumps({"text": args.text}, ensure_ascii=False),
                },
            },
        }
    else:
        print("error: must provide --file or --text", file=sys.stderr)
        return 1

    url = args.url
    headers = {"Content-Type": "application/json"}
    if args.token:
        # Inject the verification token into the payload (for plaintext mode)
        payload.setdefault("token", args.token)

    async def _post():
        import httpx
        async with httpx.AsyncClient() as c:
            resp = await c.post(url, json=payload, headers=headers, timeout=10.0)
            return resp.status_code, resp.text
    try:
        status, body = asyncio.run(_post())
    except Exception as e:
        import traceback as _tb
        print(f"error: {e!r}", file=sys.stderr)
        _tb.print_exc(file=sys.stderr)
        return 1
    print(f"[feishu inject-event] {url} → HTTP {status}")
    print(body)
    return 0 if status < 400 else 1


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
    p_init.add_argument("--skip-setup", action="store_true",
                        help="Skip the interactive chat-platform wizard. "
                             "Run `yuxu setup` later to configure.")
    p_init.set_defaults(func=_cmd_init)

    # setup
    p_setup = subs.add_parser(
        "setup",
        help="Interactive chat-platform wizard (feishu / telegram / skip).",
    )
    p_setup.add_argument("--project", default=None,
                         help="Project dir (default: cwd).")
    p_setup.add_argument("--non-interactive", action="store_true",
                         help="Only print status; don't prompt.")
    p_setup.set_defaults(func=_cmd_setup)

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
    p_serve.add_argument("--dev", action="store_true",
                         help="Dev mode: scan the installed yuxu/bundled/ "
                              "directly (skip project _system/ snapshot). "
                              "Edits to bundled source take effect on restart.")
    p_serve.set_defaults(func=_cmd_serve)

    # sync
    p_sync = subs.add_parser(
        "sync",
        help="Refresh <project>/_system/ from installed yuxu package. "
             "Run after `pip install -U yuxu` or dev edits to bundled agents.",
    )
    p_sync.add_argument("--project", default=None,
                         help="Project dir (default: cwd).")
    p_sync.set_defaults(func=_cmd_sync)

    # ps
    p_ps = subs.add_parser(
        "ps",
        help="List yuxu serves running on this machine (from ~/.yuxu/runtime/).",
    )
    p_ps.add_argument("--include-stale", action="store_true",
                       help="Show entries whose pid is dead (usually pruned).")
    p_ps.set_defaults(func=_cmd_ps)

    # run
    p_run = subs.add_parser(
        "run",
        help="Boot ephemerally, run one non-persistent agent, exit. "
             "For long-running daemons use `yuxu serve`.",
    )
    p_run.add_argument("agent", help="Agent name (must exist in this project).")
    p_run.add_argument("--dir", default=None,
                       help="Project directory (default: cwd).")
    p_run.add_argument("--log-level", default="INFO",
                       choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p_run.set_defaults(func=_cmd_run)

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

    # pair
    p_pair = subs.add_parser("pair",
                              help="Manage per-platform pairing (allowlist + pending).")
    pair_subs = p_pair.add_subparsers(dest="pair_cmd", required=True)
    p_pair_list = pair_subs.add_parser("list", help="Show allowed + pending pairings.")
    p_pair_list.add_argument("--project", default=None)
    p_pair_list.add_argument("--platform", default=None,
                              help="Filter (e.g. 'feishu', 'telegram').")
    p_pair_list.set_defaults(func=_cmd_pair_list)

    def _add_id_args(sp):
        sp.add_argument("platform", help="Platform name (feishu, telegram, ...).")
        sp.add_argument("user_id", help="Platform-specific user id (e.g. open_id).")
        sp.add_argument("--project", default=None)

    p_pair_approve = pair_subs.add_parser(
        "approve",
        help="Approve pending (or pre-provision) a user.",
    )
    _add_id_args(p_pair_approve)
    p_pair_approve.add_argument("--note", default=None,
                                 help="Human-readable note (e.g. Alice / QA tester).")
    p_pair_approve.set_defaults(func=_cmd_pair_approve)

    p_pair_reject = pair_subs.add_parser(
        "reject",
        help="Drop a pending user without approving.",
    )
    _add_id_args(p_pair_reject)
    p_pair_reject.set_defaults(func=_cmd_pair_reject)

    p_pair_revoke = pair_subs.add_parser(
        "revoke",
        help="Remove a currently-allowed user.",
    )
    _add_id_args(p_pair_revoke)
    p_pair_revoke.set_defaults(func=_cmd_pair_revoke)

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

    p_fs_inj = fs_subs.add_parser(
        "inject-event",
        help="POST a synthetic Feishu event to the local webhook "
             "(tests inbound path without needing a public HTTPS URL).",
    )
    p_fs_inj.add_argument("--url", default="http://127.0.0.1:7001/feishu/webhook",
                           help="Local webhook URL.")
    p_fs_inj.add_argument("--text", default=None,
                           help="Synthesize a plain text message with this content.")
    p_fs_inj.add_argument("--file", default=None,
                           help="Post this JSON file (raw event payload).")
    p_fs_inj.add_argument("--user-id", default=None,
                           help="sender open_id for synthesized event (default ou_tester).")
    p_fs_inj.add_argument("--chat-id", default=None,
                           help="chat_id for synthesized event (default oc_test).")
    p_fs_inj.add_argument("--chat-type", default="p2p",
                           choices=["p2p", "group"])
    p_fs_inj.add_argument("--token", default=None,
                           help="verification_token to include in the payload.")
    p_fs_inj.set_defaults(func=_cmd_feishu_inject_event)

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
