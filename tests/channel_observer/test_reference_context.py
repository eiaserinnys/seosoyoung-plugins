"""Tests for trigger-sentence reference lookup in channel interventions."""

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from seosoyoung_plugins.channel_observer import reference_context
from seosoyoung_plugins.channel_observer.intervention import InterventionAction
from seosoyoung_plugins.channel_observer.reference_context import (
    DEFAULT_ATOM_ROOTS,
    ReferenceConfig,
    ReferenceContextServices,
    build_reference_context_item,
    make_reference_search,
    normalize_query,
)


def make_reference(**overrides):
    values = {
        "personal_atom_base_url": "https://personal-atom.test",
        "personal_atom_api_key": "personal-secret",
        "atom_roots": DEFAULT_ATOM_ROOTS,
        "atom_base_url": "https://atom-nl.test",
        "atom_api_key": "atom-nl-secret",
        "atom_nl_enabled": True,
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


def clear_source_env(monkeypatch):
    for name in (
        "ATOM_AGENT_KEY",
        "ATOM_NL_BASE_URL",
        "ATOM_NL_SERVER_URL",
        "ATOM_NL_API_KEY",
        "SOULSTREAM_ORCH_URL",
        "SOULSTREAM_ORCH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


def test_normalize_query_removes_slack_tokens_emoji_and_whitespace():
    query = normalize_query(
        "<@U123> <#C1|잡담> :wave: <https://example.test/a?b=1|링크>  GPU   추론"
    )
    assert query == "GPU 추론"


def test_normalize_query_truncates_to_200_characters():
    query = normalize_query("가" * 300)
    assert len(query) == 200


@pytest.mark.asyncio
async def test_short_query_skips_all_searches(monkeypatch):
    atom = AsyncMock(return_value=[])
    atom_nl = AsyncMock(return_value=[])
    session = AsyncMock(return_value=[])
    monkeypatch.setattr(reference_context, "_search_personal_atom_cards", atom)
    monkeypatch.setattr(reference_context, "_search_atom_nl_cards", atom_nl)
    monkeypatch.setattr(reference_context, "_search_session_results", session)

    item = await build_reference_context_item(
        "<@U1> :wave:", make_reference(), channel_id="C_TEST",
    )

    assert item is None
    atom.assert_not_awaited()
    atom_nl.assert_not_awaited()
    session.assert_not_awaited()


def test_personal_atom_filter_keeps_allowed_roots_only():
    cards = [
        {
            "card_id": "p1",
            "title": "GPU 지식",
            "node_path": ["지식", "GPU"],
        },
        {
            "card_id": "p2",
            "title": "사용자 프로필",
            "node_path": ["사용자", "프로필"],
        },
        {
            "card_id": "p3",
            "title": "에이전트 규칙",
            "node_path": ["에이전트", "규칙"],
        },
        {
            "card_id": "p4",
            "title": "루트 없음",
            "node_path": [],
        },
        {
            "card_id": "p1",
            "title": "중복",
            "node_path": ["지식", "중복"],
        },
        {
            "card_id": "p5",
            "title": "서재",
            "node_path": ["서소영의 서재", "메모"],
        },
        {
            "card_id": "p6",
            "title": "레퍼런스",
            "node_path": ["레퍼런스", "링크"],
        },
        {
            "card_id": "p7",
            "title": "네 번째",
            "node_path": ["지식", "초과"],
        },
    ]

    filtered = reference_context._filter_atom_cards(cards, DEFAULT_ATOM_ROOTS)

    assert [card["card_id"] for card in filtered] == ["p1", "p5", "p6"]


def test_personal_atom_filter_honors_configured_roots():
    cards = [
        {"card_id": "p1", "title": "지식", "node_path": ["지식"]},
        {"card_id": "p2", "title": "레퍼런스", "node_path": ["레퍼런스"]},
    ]

    filtered = reference_context._filter_atom_cards(cards, ("레퍼런스",))

    assert [card["card_id"] for card in filtered] == ["p2"]


def test_atom_nl_filter_excludes_history_paths_and_summary_titles():
    cards = [
        {
            "card_id": "n1",
            "title": "패치 노트",
            "node_path": ["엠버 앤 블레이드", "패치 노트"],
        },
        {
            "card_id": "n2",
            "title": "채널 개입 이력",
            "node_path": ["서소영", "채널 개입 이력"],
        },
        {
            "card_id": "n3",
            "title": "슬랙 대화",
            "node_path": ["슬랙 대화", "C1"],
        },
        {
            "card_id": "n4",
            "title": "📚 참여자 사실",
            "node_path": ["팀"],
        },
        {
            "card_id": "n5",
            "title": "🗒️ 채널 기억",
            "node_path": ["팀"],
        },
        {
            "card_id": "n1",
            "title": "중복",
            "node_path": ["팀"],
        },
        {
            "card_id": "n6",
            "title": "두 번째",
            "node_path": ["팀"],
        },
        {
            "card_id": "n7",
            "title": "세 번째",
            "node_path": ["팀"],
        },
        {
            "card_id": "n8",
            "title": "네 번째",
            "node_path": ["팀"],
        },
    ]

    filtered = reference_context._filter_atom_nl_cards(cards)

    assert [card["card_id"] for card in filtered] == ["n1", "n6", "n7"]


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
async def test_all_empty_returns_none(monkeypatch):
    for searcher in (
        "_search_personal_atom_cards",
        "_search_atom_nl_cards",
        "_search_session_results",
    ):
        monkeypatch.setattr(reference_context, searcher, AsyncMock(return_value=[]))

    item = await build_reference_context_item(
        "GPU 품절", make_reference(), channel_id="C_TEST",
    )

    assert item is None


@pytest.mark.asyncio
async def test_one_source_failure_renders_the_others(monkeypatch, caplog):
    monkeypatch.setattr(
        reference_context,
        "_search_personal_atom_cards",
        AsyncMock(side_effect=RuntimeError("personal unavailable")),
    )
    monkeypatch.setattr(
        reference_context,
        "_search_atom_nl_cards",
        AsyncMock(
            return_value=[
                {
                    "card_id": "n1",
                    "title": "팀 패치 노트",
                    "snippet": "팀 지식",
                    "node_path": ["엠버 앤 블레이드"],
                }
            ]
        ),
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
    assert "### atom\n" not in item["content"]
    assert "### atom-nl (팀)\n- [팀 패치 노트]" in item["content"]
    assert "### 지난 작업 세션" in item["content"]
    assert "- GPU 조사 — 엠버 앤 블레이드, 2026-10-20 — GPU 공급 일정 확인" in item["content"]
    assert "reference context atom 검색 실패 (C_TEST): RuntimeError" in caplog.text
    assert "personal unavailable" not in caplog.text


@pytest.mark.asyncio
async def test_render_order_and_info_log_counts(monkeypatch, caplog):
    monkeypatch.setattr(
        reference_context,
        "_search_personal_atom_cards",
        AsyncMock(
            return_value=[
                {"card_id": "p1", "title": "개인 지식", "node_path": ["지식"]},
                {"card_id": "p2", "title": "사적 카드", "node_path": ["사용자"]},
            ]
        ),
    )
    monkeypatch.setattr(
        reference_context,
        "_search_atom_nl_cards",
        AsyncMock(
            return_value=[
                {"card_id": "n1", "title": "팀 지식", "node_path": ["팀"]},
                {"card_id": "n2", "title": "📚 요약", "node_path": ["팀"]},
                {"card_id": "n3", "title": "팀 메모", "node_path": ["팀"]},
            ]
        ),
    )
    monkeypatch.setattr(
        reference_context,
        "_search_session_results",
        AsyncMock(
            return_value=[
                {
                    "session_id": "s1",
                    "title": "지난 세션",
                    "node_id": "eias-linegames",
                    "folder_id": "C_OTHER",
                    "relevance": 0.9,
                }
            ]
        ),
    )

    with caplog.at_level(logging.INFO):
        item = await build_reference_context_item(
            "GPU 품절", make_reference(), channel_id="C_TEST",
        )

    content = item["content"]
    assert content.index("### atom\n") < content.index("### atom-nl (팀)\n")
    assert content.index("### atom-nl (팀)\n") < content.index("### 지난 작업 세션\n")
    assert "개인 지식" in content
    assert "사적 카드" not in content
    assert "팀 지식" in content
    assert "📚 요약" not in content
    assert "atom 1/2, atom-nl 2/3, sessions 1/1" in caplog.text


@pytest.mark.asyncio
async def test_atom_nl_disabled_by_config_skips_the_call(monkeypatch):
    atom_nl = AsyncMock(return_value=[])
    monkeypatch.setattr(reference_context, "_search_atom_nl_cards", atom_nl)
    monkeypatch.setattr(
        reference_context, "_search_personal_atom_cards", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        reference_context, "_search_session_results", AsyncMock(return_value=[])
    )

    item = await build_reference_context_item(
        "GPU 품절",
        make_reference(atom_nl_enabled=False),
        channel_id="C_TEST",
    )

    assert item is None
    atom_nl.assert_not_awaited()


@pytest.mark.asyncio
async def test_slow_source_is_dropped_at_deadline(monkeypatch):
    async def slow_atom_nl(_reference, _query):
        await asyncio.sleep(1)
        return [
            {
                "card_id": "late",
                "title": "늦은 카드",
                "snippet": "제외",
                "node_path": ["팀"],
            }
        ]

    monkeypatch.setattr(
        reference_context, "_search_personal_atom_cards", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(reference_context, "_search_atom_nl_cards", slow_atom_nl)
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
    assert "### atom-nl (팀)" not in item["content"]


@pytest.mark.asyncio
async def test_personal_atom_http_uses_own_key_and_limit():
    client = FakePostClient(tool_response([{"card_id": "p1"}]))

    cards = await reference_context._search_personal_atom_cards(
        make_reference(), "GPU", client=client,
    )

    assert cards == [{"card_id": "p1"}]
    path, payload, headers = client.posts[0]
    assert path == "/mcp"
    assert payload["params"]["name"] == "search_cards"
    assert payload["params"]["arguments"] == {"query": "GPU", "limit": 30}
    assert headers["x-api-key"] == "personal-secret"


@pytest.mark.asyncio
async def test_atom_nl_http_uses_own_key_and_limit():
    client = FakePostClient(tool_response([{"card_id": "n1"}]))

    cards = await reference_context._search_atom_nl_cards(
        make_reference(), "GPU", client=client,
    )

    assert cards == [{"card_id": "n1"}]
    path, payload, headers = client.posts[0]
    assert path == "/mcp"
    assert payload["params"]["name"] == "search_cards"
    assert payload["params"]["arguments"] == {"query": "GPU", "limit": 8}
    assert headers["x-api-key"] == "atom-nl-secret"


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


def test_personal_atom_key_env_is_resolved_from_config(monkeypatch):
    clear_source_env(monkeypatch)
    monkeypatch.setenv("PERSONAL_ATOM_TEST_KEY", "personal-key")

    reference = make_reference_search(
        {
            "reference_atom_url": "https://atom.example.test/",
            "reference_atom_key_env": "PERSONAL_ATOM_TEST_KEY",
            "reference_atom_roots": ["지식", "레퍼런스"],
        }
    )

    assert reference.personal_atom_base_url == "https://atom.example.test"
    assert reference.personal_atom_api_key == "personal-key"
    assert reference.atom_roots == ("지식", "레퍼런스")
    assert reference.personal_atom_enabled is True


def test_personal_atom_disabled_when_key_env_is_missing(monkeypatch, caplog):
    clear_source_env(monkeypatch)

    with caplog.at_level(logging.WARNING):
        reference = make_reference_search({})

    assert reference.personal_atom_base_url == ""
    assert reference.personal_atom_api_key == ""
    assert reference.personal_atom_enabled is False
    assert "개인 atom 검색 비활성" in caplog.text


def test_atom_nl_env_values_win_when_both_variables_are_set(monkeypatch, tmp_path):
    clear_source_env(monkeypatch)
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
    assert reference.atom_nl_active is True


def test_partial_env_falls_back_to_mcp_json(monkeypatch, tmp_path, caplog):
    clear_source_env(monkeypatch)
    monkeypatch.setenv("SCRATCH_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("ATOM_NL_BASE_URL", "https://env-atom.test")
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


def test_missing_atom_configs_disable_sources_and_never_log_keys(
    monkeypatch, tmp_path, caplog,
):
    clear_source_env(monkeypatch)
    monkeypatch.setenv("SCRATCH_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("ATOM_NL_API_KEY", "partial-secret")

    with caplog.at_level(logging.WARNING):
        reference = make_reference_search({})

    assert reference.personal_atom_base_url == ""
    assert reference.atom_base_url == ""
    assert reference.orch_base_url == ""
    assert "atom-nl" in caplog.text
    assert "SOULSTREAM_ORCH" in caplog.text
    assert "partial-secret" not in caplog.text


def test_reference_atom_nl_false_disables_resolution(monkeypatch, caplog):
    clear_source_env(monkeypatch)
    monkeypatch.setenv("ATOM_NL_BASE_URL", "https://env-atom.test")
    monkeypatch.setenv("ATOM_NL_API_KEY", "env-key")

    with caplog.at_level(logging.WARNING):
        reference = make_reference_search({"reference_atom_nl": False})

    assert reference.atom_nl_enabled is False
    assert reference.atom_base_url == ""
    assert reference.atom_api_key == ""
    assert reference.atom_nl_active is False
    assert "atom-nl 검색 비활성" not in caplog.text


LEAKED_KEY = "sk-leak-9f3c"


@pytest.mark.parametrize(
    ("searcher", "source"),
    [
        ("_search_personal_atom_cards", "atom"),
        ("_search_atom_nl_cards", "atom-nl"),
        ("_search_session_results", "session"),
    ],
)
@pytest.mark.asyncio
async def test_search_failure_logs_exception_type_without_message(
    monkeypatch, caplog, searcher, source,
):
    monkeypatch.setattr(
        reference_context,
        searcher,
        AsyncMock(side_effect=RuntimeError(f"bad header {LEAKED_KEY}")),
    )

    with caplog.at_level(logging.WARNING):
        await build_reference_context_item(
            "GPU 품절", make_reference(), channel_id="C_TEST",
        )

    assert f"reference context {source} 검색 실패 (C_TEST): RuntimeError" in caplog.text
    assert LEAKED_KEY not in caplog.text
    assert "personal-secret" not in caplog.text
    assert "atom-nl-secret" not in caplog.text
    assert "orch-secret" not in caplog.text


@pytest.mark.asyncio
async def test_search_failure_logs_status_code_for_http_status_error(
    monkeypatch, caplog,
):
    request = httpx.Request("GET", "https://orch.test/cogito/search")
    error = httpx.HTTPStatusError(
        f"401 Unauthorized {LEAKED_KEY}",
        request=request,
        response=httpx.Response(401, request=request),
    )
    monkeypatch.setattr(
        reference_context, "_search_session_results", AsyncMock(side_effect=error),
    )

    with caplog.at_level(logging.WARNING):
        await build_reference_context_item(
            "GPU 품절", make_reference(), channel_id="C_TEST",
        )

    assert "reference context session 검색 실패 (C_TEST): HTTPStatusError 401" in caplog.text
    assert LEAKED_KEY not in caplog.text


@pytest.mark.asyncio
async def test_build_failure_logs_exception_type_without_message(monkeypatch, caplog):
    for searcher in (
        "_search_personal_atom_cards",
        "_search_atom_nl_cards",
        "_search_session_results",
    ):
        monkeypatch.setattr(reference_context, searcher, AsyncMock(return_value=[]))
    monkeypatch.setattr(
        reference_context,
        "_render_context_item",
        MagicMock(side_effect=ValueError(f"render failed {LEAKED_KEY}")),
    )

    with caplog.at_level(logging.WARNING):
        result = await build_reference_context_item(
            "GPU 품절", make_reference(), channel_id="C_TEST",
        )

    assert result is None
    assert "reference context 생성 실패 (C_TEST): ValueError" in caplog.text
    assert LEAKED_KEY not in caplog.text


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
        "_search_personal_atom_cards",
        AsyncMock(
            return_value=[
                {
                    "card_id": "p1",
                    "title": "패치 노트",
                    "snippet": "GPU 공급",
                    "node_path": ["지식", "패치 노트"],
                }
            ]
        ),
    )
    monkeypatch.setattr(
        reference_context, "_search_atom_nl_cards", AsyncMock(return_value=[])
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
