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
    InterventionPrepServices,
    build_intervention_prep,
    run_and_name_session,
)


KST = ZoneInfo("Asia/Seoul")


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
                "keywords": ["GPU 품절", "추론 비용", "Astra 구독"],
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
        message_timestamps=["1001.000001", "1002.000001"],
        now=datetime(2026, 9, 13, 13, 24, 1, tzinfo=KST),
    )

    assert prep is not None
    assert llm_call.await_count == 1
    assert prep.slug == "20260913-132401"
    assert prep.suggested_title == "🗯️ GPU 품절과 추론 비용 개입"
    assert prep.keywords == ["GPU 품절", "추론 비용", "Astra 구독"]
    assert prep.threads_file == (tmp_path / "threads-C1-20260913-132401.json").resolve()
    assert prep.context_item["key"] == "intervene_prep"
    assert str(prep.threads_file) in prep.context_item["content"]
    assert '["GPU 품절", "추론 비용", "Astra 구독"]' in prep.context_item["content"]

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
async def test_build_prep_missing_message_summary_is_fail_open(tmp_path):
    llm_call = AsyncMock(
        return_value=json.dumps(
            {
                "threads": [{"ts": "1", "summary": "U1: 첫 메시지"}],
                "suggested_title": "대화",
                "keywords": ["첫째", "둘째", "대화"],
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

    result = await run_and_name_session(
        slow_run(),
        thread_ts="1000.1",
        suggested_title="🗯️ 조기 제목 개입",
        get_session_id=getter,
        set_session_name=set_name,
        poll_interval=0.01,
    )

    assert result.session_id == "session-early"
    assert named_before_finish == [True]


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
            suggested_title="🗯️ 취소 경로 개입",
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
async def test_execute_injects_prep_and_knowledge_then_names_session(
    mock_plugin_sdk, tmp_path, monkeypatch,
):
    from seosoyoung_plugins.channel_observer import pipeline

    store = MagicMock()
    store.get_digest.return_value = None
    store.load_judged.return_value = []
    store.append_judged = MagicMock()
    messages = [
        {"ts": "1100.0001", "user": "U1", "text": "GPU 품절"},
        {"ts": "1100.0002", "user": "U2", "text": "추론 비용"},
    ]
    prep_llm = AsyncMock(
        return_value=json.dumps(
            {
                "threads": [
                    {"ts": "1100.0001", "summary": "U1: GPU 품절을 공유"},
                    {"ts": "1100.0002", "summary": "U2: 추론 비용을 질문"},
                ],
                "suggested_title": "GPU와 추론 비용",
                "keywords": ["GPU", "추론", "비용"],
            },
            ensure_ascii=False,
        )
    )
    search_cards = AsyncMock(
        return_value=[
            {
                "card_id": "card-1",
                "title": "추론 비용 메모",
                "snippet": "비용 비교",
                "node_path": ["서소영의 서재", "발행 이력"],
            }
        ]
    )
    set_session_name = AsyncMock()
    services = InterventionPrepServices(
        output_dir=tmp_path,
        search_cards=search_cards,
        set_session_name=set_session_name,
    )
    mock_plugin_sdk["slack"].get_channel_history = AsyncMock(return_value=[])
    monkeypatch.setattr(
        pipeline,
        "_fetch_recent_context_bundle",
        AsyncMock(
            return_value=(
                "[C_TEST:1100.0001] <U1>: GPU 품절\n"
                "[C_TEST:1100.0002] <U2>: 추론 비용",
                ["1100.0001", "1100.0002"],
            )
        ),
    )
    mock_plugin_sdk["soulstream"].run = AsyncMock(
        return_value=RunResult(
            ok=True,
            status=RunStatus.COMPLETED,
            session_id="session-123",
            utterances=["제가 비용 자료를 볼게요."],
        )
    )
    monkeypatch.setattr(pipeline, "build_remiel_context_item", AsyncMock(return_value=None))

    await pipeline._execute_intervene(
        store=store,
        channel_id="C_TEST",
        action=InterventionAction(type="message", target="channel", content="reason"),
        pending_messages=messages,
        llm_call=prep_llm,
        prep_services=services,
    )

    context = mock_plugin_sdk["soulstream"].run.call_args.kwargs["context"]
    assert [item["key"] for item in context] == [
        "thread_context",
        "intervene_prep",
        "knowledge_context",
    ]
    set_session_name.assert_awaited_once_with(
        "session-123", "🗯️ GPU와 추론 비용 개입"
    )
    assert prep_llm.await_count == 1


@pytest.mark.asyncio
async def test_execute_atom_and_session_name_failures_do_not_block_intervention(
    mock_plugin_sdk, tmp_path, monkeypatch,
):
    from seosoyoung_plugins.channel_observer import pipeline

    store = MagicMock()
    store.get_digest.return_value = None
    store.load_judged.return_value = []
    store.append_judged = MagicMock()
    prep_llm = AsyncMock(
        return_value=json.dumps(
            {
                "threads": [{"ts": "1", "summary": "U1: 인사"}],
                "suggested_title": "인사",
                "keywords": ["인사", "채널", "대화"],
            },
            ensure_ascii=False,
        )
    )
    services = InterventionPrepServices(
        output_dir=tmp_path,
        search_cards=AsyncMock(side_effect=RuntimeError("atom unavailable")),
        set_session_name=AsyncMock(side_effect=RuntimeError("mcp unavailable")),
    )
    mock_plugin_sdk["slack"].get_channel_history = AsyncMock(return_value=[])
    monkeypatch.setattr(
        pipeline,
        "_fetch_recent_context_bundle",
        AsyncMock(return_value=("[C_TEST:1] <U1>: hello", ["1"])),
    )
    mock_plugin_sdk["soulstream"].run = AsyncMock(
        return_value=RunResult(
            ok=True,
            status=RunStatus.COMPLETED,
            session_id="session-123",
            utterances=["기존 개입은 계속됩니다."],
        )
    )

    await pipeline._execute_intervene(
        store=store,
        channel_id="C_TEST",
        action=InterventionAction(type="message", target="channel", content="reason"),
        pending_messages=[{"ts": "1", "user": "U1", "text": "hello"}],
        llm_call=prep_llm,
        prep_services=services,
    )

    context = mock_plugin_sdk["soulstream"].run.call_args.kwargs["context"]
    assert [item["key"] for item in context] == [
        "thread_context",
        "intervene_prep",
    ]
    services.set_session_name.assert_awaited_once()
    mock_plugin_sdk["slack"].send_message.assert_awaited()


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
