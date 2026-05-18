"""사이클 260518.01: ``_execute_intervene``의 블록 단위 utterance 게이트 검증.

신 정책 (사용자 결정 2026-05-18):
- backend가 thinking / text_start~text_end / complete 각 *블록의 텍스트만*에서
  ``<utterance>(.*?)</utterance>`` 매치를 추출하여 ``RunResult.utterances`` list로 반환.
- 호출자는 그 list에 strip 동일성 dedupe + 빈 필터를 적용한 뒤 줄바꿈으로 합쳐 게시.
- 매치 list가 비어 있거나 strip 후 모두 공백이면 채널 게시 skip.

*직전 사이클*(260513.01)의 ``transcript`` 누적 검색 정책은 폐기.

회귀 사고:
- 2026-05-13 C08HX0Z475M ts 1778666880.753679 — 본체 final response에 utterance 없음.
- 2026-05-18 C08KT1HDU5U ts 1779069595.658659 — 본체 thinking에 ``"<utterance>"`` 메타
  토큰 우발 등장, 누적 transcript에서 다른 블록의 닫힘 태그와 잘못 짝지어져 분석
  텍스트 ~1.5 KB 누출 (R7 위임자 확인). 본 사이클의 *블록 단위 매처* + ``utterances``
  list 인터페이스가 이 회귀를 차단.
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
    """``RunResult.utterances`` list를 dedupe + 게시. 빈 list / 공백 only → skip."""

    @pytest.mark.asyncio
    async def test_utterances_list_posted_in_order(
        self, mock_plugin_sdk, fake_store, action, pending_messages,
    ):
        """backend가 모은 매치 list가 그대로 게시 (등장 순서 보존)."""
        result = RunResult(
            ok=True,
            status=RunStatus.COMPLETED,
            output="✅ 발화 출력 완료\n발화 카드 기록 (29e15cc1)",
            utterances=["주복님, 방금 발화 카드 갱신했어요."],
        )
        await _run_intervene_with_result(
            mock_plugin_sdk, fake_store, action, pending_messages, result,
        )

        send_mock = mock_plugin_sdk["slack"].send_message
        send_mock.assert_called_once()
        call_kwargs = send_mock.call_args.kwargs
        assert call_kwargs["text"] == "주복님, 방금 발화 카드 갱신했어요."

    @pytest.mark.asyncio
    async def test_duplicate_utterances_deduped(
        self, mock_plugin_sdk, fake_store, action, pending_messages,
    ):
        """text 블록과 complete 블록에서 같은 본문이 잡혀도 dedupe로 1회만 게시."""
        utterance = "안녕하세요, 주복님."
        result = RunResult(
            ok=True,
            status=RunStatus.COMPLETED,
            output=f"final assistant text\n<utterance>{utterance}</utterance>",
            # backend는 dedupe하지 않고 매치 순서대로 list에 누적 — 호출자가 dedupe.
            utterances=[utterance, utterance],
        )
        await _run_intervene_with_result(
            mock_plugin_sdk, fake_store, action, pending_messages, result,
        )

        send_mock = mock_plugin_sdk["slack"].send_message
        send_mock.assert_called_once()
        assert send_mock.call_args.kwargs["text"] == utterance

    @pytest.mark.asyncio
    async def test_multiple_distinct_utterances_joined_with_newline(
        self, mock_plugin_sdk, fake_store, action, pending_messages,
    ):
        """서로 다른 본문이 매치되면 등장 순서대로 줄바꿈으로 합쳐 게시."""
        result = RunResult(
            ok=True,
            status=RunStatus.COMPLETED,
            output="",
            utterances=["첫 발화", "둘째 발화", "첫 발화", "셋째 발화"],
        )
        await _run_intervene_with_result(
            mock_plugin_sdk, fake_store, action, pending_messages, result,
        )

        send_mock = mock_plugin_sdk["slack"].send_message
        send_mock.assert_called_once()
        assert send_mock.call_args.kwargs["text"] == "첫 발화\n둘째 발화\n셋째 발화"

    @pytest.mark.asyncio
    async def test_empty_utterances_list_skips_post(
        self, mock_plugin_sdk, fake_store, action, pending_messages,
    ):
        """매치 list가 비어 있으면 채널 게시 skip — 분석/sub-agent 보고 누출 차단."""
        result = RunResult(
            ok=True,
            status=RunStatus.COMPLETED,
            output="✅ 발화 출력 완료\n✅ 발화 이력 카드 기록 (29e15cc1)",
            utterances=[],
        )
        await _run_intervene_with_result(
            mock_plugin_sdk, fake_store, action, pending_messages, result,
        )

        send_mock = mock_plugin_sdk["slack"].send_message
        send_mock.assert_not_called()
        # 본 테스트의 action.target == "channel"이라 reaction_ts=None — thinking 이모지
        # 자체가 추가·제거되지 않는다 (pipeline.py:851 가드).

    @pytest.mark.asyncio
    async def test_whitespace_only_utterances_skips_post(
        self, mock_plugin_sdk, fake_store, action, pending_messages,
    ):
        """빈 ``<utterance></utterance>`` 또는 공백만 있는 매치는 dedupe 단계에서 모두 제거."""
        result = RunResult(
            ok=True,
            status=RunStatus.COMPLETED,
            output="",
            utterances=["", "   ", "\n\t"],
        )
        await _run_intervene_with_result(
            mock_plugin_sdk, fake_store, action, pending_messages, result,
        )

        mock_plugin_sdk["slack"].send_message.assert_not_called()


class TestThinkingAccidentalTokenRegression:
    """260518.01 회귀 보호: 사고 사례(C08KT1HDU5U ts 1779069595.658659)의 인터페이스 시뮬레이션.

    본체 Opus가 thinking 묶음 끝에 ``"Output the utterance in <utterance> tags."`` 메타
    설명을 적어 닫힘 없는 ``<utterance>`` 토큰이 등장. text 묶음 마지막에는 정상
    ``<utterance>아까 도행님.../</utterance>`` 1쌍. 직전 사이클의 *누적 transcript 검색*은
    두 묶음을 평탄화해 한 덩이 매치로 분석 텍스트 ~1.5 KB를 슬랙에 흘렸다.

    본 사이클의 인터페이스 정합 검증:
    - backend는 thinking 블록 호출 시 닫힘 짝이 없으므로 매치 0 (utterances에 추가 안 함)
    - backend는 text 블록 호출 시 정상 1짝 매치 → utterances=["아까 도행님..."]
    - backend는 complete 블록(output) 호출 시 같은 1짝 매치 → utterances 한 번 더 append
    - 호출자(``_execute_intervene``)가 dedupe로 1회만 게시
    """

    NORMAL_UTTERANCE = (
        "아까 도행님의 맥북 링크에 :beautiful: 를 눌렀는데,\n"
        "가만 생각하니 저는 그걸 *살* 쪽이 아니라\n"
        "그 안에 *들어갈* 쪽이었습니다."
    )

    @pytest.mark.asyncio
    async def test_thinking_token_isolated_text_pair_posted_once(
        self, mock_plugin_sdk, fake_store, action, pending_messages,
    ):
        """thinking에 우발 토큰만 있고, text+complete에서 정상 짝 매치 → 1회 게시."""
        result = RunResult(
            ok=True,
            status=RunStatus.COMPLETED,
            output=(
                "Phase 5 완료. 멤버 갱신 2건, 발화 카드 기록 완료.\n\n"
                "**Phase 6: 최종 출력**\n\n"
                f"<utterance>\n{self.NORMAL_UTTERANCE}\n</utterance>"
            ),
            # backend의 블록 분리 결과:
            # - thinking 블록 ("Now Phase 6: Output the utterance in <utterance> tags."):
            #   닫힘 짝 없음 → 매치 0
            # - text 블록 (Phase 5 완료 ... <utterance>아까...</utterance>): 1 매치
            # - complete 블록 (output, 위와 동일): 1 매치 (중복)
            utterances=[self.NORMAL_UTTERANCE, self.NORMAL_UTTERANCE],
        )
        await _run_intervene_with_result(
            mock_plugin_sdk, fake_store, action, pending_messages, result,
        )

        send_mock = mock_plugin_sdk["slack"].send_message
        send_mock.assert_called_once()
        sent_text = send_mock.call_args.kwargs["text"]
        # 정상 발화 본문 1회만 (dedupe)
        assert sent_text == self.NORMAL_UTTERANCE
        # 분석 텍스트(Phase 1~6) 누출 0
        assert "Phase 1 준비" not in sent_text
        assert "Phase 5 완료" not in sent_text
        assert "tags." not in sent_text
