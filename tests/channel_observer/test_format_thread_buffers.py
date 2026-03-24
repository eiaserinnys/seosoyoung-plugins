"""Tests for _format_thread_buffers helper in pipeline.py.

Verifies that thread_buffers dict[str, list[dict]] is serialized to a
human-readable string instead of a raw nested structure that would render
as [object Object] in UI or confuse Claude Code prompt context.
"""

import pytest

from seosoyoung_plugins.channel_observer.pipeline import _format_thread_buffers


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
