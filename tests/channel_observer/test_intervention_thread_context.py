"""E2: 개입 세션에 관련 스레드 답글을 메모리에서 주입하는 계약 테스트."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from seosoyoung.plugin_sdk.soulstream import RunResult, RunStatus
from seosoyoung_plugins.channel_observer.intervention import InterventionAction
from seosoyoung_plugins.channel_observer.intervention_prep import (
    InterventionPrepServices,
)


@pytest.mark.asyncio
async def test_execute_appends_trigger_thread_replies_without_remiel_reply_timestamps(
    mock_plugin_sdk, tmp_path, monkeypatch,
):
    from seosoyoung_plugins.channel_observer import pipeline

    root_ts = "1789379341.034089"
    reply_ts = "1789379441.585799"
    store = MagicMock()
    store.get_digest.return_value = None
    store.load_judged.return_value = []
    store.append_judged = MagicMock()
    prep_builder = AsyncMock(return_value=None)
    remiel_builder = AsyncMock(return_value=None)
    resolver = MagicMock()
    resolver.resolve.side_effect = {
        "U08UYUVCBD0": "Cha, Seungkyu [scha] / [U08UYUVCBD0]",
    }.get
    monkeypatch.setattr(pipeline, "_make_resolver", lambda: resolver)
    monkeypatch.setattr(
        pipeline,
        "_fetch_recent_context_bundle",
        AsyncMock(
            return_value=(
                f"[C08KT1HDU5U:{root_ts}] <이도행 [sonanlee] / [U08HWSYE33R]>: "
                "승규님 이 페이지를 트위터에 올려도 될까요?",
                [root_ts],
            )
        ),
    )
    monkeypatch.setattr(pipeline, "build_intervention_prep", prep_builder)
    monkeypatch.setattr(pipeline, "build_remiel_context_item", remiel_builder)
    mock_plugin_sdk["soulstream"].run = AsyncMock(
        return_value=RunResult(
            ok=True,
            status=RunStatus.COMPLETED,
            utterances=["확인했습니다."],
        )
    )

    await pipeline._execute_intervene(
        store=store,
        channel_id="C08KT1HDU5U",
        action=InterventionAction(type="message", target=root_ts, content="reason"),
        pending_messages=[
            {"ts": root_ts, "user": "U08HWSYE33R", "text": "질문"},
        ],
        thread_buffers={
            root_ts: [
                {
                    "ts": reply_ts,
                    "user": "U08UYUVCBD0",
                    "text": "테트리스가 안되지 않나요?",
                },
            ],
        },
        prep_services=InterventionPrepServices(output_dir=tmp_path),
    )

    context = mock_plugin_sdk["soulstream"].run.call_args.kwargs["context"]
    thread_context = context[0]["content"]
    assert "## 스레드 답글" in thread_context
    assert f"[C08KT1HDU5U:{root_ts}] 스레드" in thread_context
    assert (
        f"  [C08KT1HDU5U:{reply_ts}] "
        "<Cha, Seungkyu [scha] / [U08UYUVCBD0]>: 테트리스가 안되지 않나요?"
    ) in thread_context
    assert prep_builder.await_args.kwargs["message_timestamps"] == [root_ts, reply_ts]
    assert remiel_builder.await_args.kwargs["timestamps"] == [root_ts]


@pytest.mark.asyncio
async def test_execute_appends_pending_messages_thread_replies(
    mock_plugin_sdk, tmp_path, monkeypatch,
):
    from seosoyoung_plugins.channel_observer import pipeline

    root_ts = "2000.000001"
    reply_ts = "2001.000001"
    trigger_ts = "3000.000001"
    store = MagicMock()
    store.get_digest.return_value = None
    store.load_judged.return_value = []
    store.append_judged = MagicMock()
    monkeypatch.setattr(pipeline, "_make_resolver", lambda: None)
    monkeypatch.setattr(
        pipeline,
        "_fetch_recent_context_bundle",
        AsyncMock(return_value=(f"[C_TEST:{trigger_ts}] <U3>: trigger", [trigger_ts])),
    )
    monkeypatch.setattr(pipeline, "build_intervention_prep", AsyncMock(return_value=None))
    monkeypatch.setattr(pipeline, "build_remiel_context_item", AsyncMock(return_value=None))
    mock_plugin_sdk["soulstream"].run = AsyncMock(
        return_value=RunResult(
            ok=True,
            status=RunStatus.COMPLETED,
            utterances=["확인했습니다."],
        )
    )

    await pipeline._execute_intervene(
        store=store,
        channel_id="C_TEST",
        action=InterventionAction(type="message", target="channel", content="reason"),
        pending_messages=[
            {"ts": reply_ts, "thread_ts": root_ts, "user": "U2", "text": "reply"},
            {"ts": trigger_ts, "user": "U3", "text": "trigger"},
        ],
        thread_buffers={
            root_ts: [{"ts": reply_ts, "user": "U2", "text": "reply"}],
        },
        prep_services=InterventionPrepServices(output_dir=tmp_path),
    )

    context = mock_plugin_sdk["soulstream"].run.call_args.kwargs["context"]
    assert "## 스레드 답글" in context[0]["content"]
    assert f"[C_TEST:{reply_ts}] <U2>: reply" in context[0]["content"]


@pytest.mark.asyncio
async def test_execute_omits_thread_reply_section_when_no_relevant_replies(
    mock_plugin_sdk, tmp_path, monkeypatch,
):
    from seosoyoung_plugins.channel_observer import pipeline

    root_ts = "4000.000001"
    store = MagicMock()
    store.get_digest.return_value = None
    store.load_judged.return_value = []
    store.append_judged = MagicMock()
    monkeypatch.setattr(
        pipeline,
        "_fetch_recent_context_bundle",
        AsyncMock(return_value=(f"[C_TEST:{root_ts}] <U1>: root", [root_ts])),
    )
    monkeypatch.setattr(pipeline, "build_intervention_prep", AsyncMock(return_value=None))
    monkeypatch.setattr(pipeline, "build_remiel_context_item", AsyncMock(return_value=None))
    mock_plugin_sdk["soulstream"].run = AsyncMock(
        return_value=RunResult(
            ok=True,
            status=RunStatus.COMPLETED,
            utterances=["확인했습니다."],
        )
    )

    await pipeline._execute_intervene(
        store=store,
        channel_id="C_TEST",
        action=InterventionAction(type="message", target="channel", content="reason"),
        pending_messages=[{"ts": root_ts, "user": "U1", "text": "root"}],
        thread_buffers={
            "unrelated": [{"ts": "5000.0", "user": "U5", "text": "other"}],
        },
        prep_services=InterventionPrepServices(output_dir=tmp_path),
    )

    context = mock_plugin_sdk["soulstream"].run.call_args.kwargs["context"]
    assert "## 스레드 답글" not in context[0]["content"]
