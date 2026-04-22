"""MiniMaxBudget — poll /token_plan/remains + per-agent attribution."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from yuxu.bundled.minimax_budget.handler import (
    COMPLETION_TOPIC,
    HARD_CAP_FRAC,
    MiniMaxBudget,
    SOFT_CAP_FRAC,
    _cap_fraction,
    _decode_model_remain,
    _is_unlimited,
)
from yuxu.core.bus import Bus

pytestmark = pytest.mark.asyncio


# -- pure helpers ----------------------------------------------


def test_is_unlimited_sentinel():
    assert _is_unlimited(0) is True
    assert _is_unlimited("0") is True
    assert _is_unlimited(1) is False
    assert _is_unlimited(None) is False
    assert _is_unlimited("abc") is False


def test_cap_fraction_handles_zero_total_as_none():
    assert _cap_fraction(10, 0) is None  # unlimited → None
    assert _cap_fraction(10, 100) == pytest.approx(0.1)
    assert _cap_fraction(None, 100) == 0.0
    assert _cap_fraction(50, None) is None


def test_decode_model_remain_shape():
    # Mirror key 1 (MiniMax-M* with weekly cap)
    rec = {
        "start_time": 1776841200000, "end_time": 1776859200000,
        "current_interval_total_count": 4500,
        "current_interval_usage_count": 31,
        "model_name": "MiniMax-M*",
        "current_weekly_total_count": 45000,
        "current_weekly_usage_count": 749,
        "weekly_start_time": 1776614400000,
        "weekly_end_time": 1777219200000,
    }
    d = _decode_model_remain(rec)
    assert d["model_name"] == "MiniMax-M*"
    iv = d["interval"]
    assert iv["used"] == 31
    assert iv["total"] == 4500
    assert iv["unlimited"] is False
    assert 0 < iv["used_fraction"] < 0.01
    wk = d["weekly"]
    assert wk["total"] == 45000
    assert wk["unlimited"] is False


def test_decode_model_remain_unlimited_weekly():
    # Mirror key 2 (weekly_total_count = 0 = no weekly cap)
    rec = {
        "start_time": 1776841200000, "end_time": 1776859200000,
        "current_interval_total_count": 4500,
        "current_interval_usage_count": 0,
        "model_name": "MiniMax-M*",
        "current_weekly_total_count": 0,  # SENTINEL: no limit
        "current_weekly_usage_count": 0,
        "weekly_start_time": 1776614400000,
        "weekly_end_time": 1777219200000,
    }
    d = _decode_model_remain(rec)
    assert d["weekly"]["unlimited"] is True
    assert d["weekly"]["used_fraction"] is None
    # interval still has a cap
    assert d["interval"]["unlimited"] is False


# -- fixture helpers -------------------------------------------


def _make_ctx(bus: Bus, pools: dict | None = None) -> SimpleNamespace:
    """ctx where `ctx.get_agent('rate_limit_service')` returns a fake holder."""
    fake_rls = SimpleNamespace(pools=pools or {})

    def get_agent(name):
        if name == "rate_limit_service":
            return fake_rls
        return None

    return SimpleNamespace(
        bus=bus, logger=None, name="minimax_budget",
        get_agent=get_agent,
    )


def _fake_pools_with_minimax_account(api_key: str = "sk-fake",
                                     base_url: str = "https://api.minimaxi.com/v1"
                                     ) -> dict:
    """Build a pools dict matching rate_limit_service's runtime shape."""
    from yuxu.bundled.rate_limit_service.handler import Account, Pool
    pool = Pool(name="minimax")
    pool.accounts.append(Account(id="default",
                                   extra={"api_key": api_key,
                                           "base_url": base_url}))
    return {"minimax": pool}


def _mock_transport_remains(model_records: list[dict]):
    def route(req: httpx.Request):
        assert "minimaxi.com" in str(req.url)
        assert req.headers.get("authorization", "").startswith("Bearer ")
        return httpx.Response(200, json={
            "model_remains": model_records,
            "base_resp": {"status_code": 0, "status_msg": "success"},
        })
    return httpx.MockTransport(route)


def _sample_record(model_name: str = "MiniMax-M*",
                    iv_used: int = 0, iv_total: int = 4500,
                    wk_used: int = 0, wk_total: int = 45000) -> dict:
    return {
        "start_time": 1776841200000, "end_time": 1776859200000,
        "current_interval_total_count": iv_total,
        "current_interval_usage_count": iv_used,
        "model_name": model_name,
        "current_weekly_total_count": wk_total,
        "current_weekly_usage_count": wk_used,
        "weekly_start_time": 1776614400000,
        "weekly_end_time": 1777219200000,
    }


