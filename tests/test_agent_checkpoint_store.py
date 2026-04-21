from __future__ import annotations

import json
from pathlib import Path

import pytest

from yuxu.bundled.checkpoint_store.handler import CheckpointStore
from yuxu.core.bus import Bus
from yuxu.core.loader import Loader

pytestmark = pytest.mark.asyncio


# -- unit tests --------------------------------------------------


async def test_save_and_load_roundtrip(tmp_path):
    s = CheckpointStore(tmp_path)
    r = s.save("ns1", "k1", {"a": 1, "b": [1, 2, 3]})
    assert r["ok"] is True
    path = Path(r["path"])
    assert path.exists()
    loaded = s.load("ns1", "k1")
    assert loaded["ok"] is True
    assert loaded["data"] == {"a": 1, "b": [1, 2, 3]}
    assert "saved_at" in loaded


async def test_file_format(tmp_path):
    s = CheckpointStore(tmp_path)
    s.save("ns", "k", {"x": 42})
    raw = json.loads((tmp_path / "ns" / "k.json").read_text())
    assert raw["version"] == 1
    assert raw["namespace"] == "ns"
    assert raw["key"] == "k"
    assert raw["data"] == {"x": 42}
    assert raw["saved_at"].endswith("+00:00")


async def test_load_missing(tmp_path):
    s = CheckpointStore(tmp_path)
    r = s.load("ghost_ns", "ghost_key")
    assert r == {"ok": False, "error": "not_found"}


async def test_save_overwrites(tmp_path):
    s = CheckpointStore(tmp_path)
    s.save("ns", "k", 1)
    s.save("ns", "k", 2)
    assert s.load("ns", "k")["data"] == 2


async def test_list_keys(tmp_path):
    s = CheckpointStore(tmp_path)
    assert s.list_keys("empty") == {"ok": True, "keys": []}
    s.save("ns", "b", 1)
    s.save("ns", "a", 1)
    s.save("ns", "c", 1)
    assert s.list_keys("ns") == {"ok": True, "keys": ["a", "b", "c"]}


async def test_list_namespaces(tmp_path):
    s = CheckpointStore(tmp_path)
    assert s.list_namespaces() == {"ok": True, "namespaces": []}
    s.save("alpha", "k", 1)
    s.save("beta", "k", 1)
    s.save("gamma", "k", 1)
    assert s.list_namespaces() == {"ok": True, "namespaces": ["alpha", "beta", "gamma"]}


async def test_handle_list_namespaces(tmp_path):
    s = CheckpointStore(tmp_path)
    s.save("a", "k", 1)
    r = await s.handle(_FakeMsg({"op": "list_namespaces"}))
    assert r == {"ok": True, "namespaces": ["a"]}


async def test_delete(tmp_path):
    s = CheckpointStore(tmp_path)
    s.save("ns", "k", 1)
    assert s.delete("ns", "k") == {"ok": True}
    assert s.load("ns", "k")["ok"] is False
    assert s.delete("ns", "k") == {"ok": False, "error": "not_found"}


async def test_invalid_key_rejected(tmp_path):
    s = CheckpointStore(tmp_path)
    for bad in ["../etc", "foo/bar", "", ".hidden"]:
        with pytest.raises(ValueError):
            s.save("ns", bad, {})
    for bad in ["../", "a/b", "", ".x"]:
        with pytest.raises(ValueError):
            s.save(bad, "k", {})


async def test_atomic_write_no_tmp_left(tmp_path):
    s = CheckpointStore(tmp_path)
    s.save("ns", "k", {"big": "x" * 1000})
    leftover = list((tmp_path / "ns").glob("*.tmp"))
    assert leftover == []


async def test_decode_error_returns_error(tmp_path):
    s = CheckpointStore(tmp_path)
    (tmp_path / "ns").mkdir()
    (tmp_path / "ns" / "bad.json").write_text("{not json")
    r = s.load("ns", "bad")
    assert r["ok"] is False
    assert "decode_error" in r["error"]


# -- handler dispatch via bus-style messages ---------------------


class _FakeMsg:
    def __init__(self, payload):
        self.payload = payload


async def test_handle_save_load_list_delete(tmp_path):
    s = CheckpointStore(tmp_path)

    r = await s.handle(_FakeMsg({"op": "save", "namespace": "n", "key": "k", "data": 7}))
    assert r["ok"] is True

    r = await s.handle(_FakeMsg({"op": "load", "namespace": "n", "key": "k"}))
    assert r["data"] == 7

    r = await s.handle(_FakeMsg({"op": "list", "namespace": "n"}))
    assert r["keys"] == ["k"]

    r = await s.handle(_FakeMsg({"op": "delete", "namespace": "n", "key": "k"}))
    assert r["ok"] is True


async def test_handle_unknown_op(tmp_path):
    s = CheckpointStore(tmp_path)
    r = await s.handle(_FakeMsg({"op": "nope"}))
    assert r["ok"] is False
    assert "unknown op" in r["error"]


async def test_handle_missing_field(tmp_path):
    s = CheckpointStore(tmp_path)
    r = await s.handle(_FakeMsg({"op": "save", "namespace": "n"}))
    assert r["ok"] is False
    assert "missing field: key" in r["error"]


async def test_handle_non_dict_payload(tmp_path):
    s = CheckpointStore(tmp_path)
    r = await s.handle(_FakeMsg("not a dict"))
    assert r["ok"] is False


# -- bus + loader integration ------------------------------------


async def test_integration_via_bus(tmp_path, monkeypatch, bundled_dir):
    monkeypatch.setenv("CHECKPOINT_ROOT", str(tmp_path))
    bus = Bus()
    loader = Loader(bus, dirs=[bundled_dir])
    await loader.scan()
    assert "checkpoint_store" in loader.specs
    await loader.ensure_running("checkpoint_store")
    assert bus.query_status("checkpoint_store") == "ready"

    r = await bus.request(
        "checkpoint_store",
        {"op": "save", "namespace": "theme_rank", "key": "run1", "data": {"step": 3}},
        timeout=2.0,
    )
    assert r["ok"] is True

    r = await bus.request(
        "checkpoint_store",
        {"op": "load", "namespace": "theme_rank", "key": "run1"},
        timeout=2.0,
    )
    assert r["ok"] is True
    assert r["data"] == {"step": 3}

    r = await bus.request(
        "checkpoint_store",
        {"op": "list", "namespace": "theme_rank"},
        timeout=2.0,
    )
    assert r["keys"] == ["run1"]
