"""`yuxu run <agent>` CLI coverage."""
from __future__ import annotations

import textwrap
from pathlib import Path

from yuxu.cli.app import main as cli_main


def _write_agent(project: Path, name: str, fm_lines: str, init_src: str) -> Path:
    d = project / "agents" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "AGENT.md").write_text(f"---\n{fm_lines}\n---\n# {name}\n", encoding="utf-8")
    (d / "__init__.py").write_text(textwrap.dedent(init_src), encoding="utf-8")
    return d


def test_run_executes_one_shot_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    proj = tmp_path / "proj"
    assert cli_main(["init", str(proj), "--skip-setup"]) == 0

    marker = tmp_path / "marker.txt"
    _write_agent(proj, "marker_bot",
                 "driver: python\nrun_mode: one_shot\nscope: user",
                 f"""
        from pathlib import Path
        async def start(ctx):
            Path({str(marker)!r}).write_text("ran")
            await ctx.ready()
    """)

    rc = cli_main(["run", "marker_bot", "--dir", str(proj)])
    assert rc == 0
    assert marker.exists() and marker.read_text() == "ran"


def test_run_unknown_agent_returns_1(tmp_path, monkeypatch):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    proj = tmp_path / "proj"
    assert cli_main(["init", str(proj), "--skip-setup"]) == 0
    rc = cli_main(["run", "nope", "--dir", str(proj)])
    assert rc == 1


def test_run_refuses_persistent_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    proj = tmp_path / "proj"
    assert cli_main(["init", str(proj), "--skip-setup"]) == 0

    _write_agent(proj, "daemon",
                 "driver: python\nrun_mode: persistent\nscope: user",
                 """
        async def start(ctx):
            await ctx.ready()
    """)

    rc = cli_main(["run", "daemon", "--dir", str(proj)])
    assert rc == 1


def test_run_outside_yuxu_project_returns_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    rc = cli_main(["run", "anything", "--dir", str(tmp_path / "not_a_proj")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "yuxu.json" in err