# -- account discovery ----------------------------------------


async def test_discover_accounts_only_minimax_base_urls():
    bus = Bus()
    from yuxu.bundled.rate_limit_service.handler import Account, Pool
    pool_mm = Pool(name="minimax")
    pool_mm.accounts.append(Account(id="k1",
                                     extra={"api_key": "sk-mm",
                                             "base_url": "https://api.minimaxi.com/v1"}))
    pool_other = Pool(name="deepseek")
    pool_other.accounts.append(Account(id="k1",
                                         extra={"api_key": "sk-ds",
                                                 "base_url": "https://api.deepseek.com"}))
    ctx = _make_ctx(bus, pools={"minimax": pool_mm, "deepseek": pool_other})

    budget = MiniMaxBudget(ctx,
                           http_client=httpx.AsyncClient(
                               transport=_mock_transport_remains([])))
    try:
        ids = [acc["id"] for acc in budget._discover_accounts()]
        assert any("minimax:" in i for i in ids)
        assert not any("deepseek:" in i for i in ids)
    finally:
        await budget.uninstall()


async def test_discover_accepts_plain_dict_accounts():
    """Tests / alt backends may pass plain-dict accounts; we still accept them."""
    bus = Bus()
    pools = {"minimax": SimpleNamespace(accounts=[
        {"id": "k1", "api_key": "sk", "base_url": "https://api.minimaxi.com/v1"},
    ])}
    ctx = _make_ctx(bus, pools=pools)
    budget = MiniMaxBudget(ctx,
                           http_client=httpx.AsyncClient(
                               transport=_mock_transport_remains([])))
    try:
        found = budget._discover_accounts()
        assert len(found) == 1 and "minimax:" in found[0]["id"]
    finally:
        await budget.uninstall()


# -- refresh / snapshot --------------------------------------


async def test_refresh_populates_snapshot(tmp_path):
    bus = Bus()
    ctx = _make_ctx(bus, pools=_fake_pools_with_minimax_account())
    client = httpx.AsyncClient(transport=_mock_transport_remains([
        _sample_record(iv_used=100, iv_total=4500, wk_used=500, wk_total=45000),
        _sample_record(model_name="speech-hd", iv_used=0, iv_total=19000),
    ]))
    budget = MiniMaxBudget(ctx, http_client=client, poll_interval=99999)
    # skip the auto poll_task; drive refresh manually
    budget._accounts = budget._discover_accounts()
    assert budget._accounts
    await budget.refresh()

    snap = budget.snapshot()
    assert snap["ok"] is True
    accounts = snap["accounts"]
    assert len(accounts) == 1
    models = {m["model_name"]: m for m in accounts[0]["models"]}
    assert "MiniMax-M*" in models
    assert models["MiniMax-M*"]["interval"]["used"] == 100
    assert models["MiniMax-M*"]["interval"]["total"] == 4500
    await budget.uninstall()


