"""Tests for the <utterance> gate in _execute_intervene.

채널 개입 응답을 슬랙에 게시하는 게이트 정책 검증:
- 누적 텍스트(`RunResult.transcript`)나 final output 어디든 `<utterance>` 태그가
  있으면 그 발화만 게시한다.
- 어디에도 매칭이 없으면 채널에 게시하지 않는다 (sub-agent 작업 보고/분석 누출 차단).

회귀 사고: 2026-05-13 C08HX0Z475M ts 1778666880.753679. 본체 LLM final response에
utterance 태그가 없어 fallback으로 sub-agent 작업 요약 전체가 게시됨.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from seosoyoung.plugin_sdk.slack import ReactionResult, SendMessageResult
from seosoyoung.plugin_sdk.soulstream import RunResult, RunStatus
from seosoyoung_plugins.channel_observer.intervention import InterventionAction
from seosoyoung_plugins.channel_observer.pipeline import _execute_intervene

# patch_host_preferred_node fixture는 channel_observer/conftest.py에서 autouse로 제공됨.


@pytest.fixture
def fake_store():
    s = MagicMock()
    s.get_digest.return_value = None
    s.load_judged.return_value = []
    s.append_judged = MagicMock()
    return s


@pytest.fixture
def action():
    """target=channel로 하면 trigger_message 검색 분기를 우회한다 (테스트 단순화)."""
    return InterventionAction(type="message", target="channel", content="(reason)")


@pytest.fixture
def pending_messages():
    return [
        {"ts": "1100.0001", "user": "U1", "text": "이전 메시지"},
        {"ts": "1100.0002", "user": "U2", "text": "트리거 후보"},
    ]


async def _run_intervene_with_result(
    mock_plugin_sdk,
    fake_store,
    action,
    pending_messages,
    run_result: RunResult,
    *,
    bot_user_id: str = "U_BOT",
):
    """soulstream.run을 주어진 RunResult로 mock하여 _execute_intervene 실행."""
    mock_plugin_sdk["soulstream"].run = AsyncMock(return_value=run_result)
    mock_plugin_sdk["slack"].send_message = AsyncMock(
        return_value=SendMessageResult(ok=True, ts="2200.9999", channel="C_TEST"),
    )
    mock_plugin_sdk["slack"].add_reaction = AsyncMock(return_value=ReactionResult(ok=True))
    mock_plugin_sdk["slack"].remove_reaction = AsyncMock(return_value=ReactionResult(ok=True))
    mock_plugin_sdk["slack"].get_channel_history = AsyncMock(return_value=[])

    # llm_call=None → soulstream.run 경로 (intervene SKILL.md 정본)
    await _execute_intervene(
        store=fake_store,
        channel_id="C_TEST",
        action=action,
        pending_messages=pending_messages,
        observer_reason="reason",
        llm_call=None,
        bot_user_id=bot_user_id,
        thread_buffers=None,
    )


class TestUtteranceGate:
    """transcript 우선, output fallback, None이면 skip을 검증한다."""

    @pytest.mark.asyncio
    async def test_transcript_has_utterance_output_does_not(
        self, mock_plugin_sdk, fake_store, action, pending_messages,
    ):
        """transcript에만 utterance가 있으면 그 발화를 게시한다 (사고 시나리오 직접 fix)."""
        result = RunResult(
            ok=True,
            status=RunStatus.COMPLETED,
            output="✅ sub-agent 작업 보고만 있고 utterance 없음",
            transcript=(
                "중간 분석...\n"
                "<utterance>주복님, 방금 발화 카드 갱신했어요.</utterance>\n"
                "그 뒤 sub-agent 호출...\n"
                "✅ sub-agent 작업 보고만 있고 utterance 없음"
            ),
        )
        await _run_intervene_with_result(
            mock_plugin_sdk, fake_store, action, pending_messages, result,
        )

        send_mock = mock_plugin_sdk["slack"].send_message
        send_mock.assert_called_once()
        call_kwargs = send_mock.call_args.kwargs
        assert call_kwargs["text"] == "주복님, 방금 발화 카드 갱신했어요."

    @pytest.mark.asyncio
    async def test_transcript_and_output_both_have_same_utterance_dedupes(
        self, mock_plugin_sdk, fake_store, action, pending_messages,
    ):
        """transcript와 output에 같은 utterance가 있으면 한 번만 게시한다 (dedupe)."""
        utterance = "<utterance>안녕하세요, 주복님.</utterance>"
        result = RunResult(
            ok=True,
            status=RunStatus.COMPLETED,
            output=f"final assistant text\n{utterance}",
            transcript=f"중간 분석\n{utterance}\n뒤 작업\nfinal assistant text\n{utterance}",
        )
        await _run_intervene_with_result(
            mock_plugin_sdk, fake_store, action, pending_messages, result,
        )

        send_mock = mock_plugin_sdk["slack"].send_message
        send_mock.assert_called_once()
        assert send_mock.call_args.kwargs["text"] == "안녕하세요, 주복님."

    @pytest.mark.asyncio
    async def test_no_utterance_anywhere_skips_post(
        self, mock_plugin_sdk, fake_store, action, pending_messages,
    ):
        """transcript에도 output에도 utterance가 없으면 채널 게시 skip."""
        result = RunResult(
            ok=True,
            status=RunStatus.COMPLETED,
            output="✅ 발화 출력 완료\n✅ 발화 이력 카드 기록 (29e15cc1)",
            transcript=(
                "중간 분석 텍스트만 있음\n"
                "sub-agent 호출 결과: 작업 성공\n"
                "✅ 발화 출력 완료\n✅ 발화 이력 카드 기록 (29e15cc1)"
            ),
        )
        await _run_intervene_with_result(
            mock_plugin_sdk, fake_store, action, pending_messages, result,
        )

        send_mock = mock_plugin_sdk["slack"].send_message
        send_mock.assert_not_called()
        # 본 테스트의 action.target == "channel"이라 reaction_ts=None — thinking 이모지
        # 자체가 추가·제거되지 않는다 (pipeline.py:851 가드). 스레드 대상 개입에서는
        # remove_reaction이 호출되지만 그 케이스는 트리거 메시지 매칭이 필요해 본 단위
        # 테스트의 범위를 벗어난다. utterance 게이트의 동작 자체는 send_message 미호출로 검증.

    @pytest.mark.asyncio
    async def test_transcript_empty_output_has_utterance_falls_back(
        self, mock_plugin_sdk, fake_store, action, pending_messages,
    ):
        """transcript가 빈 문자열이면 output에서 utterance를 찾는다 (옛 backend 호환)."""
        result = RunResult(
            ok=True,
            status=RunStatus.COMPLETED,
            output="<utterance>fallback 발화</utterance>",
            transcript="",
        )
        await _run_intervene_with_result(
            mock_plugin_sdk, fake_store, action, pending_messages, result,
        )

        send_mock = mock_plugin_sdk["slack"].send_message
        send_mock.assert_called_once()
        assert send_mock.call_args.kwargs["text"] == "fallback 발화"

    @pytest.mark.asyncio
    async def test_both_empty_skips_post(
        self, mock_plugin_sdk, fake_store, action, pending_messages,
    ):
        """transcript와 output 모두 빈 문자열이면 게시 skip."""
        result = RunResult(
            ok=True,
            status=RunStatus.COMPLETED,
            output="",
            transcript="",
        )
        await _run_intervene_with_result(
            mock_plugin_sdk, fake_store, action, pending_messages, result,
        )

        mock_plugin_sdk["slack"].send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_utterance_tag_only_skips_post(
        self, mock_plugin_sdk, fake_store, action, pending_messages,
    ):
        """빈 utterance 태그만 있으면 게시 skip (dedupe 후 빈 문자열)."""
        result = RunResult(
            ok=True,
            status=RunStatus.COMPLETED,
            output="",
            transcript="<utterance></utterance>\n<utterance>   </utterance>",
        )
        await _run_intervene_with_result(
            mock_plugin_sdk, fake_store, action, pending_messages, result,
        )

        mock_plugin_sdk["slack"].send_message.assert_not_called()
