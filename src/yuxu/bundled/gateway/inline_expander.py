r"""Inline skill expansion helpers.

Turns a skill body (markdown with `$ARGUMENTS` / `$1..$N` / `$foo`
placeholders and optional `!\`cmd\`` / ` ```! ... ``` ` preambles) into a
fully-expanded prompt string.

Used by gateway (and future intent_router) when it needs to render an
`context: inline` skill as a prompt for an LLM call. The substitution and
preamble-exec rules match Claude-Code's slash-command convention so CC /
OpenClaw inline skills can be dropped in without rewriting.

Ported verbatim from the retired `skill_executor` agent (Mode B path).
"""
from __future__ import annotations

import asyncio
import logging
import re
import shlex

log = logging.getLogger(__name__)

DEFAULT_PREAMBLE_TIMEOUT = 30.0
MAX_PREAMBLE_BYTES = 8_192

# !`command` — must not contain backtick or newline in the command
_INLINE_CMD_RE = re.compile(r"!`([^`\n]+)`")
# ```! (optional lang) \n ... \n ```
_FENCED_CMD_RE = re.compile(r"```!\s*\n(.*?)\n```", re.DOTALL)


# -- argument expansion -----------------------------------------


def parse_named_args(frontmatter: dict) -> list[str]:
    """Read `argument-names` (CC-style) or `argument_names` (snake) from
    frontmatter. Accept both list form and space-separated string form."""
    raw = frontmatter.get("argument-names", frontmatter.get("argument_names"))
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(n) for n in raw]
    if isinstance(raw, str):
        return raw.split()
    return []


def substitute_args(body: str, *, args_raw: str,
                    positional: list[str],
                    named: dict[str, str]) -> str:
    """Expand $ARGUMENTS / $1..$N / $foo placeholders in `body`.

    - `$ARGUMENTS` → the raw args string (unchanged)
    - `$1`..`$N` → positional slot (empty if missing)
    - `$foo` → named slot (empty if missing)

    Named substitution matches `[A-Za-z_][A-Za-z0-9_]*` identifiers and is
    ordered longest-first so `$foobar` wins over `$foo` when both are
    registered names. Numeric placeholders are handled separately on a word
    boundary so `$10` doesn't match inside `$100`.
    """
    out = body
    out = out.replace("$ARGUMENTS", args_raw)

    for name in sorted(named.keys(), key=len, reverse=True):
        out = re.sub(rf"\${re.escape(name)}\b", named[name], out)

    def _posn_sub(m: "re.Match") -> str:
        idx = int(m.group(1))
        if 1 <= idx <= len(positional):
            return positional[idx - 1]
        return ""
    out = re.sub(r"\$(\d+)\b", _posn_sub, out)
    return out


# -- preamble execution ----------------------------------------


async def run_shell(cmd: str, *,
                    timeout: float = DEFAULT_PREAMBLE_TIMEOUT,
                    max_bytes: int = MAX_PREAMBLE_BYTES) -> str:
    """Execute `cmd` via /bin/sh, return combined formatted output.

    Never raises; encodes errors into the return string so they land in the
    LLM prompt for the model to handle."""
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        return f"[preamble failed to spawn: {e}]"
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return f"[preamble timed out after {timeout}s: {cmd!r}]"
    out = (stdout or b"").decode("utf-8", errors="replace")
    err = (stderr or b"").decode("utf-8", errors="replace")
    if len(out.encode("utf-8", errors="replace")) > max_bytes:
        out = (out.encode("utf-8", errors="replace")[:max_bytes]
               .decode("utf-8", errors="ignore") + "\n[...stdout truncated]")
    if len(err.encode("utf-8", errors="replace")) > max_bytes:
        err = (err.encode("utf-8", errors="replace")[:max_bytes]
               .decode("utf-8", errors="ignore") + "\n[...stderr truncated]")
    tail_parts: list[str] = []
    if proc.returncode and proc.returncode != 0:
        tail_parts.append(f"[exit {proc.returncode}]")
    if out:
        tail_parts.append(out.rstrip())
    if err:
        tail_parts.append(f"[stderr]\n{err.rstrip()}")
    return "\n".join(tail_parts) if tail_parts else ""


async def execute_preambles(text: str, *,
                            timeout: float = DEFAULT_PREAMBLE_TIMEOUT) -> str:
    """Find and execute both fenced (```!) and inline (!`cmd`) preambles.
    Fenced blocks resolved first (typically heavier)."""
    parts: list[str] = []
    last = 0
    for m in _FENCED_CMD_RE.finditer(text):
        parts.append(text[last:m.start()])
        out = await run_shell(m.group(1).strip(), timeout=timeout)
        parts.append(out)
        last = m.end()
    parts.append(text[last:])
    expanded = "".join(parts)

    parts = []
    last = 0
    for m in _INLINE_CMD_RE.finditer(expanded):
        parts.append(expanded[last:m.start()])
        out = await run_shell(m.group(1), timeout=timeout)
        parts.append(out)
        last = m.end()
    parts.append(expanded[last:])
    return "".join(parts)


# -- public API ------------------------------------------------


async def expand_inline_skill(body: str, *, args_raw: str, frontmatter: dict,
                              timeout: float = DEFAULT_PREAMBLE_TIMEOUT) -> str:
    """End-to-end expansion of an inline skill's body.

    Order: argument substitution → preamble execution. Shell preambles run
    on the substituted text, so callers can parameterize preambles via
    $ARGUMENTS / $N / $name.
    """
    named_keys = parse_named_args(frontmatter)
    try:
        positional = shlex.split(args_raw)
    except ValueError:
        positional = args_raw.split()
    named = {k: (positional[i] if i < len(positional) else "")
             for i, k in enumerate(named_keys)}
    substituted = substitute_args(
        body, args_raw=args_raw, positional=positional, named=named,
    )
    return await execute_preambles(substituted, timeout=timeout)
