"""RateLimitService — named-pool concurrency + RPM limiter with account rotation.

v0.2 adds weighted fair queuing (DWRR) and a retry priority lane:

- Acquire callers may pass `agent` (identity) + `cost_hint` (expected tokens
  for this call, e.g. from minimax_budget.estimate_cost_per_call) and
  `priority` ("normal" | "retry").
- Pool config may declare `weights: {agent: int}` to set per-agent weights.
  Unlisted agents default to weight 1.
- When multiple waiters queue for a slot:
    retry waiters (FIFO)  →  weighted waiters (DWRR)
  Retry has absolute priority; within weighted, deficit-weighted round-robin
  picks the agent currently most "owed".
- Retries never debit the deficit (their first attempt already errored out
  without consuming tokens). Callers debit on SUCCESS by mutating
  `handle["actual_cost"]` inside the `async with` block; otherwise no debit.

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
VALID_PRIORITIES = ("normal", "retry")
ANON_AGENT = "_anon"


@dataclass
class Account:
    id: str
    extra: dict = field(default_factory=dict)
    concurrent: int = 0
    call_times: deque = field(default_factory=deque)


@dataclass
class Waiter:
    agent: str             # "_anon" if caller didn't pass one
    cost_hint: float       # used on successful release if caller didn't override
    priority: str          # "normal" | "retry"


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
    # v0.2: weighted admission
    weights: dict[str, int] = field(default_factory=dict)
    retry_waiters: deque = field(default_factory=deque)        # deque[Waiter]
    weighted_waiters: deque = field(default_factory=deque)     # deque[Waiter]
    credits: dict[str, float] = field(default_factory=dict)    # agent → remaining DWRR credit
    # Monotonic cumulative successful-call cost per agent. Useful for
    # observability + tests that need an invariant "failed calls don't count".
    consumed: dict[str, float] = field(default_factory=dict)


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
            weights_cfg = settings.get("weights") or {}
            if not isinstance(weights_cfg, dict):
                log.warning("rate_limit_service: pool %s weights must be dict, ignoring",
                            name)
                weights_cfg = {}
            weights = {str(k): int(v) for k, v in weights_cfg.items()
                       if isinstance(v, (int, float)) and v > 0}
            self.pools[name] = Pool(
                name=name,
                max_concurrent=settings.get("max_concurrent"),
                rpm=settings.get("rpm"),
                strategy=strategy,
                acquire_timeout=float(settings.get("acquire_timeout", 60.0)),
                accounts=accounts,
                weights=weights,
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

    # -- waiter queue + DWRR scheduling --------------------------

    def _weight_of(self, pool: Pool, agent: str) -> int:
        """Agents with explicit weight use it; others default to 1. `_anon`
        also gets weight 1 (acts as a single indistinct agent). "retry" lane
        bypasses weighting entirely."""
        return int(pool.weights.get(agent, 1))

    def _is_my_turn(self, pool: Pool, me: Waiter) -> bool:
        """Is it `me`'s turn to attempt acquisition, given the current queue?

        Ordering:
          1. retry waiters (FIFO) — absolute priority
          2. weighted waiters — DWRR by agent credit; FIFO within same agent
        """
        if me.priority == "retry":
            return bool(pool.retry_waiters) and pool.retry_waiters[0] is me
        # Normal: any retry waiter blocks us
        if pool.retry_waiters:
            return False
        if not pool.weighted_waiters:
            return False  # shouldn't happen if we're queued
        waiting_agents = list({w.agent for w in pool.weighted_waiters})
        # If every waiting agent has non-positive credit, refill by +weight
        # for each. Classic DWRR quantum bump.
        if all(pool.credits.get(a, 0.0) <= 0.0 for a in waiting_agents):
            for a in waiting_agents:
                pool.credits[a] = (pool.credits.get(a, 0.0)
                                   + float(self._weight_of(pool, a)))
        # Pick agent with highest credit. Deterministic tie-break by name
        # so multi-refill rounds don't thrash.
        best = max(waiting_agents,
                   key=lambda a: (pool.credits.get(a, 0.0), a))
        # Within that agent, FIFO
        for w in pool.weighted_waiters:
            if w.agent == best:
                return w is me
        return False

    def _dequeue(self, pool: Pool, waiter: Waiter) -> None:
        try:
            pool.retry_waiters.remove(waiter)
            return
        except ValueError:
            pass
        try:
            pool.weighted_waiters.remove(waiter)
        except ValueError:
            pass

    @asynccontextmanager
    async def acquire(self, pool_name: str, tokens: int = 1,
                      timeout: Optional[float] = None, *,
                      agent: Optional[str] = None,
                      cost_hint: Optional[float] = None,
                      priority: str = "normal"):
        if pool_name not in self.pools:
            raise KeyError(f"unknown rate-limit pool: {pool_name!r}")
        pool = self.pools[pool_name]
        if priority not in VALID_PRIORITIES:
            raise ValueError(f"invalid priority {priority!r}; must be one of {VALID_PRIORITIES}")
        deadline = time.monotonic() + (timeout if timeout is not None else pool.acquire_timeout)
        agent_key = agent or ANON_AGENT
        cost = float(cost_hint) if cost_hint is not None else 0.0
        waiter = Waiter(agent=agent_key, cost_hint=cost, priority=priority)

        acc: Optional[Account] = None
        async with pool.cond:
            if priority == "retry":
                pool.retry_waiters.append(waiter)
            else:
                pool.weighted_waiters.append(waiter)
            try:
                while True:
                    now = time.monotonic()
                    my_turn = self._is_my_turn(pool, waiter)
                    wait_hint = 0.5
                    if my_turn:
                        candidate = self._pick_account(pool)
                        allowed, wait_hint = self._eligible(pool, candidate, now)
                        if allowed:
                            candidate.concurrent += 1
                            candidate.call_times.append(now)
                            acc = candidate
                            self._dequeue(pool, waiter)
                            break
                    remaining = deadline - now
                    if remaining <= 0:
                        self._dequeue(pool, waiter)
                        raise asyncio.TimeoutError(
                            f"rate_limit acquire timeout for pool={pool_name}"
                        )
                    wait_t = min(remaining, max(0.05, wait_hint))
                    try:
                        await asyncio.wait_for(pool.cond.wait(), timeout=wait_t)
                    except asyncio.TimeoutError:
                        pass  # re-check state
            except BaseException:
                self._dequeue(pool, waiter)
                raise

        try:
            handle = {
                "pool": pool_name, "account": acc.id, "extra": acc.extra,
                "tokens": tokens,
                # v0.2: identity + cost feedback
                "agent": agent_key, "priority": priority,
                "cost_hint": cost, "actual_cost": None,
            }
            yield handle
        finally:
            async with pool.cond:
                acc.concurrent -= 1
                # Debit deficit on successful completion only. Caller signals
                # success by setting `handle["actual_cost"]` to the real
                # tokens used. Retries never debit — the first attempt is
                # what "spent" the turn; a retry is bookkeeping.
                # Debit deficit on successful completion only. Caller signals
                # success by setting `handle["actual_cost"]` to the real
                # tokens used. Failures leave it None → no debit (the whole
                # reason for retry priority).
                # NOTE: retries DO debit on success — each logical call that
                # produces tokens should count exactly once. Priority lane
                # only affects admission order, not accounting.
                actual = handle.get("actual_cost")
                if actual is not None and agent_key != ANON_AGENT:
                    pool.credits[agent_key] = (pool.credits.get(agent_key, 0.0)
                                               - float(actual))
                    pool.consumed[agent_key] = (pool.consumed.get(agent_key, 0.0)
                                                + float(actual))
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
                "weights": dict(pool.weights),
                "credits": dict(pool.credits),
                "retry_waiters": len(pool.retry_waiters),
                "weighted_waiters": len(pool.weighted_waiters),
                "accounts": accs,
            }
        return out

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op")
        if op == "status":
            return {"ok": True, "pools": self.snapshot()}
        return {"ok": False, "error": f"unknown op: {op!r}"}
