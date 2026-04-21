"""RateLimitService — named-pool concurrency + RPM limiter with account rotation.

TPM / daily_quota are not implemented in this MVP; fields are accepted
silently so future upgrades don't break config compatibility.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

VALID_STRATEGIES = ("least_load", "round_robin")


@dataclass
class Account:
    id: str
    extra: dict = field(default_factory=dict)
    concurrent: int = 0
    call_times: deque = field(default_factory=deque)


@dataclass
class Pool:
    name: str
    max_concurrent: Optional[int] = None
    rpm: Optional[int] = None
    strategy: str = "least_load"
    acquire_timeout: float = 60.0
    accounts: list[Account] = field(default_factory=list)
    rr_cursor: int = 0
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)


class RateLimitService:
    RPM_WINDOW_SEC = 60.0

    def __init__(self, config: dict) -> None:
        self.pools: dict[str, Pool] = {}
        self._load_config(config or {})

    def _load_config(self, cfg: dict) -> None:
        for name, settings in cfg.items():
            if not isinstance(settings, dict):
                log.warning("rate_limit_service: pool %s ignored (bad config)", name)
                continue
            accounts_cfg = settings.get("accounts") or [{"id": "default"}]
            accounts: list[Account] = []
            for a in accounts_cfg:
                if not isinstance(a, dict) or "id" not in a:
                    log.warning("rate_limit_service: account in %s missing id, skipping", name)
                    continue
                extra = {k: v for k, v in a.items() if k != "id"}
                accounts.append(Account(id=a["id"], extra=extra))
            if not accounts:
                log.warning("rate_limit_service: pool %s has no accounts", name)
                continue
            strategy = settings.get("strategy", "least_load")
            if strategy not in VALID_STRATEGIES:
                log.warning("rate_limit_service: pool %s invalid strategy=%s, using least_load",
                            name, strategy)
                strategy = "least_load"
            self.pools[name] = Pool(
                name=name,
                max_concurrent=settings.get("max_concurrent"),
                rpm=settings.get("rpm"),
                strategy=strategy,
                acquire_timeout=float(settings.get("acquire_timeout", 60.0)),
                accounts=accounts,
            )

    def _prune(self, acc: Account, now: float) -> None:
        cutoff = now - self.RPM_WINDOW_SEC
        while acc.call_times and acc.call_times[0] < cutoff:
            acc.call_times.popleft()

    def _pick_account(self, pool: Pool) -> Account:
        if pool.strategy == "round_robin":
            acc = pool.accounts[pool.rr_cursor % len(pool.accounts)]
            pool.rr_cursor += 1
            return acc
        return min(pool.accounts, key=lambda a: a.concurrent)

    def _eligible(self, pool: Pool, acc: Account, now: float) -> tuple[bool, float]:
        """Return (allowed, wait_hint_seconds)."""
        self._prune(acc, now)
        if pool.max_concurrent is not None and acc.concurrent >= pool.max_concurrent:
            return False, 0.5  # no timestamp to derive wait from; poll
        if pool.rpm is not None and len(acc.call_times) >= pool.rpm:
            oldest = acc.call_times[0]
            wait = max(0.01, self.RPM_WINDOW_SEC - (now - oldest) + 0.01)
            return False, wait
        return True, 0.0

    @asynccontextmanager
    async def acquire(self, pool_name: str, tokens: int = 1,
                      timeout: Optional[float] = None):
        if pool_name not in self.pools:
            raise KeyError(f"unknown rate-limit pool: {pool_name!r}")
        pool = self.pools[pool_name]
        deadline = time.monotonic() + (timeout if timeout is not None else pool.acquire_timeout)

        acc: Optional[Account] = None
        async with pool.cond:
            while True:
                now = time.monotonic()
                # Pick account each pass; under contention least_load may shift.
                candidate = self._pick_account(pool)
                allowed, wait_hint = self._eligible(pool, candidate, now)
                if allowed:
                    candidate.concurrent += 1
                    candidate.call_times.append(now)
                    acc = candidate
                    break
                remaining = deadline - now
                if remaining <= 0:
                    raise asyncio.TimeoutError(
                        f"rate_limit acquire timeout for pool={pool_name}"
                    )
                wait_t = min(remaining, max(0.05, wait_hint))
                try:
                    await asyncio.wait_for(pool.cond.wait(), timeout=wait_t)
                except asyncio.TimeoutError:
                    pass  # loop re-checks state
        try:
            yield {"pool": pool_name, "account": acc.id, "extra": acc.extra, "tokens": tokens}
        finally:
            async with pool.cond:
                acc.concurrent -= 1
                pool.cond.notify_all()

    def snapshot(self) -> dict:
        now = time.monotonic()
        out: dict = {}
        for name, pool in self.pools.items():
            accs = []
            for a in pool.accounts:
                self._prune(a, now)
                accs.append({
                    "id": a.id,
                    "concurrent": a.concurrent,
                    "calls_1m": len(a.call_times),
                })
            out[name] = {
                "max_concurrent": pool.max_concurrent,
                "rpm": pool.rpm,
                "strategy": pool.strategy,
                "accounts": accs,
            }
        return out

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op")
        if op == "status":
            return {"ok": True, "pools": self.snapshot()}
        return {"ok": False, "error": f"unknown op: {op!r}"}
