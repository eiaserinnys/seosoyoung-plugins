"""Slack 마크업 이스케이프/언이스케이프 테스트"""

import pytest

from seosoyoung_plugins.translate.slack_escape import (
    escape_slack_markup,
    unescape_slack_markup,
)


class TestEscapeLinks:
    """슬랙 링크 마크업 이스케이프"""

    def test_url_with_display_text(self):
        """<URL|display text> 형태 링크"""
        text = "Check <https://example.com|this link> please"
        escaped, replacements = escape_slack_markup(text)
        assert "https://example.com" not in escaped
        assert "[[LINK1]]" in escaped
        assert replacements["[[LINK1]]"] == "<https://example.com|this link>"

    def test_url_without_display_text(self):
        """<URL> 형태 링크"""
        text = "Visit <https://example.com>"
        escaped, replacements = escape_slack_markup(text)
        assert "https://example.com" not in escaped
        assert "[[LINK1]]" in escaped
        assert replacements["[[LINK1]]"] == "<https://example.com>"

    def test_multiple_links(self):
        """여러 링크"""
        text = "See <https://a.com|A> and <https://b.com|B>"
        escaped, replacements = escape_slack_markup(text)
        assert "[[LINK1]]" in escaped
        assert "[[LINK2]]" in escaped
        assert len([k for k in replacements if k.startswith("[[LINK")]) == 2

    def test_link_with_special_chars_in_url(self):
        """URL에 특수문자 포함"""
        text = "See <https://example.com/path?q=1&b=2|link>"
        escaped, replacements = escape_slack_markup(text)
        assert "[[LINK1]]" in escaped
        assert "q=1&b=2" not in escaped

    def test_mailto_link(self):
        """mailto 링크"""
        text = "Email <mailto:user@example.com|user@example.com>"
        escaped, replacements = escape_slack_markup(text)
        assert "[[LINK1]]" in escaped


class TestEscapeMentions:
    """슬랙 유저 멘션 이스케이프"""

    def test_user_mention(self):
        """<@U12345> 유저 멘션"""
        text = "Hello <@U12345ABC>"
        escaped, replacements = escape_slack_markup(text)
        assert "<@U12345ABC>" not in escaped
        assert "[[MENTION1]]" in escaped
        assert replacements["[[MENTION1]]"] == "<@U12345ABC>"

    def test_multiple_mentions(self):
        """여러 유저 멘션"""
        text = "<@U111> and <@U222> are here"
        escaped, replacements = escape_slack_markup(text)
        assert "[[MENTION1]]" in escaped
        assert "[[MENTION2]]" in escaped


class TestEscapeBroadcasts:
    """슬랙 브로드캐스트 멘션 이스케이프"""

    def test_here(self):
        """<!here> 브로드캐스트"""
        text = "<!here> check this"
        escaped, replacements = escape_slack_markup(text)
        assert "<!here>" not in escaped
        assert "[[BROADCAST1]]" in escaped
        assert replacements["[[BROADCAST1]]"] == "<!here>"

    def test_channel(self):
        """<!channel> 브로드캐스트"""
        text = "<!channel> important"
        escaped, replacements = escape_slack_markup(text)
        assert "[[BROADCAST1]]" in escaped

    def test_everyone(self):
        """<!everyone> 브로드캐스트"""
        text = "<!everyone> notice"
        escaped, replacements = escape_slack_markup(text)
        assert "[[BROADCAST1]]" in escaped

    def test_subteam_mention(self):
        """<!subteam^S12345|@team-name> 그룹 멘션"""
        text = "<!subteam^S12345|@devs> review this"
        escaped, replacements = escape_slack_markup(text)
        assert "[[BROADCAST1]]" in escaped
        assert replacements["[[BROADCAST1]]"] == "<!subteam^S12345|@devs>"


