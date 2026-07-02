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

