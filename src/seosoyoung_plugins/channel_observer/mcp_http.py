"""Small Streamable HTTP MCP clients used by channel intervention prep."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Awaitable, Callable

import httpx


MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@dataclass(frozen=True)
class McpHttpConfig:
    base_url: str
    auth_headers: dict[str, str]
    timeout: float = 2.0


def _request_headers(
    config: McpHttpConfig, session_id: str | None = None,
) -> dict[str, str]:
    headers = {**MCP_HEADERS, **config.auth_headers}
    if session_id:
        headers["mcp-session-id"] = session_id
    return headers


def _rpc_request(
    request_id: int, method: str, params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }
    if params is not None:
        payload["params"] = params
    return payload


def _response_payload(response: Any) -> dict[str, Any]:
    """Decode JSON or a one-event Streamable HTTP SSE response."""
    try:
        payload = response.json()
    except (ValueError, TypeError):
        text = str(getattr(response, "text", "") or "")
        data_lines = [
            line[5:].strip() for line in text.splitlines() if line.startswith("data:")
        ]
        if not data_lines:
            raise RuntimeError("MCP response contained no JSON payload")
        payload = json.loads(data_lines[-1])
    if not isinstance(payload, dict):
        raise RuntimeError("MCP response payload was not an object")
    return payload


def _decode_tool_result(response: Any) -> Any:
    payload = _response_payload(response)
    if payload.get("error"):
        raise RuntimeError(f"MCP JSON-RPC error: {payload['error']}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("MCP tool response had no result object")
    if result.get("isError") is True:
        raise RuntimeError("MCP tool returned isError")
    if "structuredContent" in result:
        return result["structuredContent"]
    content = result.get("content")
    if not isinstance(content, list):
        raise RuntimeError("MCP tool response had no content list")
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if not isinstance(text, str):
                continue
            try:
                return json.loads(text)
            except ValueError:
                return text
    raise RuntimeError("MCP tool response had no text content")


async def _post_tool(
    client: Any,
    config: McpHttpConfig,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    session_id: str | None = None,
    request_id: int = 1,
) -> Any:
    response = await client.post(
        "/mcp",
        json=_rpc_request(
            request_id,
            "tools/call",
            {"name": tool_name, "arguments": arguments},
        ),
        headers=_request_headers(config, session_id),
    )
    response.raise_for_status()
    return _decode_tool_result(response)


async def call_stateless_tool(
    config: McpHttpConfig,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    client: Any | None = None,
) -> Any:
    """Call a tool on a stateless Streamable HTTP MCP endpoint."""
    if client is not None:
        return await _post_tool(client, config, tool_name, arguments)
    async with httpx.AsyncClient(
        base_url=config.base_url, timeout=config.timeout,
    ) as owned_client:
        return await _post_tool(owned_client, config, tool_name, arguments)


async def _stateful_call(
    client: Any,
    config: McpHttpConfig,
    tool_name: str,
    arguments: dict[str, Any],
) -> Any:
    initialize = await client.post(
        "/mcp",
        json=_rpc_request(
            1,
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "seosoyoung-channel-observer",
                    "version": "1.0.0",
                },
            },
        ),
        headers=_request_headers(config),
    )
    initialize.raise_for_status()
    _response_payload(initialize)
    session_id = initialize.headers.get("mcp-session-id")
    if not session_id:
        # Older/stateless soul-server-ts deployments accept direct tools/call
        # and intentionally omit the transport session header.
        return await _post_tool(
            client,
            config,
            tool_name,
            arguments,
            request_id=2,
        )

    try:
        initialized = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=_request_headers(config, session_id),
        )
        initialized.raise_for_status()
        return await _post_tool(
            client,
            config,
            tool_name,
            arguments,
            session_id=session_id,
            request_id=2,
        )
    finally:
        try:
            closed = await client.delete(
                "/mcp", headers=_request_headers(config, session_id),
            )
            closed.raise_for_status()
        except Exception:
            # The requested tool result is authoritative; closing an ephemeral
            # MCP transport is best-effort and must not change it.
            pass


async def call_stateful_tool(
    config: McpHttpConfig,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    client: Any | None = None,
) -> Any:
    """Initialize a stateful MCP session, call one tool, then close it."""
    if client is not None:
        return await _stateful_call(client, config, tool_name, arguments)
    async with httpx.AsyncClient(
        base_url=config.base_url, timeout=config.timeout,
    ) as owned_client:
        return await _stateful_call(owned_client, config, tool_name, arguments)


SearchCards = Callable[[str], Awaitable[list[dict[str, Any]]]]
SetSessionName = Callable[[str, str], Awaitable[None]]


def make_atom_search_cards(plugin_config: dict[str, Any]) -> SearchCards | None:
    base_url = str(plugin_config.get("atom_base_url", "") or "").strip().rstrip("/")
    key_env = str(plugin_config.get("atom_api_key_env", "") or "").strip()
    api_key = os.environ.get(key_env, "") if key_env else ""
    if not base_url or not api_key:
        return None
    config = McpHttpConfig(base_url=base_url, auth_headers={"x-api-key": api_key})

    async def search_cards(keyword: str) -> list[dict[str, Any]]:
        result = await call_stateless_tool(
            config, "search_cards", {"query": keyword, "limit": 5},
        )
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]

    return search_cards


def make_session_name_setter(
    base_url: str, bearer_token: str,
) -> SetSessionName | None:
    normalized_url = str(base_url or "").strip().rstrip("/")
    token = str(bearer_token or "").strip()
    if not normalized_url or not token:
        return None
    config = McpHttpConfig(
        base_url=normalized_url,
        auth_headers={"Authorization": f"Bearer {token}"},
    )

    async def set_session_name(session_id: str, name: str) -> None:
        await call_stateful_tool(
            config,
            "set_session_name",
            {"session_id": session_id, "name": name},
        )

    return set_session_name