class TestEscapeEmoji:
    """슬랙 커스텀 이모지 이스케이프"""

    def test_simple_emoji(self):
        """:emoji: 형태"""
        text = "Hello :wave: world"
        escaped, replacements = escape_slack_markup(text)
        assert ":wave:" not in escaped
        assert "[[EMOJI1]]" in escaped
        assert replacements["[[EMOJI1]]"] == ":wave:"

    def test_hyphenated_emoji(self):
        """하이픈 포함 이모지"""
        text = "Nice :thumbs-up: work"
        escaped, replacements = escape_slack_markup(text)
        assert "[[EMOJI1]]" in escaped

    def test_emoji_with_underscores(self):
        """언더스코어 포함 이모지"""
        text = "Feeling :slightly_smiling_face:"
        escaped, replacements = escape_slack_markup(text)
        assert "[[EMOJI1]]" in escaped

    def test_multiple_emojis(self):
        """여러 이모지"""
        text = ":wave: Hello :smile: :heart:"
        escaped, replacements = escape_slack_markup(text)
        assert "[[EMOJI1]]" in escaped
        assert "[[EMOJI2]]" in escaped
        assert "[[EMOJI3]]" in escaped

    def test_emoji_with_skin_tone(self):
        """스킨톤 이모지"""
        text = "Hi :wave::skin-tone-3:"
        escaped, replacements = escape_slack_markup(text)
        # 스킨톤은 별도 이모지로 처리
        assert "[[EMOJI1]]" in escaped
        assert "[[EMOJI2]]" in escaped


class TestEscapeCode:
    """코드 블록 이스케이프"""

    def test_inline_code(self):
        """인라인 코드"""
        text = "Run `npm install` now"
        escaped, replacements = escape_slack_markup(text)
        assert "`npm install`" not in escaped
        assert "[[CODE1]]" in escaped
        assert replacements["[[CODE1]]"] == "`npm install`"

    def test_code_block(self):
        """코드 블록"""
        text = "Here:\n```\nconst x = 1;\nconsole.log(x);\n```\nDone"
        escaped, replacements = escape_slack_markup(text)
        assert "const x = 1;" not in escaped
        assert "[[CODE1]]" in escaped

    def test_code_block_with_language(self):
        """언어 지정 코드 블록"""
        text = "Example:\n```python\nprint('hello')\n```\nEnd"
        escaped, replacements = escape_slack_markup(text)
        assert "print('hello')" not in escaped
        assert "[[CODE1]]" in escaped

    def test_multiple_inline_codes(self):
        """여러 인라인 코드"""
        text = "Use `foo` and `bar`"
        escaped, replacements = escape_slack_markup(text)
        assert "[[CODE1]]" in escaped
        assert "[[CODE2]]" in escaped

    def test_code_block_preserves_surrounding_text(self):
        """코드 블록 전후 텍스트 보존"""
        text = "Before ```code``` After"
        escaped, replacements = escape_slack_markup(text)
        assert "Before" in escaped
        assert "After" in escaped


class TestEscapeMixed:
    """여러 마크업이 섞인 경우"""

    def test_link_and_mention(self):
        """링크 + 멘션"""
        text = "<@U123> shared <https://example.com|a link>"
        escaped, replacements = escape_slack_markup(text)
        assert "[[MENTION1]]" in escaped
        assert "[[LINK1]]" in escaped
        assert len(replacements) == 2

    def test_all_types_mixed(self):
        """모든 타입이 섞인 복합 메시지"""
        text = (
            "<@U123> said :wave: check <https://example.com|this> "
            "and run `npm install` <!here>"
        )
        escaped, replacements = escape_slack_markup(text)
        assert "[[MENTION1]]" in escaped
        assert "[[EMOJI1]]" in escaped
        assert "[[LINK1]]" in escaped
        assert "[[CODE1]]" in escaped
        assert "[[BROADCAST1]]" in escaped
        assert len(replacements) == 5

    def test_plain_text_unchanged(self):
        """마크업 없는 텍스트는 변경 없음"""
        text = "Hello, this is a normal message without any markup."
        escaped, replacements = escape_slack_markup(text)
        assert escaped == text
        assert replacements == {}

    def test_empty_text(self):
        """빈 텍스트"""
        escaped, replacements = escape_slack_markup("")
        assert escaped == ""
        assert replacements == {}


