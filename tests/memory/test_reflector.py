"""Reflector 단위 테스트"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from seosoyoung_plugins.soulstream_client import SoulstreamClient, SoulstreamResult
from seosoyoung_plugins.memory.reflector import Reflector, ReflectorResult, _parse_reflector_output


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


class TestParseReflectorOutput:
    def test_extracts_json_array(self):
        items = [
            {"priority": "🔴", "content": "핵심 관찰", "session_date": "2026-02-10"},
            {"priority": "🟡", "content": "보조 관찰", "session_date": "2026-02-09"},
        ]
        text = json.dumps(items)
        result = _parse_reflector_output(text)
        assert len(result) == 2
        assert result[0]["content"] == "핵심 관찰"

    def test_extracts_from_code_block(self):
        items = [{"priority": "🔴", "content": "관찰", "session_date": "2026-02-10"}]
        text = f"```json\n{json.dumps(items)}\n```"
        result = _parse_reflector_output(text)
        assert len(result) == 1

    def test_extracts_from_wrapper_object(self):
        data = {"observations": [
            {"priority": "🔴", "content": "관찰", "session_date": "2026-02-10"}
        ]}
        text = json.dumps(data)
        result = _parse_reflector_output(text)
        assert len(result) == 1

    def test_empty_returns_empty_list(self):
        result = _parse_reflector_output("")
        assert result == []

    def test_fallback_no_json(self):
        result = _parse_reflector_output("관찰 로그 압축 결과입니다.")
        assert result == []


class TestReflector:
    @pytest.fixture
    def mock_soulstream(self):
        return AsyncMock(spec=SoulstreamClient)

    @pytest.mark.asyncio
    async def test_reflect_success_under_target(self, mock_soulstream):
        """1차 시도에서 목표 이하면 바로 반환"""
        reflector = Reflector(soulstream_client=mock_soulstream)

        compressed = json.dumps([
            {"priority": "🔴", "content": "압축된 관찰", "session_date": "2026-02-10"}
        ])
        mock_soulstream.complete = AsyncMock(return_value=SoulstreamResult(
            content=compressed, input_tokens=100, output_tokens=50, session_id="test",
        ))

        result = await reflector.reflect(
            observations=_make_obs_items([("🟢", "관찰")] * 10),
            target_tokens=50000,  # 매우 높은 목표
        )

        assert result is not None
        assert len(result.observations) == 1
        assert result.observations[0]["content"] == "압축된 관찰"
        assert result.token_count > 0

    @pytest.mark.asyncio
    async def test_reflect_retry_when_over_target(self, mock_soulstream):
        """1차 시도에서 목표 초과 시 재시도"""
        reflector = Reflector(soulstream_client=mock_soulstream)

        # 1차: 긴 결과
        first_items = [
            {"priority": "🔴", "content": f"관찰 {i} " + "상세 " * 50, "session_date": "2026-02-10"}
            for i in range(50)
        ]
        first_text = json.dumps(first_items)

        # 2차: 짧은 결과
        second_items = [{"priority": "🔴", "content": "압축된 관찰", "session_date": "2026-02-10"}]
        second_text = json.dumps(second_items)

        call_count = [0]
        async def mock_complete(**kwargs):
            call_count[0] += 1
            content = first_text if call_count[0] == 1 else second_text
            return SoulstreamResult(
                content=content, input_tokens=100, output_tokens=50, session_id="test",
            )

        mock_soulstream.complete = AsyncMock(side_effect=mock_complete)

        result = await reflector.reflect(
            observations=_make_obs_items([("🟢", "관찰")] * 10),
            target_tokens=10,  # 매우 낮은 목표 → 재시도 유발
        )

        assert result is not None
        assert call_count[0] == 2  # 2번 호출

    @pytest.mark.asyncio
    async def test_reflect_api_error_returns_none(self, mock_soulstream):
        """API 오류 시 None 반환"""
        reflector = Reflector(soulstream_client=mock_soulstream)
        mock_soulstream.complete = AsyncMock(side_effect=Exception("API 오류"))

        result = await reflector.reflect(
            observations=_make_obs_items([("🟢", "관찰 로그")])
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_reflect_empty_response(self, mock_soulstream):
        """빈 응답 처리"""
        reflector = Reflector(soulstream_client=mock_soulstream)
        mock_soulstream.complete = AsyncMock(return_value=SoulstreamResult(
            content="", input_tokens=100, output_tokens=0, session_id="test",
        ))

        result = await reflector.reflect(
            observations=_make_obs_items([("🟢", "관찰 로그")]),
            target_tokens=50000,
        )

        assert result is not None
        assert result.observations == []


class TestPipelineReflectorIntegration:
    """observation_pipeline에 Reflector가 통합되었는지 테스트"""

    @pytest.fixture
    def store(self, tmp_path):
        from seosoyoung_plugins.memory.store import MemoryStore
        return MemoryStore(base_dir=tmp_path)

    @pytest.fixture
    def mock_observer(self):
        observer = AsyncMock()
        return observer

    @pytest.fixture
    def mock_reflector(self):
        reflector = AsyncMock()
        return reflector

    @pytest.mark.asyncio
    async def test_reflector_triggered_when_over_threshold(
        self, store, mock_observer, mock_reflector
    ):
        """관찰 토큰이 임계치 초과 시 Reflector 호출"""
        from seosoyoung_plugins.memory.observation_pipeline import observe_conversation
        from seosoyoung_plugins.memory.observer import ObserverResult

        # 긴 관찰 결과
        long_observations = [
            {"id": f"obs_20260210_{i:03d}", "priority": "🟢",
             "content": f"관찰 {i} " + "상세 " * 100,
             "session_date": "2026-02-10", "created_at": "2026-02-10T00:00:00+00:00", "source": "observer"}
            for i in range(100)
        ]
        mock_observer.observe.return_value = ObserverResult(
            observations=long_observations,
        )
        compressed_items = _make_obs_items([("🔴", "압축된 관찰")])
        mock_reflector.reflect.return_value = ReflectorResult(
            observations=compressed_items,
            token_count=100,
        )

        result = await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=[{"role": "user", "content": "test"}],
            min_turn_tokens=0,
            reflector=mock_reflector,
            reflection_threshold=100,  # 낮은 임계치
        )

        assert result is True
        mock_reflector.reflect.assert_called_once()
        record = store.get_record("ts_1234")
        assert record.observations == compressed_items
        assert record.reflection_count == 1

    @pytest.mark.asyncio
    async def test_reflector_not_triggered_when_under_threshold(
        self, store, mock_observer, mock_reflector
    ):
        """관찰 토큰이 임계치 이하면 Reflector 미호출"""
        from seosoyoung_plugins.memory.observation_pipeline import observe_conversation
        from seosoyoung_plugins.memory.observer import ObserverResult

        mock_observer.observe.return_value = ObserverResult(
            observations=_make_obs_items([("🔴", "짧은 관찰")]),
        )

        await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=[{"role": "user", "content": "test"}],
            min_turn_tokens=0,
            reflector=mock_reflector,
            reflection_threshold=999999,  # 높은 임계치
        )

        mock_reflector.reflect.assert_not_called()

    @pytest.mark.asyncio
    async def test_reflector_none_means_no_compression(
        self, store, mock_observer
    ):
        """Reflector 미전달 시 압축 건너뜀"""
        from seosoyoung_plugins.memory.observation_pipeline import observe_conversation
        from seosoyoung_plugins.memory.observer import ObserverResult

        long_obs = [
            {"id": f"obs_20260210_{i:03d}", "priority": "🟢",
             "content": f"관찰 {i} " + "상세 " * 100,
             "session_date": "2026-02-10", "created_at": "2026-02-10T00:00:00+00:00", "source": "observer"}
            for i in range(100)
        ]
        mock_observer.observe.return_value = ObserverResult(observations=long_obs)

        await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=[{"role": "user", "content": "test"}],
            min_turn_tokens=0,
            reflector=None,
            reflection_threshold=100,
        )

        record = store.get_record("ts_1234")
        assert record.observations == long_obs

    @pytest.mark.asyncio
    async def test_reflector_failure_keeps_original(
        self, store, mock_observer, mock_reflector
    ):
        """Reflector 실패 시 원본 관찰 유지"""
        from seosoyoung_plugins.memory.observation_pipeline import observe_conversation
        from seosoyoung_plugins.memory.observer import ObserverResult

        long_obs = [
            {"id": f"obs_20260210_{i:03d}", "priority": "🟢",
             "content": f"관찰 {i} " + "상세 " * 100,
             "session_date": "2026-02-10", "created_at": "2026-02-10T00:00:00+00:00", "source": "observer"}
            for i in range(100)
        ]
        mock_observer.observe.return_value = ObserverResult(observations=long_obs)
        mock_reflector.reflect.return_value = None  # Reflector 실패

        await observe_conversation(
            store=store,
            observer=mock_observer,
            thread_ts="ts_1234",
            user_id="U12345",
            messages=[{"role": "user", "content": "test"}],
            min_turn_tokens=0,
            reflector=mock_reflector,
            reflection_threshold=100,
        )

        record = store.get_record("ts_1234")
        assert record.observations == long_obs
        assert record.reflection_count == 0
