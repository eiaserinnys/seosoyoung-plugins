import pytest

from seosoyoung_plugins.sns_sourcing.collector import SnsCandidate
from seosoyoung_plugins.sns_sourcing.service import SnsSourcingService
from seosoyoung_plugins.sns_sourcing.session import SnsDecision
from seosoyoung_plugins.sns_sourcing.store import SnsSourcingStore


def candidate(ts):
    return SnsCandidate(
        channel_id="C1",
        channel_name="art",
        ts=ts,
        thread_ts=ts,
        text="text",
        user="U1",
        permalink=f"https://slack/{ts}",
    )


class FakeCollector:
    def __init__(self, scanned_until_by_channel=None):
        if scanned_until_by_channel is not None:
            self.scanned_until_by_channel = scanned_until_by_channel

    async def collect(self):
        return []


class FakeSession:
    max_candidates = 8

    async def classify(self, candidates, slot_key):
        return [
            SnsDecision(
                channel_id=item.channel_id,
                ts=item.ts,
                label="usable",
                reason="good",
            )
            for item in candidates
        ]


class FlakyPublisher:
    async def publish(self, cand, decision):
        if cand.ts == "1.000002":
            raise RuntimeError("publish failed")
        return type(
            "PublishResult",
            (),
            {"posted": True, "channel": "COUT", "ts": cand.ts, "dry_run": False},
        )()


class FirstOnlySession:
    max_candidates = 8

    async def classify(self, candidates, slot_key):
        first = candidates[0]
        return [
            SnsDecision(
                channel_id=first.channel_id,
                ts=first.ts,
                label="usable",
                reason="good",
            )
        ]


class NoopPublisher:
    async def publish(self, cand, decision):
        return type(
            "PublishResult",
            (),
            {"posted": False, "channel": "", "ts": "", "dry_run": True},
        )()


@pytest.mark.asyncio
async def test_individual_publish_failure_does_not_kill_tick(tmp_path):
    store = SnsSourcingStore(tmp_path)
    service = SnsSourcingService(
        store=store,
        collector=FakeCollector(),
        session=FakeSession(),
        publisher=FlakyPublisher(),
    )

    summary = await service.process_candidates(
        [candidate("1.000001"), candidate("1.000002"), candidate("1.000003")],
        "2026-07-02:10:30",
    )

    assert summary.decided == 2
    assert summary.errors == 1
    assert store.ledger_keys() == {"C1:1.000001", "C1:1.000003"}
    assert store.get_cursor("C1") == "1.000001"


@pytest.mark.asyncio
async def test_candidate_free_window_advances_to_scanned_until(tmp_path):
    store = SnsSourcingStore(tmp_path)
    store.advance_cursor("C1", "1.000000")
    service = SnsSourcingService(
        store=store,
        collector=FakeCollector({"C1": "1.000004"}),
        session=FakeSession(),
        publisher=NoopPublisher(),
    )

    summary = await service.process_candidates([], "2026-07-02:10:30")

    assert summary.collected == 0
    assert store.get_cursor("C1") == "1.000004"


@pytest.mark.asyncio
async def test_unconfirmed_candidate_blocks_cursor_before_scan_watermark(tmp_path):
    store = SnsSourcingStore(tmp_path)
    store.advance_cursor("C1", "1.000000")
    service = SnsSourcingService(
        store=store,
        collector=FakeCollector({"C1": "1.000004"}),
        session=FirstOnlySession(),
        publisher=NoopPublisher(),
    )

    summary = await service.process_candidates(
        [candidate("1.000001"), candidate("1.000003")],
        "2026-07-02:10:30",
    )

    assert summary.decided == 1
    assert summary.errors == 1
    assert store.ledger_keys() == {"C1:1.000001"}
    assert store.get_cursor("C1") == "1.000001"


@pytest.mark.asyncio
async def test_truncated_scan_without_watermark_does_not_advance_cursor(tmp_path):
    store = SnsSourcingStore(tmp_path)
    store.advance_cursor("C1", "1.000000")
    service = SnsSourcingService(
        store=store,
        collector=FakeCollector({}),
        session=FakeSession(),
        publisher=NoopPublisher(),
    )

    await service.process_candidates([], "2026-07-02:10:30")

    assert store.get_cursor("C1") == "1.000000"
