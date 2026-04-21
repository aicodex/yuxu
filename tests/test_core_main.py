import asyncio
import textwrap
from pathlib import Path

import pytest

from yuxu.core.main import boot

pytestmark = pytest.mark.asyncio


def _write(root: Path, name: str, fm: dict, init_src: str) -> None:
    import yaml as _yaml
    d = root / name
    d.mkdir(parents=True)
    (d / "AGENT.md").write_text(f"---\n{_yaml.safe_dump(fm, sort_keys=False).strip()}\n---\n")
    (d / "__init__.py").write_text(textwrap.dedent(init_src))


async def test_boot_starts_persistent_only(tmp_path):
    _write(tmp_path, "svc_a", {"run_mode": "persistent"}, """
        async def start(ctx):
            async def h(msg): return "a"
            ctx.bus.register("svc_a", h)
            await ctx.ready()
    """)
    _write(tmp_path, "one_off", {"run_mode": "one_shot"}, """
        async def start(ctx):
            await ctx.ready()
    """)
    bus, loader = await boot(dirs=[str(tmp_path)])
    assert bus.query_status("svc_a") == "ready"
    assert bus.query_status("one_off") == "unloaded"


async def test_boot_extra_agents(tmp_path):
    _write(tmp_path, "manual", {"run_mode": "one_shot"}, """
        async def start(ctx):
            await ctx.ready()
    """)
    bus, loader = await boot(dirs=[str(tmp_path)], extra_agents=["manual"])
    assert bus.query_status("manual") == "ready"


async def test_boot_continues_when_one_persistent_fails(tmp_path):
    _write(tmp_path, "good", {"run_mode": "persistent"}, """
        async def start(ctx):
            await ctx.ready()
    """)
    _write(tmp_path, "bad", {"run_mode": "persistent"}, """
        async def start(ctx):
            raise RuntimeError("nope")
    """)
    bus, loader = await boot(dirs=[str(tmp_path)])
    assert bus.query_status("good") == "ready"
    assert bus.query_status("bad") == "failed"


async def test_boot_detects_cycles_before_starting(tmp_path):
    _write(tmp_path, "a", {"run_mode": "persistent", "depends_on": ["b"]}, """
        async def start(ctx):
            await ctx.ready()
    """)
    _write(tmp_path, "b", {"run_mode": "persistent", "depends_on": ["a"]}, """
        async def start(ctx):
            await ctx.ready()
    """)
    with pytest.raises(RuntimeError, match="circular"):
        await boot(dirs=[str(tmp_path)])
