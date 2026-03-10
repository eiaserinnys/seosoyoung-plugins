"""번역 모듈 테스트"""

import pytest
from unittest.mock import patch, MagicMock

from seosoyoung_plugins.soulstream_client import SoulstreamSyncClient, SoulstreamResult
from seosoyoung_plugins.translate.translator import (
    translate,
    _build_context_text,
    _build_prompt,
    _build_glossary_section,
    _calculate_cost,
    _translate_via_soulstream,
    _backend_to_provider,
)
from seosoyoung_plugins.translate.detector import Language
from seosoyoung_plugins.translate.glossary import GlossaryMatchResult
from seosoyoung_plugins.translate.detector import Language as PluginLanguage
from seosoyoung_plugins.translate.plugin import TranslatePlugin


class TestBuildContextText:
    """컨텍스트 텍스트 생성 테스트"""

    def test_empty_context(self):
        """빈 컨텍스트"""
        assert _build_context_text([]) == ""

    def test_single_message(self):
        """단일 메시지"""
        context = [{"user": "Alice", "text": "Hello"}]
        result = _build_context_text(context)
        assert "<previous_messages>" in result
        assert "[Alice]: Hello" in result
        assert "</previous_messages>" in result

    def test_multiple_messages(self):
        """여러 메시지"""
        context = [
            {"user": "Alice", "text": "Hello"},
            {"user": "Bob", "text": "Hi there"},
        ]
        result = _build_context_text(context)
        assert "[Alice]: Hello" in result
        assert "[Bob]: Hi there" in result


class TestBuildPrompt:
    """프롬프트 생성 테스트"""

    def test_korean_to_english(self):
        """한국어 -> 영어 프롬프트"""
        prompt, terms, match_result, slack_replacements = _build_prompt("안녕하세요", Language.KOREAN, "")
        assert "English" in prompt
        assert "안녕하세요" in prompt

    def test_english_to_korean(self):
        """영어 -> 한국어 프롬프트"""
        prompt, terms, match_result, slack_replacements = _build_prompt("Hello", Language.ENGLISH, "")
        assert "Korean" in prompt
        assert "Hello" in prompt

    def test_with_context(self):
        """컨텍스트 포함"""
        context = [{"user": "Alice", "text": "Previous message"}]
        prompt, terms, match_result, slack_replacements = _build_prompt("Hello", Language.ENGLISH, "", context)
        assert "<previous_messages>" in prompt
        assert "[Alice]: Previous message" in prompt

    @patch("seosoyoung_plugins.translate.translator.find_relevant_terms_v2")
    def test_with_glossary(self, mock_find_terms_v2):
        """용어집 포함"""
        mock_result = GlossaryMatchResult(
            matched_terms=[("펜릭스", "Fenrix")],
            extracted_words=["펜릭스"],
            debug_info={}
        )
        mock_find_terms_v2.return_value = mock_result
        prompt, terms, match_result, slack_replacements = _build_prompt("펜릭스가 말했다.", Language.KOREAN, "")
        assert "<glossary>" in prompt
        assert "펜릭스 → Fenrix" in prompt
        assert terms == [("펜릭스", "Fenrix")]

    @patch("seosoyoung_plugins.translate.translator.find_relevant_terms_v2")
    def test_without_glossary(self, mock_find_terms_v2):
        """관련 용어 없을 때 용어집 섹션 없음"""
        mock_result = GlossaryMatchResult(matched_terms=[], extracted_words=[], debug_info={})
        mock_find_terms_v2.return_value = mock_result
        prompt, terms, match_result, slack_replacements = _build_prompt("Hello", Language.ENGLISH, "")
        assert "<glossary>" not in prompt
        assert terms == []

    def test_slack_markup_escaped_in_prompt(self):
        """슬랙 마크업이 프롬프트에서 플레이스홀더로 치환됨"""
        text = "<@U123> shared <https://example.com|a link>"
        prompt, terms, match_result, slack_replacements = _build_prompt(text, Language.ENGLISH, "")
        assert "<@U123>" not in prompt
        assert "https://example.com" not in prompt
        assert "[[MENTION1]]" in prompt
        assert "[[LINK1]]" in prompt
        assert len(slack_replacements) == 2

    def test_placeholder_instruction_added_when_markup_present(self):
        """마크업이 있을 때 플레이스홀더 보존 지시가 프롬프트에 포함됨"""
        text = "Check <https://example.com|this>"
        prompt, _, _, slack_replacements = _build_prompt(text, Language.ENGLISH, "")
        assert "Preserve all placeholders" in prompt
        assert len(slack_replacements) > 0

    def test_no_placeholder_instruction_for_plain_text(self):
        """마크업 없는 텍스트에는 플레이스홀더 지시 없음"""
        text = "Hello world"
        prompt, _, _, slack_replacements = _build_prompt(text, Language.ENGLISH, "")
        assert "Preserve all placeholders" not in prompt
        assert slack_replacements == {}

    def test_context_messages_also_escaped(self):
        """컨텍스트 메시지의 슬랙 마크업도 이스케이프됨"""
        context = [{"user": "Alice", "text": "See <https://example.com|link>"}]
        prompt, _, _, _ = _build_prompt("Hello", Language.ENGLISH, "", context)
        assert "https://example.com" not in prompt
        assert "[[LINK1]]" in prompt


