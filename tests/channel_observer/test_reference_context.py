"""Tests for trigger-sentence reference lookup in channel interventions."""

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from seosoyoung_plugins.channel_observer import reference_context
from seosoyoung_plugins.channel_observer.intervention import InterventionAction
from seosoyoung_plugins.channel_observer.reference_context import (
    ReferenceConfig,
    ReferenceContextServices,
    build_reference_context_item,
    make_reference_search,
    normalize_query,
)


def make_reference(**overrides):
    values = {
        "atom_base_url": "https://atom.test",
        "atom_api_key": "atom-secret",
        "orch_base_url": "https://orch.test",
        "orch_token": "orch-secret",
        "session_nodes": ("eias-linegames", "eias-linegames-wsl"),
        "session_min_relevance": 0.3,
        "folder_id": "C_INTERVENE_FOLDER",
    }
    values.update(overrides)
    return ReferenceConfig(**values)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.headers = {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakePostClient:
    def __init__(self, payload):
        self.payload = payload
        self.posts = []

    async def post(self, path, *, json=None, headers=None):
        self.posts.append((path, json, headers or {}))
        return FakeResponse(self.payload)


class FakeGetClient:
    def __init__(self, payload):
        self.payload = payload
        self.gets = []

    async def get(self, path, *, params=None, headers=None):
        self.gets.append((path, params or {}, headers or {}))
        return FakeResponse(self.payload)


def tool_response(value):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {"type": "text", "text": json.dumps(value, ensure_ascii=False)}
            ]
        },
    }


def test_normalize_query_removes_slack_tokens_emoji_and_whitespace():
    query = normalize_query(
        "<@U123> <#C1|잡담> :wave: <https://example.test/a?b=1|링크>  GPU   추론"
    )
    assert query == "GPU 추론"


def test_normalize_query_truncates_to_200_characters():
    query = normalize_query("가" * 300)
    assert len(query) == 200


@pytest.mark.asyncio
async def test_short_query_skips_both_searches(monkeypatch):
    atom = AsyncMock(return_value=[])
    session = AsyncMock(return_value=[])
    monkeypatch.setattr(reference_context, "_search_atom_cards", atom)
    monkeypatch.setattr(reference_context, "_search_session_results", session)

    item = await build_reference_context_item(
        "<@U1> :wave:", make_reference(), channel_id="C_TEST",
    )

    assert item is None
    atom.assert_not_awaited()
    session.assert_not_awaited()


def test_atom_filter_excludes_history_paths_dedupes_and_limits():
    cards = [
        {
            "card_id": "a1",
            "title": "패치 노트",
            "snippet": "내용",
            "node_path": ["엠버 앤 블레이드", "패치 노트"],
        },
        {
            "card_id": "a2",
            "title": "채널 개입 이력",
            "snippet": "제외",
            "node_path": ["서소영", "채널 개입 이력"],
        },
        {
            "card_id": "a3",
            "title": "슬랙 대화",
            "snippet": "제외",
            "node_path": ["슬랙 대화", "C1"],
        },
        {
            "card_id": "a1",
            "title": "중복",
            "snippet": "중복",
            "node_path": ["엠버 앤 블레이드"],
        },
        {
            "card_id": "a4",
            "title": "팀 관심사",
            "snippet": "내용",
            "node_path": ["팀"],
        },
        {
            "card_id": "a5",
            "title": "네 번째",
            "snippet": "제외",
            "node_path": ["팀"],
        },
    ]

    filtered = reference_context._filter_atom_cards(cards)

    assert [card["card_id"] for card in filtered] == ["a1", "a4", "a5"]


def test_session_filter_checks_node_folder_relevance_and_limits():
    reference = make_reference()
    results = [
        {
            "session_id": "s1",
            "title": "관련 세션",
            "node_id": "eias-linegames",
            "folder_id": "C_OTHER",
            "relevance": 0.66,
        },
        {
            "session_id": "s2",
            "title": "개인 노드",
            "node_id": "eiaserinnys",
            "folder_id": "C_OTHER",
            "relevance": 0.9,
        },
        {
            "session_id": "s3",
            "title": "개입 폴더",
            "node_id": "eias-linegames-wsl",
            "folder_id": "C_INTERVENE_FOLDER",
            "relevance": 0.9,
        },
        {
            "session_id": "s4",
            "title": "낮은 관련도",
            "node_id": "eias-linegames-wsl",
            "folder_id": "C_OTHER",
            "relevance": 0.29,
        },
        {
            "session_id": "s5",
            "title": "두 번째",
            "node_id": "eias-linegames-wsl",
            "folder_id": "C_OTHER",
            "relevance": 0.5,
        },
        {
            "session_id": "s6",
            "title": "세 번째",
            "node_id": "eias-linegames",
            "folder_id": "C_OTHER",
            "relevance": 0.4,
        },
        {
            "session_id": "s7",
            "title": "네 번째",
            "node_id": "eias-linegames",
            "folder_id": "C_OTHER",
            "relevance": 0.35,
        },
    ]

    filtered = reference_context._filter_session_results(reference, results)

    assert [result["session_id"] for result in filtered] == ["s1", "s5", "s6"]


