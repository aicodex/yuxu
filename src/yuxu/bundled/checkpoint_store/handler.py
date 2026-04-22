"""CheckpointStore — local filesystem state persistence.

Synchronous IO on small JSON files. See AGENT.md for the bus protocol.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class CheckpointStore:
    VERSION = 1

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._ns_locks: dict[str, asyncio.Lock] = {}

    def _get_ns_lock(self, namespace: str) -> asyncio.Lock:
        """Per-namespace asyncio lock; serializes write ops within a namespace
        so concurrent save/delete on the same key can't lose the .tmp file
        between write and rename, and so reads land on a consistent record."""
        return self._ns_locks.setdefault(namespace, asyncio.Lock())

    def _validate(self, s: str, kind: str) -> None:
        if not isinstance(s, str) or not s:
            raise ValueError(f"invalid {kind}: empty")
        if "/" in s or "\\" in s or ".." in s or s.startswith("."):
            raise ValueError(f"invalid {kind}: {s!r}")

    def _path(self, namespace: str, key: str) -> Path:
        self._validate(namespace, "namespace")
        self._validate(key, "key")
        return self.root / namespace / f"{key}.json"

    def save(self, namespace: str, key: str, data: Any) -> dict:
        path = self._path(namespace, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "version": self.VERSION,
            "namespace": namespace,
            "key": key,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return {"ok": True, "path": str(path)}

    def load(self, namespace: str, key: str) -> dict:
        path = self._path(namespace, key)
        if not path.exists():
            return {"ok": False, "error": "not_found"}
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"decode_error: {e}"}
        return {"ok": True, "data": record.get("data"), "saved_at": record.get("saved_at")}

    def list_keys(self, namespace: str) -> dict:
        self._validate(namespace, "namespace")
        d = self.root / namespace
        if not d.exists():
            return {"ok": True, "keys": []}
        keys = sorted(p.stem for p in d.glob("*.json"))
        return {"ok": True, "keys": keys}

    def list_namespaces(self) -> dict:
        if not self.root.exists():
            return {"ok": True, "namespaces": []}
        nss = sorted(d.name for d in self.root.iterdir() if d.is_dir())
        return {"ok": True, "namespaces": nss}

    def delete(self, namespace: str, key: str) -> dict:
        path = self._path(namespace, key)
        if not path.exists():
            return {"ok": False, "error": "not_found"}
        path.unlink()
        return {"ok": True}

    async def handle(self, msg) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        op = payload.get("op")
        try:
            if op == "save":
                ns = payload["namespace"]
                async with self._get_ns_lock(ns):
                    return self.save(ns, payload["key"], payload.get("data"))
            if op == "load":
                ns = payload["namespace"]
                async with self._get_ns_lock(ns):
                    return self.load(ns, payload["key"])
            if op == "list":
                ns = payload["namespace"]
                async with self._get_ns_lock(ns):
                    return self.list_keys(ns)
            if op == "list_namespaces":
                return self.list_namespaces()
            if op == "delete":
                ns = payload["namespace"]
                async with self._get_ns_lock(ns):
                    return self.delete(ns, payload["key"])
            return {"ok": False, "error": f"unknown op: {op!r}"}
        except KeyError as e:
            return {"ok": False, "error": f"missing field: {e.args[0]}"}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