class TestBuildGlossarySection:
    """용어집 섹션 생성 테스트"""

    @patch("seosoyoung_plugins.translate.translator.find_relevant_terms_v2")
    def test_builds_glossary_section(self, mock_find_terms_v2):
        """용어집 섹션 생성"""
        mock_result = GlossaryMatchResult(
            matched_terms=[("펜릭스", "Fenrix"), ("아리엘라", "Ariella")],
            extracted_words=["펜릭스", "아리엘라"],
            debug_info={}
        )
        mock_find_terms_v2.return_value = mock_result
        section, terms, match_result = _build_glossary_section("펜릭스와 아리엘라", Language.KOREAN, "")
        assert "<glossary>" in section
        assert "</glossary>" in section
        assert "펜릭스 → Fenrix" in section
        assert "아리엘라 → Ariella" in section
        assert terms == [("펜릭스", "Fenrix"), ("아리엘라", "Ariella")]

    @patch("seosoyoung_plugins.translate.translator.find_relevant_terms_v2")
    def test_empty_when_no_terms(self, mock_find_terms_v2):
        """관련 용어 없으면 빈 튜플"""
        mock_result = GlossaryMatchResult(matched_terms=[], extracted_words=[], debug_info={})
        mock_find_terms_v2.return_value = mock_result
        section, terms, match_result = _build_glossary_section("Hello world", Language.ENGLISH, "")
        assert section == ""
        assert terms == []


class TestCalculateCost:
    """비용 계산 테스트"""

    def test_calculate_cost_basic(self):
        """기본 비용 계산 (Haiku 모델)"""
        cost = _calculate_cost(1000, 100, "claude-3-5-haiku-latest")
        assert abs(cost - 0.0012) < 0.0001

    def test_calculate_cost_zero(self):
        """0 토큰"""
        cost = _calculate_cost(0, 0, "claude-3-5-haiku-latest")
        assert cost == 0.0

    def test_calculate_cost_sonnet(self):
        """Sonnet 모델 비용 계산"""
        cost = _calculate_cost(1000, 100, "claude-sonnet-4-20250514")
        assert abs(cost - 0.0045) < 0.0001

    def test_calculate_cost_unknown_model(self):
        """알 수 없는 모델은 기본 가격 사용"""
        cost = _calculate_cost(1000, 100, "unknown-model")
        assert abs(cost - 0.0045) < 0.0001

    def test_calculate_cost_openai_gpt5_mini(self):
        """OpenAI gpt-5-mini 비용 계산"""
        cost = _calculate_cost(1000, 100, "gpt-5-mini")
        assert abs(cost - 0.00056) < 0.00001

    def test_calculate_cost_openai_gpt4_1_mini(self):
        """OpenAI gpt-4.1-mini 비용 계산"""
        cost = _calculate_cost(1000, 100, "gpt-4.1-mini")
        assert abs(cost - 0.00056) < 0.00001


