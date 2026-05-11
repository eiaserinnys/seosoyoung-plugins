"""trello/plugin.py reaction trigger caller_info 회귀 테스트 (R-5 atom G-15).

R-5 fix(2026-05-11): reaction trigger handler가 `caller_info={"source":"slack"}`
단일 키만 박던 결함을 R-2 G-9 fix(handlers/mention.py 6-arg) §9 대칭으로 닫음.
plugin_sdk.slack.get_user_info → UserInfo(avatar_url/email 포함) →
build_slack_caller_info(6-arg) 패턴.

본 테스트는 `_execute_with_compact` 직접 호출 + `_on_reaction` 통합 호출 양쪽으로
T-G15-F~J 5 시나리오 검증.

R-2 G-9 fix와 다른 점:
- R-2: `slackbot/auth.py:get_user_role`로 user profile 조회 (host 내부 API 사용 가능)
- R-5: `plugin_sdk.slack.get_user_info`로 plugin_sdk 추상화 경유 (§1 plugin은 host 모듈 모름)
"""

import asyncio
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from seosoyoung.plugin_sdk import slack as plugin_slack
from seosoyoung.plugin_sdk import soulstream as plugin_soulstream
from seosoyoung.plugin_sdk.slack import (
    ReactionResult,
    SendMessageResult,
    SlackBackend,
    UserInfo,
)
from seosoyoung.plugin_sdk.soulstream import (
    RunResult,
    RunStatus,
    SoulstreamBackend,
)
from seosoyoung_plugins.trello.plugin import TrelloPlugin


SAMPLE_CONFIG = {
    "api_key": "test-trello-key",
    "token": "test-trello-token",
    "board_id": "test-board-id",
    "notify_channel": "C_NOTIFY",
    "dm_target_user_id": "U_DM",
    "poll_interval": 15,
    "polling_debug": False,
    "execute_emoji": "rocket",
    "watch_lists": {"to_go": "list-to-go-id"},
    "list_ids": {
        "draft": "",
        "backlog": "list-backlog-id",
        "in_progress": "list-inprogress-id",
        "blocked": "",
        "review": "list-review-id",
        "done": "list-done-id",
    },
}


def _make_mocks_for_execute_with_compact():
    """soulstream backend / slack backend를 mock으로 주입하여 plugin_sdk 통신 가로채기."""
    soulstream_mock = AsyncMock(spec=SoulstreamBackend)
    soulstream_mock.run.return_value = RunResult(
        ok=True, status=RunStatus.COMPLETED, session_id="sess-test"
    )
    soulstream_mock.compact.return_value = MagicMock(ok=True, session_id="sess-test")
    soulstream_mock.is_restart_pending = MagicMock(return_value=False)
    soulstream_mock.get_session_id = MagicMock(return_value=None)
    soulstream_mock.get_data_dir = MagicMock(return_value=Path("/tmp/r5-test"))

    slack_mock = AsyncMock(spec=SlackBackend)
    slack_mock.send_message.return_value = SendMessageResult(ok=True, ts="1.0", channel="C1")
    slack_mock.update_message.return_value = SendMessageResult(ok=True, ts="1.0", channel="C1")
    slack_mock.add_reaction.return_value = ReactionResult(ok=True)

    plugin_soulstream.set_backend(soulstream_mock)
    plugin_slack.set_backend(slack_mock)
    return soulstream_mock, slack_mock


def _captured_caller_info(soulstream_mock):
    """soulstream.run에 전달된 caller_info dict 추출."""
    assert soulstream_mock.run.called, "soulstream.run이 호출되어야 합니다"
    call_kwargs = soulstream_mock.run.call_args.kwargs
    return call_kwargs.get("caller_info")


@pytest.fixture(autouse=True)
def reset_backends():
    """각 테스트 후 backend module-level state 원복."""
    yield
    plugin_soulstream._backend = None
    plugin_slack._backend = None


