import json

import pytest

from seosoyoung_plugins.channel_observer.mcp_http import (
    McpHttpConfig,
    call_stateful_tool,
    call_stateless_tool,
)


class FakeResponse:
    def __init__(self, payload=None, *, headers=None):
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.posts = []
        self.deletes = []

    async def post(self, path, *, json=None, headers=None):
        self.posts.append((path, json, headers or {}))
        return next(self.responses)

    async def delete(self, path, *, headers=None):
        self.deletes.append((path, headers or {}))
        return FakeResponse({})


def tool_response(value):
    return {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]
        },
    }


@pytest.mark.asyncio
async def test_stateless_tool_call_uses_atom_auth_and_parses_text_json():
    client = FakeClient([FakeResponse(tool_response([{"card_id": "c1"}]))])
    config = McpHttpConfig(
        base_url="https://atom.test",
        auth_headers={"x-api-key": "secret"},
    )

    result = await call_stateless_tool(
        config, "search_cards", {"query": "GPU", "limit": 5}, client=client
    )

    assert result == [{"card_id": "c1"}]
    path, payload, headers = client.posts[0]
    assert path == "/mcp"
    assert payload["method"] == "tools/call"
    assert payload["params"]["name"] == "search_cards"
    assert headers["x-api-key"] == "secret"


@pytest.mark.asyncio
async def test_stateful_tool_initializes_calls_and_closes_session():
    client = FakeClient(
        [
            FakeResponse(
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}},
                headers={"mcp-session-id": "mcp-123"},
            ),
            FakeResponse(None),
            FakeResponse(tool_response({"session_id": "s1", "display_name": "제목"})),
        ]
    )
    config = McpHttpConfig(
        base_url="http://soulstream.test",
        auth_headers={"Authorization": "Bearer token"},
    )

    result = await call_stateful_tool(
        config,
        "set_session_name",
        {"session_id": "s1", "name": "제목"},
        client=client,
    )

    assert result["display_name"] == "제목"
    assert [payload["method"] for _, payload, _ in client.posts] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert client.posts[1][2]["mcp-session-id"] == "mcp-123"
    assert client.posts[2][2]["mcp-session-id"] == "mcp-123"
    assert client.deletes[0][0] == "/mcp"
    assert client.deletes[0][1]["mcp-session-id"] == "mcp-123"


@pytest.mark.asyncio
async def test_stateful_tool_falls_back_to_stateless_when_header_is_absent():
    client = FakeClient(
        [
            FakeResponse(
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
            ),
            FakeResponse(tool_response({"session_id": "s1", "display_name": "제목"})),
        ]
    )
    config = McpHttpConfig(
        base_url="http://soulstream.test",
        auth_headers={"Authorization": "Bearer token"},
    )

    result = await call_stateful_tool(
        config,
        "set_session_name",
        {"session_id": "s1", "name": "제목"},
        client=client,
    )

    assert result["display_name"] == "제목"
    assert [payload["method"] for _, payload, _ in client.posts] == [
        "initialize",
        "tools/call",
    ]
    assert client.deletes == []
