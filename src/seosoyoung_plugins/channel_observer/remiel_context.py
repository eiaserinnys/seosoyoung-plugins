"""Remiel interpretation lookup for channel interventions."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

WINDOW_CONTEXT_CONFIDENCE_THRESHOLD = 0.75
WINDOW_CONTEXT_SUMMARY_LIMIT = 160
WINDOW_CONTEXT_ITEM_LIMIT = 120
WINDOW_CONTEXT_LIST_LIMIT = 2


@dataclass(frozen=True)
class RemielContextConfig:
    """Configuration for remiel context lookup.

    Empty base_url or api_key disables lookup. The source of this config is the
    channel_observer plugin config snapshot, not environment variables.
    """

    base_url: str = ""
    api_key: str = ""
    confidence_threshold: float = 0.75
    timeout: float = 2.0

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key)

    @classmethod
    def from_plugin_config(cls, config: dict[str, Any]) -> "RemielContextConfig":
        return cls(
            base_url=str(config.get("remiel_base_url", "") or "").strip().rstrip("/"),
            api_key=str(config.get("remiel_api_key", "") or "").strip(),
            confidence_threshold=float(config.get("remiel_confidence_threshold", 0.75)),
            timeout=float(config.get("remiel_lookup_timeout", 2.0)),
        )


async def lookup_remiel_context(
    config: RemielContextConfig | None,
    *,
    channel_id: str,
    timestamps: list[str],
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """Fetch remiel interpretation lookup payload.

    Failures are intentionally fail-open: channel intervention should proceed
    with thread_context alone when remiel is unavailable.
    """

    if config is None or not config.enabled or not timestamps:
        return None

    payload = {
        "channel_id": channel_id,
        "timestamps": timestamps,
        "confidence_threshold": config.confidence_threshold,
    }
    headers = {"x-api-key": config.api_key}

    try:
        if client is not None:
            response = await client.post(
                "/api/interpretations/lookup",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
        else:
            async with httpx.AsyncClient(
                base_url=config.base_url,
                timeout=config.timeout,
            ) as owned_client:
                response = await owned_client.post(
                    "/api/interpretations/lookup",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("remiel lookup 실패 (%s): %s", channel_id, exc)
        return None

    return data if isinstance(data, dict) else None


async def build_remiel_context_item(
    config: RemielContextConfig | None,
    *,
    channel_id: str,
    timestamps: list[str],
) -> dict[str, str] | None:
    payload = await lookup_remiel_context(
        config,
        channel_id=channel_id,
        timestamps=timestamps,
    )
    try:
        return render_remiel_context_item(payload)
    except (TypeError, ValueError) as exc:
        logger.warning("remiel context 렌더링 실패 (%s): %s", channel_id, exc)
        return None


def render_remiel_context_item(payload: dict[str, Any] | None) -> dict[str, str] | None:
    """Render lookup payload as a Soulstream context item."""

    if not payload or payload.get("channel_enabled") is not True:
        return None

    coverage = payload.get("coverage")
    if not isinstance(coverage, dict) or int(coverage.get("ready") or 0) <= 0:
        return None

    content = _render_payload(payload, coverage)
    return {
        "key": "remiel_context",
        "label": "remiel 해석 컨텍스트",
        "content": content,
    }


def _render_payload(payload: dict[str, Any], coverage: dict[str, Any]) -> str:
    channel_id = payload.get("channel_id", "")
    threshold = payload.get("confidence_threshold", "")
    lines = [
        "## remiel 해석 컨텍스트",
        "",
        f"- channel_id: `{channel_id}`",
        f"- confidence_threshold: `{threshold}`",
        "- coverage: "
        f"requested={coverage.get('requested', 0)}, "
        f"ready={coverage.get('ready', 0)}, "
        f"needs_reasoning={coverage.get('needs_reasoning', 0)}, "
        f"low_confidence={coverage.get('low_confidence', 0)}, "
        f"stale={coverage.get('stale', 0)}, "
        f"missing={coverage.get('missing_message', 0) + coverage.get('missing_interpretation', 0)}",
    ]

    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    ready_items = [item for item in items if isinstance(item, dict) and item.get("status") == "ready"]
    unresolved_items = [item for item in items if isinstance(item, dict) and item.get("status") != "ready"]

    window_lines = _render_window_context(payload.get("window_context"))
    if window_lines:
        lines.extend(["", *window_lines])

    lines.extend(["", "### ready"])
    for item in ready_items:
        lines.extend(_render_ready_item(item))

    if unresolved_items:
        lines.extend(["", "### needs_reasoning"])
        for item in unresolved_items:
            ts = item.get("ts", "")
            status = item.get("status", "unknown")
            detail = _render_unresolved_detail(item)
            lines.append(f"- `{ts}`: {status}{detail}")

    return "\n".join(lines).strip()


def _render_window_context(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []

    confidence = value.get("confidence")
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or confidence < WINDOW_CONTEXT_CONFIDENCE_THRESHOLD
    ):
        return []

    summary = _compact_text(value.get("summary"), WINDOW_CONTEXT_SUMMARY_LIMIT)
    if not summary:
        return []

    lines = [
        "### window_context",
        f"- confidence: {confidence}",
        f"- summary: {summary}",
    ]
    for key in (
        "candidate_angles",
        "open_loops",
        "avoid_repetition_notes",
        "participants_focus",
    ):
        rendered = _compact_list(value.get(key))
        if rendered:
            lines.append(f"- {key}: {' / '.join(rendered)}")
    return lines


def _compact_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    rendered: list[str] = []
    for item in value:
        text = _compact_text(item, WINDOW_CONTEXT_ITEM_LIMIT)
        if text:
            rendered.append(text)
        if len(rendered) >= WINDOW_CONTEXT_LIST_LIMIT:
            break
    return rendered


def _compact_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return f"{text[:limit - 3]}..."


def _render_ready_item(item: dict[str, Any]) -> list[str]:
    ts = item.get("ts", "")
    confidence = item.get("confidence", "")
    intent = item.get("intent", "")
    summary = item.get("summary", "")
    addressees = _format_addressees(item.get("addressees"))
    adversarial_note = item.get("adversarial_note")

    lines = [
        f"- `{ts}` confidence={confidence} intent={intent}",
        f"  - addressees: {addressees}",
        f"  - summary: {summary}",
    ]
    if adversarial_note:
        lines.append(f"  - adversarial_note: {adversarial_note}")
    return lines


def _format_addressees(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "(없음)"

    rendered = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or "unknown"
        user_id = entry.get("id") or ""
        rendered.append(f"{name}({user_id})" if user_id else str(name))
    return ", ".join(rendered) if rendered else "(없음)"


def _render_unresolved_detail(item: dict[str, Any]) -> str:
    parts = []
    if "confidence" in item:
        parts.append(f"confidence={item['confidence']}")
    if "threshold" in item:
        parts.append(f"threshold={item['threshold']}")
    if "updated_at" in item:
        parts.append(f"updated_at={item['updated_at']}")
    return f" ({', '.join(parts)})" if parts else ""
