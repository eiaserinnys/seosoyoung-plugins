"""Common fixtures for seosoyoung-plugins tests."""

import os
import sys

# R-4 fix(2026-05-11, atom G-12): worktree 환경에서 plugin_sdk·soul_common path override.
# pyproject.toml `pythonpath`는 main 정합 보존(`../seosoyoung/src`, `../soulstream/packages/soul-common/src`)
# — worktree test 시 환경변수로 R-4 plugin_sdk/caller_info.py 등 신규 모듈에 접근.
# main 머지 후에는 환경변수 부재로 본 분기 skip (원본 path 사용, 정합).
for env_var, sys_path_prepend in (
    ("R4_SEOSOYOUNG_SRC", os.environ.get("R4_SEOSOYOUNG_SRC")),
    ("R4_SOUL_COMMON_SRC", os.environ.get("R4_SOUL_COMMON_SRC")),
):
    if sys_path_prepend:
        # 명시적 실패 (§4): env가 stale path를 가리키면 worktree 정리 후 silent fallback이
        # 잡히지 않도록 즉시 RuntimeError. code-reviewer 권고 (a4ae69df).
        if not os.path.isdir(sys_path_prepend):
            raise RuntimeError(
                f"{env_var}={sys_path_prepend} not a directory — stale worktree env?"
            )
        sys.path.insert(0, sys_path_prepend)

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from seosoyoung.plugin_sdk.slack import SendMessageResult, ReactionResult  # noqa: E402
from seosoyoung.plugin_sdk.soulstream import RunResult, RunStatus  # noqa: E402


@pytest.fixture
def sample_config() -> dict:
    """Provide a sample plugin configuration for testing."""
    return {
        "enabled": True,
        "debug": False,
    }


def _make_default_soulstream_run_mock(
    output: str = "mock soulstream response",
    utterances: list[str] | None = None,
):
    """soulstream.run()의 기본 mock을 생성합니다.

    채널 개입 게이트(사이클 260518.01) 통과를 위해 기본 utterances 1건을 채운다.
    호출자가 별도 utterances를 주면 그대로 사용한다.
    """
    return AsyncMock(return_value=RunResult(
        ok=True,
        status=RunStatus.COMPLETED,
        output=output,
        utterances=["mock 발화"] if utterances is None else utterances,
    ))


@pytest.fixture(autouse=True)
def mock_plugin_sdk():
    """Mock plugin_sdk for testing.

    Patches the plugin_sdk imports in the actual module locations
    where they are used (intervention.py, pipeline.py, etc).

    This is an autouse fixture so it's applied to all tests automatically,
    ensuring the mock is in place before any modules are imported.
    """
    with patch("seosoyoung_plugins.memory.intervention.slack") as mock_slack, \
         patch("seosoyoung_plugins.channel_observer.intervention.slack", mock_slack), \
         patch("seosoyoung_plugins.channel_observer.pipeline.slack", mock_slack), \
         patch("seosoyoung_plugins.trello.plugin.slack", mock_slack), \
         patch("seosoyoung_plugins.trello.watcher.slack", mock_slack), \
         patch("seosoyoung.plugin_sdk.soulstream") as mock_soulstream, \
         patch("seosoyoung_plugins.channel_observer.pipeline.soulstream", mock_soulstream):

        # slack methods are async, return dataclass objects
        mock_slack.send_message = AsyncMock(
            return_value=SendMessageResult(ok=True, ts="1234.5678", channel="C123")
        )
        mock_slack.add_reaction = AsyncMock(
            return_value=ReactionResult(ok=True)
        )
        mock_slack.remove_reaction = AsyncMock(
            return_value=ReactionResult(ok=True)
        )

        # soulstream methods are async
        mock_soulstream.run = _make_default_soulstream_run_mock()
        mock_soulstream.get_session_id = MagicMock(return_value=None)
        mock_soulstream.compact = AsyncMock()
        mock_soulstream.update_session_id = AsyncMock()

        yield {
            "slack": mock_slack,
            "soulstream": mock_soulstream,
        }
