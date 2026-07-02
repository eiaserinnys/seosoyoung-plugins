"""Slot scheduler for SNS sourcing."""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, time as day_time
from zoneinfo import ZoneInfo

from seosoyoung_plugins.sns_sourcing.service import SnsSourcingService
from seosoyoung_plugins.sns_sourcing.store import SnsSourcingStore

logger = logging.getLogger(__name__)


class SnsSourcingScheduler:
    """Polls fixed KST slots and runs each slot once per day."""

    def __init__(
        self,
        *,
        service: SnsSourcingService,
        store: SnsSourcingStore,
        timezone: str = "Asia/Seoul",
        slots: list[str] | None = None,
        poll_sec: int = 300,
    ):
        self.service = service
        self.store = store
        self.timezone = ZoneInfo(timezone)
        self.slots = [_parse_slot(slot) for slot in (slots or ["10:30", "16:30"])]
        self.poll_sec = poll_sec
        self._timer: threading.Timer | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._schedule_next()
        logger.info("sns_sourcing scheduler started: slots=%s", self.slot_labels)

    def stop(self) -> None:
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        logger.info("sns_sourcing scheduler stopped")

    @property
    def slot_labels(self) -> list[str]:
        return [slot.strftime("%H:%M") for slot in self.slots]

    def due_slot_keys(self, now: datetime | None = None) -> list[str]:
        current = now.astimezone(self.timezone) if now else datetime.now(self.timezone)
        due: list[str] = []
        for slot in self.slots:
            if current.time() < slot:
                continue
            key = f"{current.date().isoformat()}:{slot.strftime('%H:%M')}"
            if not self.store.has_run_marker(key):
                due.append(key)
        return due

    def _schedule_next(self) -> None:
        if not self._running:
            return
        self._timer = threading.Timer(self.poll_sec, self._tick)
        self._timer.daemon = True
        self._timer.start()

    def _tick(self) -> None:
        try:
            for slot_key in self.due_slot_keys():
                try:
                    asyncio.run(self.service.tick(slot_key))
                except Exception:
                    logger.exception("sns_sourcing tick failed: slot=%s", slot_key)
        finally:
            self._schedule_next()


def _parse_slot(value: str) -> day_time:
    hour, minute = value.split(":", 1)
    return day_time(hour=int(hour), minute=int(minute))

