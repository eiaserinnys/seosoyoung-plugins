"""Shared helpers for trello tests."""

from unittest.mock import MagicMock


def _make_prompt_builder_mock():
    """PromptBuilder mock — _request 메서드가 (str, list) 튜플을 반환하도록 설정."""
    mock = MagicMock()
    mock.build_to_go_request.return_value = ("test prompt", [])
    mock.build_reaction_execute_request.return_value = ("test prompt", [])
    mock.build_list_run_request.return_value = ("test prompt", [])
    return mock
