"""컨텍스트 빌더

장기 기억과 세션 관찰 로그를 시스템 프롬프트로 변환하여 Claude 세션에 주입합니다.
OM의 processInputStep에 해당하는 부분입니다.

주입 계층:
- 장기 기억 (persistent/recent.json): 매 세션 시작 시 항상 주입
- 세션 관찰 (observations/{thread_ts}.json): inject 플래그 있을 때만 주입
- 채널 관찰 (channel/{channel_id}/): 관찰 대상 채널에서 멘션될 때 주입
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from seosoyoung_plugins.memory.store import MemoryStore
from seosoyoung_plugins.utils.token_counter import TokenCounter
from seosoyoung_plugins.utils.message_formatter import format_slack_message

if TYPE_CHECKING:
    from seosoyoung_plugins.channel_observer.store import ChannelStore

logger = logging.getLogger(__name__)


@dataclass
class InjectionResult:
    """주입 결과 -- 디버그 로그용 정보를 포함"""

    prompt: str | None
    persistent_tokens: int = 0
    session_tokens: int = 0
    persistent_content: str = ""
    session_content: str = ""
    channel_digest_tokens: int = 0
    channel_buffer_tokens: int = 0
    new_observation_tokens: int = 0
    new_observation_content: str = ""


# -- 항목 렌더링 --


def render_observation_items(items: list[dict], now: datetime | None = None) -> str:
    """관찰 항목 리스트를 사람이 읽을 수 있는 텍스트로 렌더링합니다."""
    if not items:
        return ""

    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    lines: list[str] = []
    current_date = None

    for item in items:
        session_date = item.get("session_date", "")
        if session_date != current_date:
            current_date = session_date
            relative = _relative_time_str(session_date, now) if session_date else ""
            if lines:
                lines.append("")  # 섹션 사이 빈 줄
            if relative:
                lines.append(f"## [{session_date}] ({relative})")
            elif session_date:
                lines.append(f"## [{session_date}]")
            lines.append("")

        priority = item.get("priority", "\U0001f7e2")
        content = item.get("content", "")
        lines.append(f"{priority} {content}")

    return "\n".join(lines)


def render_persistent_items(items: list[dict]) -> str:
    """장기 기억 항목 리스트를 텍스트로 렌더링합니다."""
    if not items:
        return ""
    lines = []
    for item in items:
        priority = item.get("priority", "\U0001f7e2")
        content = item.get("content", "")
        lines.append(f"{priority} {content}")
    return "\n".join(lines)


def _relative_time_str(date_str: str, now: datetime) -> str:
    """날짜 문자열에 대한 상대 시간 문자열을 반환합니다."""
    try:
        obs_date = datetime.strptime(date_str, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
        delta = now - obs_date
        days = delta.days

        if days == 0:
            return "오늘"
        elif days == 1:
            return "어제"
        elif days < 7:
            return f"{days}일 전"
        elif days < 30:
            return f"{days // 7}주 전"
        elif days < 365:
            return f"{days // 30}개월 전"
        else:
            return f"{days // 365}년 전"
    except ValueError:
        return ""


# -- 항목 최적화 --


def optimize_items_for_context(
    items: list[dict], max_tokens: int = 30000
) -> list[dict]:
    """관찰 항목을 컨텍스트 주입에 최적화합니다.

    토큰 수 초과 시 오래된 낮은 우선순위 항목부터 제거합니다.
    """
    counter = TokenCounter()
    rendered = render_observation_items(items)
    token_count = counter.count_string(rendered)

    if token_count <= max_tokens:
        return items

    # 우선순위 가중치 (낮을수록 먼저 제거)
    priority_weight = {"\U0001f7e2": 0, "\U0001f7e1": 1, "\U0001f534": 2}

    # 제거 순서: 낮은 우선순위 + 오래된 것부터
    sorted_items = sorted(
        enumerate(items),
        key=lambda x: (
            priority_weight.get(x[1].get("priority", "\U0001f7e2"), 0),
            x[1].get("session_date", ""),
        ),
    )

    remove_indices: set[int] = set()
    for idx, _item in sorted_items:
        remove_indices.add(idx)
        remaining = [it for i, it in enumerate(items) if i not in remove_indices]
        rendered = render_observation_items(remaining)
        if counter.count_string(rendered) <= max_tokens:
            return remaining

    return []


# -- 하위 호환 함수 --


def add_relative_time(observations: str, now: datetime | None = None) -> str:
    """[하위 호환] 텍스트 관찰 로그의 날짜 헤더에 상대 시간 주석을 추가합니다.

    ## [2026-02-10] -> ## [2026-02-10] (3일 전)
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    def replace_date_header(match: re.Match) -> str:
        date_str = match.group(1)
        relative = _relative_time_str(date_str, now)
        if relative:
            return f"## [{date_str}] ({relative})"
        return match.group(0)

    return re.sub(r"## \[(\d{4}-\d{2}-\d{2})\]", replace_date_header, observations)


