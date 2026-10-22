"""Trigger-sentence reference lookup for channel interventions.

The sentence that triggered an intervention is used as-is to search two
sources: the team atom instance (atom-nl) and prior Soulstream sessions on the
company nodes. Both searches run concurrently inside one deadline and the
result is rendered as a single reference context item.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from html import unescape
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any

import httpx

from seosoyoung_plugins.channel_observer.intervention_prep import (
    InterventionPrepServices,
)
from seosoyoung_plugins.channel_observer.mcp_http import (
    McpHttpConfig,
    call_stateless_tool,
)


logger = logging.getLogger(__name__)

DEFAULT_SESSION_NODES = ("eias-linegames", "eias-linegames-wsl")
DEFAULT_SESSION_MIN_RELEVANCE = 0.3
REFERENCE_TIMEOUT = 4.0
ATOM_RESULT_LIMIT = 8
REFERENCE_RESULT_LIMIT = 3
QUERY_LIMIT = 200
MIN_QUERY_LENGTH = 2
EXCLUDED_ATOM_PATH_PARTS = ("채널 개입 이력", "슬랙 대화")

SLACK_TOKEN = re.compile(r"<[^>]*>")
EMOJI_TOKEN = re.compile(r":[^\s:]+:")
HTML_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class ReferenceConfig:
    """Resolved reference search configuration.

    Empty atom fields or empty orchestrator fields disable that source. The
    values are resolved once when the plugin loads; malformed or missing
    configuration is reported there instead of on every intervention.
    """

    atom_base_url: str = ""
    atom_api_key: str = ""
    orch_base_url: str = ""
    orch_token: str = ""
    session_nodes: tuple[str, ...] = DEFAULT_SESSION_NODES
    session_min_relevance: float = DEFAULT_SESSION_MIN_RELEVANCE
    folder_id: str = ""

    @property
    def atom_enabled(self) -> bool:
        return bool(self.atom_base_url and self.atom_api_key)

    @property
    def session_enabled(self) -> bool:
        return bool(self.orch_base_url and self.orch_token)


@dataclass(frozen=True)
class ReferenceContextServices(InterventionPrepServices):
    """Prep services carrying the reference lookup configuration.

    The prep services object is already threaded from the plugin to the
    pipeline. Carrying the reference config on the same object keeps the
    intervention wiring untouched until the prep path is replaced.
    """

    reference: ReferenceConfig | None = None


def normalize_query(value: Any) -> str:
    """Remove Slack tokens and emoji names, collapse whitespace, cut to 200."""

    text = SLACK_TOKEN.sub(" ", str(value or ""))
    text = EMOJI_TOKEN.sub(" ", text)
    text = " ".join(text.split())
    return text[:QUERY_LIMIT]


def _resolve_atom_config() -> tuple[str, str] | None:
    env_url = os.environ.get("ATOM_NL_BASE_URL") or os.environ.get(
        "ATOM_NL_SERVER_URL"
    )
    env_key = os.environ.get("ATOM_NL_API_KEY")
    if env_url and env_key:
        return env_url.rstrip("/"), env_key

    workspace = os.environ.get("SCRATCH_WORKSPACE_DIR", "").strip()
    if not workspace:
        return None
    try:
        with open(Path(workspace) / ".mcp.json", encoding="utf-8") as handle:
            mcp = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(mcp, dict):
        return None
    server = mcp.get("mcpServers", {}).get("atom-nl", {})
    if not isinstance(server, dict) or not server:
        return None
    raw_url = str(server.get("url") or "")
    base_url = (
        raw_url[: -len("/mcp")] if raw_url.endswith("/mcp") else raw_url.rstrip("/")
    )
    headers = server.get("headers", {})
    api_key = str(headers.get("x-api-key") or "") if isinstance(headers, dict) else ""
    if not base_url or not api_key:
        return None
    return base_url, api_key


def _session_nodes(config: dict[str, Any]) -> tuple[str, ...]:
    raw = config.get("reference_session_nodes", DEFAULT_SESSION_NODES)
    if isinstance(raw, (list, tuple)):
        nodes = tuple(str(item).strip() for item in raw if str(item).strip())
        if nodes:
            return nodes
    return DEFAULT_SESSION_NODES


def _session_min_relevance(config: dict[str, Any]) -> float:
    try:
        return float(
            config.get("reference_session_min_relevance", DEFAULT_SESSION_MIN_RELEVANCE)
        )
    except (TypeError, ValueError):
        return DEFAULT_SESSION_MIN_RELEVANCE


def make_reference_search(config: dict[str, Any]) -> ReferenceConfig:
    """Resolve atom and orchestrator settings once at plugin load."""

    atom = _resolve_atom_config()
    atom_base_url, atom_api_key = atom if atom else ("", "")
    if not atom:
        logger.warning(
            "reference context: atom 검색 비활성 — ATOM_NL_BASE_URL/"
            "ATOM_NL_API_KEY 또는 SCRATCH_WORKSPACE_DIR/.mcp.json의 atom-nl 설정 없음"
        )

    orch_base_url = os.environ.get("SOULSTREAM_ORCH_URL", "").strip().rstrip("/")
    orch_token = os.environ.get("SOULSTREAM_ORCH_TOKEN", "").strip()
    if not (orch_base_url and orch_token):
        orch_base_url, orch_token = "", ""
        logger.warning(
            "reference context: 세션 검색 비활성 — "
            "SOULSTREAM_ORCH_URL/SOULSTREAM_ORCH_TOKEN 없음"
        )

    return ReferenceConfig(
        atom_base_url=atom_base_url,
        atom_api_key=atom_api_key,
        orch_base_url=orch_base_url,
        orch_token=orch_token,
        session_nodes=_session_nodes(config),
        session_min_relevance=_session_min_relevance(config),
        folder_id=str(config.get("folder_id") or ""),
    )


def _compact(value: Any, limit: int) -> str:
    text = unescape(HTML_TAG.sub("", str(value or "")))
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return f"{text[:limit - 3]}..."


def _node_path(card: dict[str, Any]) -> list[str]:
    raw_path = card.get("node_path")
    if not isinstance(raw_path, list):
        return []
    return [str(part).strip() for part in raw_path if str(part).strip()]


def _is_excluded_atom_card(card: dict[str, Any]) -> bool:
    return any(
        excluded in part
        for part in _node_path(card)
        for excluded in EXCLUDED_ATOM_PATH_PARTS
    )


def _filter_atom_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in cards:
        if not isinstance(card, dict) or _is_excluded_atom_card(card):
            continue
        identity = str(card.get("card_id") or card.get("title") or "")
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(card)
    return unique[:REFERENCE_RESULT_LIMIT]


def _filter_session_results(
    reference: ReferenceConfig, results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    kept = []
    for result in results:
        if not isinstance(result, dict):
            continue
        if str(result.get("node_id") or "") not in reference.session_nodes:
            continue
        if str(result.get("folder_id") or "") == reference.folder_id:
            continue
        try:
            relevance = float(result.get("relevance"))
        except (TypeError, ValueError):
            continue
        if relevance < reference.session_min_relevance:
            continue
        kept.append((relevance, result))
    kept.sort(key=lambda item: item[0], reverse=True)
    return [result for _, result in kept[:REFERENCE_RESULT_LIMIT]]


async def _search_atom_cards(
    reference: ReferenceConfig,
    query: str,
    *,
    client: Any | None = None,
) -> list[dict[str, Any]]:
    config = McpHttpConfig(
        base_url=reference.atom_base_url,
        auth_headers={"x-api-key": reference.atom_api_key},
        timeout=REFERENCE_TIMEOUT,
    )
    result = await call_stateless_tool(
        config,
        "search_cards",
        {"query": query, "limit": ATOM_RESULT_LIMIT},
        client=client,
    )
    if not isinstance(result, list):
        return []
    return [item for item in result if isinstance(item, dict)]


async def _search_session_results(
    reference: ReferenceConfig,
    query: str,
    *,
    client: Any | None = None,
) -> list[dict[str, Any]]:
    params = {
        "q": query,
        "top_k": 10,
        "include_session_results": "true",
        "session_search_mode": "expanded",
        "search_session_id": "true",
    }
    headers = {"Authorization": f"Bearer {reference.orch_token}"}
    if client is not None:
        response = await client.get("/cogito/search", params=params, headers=headers)
        response.raise_for_status()
        payload = response.json()
    else:
        async with httpx.AsyncClient(
            base_url=reference.orch_base_url, timeout=REFERENCE_TIMEOUT,
        ) as owned_client:
            response = await owned_client.get(
                "/cogito/search", params=params, headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
    if not isinstance(payload, dict):
        return []
    results = payload.get("session_results")
    if not isinstance(results, list):
        return []
    return [item for item in results if isinstance(item, dict)]


def _render_atom_card(card: dict[str, Any]) -> str:
    title = _compact(card.get("title"), 120) or "제목 없음"
    source_ref = str(card.get("source_ref") or "").strip()
    title_part = f"[{title}]({source_ref})" if source_ref else f"[{title}]"
    path = " › ".join(_node_path(card)[-2:]) or "경로 없음"
    snippet = _compact(card.get("snippet"), 300) or "내용 없음"
    return f"- {title_part} — {path} — {snippet}"


def _render_session(result: dict[str, Any]) -> str:
    title = _compact(result.get("title"), 120) or "제목 없음"
    folder = _compact(result.get("folder_name"), 80) or "폴더 없음"
    created = str(result.get("created_at") or "")[:10]
    meta = f"{folder}, {created}" if created else folder
    excerpt = _compact(result.get("excerpt"), 200) or "내용 없음"
    return f"- {title} — {meta} — {excerpt}"


def _render_context_item(
    query: str,
    atom_cards: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
) -> dict[str, str] | None:
    if not atom_cards and not sessions:
        return None
    lines = ["## 참고 자료 (개입을 일으킨 문장으로 찾은 것)", f"검색어: {query}"]
    if atom_cards:
        lines.extend(["### atom-nl", *(_render_atom_card(card) for card in atom_cards)])
    if sessions:
        lines.extend(
            ["### 지난 작업 세션", *(_render_session(result) for result in sessions)]
        )
    return {
        "key": "reference_context",
        "label": "참고 자료",
        "content": "\n".join(lines),
    }


async def _resolve_searches(
    reference: ReferenceConfig,
    query: str,
    *,
    channel_id: str,
    timeout: float,
) -> dict[str, list[dict[str, Any]]]:
    tasks: dict[str, asyncio.Task] = {}
    try:
        if reference.atom_enabled:
            tasks["atom"] = asyncio.create_task(
                _search_atom_cards(reference, query)
            )
        if reference.session_enabled:
            tasks["session"] = asyncio.create_task(
                _search_session_results(reference, query)
            )
        if not tasks:
            return {}
        done, pending = await asyncio.wait(tasks.values(), timeout=timeout)
    except BaseException:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        raise

    for name, task in tasks.items():
        if task in pending:
            logger.warning(
                "reference context %s 검색 시간 초과 (%s)", name, channel_id,
            )
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    results: dict[str, list[dict[str, Any]]] = {}
    for name, task in tasks.items():
        if task not in done:
            continue
        try:
            value = task.result()
        except Exception as exc:
            logger.warning(
                "reference context %s 검색 실패 (%s): %s", name, channel_id, exc,
            )
            continue
        results[name] = value if isinstance(value, list) else []
    return results


async def _build_reference_context_item(
    text: Any,
    reference: ReferenceConfig | None,
    *,
    channel_id: str,
    timeout: float,
) -> dict[str, str] | None:
    query = normalize_query(text)
    if reference is None or len(query) < MIN_QUERY_LENGTH:
        return None

    started = time.monotonic()
    results = await _resolve_searches(
        reference, query, channel_id=channel_id, timeout=timeout,
    )
    if not results:
        return None

    atom_cards = _filter_atom_cards(results.get("atom", []))
    sessions = _filter_session_results(reference, results.get("session", []))
    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "reference context (%s): atom %d/%d, sessions %d/%d, %dms",
        channel_id,
        len(atom_cards),
        len(results.get("atom", [])),
        len(sessions),
        len(results.get("session", [])),
        elapsed_ms,
    )
    return _render_context_item(query, atom_cards, sessions)


async def build_reference_context_item(
    text: Any,
    reference: ReferenceConfig | None,
    *,
    channel_id: str = "",
    timeout: float = REFERENCE_TIMEOUT,
) -> dict[str, str] | None:
    """Fail-open boundary around both reference searches and rendering."""

    try:
        return await _build_reference_context_item(
            text, reference, channel_id=channel_id, timeout=timeout,
        )
    except Exception as exc:
        logger.warning("reference context 생성 실패 (%s): %s", channel_id, exc)
        return None
