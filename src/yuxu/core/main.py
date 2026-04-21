"""Kernel entrypoint.

Boot sequence:
  1. Create Bus
  2. Create Loader over bundled + user dirs
  3. Scan specs
  4. Start all run_mode=persistent agents (deps resolved via ensure_running)
  5. run_forever

Failure policy: kernel does not self-heal. If anything crucial fails, log and exit.
External supervisor (systemd/supervisord) restarts the process;
recovery_agent (once P2 is in) decides how to resume agent state.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from .bus import Bus
from .loader import Loader

log = logging.getLogger(__name__)

DEFAULT_DIRS = ["_system", "agents"]  # project-relative; CLI typically passes explicit dirs


async def boot(dirs: list[str] | None = None,
               extra_agents: list[str] | None = None,
               autostart_persistent: bool = True) -> tuple[Bus, Loader]:
    bus = Bus()
    loader = Loader(bus, dirs=dirs or DEFAULT_DIRS)
    await loader.scan()
    # Validate graph up-front; fail fast if cycles or missing deps.
    loader.build_dep_graph()
    if autostart_persistent:
        for spec in loader.filter(run_mode="persistent"):
            try:
                await loader.ensure_running(spec.name)
            except Exception:
                log.exception("boot: failed to start persistent agent %s", spec.name)
    for name in extra_agents or []:
        try:
            await loader.ensure_running(name)
        except Exception:
            log.exception("boot: failed to start %s", name)
    return bus, loader


async def _run(args: argparse.Namespace) -> None:
    extras: list[str] = []
    if args.agent:
        extras.append(args.agent)
    bus, loader = await boot(
        dirs=args.dir or None,
        extra_agents=extras,
        autostart_persistent=not args.no_persistent,
    )
    log.info("kernel ready: %d agents loaded", len(loader.specs))
    await bus.run_forever()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(prog="agent_framework")
    p.add_argument("--dir", action="append", help="agent directory (repeatable)")
    p.add_argument("--agent", help="also start a specific agent after persistent ones")
    p.add_argument("--no-persistent", action="store_true",
                   help="skip autostart of persistent agents")
    args = p.parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        log.info("kernel: KeyboardInterrupt, exiting")


if __name__ == "__main__":
    main()