def optimize_for_context(
    observations: str, max_tokens: int = 30000
) -> str:
    """[하위 호환] 텍스트 관찰 로그를 컨텍스트 주입에 최적화합니다."""
    counter = TokenCounter()
    token_count = counter.count_string(observations)

    if token_count <= max_tokens:
        return observations

    # 섹션 단위로 분리 (## [날짜] 기준)
    sections = re.split(r"(?=^## \[)", observations, flags=re.MULTILINE)
    sections = [s for s in sections if s.strip()]

    # 최신 섹션부터 역순으로 추가
    result_sections = []
    current_tokens = 0
    for section in reversed(sections):
        section_tokens = counter.count_string(section)
        if current_tokens + section_tokens > max_tokens:
            break
        result_sections.insert(0, section)
        current_tokens += section_tokens

    if not result_sections:
        low, high = 0, len(observations)
        while low < high:
            mid = (low + high + 1) // 2
            if counter.count_string(observations[-mid:]) <= max_tokens:
                low = mid
            else:
                high = mid - 1
        return observations[-low:] if low > 0 else observations[:1000]

    return "".join(result_sections)


# -- 컨텍스트 빌더 --


class ContextBuilder:
    """장기 기억 + 세션 관찰 로그 + 채널 관찰을 시스템 프롬프트로 변환"""

    def __init__(
        self,
        store: MemoryStore,
        channel_store: Optional["ChannelStore"] = None,
    ):
        self.store = store
        self.channel_store = channel_store
        self._counter = TokenCounter()

    def _build_channel_observation(
        self,
        channel_id: str,
        thread_ts: Optional[str] = None,
    ) -> tuple[str, int, int]:
        """채널 관찰 컨텍스트를 XML 문자열로 빌드합니다.

        Returns:
            (xml_string, digest_tokens, buffer_tokens)
        """
        if not self.channel_store or not channel_id:
            return "", 0, 0

        digest_tokens = 0
        buffer_tokens = 0
        sections = []

        # digest
        digest_data = self.channel_store.get_digest(channel_id)
        if digest_data and digest_data["content"].strip():
            digest_content = digest_data["content"]
            digest_tokens = self._counter.count_string(digest_content)
            sections.append(f"<digest>\n{digest_content}\n</digest>")

        # channel buffer (미소화 채널 루트 메시지)
        # XML 블록이 이미 채널 컨텍스트를 제공하므로 include_meta=False
        channel_messages = self.channel_store.load_channel_buffer(channel_id)
        if channel_messages:
            lines = [
                format_slack_message(m, channel=channel_id, include_meta=False)
                for m in channel_messages
            ]
            buf_text = "\n".join(lines)
            buffer_tokens += self._counter.count_string(buf_text)
            sections.append(f"<recent-channel>\n{buf_text}\n</recent-channel>")

        # thread buffer (현재 스레드만)
        if thread_ts:
            thread_messages = self.channel_store.load_thread_buffer(
                channel_id, thread_ts
            )
            if thread_messages:
                lines = [
                    format_slack_message(m, channel=channel_id, include_meta=False)
                    for m in thread_messages
                ]
                buf_text = "\n".join(lines)
                buffer_tokens += self._counter.count_string(buf_text)
                sections.append(
                    f'<recent-thread thread="{thread_ts}">\n{buf_text}\n</recent-thread>'
                )

        if not sections:
            return "", 0, 0

        inner = "\n\n".join(sections)
        xml = f'<channel-observation channel="{channel_id}">\n{inner}\n</channel-observation>'
        return xml, digest_tokens, buffer_tokens

    def build_memory_prompt(
        self,
        thread_ts: str,
        max_tokens: int = 30000,
        include_persistent: bool = False,
        include_session: bool = True,
        include_channel_observation: bool = False,
        channel_id: Optional[str] = None,
        include_new_observations: bool = False,
    ) -> InjectionResult:
        """장기 기억, 세션 관찰, 채널 관찰, 새 관찰을 합쳐서 시스템 프롬프트로 변환합니다.

        주입 순서: 장기 기억 -> 새 관찰 -> 세션 관찰 -> 채널 관찰
        """
        parts = []
        persistent_tokens = 0
        session_tokens = 0
        persistent_content = ""
        session_content = ""
        channel_digest_tokens = 0
        channel_buffer_tokens = 0
        new_observation_tokens = 0
        new_observation_content = ""

        # 1. 장기 기억 (persistent/recent.json)
        if include_persistent:
            persistent_data = self.store.get_persistent()
            if persistent_data and persistent_data["content"]:
                items = persistent_data["content"]
                content = render_persistent_items(items)
                if content.strip():
                    persistent_tokens = self._counter.count_string(content)
                    persistent_content = content
                    parts.append(
                        "<long-term-memory>\n"
                        "다음은 과거 대화들에서 축적한 장기 기억입니다.\n"
                        "응답할 때 이 기억을 자연스럽게 활용하세요.\n\n"
                        f"{content}\n"
                        "</long-term-memory>"
                    )

        # 2. 새 관찰 (현재 세션의 이전 턴에서 새로 추가된 관찰 diff)
        if include_new_observations:
            new_obs_items = self.store.get_new_observations(thread_ts)
            if new_obs_items:
                observations_text = render_observation_items(new_obs_items)
                if observations_text.strip():
                    new_observation_tokens = self._counter.count_string(
                        observations_text
                    )
                    new_observation_content = observations_text
                    parts.append(
                        "<new-observations>\n"
                        "이전 턴의 대화에서 새롭게 관찰된 사실입니다.\n\n"
                        f"{observations_text}\n"
                        "</new-observations>"
                    )
                    self.store.clear_new_observations(thread_ts)

        # 3. 세션 관찰 (observations/{thread_ts}.json)
        if include_session:
            record = self.store.get_record(thread_ts)
            if record and record.observations:
                optimized_items = optimize_items_for_context(
                    record.observations, max_tokens
                )
                observations_text = render_observation_items(optimized_items)
                if observations_text.strip():
                    session_tokens = self._counter.count_string(observations_text)
                    session_content = observations_text
                    parts.append(
                        "<observational-memory>\n"
                        "다음은 이 세션의 최근 대화에서 관찰한 내용입니다.\n\n"
                        f"{observations_text}\n"
                        "</observational-memory>"
                    )

        # 4. 채널 관찰 (channel/{channel_id}/)
        if include_channel_observation and channel_id:
            ch_xml, ch_digest_tok, ch_buf_tok = self._build_channel_observation(
                channel_id,
                thread_ts=thread_ts,
            )
            if ch_xml:
                channel_digest_tokens = ch_digest_tok
                channel_buffer_tokens = ch_buf_tok
                parts.append(ch_xml)

        prompt = "\n\n".join(parts) if parts else None

        return InjectionResult(
            prompt=prompt,
            persistent_tokens=persistent_tokens,
            session_tokens=session_tokens,
            persistent_content=persistent_content,
            session_content=session_content,
            channel_digest_tokens=channel_digest_tokens,
            channel_buffer_tokens=channel_buffer_tokens,
            new_observation_tokens=new_observation_tokens,
            new_observation_content=new_observation_content,
        )
