"""channel_observer 테스트용 conftest.

루트 conftest의 mock_plugin_sdk autouse fixture를 override한다.
pipeline_lock 같은 순수 모듈 테스트에서도 불필요한 import 실패가
발생하지 않도록 패치를 안전하게 수행한다.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from seosoyoung.plugin_sdk.slack import SendMessageResult, ReactionResult
from seosoyoung.plugin_sdk.soulstream import RunResult, RunStatus


@pytest.fixture(autouse=True)
def mock_plugin_sdk():
    """channel_observer 전용 mock.

    intervention.slack 등이 import 불가능한 환경(pipeline_lock 단위 테스트 등)에서도
    에러 없이 통과하도록 패치 실패 시 최소 mock만 제공한다.
    """
    patches = []
    mocks = {}

    try:
        p1 = patch("seosoyoung_plugins.channel_observer.intervention.slack")
        mock_slack = p1.start()
        patches.append(p1)

        p2 = patch("seosoyoung_plugins.channel_observer.pipeline.slack", mock_slack)
        p2.start()
        patches.append(p2)

        p3 = patch("seosoyoung.plugin_sdk.soulstream")
        mock_soulstream = p3.start()
        patches.append(p3)

        p4 = patch("seosoyoung_plugins.channel_observer.pipeline.soulstream", mock_soulstream)
        p4.start()
        patches.append(p4)

        mock_slack.send_message = AsyncMock(
            return_value=SendMessageResult(ok=True, ts="1234.5678", channel="C123")
        )
        mock_slack.add_reaction = AsyncMock(return_value=ReactionResult(ok=True))
        mock_slack.remove_reaction = AsyncMock(return_value=ReactionResult(ok=True))

        mock_soulstream.run = AsyncMock(return_value=RunResult(
            ok=True, status=RunStatus.COMPLETED, output="mock response",
        ))
        mock_soulstream.get_session_id = MagicMock(return_value=None)
        mock_soulstream.compact = AsyncMock()
        mock_soulstream.update_session_id = AsyncMock()

        mocks = {"slack": mock_slack, "soulstream": mock_soulstream}
    except (AttributeError, ModuleNotFoundError):
        pass

    yield mocks

    for p in reversed(patches):
        p.stop()


@pytest.fixture(autouse=True)
def patch_host_preferred_node():
    """`get_host_preferred_node`는 host Config singleton을 import하여
    `OPERATOR_USER_ID` 등 슬랙봇 env를 검증한다. unit test 환경에는 그 env들이
    없어 KeyError가 발생, _execute_intervene을 직접 호출하는 테스트가 모두
    `intervene 응답 생성 실패: 'OPERATOR_USER_ID'`로 실패한다.

    caller_info.py docstring은 *test 환경에서 graceful None* 으로 degrade하도록
    명세하며, 본 카드의 정본 패치(seosoyoung 측 caller_info.py)에서 `except`에
    KeyError를 추가하여 동일 환경에서 자연스럽게 None으로 떨어지도록 보강했다.
    본 fixture는 그 정본 패치의 *이중 안전망* — 정본 patch가 미반영된 환경(옛
    seosoyoung backend)에서도 unit test가 정상 동작하도록 보호한다.
    """
    try:
        p = patch(
            "seosoyoung_plugins.channel_observer.pipeline.get_host_preferred_node",
            return_value=None,
        )
        p.start()
        yield
        p.stop()
    except (AttributeError, ModuleNotFoundError):
        yield
