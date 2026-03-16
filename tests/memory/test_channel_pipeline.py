"""채널 소화/판단 파이프라인 통합 테스트"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from seosoyoung.plugin_sdk.soulstream import RunResult, RunStatus
from seosoyoung.slackbot.soulstream.session import SessionManager
from seosoyoung_plugins.channel_observer.intervention import InterventionAction, InterventionHistory
from seosoyoung_plugins.channel_observer.observer import (
    DigestCompressorResult,
    DigestResult,
    JudgeItem,
    JudgeResult,
)
from seosoyoung_plugins.channel_observer.pipeline import (
    _apply_importance_modifiers,
    _filter_already_reacted,
    _validate_linked_messages,
    run_channel_pipeline,
)
from seosoyoung_plugins.channel_observer.store import ChannelStore


@pytest.fixture
def store(tmp_path):
    return ChannelStore(base_dir=tmp_path)


@pytest.fixture
def channel_id():
    return "C_TEST_CHANNEL"


def _fill_pending(store: ChannelStore, channel_id: str, n: int = 10):
    """pending 버퍼에 테스트 메시지를 채운다."""
    for i in range(n):
        store.append_pending(channel_id, {
            "ts": f"100{i}.000",
            "user": f"U{i}",
            "text": f"테스트 메시지 {i}번 - " + "내용 " * 20,
        })


def _fill_judged(store: ChannelStore, channel_id: str, n: int = 5):
    """judged 버퍼에 테스트 메시지를 채운다."""
    messages = []
    for i in range(n):
        messages.append({
            "ts": f"200{i}.000",
            "user": f"U{i}",
            "text": f"판단 완료 메시지 {i}번 - " + "내용 " * 20,
        })
    store.append_judged(channel_id, messages)


class FakeObserver:
    """ChannelObserver mock (digest + judge)"""

    def __init__(
        self,
        digest_result: DigestResult | None = None,
        judge_result: JudgeResult | None = None,
    ):
        self.digest_result = digest_result or DigestResult(
            digest="새로운 digest 결과",
            token_count=100,
        )
        self.judge_result = judge_result or JudgeResult(
            importance=4,
            reaction_type="none",
        )
        self.digest_call_count = 0
        self.judge_call_count = 0
        self.judge_kwargs = {}

    async def digest(self, **kwargs) -> DigestResult | None:
        self.digest_call_count += 1
        return self.digest_result

    async def judge(self, **kwargs) -> JudgeResult | None:
        self.judge_call_count += 1
        self.judge_kwargs = kwargs
        return self.judge_result


class FakeCompressor:
    """DigestCompressor mock"""

    def __init__(self, result: DigestCompressorResult | None = None):
        self.result = result or DigestCompressorResult(
            digest="압축된 digest",
            token_count=100,
        )
        self.call_count = 0

    async def compress(self, **kwargs) -> DigestCompressorResult | None:
        self.call_count += 1
        return self.result


# ── run_channel_pipeline 테스트 ──────────────────────────

class TestRunChannelPipeline:
    """소화/판단 분리 파이프라인 통합 테스트"""

    @pytest.mark.asyncio
    async def test_skip_when_pending_below_threshold_a(self, store, channel_id, mock_plugin_sdk):
        """pending 토큰이 threshold_A 미만이면 스킵"""
        store.append_pending(channel_id, {
            "ts": "1.1", "user": "U1", "text": "짧은 메시지",
        })
        observer = FakeObserver()
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=99999,
        )

        assert observer.judge_call_count == 0
        assert observer.digest_call_count == 0
        # pending은 그대로 유지
        assert len(store.load_pending(channel_id)) == 1

    @pytest.mark.asyncio
    async def test_judge_called_when_above_threshold_a(self, store, channel_id, mock_plugin_sdk):
        """pending이 threshold_A 이상이면 judge 호출"""
        _fill_pending(store, channel_id)
        observer = FakeObserver()
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            threshold_b=999999,
        )

        assert observer.judge_call_count == 1
        # digest는 threshold_b 이하이므로 호출 안 됨
        assert observer.digest_call_count == 0

    @pytest.mark.asyncio
    async def test_pending_moved_to_judged_after_pipeline(self, store, channel_id, mock_plugin_sdk):
        """파이프라인 실행 후 pending이 judged로 이동"""
        _fill_pending(store, channel_id, n=5)
        observer = FakeObserver()
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            threshold_b=999999,
        )

        # pending은 비어야 하고 judged에 이동
        assert len(store.load_pending(channel_id)) == 0
        assert len(store.load_judged(channel_id)) == 5

    @pytest.mark.asyncio
    async def test_digest_triggered_when_above_threshold_b(self, store, channel_id, mock_plugin_sdk):
        """judged+pending이 threshold_B 초과하면 digest 호출"""
        _fill_judged(store, channel_id, n=10)
        _fill_pending(store, channel_id, n=10)
        observer = FakeObserver()
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            threshold_b=1,  # 매우 낮은 임계치
        )

        assert observer.digest_call_count == 1
        assert observer.judge_call_count == 1
        # digest 저장 확인
        saved = store.get_digest(channel_id)
        assert saved is not None
        assert saved["content"] == "새로운 digest 결과"

    @pytest.mark.asyncio
    async def test_digest_clears_judged(self, store, channel_id, mock_plugin_sdk):
        """digest 편입 후 judged가 비워짐"""
        _fill_judged(store, channel_id, n=5)
        _fill_pending(store, channel_id, n=5)
        observer = FakeObserver()
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            threshold_b=1,
        )

        # digest 편입으로 judged 비워진 후, pending이 judged로 이동
        judged = store.load_judged(channel_id)
        assert len(judged) == 5  # pending에서 이동된 것만

    @pytest.mark.asyncio
    async def test_digest_compressor_triggered(self, store, channel_id, mock_plugin_sdk):
        """digest 토큰이 max 초과하면 compressor 호출"""
        _fill_judged(store, channel_id, n=5)
        _fill_pending(store, channel_id, n=5)

        long_digest = DigestResult(
            digest="장문의 digest " * 500,
            token_count=20000,
        )
        observer = FakeObserver(digest_result=long_digest)
        compressor = FakeCompressor()
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            threshold_b=1,
            compressor=compressor,
            digest_max_tokens=10,
            digest_target_tokens=5,
        )

        assert compressor.call_count == 1
        saved = store.get_digest(channel_id)
        assert saved["content"] == "압축된 digest"

    @pytest.mark.asyncio
    async def test_no_compressor_when_under_max(self, store, channel_id, mock_plugin_sdk):
        """digest 토큰이 max 이하면 compressor 호출 안 함"""
        _fill_judged(store, channel_id, n=5)
        _fill_pending(store, channel_id, n=5)
        observer = FakeObserver()
        compressor = FakeCompressor()
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            threshold_b=1,
            compressor=compressor,
            digest_max_tokens=999999,
        )

        assert compressor.call_count == 0

    @pytest.mark.asyncio
    async def test_judge_returns_none(self, store, channel_id, mock_plugin_sdk):
        """judge가 None을 반환하면 파이프라인 중단"""
        _fill_pending(store, channel_id)

        observer = FakeObserver()
        observer.judge_result = None
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
        )

        # pending은 이동되지 않음 (judge 실패)
        assert len(store.load_pending(channel_id)) > 0

    @pytest.mark.asyncio
    async def test_react_action_executed(self, store, channel_id, mock_plugin_sdk):
        """judge가 react를 반환하면 이모지 리액션 실행"""
        _fill_pending(store, channel_id)
        observer = FakeObserver(judge_result=JudgeResult(
            importance=7,
            reaction_type="react",
            reaction_target="1001.000",
            reaction_content="laughing",
        ))
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
        )

        # 이모지 리액션 API 호출 확인
        mock_plugin_sdk["slack"].add_reaction.assert_called_once_with(
            channel=channel_id,
            ts="1001.000",
            emoji="laughing",
        )

    @pytest.mark.asyncio
    async def test_intervene_action_with_llm(self, store, channel_id, mock_plugin_sdk):
        """judge가 intervene을 반환하면 LLM으로 응답 생성"""
        _fill_pending(store, channel_id)
        observer = FakeObserver(judge_result=JudgeResult(
            importance=8,
            reaction_type="intervene",
            reaction_target="1005.000",
            reaction_content="이 대화에 끼어들어야 할 것 같습니다",
        ))
        history = InterventionHistory(base_dir=store.base_dir)
        mock_llm = AsyncMock(return_value="흥미로운 이야기로군요.")

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            intervention_threshold=0.0,  # 항상 통과
            llm_call=mock_llm,
        )

        # soulstream.run이 호출되고 슬랙에 발송됨
        mock_plugin_sdk["soulstream"].run.assert_awaited_once()
        mock_plugin_sdk["slack"].send_message.assert_called()

    @pytest.mark.asyncio
    async def test_intervene_without_llm_fallback(self, store, channel_id, mock_plugin_sdk):
        """llm_call이 없으면 직접 발송"""
        _fill_pending(store, channel_id)
        observer = FakeObserver(judge_result=JudgeResult(
            importance=8,
            reaction_type="intervene",
            reaction_target="channel",
            reaction_content="직접 발송 텍스트",
        ))
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            intervention_threshold=0.0,
            llm_call=None,
        )

        mock_plugin_sdk["slack"].send_message.assert_called()

    @pytest.mark.asyncio
    async def test_debug_log_sent(self, store, channel_id, mock_plugin_sdk):
        """디버그 채널에 로그 전송"""
        _fill_pending(store, channel_id)
        observer = FakeObserver()
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            debug_channel="C_DEBUG",
        )

        # 디버그 채널에 로그가 전송됨
        calls = mock_plugin_sdk["slack"].send_message.call_args_list
        debug_calls = [c for c in calls if c[1].get("channel") == "C_DEBUG"]
        assert len(debug_calls) >= 1

    @pytest.mark.asyncio
    async def test_thread_buffers_passed_to_judge(self, store, channel_id, mock_plugin_sdk):
        """스레드 버퍼가 judge에 전달되는지 확인"""
        _fill_pending(store, channel_id)
        store.append_thread_message(channel_id, "ts_a", {
            "ts": "9001.000", "user": "U99", "text": "스레드 대화 내용",
        })
        observer = FakeObserver()
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            threshold_b=999999,
        )

        assert observer.judge_call_count == 1
        assert "thread_buffers" in observer.judge_kwargs
        thread_buffers = observer.judge_kwargs["thread_buffers"]
        assert "ts_a" in thread_buffers
        assert thread_buffers["ts_a"][0]["text"] == "스레드 대화 내용"

    @pytest.mark.asyncio
    async def test_thread_buffers_cleared_after_pipeline(self, store, channel_id, mock_plugin_sdk):
        """파이프라인 실행 후 스레드 버퍼도 judged로 이동되고 비워짐"""
        _fill_pending(store, channel_id, n=5)
        store.append_thread_message(channel_id, "ts_a", {
            "ts": "9001.000", "user": "U99", "text": "스레드 메시지",
        })
        observer = FakeObserver()
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            threshold_b=999999,
        )

        # 스레드 버퍼는 비어야 함
        assert store.load_all_thread_buffers(channel_id) == {}
        # pending + 스레드가 모두 judged로 이동
        judged = store.load_judged(channel_id)
        assert len(judged) == 6  # 5 pending + 1 thread

    @pytest.mark.asyncio
    async def test_existing_digest_passed_to_judge(self, store, channel_id, mock_plugin_sdk):
        """기존 digest가 judge에 전달되는지 확인"""
        store.save_digest(channel_id, "이전 digest", {"token_count": 50})
        _fill_pending(store, channel_id)
        observer = FakeObserver()
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
        )

        assert observer.judge_call_count == 1


# ── 확률 기반 개입 판단 테스트 ────────────────────────────

class TestProbabilityBasedIntervention:
    """확률 기반 개입 통과/차단 테스트"""

    @pytest.mark.asyncio
    async def test_threshold_zero_always_passes(self, store, channel_id, mock_plugin_sdk):
        """임계치 0이면 항상 개입 통과"""
        _fill_pending(store, channel_id)
        observer = FakeObserver(judge_result=JudgeResult(
            importance=1,  # 아주 낮은 중요도
            reaction_type="intervene",
            reaction_target="channel",
            reaction_content="개입",
        ))
        history = InterventionHistory(base_dir=store.base_dir)
        mock_llm = AsyncMock(return_value="응답")

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            intervention_threshold=0.0,
            llm_call=mock_llm,
        )

        mock_plugin_sdk["soulstream"].run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_threshold_one_blocks_most(self, store, channel_id, mock_plugin_sdk):
        """임계치 1.0이면 대부분 차단 (importance/10 * prob ≈ 0.9 최대)"""
        _fill_pending(store, channel_id)
        observer = FakeObserver(judge_result=JudgeResult(
            importance=8,
            reaction_type="intervene",
            reaction_target="channel",
            reaction_content="개입",
        ))
        history = InterventionHistory(base_dir=store.base_dir)
        mock_llm = AsyncMock(return_value="응답")

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            intervention_threshold=1.0,
            llm_call=mock_llm,
        )

        # 임계치 1.0은 (importance/10) * prob < 1.0이므로 차단
        mock_plugin_sdk["soulstream"].run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_probability_debug_log_sent(self, store, channel_id, mock_plugin_sdk):
        """개입 시도 시 확률 디버그 로그가 전송됨"""
        _fill_pending(store, channel_id)
        observer = FakeObserver(judge_result=JudgeResult(
            importance=8,
            reaction_type="intervene",
            reaction_target="channel",
            reaction_content="개입",
        ))
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            intervention_threshold=0.0,
            debug_channel="C_DEBUG",
        )

        # 디버그 채널에 확률 판단 로그가 전송됨
        calls = mock_plugin_sdk["slack"].send_message.call_args_list
        debug_calls = [c for c in calls if c[1].get("channel") == "C_DEBUG"]
        debug_texts = [c[1]["text"] for c in debug_calls]
        assert any("개입 확률 판단" in t for t in debug_texts)




# ── 복수 판단 파이프라인 테스트 ────────────────────────────

class TestMultiJudgePipeline:
    """JudgeResult.items를 사용하는 복수 판단 파이프라인 테스트"""

    @pytest.mark.asyncio
    async def test_multi_react_all_executed(self, store, channel_id, mock_plugin_sdk):
        """복수 react 판단이 모두 실행됨"""
        _fill_pending(store, channel_id)
        observer = FakeObserver(judge_result=JudgeResult(
            items=[
                JudgeItem(ts="1001.000", importance=5, reaction_type="react",
                          reaction_target="1001.000", reaction_content="laughing"),
                JudgeItem(ts="1003.000", importance=4, reaction_type="react",
                          reaction_target="1003.000", reaction_content="eyes"),
                JudgeItem(ts="1005.000", importance=2, reaction_type="none"),
            ],
        ))
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
        )

        # 2개 이모지 리액션 실행
        assert mock_plugin_sdk["slack"].add_reaction.call_count == 2
        call_args_list = [c[1] for c in mock_plugin_sdk["slack"].add_reaction.call_args_list]
        emojis = {c["emoji"] for c in call_args_list}
        assert emojis == {"laughing", "eyes"}

    @pytest.mark.asyncio
    async def test_multi_react_plus_intervene(self, store, channel_id, mock_plugin_sdk):
        """react + intervene이 섞인 경우: react 일괄 + intervene 확률 판단"""
        _fill_pending(store, channel_id)
        observer = FakeObserver(judge_result=JudgeResult(
            items=[
                JudgeItem(ts="1001.000", importance=4, reaction_type="react",
                          reaction_target="1001.000", reaction_content="fire"),
                JudgeItem(ts="1005.000", importance=8, reaction_type="intervene",
                          reaction_target="channel",
                          reaction_content="흥미로운 대화로군요"),
            ],
        ))
        history = InterventionHistory(base_dir=store.base_dir)
        mock_llm = AsyncMock(return_value="서소영 응답")

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            intervention_threshold=0.0,
            llm_call=mock_llm,
        )

        # react 실행됨
        mock_plugin_sdk["slack"].add_reaction.assert_called_once()
        # intervene도 실행됨
        mock_plugin_sdk["soulstream"].run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_multi_all_none(self, store, channel_id, mock_plugin_sdk):
        """모든 판단이 none이면 아무 액션도 없음"""
        _fill_pending(store, channel_id)
        observer = FakeObserver(judge_result=JudgeResult(
            items=[
                JudgeItem(ts="1001.000", importance=1, reaction_type="none"),
                JudgeItem(ts="1002.000", importance=2, reaction_type="none"),
            ],
        ))
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
        )

        mock_plugin_sdk["slack"].add_reaction.assert_not_called()
        mock_plugin_sdk["slack"].send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_multi_pending_moved_to_judged(self, store, channel_id, mock_plugin_sdk):
        """복수 판단 후에도 pending이 judged로 이동"""
        _fill_pending(store, channel_id, n=5)
        observer = FakeObserver(judge_result=JudgeResult(
            items=[
                JudgeItem(ts="1001.000", importance=5, reaction_type="react",
                          reaction_target="1001.000", reaction_content="eyes"),
            ],
        ))
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
        )

        assert len(store.load_pending(channel_id)) == 0
        assert len(store.load_judged(channel_id)) == 5

    @pytest.mark.asyncio
    async def test_multi_debug_log_sent(self, store, channel_id, mock_plugin_sdk):
        """복수 판단 시 디버그 로그가 전송됨"""
        _fill_pending(store, channel_id)
        observer = FakeObserver(judge_result=JudgeResult(
            items=[
                JudgeItem(ts="1001.000", importance=3, reaction_type="none"),
                JudgeItem(ts="1002.000", importance=5, reaction_type="react",
                          reaction_target="1002.000", reaction_content="laughing"),
            ],
        ))
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            debug_channel="C_DEBUG",
        )

        debug_calls = [c for c in mock_plugin_sdk["slack"].send_message.call_args_list
                       if c[1].get("channel") == "C_DEBUG"]
        assert len(debug_calls) >= 1
        # 복수 판단 로그에 메시지 수 포함
        fallback = debug_calls[0][1]["text"]
        assert "2 messages" in fallback

    @pytest.mark.asyncio
    async def test_multi_intervene_threshold_blocks(self, store, channel_id, mock_plugin_sdk):
        """복수 판단에서 확률 임계치가 intervene을 차단"""
        _fill_pending(store, channel_id)
        observer = FakeObserver(judge_result=JudgeResult(
            items=[
                JudgeItem(ts="1001.000", importance=3, reaction_type="react",
                          reaction_target="1001.000", reaction_content="eyes"),
                JudgeItem(ts="1005.000", importance=3, reaction_type="intervene",
                          reaction_target="channel", reaction_content="개입"),
            ],
        ))
        history = InterventionHistory(base_dir=store.base_dir)
        mock_llm = AsyncMock(return_value="응답")

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            intervention_threshold=1.0,  # 높은 임계치 → 차단
            llm_call=mock_llm,
        )

        # react는 실행됨
        mock_plugin_sdk["slack"].add_reaction.assert_called_once()
        # intervene은 차단됨
        mock_plugin_sdk["soulstream"].run.assert_not_awaited()


# ── _apply_importance_modifiers 테스트 ──────────────────

class TestApplyImportanceModifiers:
    """related_to_me 가중치와 addressed_to_me 강제 반응 로직 테스트"""

    def test_related_to_me_doubles_importance(self):
        """related_to_me=True → importance × 2"""
        result = JudgeResult(items=[
            JudgeItem(ts="1.1", importance=3, related_to_me=True, reaction_type="react",
                      reaction_target="1.1", reaction_content="eyes"),
        ])
        _apply_importance_modifiers(result, [{"ts": "1.1", "user": "U1", "text": "hi"}])
        assert result.items[0].importance == 6  # 3 × 2

    def test_related_to_me_caps_at_10(self):
        """related_to_me 가중치는 10을 초과하지 않음"""
        result = JudgeResult(items=[
            JudgeItem(ts="1.1", importance=7, related_to_me=True, reaction_type="none"),
        ])
        _apply_importance_modifiers(result, [{"ts": "1.1", "user": "U1", "text": "hi"}])
        assert result.items[0].importance == 10  # min(14, 10)

    def test_related_to_me_false_no_change(self):
        """related_to_me=False → importance 변경 없음"""
        result = JudgeResult(items=[
            JudgeItem(ts="1.1", importance=3, related_to_me=False, reaction_type="none"),
        ])
        _apply_importance_modifiers(result, [{"ts": "1.1", "user": "U1", "text": "hi"}])
        assert result.items[0].importance == 3

    def test_addressed_to_me_human_forces_intervene(self):
        """addressed_to_me=True + 사람 → importance 최소 7, intervene 전환"""
        result = JudgeResult(items=[
            JudgeItem(ts="1.1", importance=3, addressed_to_me=True,
                      reaction_type="react", reaction_target="1.1", reaction_content="eyes"),
        ])
        pending = [{"ts": "1.1", "user": "U1", "text": "소영아"}]
        _apply_importance_modifiers(result, pending)
        item = result.items[0]
        assert item.importance == 7
        assert item.reaction_type == "intervene"

    def test_addressed_to_me_bot_no_force(self):
        """addressed_to_me=True + 봇 → 강제 반응 안 함"""
        result = JudgeResult(items=[
            JudgeItem(ts="1.1", importance=3, addressed_to_me=True,
                      reaction_type="react", reaction_target="1.1", reaction_content="eyes"),
        ])
        pending = [{"ts": "1.1", "user": "U1", "text": "소영아", "bot_id": "B123"}]
        _apply_importance_modifiers(result, pending)
        item = result.items[0]
        assert item.importance == 3  # 변경 없음
        assert item.reaction_type == "react"  # 변경 없음

    def test_addressed_to_me_already_intervene_keeps(self):
        """addressed_to_me=True + 이미 intervene → 유지"""
        result = JudgeResult(items=[
            JudgeItem(ts="1.1", importance=9, addressed_to_me=True,
                      reaction_type="intervene", reaction_target="channel",
                      reaction_content="이미 개입 내용"),
        ])
        pending = [{"ts": "1.1", "user": "U1", "text": "소영아"}]
        _apply_importance_modifiers(result, pending)
        item = result.items[0]
        assert item.importance == 9
        assert item.reaction_type == "intervene"
        assert item.reaction_content == "이미 개입 내용"

    def test_both_related_and_addressed(self):
        """related_to_me + addressed_to_me 동시 적용"""
        result = JudgeResult(items=[
            JudgeItem(ts="1.1", importance=3, related_to_me=True,
                      addressed_to_me=True, reaction_type="none"),
        ])
        pending = [{"ts": "1.1", "user": "U1", "text": "소영아"}]
        _apply_importance_modifiers(result, pending)
        item = result.items[0]
        # related_to_me: 3 * 2 = 6, addressed_to_me: max(6, 7) = 7
        assert item.importance == 7
        assert item.reaction_type == "intervene"


class TestValidateLinkedMessages:
    """linked_message_ts 환각 방지 검증 테스트"""

    def test_valid_linked_ts_preserved(self):
        """실제로 존재하는 ts에 대한 링크는 유지"""
        result = JudgeResult(items=[
            JudgeItem(ts="2.2", linked_message_ts="1.1", link_reason="답변"),
        ])
        judged = [{"ts": "1.1", "user": "U1", "text": "hello"}]
        pending = [{"ts": "2.2", "user": "U2", "text": "reply"}]
        _validate_linked_messages(result, judged, pending)
        assert result.items[0].linked_message_ts == "1.1"
        assert result.items[0].link_reason == "답변"

    def test_hallucinated_ts_removed(self):
        """존재하지 않는 ts에 대한 링크는 제거"""
        result = JudgeResult(items=[
            JudgeItem(ts="2.2", linked_message_ts="999.999", link_reason="환각"),
        ])
        judged = [{"ts": "1.1", "user": "U1", "text": "hello"}]
        pending = [{"ts": "2.2", "user": "U2", "text": "reply"}]
        _validate_linked_messages(result, judged, pending)
        assert result.items[0].linked_message_ts is None
        assert result.items[0].link_reason is None

    def test_no_linked_ts_unaffected(self):
        """linked_message_ts가 None인 아이템은 변경 없음"""
        result = JudgeResult(items=[
            JudgeItem(ts="1.1", linked_message_ts=None, link_reason=None),
        ])
        _validate_linked_messages(result, [], [{"ts": "1.1"}])
        assert result.items[0].linked_message_ts is None

    def test_link_to_pending_message(self):
        """같은 pending 배치 내 메시지에 대한 링크도 유효"""
        result = JudgeResult(items=[
            JudgeItem(ts="2.2", linked_message_ts="1.1", link_reason="같은 배치"),
        ])
        pending = [
            {"ts": "1.1", "user": "U1", "text": "first"},
            {"ts": "2.2", "user": "U2", "text": "reply"},
        ]
        _validate_linked_messages(result, [], pending)
        assert result.items[0].linked_message_ts == "1.1"

    def test_self_link_removed(self):
        """자기 자신을 가리키는 링크는 제거"""
        result = JudgeResult(items=[
            JudgeItem(ts="1.1", linked_message_ts="1.1", link_reason="self"),
        ])
        pending = [{"ts": "1.1", "user": "U1", "text": "msg"}]
        _validate_linked_messages(result, [], pending)
        assert result.items[0].linked_message_ts is None
        assert result.items[0].link_reason is None


class TestBotResponseRecordedInJudged:
    """봇 개입 응답 ts가 judged에 기록되는지 테스트"""

    @pytest.mark.asyncio
    async def test_bot_response_ts_appended_to_judged(self, store, channel_id, mock_plugin_sdk):
        """개입 후 봇 응답이 judged에 기록됨"""
        from seosoyoung.plugin_sdk.slack import SendMessageResult

        _fill_pending(store, channel_id)
        observer = FakeObserver(judge_result=JudgeResult(
            importance=8,
            reaction_type="intervene",
            reaction_target="1005.000",
            reaction_content="개입해야 함",
        ))
        history = InterventionHistory(base_dir=store.base_dir)
        mock_llm = AsyncMock(return_value="개입 메시지입니다.")

        # soulstream mock 응답 설정
        mock_plugin_sdk["soulstream"].run.return_value = RunResult(
            ok=True, status=RunStatus.COMPLETED, output="개입 메시지입니다.",
        )

        # 특정 ts 반환하도록 설정
        mock_plugin_sdk["slack"].send_message.return_value = SendMessageResult(
            ok=True, ts="9999.000", channel=channel_id
        )

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            intervention_threshold=0.0,
            llm_call=mock_llm,
            bot_user_id="BOT_U123",
        )

        # judged에 봇 응답이 포함되어야 함
        judged = store.load_judged(channel_id)
        bot_msgs = [m for m in judged if m.get("user") == "BOT_U123"]
        assert len(bot_msgs) == 1
        assert bot_msgs[0]["ts"] == "9999.000"
        assert bot_msgs[0]["text"] == "개입 메시지입니다."


class TestInterventionSessionCreation:
    """개입 후 세션이 생성되는지 테스트 (Phase 3-1)"""

    @pytest.mark.asyncio
    async def test_session_created_after_intervene(self, store, channel_id, mock_plugin_sdk):
        """개입 응답 후 응답 ts로 세션이 생성되어야 함"""
        from seosoyoung.plugin_sdk.slack import SendMessageResult

        _fill_pending(store, channel_id)
        observer = FakeObserver(judge_result=JudgeResult(
            importance=8,
            reaction_type="intervene",
            reaction_target="1005.000",
            reaction_content="개입해야 함",
        ))
        history = InterventionHistory(base_dir=store.base_dir)
        mock_llm = AsyncMock(return_value="개입 메시지입니다.")

        # soulstream mock 응답 설정
        mock_plugin_sdk["soulstream"].run.return_value = RunResult(
            ok=True, status=RunStatus.COMPLETED, output="개입 메시지입니다.",
        )

        # 특정 ts 반환하도록 설정
        mock_plugin_sdk["slack"].send_message.return_value = SendMessageResult(
            ok=True, ts="9999.000", channel=channel_id
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            session_mgr = SessionManager(session_dir=Path(tmpdir))

            await run_channel_pipeline(
                store=store,
                observer=observer,
                channel_id=channel_id,
                cooldown=history,
                threshold_a=1,
                intervention_threshold=0.0,
                llm_call=mock_llm,
                bot_user_id="BOT_U123",
                session_manager=session_mgr,
            )

            # 봇 응답 ts로 세션이 생성되어야 함
            session = session_mgr.get("9999.000")
            assert session is not None
            assert session.thread_ts == "9999.000"
            assert session.channel_id == channel_id
            assert session.source_type == "hybrid"
            assert session.user_id == ""  # 아직 지시자 없음

    @pytest.mark.asyncio
    async def test_session_not_created_without_session_manager(self, store, channel_id, mock_plugin_sdk):
        """session_manager가 없으면 세션 생성을 건너뜀 (호환성)"""
        from seosoyoung.plugin_sdk.slack import SendMessageResult

        _fill_pending(store, channel_id)
        observer = FakeObserver(judge_result=JudgeResult(
            importance=8,
            reaction_type="intervene",
            reaction_target="1005.000",
            reaction_content="개입해야 함",
        ))
        history = InterventionHistory(base_dir=store.base_dir)
        mock_llm = AsyncMock(return_value="개입 메시지입니다.")

        # soulstream mock 응답 설정
        mock_plugin_sdk["soulstream"].run.return_value = RunResult(
            ok=True, status=RunStatus.COMPLETED, output="개입 메시지입니다.",
        )

        # 특정 ts 반환하도록 설정
        mock_plugin_sdk["slack"].send_message.return_value = SendMessageResult(
            ok=True, ts="9999.000", channel=channel_id
        )

        # session_manager 전달 안 함 → 에러 없이 정상 동작
        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            intervention_threshold=0.0,
            llm_call=mock_llm,
            bot_user_id="BOT_U123",
        )

        # judged에는 기록됨 (기존 동작)
        judged = store.load_judged(channel_id)
        bot_msgs = [m for m in judged if m.get("user") == "BOT_U123"]
        assert len(bot_msgs) == 1

    @pytest.mark.asyncio
    async def test_channel_target_session_has_no_thread(self, store, channel_id, mock_plugin_sdk):
        """target이 'channel'이면 세션 생성하지 않음 (스레드 대화 불가)"""
        _fill_pending(store, channel_id)
        observer = FakeObserver(judge_result=JudgeResult(
            importance=8,
            reaction_type="intervene",
            reaction_target="channel",
            reaction_content="채널 전체에 개입",
        ))
        history = InterventionHistory(base_dir=store.base_dir)
        mock_llm = AsyncMock(return_value="채널 개입 메시지.")

        with tempfile.TemporaryDirectory() as tmpdir:
            session_mgr = SessionManager(session_dir=Path(tmpdir))

            await run_channel_pipeline(
                store=store,
                observer=observer,
                channel_id=channel_id,
                cooldown=history,
                threshold_a=1,
                intervention_threshold=0.0,
                llm_call=mock_llm,
                bot_user_id="BOT_U123",
                session_manager=session_mgr,
            )

            # channel 대상 개입은 세션 생성 안 함
            assert session_mgr.get("9999.000") is None


# ── _filter_already_reacted 테스트 ──────────────────────

class TestFilterAlreadyReacted:
    """봇이 이미 리액션한 메시지에 대한 react 중복 방지 테스트"""

    def test_filters_out_already_reacted(self):
        """봇이 이미 리액션한 메시지는 필터링됨"""
        actions = [
            InterventionAction(type="react", target="1.1", content="laughing"),
        ]
        pending = [{
            "ts": "1.1", "user": "U1", "text": "hi",
            "reactions": [{"name": "laughing", "users": ["BOT_U1"], "count": 1}],
        }]
        result = _filter_already_reacted(actions, pending, bot_user_id="BOT_U1")
        assert len(result) == 0

    def test_keeps_different_emoji(self):
        """봇이 다른 이모지로 리액션한 경우는 유지"""
        actions = [
            InterventionAction(type="react", target="1.1", content="fire"),
        ]
        pending = [{
            "ts": "1.1", "user": "U1", "text": "hi",
            "reactions": [{"name": "laughing", "users": ["BOT_U1"], "count": 1}],
        }]
        result = _filter_already_reacted(actions, pending, bot_user_id="BOT_U1")
        assert len(result) == 1

    def test_keeps_reaction_by_other_user(self):
        """다른 사용자의 리액션은 봇 리액션으로 취급하지 않음"""
        actions = [
            InterventionAction(type="react", target="1.1", content="laughing"),
        ]
        pending = [{
            "ts": "1.1", "user": "U1", "text": "hi",
            "reactions": [{"name": "laughing", "users": ["U2"], "count": 1}],
        }]
        result = _filter_already_reacted(actions, pending, bot_user_id="BOT_U1")
        assert len(result) == 1

    def test_no_bot_user_id_passes_all(self):
        """bot_user_id가 None이면 필터링 없이 모두 통과"""
        actions = [
            InterventionAction(type="react", target="1.1", content="laughing"),
        ]
        pending = [{
            "ts": "1.1", "user": "U1", "text": "hi",
            "reactions": [{"name": "laughing", "users": ["BOT_U1"], "count": 1}],
        }]
        result = _filter_already_reacted(actions, pending, bot_user_id=None)
        assert len(result) == 1

    def test_no_reactions_field_passes(self):
        """reactions 필드가 없는 메시지는 필터링 안 됨"""
        actions = [
            InterventionAction(type="react", target="1.1", content="laughing"),
        ]
        pending = [{"ts": "1.1", "user": "U1", "text": "hi"}]
        result = _filter_already_reacted(actions, pending, bot_user_id="BOT_U1")
        assert len(result) == 1

    def test_mixed_actions_partial_filter(self):
        """일부만 중복인 경우 중복만 필터링"""
        actions = [
            InterventionAction(type="react", target="1.1", content="laughing"),
            InterventionAction(type="react", target="2.2", content="eyes"),
        ]
        pending = [
            {
                "ts": "1.1", "user": "U1", "text": "hi",
                "reactions": [{"name": "laughing", "users": ["BOT_U1"], "count": 1}],
            },
            {"ts": "2.2", "user": "U2", "text": "hello"},
        ]
        result = _filter_already_reacted(actions, pending, bot_user_id="BOT_U1")
        assert len(result) == 1
        assert result[0].target == "2.2"

    @pytest.mark.asyncio
    async def test_pipeline_skips_already_reacted(self, store, channel_id, mock_plugin_sdk):
        """파이프라인에서 봇이 이미 리액션한 메시지에 대해 중복 리액션하지 않음"""
        # pending에 봇 리액션이 이미 기록된 메시지 추가
        for i in range(5):
            msg = {
                "ts": f"100{i}.000",
                "user": f"U{i}",
                "text": f"테스트 메시지 {i}번 - " + "내용 " * 20,
            }
            if i == 1:
                msg["reactions"] = [
                    {"name": "laughing", "users": ["BOT_U1"], "count": 1}
                ]
            store.append_pending(channel_id, msg)

        # judge가 이미 리액션한 메시지에 또 react를 판단
        observer = FakeObserver(judge_result=JudgeResult(
            items=[
                JudgeItem(ts="1001.000", importance=5, reaction_type="react",
                          reaction_target="1001.000", reaction_content="laughing"),
                JudgeItem(ts="1003.000", importance=4, reaction_type="react",
                          reaction_target="1003.000", reaction_content="eyes"),
            ],
        ))
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            bot_user_id="BOT_U1",
        )

        # 1001.000은 이미 laughing 리액션 있으므로 스킵, 1003.000만 실행
        assert mock_plugin_sdk["slack"].add_reaction.call_count == 1
        call_kwargs = mock_plugin_sdk["slack"].add_reaction.call_args[1]
        assert call_kwargs["ts"] == "1003.000"
        assert call_kwargs["emoji"] == "eyes"


# ── Bug A: move_snapshot_to_judged가 예외 시에도 실행되는지 ──

class TestBugA_MoveSnapshotInFinally:
    """Bug A: _handle_multi_judge 예외 시에도 pending이 judged로 이동되는지 확인"""

    @pytest.mark.asyncio
    async def test_pending_moved_even_on_exception(self, store, channel_id, mock_plugin_sdk):
        """_handle_multi_judge에서 예외 발생해도 pending→judged 이동됨"""
        _fill_pending(store, channel_id, n=5)
        # judge가 items를 반환하여 _handle_multi_judge 경로 진입
        observer = FakeObserver(judge_result=JudgeResult(
            items=[
                JudgeItem(ts="1001.000", importance=8, reaction_type="intervene",
                          reaction_target="1001.000",
                          reaction_content="개입"),
            ],
        ))
        # add_reaction에서 예외 발생 → _handle_multi_judge 내부 예외
        mock_plugin_sdk["slack"].add_reaction.side_effect = RuntimeError("Event loop is closed")
        mock_plugin_sdk["slack"].send_message.side_effect = RuntimeError("Event loop is closed")
        history = InterventionHistory(base_dir=store.base_dir)

        # 예외가 전파되더라도 finally에서 move_snapshot_to_judged가 실행되어야 함
        try:
            await run_channel_pipeline(
                store=store,
                observer=observer,
                channel_id=channel_id,
                cooldown=history,
                threshold_a=1,
                intervention_threshold=0.0,
            )
        except RuntimeError:
            pass

        # 핵심: 예외 발생에도 불구하고 pending이 judged로 이동되어야 함
        assert len(store.load_pending(channel_id)) == 0
        assert len(store.load_judged(channel_id)) == 5


# ── Bug B: _validate_linked_messages에서 thread_buffers ts 인식 ──

class TestBugB_ValidateLinkedWithThreadBuffers:
    """Bug B: thread_buffers 메시지에 대한 링크가 환각으로 오판되지 않아야 함"""

    def test_link_to_thread_buffer_message_preserved(self):
        """thread_buffers에 존재하는 ts에 대한 링크는 유지"""
        result = JudgeResult(items=[
            JudgeItem(ts="2.2", linked_message_ts="9001.000", link_reason="스레드 답변 참조"),
        ])
        judged = []
        pending = [{"ts": "2.2", "user": "U2", "text": "reply"}]
        thread_buffers = {
            "root_ts": [{"ts": "9001.000", "user": "U99", "text": "스레드 내용"}],
        }
        _validate_linked_messages(result, judged, pending, thread_buffers)
        assert result.items[0].linked_message_ts == "9001.000"
        assert result.items[0].link_reason == "스레드 답변 참조"

    def test_link_to_nonexistent_ts_still_removed(self):
        """thread_buffers에도 없는 ts 링크는 여전히 제거"""
        result = JudgeResult(items=[
            JudgeItem(ts="2.2", linked_message_ts="999.999", link_reason="환각"),
        ])
        judged = [{"ts": "1.1", "user": "U1", "text": "hello"}]
        pending = [{"ts": "2.2", "user": "U2", "text": "reply"}]
        thread_buffers = {
            "root_ts": [{"ts": "9001.000", "user": "U99", "text": "스레드 내용"}],
        }
        _validate_linked_messages(result, judged, pending, thread_buffers)
        assert result.items[0].linked_message_ts is None

    def test_no_thread_buffers_backward_compatible(self):
        """thread_buffers=None일 때도 기존처럼 동작"""
        result = JudgeResult(items=[
            JudgeItem(ts="2.2", linked_message_ts="1.1", link_reason="답변"),
        ])
        judged = [{"ts": "1.1", "user": "U1", "text": "hello"}]
        pending = [{"ts": "2.2", "user": "U2", "text": "reply"}]
        _validate_linked_messages(result, judged, pending, thread_buffers=None)
        assert result.items[0].linked_message_ts == "1.1"


# ── Bug C: _execute_intervene에서 엉뚱한 메시지 폴백 방지 ──

class TestBugC_InterveneFallbackPrevention:
    """Bug C: target_ts를 pending에서 못 찾으면 thread_buffers/judged 검색, 실패 시 스킵"""

    @pytest.mark.asyncio
    async def test_intervene_skipped_when_target_not_found(self, store, channel_id, mock_plugin_sdk):
        """target_ts가 어디에도 없으면 intervention 자체를 스킵"""
        _fill_pending(store, channel_id)
        # target이 pending에 없는 ts
        observer = FakeObserver(judge_result=JudgeResult(
            items=[
                JudgeItem(ts="1001.000", importance=9, reaction_type="intervene",
                          reaction_target="NONEXISTENT.000",
                          reaction_content="엉뚱한 메시지 타겟"),
            ],
        ))
        history = InterventionHistory(base_dir=store.base_dir)
        mock_llm = AsyncMock(return_value="응답")

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            intervention_threshold=0.0,
            llm_call=mock_llm,
        )

        # target을 찾지 못했으므로 soulstream 호출과 메시지 발송이 없어야 함
        mock_plugin_sdk["soulstream"].run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_intervene_finds_target_in_thread_buffers(self, store, channel_id, mock_plugin_sdk):
        """target_ts가 thread_buffers에 있으면 해당 메시지로 응답 생성"""
        _fill_pending(store, channel_id)
        store.append_thread_message(channel_id, "root_ts", {
            "ts": "THREAD.001", "user": "U99", "text": "스레드 메시지입니다",
        })
        observer = FakeObserver(judge_result=JudgeResult(
            items=[
                JudgeItem(ts="1001.000", importance=8, reaction_type="intervene",
                          reaction_target="THREAD.001",
                          reaction_content="스레드 대화에 개입"),
            ],
        ))
        history = InterventionHistory(base_dir=store.base_dir)
        mock_llm = AsyncMock(return_value="스레드 응답")

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            intervention_threshold=0.0,
            llm_call=mock_llm,
        )

        # thread_buffers에서 찾았으므로 soulstream이 호출되어야 함
        mock_plugin_sdk["soulstream"].run.assert_awaited_once()
        mock_plugin_sdk["slack"].send_message.assert_called()

    @pytest.mark.asyncio
    async def test_intervene_finds_target_in_judged(self, store, channel_id, mock_plugin_sdk):
        """target_ts가 judged에 있으면 해당 메시지로 응답 생성"""
        _fill_pending(store, channel_id)
        _fill_judged(store, channel_id, n=3)
        observer = FakeObserver(judge_result=JudgeResult(
            items=[
                JudgeItem(ts="1001.000", importance=8, reaction_type="intervene",
                          reaction_target="2001.000",
                          reaction_content="judged 메시지에 개입"),
            ],
        ))
        history = InterventionHistory(base_dir=store.base_dir)
        mock_llm = AsyncMock(return_value="judged 대상 응답")

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            intervention_threshold=0.0,
            llm_call=mock_llm,
        )

        # judged에서 찾았으므로 soulstream이 호출되어야 함
        mock_plugin_sdk["soulstream"].run.assert_awaited_once()


# ── Bug D: non-pending JudgeItem 필터링 ──

class TestBugD_FilterNonPendingJudgeItems:
    """Bug D: AI가 THREAD CONVERSATIONS 메시지에 대해 생성한 JudgeItem 필터링"""

    @pytest.mark.asyncio
    async def test_non_pending_items_filtered_out(self, store, channel_id, mock_plugin_sdk):
        """pending ts에 없는 JudgeItem은 필터링되어 react/intervene 실행 안 됨"""
        _fill_pending(store, channel_id, n=3)  # ts: 1000.000 ~ 1002.000
        store.append_thread_message(channel_id, "root_ts", {
            "ts": "THREAD.999", "user": "U99", "text": "스레드 메시지",
        })
        observer = FakeObserver(judge_result=JudgeResult(
            items=[
                JudgeItem(ts="1001.000", importance=5, reaction_type="react",
                          reaction_target="1001.000", reaction_content="eyes"),
                # AI가 잘못 생성한 thread 메시지 판단
                JudgeItem(ts="THREAD.999", importance=8, reaction_type="intervene",
                          reaction_target="THREAD.999",
                          reaction_content="스레드에 개입"),
            ],
        ))
        history = InterventionHistory(base_dir=store.base_dir)
        mock_llm = AsyncMock(return_value="응답")

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            intervention_threshold=0.0,
            llm_call=mock_llm,
        )

        # pending에 있는 1001.000 react만 실행됨
        assert mock_plugin_sdk["slack"].add_reaction.call_count == 1
        call_kwargs = mock_plugin_sdk["slack"].add_reaction.call_args[1]
        assert call_kwargs["ts"] == "1001.000"
        # THREAD.999 intervene은 필터링되어 soulstream 호출 없음
        mock_plugin_sdk["soulstream"].run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_all_pending_items_preserved(self, store, channel_id, mock_plugin_sdk):
        """모든 items가 pending ts에 있으면 전부 유지"""
        _fill_pending(store, channel_id, n=5)
        observer = FakeObserver(judge_result=JudgeResult(
            items=[
                JudgeItem(ts="1001.000", importance=3, reaction_type="react",
                          reaction_target="1001.000", reaction_content="eyes"),
                JudgeItem(ts="1003.000", importance=4, reaction_type="react",
                          reaction_target="1003.000", reaction_content="fire"),
            ],
        ))
        history = InterventionHistory(base_dir=store.base_dir)

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
        )

        # 둘 다 pending에 있으므로 모두 실행
        assert mock_plugin_sdk["slack"].add_reaction.call_count == 2


# ── burst/cooldown 전환 시나리오 테스트 ─────────────────

class TestPipelineBurstCooldown:
    """burst/cooldown 모델 파이프라인 통합 테스트"""

    @pytest.mark.asyncio
    async def test_burst_consecutive_interventions_pass(self, store, channel_id, mock_plugin_sdk):
        """burst 연속 3턴 통과 확인 — 이력 없음에서 시작하면 첫 개입 통과"""
        _fill_pending(store, channel_id)

        observer = FakeObserver(judge_result=JudgeResult(
            importance=8,
            reaction_type="intervene",
            reaction_target="channel",
            reaction_content="개입 메시지",
            items=[
                JudgeItem(
                    ts="1000.000", importance=8, reaction_type="intervene",
                    reaction_target="channel", reaction_content="개입 메시지",
                ),
            ],
        ))

        history = InterventionHistory(base_dir=store.base_dir)

        async def mock_llm_call(system_prompt, user_prompt):
            return "burst 응답입니다."

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            intervention_threshold=0.18,
            llm_call=mock_llm_call,
        )

        # 이력 없음 → burst_probability = 0.9 → 통과
        channel_calls = [
            c for c in mock_plugin_sdk["slack"].send_message.call_args_list
            if c[1].get("channel") == channel_id
        ]
        assert len(channel_calls) >= 1
        assert history.recent_count(channel_id) == 1

    @pytest.mark.asyncio
    async def test_burst_ceiling_blocks(self, store, channel_id, mock_plugin_sdk):
        """burst 상한(7턴) 도달 시 차단"""
        import time as _time
        _fill_pending(store, channel_id)

        observer = FakeObserver(judge_result=JudgeResult(
            importance=10,
            reaction_type="intervene",
            reaction_target="channel",
            reaction_content="상한 도달 테스트",
            items=[
                JudgeItem(
                    ts="1000.000", importance=10, reaction_type="intervene",
                    reaction_target="channel", reaction_content="상한 도달 테스트",
                ),
            ],
        ))

        history = InterventionHistory(base_dir=store.base_dir)
        # 7개의 이력을 직접 삽입 (모두 최근 5분 이내)
        now = _time.time()
        meta = {"history": [
            {"at": now - i * 120, "type": "message"}
            for i in range(7)
        ]}
        history._write_meta(channel_id, meta)

        async def mock_llm_call(system_prompt, user_prompt):
            return "차단되어야 하는 응답"

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            intervention_threshold=0.18,
            llm_call=mock_llm_call,
        )

        # burst 상한 → probability=0.0 → 차단 (채널에 메시지 미발송)
        channel_calls = [
            c for c in mock_plugin_sdk["slack"].send_message.call_args_list
            if c[1].get("channel") == channel_id
        ]
        assert len(channel_calls) == 0

    @pytest.mark.asyncio
    async def test_cooldown_then_recovery(self, store, channel_id, mock_plugin_sdk):
        """cooldown 후 충분한 시간이 지나면 다시 개입 가능"""
        import time as _time
        _fill_pending(store, channel_id)

        observer = FakeObserver(judge_result=JudgeResult(
            importance=8,
            reaction_type="intervene",
            reaction_target="channel",
            reaction_content="회복 후 개입",
            items=[
                JudgeItem(
                    ts="1000.000", importance=8, reaction_type="intervene",
                    reaction_target="channel", reaction_content="회복 후 개입",
                ),
            ],
        ))

        history = InterventionHistory(base_dir=store.base_dir)
        # 60분 전 burst 2턴 (cooldown 상태)
        now = _time.time()
        meta = {"history": [
            {"at": now - 60 * 60, "type": "message"},
            {"at": now - 62 * 60, "type": "message"},
        ]}
        history._write_meta(channel_id, meta)

        async def mock_llm_call(system_prompt, user_prompt):
            return "회복 후 개입입니다."

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            intervention_threshold=0.18,
            llm_call=mock_llm_call,
        )

        # 60분 경과 + importance 8 → final_score = (8/10) * recovery ≈ 높음 → 통과
        channel_calls = [
            c for c in mock_plugin_sdk["slack"].send_message.call_args_list
            if c[1].get("channel") == channel_id
        ]
        assert len(channel_calls) >= 1


class TestInterveneRecentMessagesFromJudged:
    """_execute_intervene의 recent_messages가 judged에서 보충되는지 검증"""

    @pytest.mark.asyncio
    async def test_trigger_at_pending_start_uses_judged(self, store, channel_id, mock_plugin_sdk):
        """pending에 트리거 1개만 있을 때 judged에서 recent_messages 보충"""
        # judged에 5개 메시지
        _fill_judged(store, channel_id, n=5)

        # pending에 트리거 1개만
        trigger_ts = "9999.000"
        store.append_pending(channel_id, {
            "ts": trigger_ts, "user": "UTRIG", "text": "트리거 메시지",
        })

        observer = FakeObserver(judge_result=JudgeResult(
            importance=8,
            reaction_type="intervene",
            reaction_target=trigger_ts,
            reaction_content="개입 이유",
        ))
        history = InterventionHistory(base_dir=store.base_dir)
        mock_llm = AsyncMock(return_value="응답 텍스트")

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            intervention_threshold=0.0,
            llm_call=mock_llm,
        )

        # soulstream.run 호출 확인
        mock_plugin_sdk["soulstream"].run.assert_awaited_once()
        call_kwargs = mock_plugin_sdk["soulstream"].run.call_args[1]
        # prompt = 트리거 메시지 텍스트
        assert call_kwargs["prompt"] == "트리거 메시지"
        # judged 메시지의 텍스트가 context["recent_messages"]에 포함되어야 함
        context = call_kwargs["context"]
        recent_item = next(item for item in context if item["key"] == "recent_messages")
        recent_texts = [m["text"] for m in recent_item["content"]]
        # 텍스트에 접두어만 확인 (내용이 길 수 있어 startswith 사용)
        assert any(t.startswith("판단 완료 메시지 0번") for t in recent_texts)
        assert any(t.startswith("판단 완료 메시지 4번") for t in recent_texts)

    @pytest.mark.asyncio
    async def test_combined_judged_pending_context(self, store, channel_id, mock_plugin_sdk):
        """judged 5개 + pending 3개, 트리거가 pending 마지막일 때 모두 recent에 포함"""
        _fill_judged(store, channel_id, n=5)

        # pending에 3개 (트리거는 마지막)
        for i in range(3):
            store.append_pending(channel_id, {
                "ts": f"300{i}.000", "user": f"UP{i}", "text": f"pending 메시지 {i}번",
            })

        trigger_ts = "3002.000"  # pending의 마지막 메시지
        observer = FakeObserver(judge_result=JudgeResult(
            importance=8,
            reaction_type="intervene",
            reaction_target=trigger_ts,
            reaction_content="개입 이유",
        ))
        history = InterventionHistory(base_dir=store.base_dir)
        mock_llm = AsyncMock(return_value="응답 텍스트")

        await run_channel_pipeline(
            store=store,
            observer=observer,
            channel_id=channel_id,
            cooldown=history,
            threshold_a=1,
            intervention_threshold=0.0,
            llm_call=mock_llm,
        )

        mock_plugin_sdk["soulstream"].run.assert_awaited_once()
        call_kwargs = mock_plugin_sdk["soulstream"].run.call_args[1]
        # recent_messages_count=5 (기본값)이므로 트리거 직전 5개:
        # all_context = [judged_0..4, pending_0..2], 트리거=pending_2(index 7)
        # recent = all_context[2:7] = [judged_2, judged_3, judged_4, pending_0, pending_1]
        context = call_kwargs["context"]
        recent_item = next(item for item in context if item["key"] == "recent_messages")
        recent_texts = [m["text"] for m in recent_item["content"]]
        # 텍스트에 접두어만 확인 (내용이 길 수 있어 startswith 사용)
        assert any(t.startswith("판단 완료 메시지 2번") for t in recent_texts)
        assert any(t.startswith("판단 완료 메시지 4번") for t in recent_texts)
        assert "pending 메시지 0번" in recent_texts
        assert "pending 메시지 1번" in recent_texts
        # judged_0, judged_1은 윈도우 밖 → 포함되지 않음
        assert not any(t.startswith("판단 완료 메시지 0번") for t in recent_texts)
