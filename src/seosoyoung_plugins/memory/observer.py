"""Observer 모듈

대화 내용을 분석하여 구조화된 관찰 로그를 생성합니다.
소울스트림 LLM 프록시를 통해 API를 호출하고, JSON 형식으로 결과를 파싱합니다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from seosoyoung_plugins.soulstream_client import SoulstreamClient

from seosoyoung_plugins.memory.prompts import (
    build_observer_system_prompt,
    build_observer_user_prompt,
)
from seosoyoung_plugins.memory.store import generate_obs_id

logger = logging.getLogger(__name__)


@dataclass
class ObserverResult:
    """Observer 출력 결과"""

    observations: list[dict] = field(default_factory=list)
    current_task: str = ""
    suggested_response: str = ""
    candidates: list[dict] = field(default_factory=list)


def parse_observer_output(
    text: str, existing_items: list[dict] | None = None
) -> ObserverResult:
    """Observer 응답 JSON을 파싱합니다.

    LLM이 출력한 JSON에서 observations, current_task, suggested_response, candidates를
    추출하고, 각 관찰 항목에 ID를 부여합니다.
    """
    existing = existing_items or []

    try:
        data = _extract_json(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Observer 응답 JSON 파싱 실패, fallback")
        return ObserverResult()

    # observations
    raw_obs = data.get("observations", [])
    observations = _assign_obs_ids(raw_obs, existing) if isinstance(raw_obs, list) else []

    # candidates
    raw_candidates = data.get("candidates", [])
    candidates = []
    if isinstance(raw_candidates, list):
        now_iso = datetime.now(timezone.utc).isoformat()
        for item in raw_candidates:
            if isinstance(item, dict) and item.get("content"):
                candidates.append({
                    "ts": now_iso,
                    "priority": item.get("priority", "🟢"),
                    "content": item["content"],
                })

    return ObserverResult(
        observations=observations,
        current_task=data.get("current_task", ""),
        suggested_response=data.get("suggested_response", ""),
        candidates=candidates,
    )


def _extract_json(text: str) -> dict:
    """응답 텍스트에서 JSON 객체를 추출합니다."""
    text = text.strip()

    # ```json ... ``` 블록
    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start)
        text = text[start:end].strip()
    elif "```" in text:
        start = text.index("```") + 3
        end = text.index("```", start)
        text = text[start:end].strip()

    # { 로 시작하는 부분 찾기
    brace_start = text.find("{")
    if brace_start >= 0:
        brace_end = text.rfind("}")
        if brace_end > brace_start:
            text = text[brace_start:brace_end + 1]

    return json.loads(text)


def _assign_obs_ids(raw_items: list, existing: list[dict]) -> list[dict]:
    """LLM이 출력한 관찰 항목에 ID를 부여합니다.

    기존 항목과 동일한 content+priority 조합이면 기존 ID를 유지합니다.
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
        session_date = raw.get(
            "session_date", datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )

        # ID 결정: LLM이 반환한 id > content+priority 매칭 > 신규 생성
        item_id = raw.get("id")
        if not item_id:
            key = (content, priority)
            item_id = existing_map.get(key)
        if not item_id:
            item_id = generate_obs_id(all_items, session_date)

        item = {
            "id": item_id,
            "priority": priority,
            "content": content,
            "session_date": session_date,
            "created_at": raw.get("created_at", now_iso),
            "source": raw.get("source", "observer"),
        }
        result.append(item)
        all_items.append(item)

    return result


class Observer:
    """대화를 관찰하여 구조화된 관찰 로그를 생성"""

    def __init__(self, soulstream_client: SoulstreamClient, model: str = "gpt-5.4-mini"):
        self.client = soulstream_client
        self.model = model

    async def observe(
        self,
        existing_observations: list[dict] | None,
        messages: list[dict],
    ) -> ObserverResult | None:
        """대화를 관찰하여 새 관찰 로그를 생성합니다.

        Args:
            existing_observations: 기존 관찰 항목 리스트 (없으면 None)
            messages: 누적된 미관찰 대화 내역

        Returns:
            ObserverResult 또는 None (관찰 실패 시)
        """
        system_prompt = build_observer_system_prompt()
        user_prompt = build_observer_user_prompt(existing_observations, messages)

        result = await self.client.complete(
            provider="openai",
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=16_000,
            client_id="memory",
        )

        return parse_observer_output(result.content, existing_observations)
