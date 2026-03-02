"""Common fixtures for seosoyoung-plugins tests."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from seosoyoung.plugin_sdk.slack import SendMessageResult, ReactionResult


@pytest.fixture
def sample_config() -> dict:
    """Provide a sample plugin configuration for testing."""
    return {
        "enabled": True,
        "debug": False,
    }


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
         patch("seosoyoung.plugin_sdk.soulstream") as mock_soulstream:

        # slack methods are async, return dataclass objects
        mock_slack.send_message = AsyncMock(
            return_value=SendMessageResult(ok=True, ts="1234.5678", channel="C123")
        )
        mock_slack.add_reaction = AsyncMock(
            return_value=ReactionResult(ok=True)
        )

        # soulstream methods are async
        mock_soulstream.get_session_id = AsyncMock(return_value=None)
        mock_soulstream.compact = AsyncMock()
        mock_soulstream.update_session_id = AsyncMock()

        yield {
            "slack": mock_slack,
            "soulstream": mock_soulstream,
        }
