"""Slack publishing for SNS sourcing decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from seosoyoung.plugin_sdk import slack

from seosoyoung_plugins.sns_sourcing.collector import SnsCandidate
from seosoyoung_plugins.sns_sourcing.session import SnsDecision


@dataclass
class PublishResult:
    posted: bool
    channel: str = ""
    ts: str = ""
    dry_run: bool = False


class SnsPublisher:
    """Publishes usable decisions to Slack, or previews in dry-run mode."""

    def __init__(
        self,
        *,
        output_channel: str,
        debug_channel: str = "",
        dry_run: bool = True,
        slack_api: Any = slack,
    ):
        self.output_channel = output_channel
        self.debug_channel = debug_channel
        self.dry_run = dry_run
        self.slack = slack_api

    async def publish(self, candidate: SnsCandidate, decision: SnsDecision) -> PublishResult:
        if self.dry_run:
            if not self.debug_channel:
                return PublishResult(posted=False, dry_run=True)
            result = await self.slack.send_message(
                channel=self.debug_channel,
                text=_format_preview(candidate, decision),
            )
            if not result.ok:
                raise RuntimeError(result.error or "dry-run preview failed")
            return PublishResult(
                posted=True,
                channel=result.channel,
                ts=result.ts,
                dry_run=True,
            )

        if not decision.is_usable:
            return PublishResult(posted=False)

        root = await self.slack.send_message(
            channel=self.output_channel,
            text=_format_root_message(candidate, decision),
        )
        if not root.ok:
            raise RuntimeError(root.error or "root publish failed")

        thread_text = _format_drafts(decision)
        if thread_text:
            reply = await self.slack.send_message(
                channel=self.output_channel,
                thread_ts=root.ts,
                text=thread_text,
            )
            if not reply.ok:
                raise RuntimeError(reply.error or "draft publish failed")

        return PublishResult(posted=True, channel=root.channel, ts=root.ts)


def _format_preview(candidate: SnsCandidate, decision: SnsDecision) -> str:
    return (
        "[dry-run] SNS 후보 판별\n"
        f"- 원본: <{candidate.permalink}|Slack 메시지> ({candidate.channel_name})\n"
        f"- 판정: {decision.label}\n"
        f"- 이유: {decision.reason}\n"
        f"- 소재: {decision.asset_summary or '-'}\n"
        f"{_format_drafts(decision)}"
    ).strip()


def _format_root_message(candidate: SnsCandidate, decision: SnsDecision) -> str:
    return (
        f"SNS 후보: <{candidate.permalink}|원본 메시지> ({candidate.channel_name})\n"
        "판정: 쓸만함\n"
        f"이유: {decision.reason}\n"
        f"소재: {decision.asset_summary or '-'}"
    )


def _format_drafts(decision: SnsDecision) -> str:
    parts: list[str] = []
    if decision.drafts:
        parts.append("초안")
        for index, draft in enumerate(decision.drafts, start=1):
            parts.append(
                f"{index}. {draft.get('en', '').strip()}\n"
                f"   번역: {draft.get('ko', '').strip()}"
            )
    if decision.hashtags:
        parts.append("해시태그: " + " ".join(decision.hashtags))
    return "\n".join(part for part in parts if part)

