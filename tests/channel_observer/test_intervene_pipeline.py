"""2단계: 개입 선주입·기록·보관 배선 계약 테스트."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from seosoyoung.plugin_sdk.slack import SendMessageResult
from seosoyoung.plugin_sdk.soulstream import RunResult, RunStatus
from seosoyoung_plugins.channel_observer import intervene_memory
from seosoyoung_plugins.channel_observer.intervention import InterventionAction
from seosoyoung_plugins.channel_observer.intervention_prep import (
    InterventionPrep,
    InterventionPrepServices,
)
from seosoyoung_plugins.channel_observer.reference_context import (
    ReferenceConfig,
    ReferenceContextServices,
)


async def _drain_background() -> None:
    while intervene_memory._background_tasks:
        await asyncio.gather(*list(intervene_memory._background_tasks))


def _store() -> MagicMock:
    store = MagicMock()
    store.get_digest.return_value = None
    store.load_judged.return_value = []
    store.append_judged = MagicMock()
    return store


def _prep(title: str = "🗯️ 테스트 개입") -> InterventionPrep:
    return InterventionPrep(
        slug="20261022-120000",
        threads_file=Path("/tmp/threads-C_TEST-20261022-120000.json"),
        suggested_title=title,
    )


def _run_result(utterances: list[str], output: str = "") -> RunResult:
    return RunResult(
        ok=True,
        status=RunStatus.COMPLETED,
        session_id="session-123",
        output=output,
        utterances=utterances,
    )


class FakeMemory:
    def __init__(self, channel_memory=None, member_facts=None) -> None:
        self.channel_memory = channel_memory
        self.member_facts = member_facts
        self.channel_calls: list[str] = []
        self.member_calls: list[tuple[str, list[str]]] = []
        self.record_calls: list[dict] = []
        self.archive_calls: list[tuple[str, Path]] = []

    async def load_channel_memory(self, channel_id: str):
        self.channel_calls.append(channel_id)
        return self.channel_memory

    async def load_member_facts(self, channel_id: str, slack_ids: list[str]):
        self.member_calls.append((channel_id, slack_ids))
        return self.member_facts

    async def record_utterance(self, **kwargs):
        self.record_calls.append(kwargs)
        return True

    async def archive_threads(self, channel_id: str, threads_file: Path):
        self.archive_calls.append((channel_id, threads_file))
        return True


def _patch_context(monkeypatch, pipeline, *, ret="[C_TEST:1] <U1>: hi", ts=None):
    monkeypatch.setattr(pipeline, "_make_resolver", lambda: None)
    monkeypatch.setattr(
        pipeline,
        "_fetch_recent_context_bundle",
        AsyncMock(return_value=(ret, ts if ts is not None else ["1"])),
    )


@pytest.mark.asyncio
async def test_prep_task_does_not_block_run(mock_plugin_sdk, monkeypatch):
    from seosoyoung_plugins.channel_observer import pipeline

    prep_started = asyncio.Event()
    prep_finished = asyncio.Event()
    release = asyncio.Event()

    async def slow_prep(**_kwargs):
        prep_started.set()
        await release.wait()
        prep_finished.set()
        return _prep()

    async def run(**kwargs):
        assert prep_started.is_set()
        assert not prep_finished.is_set()
        release.set()
        return _run_result(["응답"])

    monkeypatch.setattr(pipeline, "build_intervention_prep", slow_prep)
    monkeypatch.setattr(pipeline, "build_remiel_context_item", AsyncMock(return_value=None))
    _patch_context(monkeypatch, pipeline)
    mock_plugin_sdk["soulstream"].run = run

    await asyncio.wait_for(
        pipeline._execute_intervene(
            store=_store(),
            channel_id="C_TEST",
            action=InterventionAction(type="message", target="channel", content="r"),
            pending_messages=[{"ts": "1", "user": "U1", "text": "hi"}],
            llm_call=AsyncMock(return_value="{}"),
            prep_services=InterventionPrepServices(output_dir=None),
        ),
        timeout=2,
    )

    assert prep_finished.is_set()
    mock_plugin_sdk["slack"].send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_context_order_and_participant_ids(mock_plugin_sdk, monkeypatch, tmp_path):
    from seosoyoung_plugins.channel_observer import pipeline

    memory = FakeMemory(
        channel_memory={
            "previousSummary": "지난 줄거리",
            "recentUtterances": [],
            "topicsToAvoid": [],
            "needsCompression": False,
        },
        member_facts={
            "members": [
                {
                    "slackId": "U1",
                    "displayName": "주복",
                    "cacheStatus": "hit",
                    "keyFacts": ["f1"],
                }
            ]
        },
    )
    reference_item = {"key": "reference_context", "label": "참고", "content": "ref"}
    remiel_item = {"key": "remiel_context", "label": "remiel", "content": "rem"}
    reference_builder = AsyncMock(return_value=reference_item)
    remiel_builder = AsyncMock(return_value=remiel_item)
    prep_builder = AsyncMock(return_value=_prep())
    _patch_context(monkeypatch, pipeline, ts=["1"])
    monkeypatch.setattr(pipeline, "build_reference_context_item", reference_builder)
    monkeypatch.setattr(pipeline, "build_remiel_context_item", remiel_builder)
    monkeypatch.setattr(pipeline, "build_intervention_prep", prep_builder)
    mock_plugin_sdk["soulstream"].run = AsyncMock(return_value=_run_result(["응답"]))

    reference = ReferenceConfig(atom_base_url="http://atom", atom_api_key="k")
    services = ReferenceContextServices(
        output_dir=tmp_path, reference=reference, memory=memory,
    )
    pending = [
        {"ts": "1", "user": "U1", "text": "앞선", "thread_ts": "9"},
        {"ts": "2", "user": "UBOT", "text": "봇"},
        {"ts": "3", "user": "U2", "text": "트리거"},
    ]

    await pipeline._execute_intervene(
        store=_store(),
        channel_id="C_TEST",
        action=InterventionAction(type="message", target="3", content="r"),
        pending_messages=pending,
        thread_buffers={"9": [{"ts": "10", "user": "U4", "text": "답글"}]},
        bot_user_id="UBOT",
        llm_call=AsyncMock(return_value="{}"),
        remiel_config=None,
        prep_services=services,
    )

    context = mock_plugin_sdk["soulstream"].run.call_args.kwargs["context"]
    assert [item["key"] for item in context] == [
        "thread_context",
        "channel_memory",
        "member_facts",
        "reference_context",
        "remiel_context",
    ]
    assert memory.member_calls == [("C_TEST", ["U1", "U2", "U4"])]
    reference_builder.assert_awaited_once_with(
        "트리거", reference, channel_id="C_TEST",
    )
    prep_kwargs = prep_builder.await_args.kwargs
    assert prep_kwargs["slug"]
    assert prep_kwargs["thread_context"].startswith("[C_TEST:1]")


@pytest.mark.asyncio
async def test_record_and_archive_after_successful_post(
    mock_plugin_sdk, monkeypatch, tmp_path, caplog,
):
    from seosoyoung_plugins.channel_observer import pipeline

    memory = FakeMemory(
        channel_memory={
            "previousSummary": "요약",
            "recentUtterances": [],
            "topicsToAvoid": [],
            "needsCompression": True,
            "oldestTen": [{"cardId": "card-1"}],
        }
    )
    prep = _prep()
    monkeypatch.setattr(pipeline, "build_intervention_prep", AsyncMock(return_value=prep))
    monkeypatch.setattr(pipeline, "build_remiel_context_item", AsyncMock(return_value=None))
    _patch_context(monkeypatch, pipeline)
    mock_plugin_sdk["soulstream"].run = AsyncMock(
        return_value=_run_result(
            ["게시 본문"],
            output=(
                "<utterance>게시 본문</utterance>"
                "<topics>주제1, 주제2</topics>"
                "<summary_line>한 줄 요약</summary_line>"
                "<compressed_summary>압축 줄거리</compressed_summary>"
            ),
        )
    )
    mock_plugin_sdk["slack"].send_message = AsyncMock(
        return_value=SendMessageResult(ok=True, ts="999.001", channel="C_TEST")
    )

    with caplog.at_level("INFO"):
        await pipeline._execute_intervene(
            store=_store(),
            channel_id="C_TEST",
            action=InterventionAction(type="message", target="channel", content="r"),
            pending_messages=[{"ts": "1", "user": "U1", "text": "hi"}],
            llm_call=AsyncMock(return_value="{}"),
            prep_services=InterventionPrepServices(output_dir=tmp_path, memory=memory),
        )
        await _drain_background()

    assert any("intervene preload" in rec.message for rec in caplog.records)
    assert len(memory.record_calls) == 1
    recorded = memory.record_calls[0]
    assert recorded["channel_id"] == "C_TEST"
    assert recorded["body"] == "게시 본문"
    assert recorded["topics"] == ["주제1", "주제2"]
    assert recorded["summary_line"] == "한 줄 요약"
    assert recorded["compressed_summary"] == "압축 줄거리"
    assert recorded["channel_memory"]["needsCompression"] is True
    assert recorded["output_dir"] == tmp_path
    assert memory.archive_calls == [("C_TEST", prep.threads_file)]


@pytest.mark.asyncio
async def test_no_record_without_utterance_but_archive_runs(
    mock_plugin_sdk, monkeypatch, tmp_path,
):
    from seosoyoung_plugins.channel_observer import pipeline

    memory = FakeMemory(channel_memory={"previousSummary": "", "needsCompression": False})
    prep = _prep()
    monkeypatch.setattr(pipeline, "build_intervention_prep", AsyncMock(return_value=prep))
    monkeypatch.setattr(pipeline, "build_remiel_context_item", AsyncMock(return_value=None))
    _patch_context(monkeypatch, pipeline)
    mock_plugin_sdk["soulstream"].run = AsyncMock(return_value=_run_result([]))

    await pipeline._execute_intervene(
        store=_store(),
        channel_id="C_TEST",
        action=InterventionAction(type="message", target="channel", content="r"),
        pending_messages=[{"ts": "1", "user": "U1", "text": "hi"}],
        llm_call=AsyncMock(return_value="{}"),
        prep_services=InterventionPrepServices(output_dir=tmp_path, memory=memory),
    )
    await _drain_background()

    assert memory.record_calls == []
    assert memory.archive_calls == [("C_TEST", prep.threads_file)]
    mock_plugin_sdk["slack"].send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_record_when_send_fails_but_archive_runs(
    mock_plugin_sdk, monkeypatch, tmp_path,
):
    from seosoyoung_plugins.channel_observer import pipeline

    memory = FakeMemory(channel_memory={"previousSummary": "", "needsCompression": False})
    prep = _prep()
    monkeypatch.setattr(pipeline, "build_intervention_prep", AsyncMock(return_value=prep))
    monkeypatch.setattr(pipeline, "build_remiel_context_item", AsyncMock(return_value=None))
    _patch_context(monkeypatch, pipeline)
    mock_plugin_sdk["soulstream"].run = AsyncMock(return_value=_run_result(["본문"]))
    mock_plugin_sdk["slack"].send_message = AsyncMock(
        return_value=SendMessageResult(ok=False, error="rate limited")
    )

    await pipeline._execute_intervene(
        store=_store(),
        channel_id="C_TEST",
        action=InterventionAction(type="message", target="channel", content="r"),
        pending_messages=[{"ts": "1", "user": "U1", "text": "hi"}],
        llm_call=AsyncMock(return_value="{}"),
        prep_services=InterventionPrepServices(output_dir=tmp_path, memory=memory),
    )
    await _drain_background()

    assert memory.record_calls == []
    assert memory.archive_calls == [("C_TEST", prep.threads_file)]


class SlowDrainMemory:
    """운영처럼 루프가 닫히기 전에 반드시 끝나야 하는 기록·보관을 흉내낸다."""

    def __init__(self) -> None:
        self.recorded = False
        self.archived = False

    async def load_channel_memory(self, channel_id: str):
        return {"previousSummary": "", "needsCompression": False}

    async def load_member_facts(self, channel_id: str, slack_ids: list[str]):
        return None

    async def record_utterance(self, **kwargs):
        await asyncio.sleep(0.05)
        self.recorded = True
        return True

    async def archive_threads(self, channel_id: str, threads_file: Path):
        await asyncio.sleep(0.05)
        self.archived = True
        return True


def test_new_event_loop_drains_record_and_archive_before_close(
    mock_plugin_sdk, monkeypatch, tmp_path,
):
    from seosoyoung_plugins.channel_observer import pipeline

    memory = SlowDrainMemory()
    monkeypatch.setattr(
        pipeline, "build_intervention_prep", AsyncMock(return_value=_prep())
    )
    monkeypatch.setattr(
        pipeline, "build_remiel_context_item", AsyncMock(return_value=None)
    )
    _patch_context(monkeypatch, pipeline)
    mock_plugin_sdk["soulstream"].run = AsyncMock(return_value=_run_result(["본문"]))
    mock_plugin_sdk["slack"].send_message = AsyncMock(
        return_value=SendMessageResult(ok=True, ts="999.001", channel="C_TEST")
    )

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            pipeline._execute_intervene(
                store=_store(),
                channel_id="C_TEST",
                action=InterventionAction(
                    type="message", target="channel", content="r",
                ),
                pending_messages=[{"ts": "1", "user": "U1", "text": "hi"}],
                llm_call=AsyncMock(return_value="{}"),
                prep_services=InterventionPrepServices(
                    output_dir=tmp_path, memory=memory,
                ),
            )
        )
    finally:
        loop.close()

    assert memory.recorded is True
    assert memory.archived is True
