"""관찰 파이프라인 테스트 (매턴 호출 방식)"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import seosoyoung_plugins.memory.observation_pipeline as _op_mod
from seosoyoung_plugins.memory.observation_pipeline import (
    _extract_new_observations,
    observe_conversation,
)
from seosoyoung_plugins.memory.observer import ObserverResult
from seosoyoung_plugins.memory.store import MemoryRecord, MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(base_dir=tmp_path)


@pytest.fixture
def mock_observer():
    observer = AsyncMock()
    observer.observe = AsyncMock()
    return observer


@pytest.fixture
def sample_messages():
    return [
        {"role": "user", "content": "안녕하세요, 캐릭터 정보 찾아줘. 펜릭스에 대해서 알려줘."},
        {"role": "assistant", "content": "네, 찾아보겠습니다. 펜릭스는 엠버 앤 블레이드의 핵심 캐릭터입니다."},
        {"role": "assistant", "content": "펜릭스는 마법검사이며, 고대 성채를 탐험하는 여정을 떠나는 주인공입니다."},
    ]


def _make_obs_items(items_data):
    """테스트 헬퍼: 간단한 관찰 항목 리스트 생성"""
    result = []
    for i, (priority, content) in enumerate(items_data):
        result.append({
            "id": f"obs_20260210_{i:03d}",
            "priority": priority,
            "content": content,
            "session_date": "2026-02-10",
            "created_at": "2026-02-10T00:00:00+00:00",
            "source": "observer",
        })
    return result


class TestExtractNewObservations:
    def test_no_existing_returns_updated(self):
        """기존 관찰이 없으면 (첫 관찰) 전체가 새 관찰"""
        updated = _make_obs_items([("🔴", "새 관찰 1"), ("🟡", "새 관찰 2")])
        assert _extract_new_observations(None, updated) == updated
        assert _extract_new_observations([], updated) == updated

    def test_extracts_only_new_items(self):
        """기존 관찰에 없는 ID만 추출"""
        existing = _make_obs_items([("🔴", "기존 관찰")])
        updated = existing + [
            {"id": "obs_20260210_100", "priority": "🟡", "content": "새 관찰",
             "session_date": "2026-02-10", "created_at": "2026-02-10T00:00:00+00:00", "source": "observer"}
        ]
        result = _extract_new_observations(existing, updated)
        assert len(result) == 1
        assert result[0]["content"] == "새 관찰"

    def test_all_items_same_returns_empty(self):
        """모든 항목이 동일하면 빈 리스트 반환"""
        items = _make_obs_items([("🔴", "동일한 관찰")])
        result = _extract_new_observations(items, items)
        assert result == []


class TestObserveConversation:
    @pytest.mark.asyncio
    async def test_basic_observation(self, store, mock_observer, sample_messages):
        """매턴 관찰이 정상적으로 수행됨"""
        obs_items = _make_obs_items([("🔴", "캐릭터 정보 조회")])
        mock_observer.observe.return_value = ObserverResult(
            observations=obs_items,
            current_task="캐릭터 정보 조회",
        )

        result = await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=sample_messages,
            min_turn_tokens=0,
        )

        assert result is True
        record = store.get_record("ts_1234")
        assert record is not None
        assert len(record.observations) == 1
        assert record.observations[0]["content"] == "캐릭터 정보 조회"
        assert record.thread_ts == "ts_1234"
        assert record.user_id == "U12345"
        assert record.total_sessions_observed == 1
        assert record.observation_tokens > 0

    @pytest.mark.asyncio
    async def test_min_token_skip(self, store, mock_observer):
        """최소 토큰 미달 시 pending 버퍼에 누적하고 스킵"""
        short_messages = [
            {"role": "user", "content": "안녕"},
            {"role": "assistant", "content": "네"},
        ]

        result = await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=short_messages,
            min_turn_tokens=999999,
        )

        assert result is False
        mock_observer.observe.assert_not_called()
        assert store.get_record("ts_1234") is None
        # pending 버퍼에 누적되었는지 확인
        pending = store.load_pending_messages("ts_1234")
        assert len(pending) == 2

    @pytest.mark.asyncio
    async def test_pending_buffer_accumulation_triggers_observation(
        self, store, mock_observer
    ):
        """pending 버퍼 누적이 임계치를 넘으면 관찰 트리거"""
        mock_observer.observe.return_value = ObserverResult(
            observations=_make_obs_items([("🟢", "누적 관찰 완료")])
        )
        short_messages = [
            {"role": "user", "content": "hi"},
        ]

        # 1차: 짧은 대화 → pending에 누적, 스킵
        result1 = await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=short_messages,
            min_turn_tokens=999999,
        )
        assert result1 is False
        assert len(store.load_pending_messages("ts_1234")) == 1

        # 2차: 더 긴 대화 (min_turn_tokens를 낮춰서 pending + 새 메시지가 넘도록)
        longer_messages = [
            {"role": "user", "content": "이번에는 충분히 긴 메시지를 보내봅니다. " * 10},
            {"role": "assistant", "content": "네, 충분히 긴 응답입니다. " * 10},
        ]
        result2 = await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=longer_messages,
            min_turn_tokens=10,
        )
        assert result2 is True
        mock_observer.observe.assert_called_once()
        # Observer에 전달된 messages에는 pending(1건) + 새 메시지(2건) = 3건
        call_args = mock_observer.observe.call_args
        passed_messages = call_args.kwargs.get("messages", call_args[1].get("messages", []))
        assert len(passed_messages) == 3
        # 관찰 후 pending 비워짐
        assert store.load_pending_messages("ts_1234") == []

    @pytest.mark.asyncio
    async def test_pending_buffer_cleared_after_observation(
        self, store, mock_observer, sample_messages
    ):
        """관찰 성공 후 pending 버퍼가 비워지는지 확인"""
        mock_observer.observe.return_value = ObserverResult(
            observations=_make_obs_items([("🟢", "관찰 완료")])
        )
        # 먼저 pending에 무언가를 넣어둠
        store.append_pending_messages("ts_1234", [{"role": "user", "content": "이전 데이터"}])

        result = await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=sample_messages,
            min_turn_tokens=0,
        )

        assert result is True
        assert store.load_pending_messages("ts_1234") == []

    @pytest.mark.asyncio
    async def test_min_token_zero_always_observes(
        self, store, mock_observer, sample_messages
    ):
        """min_turn_tokens=0이면 항상 관찰"""
        mock_observer.observe.return_value = ObserverResult(
            observations=_make_obs_items([("🟢", "관찰 내용")])
        )

        result = await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=sample_messages,
            min_turn_tokens=0,
        )

        assert result is True
        mock_observer.observe.assert_called_once()

    @pytest.mark.asyncio
    async def test_existing_observations_passed_to_observer(
        self, store, mock_observer, sample_messages
    ):
        """기존 관찰 로그가 Observer에 전달됨"""
        existing_items = _make_obs_items([("🔴", "기존 관찰 내용")])
        existing = MemoryRecord(
            thread_ts="ts_1234",
            user_id="U12345",
            observations=existing_items,
        )
        store.save_record(existing)

        mock_observer.observe.return_value = ObserverResult(
            observations=existing_items + _make_obs_items([("🟡", "갱신된 관찰")]),
        )

        await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=sample_messages,
            min_turn_tokens=0,
        )

        call_kwargs = mock_observer.observe.call_args.kwargs
        assert call_kwargs["existing_observations"] == existing_items

    @pytest.mark.asyncio
    async def test_no_existing_record_passes_none(
        self, store, mock_observer, sample_messages
    ):
        """기존 레코드 없을 때 None 전달"""
        mock_observer.observe.return_value = ObserverResult(
            observations=_make_obs_items([("🟢", "새 관찰")])
        )

        await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=sample_messages,
            min_turn_tokens=0,
        )

        call_kwargs = mock_observer.observe.call_args.kwargs
        assert call_kwargs["existing_observations"] is None

    @pytest.mark.asyncio
    async def test_subsequent_observation_increments_count(
        self, store, mock_observer, sample_messages
    ):
        """반복 관찰 시 카운터 증가"""
        existing = MemoryRecord(
            thread_ts="ts_1234",
            user_id="U12345",
            observations=_make_obs_items([("🟢", "이전 관찰")]),
            total_sessions_observed=3,
        )
        store.save_record(existing)

        mock_observer.observe.return_value = ObserverResult(
            observations=_make_obs_items([("🟢", "갱신된 관찰")])
        )

        await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=sample_messages,
            min_turn_tokens=0,
        )

        record = store.get_record("ts_1234")
        assert record.total_sessions_observed == 4

    @pytest.mark.asyncio
    async def test_observer_error_returns_false(
        self, store, mock_observer, sample_messages
    ):
        """Observer 오류 시 False 반환"""
        mock_observer.observe.side_effect = Exception("API 오류")

        result = await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=sample_messages,
            min_turn_tokens=0,
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_observer_returns_none(self, store, mock_observer, sample_messages):
        """Observer가 None 반환 시 False"""
        mock_observer.observe.return_value = None

        result = await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=sample_messages,
            min_turn_tokens=0,
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_no_inject_flag_after_observation(
        self, store, mock_observer, sample_messages
    ):
        """관찰 완료 시 inject 플래그 미설정 (PreCompact 훅에서만 설정)"""
        mock_observer.observe.return_value = ObserverResult(
            observations=_make_obs_items([("🟢", "관찰 내용")])
        )

        result = await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=sample_messages,
            min_turn_tokens=0,
        )

        assert result is True
        assert store.check_and_clear_inject_flag("ts_1234") is False

    @pytest.mark.asyncio
    async def test_no_inject_flag_on_skip(self, store, mock_observer):
        """스킵 시 inject 플래그 미설정"""
        result = await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=[{"role": "user", "content": "hi"}],
            min_turn_tokens=999999,
        )

        assert result is False
        assert store.check_and_clear_inject_flag("ts_1234") is False

    @pytest.mark.asyncio
    async def test_different_sessions_independent(
        self, store, mock_observer, sample_messages
    ):
        """다른 세션은 독립적으로 관찰"""
        items_a = _make_obs_items([("🟢", "세션 A 관찰")])
        mock_observer.observe.return_value = ObserverResult(observations=items_a)

        await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_a",
            user_id="U12345",
            messages=sample_messages,
            min_turn_tokens=0,
        )

        items_b = [{"id": "obs_20260210_010", "priority": "🟢", "content": "세션 B 관찰",
                     "session_date": "2026-02-10", "created_at": "2026-02-10T00:00:00+00:00", "source": "observer"}]
        mock_observer.observe.return_value = ObserverResult(observations=items_b)

        await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_b",
            user_id="U12345",
            messages=[{"role": "user", "content": "다른 긴 질문을 합니다. 펜릭스 캐릭터 설정에 대해 알려주세요."}],
            min_turn_tokens=0,
        )

        record_a = store.get_record("ts_a")
        record_b = store.get_record("ts_b")
        assert record_a.observations[0]["content"] == "세션 A 관찰"
        assert record_b.observations[0]["content"] == "세션 B 관찰"


class TestCandidateCollection:
    @pytest.mark.asyncio
    async def test_candidates_stored(self, store, mock_observer, sample_messages):
        """후보가 있으면 store에 적재"""
        mock_observer.observe.return_value = ObserverResult(
            observations=_make_obs_items([("🟢", "관찰 내용")]),
            candidates=[
                {"ts": "2026-02-10T00:00:00+00:00", "priority": "🔴", "content": "사용자는 한국어 커밋 메시지 선호"},
                {"ts": "2026-02-10T00:00:00+00:00", "priority": "🟡", "content": "트렐로 체크리스트 패턴"},
            ],
        )

        result = await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=sample_messages,
            min_turn_tokens=0,
        )

        assert result is True
        candidates = store.load_candidates("ts_1234")
        assert len(candidates) == 2
        assert candidates[0]["priority"] == "🔴"
        assert "한국어 커밋 메시지" in candidates[0]["content"]
        assert candidates[1]["priority"] == "🟡"

    @pytest.mark.asyncio
    async def test_no_candidates_no_store(self, store, mock_observer, sample_messages):
        """후보가 없으면 store에 적재하지 않음"""
        mock_observer.observe.return_value = ObserverResult(
            observations=_make_obs_items([("🟢", "관찰 내용")]),
            candidates=[],
        )

        await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=sample_messages,
            min_turn_tokens=0,
        )

        candidates = store.load_candidates("ts_1234")
        assert len(candidates) == 0

    @pytest.mark.asyncio
    async def test_candidates_accumulate_across_turns(
        self, store, mock_observer, sample_messages
    ):
        """여러 턴의 후보가 누적"""
        mock_observer.observe.return_value = ObserverResult(
            observations=_make_obs_items([("🟢", "관찰 1")]),
            candidates=[{"ts": "t", "priority": "🔴", "content": "첫 번째 후보"}],
        )

        await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=sample_messages,
            min_turn_tokens=0,
        )

        mock_observer.observe.return_value = ObserverResult(
            observations=_make_obs_items([("🟢", "관찰 2")]),
            candidates=[{"ts": "t", "priority": "🟡", "content": "두 번째 후보"}],
        )

        await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=sample_messages,
            min_turn_tokens=0,
        )

        candidates = store.load_candidates("ts_1234")
        assert len(candidates) == 2
        assert candidates[0]["priority"] == "🔴"
        assert candidates[1]["priority"] == "🟡"


class TestReflector:
    @pytest.mark.asyncio
    async def test_reflector_triggered(self, store, mock_observer, sample_messages):
        """관찰 토큰이 임계치를 넘으면 Reflector 호출"""
        long_observations = [
            {"id": f"obs_20260210_{i:03d}", "priority": "🟢",
             "content": f"관찰 내용 {i} " + "detail " * 50,
             "session_date": "2026-02-10", "created_at": "2026-02-10T00:00:00+00:00", "source": "observer"}
            for i in range(50)
        ]
        mock_observer.observe.return_value = ObserverResult(
            observations=long_observations,
        )

        mock_reflector = AsyncMock()
        from seosoyoung_plugins.memory.reflector import ReflectorResult
        compressed_items = _make_obs_items([("🔴", "압축된 관찰")])
        mock_reflector.reflect.return_value = ReflectorResult(
            observations=compressed_items,
            token_count=100,
        )

        await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=sample_messages,
            min_turn_tokens=0,
            reflector=mock_reflector,
            reflection_threshold=10,
        )

        mock_reflector.reflect.assert_called_once()
        record = store.get_record("ts_1234")
        assert record.observations == compressed_items
        assert record.reflection_count == 1


class TestSendDebugLogThreadTs:
    """_send_debug_log에 thread_ts가 올바르게 전달되는지 테스트"""

    def test_send_debug_log_without_thread_ts(self):
        """thread_ts 없이 호출하면 thread_ts가 kwargs에 포함되지 않음"""
        import sys
        from seosoyoung_plugins.memory.observation_pipeline import _send_debug_log

        mock_instance = MagicMock()
        mock_instance.chat_postMessage.return_value = {"ts": "1234.5678"}
        mock_sdk = MagicMock()
        mock_sdk.WebClient.return_value = mock_instance

        with patch.dict(sys.modules, {"slack_sdk": mock_sdk}):
            result = _send_debug_log("C_DEBUG", "테스트 메시지", bot_token="xoxb-test")

        assert result == "1234.5678"
        call_kwargs = mock_instance.chat_postMessage.call_args
        assert "thread_ts" not in call_kwargs.kwargs

    def test_send_debug_log_with_thread_ts(self):
        """thread_ts가 있으면 kwargs에 포함됨"""
        import sys
        from seosoyoung_plugins.memory.observation_pipeline import _send_debug_log

        mock_instance = MagicMock()
        mock_instance.chat_postMessage.return_value = {"ts": "9999.0001"}
        mock_sdk = MagicMock()
        mock_sdk.WebClient.return_value = mock_instance

        with patch.dict(sys.modules, {"slack_sdk": mock_sdk}):
            result = _send_debug_log("C_DEBUG", "스레드 메시지", thread_ts="1234.5678", bot_token="xoxb-test")

        assert result == "9999.0001"
        call_kwargs = mock_instance.chat_postMessage.call_args.kwargs
        assert call_kwargs["thread_ts"] == "1234.5678"

    def test_send_debug_log_empty_thread_ts_not_included(self):
        """thread_ts가 빈 문자열이면 kwargs에 포함되지 않음"""
        import sys
        from seosoyoung_plugins.memory.observation_pipeline import _send_debug_log

        mock_instance = MagicMock()
        mock_instance.chat_postMessage.return_value = {"ts": "1234.5678"}
        mock_sdk = MagicMock()
        mock_sdk.WebClient.return_value = mock_instance

        with patch.dict(sys.modules, {"slack_sdk": mock_sdk}):
            _send_debug_log("C_DEBUG", "메시지", thread_ts="", bot_token="xoxb-test")

        call_kwargs = mock_instance.chat_postMessage.call_args.kwargs
        assert "thread_ts" not in call_kwargs

    def test_send_debug_log_without_bot_token_returns_empty(self):
        """bot_token 없으면 빈 문자열 반환 (Slack API 미호출)"""
        from seosoyoung_plugins.memory.observation_pipeline import _send_debug_log

        result = _send_debug_log("C_DEBUG", "테스트 메시지")
        assert result == ""


class TestObserveConversationAnchorTs:
    """observe_conversation에 anchor_ts가 올바르게 전파되는지 테스트"""

    @pytest.mark.asyncio
    async def test_anchor_ts_passed_to_debug_log(self, store, mock_observer, sample_messages):
        """anchor_ts가 _send_debug_log에 전달되는지 확인"""
        mock_observer.observe.return_value = ObserverResult(
            observations=_make_obs_items([("🟢", "관찰 내용")]),
        )

        with patch("seosoyoung_plugins.memory.observation_pipeline._send_debug_log") as mock_send:
            mock_send.return_value = "debug_ts_123"
            with patch("seosoyoung_plugins.memory.observation_pipeline._update_debug_log"):
                await observe_conversation(
                    store=store,
                    observer=mock_observer,
                    thread_ts="ts_1234",
                    user_id="U12345",
                    messages=sample_messages,
                    min_turn_tokens=0,
                    debug_channel="C_DEBUG",
                    anchor_ts="anchor_123",
                    slack_bot_token="xoxb-test",
                )

        # _send_debug_log 호출 시 thread_ts=anchor_123이 전달되었는지 확인
        for call in mock_send.call_args_list:
            assert call.kwargs.get("thread_ts") == "anchor_123"

    @pytest.mark.asyncio
    async def test_anchor_ts_default_empty(self, store, mock_observer, sample_messages):
        """anchor_ts 미지정 시 빈 문자열이 기본값"""
        mock_observer.observe.return_value = ObserverResult(
            observations=_make_obs_items([("🟢", "관찰 내용")]),
        )

        with patch("seosoyoung_plugins.memory.observation_pipeline._send_debug_log") as mock_send:
            mock_send.return_value = "debug_ts"
            with patch("seosoyoung_plugins.memory.observation_pipeline._update_debug_log"):
                await observe_conversation(
                    store=store,
                    observer=mock_observer,
                    thread_ts="ts_1234",
                    user_id="U12345",
                    messages=sample_messages,
                    min_turn_tokens=0,
                    debug_channel="C_DEBUG",
                    slack_bot_token="xoxb-test",
                )

        for call in mock_send.call_args_list:
            assert call.kwargs.get("thread_ts") == ""

    @pytest.mark.asyncio
    async def test_anchor_ts_passed_on_skip(self, store, mock_observer):
        """스킵 시에도 anchor_ts가 _send_debug_log에 전달됨"""
        with patch("seosoyoung_plugins.memory.observation_pipeline._send_debug_log") as mock_send:
            mock_send.return_value = ""
            await observe_conversation(
                store=store,
                observer=mock_observer,
                thread_ts="ts_1234",
                user_id="U12345",
                messages=[{"role": "user", "content": "안녕"}],
                min_turn_tokens=999999,
                debug_channel="C_DEBUG",
                anchor_ts="anchor_skip",
                slack_bot_token="xoxb-test",
            )

        mock_send.assert_called_once()
        assert mock_send.call_args.kwargs.get("thread_ts") == "anchor_skip"


class TestObserveConversationSkipsDebugWithoutAnchor:
    """anchor_ts가 빈 문자열일 때 observe_conversation이 디버그 로그를 스킵하는지 테스트"""

    @pytest.mark.asyncio
    async def test_skips_debug_on_observation_when_anchor_ts_empty(self, store, mock_observer, sample_messages):
        """anchor_ts가 빈 문자열이면 observe_conversation에서 디버그 로그를 발송하지 않음"""
        mock_observer.observe.return_value = ObserverResult(
            observations=_make_obs_items([("🟢", "관찰 내용")]),
        )

        with patch("seosoyoung_plugins.memory.observation_pipeline._send_debug_log") as mock_send:
            mock_send.return_value = ""
            with patch("seosoyoung_plugins.memory.observation_pipeline._update_debug_log"):
                await observe_conversation(
                    store=store,
                    observer=mock_observer,
                    thread_ts="ts_1234",
                    user_id="U12345",
                    messages=sample_messages,
                    min_turn_tokens=0,
                    debug_channel="C_DEBUG",
                    anchor_ts="",  # 빈 문자열 — 앵커 생성 실패
                )

        # anchor_ts가 비었으므로 _send_debug_log가 호출되지 않아야 함
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_debug_on_skip_when_anchor_ts_empty(self, store, mock_observer):
        """스킵 시에도 anchor_ts가 빈 문자열이면 디버그 로그를 발송하지 않음"""
        with patch("seosoyoung_plugins.memory.observation_pipeline._send_debug_log") as mock_send:
            mock_send.return_value = ""
            await observe_conversation(
                store=store,
                observer=mock_observer,
                thread_ts="ts_1234",
                user_id="U12345",
                messages=[{"role": "user", "content": "안녕"}],
                min_turn_tokens=999999,
                debug_channel="C_DEBUG",
                anchor_ts="",  # 빈 문자열
            )

        # anchor_ts가 비었으므로 스킵 로그도 발송하지 않아야 함
        mock_send.assert_not_called()
