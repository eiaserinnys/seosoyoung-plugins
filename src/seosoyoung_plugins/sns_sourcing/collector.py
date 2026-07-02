"""Slack history collector for SNS sourcing."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from seosoyoung.plugin_sdk import slack
from seosoyoung.plugin_sdk.slack import Message

from seosoyoung_plugins.sns_sourcing.permalink import build_slack_permalink
from seosoyoung_plugins.sns_sourcing.store import SnsSourcingStore


@dataclass(frozen=True)
class SourceChannel:
    id: str
    name: str = ""


@dataclass
class CandidateFile:
    name: str = ""
    title: str = ""
    mimetype: str = ""
    permalink: str = ""


@dataclass
class SnsCandidate:
    channel_id: str
    channel_name: str
    ts: str
    thread_ts: str
    text: str
    user: str
    permalink: str
    files: list[CandidateFile] = field(default_factory=list)
    context: list[dict[str, str]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.channel_id}:{self.ts}"

    @property
    def mimetypes(self) -> list[str]:
        return [f.mimetype for f in self.files if f.mimetype]


class SlackHistoryCollector:
    """Collect unseen messages from configured Slack channels."""

    _SYSTEM_SUBTYPES = {
        "channel_join",
        "channel_leave",
        "channel_name",
        "channel_purpose",
        "channel_topic",
        "group_join",
        "group_leave",
        "message_deleted",
    }

    def __init__(
        self,
        *,
        store: SnsSourcingStore,
        source_channels: list[SourceChannel],
        workspace_domain: str,
        bot_user_id: str = "",
        page_limit: int = 100,
        max_pages_per_channel: int = 2,
        bootstrap: str = "now",
        media_only: bool = True,
        media_mimetype_prefixes: list[str] | None = None,
        context_before: int = 3,
        context_after: int = 3,
        slack_api: Any = slack,
    ):
        self.store = store
        self.source_channels = source_channels
        self.workspace_domain = workspace_domain
        self.bot_user_id = bot_user_id
        self.page_limit = page_limit
        self.max_pages_per_channel = max_pages_per_channel
        self.bootstrap = bootstrap
        self.media_only = media_only
        self.media_mimetype_prefixes = (
            ["image/", "video/"]
            if media_mimetype_prefixes is None
            else media_mimetype_prefixes
        )
        self.context_before = context_before
        self.context_after = context_after
        self.slack = slack_api
        self.scanned_until_by_channel: dict[str, str] = {}

    async def collect(self) -> list[SnsCandidate]:
        candidates: list[SnsCandidate] = []
        known_keys = self.store.ledger_keys()
        self.scanned_until_by_channel = {}
        for source in self.source_channels:
            candidates.extend(await self._collect_channel(source, known_keys))
        return sorted(candidates, key=lambda c: (c.channel_id, Decimal(c.ts)))

    async def _collect_channel(
        self,
        source: SourceChannel,
        known_keys: set[str],
    ) -> list[SnsCandidate]:
        oldest = self.store.get_cursor(source.id)
        if not oldest and self.bootstrap == "now":
            self.store.set_cursor_if_empty(source.id, _slack_ts_now())
            return []

        page_cursor: str | None = None
        messages: list[Message] = []
        truncated = False
        for _ in range(self.max_pages_per_channel):
            page = await self.slack.get_channel_history_page(
                source.id,
                oldest=oldest or None,
                cursor=page_cursor,
                limit=self.page_limit,
            )
            messages.extend(page.messages)
            if not page.has_more or not page.next_cursor:
                truncated = False
                break
            page_cursor = page.next_cursor
            truncated = True

        ordered = sorted(messages, key=lambda msg: Decimal(msg.ts))
        if ordered and not truncated:
            self.scanned_until_by_channel[source.id] = ordered[-1].ts

        candidates: list[SnsCandidate] = []
        for index, msg in enumerate(ordered):
            if self._should_skip_message(msg):
                continue
            if not self._is_candidate_message(msg):
                continue
            key = self.store.candidate_key(source.id, msg.ts)
            if key in known_keys:
                continue
            candidates.append(self._to_candidate(source, msg, ordered, index))
        return candidates

    def _should_skip_message(self, msg: Message) -> bool:
        subtype = getattr(msg, "subtype", "") or ""
        if subtype in self._SYSTEM_SUBTYPES:
            return True
        if subtype == "bot_message" or getattr(msg, "bot_id", ""):
            return True
        if self.bot_user_id and msg.user == self.bot_user_id:
            return True
        if not msg.text and not msg.files:
            return True
        return False

    def _is_candidate_message(self, msg: Message) -> bool:
        if self.media_only:
            return any(
                _mimetype_matches(file.mimetype, self.media_mimetype_prefixes)
                for file in msg.files
            )
        return bool(msg.text or msg.files)

    def _to_candidate(
        self,
        source: SourceChannel,
        msg: Message,
        ordered: list[Message],
        index: int,
    ) -> SnsCandidate:
        return SnsCandidate(
            channel_id=source.id,
            channel_name=source.name or source.id,
            ts=msg.ts,
            thread_ts=msg.thread_ts or msg.ts,
            text=msg.text,
            user=msg.user,
            permalink=build_slack_permalink(self.workspace_domain, source.id, msg.ts),
            files=[
                CandidateFile(
                    name=file.name,
                    title=file.title,
                    mimetype=file.mimetype,
                    permalink=file.permalink,
                )
                for file in msg.files
                if not self.media_only
                or _mimetype_matches(file.mimetype, self.media_mimetype_prefixes)
            ],
            context=_nearby_context(
                ordered,
                index,
                before=self.context_before,
                after=self.context_after,
            ),
        )


def _nearby_context(
    messages: list[Message],
    index: int,
    *,
    before: int,
    after: int,
) -> list[dict[str, str]]:
    start = max(0, index - max(0, before))
    end = min(len(messages), index + max(0, after) + 1)
    return [
        {"ts": msg.ts, "user": msg.user, "text": msg.text}
        for msg in messages[start:end]
        if msg.text
    ]


def _mimetype_matches(mimetype: str, prefixes: list[str]) -> bool:
    return any(mimetype.startswith(prefix) for prefix in prefixes)


def _slack_ts_now() -> str:
    return f"{time.time():.6f}"
