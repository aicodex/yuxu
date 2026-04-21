"""yuxu (玉虚) — long-running agent creation and supervision framework."""
from __future__ import annotations

__version__ = "0.0.1"

# Re-export the stable public surface (contract in docs/CORE_INTERFACE.md)
from .core import Bus, Message, Loader, AgentSpec, AgentContext, parse_frontmatter

__all__ = [
    "__version__",
    "Bus",
    "Message",
    "Loader",
    "AgentSpec",
    "AgentContext",
    "parse_frontmatter",
]
