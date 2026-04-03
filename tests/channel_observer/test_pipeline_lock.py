"""pipeline_lock 모듈 단위 테스트"""

import threading

from seosoyoung_plugins.channel_observer import pipeline_lock


class TestTryAcquire:
    """try_acquire 동작 검증"""

    def setup_method(self):
        """각 테스트 전 lock 상태 초기화"""
        pipeline_lock._reset_for_test()

    def test_acquire_returns_true_on_first_call(self):
        assert pipeline_lock.try_acquire("C001") is True

    def test_acquire_returns_false_when_already_running(self):
        pipeline_lock.try_acquire("C001")
        assert pipeline_lock.try_acquire("C001") is False

    def test_different_channels_are_independent(self):
        pipeline_lock.try_acquire("C001")
        assert pipeline_lock.try_acquire("C002") is True

    def test_release_allows_reacquire(self):
        pipeline_lock.try_acquire("C001")
        pipeline_lock.release("C001")
        assert pipeline_lock.try_acquire("C001") is True

    def test_release_without_acquire_is_safe(self):
        pipeline_lock.release("C001")  # should not raise

    def test_thread_safety(self):
        """두 스레드가 동시에 같은 채널을 acquire하면 하나만 성공"""
        results = []

        def acquire():
            results.append(pipeline_lock.try_acquire("C001"))

        t1 = threading.Thread(target=acquire)
        t2 = threading.Thread(target=acquire)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert results.count(True) == 1
        assert results.count(False) == 1
