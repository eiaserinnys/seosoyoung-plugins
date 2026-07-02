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
async def test_collects_unseen_messages_and_filters_bot_and_system(tmp_path):
    store = SnsSourcingStore(tmp_path)
    store.advance_cursor("C1", "1000.000000")
    store.append_ledger({"channel_id": "C1", "ts": "1000.000003"})
    file = SimpleNamespace(
        name="shot.png",
        title="Shot",
        mimetype="image/png",
        permalink="https://slack/files/F1",
    )
    fake_slack = FakeSlack(
        [
            page(
                [
                    msg("1000.000001", "bot", user="UBOT"),
                    msg("1000.000002", "join", subtype="channel_join"),
                    msg("1000.000003", "already"),
                    msg("1000.000004", "candidate", files=[file]),
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

    assert [candidate.ts for candidate in candidates] == ["1000.000004"]
    assert candidates[0].permalink.endswith("/archives/C1/p1000000004")
    assert candidates[0].mimetypes == ["image/png"]


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

