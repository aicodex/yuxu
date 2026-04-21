"""`yuxu init` → wizard wiring: --skip-setup and non-TTY bypass."""
from __future__ import annotations

from pathlib import Path

import pytest

from yuxu.cli.app import main as cli_main


def test_init_skip_setup_flag_bypasses_wizard(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    # Make stdin look like a TTY so only --skip-setup would dodge the wizard.
    import sys
    class _TTY:
        def isatty(self): return True
    monkeypatch.setattr(sys, "stdin", _TTY())

    # If the wizard did run and tried to prompt, it'd block forever. If this
    # call returns quickly, --skip-setup did its job.
    rc = cli_main(["init", str(tmp_path / "p"), "--skip-setup"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Initialized project at" in out
    # Wizard's banner should NOT appear.
    assert "No chat platform configured yet" not in out


def test_init_non_tty_skips_wizard(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    # Pytest capture already makes stdin non-tty, but be explicit.
    import sys
    class _NotATTY:
        def isatty(self): return False
    monkeypatch.setattr(sys, "stdin", _NotATTY())

    rc = cli_main(["init", str(tmp_path / "p")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Initialized project at" in out
    assert "No chat platform configured yet" not in out


def test_setup_subcommand_non_interactive_reports_status(tmp_path,
                                                           monkeypatch, capsys):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    proj = tmp_path / "p"
    assert cli_main(["init", str(proj), "--skip-setup"]) == 0

    rc = cli_main(["setup", "--project", str(proj), "--non-interactive"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No chat platform configured" in out


def test_setup_subcommand_reports_existing_config(tmp_path, monkeypatch,
                                                    capsys):
    monkeypatch.setenv("YUXU_HOME", str(tmp_path / ".yuxu"))
    proj = tmp_path / "p"
    assert cli_main(["init", str(proj), "--skip-setup"]) == 0
    secrets = proj / "config" / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "telegram.yaml").write_text("bot_token: 1:x\n",
                                             encoding="utf-8")
    rc = cli_main(["setup", "--project", str(proj), "--non-interactive"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Already configured" in out
    assert "telegram" in out
