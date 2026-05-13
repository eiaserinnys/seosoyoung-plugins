import pytest

from seosoyoung_plugins.channel_observer.pipeline import _extract_utterances


class TestExtractUtterances:
    def test_single_utterance(self):
        text = "분석 내용\n<utterance>안녕하세요</utterance>"
        assert _extract_utterances(text) == "안녕하세요"

    def test_multiple_utterances(self):
        text = "<utterance>첫 번째</utterance>\n중간 텍스트\n<utterance>두 번째</utterance>"
        assert _extract_utterances(text) == "첫 번째\n두 번째"

    def test_empty_utterance(self):
        text = "<utterance></utterance>"
        assert _extract_utterances(text) == ""

    def test_no_utterance_tag(self):
        text = "그냥 일반 텍스트입니다."
        assert _extract_utterances(text) is None

    def test_ignores_outside_text(self):
        text = "이것은 분석입니다.\n판단: 긍정적\n<utterance>실제 발화 내용</utterance>\n끝."
        assert _extract_utterances(text) == "실제 발화 내용"

    def test_multiline_content(self):
        text = "<utterance>\n첫째 줄\n둘째 줄\n</utterance>"
        assert _extract_utterances(text) == "첫째 줄\n둘째 줄"

    def test_whitespace_only_utterance(self):
        text = "<utterance>   \n  \n   </utterance>"
        assert _extract_utterances(text) == ""

    def test_dedupe_same_utterance(self):
        """누적 텍스트에 같은 utterance가 두 번 등장하면 한 번만 게시한다."""
        text = "<utterance>안녕</utterance>\n중간 분석\n<utterance>안녕</utterance>"
        assert _extract_utterances(text) == "안녕"

    def test_dedupe_preserves_order(self):
        """dedupe 후에도 등장 순서를 보존한다."""
        text = (
            "<utterance>A</utterance>\n"
            "<utterance>B</utterance>\n"
            "<utterance>A</utterance>\n"
            "<utterance>C</utterance>"
        )
        assert _extract_utterances(text) == "A\nB\nC"

    def test_dedupe_ignores_whitespace_differences(self):
        """strip 후 동일한 본문은 dedupe한다 (양옆 공백 차이 무시)."""
        text = "<utterance>  안녕  </utterance>\n<utterance>안녕</utterance>"
        assert _extract_utterances(text) == "안녕"
