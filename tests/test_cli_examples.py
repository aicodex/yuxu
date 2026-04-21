"""`yuxu examples list / install` CLI coverage."""
from __future__ import annotations

from pathlib import Path

import pytest

from yuxu.cli.app import main as cli_main


def test_examples_list(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    rc = cli_main(["examples", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "echo_bot" in out


def test_examples_install_into_project(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    proj = tmp_path / "proj"
    rc_init = cli_main(["init", str(proj)])
    assert rc_init == 0

    rc_install = cli_main(["examples", "install", "echo_bot",
                            "--project", str(proj)])
    assert rc_install == 0
    dest = proj / "agents" / "echo_bot"
    assert (dest / "AGENT.md").exists()
    assert (dest / "__init__.py").exists()
    assert (dest / "handler.py").exists()


def test_examples_install_unknown_name(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    proj = tmp_path / "proj"
    cli_main(["init", str(proj)])
    rc = cli_main(["examples", "install", "nope",
                    "--project", str(proj)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no such example" in err


def test_examples_install_outside_yuxu_project(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    # Target has no yuxu.json
    rc = cli_main(["examples", "install", "echo_bot",
                    "--project", str(tmp_path / "random")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not a yuxu project" in err


def test_examples_install_refuses_overwrite_without_force(tmp_path, monkeypatch):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    proj = tmp_path / "proj"
    cli_main(["init", str(proj)])
    cli_main(["examples", "install", "echo_bot", "--project", str(proj)])
    rc = cli_main(["examples", "install", "echo_bot", "--project", str(proj)])
    assert rc == 1


def test_examples_install_force_overwrites(tmp_path, monkeypatch):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    proj = tmp_path / "proj"
    cli_main(["init", str(proj)])
    cli_main(["examples", "install", "echo_bot", "--project", str(proj)])
    rc = cli_main(["examples", "install", "echo_bot",
                    "--project", str(proj), "--force"])
    assert rc == 0
