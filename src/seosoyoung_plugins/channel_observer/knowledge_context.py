"""Atom BM25 context lookup for channel interventions."""

from __future__ import annotations

import asyncio
from html import unescape
import logging
import re
from typing import Any, Awaitable, Callable


logger = logging.getLogger(__name__)

SearchCards = Callable[[str], Awaitable[list[dict[str, Any]]]]
HTML_TAG = re.compile(r"<[^>]+>")


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


def _is_slack_conversation(card: dict[str, Any]) -> bool:
    return any("슬랙 대화" in part for part in _node_path(card))


def _is_library_history(card: dict[str, Any]) -> bool:
    path = _node_path(card)
    return (
        any("서소영의 서재" in part for part in path)
        and any("발행 이력" in part for part in path)
    )


def _identity(card: dict[str, Any]) -> str:
    for key in ("card_id", "node_id"):
        value = card.get(key)
        if value:
            return f"{key}:{value}"
    return f"title:{card.get('title', '')}"


def _render_card(card: dict[str, Any]) -> str:
    title = _compact(card.get("title"), 120) or "제목 없음"
    source_ref = str(card.get("source_ref") or "").strip()
    title_part = f"[{title}]({source_ref})" if source_ref else f"[{title}]"
    path = " › ".join(_node_path(card)[-2:]) or "경로 없음"
    snippet = _compact(card.get("snippet"), 300) or "내용 없음"
    return f"- {title_part} — {path} — {snippet}"


async def _build_knowledge_context_item(
    keywords: list[str],
    search_cards: SearchCards | None,
    *,
    timeout: float = 2.0,
) -> dict[str, str] | None:
    """Search all keywords within one deadline and render at most three cards."""
    normalized = list(dict.fromkeys(k.strip() for k in keywords if k.strip()))
    if not normalized or search_cards is None:
        return None

    tasks: list[asyncio.Task] = []
    try:
        for keyword in normalized:
            tasks.append(asyncio.create_task(search_cards(keyword)))
        done, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout))
    except BaseException:
        # Construction can fail after earlier keyword tasks were scheduled;
        # cancellation must not leave those searches detached from the pipeline.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    discovered: list[dict[str, Any]] = []
    for keyword, task in zip(normalized, tasks):
        if task not in done or task.cancelled():
            continue
        try:
            result = task.result()
        except Exception as exc:
            logger.warning("atom search_cards 실패 (%s): %s", keyword, exc)
            continue
        if isinstance(result, list):
            discovered.extend(card for card in result if isinstance(card, dict))

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in discovered:
        if _is_slack_conversation(card):
            continue
        identity = _identity(card)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(card)

    ranked = sorted(unique, key=lambda card: not _is_library_history(card))[:3]
    if not ranked:
        return None
    return {
        "key": "knowledge_context",
        "label": "관련 지식 (atom)",
        "content": "\n".join(
            ["## 관련 지식 (atom)", "", *(_render_card(card) for card in ranked)]
        ),
    }


async def build_knowledge_context_item(
    keywords: list[str],
    search_cards: SearchCards | None,
    *,
    timeout: float = 2.0,
) -> dict[str, str] | None:
    """Fail-open boundary around all Atom search, ranking, and rendering work."""
    try:
        return await _build_knowledge_context_item(
            keywords, search_cards, timeout=timeout,
        )
    except Exception as exc:
        logger.warning("knowledge context 생성 실패: %s", exc)
        return None