class TestTranslate:
    """번역 함수 테스트 (소울스트림 프록시 경유)"""

    def _make_mock_client(self, content="Hello", input_tokens=100, output_tokens=10):
        mock_client = MagicMock(spec=SoulstreamSyncClient)
        mock_client.complete.return_value = SoulstreamResult(
            content=content, input_tokens=input_tokens,
            output_tokens=output_tokens, session_id="test",
        )
        return mock_client

    def test_translate_korean_to_english(self):
        """한국어 -> 영어 번역"""
        mock_client = self._make_mock_client("Hello")

        text, cost, terms, match_result = translate(
            "안녕하세요", Language.KOREAN,
            backend="anthropic",
            model="claude-3-5-haiku-latest",
            soulstream_client=mock_client,
            glossary_path="",
        )

        assert text == "Hello"
        assert cost > 0
        assert isinstance(terms, list)
        mock_client.complete.assert_called_once()
        call_args = mock_client.complete.call_args
        assert call_args.kwargs["provider"] == "anthropic"

    def test_translate_english_to_korean(self):
        """영어 -> 한국어 번역"""
        mock_client = self._make_mock_client("안녕하세요")

        text, cost, terms, match_result = translate(
            "Hello", Language.ENGLISH,
            backend="anthropic",
            model="claude-3-5-haiku-latest",
            soulstream_client=mock_client,
            glossary_path="",
        )

        assert text == "안녕하세요"
        assert cost > 0
        assert isinstance(terms, list)

    def test_translate_invalid_backend(self):
        """잘못된 backend 호출 시 에러"""
        mock_client = self._make_mock_client()
        with pytest.raises(ValueError, match="Unknown backend"):
            translate(
                "Hello", Language.ENGLISH,
                backend="invalid",
                model="test-model",
                soulstream_client=mock_client,
                glossary_path="",
            )

    def test_translate_with_custom_model(self):
        """커스텀 모델 사용"""
        mock_client = self._make_mock_client("Result")

        translate(
            "Test", Language.ENGLISH,
            backend="anthropic",
            model="custom-model",
            soulstream_client=mock_client,
            glossary_path="",
        )

        call_args = mock_client.complete.call_args
        assert call_args.kwargs["model"] == "custom-model"

    @patch("seosoyoung_plugins.translate.translator.find_relevant_terms_v2")
    def test_translate_returns_glossary_terms(self, mock_find_terms_v2):
        """번역 시 참고한 용어 목록 반환"""
        mock_result = GlossaryMatchResult(
            matched_terms=[("펜릭스", "Fenrix"), ("아리엘라", "Ariella")],
            extracted_words=["펜릭스", "아리엘라"],
            debug_info={}
        )
        mock_find_terms_v2.return_value = mock_result

        mock_client = self._make_mock_client("Fenrix and Ariella")

        text, cost, terms, match_result = translate(
            "펜릭스와 아리엘라", Language.KOREAN,
            backend="anthropic",
            model="claude-3-5-haiku-latest",
            soulstream_client=mock_client,
            glossary_path="",
        )

        assert text == "Fenrix and Ariella"
        assert terms == [("펜릭스", "Fenrix"), ("아리엘라", "Ariella")]


class TestTranslateSoulstream:
    """소울스트림 프록시 번역 테스트"""

    def _make_mock_client(self, content="Hello", input_tokens=100, output_tokens=10):
        mock_client = MagicMock(spec=SoulstreamSyncClient)
        mock_client.complete.return_value = SoulstreamResult(
            content=content, input_tokens=input_tokens,
            output_tokens=output_tokens, session_id="test",
        )
        return mock_client

    def test_translate_openai_backend(self):
        """OpenAI backend로 한국어 -> 영어 번역"""
        mock_client = self._make_mock_client("Hello")

        text, cost, terms, match_result = translate(
            "안녕하세요", Language.KOREAN,
            backend="openai",
            model="gpt-5-mini",
            soulstream_client=mock_client,
            glossary_path="",
        )

        assert text == "Hello"
        assert cost > 0
        mock_client.complete.assert_called_once()
        call_args = mock_client.complete.call_args
        assert call_args.kwargs["provider"] == "openai"
        assert call_args.kwargs["model"] == "gpt-5-mini"

    def test_translate_anthropic_backend(self):
        """backend 파라미터로 anthropic 명시적 지정"""
        mock_client = self._make_mock_client("안녕하세요")

        text, cost, terms, match_result = translate(
            "Hello", Language.ENGLISH,
            backend="anthropic",
            model="claude-3-5-haiku-latest",
            soulstream_client=mock_client,
            glossary_path="",
        )

        assert text == "안녕하세요"
        call_args = mock_client.complete.call_args
        assert call_args.kwargs["provider"] == "anthropic"

    def test_translate_uses_max_tokens(self):
        """소울스트림 호출 시 max_tokens=2048 사용"""
        mock_client = self._make_mock_client("Hello")

        translate(
            "안녕하세요", Language.KOREAN,
            backend="openai",
            model="gpt-5-mini",
            soulstream_client=mock_client,
            glossary_path="",
        )

        call_args = mock_client.complete.call_args
        assert call_args.kwargs["max_tokens"] == 2048

    def test_translate_custom_model(self):
        """커스텀 모델 사용"""
        mock_client = self._make_mock_client("Result")

        translate(
            "Test", Language.ENGLISH,
            backend="openai",
            model="gpt-4o",
            soulstream_client=mock_client,
            glossary_path="",
        )

        call_args = mock_client.complete.call_args
        assert call_args.kwargs["model"] == "gpt-4o"

    def test_backend_to_provider(self):
        """backend -> provider 변환"""
        assert _backend_to_provider("openai") == "openai"
        assert _backend_to_provider("anthropic") == "anthropic"
        with pytest.raises(ValueError):
            _backend_to_provider("invalid")


