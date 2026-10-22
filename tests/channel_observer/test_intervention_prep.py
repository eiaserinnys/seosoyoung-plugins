import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from seosoyoung.plugin_sdk.soulstream import RunResult, RunStatus
from seosoyoung_plugins.channel_observer.intervention import InterventionAction
from seosoyoung_plugins.channel_observer.intervention_prep import (
    InterventionPrep,
    build_intervention_prep,
    run_and_name_session,
)


KST = ZoneInfo("Asia/Seoul")


async def _resolved_prep(title: str) -> InterventionPrep:
    return InterventionPrep(
        slug="slug",
        threads_file=Path("/tmp/threads.json"),
        suggested_title=title,
    )


@pytest.mark.asyncio
async def test_build_prep_calls_llm_once_and_writes_contract_file(tmp_path):
    llm_call = AsyncMock(
        return_value=json.dumps(
            {
                "threads": [
                    {"ts": "1001.000001", "summary": "주복: GPU 품절을 공유"},
                    {"ts": "1002.000001", "summary": "서하: 추론 비용을 질문"},
                ],
                "suggested_title": "GPU 품절과 추론 비용",
            },
            ensure_ascii=False,
        )
    )
    thread_context = "[C1:1001.000001] <주복>: GPU가 품절이래\n[C1:1002.000001] <서하>: 추론 비용은?"

    prep = await build_intervention_prep(
        llm_call=llm_call,
        channel_id="C1",
        thread_context=thread_context,
        output_dir=tmp_path,
        slug="20260913-132401",
        message_timestamps=["1001.000001", "1002.000001"],
        now=datetime(2026, 9, 13, 13, 24, 1, tzinfo=KST),
    )

    assert prep is not None
    assert llm_call.await_count == 1
    assert prep.slug == "20260913-132401"
    assert prep.suggested_title == "🗯️ GPU 품절과 추론 비용 개입"
    assert prep.threads_file == (tmp_path / "threads-C1-20260913-132401.json").resolve()
    assert not hasattr(prep, "context_item")
    assert "keywords" not in llm_call.await_args.args[0]

    payload = json.loads(prep.threads_file.read_text(encoding="utf-8"))
    assert payload == {
        "date": "2026-09-13",
        "threads": [
            {"ts": "1001.000001", "summary": "주복: GPU 품절을 공유"},
            {"ts": "1002.000001", "summary": "서하: 추론 비용을 질문"},
        ],
        "verbatim": thread_context,
    }
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["not-json", "{}", '{"threads": []}'])
async def test_build_prep_malformed_llm_output_is_fail_open(tmp_path, result):
    llm_call = AsyncMock(return_value=result)

    prep = await build_intervention_prep(
        llm_call=llm_call,
        channel_id="C1",
        thread_context="[C1:1] <U1>: hello",
        output_dir=tmp_path,
    )

    assert prep is None
    assert llm_call.await_count == 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_build_prep_empty_llm_content_is_fail_open_with_specific_warning(
    tmp_path, caplog,
):
    llm_call = AsyncMock(return_value="")

    with caplog.at_level(logging.WARNING):
        prep = await build_intervention_prep(
            llm_call=llm_call,
            channel_id="C1",
            thread_context="[C1:1] <U1>: hello",
            output_dir=tmp_path,
        )

    assert prep is None
    assert llm_call.await_count == 1
    assert any("prep output was empty" in rec.message for rec in caplog.records)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_build_prep_missing_message_summary_is_fail_open(tmp_path):
    llm_call = AsyncMock(
        return_value=json.dumps(
            {
                "threads": [{"ts": "1", "summary": "U1: 첫 메시지"}],
                "suggested_title": "대화",
            },
            ensure_ascii=False,
        )
    )

    prep = await build_intervention_prep(
        llm_call=llm_call,
        channel_id="C1",
        thread_context="[C1:1] <U1>: first\n[C1:2] <U2>: second",
        output_dir=tmp_path,
        message_timestamps=["1", "2"],
    )

    assert prep is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_session_is_named_before_long_running_agent_finishes():
    run_finished = False
    getter = MagicMock(side_effect=[None, "session-early"])
    named_before_finish = []

    async def slow_run():
        nonlocal run_finished
        await asyncio.sleep(0.08)
        run_finished = True
        return RunResult(
            ok=True,
            status=RunStatus.COMPLETED,
            session_id="session-early",
        )

    async def set_name(_session_id, _name):
        named_before_finish.append(not run_finished)

    async def prep():
        return InterventionPrep(
            slug="slug",
            threads_file=Path("/tmp/threads.json"),
            suggested_title="🗯️ 조기 제목 개입",
        )

    result = await run_and_name_session(
        slow_run(),
        thread_ts="1000.1",
        prep_task=asyncio.create_task(prep()),
        get_session_id=getter,
        set_session_name=set_name,
        poll_interval=0.01,
    )

    assert result.session_id == "session-early"
    assert named_before_finish == [True]


@pytest.mark.asyncio
async def test_run_result_returns_before_slow_prep_finishes():
    """prep이 세션보다 늦어도 게시를 막지 않고, prep 완료 뒤 백그라운드로 이름을 붙인다."""
    from seosoyoung_plugins.channel_observer import intervene_memory

    release = asyncio.Event()
    named = []

    async def slow_prep():
        await release.wait()
        return await _resolved_prep("🗯️ 늦은 제목 개입")

    async def set_name(session_id, name):
        named.append((session_id, name))

    result = await run_and_name_session(
        asyncio.sleep(
            0,
            result=RunResult(
                ok=True,
                status=RunStatus.COMPLETED,
                session_id="session-slow",
            ),
        ),
        thread_ts="1000.1",
        prep_task=asyncio.create_task(slow_prep()),
        get_session_id=lambda _thread_ts: "session-slow",
        set_session_name=set_name,
        poll_interval=0.01,
    )

    assert result.session_id == "session-slow"
    assert named == []

    release.set()
    while intervene_memory._background_tasks:
        await asyncio.gather(*list(intervene_memory._background_tasks))

    assert named == [("session-slow", "🗯️ 늦은 제목 개입")]


@pytest.mark.asyncio
async def test_session_run_is_cancelled_with_naming_wrapper():
    run_cancelled = asyncio.Event()

    async def slow_run():
        try:
            await asyncio.Event().wait()
        finally:
            run_cancelled.set()

    wrapper = asyncio.create_task(
        run_and_name_session(
            slow_run(),
            thread_ts="1000.1",
            prep_task=asyncio.create_task(
                _resolved_prep("🗯️ 취소 경로 개입")
            ),
            get_session_id=lambda _thread_ts: None,
            set_session_name=AsyncMock(),
            poll_interval=0.01,
        )
    )
    await asyncio.sleep(0)
    wrapper.cancel()

    with pytest.raises(asyncio.CancelledError):
        await wrapper
    assert run_cancelled.is_set()


@pytest.mark.asyncio
async def test_build_prep_missing_output_dir_warns_instead_of_silent_skip(caplog):
    """SCRATCH_WORKSPACE_DIR 누락(output_dir=None)은 fail-open이되 경고를 남긴다 (2026-09-14)."""
    calls = []

    async def llm_call(system, user):
        calls.append((system, user))
        return "{}"

    with caplog.at_level(logging.WARNING):
        result = await build_intervention_prep(
            llm_call=llm_call,
            channel_id="C123",
            thread_context="[C123:1.0] <a>: hi",
            output_dir=None,
        )

    assert result is None
    assert calls == []
    assert any(
        "intervene prep skip" in rec.message and "output_dir=missing" in rec.message
        for rec in caplog.records
    )
