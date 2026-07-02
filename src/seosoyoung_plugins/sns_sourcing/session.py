"""Soulstream session prompt and decision parsing for SNS sourcing."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from seosoyoung.plugin_sdk import soulstream
from seosoyoung.plugin_sdk.caller_info import (
    build_bot_caller_info,
    get_host_preferred_node,
)

from seosoyoung_plugins.sns_sourcing.collector import SnsCandidate


@dataclass
class SnsDecision:
    channel_id: str
    ts: str
    label: str
    reason: str
    asset_summary: str = ""
    drafts: list[dict[str, str]] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.channel_id}:{self.ts}"

    @property
    def is_usable(self) -> bool:
        return self.label == "usable"


class SnsDecisionSession:
    """Runs the batch classification prompt through Soulstream."""

    def __init__(
        self,
        *,
        output_channel: str,
        folder_id: str,
        agent_id: str,
        debug_channel: str = "",
        max_candidates: int = 8,
        soulstream_api: Any = soulstream,
    ):
        self.output_channel = output_channel
        self.debug_channel = debug_channel
        self.folder_id = folder_id
        self.agent_id = agent_id
        self.max_candidates = max_candidates
        self.soulstream = soulstream_api

    async def classify(self, candidates: list[SnsCandidate], slot_key: str) -> list[SnsDecision]:
        if not candidates:
            return []
        prompt = build_classification_prompt(candidates)
        result = await self.soulstream.run(
            prompt=prompt,
            channel=self.debug_channel or self.output_channel,
            thread_ts=_synthetic_thread_ts(),
            text_only=True,
            folder_id=self.folder_id,
            agent_id=self.agent_id,
            caller_info=build_bot_caller_info(
                source="sns_sourcing",
                display_name="SNS 소재 수집",
                agent_node=get_host_preferred_node(),
            ),
            system_prompt=(
                "너는 게임 마케팅용 SNS 소재를 선별하는 편집자다. "
                "반드시 사용자가 요구한 JSON 형식만 출력한다."
            ),
        )
        if not result.ok:
            raise RuntimeError(result.error or "soulstream.run failed")
        payload = parse_decision_payload(result.output or "\n".join(result.utterances))
        return [
            SnsDecision(
                channel_id=item["channel_id"],
                ts=item["ts"],
                label=item["label"],
                reason=item.get("reason", ""),
                asset_summary=item.get("asset_summary", ""),
                drafts=item.get("drafts", []) or [],
                hashtags=item.get("hashtags", []) or [],
            )
            for item in payload.get("decisions", [])
        ]


def build_classification_prompt(candidates: list[SnsCandidate]) -> str:
    data = [
        {
            "channel_id": c.channel_id,
            "channel_name": c.channel_name,
            "ts": c.ts,
            "thread_ts": c.thread_ts,
            "permalink": c.permalink,
            "text": c.text,
            "user": c.user,
            "mimetypes": c.mimetypes,
            "files": [asdict(file) for file in c.files],
            "context": c.context,
        }
        for c in candidates
    ]
    return (
        "SNS 소재 후보를 판별하고 초안을 작성해라.\n\n"
        "분류는 정확히 셋 중 하나다.\n"
        "- usable: 공개 SNS 소재로 쓸만함\n"
        "- irrelevant: 업무 조율, 잡담, 구현 논의처럼 SNS 소재와 무관함\n"
        "- non_public: 버그, 내부 수치, 미공개 WIP, 스포일러, 라이선스, 민감정보, 비공개 대화\n\n"
        "판별 절차:\n"
        "1. 먼저 후보의 텍스트와 전후 맥락만으로 판단한다.\n"
        "2. 판단이 불충분하고 후보에 미디어 mimetype이 있으면, 세션이 직접 MCP "
        "`slack_download_thread_files(channel, thread_ts)`로 원본을 다운로드한다.\n"
        "3. 영상은 Bash에서 `/usr/bin/ffmpeg`로 6~9장 프레임을 추출한 뒤 Read 도구로 본다.\n"
        "4. 비전 판독은 폴백이다. 텍스트만으로 충분하면 다운로드하지 않는다.\n"
        "5. 입력 채널에는 쓰지 말고, 이모지 리액션도 달지 않는다.\n\n"
        "usable이면 drafts에 영문 카피 2~3안과 각 국문 번역을 넣고 hashtags를 제안한다. "
        "irrelevant/non_public이면 drafts와 hashtags는 빈 배열이다.\n\n"
        "출력은 다음 JSON만 허용한다.\n"
        "{\n"
        '  "decisions": [\n'
        "    {\n"
        '      "channel_id": "C...",\n'
        '      "ts": "1234567890.123456",\n'
        '      "label": "usable|irrelevant|non_public",\n'
        '      "reason": "근거 1문장",\n'
        '      "asset_summary": "소재 요약",\n'
        '      "drafts": [{"en": "...", "ko": "..."}],\n'
        '      "hashtags": ["#..."]\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "후보 JSON:\n"
        f"{json.dumps(data, ensure_ascii=False, indent=2)}"
    )


def parse_decision_payload(text: str) -> dict[str, Any]:
    payload = _extract_json(text)
    data = json.loads(payload)
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        raise ValueError("decision output must contain decisions list")
    for item in data["decisions"]:
        if item.get("label") not in {"usable", "irrelevant", "non_public"}:
            raise ValueError(f"invalid decision label: {item.get('label')}")
        if not item.get("channel_id") or not item.get("ts"):
            raise ValueError("decision item missing channel_id or ts")
    return data


def _extract_json(text: str) -> str:
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.S)
    if fence:
        return fence.group(1)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("decision output does not contain JSON object")
    return stripped[start : end + 1]


def _synthetic_thread_ts() -> str:
    return f"{time.time():.6f}"

