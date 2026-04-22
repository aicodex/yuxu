"""SkillExecutor — skill runtime layer.

Reads the skill catalog from `skill_picker` at install (and `rescan`),
dynamically imports each skill's handler module and registers an
`execute(input, ctx)` function at bus address `skill.{name}` (Mode A).
Skills whose frontmatter declares `context: inline` are not bus-registered;
they're accessible only via `op: expand_inline` (Mode B), which substitutes
$ARGUMENTS / $1 / $foo and executes `!cmd` preambles before returning the
expanded prompt text.

Mode C (sub-agent fork) deferred until yuxu has a sub-agent framework.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

DEFAULT_PREAMBLE_TIMEOUT = 30.0
MAX_PREAMBLE_BYTES = 8_192

# !`command` — must not contain backtick or newline in the command
_INLINE_CMD_RE = re.compile(r"!`([^`\n]+)`")
# ```! (optional lang) \n ... \n ```
_FENCED_CMD_RE = re.compile(r"```!\s*\n(.*?)\n```", re.DOTALL)


# -- argument expansion -----------------------------------------


def _parse_named_args(frontmatter: dict) -> list[str]:
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


def _substitute_args(body: str, *, args_raw: str,
                     positional: list[str],
                     named: dict[str, str]) -> str:
    """Expand $ARGUMENTS / $1..$N / $foo placeholders in `body`.

    - `$ARGUMENTS` → the raw args string (unchanged)
    - `$1`..`$N` → shell-quote-parsed positional slot (empty if missing)
    - `$foo` → named slot (empty if missing)

    Named substitution only matches `[A-Za-z_][A-Za-z0-9_]*` identifiers so
    it won't eat e.g. `$foo123_bar` halfway. Numeric placeholders are handled
    separately.
    """
    out = body
    # $ARGUMENTS — verbatim args string
    out = out.replace("$ARGUMENTS", args_raw)

    # Longest-first ordering so `$foobar` wins over `$foo` when both are
    # registered names. Named before numeric so `$1a` is not parsed as `$1 a`.
    for name in sorted(named.keys(), key=len, reverse=True):
        out = re.sub(rf"\${re.escape(name)}\b", named[name], out)

    # $N: substitute only on word boundary to avoid $10 matching inside $100
    def _posn_sub(m: "re.Match") -> str:
        idx = int(m.group(1))
        if 1 <= idx <= len(positional):
            return positional[idx - 1]
        return ""
    out = re.sub(r"\$(\d+)\b", _posn_sub, out)
    return out


# -- preamble execution ----------------------------------------


async def _run_shell(cmd: str, *,
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


async def _execute_preambles(text: str, *,
                              timeout: float = DEFAULT_PREAMBLE_TIMEOUT) -> str:
    """Find and execute both fenced (```!) and inline (!`cmd`) preambles.
    Fenced ones are resolved first (they're typically heavier)."""
    # Fenced blocks
    parts: list[str] = []
    last = 0
    for m in _FENCED_CMD_RE.finditer(text):
        parts.append(text[last:m.start()])
        out = await _run_shell(m.group(1).strip(), timeout=timeout)
        parts.append(out)
        last = m.end()
    parts.append(text[last:])
    expanded = "".join(parts)

    # Inline blocks (second pass so fenced output can contain !`…` literally
    # without being re-executed)
    parts = []
    last = 0
    for m in _INLINE_CMD_RE.finditer(expanded):
        parts.append(expanded[last:m.start()])
        out = await _run_shell(m.group(1), timeout=timeout)
        parts.append(out)
        last = m.end()
    parts.append(expanded[last:])
    return "".join(parts)


# -- dynamic import --------------------------------------------


def _import_skill_module(name: str, path: Path, handler_filename: str):
    """Load {path}/{handler_filename} as an ephemeral module named
    `_skills.{name}`. Raises any import-time exception."""
    mod_name = f"_skills.{name.replace('-', '_')}"
    handler_path = path / handler_filename
    if not handler_path.exists():
        raise FileNotFoundError(f"{handler_path}")
    spec = importlib.util.spec_from_file_location(
        mod_name, handler_path, submodule_search_locations=[str(path)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create spec for {handler_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# -- main class ------------------------------------------------


class SkillExecutor:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        # skill_name -> (imported module, registered bus addr)
        self._bus_registered: dict[str, str] = {}
        # skill_name -> inline-only flag (not bus-registered)
        self._inline_only: set[str] = set()
        self._modules: dict[str, Any] = {}

    async def install(self) -> None:
        await self.rescan()

    async def uninstall(self) -> None:
        self._unregister_all()

    # -- bus-dispatch registration -----------------------------

    def _unregister_all(self) -> None:
        for name, addr in list(self._bus_registered.items()):
            try:
                self.ctx.bus.unregister(addr)
            except Exception:
                pass
        self._bus_registered.clear()
        self._inline_only.clear()
        self._modules.clear()

    def _make_handler(self, name: str, fn: Callable) -> Callable:
        async def handler(msg):
            payload = msg.payload if isinstance(msg.payload, dict) else {}
            # Accept either `{input: {...}}` or a flat payload for the skill
            if "input" in payload:
                skill_input = payload["input"]
            else:
                skill_input = {k: v for k, v in payload.items() if k != "op"}
            try:
                result = fn(skill_input, self.ctx)
                if asyncio.iscoroutine(result):
                    result = await result
            except Exception as e:
                log.exception("skill_executor: skill %s crashed", name)
                return {"ok": False, "error": f"skill {name} crashed: {e}"}
            if isinstance(result, dict):
                return result
            return {"ok": True, "result": result}
        handler.__name__ = f"skill_handler_{name}"
        return handler

    async def rescan(self) -> dict:
        """Refresh the catalog + re-register bus endpoints. Idempotent.

        Returns a report: {ok, registered: [names], inline_only: [names],
        failed: [{name, error}]}."""
        self._unregister_all()
        try:
            r = await self.ctx.bus.request(
                "skill_picker", {"op": "list_all"}, timeout=5.0,
            )
        except Exception as e:
            return {"ok": False, "error": f"skill_picker unreachable: {e}",
                    "registered": [], "inline_only": [], "failed": []}
        skills = r.get("skills") or []

        registered: list[str] = []
        inline_only: list[str] = []
        failed: list[dict] = []
        for entry in skills:
            name = entry.get("name")
            if not name:
                continue
            if not entry.get("enabled", False):
                continue
            fm = entry.get("frontmatter") or {}
            ctx_mode = fm.get("context")
            handler_filename = entry.get("handler_filename") or \
                str(fm.get("handler") or "handler.py")
            path_s = entry.get("path")
            if not path_s:
                continue
            path = Path(path_s)
            # inline-only → do NOT bus-register
            if ctx_mode == "inline":
                inline_only.append(name)
                continue
            handler_path = path / handler_filename
            if not handler_path.exists():
                # No handler file → nothing bus-dispatchable; if caller wants
                # it rendered inline, `expand_inline` will still work.
                inline_only.append(name)
                continue
            try:
                mod = _import_skill_module(name, path, handler_filename)
            except Exception as e:
                log.warning("skill_executor: import %s failed: %s", name, e)
                failed.append({"name": name, "error": str(e)})
                continue
            fn = getattr(mod, "execute", None)
            if fn is None:
                failed.append({"name": name, "error": "module has no execute()"})
                continue
            addr = f"skill.{name}"
            self.ctx.bus.register(addr, self._make_handler(name, fn))
            self._bus_registered[name] = addr
            self._modules[name] = mod
            registered.append(name)
        self._inline_only.update(inline_only)
        return {"ok": True, "registered": sorted(registered),
                "inline_only": sorted(self._inline_only),
                "failed": failed}

    # -- Mode B: inline expand --------------------------------

    async def expand_inline(self, *, skill_name: str, args: str = "",
                            for_agent: Optional[str] = None,
                            for_project: Optional[str] = None,
                            preamble_timeout: float = DEFAULT_PREAMBLE_TIMEOUT
                            ) -> dict:
        load_payload = {
            "op": "load", "name": skill_name, "only_enabled": False,
        }
        if for_agent is not None:
            load_payload["for_agent"] = for_agent
        if for_project is not None:
            load_payload["for_project"] = for_project
        try:
            r = await self.ctx.bus.request("skill_picker", load_payload,
                                            timeout=5.0)
        except Exception as e:
            return {"ok": False, "error": f"skill_picker load failed: {e}"}
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error", "skill not found")}
        body = r.get("body") or ""
        fm = r.get("frontmatter") or {}
        positional = shlex.split(args) if args else []
        names = _parse_named_args(fm)
        named = {}
        for i, n in enumerate(names):
            if i < len(positional):
                named[n] = positional[i]
        substituted = _substitute_args(body, args_raw=args,
                                       positional=positional, named=named)
        expanded = await _execute_preambles(substituted,
                                            timeout=preamble_timeout)
        return {"ok": True, "expanded_prompt": expanded,
                "skill_name": skill_name,
                "positional_args": positional, "named_args": named}

    # -- Mode A: bus dispatch shortcut ------------------------

    async def dispatch_bus(self, *, skill_name: str, input: dict | None = None
                           ) -> dict:
        if skill_name not in self._bus_registered:
            return {"ok": False, "error": (
                f"skill {skill_name!r} not bus-registered "
                f"(registered: {sorted(self._bus_registered)})"
            )}
        addr = self._bus_registered[skill_name]
        try:
            r = await self.ctx.bus.request(addr, {"input": input or {}},
                                           timeout=120.0)
        except Exception as e:
            return {"ok": False, "error": f"dispatch raised: {e}"}
        return r if isinstance(r, dict) else {"ok": True, "result": r}

    async def execute(self, *, skill_name: str, input: dict | None = None,
                      args: str = "") -> dict:
        """Auto-route: inline if skill declared so or no handler, else bus."""
        if skill_name in self._inline_only:
            return await self.expand_inline(skill_name=skill_name, args=args)
        if skill_name in self._bus_registered:
            return await self.dispatch_bus(skill_name=skill_name,
                                           input=input)
        # Unknown — ask the picker what it is
        try:
            r = await self.ctx.bus.request("skill_picker", {
                "op": "load", "name": skill_name, "only_enabled": False,
            }, timeout=5.0)
        except Exception as e:
            return {"ok": False, "error": f"unknown skill {skill_name!r}: {e}"}
        if not r.get("ok"):
            return {"ok": False, "error": f"unknown skill {skill_name!r}"}
        # Fallback: inline-expand
        return await self.expand_inline(skill_name=skill_name, args=args)

    # -- bus surface ------------------------------------------

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op", "status")
        try:
            if op == "execute":
                name = payload.get("skill_name") or payload.get("name")
                if not name:
                    return {"ok": False, "error": "missing skill_name"}
                return await self.execute(
                    skill_name=name,
                    input=payload.get("input"),
                    args=payload.get("args") or "",
                )
            if op == "dispatch_bus":
                name = payload.get("skill_name") or payload.get("name")
                if not name:
                    return {"ok": False, "error": "missing skill_name"}
                return await self.dispatch_bus(
                    skill_name=name, input=payload.get("input"),
                )
            if op == "expand_inline":
                name = payload.get("skill_name") or payload.get("name")
                if not name:
                    return {"ok": False, "error": "missing skill_name"}
                return await self.expand_inline(
                    skill_name=name, args=payload.get("args") or "",
                    for_agent=payload.get("for_agent"),
                    for_project=payload.get("for_project"),
                    preamble_timeout=float(payload.get(
                        "preamble_timeout", DEFAULT_PREAMBLE_TIMEOUT)),
                )
            if op == "rescan":
                return await self.rescan()
            if op == "status":
                return {"ok": True,
                        "registered": sorted(self._bus_registered),
                        "inline_only": sorted(self._inline_only)}
            return {"ok": False, "error": f"unknown op: {op!r}"}
        except (TypeError, KeyError) as e:
            return {"ok": False, "error": f"bad request: {e}"}
