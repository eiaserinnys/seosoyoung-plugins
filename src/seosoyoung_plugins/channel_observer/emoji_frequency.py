"""Persist reaction frequency and provide compact Jev emoji candidates."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SYSTEM_STATUS_EMOJIS: frozenset[str] = frozenset(
    {"hourglass_flowing_sand", "rocket", "hammer"}
)

DEFAULT_EMOJI_DESCRIPTIONS: dict[str, str] = {
    "ㅋㅋ": "웃긴 이야기, 유머, 재치 있는 농담에 가볍게 웃을 때",
    "eyes": "주목할 만한 소식, 흥미로운 공유, 확인 중일 때",
    "ssy-smirk": "서소영의 의미심장한 미소, 재치 있거나 날카로운 한마디에 공감할 때",
    "ssy-curious": "서소영의 호기심, 흥미롭거나 궁금한 화제일 때",
    "ssy-surprise": "서소영의 놀람, 뜻밖의 소식이나 인상적인 결과일 때",
    "ssy-happy": "서소영의 기쁨, 축하, 감사, 반가운 소식일 때",
    "ssy-laugh": "서소영의 활짝 웃음, 아주 즐겁고 유쾌한 대화일 때",
    "ssy-thinking": "서소영의 깊은 고민, 함께 생각해 볼 문제일 때",
    "ssy-pleading": "서소영의 간절함, 아쉬움이나 응원할 때",
    "ssy-shy": "서소영의 수줍음, 칭찬이나 따뜻한 말에 반응할 때",
    "ssy-cry": "서소영의 눈물, 슬프거나 안타까운 소식일 때",
    "fire": "열정적인 작업, 멋진 성과나 불타오르는 분위기일 때",
    "raised_hands": "환호, 만세, 다 함께 기뻐할 만한 성취일 때",
    "thinking_face": "흥미로운 고민거리나 질문에 공감할 때",
    "beautiful": "아름답거나 완성도 높은 아트·결과물을 봤을 때",
    "laughing": "크게 웃긴 상황이나 유쾌한 농담일 때",
    "tada": "출시, 배포, 작업 완료 등을 축하할 때",
    "clap": "수고와 노력을 격려하고 박수칠 때",
    "thumbsup": "동의, 확인, 좋은 의견에 공감할 때",
    "+1": "찬성, 확인, 가벼운 동의 표시",
    "heart": "따뜻한 감사나 애정 어린 반응",
    "coffee": "여유, 휴식, 티타임 관련 이야기",
    "confetti_ball": "경사스러운 일이나 완료 축하",
    "wave": "인사, 환영, 출퇴근 인사",
    "white_check_mark": "완료 확인, 해결됨",
}


def normalize_emoji_name(name: str) -> str:
    """Remove Slack-style colon delimiters and surrounding whitespace."""
    return str(name or "").strip().strip(":").strip()


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


class EmojiFrequencyStore:
    """Track emoji reaction counts and channels already backfilled."""

    def __init__(self, file_path: Path):
        self.file_path = Path(file_path)
        self.counts: dict[str, int] = {}
        self.seeded_channels: set[str] = set()

        if self.file_path.exists():
            try:
                data = json.loads(self.file_path.read_text(encoding="utf-8"))
                stored_counts = data["counts"]
                stored_channels = data.get("seeded_channels", [])
                if not isinstance(stored_counts, dict) or not isinstance(
                    stored_channels, list
                ):
                    raise ValueError("invalid emoji frequency data")
                self.counts = {
                    str(name): int(count)
                    for name, count in stored_counts.items()
                }
                self.seeded_channels = {str(channel) for channel in stored_channels}
                return
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                pass

        self.counts = {
            name: 1 for name in DEFAULT_EMOJI_DESCRIPTIONS
        }

    def _save(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.file_path.with_name(
            f".{self.file_path.name}.tmp"
        )
        temporary_path.write_text(
            json.dumps(
                {
                    "counts": self.counts,
                    "seeded_channels": sorted(self.seeded_channels),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temporary_path, self.file_path)

    def record_reaction(
        self,
        emoji: str,
        action: str = "added",
        *,
        user_id: str | None = None,
        bot_user_id: str | None = None,
    ) -> bool:
        if bot_user_id and user_id == bot_user_id:
            return False

        name = normalize_emoji_name(emoji)
        if not name or name in SYSTEM_STATUS_EMOJIS:
            return False

        if action == "added":
            self.counts[name] = self.counts.get(name, 0) + 1
        elif action == "removed":
            count = max(0, self.counts.get(name, 0) - 1)
            if count == 0 and name not in DEFAULT_EMOJI_DESCRIPTIONS:
                self.counts.pop(name, None)
            else:
                self.counts[name] = count
        else:
            return False

        self._save()
        return True

    def seed_from_messages(
        self,
        channel_id: str,
        messages: list[Any],
        *,
        bot_user_id: str | None = None,
    ) -> int:
        if channel_id in self.seeded_channels:
            return 0

        total_added = 0
        for message in messages:
            for reaction in _field(message, "reactions", []) or []:
                name = normalize_emoji_name(_field(reaction, "name", ""))
                if not name or name in SYSTEM_STATUS_EMOJIS:
                    continue
                count = int(_field(reaction, "count", 0) or 0)
                users = _field(reaction, "users", []) or []
                valid_count = max(0, count - int(bool(bot_user_id and bot_user_id in users)))
                if valid_count:
                    self.counts[name] = self.counts.get(name, 0) + valid_count
                    total_added += valid_count

        self.seeded_channels.add(channel_id)
        self._save()
        return total_added

    def get_top_candidates(self, limit: int = 30) -> dict[str, str]:
        limit = max(1, min(255, int(limit)))
        ranked = [
            (name, count)
            for name, count in self.counts.items()
            if count > 0 and name not in SYSTEM_STATUS_EMOJIS
        ]
        if ranked:
            ranked.sort(
                key=lambda item: (
                    -item[1],
                    item[0] not in DEFAULT_EMOJI_DESCRIPTIONS,
                    item[0],
                )
            )
            selected = ranked[:limit]
        else:
            selected = [
                (name, self.counts.get(name, 0))
                for name in DEFAULT_EMOJI_DESCRIPTIONS
            ][:limit]

        return {
            name: (
                f"{DEFAULT_EMOJI_DESCRIPTIONS.get(name, f':{name}: 커스텀 이모지')} "
                f"(사용 빈도 {count})"
            )
            for name, count in selected
        }
