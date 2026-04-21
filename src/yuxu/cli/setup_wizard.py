"""`yuxu setup` — interactive first-run wizard for chat-platform pairing.

Runs automatically at the end of `yuxu init` when stdin is a TTY; can also
be re-run manually (`yuxu setup`). Idempotent: if a platform is already
configured it skips that branch instead of re-asking.

Flow:

  1. Detect existing platform configs in config/secrets/{feishu,telegram}.yaml
  2. If none, ask: 1) Feishu (scan QR) / 2) Telegram (paste token) / 3) Skip
  3. Feishu  -> reuse `yuxu feishu register` (scan-to-create) +
                self-pair the returned open_id into pairings.yaml.
  4. Telegram -> prompt for bot_token, optional allowed_user_ids, save.
                 (Self-pair deferred: telegram doesn't hand us a user_id
                 until the user first messages the bot. Wizard prints the
                 exact follow-up command.)
  5. Skip    -> print a note that `yuxu serve` stdin is still available.

Every interactive prompt is routed through the `ask`/`confirm` callables
so tests can drive the wizard without monkey-patching builtins.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import yaml

from ..bundled.gateway.pairing import DEFAULT_PAIRING_PATH, PairingRegistry


AskFn = Callable[[str], str]
PrintFn = Callable[[str], None]


def _has_feishu_config(project_dir: Path) -> bool:
    p = project_dir / "config" / "secrets" / "feishu.yaml"
    if not p.exists():
        return False
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    return bool(data.get("app_id") and data.get("app_secret"))


def _has_telegram_config(project_dir: Path) -> bool:
    p = project_dir / "config" / "secrets" / "telegram.yaml"
    if not p.exists():
        return False
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    return bool(data.get("bot_token"))


def _write_telegram_yaml(project_dir: Path, *, bot_token: str,
                         allowed_user_ids: Optional[list[int]] = None) -> Path:
    secrets_dir = project_dir / "config" / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    target = secrets_dir / "telegram.yaml"
    payload: dict = {"bot_token": bot_token}
    if allowed_user_ids:
        payload["allowed_user_ids"] = list(allowed_user_ids)
    target.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return target


def _self_pair(project_dir: Path, platform: str, user_id: str,
               note: str = "self-pair via setup") -> Path:
    pairings_path = project_dir / DEFAULT_PAIRING_PATH
    pairings_path.parent.mkdir(parents=True, exist_ok=True)
    reg = PairingRegistry(pairings_path)
    reg.allow(platform, user_id, note=note)
    return pairings_path


def _parse_allowed_ids(raw: str) -> list[int]:
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


def run_setup_wizard(
    project_dir: Path,
    *,
    ask: AskFn = input,
    out: PrintFn = print,
    register_feishu=None,
    interactive: bool = True,
) -> int:
    """Run the setup wizard. Returns 0 on success, non-zero on error.

    If the project already has a platform configured, prints the state and
    returns 0 (nothing to do). When `interactive=False`, only the summary
    path runs — the wizard never prompts.
    """
    project_dir = project_dir.expanduser().resolve()
    if not (project_dir / "yuxu.json").exists():
        out(f"error: {project_dir} is not a yuxu project (no yuxu.json).")
        return 1

    has_fs = _has_feishu_config(project_dir)
    has_tg = _has_telegram_config(project_dir)

    if has_fs or has_tg:
        out("[yuxu setup] Already configured:")
        if has_fs:
            out("  ✓ feishu   (config/secrets/feishu.yaml)")
        if has_tg:
            out("  ✓ telegram (config/secrets/telegram.yaml)")
        out("  (run `yuxu setup --reset` to wipe and reconfigure — TODO)")
        return 0

    if not interactive:
        out("[yuxu setup] No chat platform configured; re-run with a TTY to set one up.")
        return 0

    out("")
    out("[yuxu setup] No chat platform configured yet. Pick one:")
    out("  1) Feishu / Lark  — scan QR (recommended)")
    out("  2) Telegram       — paste bot token")
    out("  3) Skip           — CLI-only chat via `yuxu serve` stdin")
    choice = (ask("Choice [1/2/3, default 3]: ") or "3").strip()

    if choice == "1":
        return _do_feishu(project_dir, ask=ask, out=out,
                          register_feishu=register_feishu)
    if choice == "2":
        return _do_telegram(project_dir, ask=ask, out=out)
    out("[yuxu setup] Skipped. You can `yuxu setup` again anytime.")
    return 0


def _do_feishu(project_dir: Path, *, ask: AskFn, out: PrintFn,
                register_feishu) -> int:
    if register_feishu is None:
        # Import lazily so tests can inject a stub without touching httpx.
        from ..bundled.gateway.adapters.feishu_onboard import register_feishu \
            as _register_feishu
        register_feishu = _register_feishu

    use_lark = (ask("  Use Lark (international) instead of Feishu? [y/N]: ")
                or "").strip().lower().startswith("y")
    domain = "lark" if use_lark else "feishu"
    out(f"[yuxu setup] Starting {domain} onboarding — scan the QR code...")
    creds = register_feishu(initial_domain=domain)
    if not creds:
        out("error: onboarding failed (denied, timed out, or network error).")
        return 1

    # Persist to config/secrets/feishu.yaml (mirror of `yuxu feishu register`).
    secrets_dir = project_dir / "config" / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    target = secrets_dir / "feishu.yaml"
    target.write_text(
        yaml.safe_dump({
            "app_id":       creds["app_id"],
            "app_secret":   creds["app_secret"],
            "domain":       creds["domain"],
            "open_id":      creds.get("open_id"),
            "bot_name":     creds.get("bot_name"),
            "bot_open_id":  creds.get("bot_open_id"),
        }, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    out(f"[yuxu setup] ✓ Feishu credentials saved to {target}")

    # Self-pair the admin so they can immediately talk to the bot.
    open_id = (creds.get("open_id") or "").strip()
    if open_id:
        path = _self_pair(project_dir, "feishu", open_id, note="admin (setup)")
        out(f"[yuxu setup] ✓ Self-paired feishu/{open_id} → {path}")
    else:
        out("[yuxu setup] note: no open_id returned; run "
            "`yuxu pair approve feishu <your_open_id>` after first message.")

    out("[yuxu setup] Next: subscribe the event webhook in Feishu console "
        "(see config/secrets/feishu.yaml keys webhook_host/port/path), "
        "then `yuxu serve`.")
    return 0


def _do_telegram(project_dir: Path, *, ask: AskFn, out: PrintFn) -> int:
    token = ask("  Telegram bot token (from @BotFather): ").strip()
    if not token or ":" not in token:
        out("error: bot token must be of the form '123456:ABC-...'.")
        return 1
    allowed_raw = ask(
        "  Allowed Telegram user_ids (csv, optional; empty = rely on pairing): "
    ).strip()
    allowed = _parse_allowed_ids(allowed_raw) if allowed_raw else []
    target = _write_telegram_yaml(project_dir, bot_token=token,
                                  allowed_user_ids=allowed)
    out(f"[yuxu setup] ✓ Telegram token saved to {target}")

    # Without env, we can't pre-self-pair (no user_id yet).
    out("[yuxu setup] next:")
    out("  1. Run `yuxu serve` (the bot goes online).")
    out("  2. Send any message from YOUR account to the bot in Telegram.")
    out("     The bot will reply with a pairing hint that includes your user_id.")
    out("  3. On the server, run `yuxu pair approve telegram <user_id>`.")
    out("  4. Message again — you're in.")
    return 0
