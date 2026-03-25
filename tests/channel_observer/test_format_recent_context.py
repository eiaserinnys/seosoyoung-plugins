"""_format_recent_context 및 _fetch_recent_context 단위 테스트

[BOT MENTION THREAD] 태그 삽입 로직과 rich data 직렬화를 검증합니다.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from seosoyoung.plugin_sdk.slack import FileInfo, Message, Reaction
from seosoyoung_plugins.channel_observer.pipeline import (
    _format_recent_context,
    _fetch_recent_context,
)


class TestFormatRecentContext:
    """_format_recent_context 단위 테스트"""

    def test_no_bot_user_id_no_tag(self):
        """bot_user_id가 없으면 태그를 붙이지 않는다."""
        messages = [
            Message(ts="1000.0", text="안녕하세요 <@BOTXXX> 여기 봐주세요", user="U001"),
            Message(ts="1001.0", text="일반 메시지", user="U002"),
        ]
        result = _format_recent_context(messages)
        assert "[BOT MENTION THREAD]" not in result
        assert "[U001]: 안녕하세요 <@BOTXXX> 여기 봐주세요" in result
        assert "[U002]: 일반 메시지" in result

    def test_mention_message_tagged(self):
        """<@BOT123>이 포함된 메시지에 [BOT MENTION THREAD] 태그가 붙는다."""
        messages = [
            Message(ts="1000.0", text="<@BOT123> 작업해줘", user="U001"),
        ]
        result = _format_recent_context(messages, bot_user_id="BOT123")
        assert "[U001]: <@BOT123> 작업해줘 [BOT MENTION THREAD]" in result

    def test_non_mention_message_not_tagged(self):
        """봇 멘션이 없는 메시지에는 태그가 붙지 않는다."""
        messages = [
            Message(ts="1000.0", text="일반 대화입니다", user="U001"),
        ]
        result = _format_recent_context(messages, bot_user_id="BOT123")
        assert "[BOT MENTION THREAD]" not in result
        assert "[U001]: 일반 대화입니다" in result

    def test_mixed_messages(self):
        """멘션/비멘션 혼합 목록에서 멘션 메시지만 태깅된다."""
        messages = [
            Message(ts="1000.0", text="일반 이야기", user="U001"),
            Message(ts="1001.0", text="<@BOT123> 확인 부탁드려요", user="U002"),
            Message(ts="1002.0", text="다른 이야기", user="U003"),
            Message(ts="1003.0", text="<@BOT123> 추가 요청", user="U004"),
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

    def test_reactions_formatted(self):
        """reactions가 있는 메시지는 리액션 정보를 보조 줄로 포함한다."""
        messages = [
            Message(
                ts="1000.0",
                text="좋은 아이디어네요",
                user="U001",
                reactions=[
                    Reaction(name="thumbsup", count=3, users=["U002", "U003", "U004"]),
                    Reaction(name="heart", count=1, users=["U005"]),
                ],
            ),
        ]
        result = _format_recent_context(messages)
        assert "[U001]: 좋은 아이디어네요" in result
        assert ":thumbsup: ×3 (눌린 사람: U002, U003, U004)" in result
        assert ":heart: ×1 (눌린 사람: U005)" in result
        assert "리액션:" in result

    def test_files_formatted(self):
        """files가 있는 메시지는 첨부 파일 정보를 보조 줄로 포함한다."""
        messages = [
            Message(
                ts="1000.0",
                text="파일 공유합니다",
                user="U001",
                files=[
                    FileInfo(
                        name="report.pdf",
                        title="분기 보고서",
                        mimetype="application/pdf",
                        permalink="https://slack.com/files/xxx",
                    ),
                ],
            ),
        ]
        result = _format_recent_context(messages)
        assert "[U001]: 파일 공유합니다" in result
        assert "분기 보고서 (application/pdf)" in result
        assert "첨부:" in result

    def test_blocks_formatted(self):
        """blocks가 있는 메시지는 [블록 포함] 표시를 보조 줄로 포함한다."""
        messages = [
            Message(
                ts="1000.0",
                text="블록 메시지",
                user="U001",
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "내용"}}],
            ),
        ]
        result = _format_recent_context(messages)
        assert "[U001]: 블록 메시지" in result
        assert "[블록 포함]" in result

    def test_no_rich_data_preserves_original_format(self):
        """rich data가 없는 메시지는 기존 포맷(단일 줄)을 유지한다."""
        messages = [
            Message(ts="1000.0", text="일반 메시지", user="U001"),
        ]
        result = _format_recent_context(messages)
        assert result == "[U001]: 일반 메시지"
        assert "\n" not in result


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
