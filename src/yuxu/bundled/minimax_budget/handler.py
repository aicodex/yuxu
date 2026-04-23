"""MiniMaxBudget — poll `/v1/token_plan/remains` + attribute llm_service
requests per yuxu agent.

**Pure tracker.** No gate-keeping, no forced throttling. Emits soft/hard-cap
events when usage crosses thresholds so upstream (scheduler /
performance_ranker) can decide how to react.

MiniMax quota quirks we honor (from reference_minimax_quota_policy memory):

- Quota measured per REQUEST, not token. We still track tokens locally so
  cross-provider trackers can use a common `estimate` shape later.
- `current_*_total_count == 0` means NO LIMIT (sentinel). Not "zero budget".
- Interval windows are FIXED (not rolling) — see `start_time`/`end_time`.
- `remains_time` is ms until this interval closes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

REMAINS_URL = "https://www.minimaxi.com/v1/token_plan/remains"
MINIMAX_DOMAIN = "minimaxi.com"
DEFAULT_POLL_INTERVAL = 30.0       # seconds between polls per account
DEFAULT_HTTP_TIMEOUT = 10.0
SOFT_CAP_FRAC = 0.80
HARD_CAP_FRAC = 0.95
COMPLETION_TOPIC = "llm_service.request_completed"

# Rolling-window cost estimate tuning
WINDOW_1H_SEC = 3600.0
# Minimum calls in the 1h window before we trust the per-agent mean as a
# prediction. Below this, fall back to cross-agent mean → default.
N_MIN_1H = 3
# Last-resort cost when no history exists at all (fresh install).
DEFAULT_CALL_TOKENS = 2000

# Reservation window matches MiniMax's interval accounting (5h rolling).
# We reset per-agent interval request counters on rollover.
RESERVATION_WINDOW_SEC = 5 * 3600.0


@dataclass
class _Call:
    ts: float
    tokens: int


# -- helpers ----------------------------------------------------


def _is_unlimited(total: Any) -> bool:
    """MiniMax API uses `0` as "no limit" sentinel. Anything else is a cap."""
    try:
        return int(total) == 0
    except (TypeError, ValueError):
        return False


def _cap_fraction(used: Any, total: Any) -> Optional[float]:
    """Return used/total as a float, or None when unlimited or total invalid."""
    try:
        used_i = int(used or 0)
        total_i = int(total or 0)
    except (TypeError, ValueError):
        return None
    if total_i <= 0:
        return None
    return used_i / total_i


def _decode_model_remain(rec: dict) -> dict:
    """Convert a raw model_remains entry into a friendlier yuxu-side view.

    Returns a dict with separate `interval` and `weekly` blocks, each
    carrying `used / total / unlimited / remaining_sec / remaining_fraction`.
    """
    now_ms = int(time.time() * 1000)
    iv_end = int(rec.get("end_time") or 0)
    wk_end = int(rec.get("weekly_end_time") or 0)
    iv_used = int(rec.get("current_interval_usage_count") or 0)
    iv_total = int(rec.get("current_interval_total_count") or 0)
    wk_used = int(rec.get("current_weekly_usage_count") or 0)
    wk_total = int(rec.get("current_weekly_total_count") or 0)
    return {
        "model_name": rec.get("model_name"),
        "interval": {
            "used": iv_used,
            "total": iv_total,
            "unlimited": _is_unlimited(iv_total),
            "remaining_sec": max(0, (iv_end - now_ms) / 1000) if iv_end else None,
            "used_fraction": _cap_fraction(iv_used, iv_total),
            "start_ts": (rec.get("start_time") or 0) / 1000 or None,
            "end_ts": iv_end / 1000 or None,
        },
        "weekly": {
            "used": wk_used,
            "total": wk_total,
            "unlimited": _is_unlimited(wk_total),
            "remaining_sec": max(0, (wk_end - now_ms) / 1000) if wk_end else None,
            "used_fraction": _cap_fraction(wk_used, wk_total),
            "start_ts": (rec.get("weekly_start_time") or 0) / 1000 or None,
            "end_ts": wk_end / 1000 or None,
        },
    }


async def _fetch_remains(client: httpx.AsyncClient, api_key: str,
                         *, url: str = REMAINS_URL,
                         timeout: float = DEFAULT_HTTP_TIMEOUT) -> dict:
    resp = await client.get(
        url,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    # MiniMax returns {base_resp: {status_code, status_msg}, model_remains: [...]}
    base = data.get("base_resp") or {}
    if base.get("status_code", 0) != 0:
        raise RuntimeError(f"MiniMax remains API: {base.get('status_msg')}")
    return data


# -- main class ------------------------------------------------


class MiniMaxBudget:
    def __init__(self, ctx, *,
                 poll_interval: float = DEFAULT_POLL_INTERVAL,
                 http_client: Optional[httpx.AsyncClient] = None,
                 reservations: Optional[dict[str, int]] = None,
                 reservation_window_sec: float = RESERVATION_WINDOW_SEC) -> None:
        self.ctx = ctx
        self.poll_interval = poll_interval
        self._client = http_client
        self._owned_client = http_client is None
        # per-account snapshot cache:
        # { account_id: {"fetched_at": ts, "models": {model_name: decoded}} }
        self._snapshots: dict[str, dict] = {}
        # per-account config: {id, api_key, base_url}
        self._accounts: list[dict] = []
        # local per-agent attribution:
        #   _local[(agent, model)] = {"requests": int, "total_tokens": int,
        #                             "window": deque[_Call]}   # 1h rolling
        # The cumulative `requests` / `total_tokens` stay for ops that want
        # lifetime stats; `window` drives cost-per-call estimation.
        self._local: dict[tuple[str, str], dict] = {}
        # Global cumulative counters — power cross-agent cold-start fallback.
        self._global_calls: int = 0
        self._global_tokens: int = 0
        # Per-agent reservations (requests per interval window). Unset agents
        # have no floor; unlisted agents share whatever's left after honoring
        # others' reservations. See `can_serve(agent)`.
        self._reservations: dict[str, int] = dict(reservations or {})
        self._reservation_window_sec = float(reservation_window_sec)
        self._interval_start_mono: float = time.monotonic()
        self._interval_requests: dict[str, int] = {}
        # track which (account, model) we've already alerted on in the
        # current interval to avoid alert storms
        self._alerted_interval_soft: set[tuple[str, str, int]] = set()
        self._alerted_interval_hard: set[tuple[str, str, int]] = set()
        self._alerted_weekly_soft: set[tuple[str, str, int]] = set()
        self._alerted_weekly_hard: set[tuple[str, str, int]] = set()
        self._poll_task: Optional[asyncio.Task] = None
        self._stopping = False

    # -- setup / teardown ------------------------------------

    def _discover_accounts(self) -> list[dict]:
        """Walk rate_limit_service's config to find MiniMax accounts.

        Looks for accounts whose `base_url` contains `minimaxi.com`. Each
        account yields a dict with id + api_key + base_url."""
        found: list[dict] = []
        rls = self.ctx.get_agent("rate_limit_service")
        if rls is None:
            return found
        # rate_limit_service keeps pools as {pool_name: RateLimitPool} with
        # each pool holding `.accounts` list. The exact shape varies — we
        # duck-type: iterate attrs looking for `accounts` lists of dicts.
        pools = getattr(rls, "pools", None)
        if not isinstance(pools, dict):
            return found
        for pool_name, pool in pools.items():
            accounts = getattr(pool, "accounts", None)
            if not isinstance(accounts, list):
                continue
            for acc in accounts:
                # rate_limit_service uses `Account(id, extra={api_key, base_url, ...})`
                # but accept plain dicts too for test/mock convenience.
                if isinstance(acc, dict):
                    acc_id = acc.get("id")
                    extra = {k: v for k, v in acc.items() if k != "id"}
                else:
                    acc_id = getattr(acc, "id", None)
                    extra = getattr(acc, "extra", None) or {}
                api_key = extra.get("api_key")
                base_url = extra.get("base_url") or ""
                if not api_key or not base_url:
                    continue
                if MINIMAX_DOMAIN not in base_url:
                    continue
                found.append({
                    "id": f"{pool_name}:{acc_id or id(acc)}",
                    "pool": pool_name,
                    "api_key": api_key,
                    "base_url": base_url,
                })
        return found

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
            self._owned_client = True
        return self._client

    async def install(self) -> None:
        self._accounts = self._discover_accounts()
        self.ctx.bus.subscribe(COMPLETION_TOPIC, self._on_llm_completed)
        if self._accounts:
            # prime the cache once synchronously so `snapshot()` works immediately
            await self.refresh()
            self._poll_task = asyncio.create_task(self._poll_loop(),
                                                   name="minimax_budget.poll")
        else:
            log.info("minimax_budget: no MiniMax accounts discovered; "
                     "tracker idle but still accepts per-agent events.")

    async def uninstall(self) -> None:
        self._stopping = True
        try:
            self.ctx.bus.unsubscribe(COMPLETION_TOPIC, self._on_llm_completed)
        except Exception:
            pass
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- polling --------------------------------------------

    async def _poll_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(self.poll_interval)
                if self._stopping:
                    return
                await self.refresh()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("minimax_budget: poll loop iteration failed")

    async def refresh(self, account_id: Optional[str] = None) -> dict:
        """Poll MiniMax's remains API once for each relevant account.

        If account_id is given, refresh only that one (supports `op: refresh`
        from callers that want a fresh read on demand)."""
        results: list[dict] = []
        client = self._get_client()
        for acc in self._accounts:
            if account_id is not None and acc["id"] != account_id:
                continue
            try:
                data = await _fetch_remains(client, acc["api_key"])
            except Exception as e:
                log.warning("minimax_budget: fetch for %s failed: %s",
                            acc["id"], e)
                results.append({"account_id": acc["id"], "ok": False,
                                "error": str(e)})
                continue
            models = {}
            for rec in data.get("model_remains") or []:
                decoded = _decode_model_remain(rec)
                mname = decoded.get("model_name") or "?"
                models[mname] = decoded
                # emit alerts on soft/hard-cap crossings
                self._maybe_alert(acc["id"], mname, decoded)
            self._snapshots[acc["id"]] = {
                "fetched_at": time.time(),
                "models": models,
            }
            results.append({"account_id": acc["id"], "ok": True,
                            "n_models": len(models)})
        return {"ok": True, "refreshed": results}

    def _maybe_alert(self, account_id: str, model_name: str,
                     decoded: dict) -> None:
        iv = decoded.get("interval") or {}
        wk = decoded.get("weekly") or {}
        iv_end = int(decoded.get("interval", {}).get("end_ts") or 0)
        wk_end = int(decoded.get("weekly", {}).get("end_ts") or 0)

        def _check(block: dict, window_end: int, tag: str,
                   soft_set: set, hard_set: set):
            if block.get("unlimited"):
                return
            frac = block.get("used_fraction")
            if frac is None:
                return
            key = (account_id, model_name, window_end)
            if frac >= HARD_CAP_FRAC and key not in hard_set:
                hard_set.add(key)
                soft_set.add(key)  # imply soft too
                self._fire_alert(f"minimax_budget.{tag}_hard_cap",
                                 account_id, model_name, block, frac)
            elif frac >= SOFT_CAP_FRAC and key not in soft_set:
                soft_set.add(key)
                self._fire_alert(f"minimax_budget.{tag}_soft_cap",
                                 account_id, model_name, block, frac)

        _check(iv, iv_end, "interval",
               self._alerted_interval_soft, self._alerted_interval_hard)
        _check(wk, wk_end, "weekly",
               self._alerted_weekly_soft, self._alerted_weekly_hard)

    def _fire_alert(self, topic: str, account_id: str, model_name: str,
                    block: dict, frac: float) -> None:
        # schedule as a task; not critical to await here
        payload = {
            "account_id": account_id,
            "model_name": model_name,
            "used": block.get("used"),
            "total": block.get("total"),
            "used_fraction": frac,
            "remaining_sec": block.get("remaining_sec"),
            "window_end_ts": block.get("end_ts"),
        }
        try:
            asyncio.create_task(self.ctx.bus.publish(topic, payload))
        except Exception:
            log.exception("minimax_budget: failed to publish %s", topic)

    # -- per-agent attribution (subscribes to llm_service) ----

    async def _on_llm_completed(self, event: dict) -> None:
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            return
        agent = payload.get("agent") or "unknown"
        model = payload.get("model") or "?"
        usage = payload.get("usage") or {}
        total_tokens = int(usage.get("total_tokens") or 0)
        key = (agent, model)
        rec = self._local.setdefault(
            key, {"requests": 0, "total_tokens": 0, "window": deque()}
        )
        # Back-compat guard for records created without window (tests that
        # mutate _local directly).
        if "window" not in rec:
            rec["window"] = deque()
        rec["requests"] += 1
        rec["total_tokens"] += total_tokens
        now = time.monotonic()
        dq: deque = rec["window"]
        self._prune_window(dq, now)
        dq.append(_Call(ts=now, tokens=total_tokens))
        self._global_calls += 1
        self._global_tokens += total_tokens
        # Reservation interval counter with rollover
        self._maybe_roll_interval(now)
        self._interval_requests[agent] = \
            self._interval_requests.get(agent, 0) + 1

    # -- reservation window -------------------------------------

    def _maybe_roll_interval(self, now: float) -> bool:
        """Reset per-agent interval counters when the 5h window elapses.

        Returns True if a rollover happened. Time-based (not synced to
        MiniMax's actual start_time yet — phase 5 task). Aligning to the
        authoritative interval end would eliminate the "we reset at a slightly
        different time than the vendor does" drift.
        """
        if now - self._interval_start_mono >= self._reservation_window_sec:
            self._interval_requests.clear()
            self._interval_start_mono = now
            return True
        return False

    def _current_interval_remaining(self) -> Optional[int]:
        """Return the MOST CONSTRAINED interval.remaining across our accounts
        and models, or None if no data / everything unlimited.

        Conservative strategy: if any (account, model) is limited, use its
        remaining as the global floor for reservation math. Unlimited entries
        don't contribute."""
        min_remaining: Optional[int] = None
        for snap in self._snapshots.values():
            models = (snap or {}).get("models") or {}
            for m in models.values():
                iv = (m or {}).get("interval") or {}
                if iv.get("unlimited"):
                    continue
                total = iv.get("total")
                used = iv.get("used")
                if not isinstance(total, int) or not isinstance(used, int):
                    continue
                remaining = max(0, total - used)
                if min_remaining is None or remaining < min_remaining:
                    min_remaining = remaining
        return min_remaining

    def can_serve(self, agent: str) -> dict:
        """Decide whether `agent` is allowed to make a request under the
        current reservation configuration.

        Rules:
        - No reservations set → always True.
        - If agent has its own reservation and agent_interval_used <
          agent_reservation → True (floor guarantee).
        - Else: True iff remaining > Σ(unused reservations of OTHER agents).

        Returns a dict with decision + diagnostics so callers can log why.
        """
        self._maybe_roll_interval(time.monotonic())
        if not self._reservations:
            return {"ok": True, "allowed": True, "reason": "no_reservations",
                    "agent": agent}
        remaining = self._current_interval_remaining()
        if remaining is None:
            # No data or all unlimited → don't gate
            return {"ok": True, "allowed": True, "reason": "no_remaining_data",
                    "agent": agent}
        own_reserved = self._reservations.get(agent, 0)
        own_used = self._interval_requests.get(agent, 0)
        reserved_for_others = sum(
            max(0, cap - self._interval_requests.get(a, 0))
            for a, cap in self._reservations.items()
            if a != agent
        )
        diag = {
            "ok": True,
            "agent": agent,
            "remaining": remaining,
            "own_reserved": own_reserved,
            "own_used_in_interval": own_used,
            "reserved_for_others": reserved_for_others,
        }
        if own_reserved > 0 and own_used < own_reserved:
            return {**diag, "allowed": True, "reason": "own_floor"}
        if remaining > reserved_for_others:
            return {**diag, "allowed": True, "reason": "free_pool"}
        return {**diag, "allowed": False, "reason": "reserved_for_others"}

    # -- cost estimation -----------------------------------------

    @staticmethod
    def _prune_window(dq: deque, now: float) -> None:
        cutoff = now - WINDOW_1H_SEC
        while dq and dq[0].ts < cutoff:
            dq.popleft()

    def _global_mean(self) -> Optional[float]:
        if self._global_calls <= 0:
            return None
        return self._global_tokens / self._global_calls

    def _window_stats(self, key: tuple[str, str],
                      now: Optional[float] = None) -> tuple[int, int]:
        """Return (calls_1h, tokens_1h) for a given (agent, model)."""
        rec = self._local.get(key)
        if not rec:
            return (0, 0)
        dq = rec.get("window")
        if not dq:
            return (0, 0)
        now = now or time.monotonic()
        self._prune_window(dq, now)
        calls = len(dq)
        tokens = sum(c.tokens for c in dq)
        return (calls, tokens)

    def _agent_window_stats(self, agent: str,
                            now: Optional[float] = None) -> tuple[int, int]:
        """Aggregate rolling stats across all models for an agent."""
        now = now or time.monotonic()
        calls = 0
        tokens = 0
        for (a, _m), rec in self._local.items():
            if a != agent:
                continue
            dq = rec.get("window")
            if not dq:
                continue
            self._prune_window(dq, now)
            calls += len(dq)
            tokens += sum(c.tokens for c in dq)
        return (calls, tokens)

    def estimate_cost_per_call(self, agent: str) -> dict:
        """Predict tokens the next call by `agent` will consume.

        Fallback chain:
          1. per-agent 1h mean    (needs ≥ N_MIN_1H samples)
          2. cross-agent global cumulative mean
          3. DEFAULT_CALL_TOKENS constant
        """
        now = time.monotonic()
        calls_1h, tokens_1h = self._agent_window_stats(agent, now)
        if calls_1h >= N_MIN_1H:
            return {
                "value": tokens_1h / calls_1h,
                "source": "per_agent_1h",
                "calls_1h": calls_1h,
                "tokens_1h": tokens_1h,
            }
        gm = self._global_mean()
        if gm is not None:
            return {
                "value": gm,
                "source": "global_mean",
                "calls_1h": calls_1h,
                "tokens_1h": tokens_1h,
                "global_calls": self._global_calls,
                "global_tokens": self._global_tokens,
            }
        return {
            "value": float(DEFAULT_CALL_TOKENS),
            "source": "default",
            "calls_1h": calls_1h,
            "tokens_1h": tokens_1h,
        }

    # -- queries --------------------------------------------

    def snapshot(self, account_id: Optional[str] = None) -> dict:
        if account_id is not None:
            snap = self._snapshots.get(account_id)
            return {"ok": True, "accounts": [
                {"id": account_id,
                 "fetched_at": (snap or {}).get("fetched_at"),
                 "models": list(((snap or {}).get("models") or {}).values())},
            ]}
        accounts = []
        for acc in self._accounts:
            snap = self._snapshots.get(acc["id"])
            accounts.append({
                "id": acc["id"],
                "pool": acc["pool"],
                "fetched_at": (snap or {}).get("fetched_at"),
                "models": list(((snap or {}).get("models") or {}).values()),
            })
        return {"ok": True, "accounts": accounts,
                "poll_interval": self.poll_interval}

    def agent_usage(self, agent: Optional[str] = None) -> dict:
        now = time.monotonic()
        out = []
        for (name, model), rec in sorted(self._local.items()):
            if agent is not None and name != agent:
                continue
            reqs = rec.get("requests") or 0
            toks = rec.get("total_tokens") or 0
            calls_1h, tokens_1h = self._window_stats((name, model), now)
            out.append({
                "agent": name,
                "model": model,
                "requests": reqs,
                "total_tokens": toks,
                "avg_tokens_per_req": (toks / reqs) if reqs else None,
                # Rolling 1h fields (additive, don't replace lifetime)
                "calls_1h": calls_1h,
                "tokens_1h": tokens_1h,
                "avg_tokens_per_req_1h": (
                    (tokens_1h / calls_1h) if calls_1h else None
                ),
            })
        return {"ok": True, "usage": out}

    def estimate(self, *, agent: str,
                 n_requests: Optional[int] = None,
                 n_tokens: Optional[int] = None) -> dict:
        """If `n_requests` given → project token cost using best available
        mean. If `n_tokens` given → project request count needed to burn that
        many tokens (inverse).

        Projection uses the rolling 1h per-agent mean when available, else
        falls back to the cross-agent global mean. See `estimate_cost_per_call`.
        The response carries:
          - `avg_tokens_per_req`: LEGACY all-time agent average (unchanged)
          - `cost_per_call` + `cost_source`: NEW rolling-first estimate
          - projections use `cost_per_call` as the multiplier
        """
        # aggregate this agent's all-time history across models (legacy)
        total_reqs = 0
        total_toks = 0
        for (name, _m), rec in self._local.items():
            if name != agent:
                continue
            total_reqs += rec.get("requests") or 0
            total_toks += rec.get("total_tokens") or 0
        avg = (total_toks / total_reqs) if total_reqs else None

        cost = self.estimate_cost_per_call(agent)
        cpc = cost["value"]

        out: dict[str, Any] = {
            "ok": True,
            "agent": agent,
            "history_requests": total_reqs,
            "history_tokens": total_toks,
            "avg_tokens_per_req": avg,
            "cost_per_call": cpc,
            "cost_source": cost["source"],
            "calls_1h": cost["calls_1h"],
            "tokens_1h": cost["tokens_1h"],
        }
        if n_requests is not None:
            out["projected_requests"] = int(n_requests)
            out["projected_tokens"] = int(round(cpc * n_requests))
        elif n_tokens is not None:
            out["projected_tokens"] = int(n_tokens)
            out["projected_requests"] = (
                int(round(n_tokens / cpc)) if cpc > 0 else None
            )
        return out

    def reset_local(self, agent: Optional[str] = None) -> dict:
        if agent is None:
            self._local.clear()
            self._global_calls = 0
            self._global_tokens = 0
            return {"ok": True, "cleared": "all"}
        before = len(self._local)
        self._local = {k: v for k, v in self._local.items() if k[0] != agent}
        return {"ok": True, "cleared": before - len(self._local)}

    # -- bus surface ----------------------------------------

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op", "snapshot")
        try:
            if op == "snapshot":
                return self.snapshot(payload.get("account_id"))
            if op == "agent_usage":
                return self.agent_usage(payload.get("agent"))
            if op == "estimate":
                agent = payload.get("agent")
                if not agent:
                    return {"ok": False, "error": "missing field: agent"}
                return self.estimate(
                    agent=agent,
                    n_requests=payload.get("n_requests"),
                    n_tokens=payload.get("n_tokens"),
                )
            if op == "cost_per_call":
                # Lightweight hot-path query for rate_limit / llm_driver.
                agent = payload.get("agent")
                if not agent:
                    return {"ok": False, "error": "missing field: agent"}
                return {"ok": True, "agent": agent,
                        **self.estimate_cost_per_call(agent)}
            if op == "can_serve":
                agent = payload.get("agent")
                if not agent:
                    return {"ok": False, "error": "missing field: agent"}
                return self.can_serve(agent)
            if op == "refresh":
                return await self.refresh(payload.get("account_id"))
            if op == "reset_local":
                return self.reset_local(payload.get("agent"))
            return {"ok": False, "error": f"unknown op: {op!r}"}
        except (TypeError, ValueError, KeyError) as e:
            return {"ok": False, "error": f"bad request: {e}"}
