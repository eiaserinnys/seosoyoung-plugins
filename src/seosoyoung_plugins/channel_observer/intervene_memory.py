"""채널 개입 기억 선주입·기록·보관을 개입 스킬 스크립트에 위임한다.

개입 세션이 직접 돌리던 ``channel-intervene/scripts`` 호출을 봇 프로세스가
대신 수행한다. 세션은 도구 없이 발화만 만들고, 봇은 세션 전에 채널 기억과
참여자 사실을 context item으로 주입하며 게시 뒤에 발화를 기록한다.

스크립트 경로 정본은 ``seosoyoung-common/skills/channel-intervene/scripts``다.
플러그인 설정 ``intervene_scripts_dir``이 있으면 그것을, 없으면
``AGENT_COMMON_FILES_DIR`` 환경변수를 사용한다. 둘 다 없으면 플러그인 로드
시점에 WARNING 한 번을 남기고 선주입·기록·보관을 모두 건너뛴다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any

logger = logging.getLogger(__name__)

CHANNEL_TIMEOUT = 20.0
MEMBER_TIMEOUT = 20.0
RECORD_TIMEOUT = 60.0
ARCHIVE_TIMEOUT = 60.0

CHANNEL_SCRIPT = "load_channel.py"
MEMBER_SCRIPT = "load_member.py"
RECORD_SCRIPT = "record_utterance.py"
ARCHIVE_SCRIPT = "load_thread_context.py"

_RECENT_UTTERANCE_LIMIT = 10
_KEY_FACT_LIMIT = 6
_KEY_FACT_CHAR_LIMIT = 120
_TOPIC_LIMIT = 5
_STDERR_LOG_LIMIT = 300

_background_tasks: set[asyncio.Task] = set()


def _safe_segment(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in "-_" else "_" for char in value
    )


def spawn_background(coro: Any) -> asyncio.Task:
    """백그라운드 task를 모듈 수준 set에 보관하고 예외를 WARNING으로 남긴다."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _done(completed: asyncio.Task) -> None:
        _background_tasks.discard(completed)
        if completed.cancelled():
            return
        exc = completed.exception()
        if exc is not None:
            logger.warning("intervene memory background task failed: %s", exc)

    task.add_done_callback(_done)
    return task


async def drain_background(timeout: float = 120.0) -> None:
    """현재 루프에 속한 백그라운드 작업이 끝날 때까지 기다린다.

    개입 파이프라인은 채널마다 새 이벤트 루프를 만들고 ``loop.close()``로
    끝낸다. 그 전에 기록·보관·늦은 이름 붙이기를 기다리지 않으면 task가
    루프와 함께 사라진다. 다른 스레드의 루프에 속한 task는 건드리지 않는다.
    """
    loop = asyncio.get_running_loop()
    tasks = [
        task
        for task in _background_tasks
        if not task.done() and task.get_loop() is loop
    ]
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        logger.warning(
            "intervene memory background drain timeout (%ss): %d remaining",
            timeout,
            len(pending),
        )