class TestUnescape:
    """언이스케이프 (복원)"""

    def test_restore_single_placeholder(self):
        """단일 플레이스홀더 복원"""
        text = "Check [[LINK1]] please"
        replacements = {"[[LINK1]]": "<https://example.com|this link>"}
        result = unescape_slack_markup(text, replacements)
        assert result == "Check <https://example.com|this link> please"

    def test_restore_multiple_placeholders(self):
        """여러 플레이스홀더 복원"""
        text = "[[MENTION1]] said [[EMOJI1]]"
        replacements = {
            "[[MENTION1]]": "<@U12345>",
            "[[EMOJI1]]": ":wave:",
        }
        result = unescape_slack_markup(text, replacements)
        assert result == "<@U12345> said :wave:"

    def test_restore_empty_replacements(self):
        """빈 치환 맵"""
        text = "Hello world"
        result = unescape_slack_markup(text, {})
        assert result == "Hello world"

    def test_missing_placeholder_untouched(self):
        """텍스트에 없는 플레이스홀더는 무시"""
        text = "Hello [[LINK1]] world"
        replacements = {
            "[[LINK1]]": "<https://example.com>",
            "[[LINK2]]": "<https://other.com>",
        }
        result = unescape_slack_markup(text, replacements)
        assert result == "Hello <https://example.com> world"

    def test_placeholder_in_translated_text_restored(self):
        """번역 후 플레이스홀더가 잘 복원됨"""
        # 번역기가 플레이스홀더를 그대로 유지한 경우를 시뮬레이션
        translated = "확인해 주세요 [[LINK1]] 감사합니다 [[EMOJI1]]"
        replacements = {
            "[[LINK1]]": "<https://example.com|this link>",
            "[[EMOJI1]]": ":pray:",
        }
        result = unescape_slack_markup(translated, replacements)
        assert result == "확인해 주세요 <https://example.com|this link> 감사합니다 :pray:"


class TestRoundTrip:
    """이스케이프 -> 언이스케이프 라운드트립"""

    @pytest.mark.parametrize(
        "original",
        [
            "Hello <@U123> check <https://example.com|link>",
            "<!here> run `npm install` :wave:",
            "<@U111> <@U222> :smile: :heart: `code` ```block```",
            "No markup at all",
            "",
            "Just :emoji: only",
            "<https://a.com> <https://b.com|B> <https://c.com>",
            "Code: ```python\nprint('hello')\n``` end",
            "<!channel> <!here> <!everyone>",
            "<@U123> said <!here> check <https://example.com|this> and run `npm install` :wave:",
        ],
    )
    def test_roundtrip_preserves_original(self, original):
        """이스케이프 후 언이스케이프하면 원본 복원"""
        escaped, replacements = escape_slack_markup(original)
        restored = unescape_slack_markup(escaped, replacements)
        assert restored == original


class TestEdgeCases:
    """에지 케이스"""

    def test_angle_bracket_not_link(self):
        """슬랙 마크업이 아닌 < > 텍스트"""
        text = "a < b and c > d"
        escaped, replacements = escape_slack_markup(text)
        # 슬랙 마크업 패턴이 아닌 <는 그대로 유지
        assert "a < b and c > d" == escaped

    def test_colon_in_normal_text(self):
        """일반 텍스트의 콜론은 이모지로 처리하지 않음"""
        text = "Time is 10:30:00"
        escaped, replacements = escape_slack_markup(text)
        # 숫자로 된 :30:은 이모지 패턴에 매칭되지 않아야 함
        assert "10" in escaped
        assert "00" in escaped

    def test_backtick_without_pair(self):
        """짝이 없는 백틱은 무시"""
        text = "Use the `command without closing"
        escaped, replacements = escape_slack_markup(text)
        # 짝이 없으므로 코드로 처리하지 않음
        assert "command without closing" in escaped

    def test_nested_backticks(self):
        """코드 블록 안의 인라인 코드 (코드 블록이 우선)"""
        text = "Example: ```use `inner` here``` done"
        escaped, replacements = escape_slack_markup(text)
        # 코드 블록 전체가 하나의 플레이스홀더
        assert "[[CODE1]]" in escaped
        assert "done" in escaped

    def test_emoji_adjacent_to_text(self):
        """이모지가 텍스트에 붙어있는 경우"""
        text = "Hello:wave:world"
        escaped, replacements = escape_slack_markup(text)
        assert "[[EMOJI1]]" in escaped
        assert "Hello" in escaped
        assert "world" in escaped

    def test_channel_link(self):
        """<#C12345> 채널 링크"""
        text = "Check <#C12345|general>"
        escaped, replacements = escape_slack_markup(text)
        # 채널 링크도 보존 대상
        assert "[[LINK1]]" in escaped or "[[MENTION1]]" in escaped

    def test_url_inside_code_block_not_double_escaped(self):
        """코드 블록 안의 URL은 이중 이스케이프되지 않음"""
        text = "See ```<https://example.com>``` here"
        escaped, replacements = escape_slack_markup(text)
        # 코드 블록이 먼저 이스케이프되므로 URL은 코드 블록 안에 포함됨
        code_placeholder = [k for k in replacements if k.startswith("[[CODE")]
        assert len(code_placeholder) == 1
        # URL 플레이스홀더가 별도로 생기지 않아야 함
        link_placeholders = [k for k in replacements if k.startswith("[[LINK")]
        assert len(link_placeholders) == 0
