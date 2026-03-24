"""_format_recent_context 및 _fetch_recent_context 단위 테스트

[BOT MENTION THREAD] 태그 삽입 로직을 검증합니다.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from seosoyoung_plugins.channel_observer.pipeline import (
    _format_recent_context,
    _fetch_recent_context,
)


class TestFormatRecentContext:
    """_format_recent_context 단위 테스트"""

    def test_no_bot_user_id_no_tag(self):
        """bot_user_id가 없으면 태그를 붙이지 않는다."""
        messages = [
            {"user": "U001", "text": "안녕하세요 <@BOTXXX> 여기 봐주세요"},
            {"user": "U002", "text": "일반 메시지"},
        ]
        result = _format_recent_context(messages)
        assert "[BOT MENTION THREAD]" not in result
        assert "[U001]: 안녕하세요 <@BOTXXX> 여기 봐주세요" in result
        assert "[U002]: 일반 메시지" in result

    def test_mention_message_tagged(self):
        """<@BOT123>이 포함된 메시지에 [BOT MENTION THREAD] 태그가 붙는다."""
        messages = [
            {"user": "U001", "text": "<@BOT123> 작업해줘"},
        ]
        result = _format_recent_context(messages, bot_user_id="BOT123")
        assert "[U001]: <@BOT123> 작업해줘 [BOT MENTION THREAD]" in result

    def test_non_mention_message_not_tagged(self):
        """봇 멘션이 없는 메시지에는 태그가 붙지 않는다."""
        messages = [
            {"user": "U001", "text": "일반 대화입니다"},
        ]
        result = _format_recent_context(messages, bot_user_id="BOT123")
        assert "[BOT MENTION THREAD]" not in result
        assert "[U001]: 일반 대화입니다" in result

    def test_mixed_messages(self):
        """멘션/비멘션 혼합 목록에서 멘션 메시지만 태깅된다."""
        messages = [
            {"user": "U001", "text": "일반 이야기"},
            {"user": "U002", "text": "<@BOT123> 확인 부탁드려요"},
            {"user": "U003", "text": "다른 이야기"},
            {"user": "U004", "text": "<@BOT123> 추가 요청"},
        ]
        result = _format_recent_context(messages, bot_user_id="BOT123")
        lines = result.split("\n")
        assert len(lines) == 4
        assert "[BOT MENTION THREAD]" not in lines[0]
        assert "[BOT MENTION THREAD]" in lines[1]
        assert "[BOT MENTION THREAD]" not in lines[2]
        assert "[BOT MENTION THREAD]" in lines[3]

    def test_empty_messages(self):
        """메시지가 없으면 빈 문자열을 반환한다."""
        assert _format_recent_context([]) == ""
        assert _format_recent_context([], bot_user_id="BOT123") == ""


class TestFetchRecentContextPassesBotUserId:
    """_fetch_recent_context가 bot_user_id를 _format_recent_context에 전달하는지 검증"""

    @pytest.mark.asyncio
    async def test_fetch_recent_context_passes_bot_user_id(self):
        """_fetch_recent_context가 bot_user_id 키워드 인자를 _format_recent_context에 전달한다."""
        mock_message = MagicMock()
        mock_message.user = "U001"
        mock_message.text = "테스트 메시지"

        with patch(
            "seosoyoung_plugins.channel_observer.pipeline.slack"
        ) as mock_slack, patch(
            "seosoyoung_plugins.channel_observer.pipeline._format_recent_context"
        ) as mock_format:
            mock_slack.get_channel_history = AsyncMock(return_value=[mock_message])
            mock_format.return_value = "formatted"

            result = await _fetch_recent_context("C123", bot_user_id="BOT123")

            assert result == "formatted"
            mock_format.assert_called_once()
            _, kwargs = mock_format.call_args
            assert kwargs.get("bot_user_id") == "BOT123"
