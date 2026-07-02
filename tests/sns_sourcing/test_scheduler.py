from datetime import datetime
from zoneinfo import ZoneInfo

from seosoyoung_plugins.sns_sourcing.scheduler import SnsSourcingScheduler
from seosoyoung_plugins.sns_sourcing.store import SnsSourcingStore


class DummyService:
    async def tick(self, slot_key):
        return None


def test_due_slots_use_kst_and_markers(tmp_path):
    store = SnsSourcingStore(tmp_path)
    scheduler = SnsSourcingScheduler(
        service=DummyService(),
        store=store,
        slots=["10:30", "16:30"],
    )
    now = datetime(2026, 7, 2, 17, 0, tzinfo=ZoneInfo("Asia/Seoul"))

    assert scheduler.due_slot_keys(now) == [
        "2026-07-02:10:30",
        "2026-07-02:16:30",
    ]

    store.mark_run_slot("2026-07-02:10:30", "done")

    assert scheduler.due_slot_keys(now) == ["2026-07-02:16:30"]

