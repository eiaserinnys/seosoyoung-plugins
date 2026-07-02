"""File-backed state for the SNS sourcing plugin."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from filelock import FileLock


def _ts_value(ts: str) -> Decimal:
    try:
        return Decimal(ts)
    except (InvalidOperation, TypeError):
        return Decimal("0")


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Object is not JSON serializable: {type(value)!r}")


class SnsSourcingStore:
    """Persistent cursors, ledger, error log, and slot markers."""

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._cursors_path = self.base_dir / "cursors.json"
        self._ledger_path = self.base_dir / "ledger.jsonl"
        self._errors_path = self.base_dir / "errors.jsonl"
        self._runmarkers_path = self.base_dir / "runmarkers.json"

    def _lock(self, name: str) -> FileLock:
        return FileLock(str(self.base_dir / f"{name}.lock"), timeout=5)

    def _load_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json(self, path: Path, data: Any) -> None:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def get_cursor(self, channel_id: str) -> str:
        with self._lock("cursors"):
            return self._load_json(self._cursors_path, {}).get(channel_id, "")

    def advance_cursor(self, channel_id: str, ts: str) -> None:
        with self._lock("cursors"):
            cursors = self._load_json(self._cursors_path, {})
            current = cursors.get(channel_id, "")
            if not current or _ts_value(ts) > _ts_value(current):
                cursors[channel_id] = ts
                self._write_json(self._cursors_path, cursors)

    def set_cursor_if_empty(self, channel_id: str, ts: str) -> bool:
        with self._lock("cursors"):
            cursors = self._load_json(self._cursors_path, {})
            if cursors.get(channel_id):
                return False
            cursors[channel_id] = ts
            self._write_json(self._cursors_path, cursors)
            return True

    @staticmethod
    def candidate_key(channel_id: str, ts: str) -> str:
        return f"{channel_id}:{ts}"

    def ledger_keys(self) -> set[str]:
        if not self._ledger_path.exists():
            return set()
        with self._lock("ledger"):
            keys: set[str] = set()
            with self._ledger_path.open("r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    key = row.get("key")
                    if key:
                        keys.add(key)
            return keys

    def has_ledger(self, channel_id: str, ts: str) -> bool:
        return self.candidate_key(channel_id, ts) in self.ledger_keys()

    def append_ledger(self, record: dict[str, Any]) -> None:
        row = dict(record)
        row.setdefault("key", self.candidate_key(row["channel_id"], row["ts"]))
        with self._lock("ledger"):
            with self._ledger_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=_json_default) + "\n")

    def append_error(self, record: dict[str, Any]) -> None:
        with self._lock("errors"):
            with self._errors_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")

    def has_run_marker(self, slot_key: str) -> bool:
        with self._lock("runmarkers"):
            return slot_key in self._load_json(self._runmarkers_path, {})

    def mark_run_slot(self, slot_key: str, marked_at: str) -> None:
        with self._lock("runmarkers"):
            markers = self._load_json(self._runmarkers_path, {})
            markers[slot_key] = marked_at
            self._write_json(self._runmarkers_path, markers)

