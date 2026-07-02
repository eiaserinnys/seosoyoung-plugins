from types import SimpleNamespace

import pytest

from seosoyoung_plugins.sns_sourcing.collector import SlackHistoryCollector, SourceChannel
from seosoyoung_plugins.sns_sourcing.store import SnsSourcingStore


class FakeSlack:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def get_channel_history_page(self, channel, oldest=None, cursor=None, limit=100):
        self.calls.append(
            {"channel": channel, "oldest": oldest, "cursor": cursor, "limit": limit}
        )
        return self.pages.pop(0)


def page(messages, has_more=False, next_cursor=""):
    return SimpleNamespace(messages=messages, has_more=has_more, next_cursor=next_cursor)


def msg(ts, text="hello", user="U1", **kwargs):
    return SimpleNamespace(
        ts=ts,
        text=text,
        user=user,
        thread_ts=kwargs.get("thread_ts"),
        files=kwargs.get("files", []),
        subtype=kwargs.get("subtype", ""),
        bot_id=kwargs.get("bot_id", ""),
    )


@pytest.mark.asyncio
async def test_collects_only_unseen_media_messages_and_keeps_text_context(tmp_path):
    store = SnsSourcingStore(tmp_path)
    store.advance_cursor("C1", "1000.000000")
    store.append_ledger({"channel_id": "C1", "ts": "1000.000004"})
    file = SimpleNamespace(
        name="shot.png",
        title="Shot",
        mimetype="image/png",
        permalink="https://slack/files/F1",
    )
    pdf = SimpleNamespace(
        name="spec.pdf",
        title="Spec",
        mimetype="application/pdf",
        permalink="https://slack/files/F2",
    )
    fake_slack = FakeSlack(
        [
            page(
                [
                    msg("1000.000001", "bot", user="UBOT"),
                    msg("1000.000002", "join", subtype="channel_join"),
                    msg("1000.000003", "text-only context"),
                    msg("1000.000004", "already", files=[file]),
                    msg("1000.000005", "doc", files=[pdf]),
                    msg("1000.000006", "candidate", files=[file]),
                    msg("1000.000007", "after context"),
                ]
            )
        ]
    )
    collector = SlackHistoryCollector(
        store=store,
        source_channels=[SourceChannel(id="C1", name="art")],
        workspace_domain="thelinegames.slack.com",
        bot_user_id="UBOT",
        bootstrap="all",
        slack_api=fake_slack,
    )

    candidates = await collector.collect()

    assert [candidate.ts for candidate in candidates] == ["1000.000006"]
    assert candidates[0].permalink.endswith("/archives/C1/p1000000006")
    assert candidates[0].mimetypes == ["image/png"]
    assert [item["text"] for item in candidates[0].context] == [
        "text-only context",
        "already",
        "doc",
        "candidate",
        "after context",
    ]
    assert collector.scanned_until_by_channel == {"C1": "1000.000007"}


@pytest.mark.asyncio
async def test_collects_text_only_when_media_only_is_disabled(tmp_path):
    store = SnsSourcingStore(tmp_path)
    store.advance_cursor("C1", "1000.000000")
    fake_slack = FakeSlack([page([msg("1000.000001", "text-only")])])
    collector = SlackHistoryCollector(
        store=store,
        source_channels=[SourceChannel(id="C1")],
        workspace_domain="thelinegames.slack.com",
        bootstrap="all",
        media_only=False,
        slack_api=fake_slack,
    )

    candidates = await collector.collect()

    assert [candidate.ts for candidate in candidates] == ["1000.000001"]


@pytest.mark.asyncio
async def test_does_not_mark_scan_complete_when_page_is_truncated(tmp_path):
    store = SnsSourcingStore(tmp_path)
    store.advance_cursor("C1", "1000.000000")
    file = SimpleNamespace(
        name="shot.png",
        title="Shot",
        mimetype="image/png",
        permalink="https://slack/files/F1",
    )
    fake_slack = FakeSlack(
        [page([msg("1000.000001", "candidate", files=[file])], has_more=True, next_cursor="n1")]
    )
    collector = SlackHistoryCollector(
        store=store,
        source_channels=[SourceChannel(id="C1")],
        workspace_domain="thelinegames.slack.com",
        bootstrap="all",
        max_pages_per_channel=1,
        slack_api=fake_slack,
    )

    candidates = await collector.collect()

    assert [candidate.ts for candidate in candidates] == ["1000.000001"]
    assert collector.scanned_until_by_channel == {}


@pytest.mark.asyncio
async def test_bootstrap_now_sets_cursor_without_backfill(tmp_path):
    store = SnsSourcingStore(tmp_path)
    fake_slack = FakeSlack([])
    collector = SlackHistoryCollector(
        store=store,
        source_channels=[SourceChannel(id="C1")],
        workspace_domain="thelinegames.slack.com",
        bootstrap="now",
        slack_api=fake_slack,
    )

    assert await collector.collect() == []
    assert store.get_cursor("C1")
    assert fake_slack.calls == []
