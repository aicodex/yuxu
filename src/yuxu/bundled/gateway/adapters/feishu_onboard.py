"""Feishu / Lark scan-to-create onboarding (QR device-code flow).

Direct port of Hermes' `gateway/platforms/feishu.py:qr_register` to yuxu.
Rather than requiring the user to manually create a bot app in Feishu's
admin console, this calls Feishu's registration endpoint which returns a
`device_code` + `verification_uri_complete`. We render that URL as a QR
in the terminal; the user scans it in the Feishu / Lark mobile app and
Feishu creates a fresh bot application + returns its `app_id` / `app_secret`.

Usage:
    creds = register_feishu()  # blocks; prints QR; polls
    if creds:
        write to config/secrets/feishu.yaml and restart `yuxu serve`

This is a **CLI-time sync flow** — not run inside the daemon. Uses stdlib
`urllib` only (no new deps). `qrcode` is imported lazily; if absent we
print the URL instead.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

ACCOUNTS_URLS = {
    "feishu": "https://accounts.feishu.cn",
    "lark":   "https://accounts.larksuite.com",
}
OPEN_URLS = {
    "feishu": "https://open.feishu.cn",
    "lark":   "https://open.larksuite.com",
}
REGISTRATION_PATH = "/oauth/v1/app/registration"
REQUEST_TIMEOUT_S = 10.0


# -- low-level HTTP -------------------------------------------


def _post_registration(base_url: str, body: dict[str, str]) -> dict:
    """POST form-encoded to {base}/oauth/v1/app/registration.

    Endpoint returns JSON even on 4xx (e.g. `authorization_pending` is a
    polling state returned as 400). We always try to parse the body.
    """
    url = f"{base_url}{REGISTRATION_PATH}"
    data = urlencode(body).encode("utf-8")
    req = Request(url, data=data,
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body_bytes = exc.read()
        if body_bytes:
            try:
                return json.loads(body_bytes.decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                raise exc from None
        raise


# -- stages: init, begin, poll --------------------------------


def _init_registration(domain: str = "feishu") -> None:
    """Check the environment supports client_secret auth. Raises if not."""
    res = _post_registration(ACCOUNTS_URLS[domain], {"action": "init"})
    methods = res.get("supported_auth_methods") or []
    if "client_secret" not in methods:
        raise RuntimeError(
            f"Feishu / Lark registration does not support client_secret auth. "
            f"Supported: {methods}"
        )


def _begin_registration(domain: str = "feishu") -> dict:
    """Kick off the device-code flow. Returns device_code, qr_url, etc."""
    res = _post_registration(ACCOUNTS_URLS[domain], {
        "action": "begin",
        "archetype": "PersonalAgent",
        "auth_method": "client_secret",
        "request_user_info": "open_id",
    })
    device_code = res.get("device_code")
    if not device_code:
        raise RuntimeError("Feishu / Lark did not return a device_code")
    qr_url = res.get("verification_uri_complete", "")
    # Branding params (optional; Feishu accepts unknown query params)
    if "?" in qr_url:
        qr_url += "&from=yuxu&tp=yuxu"
    elif qr_url:
        qr_url += "?from=yuxu&tp=yuxu"
    return {
        "device_code": device_code,
        "qr_url": qr_url,
        "user_code": res.get("user_code", ""),
        "interval": int(res.get("interval") or 5),
        "expire_in": int(res.get("expire_in") or 600),
    }


def _poll_registration(*, device_code: str, interval: int, expire_in: int,
                       domain: str = "feishu",
                       on_dot: Optional[callable] = None) -> Optional[dict]:
    """Poll until user scans + approves, or timeout / denial.

    On success returns {app_id, app_secret, domain, open_id}.
    On terminal failure (denied, expired, timeout) returns None.
    Network / JSON glitches: retry silently.
    """
    deadline = time.time() + expire_in
    current_domain = domain
    domain_switched = False
    poll_count = 0

    while time.time() < deadline:
        try:
            res = _post_registration(ACCOUNTS_URLS[current_domain], {
                "action": "poll",
                "device_code": device_code,
                "tp": "ob_app",
            })
        except (URLError, OSError, json.JSONDecodeError):
            time.sleep(interval)
            continue

        poll_count += 1
        if on_dot is not None:
            try:
                on_dot(poll_count)
            except Exception:
                pass

        # Domain auto-detect: tenant may live on Lark even if we started with feishu
        user_info = res.get("user_info") or {}
        if user_info.get("tenant_brand") == "lark" and not domain_switched:
            current_domain = "lark"
            domain_switched = True
            # fall through — this same response may already hold credentials

        if res.get("client_id") and res.get("client_secret"):
            return {
                "app_id": res["client_id"],
                "app_secret": res["client_secret"],
                "domain": current_domain,
                "open_id": user_info.get("open_id"),
            }

        error = res.get("error", "")
        if error in ("access_denied", "expired_token"):
            log.warning("feishu onboarding: %s", error)
            return None

        time.sleep(interval)

    log.warning("feishu onboarding: poll timed out after %ds", expire_in)
    return None


# -- terminal QR rendering ------------------------------------


def render_qr(url: str) -> bool:
    """Draw a QR code in the current terminal. Returns False if not possible
    (e.g. `qrcode` library not installed)."""
    try:
        import qrcode  # type: ignore
    except ImportError:
        return False
    try:
        q = qrcode.QRCode()
        q.add_data(url)
        q.make(fit=True)
        q.print_ascii(invert=True)
        return True
    except Exception:
        log.exception("render_qr failed")
        return False


# -- optional bot probe ---------------------------------------


def probe_bot(app_id: str, app_secret: str, domain: str) -> Optional[dict]:
    """After registration, fetch bot name + open_id as a sanity check.

    Uses /open-apis/auth/v3/tenant_access_token/internal + /open-apis/bot/v3/info.
    Best-effort — returns None on any error.
    """
    base = OPEN_URLS.get(domain, OPEN_URLS["feishu"])
    try:
        # 1. get tenant token
        tok_req = Request(
            f"{base}/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(tok_req, timeout=REQUEST_TIMEOUT_S) as resp:
            tok_res = json.loads(resp.read().decode("utf-8"))
        access_token = tok_res.get("tenant_access_token")
        if not access_token:
            return None
        # 2. probe bot
        bot_req = Request(
            f"{base}/open-apis/bot/v3/info",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        with urlopen(bot_req, timeout=REQUEST_TIMEOUT_S) as resp:
            bot_res = json.loads(resp.read().decode("utf-8"))
    except (URLError, OSError, KeyError, json.JSONDecodeError):
        return None
    if bot_res.get("code") != 0:
        return None
    bot = bot_res.get("bot") or bot_res.get("data", {}).get("bot") or {}
    return {
        "bot_name": bot.get("bot_name"),
        "bot_open_id": bot.get("open_id"),
    }


# -- top-level orchestrator -----------------------------------


def register_feishu(*, initial_domain: str = "feishu",
                    timeout_seconds: int = 600,
                    quiet: bool = False) -> Optional[dict]:
    """Run the full scan-to-create flow. Returns credentials dict or None.

    On success:
        {app_id, app_secret, domain, open_id, bot_name, bot_open_id}
    """
    try:
        return _register_feishu_inner(
            initial_domain=initial_domain,
            timeout_seconds=timeout_seconds,
            quiet=quiet,
        )
    except (RuntimeError, URLError, OSError, json.JSONDecodeError) as e:
        log.warning("feishu onboarding failed: %s", e)
        return None


def _register_feishu_inner(*, initial_domain: str, timeout_seconds: int,
                            quiet: bool) -> Optional[dict]:
    _out = (lambda *a, **k: None) if quiet else print

    _out("  Connecting to Feishu / Lark...", end="", flush=True)
    _init_registration(initial_domain)
    begin = _begin_registration(initial_domain)
    _out(" done.")

    _out()
    qr_url = begin["qr_url"]
    if not quiet:
        if render_qr(qr_url):
            _out(f"\n  Scan the QR above, or open this URL on your phone:\n"
                 f"  {qr_url}\n")
        else:
            _out(f"  Open this URL on your phone (Feishu / Lark app):\n\n"
                 f"  {qr_url}\n")
            _out("  Tip: `pip install qrcode` for a scannable QR next time.\n")

    def _on_dot(n: int) -> None:
        if quiet:
            return
        if n == 1:
            print("  Waiting for scan...", end="", flush=True)
        elif n % 6 == 0:
            print(".", end="", flush=True)

    result = _poll_registration(
        device_code=begin["device_code"],
        interval=begin["interval"],
        expire_in=min(begin["expire_in"], timeout_seconds),
        domain=initial_domain,
        on_dot=_on_dot,
    )
    if not quiet:
        print()
    if not result:
        return None

    bot = probe_bot(result["app_id"], result["app_secret"], result["domain"])
    result["bot_name"] = bot["bot_name"] if bot else None
    result["bot_open_id"] = bot["bot_open_id"] if bot else None
    return result
