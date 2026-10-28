"""TypeSafe Jev message-level reaction and intervention judgment."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JevMessageVerdict:
    should_react: bool
    react_score: float
    selected_emoji: str | None
    emoji_confidence: float
    emoji_probabilities: dict[str, float]
    should_intervene: bool
    intervene_score: float
    suppressed_by_density: bool = False


def compute_reaction_density(
    recent_messages: list[dict], bot_user_id: str | None = None
) -> dict[str, Any]:
    del bot_user_id
    last_bot_emoji = next(
        (
            reactions[-1]
            for message in reversed(recent_messages)
            if (reactions := message.get("bot_reactions"))
        ),
        None,
    )
    consecutive_bot_reactions = 0
    for message in reversed(recent_messages):
        if not message.get("bot_reactions"):
            break
        consecutive_bot_reactions += 1

    return {
        "recent_message_count": len(recent_messages),
        "messages_with_any_emoji": sum(
            bool(message.get("reactions")) for message in recent_messages
        ),
        "messages_with_bot_emoji": sum(
            bool(message.get("bot_reactions")) for message in recent_messages
        ),
        "last_5_bot_emoji_count": sum(
            bool(message.get("bot_reactions")) for message in recent_messages[-5:]
        ),
        "consecutive_bot_reactions": consecutive_bot_reactions,
        "last_bot_emoji": last_bot_emoji,
    }


class JevChannelJudge:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.typesafe.ai",
        model: str = "jev-latest",
        timeout: float = 5.0,
        emoji_threshold: float = 0.50,
        intervene_threshold: float = 0.65,
        trigger_intervene_threshold: float = 0.35,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.emoji_threshold = emoji_threshold
        self.intervene_threshold = intervene_threshold
        self.trigger_intervene_threshold = trigger_intervene_threshold

    def build_payload(
        self,
        *,
        channel_id: str,
        target_message: dict,
        recent_messages: list[dict],
        emoji_candidates: dict[str, str],
        trigger_words_matched: list[str] | None = None,
        bot_user_id: str | None = None,
    ) -> dict[str, Any]:
        density = compute_reaction_density(
            recent_messages, bot_user_id=bot_user_id
        )
        state = {
            "channel_id": channel_id,
            "recent_reaction_summary": density,
            "recent_messages": [
                {
                    key: message.get(key)
                    for key in ("ts", "user", "text", "reactions", "bot_reactions")
                }
                for message in recent_messages
            ],
            "target_message": {
                key: target_message.get(key)
                for key in ("ts", "user", "text", "thread_ts")
            },
            "trigger_words_matched": trigger_words_matched or [],
        }
        return {
            "model": self.model,
            "state": json.dumps(state, ensure_ascii=False),
            "questions": {
                "should_react": {
                    "type": "noul",
                    "instructions": (
                        "이전 메시지들(recent_messages의 reactions/bot_reactions와 "
                        "recent_reaction_summary)에 달린 이모지를 확인하세요. 모든 메시지에 "
                        "연속으로 이모지를 달지 않도록 엄격히 절제하고, 평범한 단답이나 "
                        "업무 전달에는 달지 마세요. 유머, 축하, 반가운 소식, 강한 공감처럼 "
                        "꼭 어울릴 때만 반응하세요."
                    ),
                    "criteria": {
                        "true": "이전 메시지들에 이모지가 과도하지 않으며 대상 메시지에 감정 표현이나 공감 이모지를 다는 것이 매우 자연스럽다.",
                        "false": "평범한 대화이거나 이전 메시지들에 이미 이모지가 달려 있어 연속 부착을 피해야 한다.",
                    },
                },
                "emoji": {
                    "type": "choice",
                    "instructions": "대상 메시지(target_message)에 이모지를 단다면 문맥과 분위기에 가장 어울리는 Slack 이모지 이름을 하나 고르세요.",
                    "criteria": emoji_candidates,
                },
                "should_intervene": {
                    "type": "noul",
                    "instructions": (
                        "대상 메시지에 서소영(소영, <엠버 앤 블레이드> 등장인물이자 팀 지원 에이전트)이 "
                        "대화에 끼어들어 답글을 달아야 하는지 판단하세요. 절대 개입 금지(false): "
                        "(1) 서소영이 아닌 특정 타인(@멘션이나 이름)을 지목한 질문·요청, "
                        "(2) 다른 멤버들끼리 진행 중인 실무 스레드 문답, (3) 멤버 본인의 결정을 "
                        "평가하거나 훈수하는 상황, (4) 단순 단답이나 평범한 잡담. 개입 적합(true): "
                        "(1) '소영', '서소영' 등 트리거 단어로 부르거나 언급한 경우, "
                        "(2) 서소영이나 <엠버 앤 블레이드> 설정·지식으로 답할 수 있는 열린 질문이거나 "
                        "자연스럽게 한마디 보탤 명확한 계기가 있는 경우."
                    ),
                    "criteria": {
                        "true": "서소영을 부르거나 서소영이 답하기에 꼭 알맞은 열린 질문·화제여서 대화에 끼어드는 것이 자연스럽다.",
                        "false": "타인 지목 질문, 실무 스레드 문답, 훈수 위험, 평범한 잡담 등으로 조용히 지켜보는 것이 맞다.",
                    },
                },
            },
        }

    async def judge_message(
        self,
        *,
        channel_id: str,
        target_message: dict,
        recent_messages: list[dict],
        emoji_candidates: dict[str, str],
        trigger_words_matched: list[str] | None = None,
        bot_user_id: str | None = None,
    ) -> JevMessageVerdict | None:
        if not self.api_key:
            logger.warning("Jev 판단 생략: API 키가 설정되지 않았습니다")
            return None

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url.rstrip('/')}/v1/systemone",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=self.build_payload(
                        channel_id=channel_id,
                        target_message=target_message,
                        recent_messages=recent_messages,
                        emoji_candidates=emoji_candidates,
                        trigger_words_matched=trigger_words_matched,
                        bot_user_id=bot_user_id,
                    ),
                )
                response.raise_for_status()
                answers = response.json()["answers"]

            react_score = float(answers["should_react"]["noul"])
            emoji_answer = answers.get("emoji", {}) or {}
            choice = emoji_answer.get("choice")
            confidence = float(emoji_answer.get("confidence", 0.0))
            probabilities = {
                key: float(value)
                for key, value in (emoji_answer.get("probabilities") or {}).items()
            }
            intervene_score = float(answers["should_intervene"]["noul"])

            density = compute_reaction_density(
                recent_messages, bot_user_id=bot_user_id
            )
            suppressed_by_density = (
                density["consecutive_bot_reactions"] >= 2
                or density["last_5_bot_emoji_count"] >= 2
            )
            effective_emoji_threshold = (
                min(0.85, self.emoji_threshold + 0.20)
                if density["consecutive_bot_reactions"] == 1
                else self.emoji_threshold
            )
            should_react = (
                not suppressed_by_density
                and react_score >= effective_emoji_threshold
                and bool(choice)
            )
            effective_intervene_threshold = (
                self.trigger_intervene_threshold
                if trigger_words_matched
                else self.intervene_threshold
            )
            should_intervene = intervene_score >= effective_intervene_threshold
            return JevMessageVerdict(
                should_react=should_react,
                react_score=react_score,
                selected_emoji=choice,
                emoji_confidence=confidence,
                emoji_probabilities=probabilities,
                should_intervene=should_intervene,
                intervene_score=intervene_score,
                suppressed_by_density=suppressed_by_density,
            )
        except Exception as error:
            logger.warning("Jev 메시지 판단 실패 (%s): %s", channel_id, error)
            return None
