"""AgentContext — the single argument passed to every agent's start/stop.

**Contract stability principle**: only ADD fields over time, never rename or
remove. Downstream agents sign against this shape; growing it is safe,
shrinking it breaks the ecosystem.

See `docs/CORE_INTERFACE.md` for the full contract.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .bus import Bus
    from .loader import Loader


@dataclass(frozen=True)
class AgentContext:
    name: str                              # agent's registered name (= folder name)
    agent_dir: Path                        # absolute path to the agent folder
    frontmatter: dict                      # parsed AGENT.md frontmatter (may be {})
    body: str                              # AGENT.md body (for LLM agents; may be "")
    bus: "Bus"                             # message bus
    loader: "Loader"                       # use sparingly: introspection / cross-agent handles
    logger: logging.Logger                 # pre-bound logger (`agent.{name}`)

    # -- shortcuts: these are stable sugar over bus/loader, safe to rely on -----

    async def ready(self) -> None:
        """Declare this agent ready. Must be called once during start()."""
        await self.bus.ready(self.name)

    def get_agent(self, name: str) -> Any:
        """Return another agent's public handle (from its `get_handle(ctx)`),
        or None if that agent doesn't expose one or isn't loaded yet.

        Prefer bus.request(...) for request/reply style; use this when you
        need a live Python object (e.g., rate_limit_service.acquire).
        """
        return self.loader.get_handle(name)

    async def wait_for(self, name: str, timeout: Optional[float] = None) -> None:
        """Block until another agent reaches 'ready'."""
        await self.bus.wait_for_service(name, timeout=timeout)
