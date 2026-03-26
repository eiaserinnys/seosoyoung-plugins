"""Tests for _format_thread_buffers, _format_recent_context, _fetch_recent_context.

Verifies that thread_buffers dict[str, list[dict]] is serialized to a
human-readable string instead of a raw nested structure that would render
as [object Object] in UI or confuse Claude Code prompt context.
"""

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, patch

import pytest

from seosoyoung.plugin_sdk.slack import Message
from seosoyoung_plugins.channel_observer.pipeline import (
    _fetch_recent_context,
    _format_recent_context,
    _format_thread_buffers,
)


class TestFormatThreadBuffers:
    def test_none_returns_empty_string(self):
        assert _format_thread_buffers(None) == ""

    def test_empty_dict_returns_empty_string(self):
        assert _format_thread_buffers({}) == ""

    def test_single_thread_single_message(self):
        result = _format_thread_buffers({
            "1700000000.000001": [{"user": "U123", "text": "안녕하세요"}]
        })
        assert "[1700000000.000001]" in result
        assert "U123: 안녕하세요" in result

    def test_single_thread_multiple_messages(self):
        result = _format_thread_buffers({
            "1700000000.000001": [
                {"user": "U001", "text": "첫 번째"},
                {"user": "U002", "text": "두 번째"},
            ]
        })
        assert "U001: 첫 번째" in result
        assert "U002: 두 번째" in result

    def test_multiple_threads_separated_by_blank_line(self):
        result = _format_thread_buffers({
            "1700000000.000001": [{"user": "U001", "text": "스레드 A"}],
            "1700000000.000002": [{"user": "U002", "text": "스레드 B"}],
        })
        assert "[1700000000.000001]" in result
        assert "[1700000000.000002]" in result
        # 두 블록이 빈 줄로 구분됨
        assert "\n\n" in result

    def test_missing_user_and_text_keys(self):
        result = _format_thread_buffers({
            "1700000000.000001": [{"user": "", "text": ""}]
        })
        assert "[1700000000.000001]" in result
        # 키가 없어도 에러 없이 처리
        assert ": " in result

    def test_result_is_string(self):
        result = _format_thread_buffers({
            "1700000000.000001": [{"user": "U001", "text": "hello"}]
        })
        assert isinstance(result, str)

    def test_no_object_object_in_output(self):
        """핵심: [object Object] 같은 문자열이 결과에 없어야 함."""
        result = _format_thread_buffers({
            "1774317667.361259": [
                {"user": "U_BOT", "text": "반갑습니다"},
                {"user": "U_USR", "text": "안녕하세요"},
            ]
        })
        assert "[object Object]" not in result
        assert "U_BOT: 반갑습니다" in result
        assert "U_USR: 안녕하세요" in result


class TestFormatRecentContext:
    """_format_recent_context 테스트."""

    def test_empty_list_returns_empty_string(self):
        assert _format_recent_context([]) == ""

    def test_formats_user_and_text(self):
        messages = [Message(ts="1000.0", text="안녕하세요", user="U001")]
        result = _format_recent_context(messages)
        assert "[1000.0] <U001>: 안녕하세요" in result

    def test_multiple_messages(self):
        messages = [
            Message(ts="1000.0", text="첫 번째", user="U001"),
            Message(ts="1001.0", text="두 번째", user="U002"),
        ]
        result = _format_recent_context(messages)
        assert "[1000.0] <U001>: 첫 번째" in result
        assert "[1001.0] <U002>: 두 번째" in result

    def test_all_messages_included(self):
        """truncation 없이 전달된 메시지를 모두 포맷한다."""
        messages = [Message(ts=f"{i}.0", text=f"msg{i}", user=f"U{i:03d}") for i in range(20)]
        result = _format_recent_context(messages)
        lines = [line for line in result.split("\n") if line.strip() and line.strip().startswith("[")]
        assert len(lines) == 20

    def test_missing_user_becomes_unknown(self):
        messages = [Message(ts="1000.0", text="내용")]
        result = _format_recent_context(messages)
        assert "<unknown>:" in result


@dataclass
class FakeMessage:
    """slack.get_channel_history가 반환하는 Message 모방."""
    ts: str = ""
    text: str = ""
    user: str = ""
    thread_ts: str | None = None
    channel: str = ""
    reactions: list = field(default_factory=list)
    files: list = field(default_factory=list)
    blocks: list = field(default_factory=list)


class TestFetchRecentContext:
    """_fetch_recent_context 비동기 테스트."""

    @pytest.fixture
    def mock_slack(self):
        """slack 모듈의 get_channel_history를 mock."""
        with patch(
            "seosoyoung_plugins.channel_observer.pipeline.slack"
        ) as mock:
            mock.get_channel_history = AsyncMock()
            yield mock

    async def test_normal_returns_formatted(self, mock_slack):
        mock_slack.get_channel_history.return_value = [
            FakeMessage(ts="3", text="newest", user="U003"),
            FakeMessage(ts="2", text="middle", user="U002"),
            FakeMessage(ts="1", text="oldest", user="U001"),
        ]
        result = await _fetch_recent_context("C123", count=15)
        lines = result.strip().split("\n")
        # reversed → 시간순 (oldest first), channel_id 포함 포맷
        assert "[C123:1] <U001>: oldest" in lines[0]
        assert "[C123:3] <U003>: newest" in lines[2]

    async def test_api_failure_returns_empty(self, mock_slack):
        mock_slack.get_channel_history.side_effect = Exception("API error")
        result = await _fetch_recent_context("C123")
        assert result == ""

    async def test_empty_channel_returns_empty(self, mock_slack):
        mock_slack.get_channel_history.return_value = []
        result = await _fetch_recent_context("C123")
        assert result == ""

    async def test_passes_count_as_limit(self, mock_slack):
        mock_slack.get_channel_history.return_value = []
        await _fetch_recent_context("C123", count=10)
        mock_slack.get_channel_history.assert_called_once_with("C123", limit=10)

    async def test_none_user_becomes_unknown(self, mock_slack):
        mock_slack.get_channel_history.return_value = [
            FakeMessage(ts="1", text="hello", user=None),
        ]
        result = await _fetch_recent_context("C123")
        assert "[C123:1] <unknown>: hello" in result
