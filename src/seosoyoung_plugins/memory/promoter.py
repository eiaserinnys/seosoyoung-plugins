"""Promoter / Compactor 모듈

장기 기억 후보를 검토하여 승격(Promoter)하고,
장기 기억이 임계치를 넘으면 압축(Compactor)합니다.

소울스트림 LLM 프록시를 통해 API를 호출합니다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from seosoyoung_plugins.soulstream_client import SoulstreamClient

from seosoyoung_plugins.memory.prompts import build_compactor_prompt, build_promoter_prompt
from seosoyoung_plugins.memory.store import generate_ltm_id
from seosoyoung_plugins.utils.token_counter import TokenCounter

logger = logging.getLogger(__name__)


@dataclass
class PromoterResult:
    """Promoter 출력 결과"""

    promoted: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    promoted_count: int = 0
    rejected_count: int = 0
    priority_counts: dict = None

    def __post_init__(self):
        if self.priority_counts is None:
            self.priority_counts = {}


@dataclass
class CompactorResult:
    """Compactor 출력 결과"""

    compacted: list[dict] = field(default_factory=list)
    token_count: int = 0


def _extract_json(text: str) -> dict | list:
    """응답 텍스트에서 JSON을 추출합니다."""
    text = text.strip()

    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start)
        text = text[start:end].strip()
    elif "```" in text:
        start = text.index("```") + 3
        end = text.index("```", start)
        text = text[start:end].strip()

    # 배열 또는 객체
    bracket_start = text.find("[")
    brace_start = text.find("{")

    if bracket_start >= 0 and (brace_start < 0 or bracket_start < brace_start):
        bracket_end = text.rfind("]")
        if bracket_end > bracket_start:
            return json.loads(text[bracket_start:bracket_end + 1])

    if brace_start >= 0:
        brace_end = text.rfind("}")
        if brace_end > brace_start:
            return json.loads(text[brace_start:brace_end + 1])

    return json.loads(text)


def _assign_ltm_ids(raw_items: list, existing: list[dict]) -> list[dict]:
    """LTM 항목에 ID를 부여합니다.

    기존 항목과 content+priority가 일치하면 기존 ID를 유지합니다.
    LLM이 id를 반환한 경우 그 ID를 우선 사용합니다.
    """
    existing_map: dict[tuple, str] = {}
    for item in existing:
        key = (item.get("content", ""), item.get("priority", ""))
        existing_map[key] = item.get("id", "")

    result: list[dict] = []
    all_items = list(existing)
    now_iso = datetime.now(timezone.utc).isoformat()

    for raw in raw_items:
        if not isinstance(raw, dict) or not raw.get("content"):
            continue

        priority = raw.get("priority", "🟢")
        content = raw["content"]

        # ID 결정: LLM이 반환한 id > content+priority 매칭 > 신규 생성
        item_id = raw.get("id")
        if not item_id:
            key = (content, priority)
            item_id = existing_map.get(key)
        if not item_id:
            item_id = generate_ltm_id(all_items)

        item = {
            "id": item_id,
            "priority": priority,
            "content": content,
            "promoted_at": raw.get("promoted_at", now_iso),
        }
        if raw.get("source_obs_ids"):
            item["source_obs_ids"] = raw["source_obs_ids"]

        result.append(item)
        all_items.append(item)

    return result


def parse_promoter_output(
    text: str, existing_items: list[dict] | None = None
) -> PromoterResult:
    """Promoter 응답 JSON에서 promoted와 rejected를 파싱합니다."""
    existing = existing_items or []

    try:
        data = _extract_json(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Promoter 응답 JSON 파싱 실패")
        return PromoterResult()

    if not isinstance(data, dict):
        return PromoterResult()

    # promoted
    raw_promoted = data.get("promoted", [])
    promoted = (
        _assign_ltm_ids(raw_promoted, existing) if isinstance(raw_promoted, list) else []
    )

    # rejected
    raw_rejected = data.get("rejected", [])
    rejected = raw_rejected if isinstance(raw_rejected, list) else []

    # 우선순위 카운트
    priority_counts: dict[str, int] = {}
    for item in promoted:
        p = item.get("priority", "🟢")
        priority_counts[p] = priority_counts.get(p, 0) + 1

    return PromoterResult(
        promoted=promoted,
        rejected=rejected,
        promoted_count=len(promoted),
        rejected_count=len(rejected),
        priority_counts=priority_counts,
    )


def parse_compactor_output(
    text: str, existing_items: list[dict] | None = None
) -> list[dict]:
    """Compactor 응답에서 JSON 배열을 파싱합니다."""
    existing = existing_items or []

    try:
        data = _extract_json(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Compactor 응답 JSON 파싱 실패")
        return existing  # fallback: 기존 항목 유지

    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_items = data.get("compacted", data.get("items", []))
    else:
        return existing

    return _assign_ltm_ids(raw_items, existing)


class Promoter:
    """장기 기억 후보를 검토하여 승격"""

    def __init__(self, soulstream_client: SoulstreamClient, model: str = "gpt-5.2"):
        self.client = soulstream_client
        self.model = model

    async def promote(
        self,
        candidates: list[dict],
        existing_persistent: list[dict],
    ) -> PromoterResult:
        """후보 항목들을 검토하여 장기 기억 승격 여부를 판단합니다.

        Args:
            candidates: 후보 항목 리스트 [{"ts": ..., "priority": ..., "content": ...}]
            existing_persistent: 기존 장기 기억 항목 리스트

        Returns:
            PromoterResult
        """
        prompt = build_promoter_prompt(existing_persistent, candidates)

        result = await self.client.complete(
            provider="openai",
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8_000,
            client_id="memory",
        )

        return parse_promoter_output(result.content, existing_persistent)

    @staticmethod
    def merge_promoted(existing: list[dict], promoted: list[dict]) -> list[dict]:
        """승격된 항목을 기존 장기 기억에 머지합니다. ID 기반 중복 제거."""
        merged = list(existing)
        existing_ids = {item.get("id") for item in existing if item.get("id")}

        for item in promoted:
            item_id = item.get("id")
            if item_id and item_id in existing_ids:
                # 기존 항목 업데이트
                for i, ex in enumerate(merged):
                    if ex.get("id") == item_id:
                        merged[i] = item
                        break
            else:
                merged.append(item)

        return merged


class Compactor:
    """장기 기억을 압축"""

    def __init__(self, soulstream_client: SoulstreamClient, model: str = "gpt-5.2"):
        self.client = soulstream_client
        self.model = model
        self.token_counter = TokenCounter()

    async def compact(
        self,
        persistent: list[dict],
        target_tokens: int,
    ) -> CompactorResult:
        """장기 기억을 압축합니다.

        Args:
            persistent: 현재 장기 기억 항목 리스트
            target_tokens: 목표 토큰 수

        Returns:
            CompactorResult
        """
        prompt = build_compactor_prompt(persistent, target_tokens)

        result = await self.client.complete(
            provider="openai",
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=16_000,
            client_id="memory",
        )

        compacted = parse_compactor_output(result.content, persistent)
        token_count = self.token_counter.count_string(
            json.dumps(compacted, ensure_ascii=False)
        )

        return CompactorResult(compacted=compacted, token_count=token_count)
