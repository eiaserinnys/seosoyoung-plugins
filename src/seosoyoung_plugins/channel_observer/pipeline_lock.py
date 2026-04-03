"""채널별 파이프라인 실행 잠금

메시지 트리거 경로(plugin.py)와 스케줄러 경로(scheduler.py) 양쪽에서
동일한 lock을 사용하여 같은 채널의 파이프라인이 중복 실행되는 것을 방지한다.
"""

import threading

_running: dict[str, bool] = {}
_lock = threading.Lock()


def try_acquire(channel_id: str) -> bool:
    """채널의 파이프라인 lock 획득을 시도한다.

    이미 실행 중이면 False를 반환한다.
    """
    with _lock:
        if _running.get(channel_id):
            return False
        _running[channel_id] = True
        return True


def release(channel_id: str) -> None:
    """채널의 파이프라인 lock을 해제한다."""
    with _lock:
        _running.pop(channel_id, None)


def _reset_for_test() -> None:
    """테스트 전용: 모든 lock 상태를 초기화한다."""
    with _lock:
        _running.clear()
