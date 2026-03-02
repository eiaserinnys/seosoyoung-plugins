"""Common fixtures for seosoyoung-plugins tests."""

import pytest


@pytest.fixture
def sample_config() -> dict:
    """Provide a sample plugin configuration for testing."""
    return {
        "enabled": True,
        "debug": False,
    }
