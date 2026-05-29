"""pipeline judge 호출 가드 테스트

Bug 1: judge_result is None일 때 move_snapshot_to_judged가 호출되지 않아
       스레드 버퍼가 영원히 pending에 남는 무한 루프
Bug 2: 멘션 필터링 후 judge_pending이 0건인데도 judge() 호출 (빈 LLM 호출 낭비)
Bug 3: httpx.AsyncClient가 이벤트 루프 간 공유되어 Event loop is closed 에러
"""

import asyncio

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

    def test_channel_observer_judge_reuses_soulstream_client_across_event_loops(self):
        """같은 SoulstreamClient로 두 event loop에서 judge를 연속 실행해도 실패하지 않는다."""
        from seosoyoung_plugins.soulstream_client import SoulstreamClient

        created_clients = []

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "content": (
                        "<importance>1</importance>\n"
                        '<reaction type="none" />\n'
                        "<reasoning>ok</reasoning>"
                    ),
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "session_id": "test-session",
                }

        class LoopBoundAsyncClient:
            """httpx.AsyncClient의 loop-bound transport 성질을 테스트에서 드러낸다."""

            def __init__(self, *args, **kwargs):
                self.is_closed = False
                self._bound_loop = None
                created_clients.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                await self.aclose()

            async def aclose(self):
                self.is_closed = True

            async def post(self, path, json):
                current_loop = asyncio.get_running_loop()
                if self._bound_loop is None:
                    self._bound_loop = current_loop
                elif self._bound_loop is not current_loop:
                    raise RuntimeError("Event loop is closed")
                assert path == "/llm/completions"
                return FakeResponse()

        async def judge_once(observer):
            return await observer.judge(
                channel_id="C_TEST",
                digest=None,
                judged_messages=[],
                pending_messages=[{"ts": "1001.0", "text": "테스트 메시지"}],
                thread_buffers={},
            )

        with patch(
            "seosoyoung_plugins.soulstream_client.httpx.AsyncClient",
            LoopBoundAsyncClient,
        ):
            client = SoulstreamClient(
                base_url="http://localhost:4105",
                bearer_token="test-token",
            )
            observer = ChannelObserver(client)

            first = asyncio.run(judge_once(observer))
            second = asyncio.run(judge_once(observer))

        assert first is not None
        assert second is not None
        assert first.reaction_type == "none"
        assert second.reaction_type == "none"

        # 요청 단위 AsyncClient면 호출마다 새 client가 만들어진다.
        assert len(created_clients) == 2
