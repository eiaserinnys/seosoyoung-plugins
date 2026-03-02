"""Pytest fixtures for trello tests."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from seosoyoung.plugin_sdk import slack as slack_module, soulstream as soulstream_module


@pytest.fixture(autouse=True)
def mock_plugin_sdk():
    """Mock plugin_sdk backends (slack and soulstream)."""
    # Create mock backends
    mock_slack_backend = MagicMock()
    mock_slack_backend.send_message = AsyncMock(return_value=MagicMock(ok=True, ts="1234.5678", channel="C123"))
    mock_slack_backend.add_reaction = AsyncMock(return_value=MagicMock(ok=True))
    mock_slack_backend.remove_reaction = AsyncMock(return_value=MagicMock(ok=True))
    mock_slack_backend.open_dm = AsyncMock(return_value="D123")

    mock_soulstream_backend = MagicMock()
    mock_soulstream_backend.run = AsyncMock(return_value=MagicMock(ok=True, session_id="session-123"))
    mock_soulstream_backend.compact = AsyncMock(return_value=MagicMock(ok=True))
    mock_soulstream_backend.get_session_id = MagicMock(return_value=None)

    # Set backends directly
    slack_module.set_backend(mock_slack_backend)
    soulstream_module.set_backend(mock_soulstream_backend)

    yield {
        "slack": mock_slack_backend,
        "soulstream": mock_soulstream_backend,
    }

    # Clean up
    slack_module.set_backend(None)
    soulstream_module.set_backend(None)