class TestFormatResponse:
    """응답 포맷팅 테스트 (TranslatePlugin._format_response)"""

    @pytest.fixture(autouse=True)
    def _setup_plugin(self):
        """테스트 전 기본 show_glossary=False, show_cost=True 플러그인 설정"""
        self.plugin = TranslatePlugin()
        # on_load 없이 필요한 필드만 직접 설정 (테스트 전용)
        self.plugin._show_glossary = False
        self.plugin._show_cost = True

    def test_korean_to_english_without_glossary(self):
        """한국어 -> 영어 (용어집 없음)"""
        result = self.plugin._format_response("홍길동", "Hello", PluginLanguage.KOREAN, 0.0012)
        assert "`홍길동 said,`" in result
        assert '"Hello"' in result
        assert "`~💵$0.0012`" in result
        assert "📖" not in result

    def test_english_to_korean_without_glossary(self):
        """영어 -> 한국어 (용어집 없음)"""
        result = self.plugin._format_response("John", "안녕하세요", PluginLanguage.ENGLISH, 0.0012)
        assert "`John님이`" in result
        assert '"안녕하세요"' in result
        assert "`라고 하셨습니다.`" in result
        assert "`~💵$0.0012`" in result
        assert "📖" not in result

    def test_korean_to_english_with_glossary(self):
        """한국어 -> 영어 (용어집 있음, 표시 켜짐)"""
        self.plugin._show_glossary = True
        terms = [("펜릭스", "Fenrix"), ("아리엘라", "Ariella")]
        result = self.plugin._format_response("홍길동", "Fenrix and Ariella", PluginLanguage.KOREAN, 0.0012, terms)
        assert "`홍길동 said,`" in result
        assert "`📖 펜릭스 (Fenrix), 아리엘라 (Ariella)`" in result
        assert "`~💵$0.0012`" in result

    def test_english_to_korean_with_glossary(self):
        """영어 -> 한국어 (용어집 있음, 표시 켜짐)"""
        self.plugin._show_glossary = True
        terms = [("Fenrix", "펜릭스")]
        result = self.plugin._format_response("John", "펜릭스가 말했다", PluginLanguage.ENGLISH, 0.0012, terms)
        assert "`John님이`" in result
        assert "`📖 Fenrix (펜릭스)`" in result
        assert "`~💵$0.0012`" in result

    def test_with_empty_glossary(self):
        """빈 용어집"""
        self.plugin._show_glossary = True
        result = self.plugin._format_response("홍길동", "Hello", PluginLanguage.KOREAN, 0.0012, [])
        assert "📖" not in result

    def test_with_none_glossary(self):
        """None 용어집"""
        self.plugin._show_glossary = True
        result = self.plugin._format_response("홍길동", "Hello", PluginLanguage.KOREAN, 0.0012, None)
        assert "📖" not in result

    def test_glossary_hidden_when_option_off(self):
        """용어집 표시 옵션 꺼짐"""
        self.plugin._show_glossary = False
        terms = [("펜릭스", "Fenrix")]
        result = self.plugin._format_response("홍길동", "Fenrix", PluginLanguage.KOREAN, 0.0012, terms)
        assert "📖" not in result

    def test_cost_hidden_when_option_off(self):
        """비용 표시 옵션 꺼짐"""
        self.plugin._show_glossary = False
        self.plugin._show_cost = False
        result = self.plugin._format_response("홍길동", "Hello", PluginLanguage.KOREAN, 0.0012)
        assert "💵" not in result
