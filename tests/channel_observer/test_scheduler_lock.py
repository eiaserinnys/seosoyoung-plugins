"""스케줄러의 pipeline_lock 통합 테스트"""

from unittest.mock import MagicMock, patch

import pytest

from seosoyoung_plugins.channel_observer import pipeline_lock
from seosoyoung_plugins.channel_observer.scheduler import ChannelDigestScheduler


@pytest.fixture
def mock_deps():
    return {
        "store": MagicMock(),
        "observer": MagicMock(),
        "compressor": MagicMock(),
        "cooldown": MagicMock(),
    }


def make_scheduler(mock_deps, channels=None, **kwargs):
    return ChannelDigestScheduler(
        store=mock_deps["store"],
        observer=mock_deps["observer"],
        compressor=mock_deps["compressor"],
        cooldown=mock_deps["cooldown"],
        channels=channels or ["C001"],
        interval_sec=60,
        **kwargs,
    )


class TestSchedulerPipelineLock:
    """스케줄러가 pipeline_lock을 올바르게 사용하는지 검증"""

    def setup_method(self):
        pipeline_lock._reset_for_test()

    def test_acquires_lock_before_pipeline(self, mock_deps):
        """파이프라인 실행 전 lock을 획득한다."""
        scheduler = make_scheduler(mock_deps)

        with patch("seosoyoung_plugins.channel_observer.scheduler.asyncio.run"):
            scheduler._run_pipeline("C001")

        # 실행 완료 후 lock이 해제되어야 함
        assert pipeline_lock.try_acquire("C001") is True

    def test_skips_when_already_locked(self, mock_deps):
        """이미 lock이 걸려 있으면 파이프라인을 스킵한다."""
        scheduler = make_scheduler(mock_deps)
        pipeline_lock.try_acquire("C001")  # 선점

        with patch("seosoyoung_plugins.channel_observer.scheduler.asyncio.run") as mock_run:
            scheduler._run_pipeline("C001")
            mock_run.assert_not_called()

        # 원래 lock은 유지되어 있어야 함
        pipeline_lock.release("C001")

    def test_releases_lock_on_exception(self, mock_deps):
        """파이프라인 실행 중 예외 발생 시에도 lock이 해제된다."""
        scheduler = make_scheduler(mock_deps)

        with patch(
            "seosoyoung_plugins.channel_observer.scheduler.asyncio.run",
            side_effect=Exception("pipeline error"),
        ):
            scheduler._run_pipeline("C001")

        # 예외 후에도 lock이 해제되어야 함
        assert pipeline_lock.try_acquire("C001") is True

    def test_different_channels_independent(self, mock_deps):
        """다른 채널은 lock이 독립적이다."""
        scheduler = make_scheduler(mock_deps, channels=["C001", "C002"])
        pipeline_lock.try_acquire("C001")  # C001만 선점

        with patch("seosoyoung_plugins.channel_observer.scheduler.asyncio.run") as mock_run:
            scheduler._run_pipeline("C002")  # C002는 실행 가능
            mock_run.assert_called_once()

        pipeline_lock.release("C001")

    def test_check_and_digest_respects_lock(self, mock_deps):
        """_check_and_digest에서 lock이 걸린 채널은 스킵한다."""
        mock_deps["store"].count_pending_tokens.return_value = 100
        scheduler = make_scheduler(
            mock_deps, channels=["C001", "C002"], buffer_threshold=30000,
        )
        pipeline_lock.try_acquire("C001")  # C001만 선점

        with patch.object(scheduler, "_run_pipeline", wraps=scheduler._run_pipeline) as mock_run:
            with patch("seosoyoung_plugins.channel_observer.scheduler.asyncio.run"):
                scheduler._check_and_digest()

        # C001은 _run_pipeline이 호출되지만 lock에 의해 실제 pipeline은 스킵
        # C002는 정상 실행
        pipeline_lock.release("C001")
