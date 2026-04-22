"""`yuxu setup` wizard — platform selection, token/QR persistence, self-pair."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from yuxu.bundled.gateway.pairing import DEFAULT_PAIRING_PATH, PairingRegistry
from yuxu.cli.setup_wizard import run_setup_wizard
from yuxu.skills_bundled.create_project.handler import create_project


def _make_project(tmp_path: Path) -> Path:
    return create_project(tmp_path / "p")


class _Answers:
    """Canned-answer ask() replacement."""
    def __init__(self, *answers: str) -> None:
        self._iter = iter(answers)

    def __call__(self, prompt: str = "") -> str:
        try:
            return next(self._iter)
        except StopIteration:
            return ""


class _Capture:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, s: str = "") -> None:
        self.lines.append(str(s))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def test_wizard_rejects_non_yuxu_directory(tmp_path):
    cap = _Capture()
    rc = run_setup_wizard(tmp_path / "nope", ask=_Answers(), out=cap,
                          interactive=True)
    assert rc == 1
    assert "not a yuxu project" in cap.text


def test_wizard_non_interactive_without_config_is_noop(tmp_path):
    project = _make_project(tmp_path)
    cap = _Capture()
    rc = run_setup_wizard(project, ask=_Answers(), out=cap,
                          interactive=False)
    assert rc == 0
    assert "No chat platform configured" in cap.text


def test_wizard_reports_existing_feishu_and_exits(tmp_path):
    project = _make_project(tmp_path)
    (project / "config" / "secrets").mkdir(parents=True, exist_ok=True)
    (project / "config" / "secrets" / "feishu.yaml").write_text(
        "app_id: cli_x\napp_secret: s\ndomain: feishu\n", encoding="utf-8",
    )
    cap = _Capture()
    rc = run_setup_wizard(project, ask=_Answers(), out=cap)
    assert rc == 0
    assert "Already configured" in cap.text
    assert "✓ feishu" in cap.text


def test_wizard_skip_choice(tmp_path):
    project = _make_project(tmp_path)
    cap = _Capture()
    rc = run_setup_wizard(project, ask=_Answers("3"), out=cap)
    assert rc == 0
    assert "Skipped" in cap.text
    # No platform files written.
    assert not (project / "config" / "secrets" / "telegram.yaml").exists()
    assert not (project / "config" / "secrets" / "feishu.yaml").exists()


def test_wizard_telegram_happy_path(tmp_path):
    project = _make_project(tmp_path)
    cap = _Capture()
    rc = run_setup_wizard(
        project,
        ask=_Answers("2", "123456:ABCdef", "42,43"),
        out=cap,
    )
    assert rc == 0
    tg_yaml = project / "config" / "secrets" / "telegram.yaml"
    assert tg_yaml.exists()
    data = yaml.safe_load(tg_yaml.read_text(encoding="utf-8"))
    assert data["bot_token"] == "123456:ABCdef"
    assert data["allowed_user_ids"] == [42, 43]
    assert "yuxu pair approve telegram" in cap.text  # onboarding hint


def test_wizard_telegram_rejects_obviously_bad_token(tmp_path):
    project = _make_project(tmp_path)
    cap = _Capture()
    rc = run_setup_wizard(project, ask=_Answers("2", "not-a-token", ""),
                          out=cap)
    assert rc == 1
    assert not (project / "config" / "secrets" / "telegram.yaml").exists()


def test_wizard_feishu_happy_path_self_pairs(tmp_path):
    project = _make_project(tmp_path)

    def fake_register_feishu(*, initial_domain="feishu", **_):
        return {
            "app_id": "cli_stub",
            "app_secret": "secret",
            "domain": initial_domain,
            "open_id": "ou_admin",
            "bot_name": "yuxu-bot",
            "bot_open_id": "ou_bot",
        }

    cap = _Capture()
    rc = run_setup_wizard(
        project,
        ask=_Answers("1", "n"),  # "1" = feishu, "n" = not Lark
        out=cap,
        register_feishu=fake_register_feishu,
    )
    assert rc == 0
    fs_yaml = project / "config" / "secrets" / "feishu.yaml"
    assert fs_yaml.exists()
    data = yaml.safe_load(fs_yaml.read_text(encoding="utf-8"))
    assert data["app_id"] == "cli_stub"
    assert data["open_id"] == "ou_admin"

    # self-pair should have populated pairings.yaml
    reg = PairingRegistry(project / DEFAULT_PAIRING_PATH)
    assert reg.is_allowed("feishu", "ou_admin")
    assert "Self-paired" in cap.text


def test_wizard_feishu_onboard_failure(tmp_path):
    project = _make_project(tmp_path)

    def fake_register_feishu(*, initial_domain="feishu", **_):
        return None

    cap = _Capture()
    rc = run_setup_wizard(project, ask=_Answers("1", "n"), out=cap,
                          register_feishu=fake_register_feishu)
    assert rc == 1
    assert "onboarding failed" in cap.text
