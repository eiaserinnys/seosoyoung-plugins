"""Reflector 모듈

관찰 로그가 임계치를 초과할 때 재구조화하고 압축합니다.
OpenAI API를 사용하여 관찰 로그를 요약하고, JSON 형식으로 결과를 파싱합니다.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import openai

from seosoyoung_plugins.memory.prompts import (
    build_reflector_system_prompt,
    build_reflector_retry_prompt,
)
from seosoyoung_plugins.memory.store import generate_obs_id
from seosoyoung_plugins.utils.token_counter import TokenCounter

logger = logging.getLogger(__name__)


@dataclass
class ReflectorResult:
    """Reflector 출력 결과"""

    observations: list[dict] = field(default_factory=list)
    token_count: int = 0


def _parse_reflector_output(text: str) -> list[dict]:
    """Reflector 응답 JSON에서 관찰 항목 리스트를 추출합니다."""
    text = text.strip()

    # ```json ... ``` 블록 제거
    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start)
        text = text[start:end].strip()
    elif "```" in text:
        start = text.index("```") + 3
        end = text.index("```", start)
        text = text[start:end].strip()

    # JSON 배열 또는 객체 파싱
    bracket_start = text.find("[")
    brace_start = text.find("{")

    if bracket_start >= 0 and (brace_start < 0 or bracket_start < brace_start):
        bracket_end = text.rfind("]")
        if bracket_end > bracket_start:
            raw = json.loads(text[bracket_start:bracket_end + 1])
            return raw if isinstance(raw, list) else []

    if brace_start >= 0:
        brace_end = text.rfind("}")
        if brace_end > brace_start:
            data = json.loads(text[brace_start:brace_end + 1])
            obs = data.get("observations", [])
            return obs if isinstance(obs, list) else []

    return []


def _assign_reflector_ids(raw_items: list) -> list[dict]:
    """Reflector가 출력한 항목에 ID를 부여합니다."""
    result: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for raw in raw_items:
        if not isinstance(raw, dict) or not raw.get("content"):
            continue

        session_date = raw.get(
            "session_date", datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )
        item_id = raw.get("id") or generate_obs_id(result, session_date)

        item = {
            "id": item_id,
            "priority": raw.get("priority", "🟢"),
            "content": raw["content"],
            "session_date": session_date,
            "created_at": raw.get("created_at", now_iso),
            "source": "reflector",
        }
        result.append(item)

    return result


class Reflector:
    """관찰 로그를 압축하고 재구조화"""

    def __init__(self, api_key: str, model: str = "gpt-4.1-mini"):
        self.client = openai.AsyncOpenAI(api_key=api_key)
        self.model = model
        self.token_counter = TokenCounter()

    async def reflect(
        self,
        observations: list[dict],
        target_tokens: int = 15000,
    ) -> ReflectorResult | None:
        """관찰 로그를 압축합니다.

        Args:
            observations: 압축할 관찰 항목 리스트
            target_tokens: 목표 토큰 수

        Returns:
            ReflectorResult 또는 None (API 오류 시)
        """
        system_prompt = build_reflector_system_prompt()
        obs_json = json.dumps(observations, ensure_ascii=False, indent=2)

        try:
            # 1차 시도
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": obs_json},
                ],
                max_completion_tokens=16_000,
            )

            result_text = response.choices[0].message.content or ""
            raw_items = _parse_reflector_output(result_text)
            compressed = _assign_reflector_ids(raw_items)
            token_count = self.token_counter.count_string(
                json.dumps(compressed, ensure_ascii=False)
            )

            logger.info(
                f"Reflector 1차 압축: {token_count} tokens (목표: {target_tokens})"
            )

            # 목표 이하면 바로 반환
            if token_count <= target_tokens:
                return ReflectorResult(
                    observations=compressed,
                    token_count=token_count,
                )

            # 2차 시도 (재시도)
            retry_prompt = build_reflector_retry_prompt(token_count, target_tokens)
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": obs_json},
                    {"role": "assistant", "content": result_text},
                    {"role": "user", "content": retry_prompt},
                ],
                max_completion_tokens=16_000,
            )

            retry_text = response.choices[0].message.content or ""
            raw_items = _parse_reflector_output(retry_text)
            compressed = _assign_reflector_ids(raw_items)
            token_count = self.token_counter.count_string(
                json.dumps(compressed, ensure_ascii=False)
            )

            logger.info(
                f"Reflector 2차 압축: {token_count} tokens (목표: {target_tokens})"
            )

            return ReflectorResult(
                observations=compressed,
                token_count=token_count,
            )

        except Exception as e:
            logger.error(f"Reflector API 호출 실패: {e}")
            return None