@pytest.mark.asyncio
async def test_both_empty_returns_none(monkeypatch):
    monkeypatch.setattr(
        reference_context, "_search_atom_cards", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        reference_context, "_search_session_results", AsyncMock(return_value=[])
    )

    item = await build_reference_context_item(
        "GPU 품절", make_reference(), channel_id="C_TEST",
    )

    assert item is None


@pytest.mark.asyncio
async def test_one_side_failure_renders_the_other(monkeypatch, caplog):
    monkeypatch.setattr(
        reference_context,
        "_search_atom_cards",
        AsyncMock(side_effect=RuntimeError("atom unavailable")),
    )
    monkeypatch.setattr(
        reference_context,
        "_search_session_results",
        AsyncMock(
            return_value=[
                {
                    "session_id": "s1",
                    "title": "GPU 조사",
                    "node_id": "eias-linegames",
                    "folder_id": "C_OTHER",
                    "folder_name": "엠버 앤 블레이드",
                    "relevance": 0.66,
                    "excerpt": "GPU 공급\n일정 확인",
                    "created_at": "2026-10-20T12:00:00Z",
                }
            ]
        ),
    )

    with caplog.at_level(logging.WARNING):
        item = await build_reference_context_item(
            "GPU 품절", make_reference(), channel_id="C_TEST",
        )

    assert item is not None
    assert item["key"] == "reference_context"
    assert item["label"] == "참고 자료"
    assert "검색어: GPU 품절" in item["content"]
    assert "### atom-nl" not in item["content"]
    assert "### 지난 작업 세션" in item["content"]
    assert "- GPU 조사 — 엠버 앤 블레이드, 2026-10-20 — GPU 공급 일정 확인" in item["content"]
    assert "atom unavailable" in caplog.text


@pytest.mark.asyncio
async def test_slow_side_is_dropped_at_deadline(monkeypatch):
    async def slow_atom(_reference, _query):
        await asyncio.sleep(1)
        return [
            {
                "card_id": "late",
                "title": "늦은 카드",
                "snippet": "제외",
                "node_path": ["팀"],
            }
        ]

    monkeypatch.setattr(reference_context, "_search_atom_cards", slow_atom)
    monkeypatch.setattr(
        reference_context,
        "_search_session_results",
        AsyncMock(
            return_value=[
                {
                    "session_id": "s1",
                    "title": "빠른 세션",
                    "node_id": "eias-linegames",
                    "folder_id": "C_OTHER",
                    "folder_name": "폴더",
                    "relevance": 0.66,
                    "excerpt": "내용",
                    "created_at": "2026-10-20T12:00:00Z",
                }
            ]
        ),
    )

    item = await build_reference_context_item(
        "GPU 품절", make_reference(), channel_id="C_TEST", timeout=0.05,
    )

    assert item is not None
    assert "빠른 세션" in item["content"]
    assert "늦은 카드" not in item["content"]
    assert "### atom-nl" not in item["content"]


@pytest.mark.asyncio
async def test_atom_search_http_uses_atom_nl_key_and_limit():
    client = FakePostClient(tool_response([{"card_id": "a1"}]))

    cards = await reference_context._search_atom_cards(
        make_reference(), "GPU", client=client,
    )

    assert cards == [{"card_id": "a1"}]
    path, payload, headers = client.posts[0]
    assert path == "/mcp"
    assert payload["params"]["name"] == "search_cards"
    assert payload["params"]["arguments"] == {"query": "GPU", "limit": 8}
    assert headers["x-api-key"] == "atom-secret"


@pytest.mark.asyncio
async def test_session_search_http_uses_orch_endpoint_and_bearer():
    client = FakeGetClient({"session_results": [{"session_id": "s1"}]})

    results = await reference_context._search_session_results(
        make_reference(), "GPU 품절", client=client,
    )

    assert results == [{"session_id": "s1"}]
    path, params, headers = client.gets[0]
    assert path == "/cogito/search"
    assert params == {
        "q": "GPU 품절",
        "top_k": 10,
        "include_session_results": "true",
        "session_search_mode": "expanded",
        "search_session_id": "true",
    }
    assert headers["Authorization"] == "Bearer orch-secret"


