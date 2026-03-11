"""스레드 안전한 async 실행 헬퍼.

전용 데몬 스레드에서 이벤트 루프를 run_forever()로 실행하고,
모든 코루틴을 asyncio.run_coroutine_threadsafe()로 제출한다.
어느 스레드에서든 안전하게 async 호출이 가능하다.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine, Optional, TypeVar

T = TypeVar("T")


class AsyncRunner:
    """스레드 안전한 async 코루틴 실행기.

    전용 데몬 스레드에서 이벤트 루프를 실행하고, run()을 통해
    어느 스레드에서든 코루틴을 제출하고 결과를 동기적으로 대기한다.
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """내부 이벤트 루프 참조 (읽기 전용)."""
        return self._loop

    def start(self) -> None:
        """전용 데몬 스레드에서 이벤트 루프를 시작한다."""
        if self._loop and self._loop.is_running():
            raise RuntimeError("AsyncRunner already started")
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="async-runner"
        )
        self._thread.start()

    def _run_loop(self) -> None:
        """데몬 스레드에서 이벤트 루프를 실행한다."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """코루틴을 제출하고 결과를 동기적으로 대기한다.

        모든 스레드에서 호출 안전.
        예외는 호출 스레드에서 re-raise된다.

        Args:
            coro: 실행할 코루틴.

        Returns:
            코루틴의 반환값.

        Raises:
            RuntimeError: AsyncRunner가 시작되지 않았거나 이미 중지된 경우.
            Exception: 코루틴 내부에서 발생한 예외가 그대로 전파된다.
        """
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("AsyncRunner not started")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def stop(self) -> None:
        """이벤트 루프를 중지하고 스레드를 join한다."""
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=10)
        if self._loop and not self._loop.is_closed():
            self._loop.close()
        self._loop = None
        self._thread = None
