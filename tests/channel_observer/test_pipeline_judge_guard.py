"""pipeline judge 호출 가드 테스트

Bug 1: judge_result is None일 때 move_snapshot_to_judged가 호출되지 않아
       스레드 버퍼가 영원히 pending에 남는 무한 루프
Bug 2: 멘션 필터링 후 judge_pending이 0건인데도 judge() 호출 (빈 LLM 호출 낭비)
Bug 3: httpx.AsyncClient가 이벤트 루프 간 공유되어 Event loop is closed 에러
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from seosoyoung_plugins.channel_observer.observer import (
    ChannelObserver,
    JudgeResult,
)
from seosoyoung_plugins.channel_observer.store import ChannelStore
from seosoyoung_plugins.channel_observer.intervention import InterventionHistory
from seosoyoung_plugins.channel_observer.pipeline import run_channel_pipeline


def _make_store_with_thread_buffer(
    pending_messages: list[dict] | None = None,
    judged_messages: list[dict] | None = None,
    thread_buffers: dict[str, list[dict]] | None = None,
    pending_tokens: int = 200,
) -> MagicMock:
    """pending은 비어있고 thread_buffer만 있는 store mock"""
    store = MagicMock(spec=ChannelStore)
    store.count_pending_tokens.return_value = pending_tokens
    store.count_judged_plus_pending_tokens.return_value = 100
    store.load_pending.return_value = pending_messages or []
    store.load_judged.return_value = judged_messages or [
        {"ts": "1001.0", "text": "이전 대화 1"},
        {"ts": "1002.0", "text": "이전 대화 2"},
    ]
    store.load_all_thread_buffers.return_value = thread_buffers or {
        "1000.0": [{"ts": "1000.1", "text": "스레드 메시지", "thread_ts": "1000.0"}]
    }
    store.get_digest.return_value = None
    store.move_snapshot_to_judged = MagicMock()
    return store


def _make_observer(judge_return=None) -> MagicMock:
    """observer mock"""
    observer = MagicMock(spec=ChannelObserver)
    observer.judge = AsyncMock(return_value=judge_return)
    return observer


def _make_cooldown() -> MagicMock:
    cooldown = MagicMock(spec=InterventionHistory)
    cooldown.burst_probability.return_value = 0.0
    cooldown.minutes_since_last.return_value = 999
    return cooldown


class TestBug1_JudgeNoneSnapshotCleanup:
    """Bug 1: judge가 None을 반환해도 move_snapshot_to_judged가 호출되어야 한다."""

    @pytest.mark.asyncio
    async def test_judge_none_still_moves_snapshot(self):
        """judge()가 None을 반환해도 스냅샷이 judged로 이동해야 한다.
        그렇지 않으면 스레드 버퍼가 pending에 남아 무한 루프를 유발한다."""
        store = _make_store_with_thread_buffer()
        observer = _make_observer(judge_return=None)  # judge 실패
        cooldown = _make_cooldown()

        with patch("seosoyoung_plugins.channel_observer.pipeline.mention") as mock_mention:
            mock_mention.get_backend.return_value = None

            await run_channel_pipeline(
                store=store,
                observer=observer,
                channel_id="C_TEST",
                cooldown=cooldown,
                threshold_a=1,
                threshold_b=30000,
            )

        # judge가 None이어도 스냅샷 정리가 호출되어야 함
        store.move_snapshot_to_judged.assert_called_once()


class TestBug2_EmptyPendingSkipJudge:
    """Bug 2: 필터링 후 judge_pending이 0건이면 judge()를 호출하지 않아야 한다."""

    @pytest.mark.asyncio
    async def test_empty_pending_skips_judge(self):
        """pending 메시지가 0건이면 judge() 호출을 건너뛰어 LLM 비용을 절약한다."""
        store = _make_store_with_thread_buffer(
            pending_messages=[],
            thread_buffers={},
            pending_tokens=200,  # 토큰은 임계치 이상이지만 실제 메시지는 0
        )
        observer = _make_observer()
        cooldown = _make_cooldown()

        with patch("seosoyoung_plugins.channel_observer.pipeline.mention") as mock_mention:
            mock_mention.get_backend.return_value = None

            await run_channel_pipeline(
                store=store,
                observer=observer,
                channel_id="C_TEST",
                cooldown=cooldown,
                threshold_a=1,
                threshold_b=30000,
            )

        # pending 0건이면 judge를 호출하지 않아야 함
        observer.judge.assert_not_called()

    @pytest.mark.asyncio
    async def test_mention_filtered_pending_skips_judge(self):
        """멘션 스레드 필터링 후 judge_pending이 0건이면 judge()를 건너뛴다."""
        # pending에 메시지가 있지만 모두 멘션 스레드에 속함
        pending = [
            {"ts": "2001.0", "text": "멘션 스레드 메시지", "thread_ts": "2000.0"},
        ]
        store = _make_store_with_thread_buffer(
            pending_messages=pending,
            thread_buffers={"2000.0": pending},
            pending_tokens=200,
        )
        observer = _make_observer()
        cooldown = _make_cooldown()

        with patch("seosoyoung_plugins.channel_observer.pipeline.mention") as mock_mention:
            mock_mention.get_backend.return_value = MagicMock()
            # 모든 메시지가 멘션 핸들됨
            mock_mention.is_handled.return_value = True

            await run_channel_pipeline(
                store=store,
                observer=observer,
                channel_id="C_TEST",
                cooldown=cooldown,
                threshold_a=1,
                threshold_b=30000,
            )

        # 필터링 후 0건이면 judge 호출하지 않아야 함
        observer.judge.assert_not_called()
        # 하지만 스냅샷 정리는 호출되어야 함
        store.move_snapshot_to_judged.assert_called_once()

    @pytest.mark.asyncio
    async def test_nonempty_pending_calls_judge(self):
        """pending이 있으면 정상적으로 judge()를 호출한다."""
        pending = [
            {"ts": "3001.0", "text": "일반 메시지"},
        ]
        judge_result = JudgeResult(importance=3, reaction_type="none")
        store = _make_store_with_thread_buffer(
            pending_messages=pending,
            pending_tokens=200,
        )
        observer = _make_observer(judge_return=judge_result)
        cooldown = _make_cooldown()

        with patch("seosoyoung_plugins.channel_observer.pipeline.mention") as mock_mention:
            mock_mention.get_backend.return_value = None

            await run_channel_pipeline(
                store=store,
                observer=observer,
                channel_id="C_TEST",
                cooldown=cooldown,
                threshold_a=1,
                threshold_b=30000,
            )

        # pending이 있으면 judge를 호출해야 함
        observer.judge.assert_called_once()


class TestBug3_AsyncClientEventLoop:
    """Bug 3: SoulstreamClient가 이벤트 루프 간 공유되어도 에러가 발생하지 않아야 한다."""

    def test_ensure_client_recreates_when_closed(self):
        """닫힌 클라이언트가 _ensure_client()로 재생성되는지 확인."""
        from seosoyoung_plugins.soulstream_client import SoulstreamClient

        client = SoulstreamClient(
            base_url="http://localhost:4105",
            bearer_token="test-token",
        )

        original_client = client._client
        # is_closed를 True로 패치하여 닫힌 상태 시뮬레이션
        with patch.object(type(original_client), "is_closed", new_callable=lambda: property(lambda self: True)):
            new_client = client._ensure_client()

        assert new_client is not original_client
        assert not new_client.is_closed

    def test_ensure_client_reuses_when_open(self):
        """열려있는 클라이언트는 그대로 재사용."""
        from seosoyoung_plugins.soulstream_client import SoulstreamClient

        client = SoulstreamClient(
            base_url="http://localhost:4105",
            bearer_token="test-token",
        )

        original_client = client._client
        reused = client._ensure_client()
        assert reused is original_client

    @pytest.mark.asyncio
    async def test_complete_succeeds_after_client_closed(self):
        """클라이언트가 닫힌 후에도 complete()가 새 클라이언트로 동작해야 한다."""
        from seosoyoung_plugins.soulstream_client import SoulstreamClient

        client = SoulstreamClient(
            base_url="http://localhost:4105",
            bearer_token="test-token",
        )

        # 내부 클라이언트를 닫아 "Event loop is closed" 상황 시뮬레이션
        await client._client.aclose()
        assert client._client.is_closed

        # _create_client를 mock하여 동작하는 클라이언트를 반환
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": "test response",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "session_id": "test-session",
        }
        mock_response.raise_for_status = MagicMock()

        mock_new_client = AsyncMock()
        mock_new_client.is_closed = False
        mock_new_client.post = AsyncMock(return_value=mock_response)

        with patch.object(client, "_create_client", return_value=mock_new_client):
            result = await client.complete(
                provider="openai",
                model="gpt-5-mini",
                messages=[{"role": "user", "content": "test"}],
            )

        assert result.content == "test response"
        mock_new_client.post.assert_called_once()