def _first_tag(text: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    if match is None:
        return None
    value = match.group(1).strip()
    return value or None


def parse_record_tags(
    output: str | None,
) -> tuple[list[str], str | None, str | None]:
    """세션 최종 응답에서 기록용 태그를 읽는다 (비탐욕 첫 매치)."""
    text = output or ""
    raw_topics = _first_tag(text, "topics")
    topics: list[str] = []
    if raw_topics:
        topics = [
            topic.strip()
            for topic in raw_topics.split(",")
            if topic.strip()
        ][:_TOPIC_LIMIT]
    return (
        topics,
        _first_tag(text, "summary_line"),
        _first_tag(text, "compressed_summary"),
    )


def render_channel_memory_item(memory: dict[str, Any]) -> dict[str, str]:
    """``load_channel.py`` 출력 dict를 채널 기억 context item으로 만든다."""
    lines = [
        "## 채널 기억",
        "### 지난 줄거리",
        str(memory.get("previousSummary") or "").strip() or "(없음)",
        "### 최근 내 발화 (최신순)",
    ]
    for utterance in (memory.get("recentUtterances") or [])[
        :_RECENT_UTTERANCE_LIMIT
    ]:
        topics = ", ".join(utterance.get("topics") or [])
        lines.append(f"- {str(utterance.get('title') or '').strip()} [{topics}]")

    topics_to_avoid = memory.get("topicsToAvoid") or []
    lines.append("### 피할 화제")
    lines.append(
        ", ".join(str(topic) for topic in topics_to_avoid) if topics_to_avoid else "(없음)"
    )

    if memory.get("needsCompression"):
        lines.extend(
            [
                "### 압축 필요",
                "지난 줄거리와 아래 오래된 발화를 800~1500자로 압축해 "
                "응답에 <compressed_summary>로 함께 낸다.",
            ]
        )
        for oldest in memory.get("oldestTen") or []:
            lines.append(
                f"- {str(oldest.get('title') or '').strip()}: "
                f"{str(oldest.get('content') or '').strip()}"
            )

    return {
        "key": "channel_memory",
        "label": "채널 기억",
        "content": "\n".join(lines),
    }


def render_member_facts_item(
    payload: dict[str, Any] | None,
) -> dict[str, str] | None:
    """``load_member.py`` 출력 dict를 대화 참여자 context item으로 만든다."""
    members = (payload or {}).get("members") or []
    if not members:
        return None

    lines = ["## 대화 참여자"]
    for member in members:
        slack_id = str(member.get("slackId") or "").strip()
        if member.get("cacheStatus") not in {"hit", "miss-refreshed"}:
            lines.append(f"- {slack_id}: (정보 없음)")
            continue
        display_name = str(member.get("displayName") or "").strip() or slack_id
        qualifier = f"{slack_id}, 봇" if member.get("isBot") else slack_id
        facts = " / ".join(
            str(fact).strip()[:_KEY_FACT_CHAR_LIMIT]
            for fact in (member.get("keyFacts") or [])[:_KEY_FACT_LIMIT]
        )
        lines.append(f"- {display_name} ({qualifier}): {facts}")

    return {
        "key": "member_facts",
        "label": "대화 참여자",
        "content": "\n".join(lines),
    }


class InterveneMemory:
    """개입 스킬 스크립트 실행기. 스크립트 경로가 없으면 스스로 비활성화된다."""

    def __init__(self, scripts_dir: Path | None = None) -> None:
        self._scripts_dir = scripts_dir

    @property
    def enabled(self) -> bool:
        return self._scripts_dir is not None

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "InterveneMemory":
        configured = str(config.get("intervene_scripts_dir") or "").strip()
        if configured:
            candidate: Path | None = Path(configured).expanduser()
        else:
            common = os.environ.get("AGENT_COMMON_FILES_DIR", "").strip()
            candidate = (
                Path(common) / "skills" / "channel-intervene" / "scripts"
                if common
                else None
            )
        if candidate is None or not candidate.is_dir():
            logger.warning(
                "intervene memory disabled: scripts directory not found "
                "(intervene_scripts_dir=%s, AGENT_COMMON_FILES_DIR=%s)",
                configured or "missing",
                os.environ.get("AGENT_COMMON_FILES_DIR", "").strip() or "missing",
            )
            return cls(None)
        return cls(candidate)

    async def _run_script(
        self,
        script_name: str,
        args: list[str],
        *,
        timeout: float,
    ) -> Any | None:
        if self._scripts_dir is None:
            return None
        script_path = self._scripts_dir / script_name
        if not script_path.is_file():
            logger.warning("intervene memory script missing: %s", script_path)
            return None

        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(script_path),
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            logger.warning("intervene memory script failed (%s): %s", script_name, exc)
            return None

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            logger.warning(
                "intervene memory script timeout (%ss): %s", timeout, script_name,
            )
            return None

        if process.returncode != 0:
            logger.warning(
                "intervene memory script exit %s (%s): %s",
                process.returncode,
                script_name,
                stderr.decode("utf-8", errors="replace")[:_STDERR_LOG_LIMIT],
            )
            return None

        try:
            return json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning(
                "intervene memory script returned bad JSON (%s): %s",
                script_name,
                exc,
            )
            return None

    async def load_channel_memory(self, channel_id: str) -> dict[str, Any] | None:
        data = await self._run_script(
            CHANNEL_SCRIPT, [channel_id], timeout=CHANNEL_TIMEOUT,
        )
        return data if isinstance(data, dict) else None

    async def load_member_facts(
        self,
        channel_id: str,
        slack_ids: list[str],
    ) -> dict[str, Any] | None:
        ids = [slack_id for slack_id in slack_ids if slack_id]
        if not ids:
            return None
        data = await self._run_script(
            MEMBER_SCRIPT, [channel_id, *ids], timeout=MEMBER_TIMEOUT,
        )
        return data if isinstance(data, dict) else None

    async def record_utterance(
        self,
        *,
        channel_id: str,
        body: str,
        slug: str,
        topics: list[str],
        summary_line: str | None,
        compressed_summary: str | None,
        channel_memory: dict[str, Any] | None,
        output_dir: Path | None,
    ) -> bool:
        """게시한 발화를 atom에 기록한다. 실패는 조용히 넘기지 않고 WARNING."""
        if self._scripts_dir is None:
            return False
        if output_dir is None:
            logger.warning(
                "intervene record skip (%s): output_dir missing", channel_id,
            )
            return False

        safe_channel = _safe_segment(channel_id)
        safe_slug = _safe_segment(slug)
        directory = Path(output_dir)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            utterance_file = (
                directory / f"utterance-{safe_channel}-{safe_slug}.txt"
            )
            utterance_file.write_text(body, encoding="utf-8")
        except OSError as exc:
            logger.warning("intervene record file write failed (%s): %s", channel_id, exc)
            return False

        args = [
            "--channel", channel_id,
            "--utterance-file", str(utterance_file.resolve()),
        ]
        if topics:
            args += ["--topics", ",".join(topics)]

        fallback_summary = ""
        for line in body.strip().splitlines():
            fallback_summary = line.strip()[:20]
            break
        summary = (summary_line or "").strip() or fallback_summary
        if summary:
            args += ["--summary-line", summary]

        memory = channel_memory or {}
        if memory.get("needsCompression") and compressed_summary:
            compressed_file = (
                directory / f"compressed-{safe_channel}-{safe_slug}.txt"
            )
            try:
                compressed_file.write_text(compressed_summary, encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    "intervene record compressed write failed (%s): %s",
                    channel_id,
                    exc,
                )
                return False
            args += ["--compressed-summary", str(compressed_file.resolve())]
            deleted_cards = [
                str(item.get("cardId"))
                for item in (memory.get("oldestTen") or [])
                if item.get("cardId")
            ]
            if deleted_cards:
                args += ["--deleted-cards", ",".join(deleted_cards)]

        data = await self._run_script(
            RECORD_SCRIPT, args, timeout=RECORD_TIMEOUT,
        )
        return isinstance(data, dict) and data.get("status") == "recorded"

    async def archive_threads(
        self,
        channel_id: str,
        threads_file: Path,
    ) -> bool:
        if self._scripts_dir is None:
            return False
        data = await self._run_script(
            ARCHIVE_SCRIPT,
            [
                "--channel", channel_id,
                "--threads-file", str(threads_file),
            ],
            timeout=ARCHIVE_TIMEOUT,
        )
        return isinstance(data, dict)