def test_env_values_win_when_both_atom_variables_are_set(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRATCH_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("ATOM_NL_BASE_URL", "https://env-atom.test")
    monkeypatch.setenv("ATOM_NL_API_KEY", "env-key")
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "atom-nl": {
                        "url": "https://file-atom.test/mcp",
                        "headers": {"x-api-key": "file-key"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    reference = make_reference_search({})

    assert reference.atom_base_url == "https://env-atom.test"
    assert reference.atom_api_key == "env-key"


def test_partial_env_falls_back_to_mcp_json(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SCRATCH_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("ATOM_NL_BASE_URL", "https://env-atom.test")
    monkeypatch.delenv("ATOM_NL_API_KEY", raising=False)
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "atom-nl": {
                        "url": "https://file-atom.test/mcp",
                        "headers": {"x-api-key": "file-key"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        reference = make_reference_search({})

    assert reference.atom_base_url == "https://file-atom.test"
    assert reference.atom_api_key == "file-key"
    assert "file-key" not in caplog.text


def test_missing_atom_config_disables_atom_and_warns_without_key(
    monkeypatch, tmp_path, caplog,
):
    monkeypatch.setenv("SCRATCH_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("ATOM_NL_API_KEY", "partial-secret")
    monkeypatch.delenv("ATOM_NL_BASE_URL", raising=False)
    monkeypatch.delenv("ATOM_NL_SERVER_URL", raising=False)
    monkeypatch.delenv("SOULSTREAM_ORCH_URL", raising=False)
    monkeypatch.delenv("SOULSTREAM_ORCH_TOKEN", raising=False)

    with caplog.at_level(logging.WARNING):
        reference = make_reference_search({})

    assert reference.atom_base_url == ""
    assert reference.orch_base_url == ""
    assert "atom" in caplog.text
    assert "SOULSTREAM_ORCH" in caplog.text
    assert "partial-secret" not in caplog.text


@pytest.mark.asyncio
async def test_search_failure_logs_do_not_contain_api_keys(monkeypatch, caplog):
    monkeypatch.setattr(
        reference_context,
        "_search_atom_cards",
        AsyncMock(side_effect=RuntimeError("connection refused")),
    )
    monkeypatch.setattr(
        reference_context, "_search_session_results", AsyncMock(return_value=[])
    )

    with caplog.at_level(logging.WARNING):
        await build_reference_context_item(
            "GPU 품절", make_reference(), channel_id="C_TEST",
        )

    assert "connection refused" in caplog.text
    assert "atom-secret" not in caplog.text
    assert "orch-secret" not in caplog.text


@pytest.mark.asyncio
async def test_pipeline_injects_reference_context_without_prep(
    mock_plugin_sdk, monkeypatch,
):
    from seosoyoung_plugins.channel_observer import pipeline

    store = MagicMock()
    store.get_digest.return_value = None
    store.load_judged.return_value = []
    monkeypatch.setattr(
        pipeline,
        "_fetch_recent_context_bundle",
        AsyncMock(return_value=("[C_TEST:1] <U1>: GPU 품절", ["1"])),
    )
    monkeypatch.setattr(pipeline, "build_remiel_context_item", AsyncMock(return_value=None))
    monkeypatch.setattr(
        reference_context,
        "_search_atom_cards",
        AsyncMock(
            return_value=[
                {
                    "card_id": "a1",
                    "title": "패치 노트",
                    "snippet": "GPU 공급",
                    "node_path": ["엠버 앤 블레이드", "패치 노트"],
                }
            ]
        ),
    )
    monkeypatch.setattr(
        reference_context, "_search_session_results", AsyncMock(return_value=[])
    )
    services = ReferenceContextServices(reference=make_reference())

    await pipeline._execute_intervene(
        store=store,
        channel_id="C_TEST",
        action=InterventionAction(type="message", target="channel", content="reason"),
        pending_messages=[
            {"ts": "1", "user": "U1", "text": "GPU 품절 자료 있나요"}
        ],
        prep_services=services,
    )

    context = mock_plugin_sdk["soulstream"].run.call_args.kwargs["context"]
    assert [item["key"] for item in context] == [
        "thread_context",
        "reference_context",
    ]
    assert "패치 노트" in context[1]["content"]