class TestExecuteWithCompactCallerInfoR5:
    """R-5 G-15: `_execute_with_compact`가 user_info를 build_slack_caller_info에 forward."""

    @pytest.mark.asyncio
    async def test_single_user_reaction_six_arg_caller_info(self):
        """T-G15-F: reaction trigger single user → caller_info 6-arg dict 박힘."""
        soulstream_mock, _ = _make_mocks_for_execute_with_compact()

        plugin = TrelloPlugin()
        await plugin.on_load(SAMPLE_CONFIG)

        user_info = UserInfo(
            id="U_alice",
            name="alice",
            real_name="Alice Wonderland",
            display_name="앨리스",
            avatar_url="https://avatars.slack-edge.com/alice_192.jpg",
            email="alice@example.com",
        )

        await plugin._execute_with_compact(
            channel="C_general",
            thread_ts="1234567890.123456",
            start_msg_ts="1234567890.000000",
            session_id=None,
            prompt="run this card",
            context=None,
            user_id="U_alice",
            user_info=user_info,
        )

        caller_info = _captured_caller_info(soulstream_mock)
        # source / user_id (top-level) / display_name / avatar_url / email / slack sub-dict
        assert caller_info["source"] == "slack"
        assert caller_info["user_id"] == "U_alice"
        assert caller_info["display_name"] == "앨리스"
        assert caller_info["avatar_url"] == "https://avatars.slack-edge.com/alice_192.jpg"
        assert caller_info["email"] == "alice@example.com"
        assert caller_info["slack"]["channel_id"] == "C_general"
        assert caller_info["slack"]["user_id"] == "U_alice"
        assert caller_info["slack"]["thread_ts"] == "1234567890.123456"
        assert caller_info["bot_name"] == "seosoyoung"

    @pytest.mark.asyncio
    async def test_different_user_reaction_id_is_reactor(self):
        """T-G15-G: 다른 user reaction (event.user ≠ thread starter) → caller_info.user_id = reactor.

        thread는 user A가 시작했고 user B가 reaction 단 시나리오.
        caller_info.user_id는 *reactor* (event.user) — R-2 G-9 fix의 멀티 유저 thread mix
        예방 패턴 §9 대칭.
        """
        soulstream_mock, _ = _make_mocks_for_execute_with_compact()

        plugin = TrelloPlugin()
        await plugin.on_load(SAMPLE_CONFIG)

        user_info_B = UserInfo(
            id="U_bob",
            name="bob",
            real_name="Bob Builder",
            display_name="밥",
        )

        # thread_ts는 user A의 thread 시작 ts, 그러나 user_id는 reactor B
        await plugin._execute_with_compact(
            channel="C_general",
            thread_ts="user_A_thread_ts",  # A가 시작한 thread
            start_msg_ts="start_ts",
            session_id=None,
            prompt="run",
            context=None,
            user_id="U_bob",       # reactor B
            user_info=user_info_B,
        )

        caller_info = _captured_caller_info(soulstream_mock)
        assert caller_info["user_id"] == "U_bob"           # reactor B
        assert caller_info["slack"]["user_id"] == "U_bob"  # 의도적 중복
        assert caller_info["display_name"] == "밥"
        # thread_ts는 그대로 보존 (어디서 trigger됐는지)
        assert caller_info["slack"]["thread_ts"] == "user_A_thread_ts"

    @pytest.mark.asyncio
    async def test_dm_channel_preserved(self):
        """T-G15-H: DM 채널 (item.channel='D...') → caller_info.slack.channel_id 보존."""
        soulstream_mock, _ = _make_mocks_for_execute_with_compact()

        plugin = TrelloPlugin()
        await plugin.on_load(SAMPLE_CONFIG)

        user_info = UserInfo(id="U_alice", name="alice", display_name="앨리스")

        await plugin._execute_with_compact(
            channel="D_DM12345",  # DM 채널 (D-prefix)
            thread_ts="1234567890.123456",
            start_msg_ts="start_ts",
            session_id=None,
            prompt="run",
            context=None,
            user_id="U_alice",
            user_info=user_info,
        )

        caller_info = _captured_caller_info(soulstream_mock)
        # DM 채널 ID 보존 (D-prefix 그대로)
        assert caller_info["slack"]["channel_id"] == "D_DM12345"

    @pytest.mark.asyncio
    async def test_group_channel_with_thread(self):
        """T-G15-I: 그룹 채널 + thread → caller_info.slack.thread_ts 포함."""
        soulstream_mock, _ = _make_mocks_for_execute_with_compact()

        plugin = TrelloPlugin()
        await plugin.on_load(SAMPLE_CONFIG)

        user_info = UserInfo(id="U_alice", name="alice", display_name="앨리스")

        await plugin._execute_with_compact(
            channel="C_general",   # 그룹 채널 (C-prefix)
            thread_ts="1700000000.000001",  # thread parent ts
            start_msg_ts="start_ts",
            session_id=None,
            prompt="run",
            context=None,
            user_id="U_alice",
            user_info=user_info,
        )

        caller_info = _captured_caller_info(soulstream_mock)
        assert caller_info["slack"]["channel_id"] == "C_general"
        assert caller_info["slack"]["thread_ts"] == "1700000000.000001"

    @pytest.mark.asyncio
    async def test_user_info_none_graceful_continues(self):
        """T-G15-J: user_info=None (slack.get_user_info 실패) → 신원 키 부재, 호출 진행."""
        soulstream_mock, _ = _make_mocks_for_execute_with_compact()

        plugin = TrelloPlugin()
        await plugin.on_load(SAMPLE_CONFIG)

        await plugin._execute_with_compact(
            channel="C_general",
            thread_ts="1234.5678",
            start_msg_ts="start",
            session_id=None,
            prompt="run",
            context=None,
            user_id="U_alice",
            user_info=None,  # slack.get_user_info → None (네트워크/권한 실패 등)
        )

        # 호출은 진행 (차단 안 됨)
        assert soulstream_mock.run.called

        caller_info = _captured_caller_info(soulstream_mock)
        # source/user_id(top-level)/slack/bot_name은 박힘 (호출자 인자만으로 확정)
        assert caller_info["source"] == "slack"
        assert caller_info["user_id"] == "U_alice"
        assert caller_info["slack"]["channel_id"] == "C_general"
        assert caller_info["bot_name"] == "seosoyoung"
        # 신원 필드(display_name/avatar_url/email)는 키 부재 (graceful)
        assert "display_name" not in caller_info
        assert "avatar_url" not in caller_info
        assert "email" not in caller_info


