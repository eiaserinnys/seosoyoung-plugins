import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from seosoyoung.plugin_sdk.soulstream import RunResult, RunStatus
from seosoyoung_plugins.channel_observer.intervention import InterventionAction
from seosoyoung_plugins.channel_observer.intervention_prep import InterventionPrepServices
from seosoyoung_plugins.channel_observer.pipeline import _build_channel_pas_delta, _execute_intervene
from seosoyoung_plugins.channel_observer.store import ChannelStore


@pytest.mark.asyncio
async def test_delta_uses_root_and_thread_read_cursors(tmp_path, mock_plugin_sdk, monkeypatch):
    from seosoyoung_plugins.channel_observer import pipeline

    store = ChannelStore(tmp_path)
    store.upsert_pending("C001", {"ts": "1.0", "user": "U1", "text": "already read"})
    store.upsert_pending("C001", {"ts": "2.0", "user": "U2", "text": "new root"})
    store.upsert_thread_message("C001", "1.0", {"ts": "1.5", "user": "U3", "text": "new reply"})
    store.upsert_thread_message("C001", "2.0", {"ts": "2.1", "user": "U4", "text": "reply"})
    monkeypatch.setattr(pipeline.slack, "get_channel_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(pipeline, "_make_resolver", lambda: None)

    delta = await _build_channel_pas_delta(
        store=store,
        channel_id="C001",
        pas_state={"read_ts": "1.0", "read_thread_ts_map": {"1.0": "1.2"}},
        target_ts="2.1",
        target_message={"ts": "2.1", "thread_ts": "2.0", "user": "U4", "text": "reply"},
        current_thread_ts="2.0",
        pending_messages=[],
        thread_buffers=None,
        bot_user_id="UBOT",
        recent_messages_count=3,
        judgment_reason="asked for help",
        buffer_threshold=1000,
    )

    assert [item["ts"] for item in delta["roots"]] == ["2.0"]
    assert [item["ts"] for item in delta["replies"]["1.0"]] == ["1.5"]
    assert [item["ts"] for item in delta["replies"]["2.0"]] == ["2.1"]
    assert delta["snapshot_ts"] == {"2.0"}
    assert delta["snapshot_thread_msg_ts"] == {"1.0": {"1.5"}, "2.0": {"2.1"}}
    assert "이번 개입 트리거: [C001:2.1]" in delta["text"]
    assert "Jev 판단 사유: asked for help" in delta["text"]
    assert delta["batch_id"].startswith("chan-pas-C001-")


@pytest.mark.asyncio
@pytest.mark.parametrize("compressor_mode", ["none", "failure"])
async def test_delta_keeps_full_timeline_when_compressor_is_unavailable(
    tmp_path, mock_plugin_sdk, monkeypatch, compressor_mode
):
    from seosoyoung_plugins.channel_observer import pipeline

    store = ChannelStore(tmp_path)
    messages = [
        {"ts": f"{index}.0", "user": f"U{index}", "text": f"message-{index}"}
        for index in range(1, 5)
    ]
    for message in messages:
        store.upsert_pending("C001", message)
    monkeypatch.setattr(pipeline.slack, "get_channel_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(pipeline, "_make_resolver", lambda: None)
    compressor = None
    if compressor_mode == "failure":
        compressor = SimpleNamespace(
            compress=AsyncMock(side_effect=RuntimeError("compression unavailable"))
        )

    delta = await _build_channel_pas_delta(
        store=store,
        channel_id="C001",
        pas_state={"read_ts": "", "read_thread_ts_map": {}},
        target_ts="4.0",
        target_message=messages[-1],
        current_thread_ts=None,
        pending_messages=messages,
        thread_buffers=None,
        bot_user_id="UBOT",
        recent_messages_count=2,
        judgment_reason=None,
        buffer_threshold=1,
        compressor=compressor,
    )

    for message in messages:
        assert message["text"] in delta["text"]
    assert "## 이전 대화 요약" not in delta["text"]
    assert "건은 생략했습니다" not in delta["text"]


@pytest.mark.asyncio
async def test_delta_timeline_interleaves_roots_and_thread_replies_by_timestamp(
    tmp_path, mock_plugin_sdk, monkeypatch
):
    from seosoyoung_plugins.channel_observer import pipeline

    store = ChannelStore(tmp_path)
    store.upsert_pending("C001", {"ts": "10.0", "user": "U1", "text": "root before"})
    store.upsert_thread_message(
        "C001", "10.0", {"ts": "11.0", "user": "U2", "text": "reply"}
    )
    store.upsert_pending("C001", {"ts": "12.0", "user": "U3", "text": "root after"})
    monkeypatch.setattr(pipeline.slack, "get_channel_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(pipeline, "_make_resolver", lambda: None)

    delta = await _build_channel_pas_delta(
        store=store,
        channel_id="C001",
        pas_state={"read_ts": "", "read_thread_ts_map": {}},
        target_ts="12.0",
        target_message=None,
        current_thread_ts=None,
        pending_messages=[],
        thread_buffers=None,
        bot_user_id="UBOT",
        recent_messages_count=5,
        judgment_reason=None,
        buffer_threshold=1000,
    )

    before = delta["text"].index("[C001:10.0]")
    reply = delta["text"].index("[C001:11.0]")
    after = delta["text"].index("[C001:12.0]")
    assert before < reply < after
    assert "(스레드 C001:10.0, 원문: root before)" in delta["text"]


@pytest.mark.asyncio
async def test_pas_run_lock_releases_between_interventions(tmp_path, monkeypatch):
    from seosoyoung_plugins.channel_observer import pipeline

    store = ChannelStore(tmp_path)
    calls = []

    async def record_locked_call(**kwargs):
        calls.append(kwargs["channel_id"])

    monkeypatch.setattr(pipeline, "_execute_intervene_locked", record_locked_call)
    invocation = {
        "store": store,
        "channel_id": "C001",
        "action": InterventionAction(type="message", target="channel", content="test"),
        "pending_messages": [],
    }

    await asyncio.wait_for(_execute_intervene(**invocation), timeout=2)
    await asyncio.wait_for(_execute_intervene(**invocation), timeout=2)

    assert calls == ["C001", "C001"]


@pytest.mark.asyncio
async def test_silence_advances_read_watermarks_without_posting(
    tmp_path, mock_plugin_sdk, monkeypatch
):
    from seosoyoung_plugins.channel_observer import pipeline

    store = ChannelStore(tmp_path)
    message = {"ts": "10.2", "user": "U1", "text": "질문"}
    store.upsert_pending("C001", message)
    ensure_pas = AsyncMock(return_value="pas-C001")
    run = AsyncMock(return_value=RunResult(
        ok=True, status=RunStatus.COMPLETED, session_id="pas-C001", utterances=[]
    ))
    monkeypatch.setattr(pipeline, "soulstream", SimpleNamespace(ensure_channel_pas=ensure_pas, run=run))
    monkeypatch.setattr(pipeline.slack, "get_channel_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(pipeline, "_make_resolver", lambda: None)
    monkeypatch.setattr(pipeline, "build_remiel_context_item", AsyncMock(return_value=None))
    monkeypatch.setattr(pipeline, "build_intervention_prep", AsyncMock(return_value=None))

    await _execute_intervene(
        store=store,
        channel_id="C001",
        action=InterventionAction(type="message", target="channel", content="질문"),
        pending_messages=[message],
        target_message=message,
        target_ts="10.2",
        observer_reason="질문",
        recent_messages_count=3,
        bot_user_id="UBOT",
        prep_services=InterventionPrepServices(output_dir=None),
    )

    state = store.load_pas_state("C001")
    assert state["session_id"] == "pas-C001"
    assert state["read_ts"] == "10.2"
    assert state["initialized_context"] is True
    assert state["last_posted_ts"] is None
    assert store.load_pending("C001") == []
    assert [item["ts"] for item in store.load_judged("C001")] == ["10.2"]
    assert ensure_pas.await_args.kwargs["channel_id"] == "C001"
    assert run.await_args.kwargs["session_id"] == "pas-C001"
    assert run.await_args.kwargs["use_persistent_session"] is True
    assert run.await_args.kwargs["agent_id"] == "seosoyoung-intervene"
    assert not mock_plugin_sdk["slack"].send_message.await_count


@pytest.mark.asyncio
async def test_failed_pas_run_keeps_delta_unread(tmp_path, monkeypatch):
    from seosoyoung_plugins.channel_observer import pipeline

    store = ChannelStore(tmp_path)
    message = {"ts": "10.2", "user": "U1", "text": "질문"}
    store.upsert_pending("C001", message)
    monkeypatch.setattr(
        pipeline,
        "soulstream",
        SimpleNamespace(
            ensure_channel_pas=AsyncMock(return_value="pas-C001"),
            run=AsyncMock(return_value=RunResult(
                ok=False, status=RunStatus.FAILED, session_id="pas-C001", error="failed"
            )),
        ),
    )
    monkeypatch.setattr(pipeline.slack, "get_channel_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(pipeline, "_make_resolver", lambda: None)
    monkeypatch.setattr(pipeline, "build_remiel_context_item", AsyncMock(return_value=None))
    monkeypatch.setattr(pipeline, "build_intervention_prep", AsyncMock(return_value=None))

    await _execute_intervene(
        store=store,
        channel_id="C001",
        action=InterventionAction(type="message", target="channel", content="질문"),
        pending_messages=[message],
        target_message=message,
        target_ts="10.2",
        prep_services=InterventionPrepServices(output_dir=None),
    )

    state = store.load_pas_state("C001")
    assert state["read_ts"] == ""
    assert store.load_pending("C001") == [message]
