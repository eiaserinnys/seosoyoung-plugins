import pytest

from seosoyoung_plugins.sns_sourcing.collector import SnsCandidate
from seosoyoung_plugins.sns_sourcing.session import (
    build_classification_prompt,
    parse_decision_payload,
)


def test_prompt_makes_media_review_the_default_step():
    prompt = build_classification_prompt(
        [
            SnsCandidate(
                channel_id="C1",
                channel_name="art",
                ts="1.000001",
                thread_ts="1.000001",
                text="look",
                user="U1",
                permalink="https://slack/p1",
            )
        ]
    )

    assert "slack_download_thread_files" in prompt
    assert "/usr/bin/ffmpeg" in prompt
    assert "6~9" in prompt
    assert "후보는 이미지/영상 첨부가 있는 Slack 메시지" in prompt
    assert "먼저 미디어를 확인" in prompt
    assert "텍스트 맥락만으로 non_public이 명백" in prompt
    assert "비전 판독은 폴백" not in prompt


def test_parse_decision_payload_from_fenced_json():
    data = parse_decision_payload(
        """```json
        {"decisions":[{"channel_id":"C1","ts":"1.000001","label":"usable","reason":"good"}]}
        ```"""
    )

    assert data["decisions"][0]["label"] == "usable"


def test_parse_rejects_invalid_label():
    with pytest.raises(ValueError, match="invalid decision label"):
        parse_decision_payload(
            '{"decisions":[{"channel_id":"C1","ts":"1.000001","label":"maybe"}]}'
        )