class TestOnReactionAwaitsUserInfoR5:
    """R-5 G-15: `_on_reaction`이 plugin_sdk.slack.get_user_info를 await하고 forward."""

    @pytest.mark.asyncio
    async def test_on_reaction_calls_slack_get_user_info(self):
        """_on_reaction이 ctx.event.user를 추출하여 slack.get_user_info(user_id) await.

        background thread fork 직전에 await — UserInfo capture + _execute_with_compact에 forward.
        """
        soulstream_mock, slack_mock = _make_mocks_for_execute_with_compact()
        slack_mock.get_user_info.return_value = UserInfo(
            id="U_alice",
            name="alice",
            display_name="앨리스",
            avatar_url="https://x.com/a.png",
            email="alice@example.com",
        )

        plugin = TrelloPlugin()
        await plugin.on_load(SAMPLE_CONFIG)

        # watcher mock — tracked card 반환
        watcher_mock = MagicMock()
        tracked_mock = MagicMock(card_name="Card", card_id="card-1", has_execute=False)
        watcher_mock.get_tracked_by_thread_ts.return_value = tracked_mock
        watcher_mock.build_reaction_execute_request.return_value = ("prompt", [])
        plugin._watcher = watcher_mock

        # ctx — execute_emoji reaction 진입
        ctx = MagicMock()
        ctx.args = {
            "event": {
                "reaction": "rocket",  # SAMPLE_CONFIG.execute_emoji 정합
                "user": "U_alice",
                "item": {
                    "ts": "1234567890.123456",
                    "channel": "C_general",
                },
            },
        }

        await plugin._on_reaction(ctx)

        # plugin_sdk.slack.get_user_info가 reactor user_id로 호출됐는지
        slack_mock.get_user_info.assert_called_once_with("U_alice")
