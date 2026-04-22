"""`yuxu sync` + `yuxu ps` CLI + runtime_monitor integration coverage."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from yuxu.bundled.runtime_monitor.handler import (
    RuntimeMonitor,
    _pid_alive,
    _slug_from_project,
)
from yuxu.cli.app import main as cli_main


@pytest.fixture
def yuxu_home(tmp_path, monkeypatch):
    home = tmp_path / "_yuxu_home"
    monkeypatch.setenv("YUXU_HOME", str(home))
    return home


# -- yuxu sync ------------------------------------------------


def test_sync_refreshes_bundled_dirs(tmp_path, yuxu_home, capsys):
    proj = tmp_path / "p"
    assert cli_main(["init", str(proj), "--skip-setup"]) == 0

    # Tamper a bundled agent to simulate stale project-side copy
    stamp = "# TAMPERED\n"
    gateway_md = proj / "_system" / "gateway" / "AGENT.md"
    gateway_md.write_text(gateway_md.read_text() + stamp)

    rc = cli_main(["sync", "--project", str(proj)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[yuxu sync]" in out
    # Tamper gone (file freshly overwritten from installed bundled)
    assert stamp not in (proj / "_system" / "gateway" / "AGENT.md").read_text()


def test_sync_leaves_unrelated_dirs_alone(tmp_path, yuxu_home):
    """sync only overwrites bundled-matching dirs; user-added content stays."""
    proj = tmp_path / "p"
    cli_main(["init", str(proj), "--skip-setup"])
    # User drops a custom folder into _system/ (not a bundled name)
    custom = proj / "_system" / "my_custom_agent"
    custom.mkdir()
    (custom / "AGENT.md").write_text("---\n---\n# custom\n")

    cli_main(["sync", "--project", str(proj)])
    # The custom dir stays — sync doesn't nuke unknown content
    assert custom.exists()
    assert (custom / "AGENT.md").exists()


def test_sync_rejects_non_yuxu_dir(tmp_path, capsys):
    rc = cli_main(["sync", "--project", str(tmp_path / "nope")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "yuxu.json" in err


def test_sync_updates_version_file(tmp_path, yuxu_home):
    proj = tmp_path / "p"
    cli_main(["init", str(proj), "--skip-setup"])
    # Manually tamper the recorded version
    (proj / ".yuxu" / "version").write_text("0.0.0-old\n")
    from yuxu import __version__ as ver
    cli_main(["sync", "--project", str(proj)])
    assert (proj / ".yuxu" / "version").read_text(encoding="utf-8").strip() == ver


# -- runtime_monitor internals -------------------------------


def test_slug_from_project_stable_and_unique():
    a = _slug_from_project(Path("/home/a/proj"))
    b = _slug_from_project(Path("/home/a/proj"))
    c = _slug_from_project(Path("/home/b/proj"))
    assert a == b             # stable
    assert a != c             # different path → different slug


def test_pid_alive_true_for_current_process():
    assert _pid_alive(os.getpid()) is True


def test_pid_alive_false_for_dead_pid():
    # 1 is init, always alive; 2^31 - 1 is an unused pid
    assert _pid_alive(2_147_483_000) is False


@pytest.mark.asyncio
async def test_runtime_monitor_registers_and_lists(tmp_path, yuxu_home):
    """RuntimeMonitor writes ~/.yuxu/runtime/<slug>.json + list_entries
    returns our own pid as alive."""
    from types import SimpleNamespace
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "yuxu.json").write_text("{}")
    agent_dir = proj / "_system" / "runtime_monitor"
    agent_dir.mkdir(parents=True)
    ctx = SimpleNamespace(bus=None, agent_dir=agent_dir, loader=None,
                            name="runtime_monitor")
    mon = RuntimeMonitor(ctx, prune_interval=999)
    try:
        mon._register_self()   # skip the prune loop for test determinism
        # File exists on disk
        assert mon._my_file is not None and mon._my_file.exists()
        data = json.loads(mon._my_file.read_text(encoding="utf-8"))
        assert data["pid"] == os.getpid()
        assert data["project_dir"] == str(proj)
        # list_entries sees us as alive
        entries = mon.list_entries()
        assert any(e["pid"] == os.getpid() and e["alive"] for e in entries)
    finally:
        if mon._my_file and mon._my_file.exists():
            mon._my_file.unlink()


@pytest.mark.asyncio
async def test_prune_stale_removes_dead_pid_entries(tmp_path, yuxu_home):
    from types import SimpleNamespace
    rd = yuxu_home / "runtime"
    rd.mkdir(parents=True)
    # write a stale entry (impossible pid)
    (rd / "ghost-deadbeef.json").write_text(json.dumps({
        "pid": 2_147_483_000, "project_dir": "/dead", "started_at": "2026-01-01T00:00:00+00:00",
    }))
    # write a live one (our own pid)
    (rd / "alive-cafebabe.json").write_text(json.dumps({
        "pid": os.getpid(), "project_dir": "/alive", "started_at": "2026-01-01T00:00:00+00:00",
    }))
    proj = tmp_path / "p"; proj.mkdir(); (proj / "yuxu.json").write_text("{}")
    ctx = SimpleNamespace(bus=None, agent_dir=proj, loader=None,
                            name="runtime_monitor")
    mon = RuntimeMonitor(ctx, prune_interval=999)
    removed = mon.prune_stale()
    assert removed == 1
    files = {p.name for p in rd.glob("*.json")}
    assert "ghost-deadbeef.json" not in files
    assert "alive-cafebabe.json" in files


# -- yuxu ps --------------------------------------------------


def test_ps_shows_nothing_when_runtime_empty(yuxu_home, capsys):
    rc = cli_main(["ps"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no" in out.lower() or "runtime" in out.lower()


def test_ps_lists_live_entries(yuxu_home, capsys):
    rd = yuxu_home / "runtime"
    rd.mkdir(parents=True)
    (rd / "me.json").write_text(json.dumps({
        "pid": os.getpid(),
        "project_dir": "/tmp/xyz",
        "started_at": "2026-04-22T12:00:00+00:00",
        "yuxu_version": "0.0.1",
        "adapters": ["console", "telegram"],
    }))
    rc = cli_main(["ps"])
    assert rc == 0
    out = capsys.readouterr().out
    assert str(os.getpid()) in out
    assert "/tmp/xyz" in out


def test_ps_prunes_stale_by_default_but_keeps_with_flag(yuxu_home, capsys):
    rd = yuxu_home / "runtime"
    rd.mkdir(parents=True)
    dead_pid = 2_147_483_000
    (rd / "dead.json").write_text(json.dumps({
        "pid": dead_pid, "project_dir": "/tmp/gone",
        "started_at": "2026-04-22T12:00:00+00:00",
    }))
    # Default: stale gets pruned silently
    cli_main(["ps"])
    assert not (rd / "dead.json").exists()
    # Recreate; --include-stale keeps + shows
    (rd / "dead.json").write_text(json.dumps({
        "pid": dead_pid, "project_dir": "/tmp/gone",
        "started_at": "2026-04-22T12:00:00+00:00",
    }))
    cli_main(["ps", "--include-stale"])
    out = capsys.readouterr().out
    assert "stale" in out
    assert (rd / "dead.json").exists()   # not pruned when --include-stale
