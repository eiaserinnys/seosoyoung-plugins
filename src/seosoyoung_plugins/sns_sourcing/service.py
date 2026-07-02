"""Tick orchestration for the SNS sourcing plugin."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal

from seosoyoung_plugins.sns_sourcing.collector import SnsCandidate, SlackHistoryCollector
from seosoyoung_plugins.sns_sourcing.publisher import SnsPublisher
from seosoyoung_plugins.sns_sourcing.session import SnsDecision, SnsDecisionSession
from seosoyoung_plugins.sns_sourcing.store import SnsSourcingStore


@dataclass
class TickSummary:
    collected: int = 0
    decided: int = 0
    published: int = 0
    errors: int = 0
    skipped: bool = False


class SnsSourcingService:
    """Coordinates collect → classify → publish → ledger."""

    def __init__(
        self,
        *,
        store: SnsSourcingStore,
        collector: SlackHistoryCollector,
        session: SnsDecisionSession,
        publisher: SnsPublisher,
    ):
        self.store = store
        self.collector = collector
        self.session = session
        self.publisher = publisher

    async def tick(self, slot_key: str) -> TickSummary:
        if self.store.has_run_marker(slot_key):
            return TickSummary(skipped=True)

        candidates = await self.collector.collect()
        summary = await self.process_candidates(candidates, slot_key)
        self.store.mark_run_slot(slot_key, datetime.now(UTC).isoformat())
        return summary

    async def process_candidates(
        self,
        candidates: list[SnsCandidate],
        slot_key: str,
    ) -> TickSummary:
        summary = TickSummary(collected=len(candidates))
        for chunk in _chunks(candidates, self.session.max_candidates):
            try:
                decisions = await self.session.classify(chunk, slot_key)
            except Exception as exc:
                summary.errors += len(chunk)
                for candidate in chunk:
                    self._record_error(candidate, slot_key, "session", exc)
                continue

            by_key = {decision.key: decision for decision in decisions}
            for candidate in chunk:
                decision = by_key.get(candidate.key)
                if decision is None:
                    summary.errors += 1
                    self._record_error(candidate, slot_key, "missing_decision", None)
                    continue
                try:
                    publish = await self.publisher.publish(candidate, decision)
                    self.store.append_ledger(
                        {
                            "slot_key": slot_key,
                            "channel_id": candidate.channel_id,
                            "ts": candidate.ts,
                            "thread_ts": candidate.thread_ts,
                            "permalink": candidate.permalink,
                            "decision": asdict(decision),
                            "published": publish.posted,
                            "posted_channel": publish.channel,
                            "posted_ts": publish.ts,
                            "dry_run": publish.dry_run,
                            "recorded_at": datetime.now(UTC).isoformat(),
                        }
                    )
                    summary.decided += 1
                    if publish.posted:
                        summary.published += 1
                except Exception as exc:
                    summary.errors += 1
                    self._record_error(candidate, slot_key, "publish", exc)

        self._advance_confirmed_cursors(candidates)
        return summary

    def _advance_confirmed_cursors(self, candidates: list[SnsCandidate]) -> None:
        by_channel: dict[str, list[SnsCandidate]] = {}
        for candidate in candidates:
            by_channel.setdefault(candidate.channel_id, []).append(candidate)

        ledger_keys = self.store.ledger_keys()
        for channel_id, channel_candidates in by_channel.items():
            last_confirmed = ""
            for candidate in sorted(channel_candidates, key=lambda c: Decimal(c.ts)):
                if candidate.key not in ledger_keys:
                    break
                last_confirmed = candidate.ts
            if last_confirmed:
                self.store.advance_cursor(channel_id, last_confirmed)

    def _record_error(
        self,
        candidate: SnsCandidate,
        slot_key: str,
        stage: str,
        exc: Exception | None,
    ) -> None:
        self.store.append_error(
            {
                "slot_key": slot_key,
                "stage": stage,
                "channel_id": candidate.channel_id,
                "ts": candidate.ts,
                "permalink": candidate.permalink,
                "error": str(exc) if exc else "",
                "recorded_at": datetime.now(UTC).isoformat(),
            }
        )


def _chunks(items: list[SnsCandidate], size: int):
    for index in range(0, len(items), max(1, size)):
        yield items[index : index + size]

