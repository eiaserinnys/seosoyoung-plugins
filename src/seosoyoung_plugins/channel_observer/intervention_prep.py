"""Prepare low-cost context artifacts before a channel intervention run."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from seosoyoung_plugins.channel_observer.intervene_memory import InterveneMemory
from seosoyoung_plugins.channel_observer.mcp_http import SetSessionName


logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

LlmCall = Callable[[str, str], Awaitable[str]]

PREP_SYSTEM_PROMPT = """채널 개입 준비 데이터를 JSON 하나로 만든다.
출력은 설명이나 코드 펜스 없이 다음 키만 포함한 JSON object다.
- threads: [{"ts": "원문 timestamp", "summary": "40자 이내 화자: 행위"}]
- suggested_title: 핵심 화제 20자 이내. 이모지와 '개입' 접미사는 쓰지 않는다.
threads에는 입력의 모든 메시지를 빠짐없이 같은 순서로 넣는다.
원문에 없는 사실을 추가하지 않는다."""


@dataclass(frozen=True)
class InterventionPrepServices:
    output_dir: Path | None = None
    set_session_name: SetSessionName | None = None
    memory: InterveneMemory | None = None


@dataclass(frozen=True)
class InterventionPrep:
    slug: str
    threads_file: Path
    suggested_title: str


def default_prep_output_dir() -> Path | None:
    workspace = os.environ.get("SCRATCH_WORKSPACE_DIR", "").strip()
    return Path(workspace) / ".local" / "tmp" if workspace else None


def _extract_object(raw: str) -> dict[str, Any]:
    if not raw.strip():
        raise ValueError("prep output was empty")
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("prep output had no JSON object")
    parsed = json.loads(raw[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("prep output was not a JSON object")
    return parsed


def _normalize_title(value: Any) -> str:
    title = " ".join(str(value or "").split()).strip()
    if title.startswith("🗯️"):
        title = title.removeprefix("🗯️").strip()
    if title.endswith("개입"):
        title = title.removesuffix("개입").strip()
    if not title:
        raise ValueError("suggested_title was empty")
    return f"🗯️ {title[:20].rstrip()} 개입"


def _normalize_threads(
    value: Any,
    expected_timestamps: list[str] | None,
) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ValueError("threads was empty")
    threads: list[dict[str, str]] = []
    seen_timestamps: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        ts = str(item.get("ts") or "").strip()
        summary = " ".join(str(item.get("summary") or "").split()).strip()
        if ts and summary:
            if ts in seen_timestamps:
                raise ValueError(f"threads contained duplicate ts={ts}")
            seen_timestamps.add(ts)
            threads.append({"ts": ts, "summary": summary[:40].rstrip()})
    if not threads:
        raise ValueError("threads had no valid entries")
    if expected_timestamps is not None:
        by_timestamp = {thread["ts"]: thread for thread in threads}
        if set(by_timestamp) != set(expected_timestamps):
            raise ValueError("threads did not cover the input messages exactly")
        threads = [by_timestamp[timestamp] for timestamp in expected_timestamps]
    return threads


def _write_threads_file(
    path: Path,
    *,
    date: str,
    threads: list[dict[str, str]],
    verbatim: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(
                {"date": date, "threads": threads, "verbatim": verbatim},
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


async def build_intervention_prep(
    *,
    llm_call: LlmCall | None,
    channel_id: str,
    thread_context: str,
    output_dir: Path | None,
    slug: str | None = None,
    message_timestamps: list[str] | None = None,
    now: datetime | None = None,
) -> InterventionPrep | None:
    """Make all LLM-derived prep in exactly one call, or fail open."""
    if llm_call is None or output_dir is None or not thread_context.strip():
        # fail-open이되 *조용히* 넘기지 않는다 — 준비물이 빠지면 개입 세션이 fallback
        # 경로(threads-file 자작·제목 미지정)로 4분 넘게 돌던 사고(2026-09-14)의 원인이
        # 봇 env의 SCRATCH_WORKSPACE_DIR 누락이었는데 로그에 흔적이 없었다.
        logger.warning(
            "intervene prep skip (%s): llm_call=%s output_dir=%s thread_context=%s "
            "— SCRATCH_WORKSPACE_DIR / soulstream 설정을 확인하십시오",
            channel_id,
            "ok" if llm_call is not None else "missing",
            str(output_dir) if output_dir is not None else "missing",
            "ok" if thread_context.strip() else "empty",
        )
        return None
    try:
        raw = await llm_call(
            PREP_SYSTEM_PROMPT,
            f"channel_id: {channel_id}\n\n{thread_context}",
        )
        parsed = _extract_object(raw)
        threads = _normalize_threads(parsed.get("threads"), message_timestamps)
        suggested_title = _normalize_title(parsed.get("suggested_title"))

        timestamp = now or datetime.now(KST)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=KST)
        timestamp = timestamp.astimezone(KST)
        if not slug:
            slug = timestamp.strftime("%Y%m%d-%H%M%S")
        safe_channel = "".join(
            char if char.isalnum() or char in "-_" else "_" for char in channel_id
        )
        threads_file = (
            output_dir / f"threads-{safe_channel}-{slug}.json"
        ).resolve()
        _write_threads_file(
            threads_file,
            date=timestamp.strftime("%Y-%m-%d"),
            threads=threads,
            verbatim=thread_context,
        )
        return InterventionPrep(
            slug=slug,
            threads_file=threads_file,
            suggested_title=suggested_title,
        )
    except Exception as exc:
        logger.warning("intervene prep 실패 (%s): %s", channel_id, exc)
        return None


async def run_and_name_session(
    run_awaitable: Awaitable[Any],
    *,
    thread_ts: str,
    prep_task: Awaitable[InterventionPrep | None] | None,
    get_session_id: Callable[[str], str | None],
    set_session_name: SetSessionName | None,
    poll_interval: float = 0.05,
) -> Any:
    """Name the session as soon as the backend registers its SSE session ID.

    ``prep_task`` is started by the caller and may still be running. The name
    is applied only after it resolves, so prep never extends the critical path
    of the run itself.
    """
    run_task = asyncio.create_task(run_awaitable)
    try:
        if prep_task is None or set_session_name is None:
            return await run_task

        session_id: str | None = None
        result: Any | None = None
        while not run_task.done():
            try:
                candidate = get_session_id(thread_ts)
                session_id = (
                    candidate.strip()
                    if isinstance(candidate, str) and candidate.strip()
                    else None
                )
            except Exception:
                # Some test or startup backends cannot expose the session manager.
                # The completed RunResult remains a fallback source below.
                session_id = None
            if session_id:
                break
            await asyncio.sleep(poll_interval)

        if not session_id:
            result = await run_task
            session_id = getattr(result, "session_id", None)

        if session_id:
            prep = await prep_task
            if prep is not None:
                try:
                    await set_session_name(session_id, prep.suggested_title)
                except Exception as exc:
                    logger.warning("개입 세션 이름 설정 실패 (%s): %s", session_id, exc)
        return result if result is not None else await run_task
    except asyncio.CancelledError:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        raise
