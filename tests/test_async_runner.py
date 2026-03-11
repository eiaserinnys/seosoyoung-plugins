"""AsyncRunner 단위 테스트.

스레드 안전성, 예외 전파, 라이프사이클을 검증한다.
"""

import asyncio
import concurrent.futures
import pytest

from seosoyoung_plugins.utils.async_runner import AsyncRunner


class TestAsyncRunnerLifecycle:
    """AsyncRunner 시작/중지 라이프사이클 테스트."""

    def test_start_and_stop(self):
        """start() 후 loop가 실행 중이고, stop() 후 정리된다."""
        runner = AsyncRunner()
        runner.start()
        assert runner.loop is not None
        assert runner.loop.is_running()
        runner.stop()
        assert runner.loop is None

    def test_run_before_start_raises(self):
        """start() 없이 run() 호출 시 RuntimeError."""
        runner = AsyncRunner()

        async def noop():
            return 42

        coro = noop()
        with pytest.raises(RuntimeError, match="not started"):
            runner.run(coro)
        coro.close()

    def test_run_after_stop_raises(self):
        """stop() 후 run() 호출 시 RuntimeError."""
        runner = AsyncRunner()
        runner.start()
        runner.stop()

        async def noop():
            return 42

        coro = noop()
        with pytest.raises(RuntimeError, match="not started"):
            runner.run(coro)
        coro.close()

    def test_double_start_raises(self):
        """이미 시작된 상태에서 start() 재호출 시 RuntimeError."""
        runner = AsyncRunner()
        runner.start()
        try:
            with pytest.raises(RuntimeError, match="already started"):
                runner.start()
        finally:
            runner.stop()


class TestAsyncRunnerExecution:
    """AsyncRunner 코루틴 실행 테스트."""

    def test_run_returns_value(self):
        """코루틴의 반환값을 정상적으로 받는다."""
        runner = AsyncRunner()
        runner.start()
        try:
            async def add(a, b):
                return a + b

            assert runner.run(add(3, 7)) == 10
        finally:
            runner.stop()

    def test_run_propagates_exception(self):
        """코루틴 내부 예외가 호출 스레드에서 re-raise된다."""
        runner = AsyncRunner()
        runner.start()
        try:
            async def fail():
                raise ValueError("test error")

            with pytest.raises(ValueError, match="test error"):
                runner.run(fail())
        finally:
            runner.stop()

    def test_run_propagates_custom_exception(self):
        """사용자 정의 예외도 정확히 전파된다."""
        runner = AsyncRunner()
        runner.start()
        try:
            class CustomError(Exception):
                pass

            async def fail_custom():
                raise CustomError("custom")

            with pytest.raises(CustomError, match="custom"):
                runner.run(fail_custom())
        finally:
            runner.stop()

    def test_run_after_exception_still_works(self):
        """예외 발생 후에도 다음 run()이 정상 동작한다."""
        runner = AsyncRunner()
        runner.start()
        try:
            async def fail():
                raise RuntimeError("boom")

            async def ok():
                return "recovered"

            with pytest.raises(RuntimeError):
                runner.run(fail())

            assert runner.run(ok()) == "recovered"
        finally:
            runner.stop()


class TestAsyncRunnerThreadSafety:
    """여러 스레드에서 동시 run() 호출 시 안전성 테스트."""

    def test_concurrent_runs(self):
        """여러 스레드에서 동시에 run()을 호출해도 각자 정확한 결과를 받는다."""
        runner = AsyncRunner()
        runner.start()
        try:
            async def compute(n):
                await asyncio.sleep(0.01)
                return n * n

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                futures = [
                    pool.submit(runner.run, compute(i))
                    for i in range(20)
                ]
                results = [f.result(timeout=5) for f in futures]

            assert results == [i * i for i in range(20)]
        finally:
            runner.stop()

    def test_concurrent_runs_with_exceptions(self):
        """여러 스레드에서 동시 호출 시, 일부 예외가 다른 호출에 영향을 주지 않는다."""
        runner = AsyncRunner()
        runner.start()
        try:
            async def maybe_fail(n):
                await asyncio.sleep(0.01)
                if n % 3 == 0:
                    raise ValueError(f"fail-{n}")
                return n

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                futures = {
                    i: pool.submit(runner.run, maybe_fail(i))
                    for i in range(1, 13)
                }

                for i, fut in futures.items():
                    if i % 3 == 0:
                        with pytest.raises(ValueError, match=f"fail-{i}"):
                            fut.result(timeout=5)
                    else:
                        assert fut.result(timeout=5) == i
        finally:
            runner.stop()