async def test_refresh_handles_fetch_failure_without_crashing(tmp_path):
    """If MiniMax returns 500 we log and move on; other accounts/models
    unaffected. Snapshot keeps any prior cached entries."""
    bus = Bus()
    ctx = _make_ctx(bus, pools=_fake_pools_with_minimax_account())

    def boom(req):
        return httpx.Response(500, text="server down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    budget = MiniMaxBudget(ctx, http_client=client, poll_interval=99999)
    budget._accounts = budget._discover_accounts()
    r = await budget.refresh()
    # Does not raise; returns a per-account status.
    assert r["ok"] is True
    assert r["refreshed"][0]["ok"] is False
    await budget.uninstall()


async def test_refresh_respects_account_id_filter(tmp_path):
    bus = Bus()
    from yuxu.bundled.rate_limit_service.handler import Account, Pool
    pool = Pool(name="minimax")
    pool.accounts.append(Account(id="k1",
                                   extra={"api_key": "sk1",
                                           "base_url": "https://api.minimaxi.com/v1"}))
    pool.accounts.append(Account(id="k2",
                                   extra={"api_key": "sk2",
                                           "base_url": "https://api.minimaxi.com/v1"}))
    ctx = _make_ctx(bus, pools={"minimax": pool})
    calls: list[str] = []

    def route(req):
        calls.append(req.headers.get("authorization") or "")
        return httpx.Response(200, json={
            "model_remains": [_sample_record()],
            "base_resp": {"status_code": 0, "status_msg": "success"},
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(route))
    budget = MiniMaxBudget(ctx, http_client=client, poll_interval=99999)
    budget._accounts = budget._discover_accounts()
    target = budget._accounts[1]["id"]
    r = await budget.refresh(account_id=target)
    assert r["refreshed"][0]["account_id"] == target
    assert len(calls) == 1
    assert "Bearer sk2" in calls[0]
    await budget.uninstall()


# -- per-agent attribution -----------------------------------


async def test_on_llm_completed_accumulates_per_agent_model():
    bus = Bus()
    ctx = _make_ctx(bus, pools={})
    budget = MiniMaxBudget(ctx,
                           http_client=httpx.AsyncClient(
                               transport=_mock_transport_remains([])))
    await budget._on_llm_completed({"payload": {
        "agent": "a", "pool": "minimax", "model": "m1",
        "usage": {"total_tokens": 100},
    }})
    await budget._on_llm_completed({"payload": {
        "agent": "a", "pool": "minimax", "model": "m1",
        "usage": {"total_tokens": 300},
    }})
    await budget._on_llm_completed({"payload": {
        "agent": "b", "pool": "minimax", "model": "m1",
        "usage": {"total_tokens": 50},
    }})
    usage = budget.agent_usage()["usage"]
    by = {(u["agent"], u["model"]): u for u in usage}
    assert by[("a", "m1")]["requests"] == 2
    assert by[("a", "m1")]["total_tokens"] == 400
    assert by[("a", "m1")]["avg_tokens_per_req"] == 200
    assert by[("b", "m1")]["requests"] == 1
    assert by[("b", "m1")]["avg_tokens_per_req"] == 50
    await budget.uninstall()


async def test_agent_usage_filters_by_agent():
    bus = Bus()
    ctx = _make_ctx(bus, pools={})
    budget = MiniMaxBudget(ctx,
                           http_client=httpx.AsyncClient(
                               transport=_mock_transport_remains([])))
    await budget._on_llm_completed({"payload": {
        "agent": "a", "pool": "minimax", "model": "m",
        "usage": {"total_tokens": 10},
    }})
    await budget._on_llm_completed({"payload": {
        "agent": "b", "pool": "minimax", "model": "m",
        "usage": {"total_tokens": 20},
    }})
    r = budget.agent_usage("a")
    assert len(r["usage"]) == 1 and r["usage"][0]["agent"] == "a"
    await budget.uninstall()


async def test_estimate_projects_tokens_from_avg():
    bus = Bus()
    ctx = _make_ctx(bus, pools={})
    budget = MiniMaxBudget(ctx,
                           http_client=httpx.AsyncClient(
                               transport=_mock_transport_remains([])))
    # 3 requests avg 100 tokens each
    for t in (80, 120, 100):
        await budget._on_llm_completed({"payload": {
            "agent": "x", "pool": "minimax", "model": "m",
            "usage": {"total_tokens": t},
        }})
    r = budget.estimate(agent="x", n_requests=5)
    assert r["history_requests"] == 3
    assert r["history_tokens"] == 300
    assert r["avg_tokens_per_req"] == 100
    assert r["projected_requests"] == 5
    assert r["projected_tokens"] == 500
    await budget.uninstall()


async def test_estimate_inverse_from_tokens():
    bus = Bus()
    ctx = _make_ctx(bus, pools={})
    budget = MiniMaxBudget(ctx,
                           http_client=httpx.AsyncClient(
                               transport=_mock_transport_remains([])))
    for t in (40, 60):
        await budget._on_llm_completed({"payload": {
            "agent": "x", "pool": "minimax", "model": "m",
            "usage": {"total_tokens": t},
        }})
    r = budget.estimate(agent="x", n_tokens=500)
    assert r["projected_tokens"] == 500
    assert r["projected_requests"] == 10   # 500 / 50 avg
    await budget.uninstall()


async def test_estimate_with_no_history_returns_none_avg():
    bus = Bus()
    ctx = _make_ctx(bus, pools={})
    budget = MiniMaxBudget(ctx,
                           http_client=httpx.AsyncClient(
                               transport=_mock_transport_remains([])))
    r = budget.estimate(agent="ghost", n_requests=10)
    assert r["history_requests"] == 0
    assert r["avg_tokens_per_req"] is None
    assert r["projected_tokens"] is None
    await budget.uninstall()


async def test_reset_local_clears_all_or_one_agent():
    bus = Bus()
    ctx = _make_ctx(bus, pools={})
    budget = MiniMaxBudget(ctx,
                           http_client=httpx.AsyncClient(
                               transport=_mock_transport_remains([])))
    for a in ("a", "b"):
        await budget._on_llm_completed({"payload": {
            "agent": a, "model": "m", "usage": {"total_tokens": 1},
        }})
    budget.reset_local("a")
    assert budget.agent_usage()["usage"][0]["agent"] == "b"
    budget.reset_local()
    assert budget.agent_usage()["usage"] == []
    await budget.uninstall()


# -- cap alerts ----------------------------------------------


async def test_interval_soft_cap_fires_once_per_window():
    bus = Bus()
    ctx = _make_ctx(bus, pools=_fake_pools_with_minimax_account())
    # 85% of 4500 is ~3825 → soft cap hits. 95% → hard cap.
    soft_record = _sample_record(iv_used=3900, iv_total=4500)
    client = httpx.AsyncClient(transport=_mock_transport_remains([soft_record]))
    budget = MiniMaxBudget(ctx, http_client=client, poll_interval=99999)
    budget._accounts = budget._discover_accounts()

    alerts_soft: list[dict] = []
    alerts_hard: list[dict] = []
    bus.subscribe("minimax_budget.interval_soft_cap",
                   lambda ev: alerts_soft.append(ev.get("payload") or {}))
    bus.subscribe("minimax_budget.interval_hard_cap",
                   lambda ev: alerts_hard.append(ev.get("payload") or {}))

    await budget.refresh()
    # refresh twice — same window → no duplicate alert
    await budget.refresh()
    for _ in range(10):
        await asyncio.sleep(0)

    assert len(alerts_soft) == 1
    assert alerts_hard == []
    assert alerts_soft[0]["used"] == 3900
    assert alerts_soft[0]["used_fraction"] >= SOFT_CAP_FRAC
    await budget.uninstall()


async def test_hard_cap_fires_with_soft():
    bus = Bus()
    ctx = _make_ctx(bus, pools=_fake_pools_with_minimax_account())
    hard_record = _sample_record(iv_used=4400, iv_total=4500)  # 97.7%
    client = httpx.AsyncClient(transport=_mock_transport_remains([hard_record]))
    budget = MiniMaxBudget(ctx, http_client=client, poll_interval=99999)
    budget._accounts = budget._discover_accounts()

    got: list[str] = []
    bus.subscribe("minimax_budget.*",
                   lambda ev: got.append(ev.get("topic") or ""))
    await budget.refresh()
    for _ in range(10):
        await asyncio.sleep(0)

    assert "minimax_budget.interval_hard_cap" in got
    await budget.uninstall()


async def test_unlimited_weekly_does_not_fire_weekly_cap():
    """Key with weekly_total=0 (unlimited) should never trigger weekly cap
    even as weekly_used grows."""
    bus = Bus()
    ctx = _make_ctx(bus, pools=_fake_pools_with_minimax_account())
    # huge weekly_used, but weekly_total=0 → unlimited
    rec = _sample_record(iv_used=10, iv_total=4500,
                          wk_used=50_000_000, wk_total=0)
    client = httpx.AsyncClient(transport=_mock_transport_remains([rec]))
    budget = MiniMaxBudget(ctx, http_client=client, poll_interval=99999)
    budget._accounts = budget._discover_accounts()

    got: list[dict] = []
    bus.subscribe("minimax_budget.weekly_soft_cap",
                   lambda ev: got.append(ev.get("payload") or {}))
    bus.subscribe("minimax_budget.weekly_hard_cap",
                   lambda ev: got.append(ev.get("payload") or {}))
    await budget.refresh()
    for _ in range(10):
        await asyncio.sleep(0)

    assert got == []
    await budget.uninstall()


# -- handle() surface ----------------------------------------


class _Msg:
    def __init__(self, payload):
        self.payload = payload


async def test_handle_snapshot_op():
    bus = Bus()
    ctx = _make_ctx(bus, pools=_fake_pools_with_minimax_account())
    client = httpx.AsyncClient(transport=_mock_transport_remains([_sample_record()]))
    budget = MiniMaxBudget(ctx, http_client=client, poll_interval=99999)
    budget._accounts = budget._discover_accounts()
    await budget.refresh()
    r = await budget.handle(_Msg({"op": "snapshot"}))
    assert r["ok"] is True
    assert len(r["accounts"]) == 1
    await budget.uninstall()


async def test_handle_agent_usage_op():
    bus = Bus()
    ctx = _make_ctx(bus, pools={})
    budget = MiniMaxBudget(ctx,
                           http_client=httpx.AsyncClient(
                               transport=_mock_transport_remains([])))
    await budget._on_llm_completed({"payload": {
        "agent": "z", "model": "m", "usage": {"total_tokens": 42},
    }})
    r = await budget.handle(_Msg({"op": "agent_usage"}))
    assert r["usage"][0]["agent"] == "z"
    assert r["usage"][0]["total_tokens"] == 42
    await budget.uninstall()


async def test_handle_estimate_op():
    bus = Bus()
    ctx = _make_ctx(bus, pools={})
    budget = MiniMaxBudget(ctx,
                           http_client=httpx.AsyncClient(
                               transport=_mock_transport_remains([])))
    await budget._on_llm_completed({"payload": {
        "agent": "z", "model": "m", "usage": {"total_tokens": 50},
    }})
    r = await budget.handle(_Msg({
        "op": "estimate", "agent": "z", "n_requests": 4,
    }))
    assert r["projected_tokens"] == 200
    await budget.uninstall()


async def test_handle_refresh_op():
    bus = Bus()
    ctx = _make_ctx(bus, pools=_fake_pools_with_minimax_account())
    n_calls = [0]

    def route(req):
        n_calls[0] += 1
        return httpx.Response(200, json={
            "model_remains": [_sample_record()],
            "base_resp": {"status_code": 0, "status_msg": "success"},
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(route))
    budget = MiniMaxBudget(ctx, http_client=client, poll_interval=99999)
    budget._accounts = budget._discover_accounts()
    await budget.handle(_Msg({"op": "refresh"}))
    assert n_calls[0] == 1
    await budget.uninstall()


async def test_handle_unknown_op():
    bus = Bus()
    ctx = _make_ctx(bus, pools={})
    budget = MiniMaxBudget(ctx,
                           http_client=httpx.AsyncClient(
                               transport=_mock_transport_remains([])))
    r = await budget.handle(_Msg({"op": "weird"}))
    assert r["ok"] is False
    await budget.uninstall()


async def test_handle_estimate_missing_agent():
    bus = Bus()
    ctx = _make_ctx(bus, pools={})
    budget = MiniMaxBudget(ctx,
                           http_client=httpx.AsyncClient(
                               transport=_mock_transport_remains([])))
    r = await budget.handle(_Msg({"op": "estimate"}))
    assert r["ok"] is False
    await budget.uninstall()


# -- end-to-end: llm_service publishes → budget subscribes ----


async def test_llm_service_event_attributes_to_budget():
    """Wire llm_service + minimax_budget over a shared bus, drive a request
    through llm_service, verify budget records it."""
    from yuxu.bundled.llm_service.handler import LLMService
    from yuxu.bundled.rate_limit_service.handler import RateLimitService

    bus = Bus()
    rate = RateLimitService({"minimax": {
        "max_concurrent": 2,
        "accounts": [{"id": "k1", "api_key": "sk",
                       "base_url": "https://api.minimaxi.com/v1"}],
    }})

    def llm_route(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hi"},
                          "finish_reason": "stop"}],
            "usage": {"total_tokens": 77, "prompt_tokens": 60,
                      "completion_tokens": 17},
        })

    svc = LLMService(rate.acquire, bus=bus,
                     client=httpx.AsyncClient(
                         transport=httpx.MockTransport(llm_route)))
    bus.register("llm_service", svc.handle)

    ctx = _make_ctx(bus, pools=rate.pools)
    budget = MiniMaxBudget(ctx, http_client=httpx.AsyncClient(
        transport=_mock_transport_remains([])),
        poll_interval=99999)
    budget._accounts = budget._discover_accounts()
    # Subscribe for llm event BEFORE the request
    bus.subscribe(COMPLETION_TOPIC, budget._on_llm_completed)

    class _MsgSender:
        def __init__(self, payload, sender):
            self.payload = payload
            self.sender = sender

    await svc.handle(_MsgSender(
        {"pool": "minimax", "model": "MiniMax-M2.7-highspeed",
         "messages": [{"role": "user", "content": "hi"}]},
        sender="sim_agent",
    ))
    for _ in range(15):
        await asyncio.sleep(0)

    r = budget.agent_usage()["usage"]
    assert len(r) == 1
    assert r[0]["agent"] == "sim_agent"
    assert r[0]["model"] == "MiniMax-M2.7-highspeed"
    assert r[0]["total_tokens"] == 77
    await svc.close()
    await budget.uninstall()
