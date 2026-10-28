"""Jev message judgment and emoji frequency behavior."""

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest


@dataclass
class Reaction:
    name: str
    count: int = 1
    users: list[str] | None = None


@dataclass
class Message:
    ts: str
    user: str
    text: str = ""
    thread_ts: str | None = None
    reactions: list[Reaction] | None = None


def test_emoji_frequency_records_and_removes_reactions(tmp_path):
    from seosoyoung_plugins.channel_observer.emoji_frequency import (
        EmojiFrequencyStore,
    )

    path = tmp_path / "emoji_frequency.json"
    store = EmojiFrequencyStore(path)

    assert store.record_reaction(" :custom: ") is True
    assert store.record_reaction("custom", "removed") is True
    assert "custom" not in store.counts

    assert store.record_reaction("thumbsup") is True
    restored = EmojiFrequencyStore(path)
    assert restored.counts["thumbsup"] == 2


def test_emoji_frequency_ignores_bot_and_system_reactions(tmp_path):
    from seosoyoung_plugins.channel_observer.emoji_frequency import (
        EmojiFrequencyStore,
    )

    store = EmojiFrequencyStore(tmp_path / "emoji_frequency.json")
    original = dict(store.counts)

    assert store.record_reaction(
        "custom", user_id="UBOT", bot_user_id="UBOT"
    ) is False
    for emoji in ("hourglass_flowing_sand", "rocket", "hammer"):
        assert store.record_reaction(emoji) is False

    assert store.counts == original


def test_emoji_frequency_defaults_when_file_is_invalid(tmp_path):
    from seosoyoung_plugins.channel_observer.emoji_frequency import (
        DEFAULT_EMOJI_DESCRIPTIONS,
        EmojiFrequencyStore,
    )

    path = tmp_path / "emoji_frequency.json"
    path.write_text("not json", encoding="utf-8")

    store = EmojiFrequencyStore(path)

    assert store.counts == {name: 1 for name in DEFAULT_EMOJI_DESCRIPTIONS}
    assert store.seeded_channels == set()


def test_emoji_frequency_seeds_history_once_and_excludes_bot_reaction(tmp_path):
    from seosoyoung_plugins.channel_observer.emoji_frequency import (
        EmojiFrequencyStore,
    )

    store = EmojiFrequencyStore(tmp_path / "emoji_frequency.json")
    messages = [
        Message(
            ts="1",
            user="U1",
            reactions=[
                Reaction("custom", count=3, users=["U1", "UBOT", "U2"]),
                Reaction("rocket", count=4, users=["U1", "U2", "U3", "U4"]),
            ],
        ),
        {
            "ts": "2",
            "reactions": [
                {"name": "eyes", "count": 2, "users": ["U1", "U2"]}
            ],
        },
    ]

    assert store.seed_from_messages("C1", messages, bot_user_id="UBOT") == 4
    assert store.seed_from_messages("C1", messages, bot_user_id="UBOT") == 0
    assert store.counts["custom"] == 2
    assert store.counts["eyes"] == 3
    assert "rocket" not in store.counts
    assert "C1" in store.seeded_channels

    restored = EmojiFrequencyStore(tmp_path / "emoji_frequency.json")
    assert "C1" in restored.seeded_channels
    assert restored.counts["custom"] == 2


def test_emoji_candidates_are_ranked_and_include_descriptions(tmp_path):
    from seosoyoung_plugins.channel_observer.emoji_frequency import (
        DEFAULT_EMOJI_DESCRIPTIONS,
        EmojiFrequencyStore,
    )

    store = EmojiFrequencyStore(tmp_path / "emoji_frequency.json")
    store.counts.update({"custom-z": 9, "custom-a": 9, "ㅋㅋ": 9})

    candidates = store.get_top_candidates(limit=3)

    assert list(candidates) == ["ㅋㅋ", "custom-a", "custom-z"]
    assert candidates["ㅋㅋ"] == f"{DEFAULT_EMOJI_DESCRIPTIONS['ㅋㅋ']} (사용 빈도 9)"
    assert candidates["custom-a"] == ":custom-a: 커스텀 이모지 (사용 빈도 9)"
    store.counts.update({f"custom-{index}": index for index in range(300)})
    assert len(store.get_top_candidates(limit=1000)) == 255

    store.counts = {name: 0 for name in store.counts}
    assert list(store.get_top_candidates(limit=2)) == list(
        DEFAULT_EMOJI_DESCRIPTIONS
    )[:2]


def test_compute_reaction_density_summarizes_recent_bot_reactions():
    from seosoyoung_plugins.channel_observer.jev_judge import (
        compute_reaction_density,
    )

    recent = [
        {"reactions": [], "bot_reactions": []},
        {"reactions": ["eyes"], "bot_reactions": ["eyes"]},
        {"reactions": ["clap"], "bot_reactions": ["clap"]},
        {"reactions": ["heart"], "bot_reactions": []},
    ]

    assert compute_reaction_density(recent) == {
        "recent_message_count": 4,
        "messages_with_any_emoji": 3,
        "messages_with_bot_emoji": 2,
        "last_5_bot_emoji_count": 2,
        "consecutive_bot_reactions": 0,
        "last_bot_emoji": "clap",
    }


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeAsyncClient:
    def __init__(self, *, timeout, response, requests):
        self.timeout = timeout
        self.response = response
        self.requests = requests

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, *, headers, json):
        self.requests.append((url, headers, json))
        return self.response


def _answers(*, react=0.9, choice="laughing", confidence=0.8, probs=None, intervene=0.7):
    return {
        "should_react": {"noul": react},
        "emoji": {
            "choice": choice,
            "confidence": confidence,
            "probabilities": probs or {choice: confidence},
        },
        "should_intervene": {"noul": intervene},
    }


def _install_jev_response(monkeypatch, payload):
    from seosoyoung_plugins.channel_observer import jev_judge

    requests = []
    monkeypatch.setattr(
        jev_judge.httpx,
        "AsyncClient",
        lambda *, timeout: FakeAsyncClient(
            timeout=timeout,
            response=FakeResponse(payload),
            requests=requests,
        ),
    )
    return requests


@pytest.mark.asyncio
async def test_jev_judge_posts_all_questions_and_parses_verdict(monkeypatch):
    from seosoyoung_plugins.channel_observer.jev_judge import JevChannelJudge

    requests = _install_jev_response(monkeypatch, {"answers": _answers()})
    judge = JevChannelJudge(api_key="secret", base_url="https://jev.example/")

    verdict = await judge.judge_message(
        channel_id="C1",
        target_message={"ts": "2", "user": "U1", "text": "재밌다", "thread_ts": None},
        recent_messages=[
            {"ts": "1", "user": "U2", "text": "이전", "reactions": [], "bot_reactions": []}
        ],
        emoji_candidates={"laughing": "유쾌한 상황"},
    )

    assert verdict.should_react is True
    assert verdict.selected_emoji == "laughing"
    assert verdict.emoji_confidence == 0.8
    assert verdict.emoji_probabilities == {"laughing": 0.8}
    assert verdict.should_intervene is True
    assert len(requests) == 1
    url, headers, payload = requests[0]
    assert url == "https://jev.example/v1/systemone"
    assert headers["Authorization"] == "Bearer secret"
    assert payload["model"] == "jev-latest"
    assert set(payload["questions"]) == {
        "should_react", "emoji", "should_intervene"
    }
    assert payload["questions"]["emoji"]["criteria"] == {
        "laughing": "유쾌한 상황"
    }
    state = json.loads(payload["state"])
    assert state["target_message"]["text"] == "재밌다"
    assert state["recent_reaction_summary"]["recent_message_count"] == 1
    assert "재밌다" in payload["state"]


@pytest.mark.asyncio
async def test_jev_judge_without_api_key_returns_none(caplog):
    from seosoyoung_plugins.channel_observer.jev_judge import JevChannelJudge

    judge = JevChannelJudge(api_key="")
    verdict = await judge.judge_message(
        channel_id="C1", target_message={}, recent_messages=[], emoji_candidates={}
    )

    assert verdict is None
    assert "API 키" in caplog.text


@pytest.mark.asyncio
async def test_jev_judge_suppresses_dense_reactions_and_replaces_repeat(monkeypatch):
    from seosoyoung_plugins.channel_observer.jev_judge import JevChannelJudge

    dense = [
        {"reactions": [], "bot_reactions": []},
        {"reactions": ["eyes"], "bot_reactions": ["eyes"]},
        {"reactions": ["clap"], "bot_reactions": ["clap"]},
    ]
    _install_jev_response(monkeypatch, {"answers": _answers(react=1.0)})
    judge = JevChannelJudge(api_key="secret")
    verdict = await judge.judge_message(
        channel_id="C1", target_message={}, recent_messages=dense,
        emoji_candidates={"laughing": "웃음"},
    )
    assert verdict.should_react is False
    assert verdict.suppressed_by_density is True

    spread = [
        {"reactions": ["eyes"], "bot_reactions": ["eyes"]},
        {"reactions": [], "bot_reactions": []},
        {"reactions": ["clap"], "bot_reactions": ["clap"]},
        {"reactions": [], "bot_reactions": []},
    ]
    _install_jev_response(monkeypatch, {"answers": _answers(react=1.0)})
    verdict = await judge.judge_message(
        channel_id="C1", target_message={}, recent_messages=spread,
        emoji_candidates={"laughing": "웃음"},
    )
    assert verdict.should_react is False
    assert verdict.suppressed_by_density is True

    _install_jev_response(
        monkeypatch,
        {"answers": _answers(react=0.6, probs={"laughing": 0.6})},
    )
    verdict = await judge.judge_message(
        channel_id="C1", target_message={},
        recent_messages=[{"reactions": [], "bot_reactions": ["eyes"]}],
        emoji_candidates={"laughing": "웃음"},
    )
    assert verdict.should_react is False

    _install_jev_response(
        monkeypatch,
        {"answers": _answers(react=0.9, probs={"laughing": 0.8, "clap": 0.2})},
    )
    verdict = await judge.judge_message(
        channel_id="C1",
        target_message={},
        recent_messages=[{"reactions": [], "bot_reactions": ["laughing"]}],
        emoji_candidates={"laughing": "웃음", "clap": "박수"},
    )
    assert verdict.should_react is True
    assert verdict.selected_emoji == "laughing"
    assert verdict.suppressed_by_density is False


@pytest.mark.asyncio
async def test_jev_judge_uses_lower_intervene_threshold_for_trigger(monkeypatch):
    from seosoyoung_plugins.channel_observer.jev_judge import JevChannelJudge

    judge = JevChannelJudge(api_key="secret")
    _install_jev_response(monkeypatch, {"answers": _answers(intervene=0.5)})
    ordinary = await judge.judge_message(
        channel_id="C1", target_message={}, recent_messages=[], emoji_candidates={}
    )
    _install_jev_response(monkeypatch, {"answers": _answers(intervene=0.5)})
    triggered = await judge.judge_message(
        channel_id="C1", target_message={}, recent_messages=[], emoji_candidates={},
        trigger_words_matched=["서소영"],
    )

    assert ordinary.should_intervene is False
    assert triggered.should_intervene is True


@pytest.mark.asyncio
async def test_message_pipeline_reacts_and_runs_intervention(monkeypatch, mock_plugin_sdk):
    from seosoyoung.plugin_sdk.slack import ReactionResult
    from seosoyoung_plugins.channel_observer import pipeline
    from seosoyoung_plugins.channel_observer.emoji_frequency import EmojiFrequencyStore
    from seosoyoung_plugins.channel_observer.jev_judge import JevMessageVerdict

    judge = MagicMock()
    judge.judge_message = AsyncMock(return_value=JevMessageVerdict(
        should_react=True,
        react_score=0.9,
        selected_emoji="laughing",
        emoji_confidence=0.8,
        emoji_probabilities={"laughing": 0.8},
        should_intervene=True,
        intervene_score=0.8,
    ))
    emoji_store = MagicMock(spec=EmojiFrequencyStore)
    emoji_store.seed_from_messages.return_value = 0
    emoji_store.get_top_candidates.return_value = {"laughing": "웃음"}
    store = MagicMock()
    cooldown = MagicMock()
    cooldown.burst_probability.return_value = 1.0
    cooldown.minutes_since_last.return_value = 10.0
    intervention = AsyncMock()
    monkeypatch.setattr(pipeline, "_execute_intervene", intervention)
    monkeypatch.setattr(
        pipeline.slack,
        "get_channel_history",
        AsyncMock(return_value=[
            Message("1", "U1", "스레드 루트"),
        ]),
    )
    monkeypatch.setattr(
        pipeline.slack,
        "get_thread_replies",
        AsyncMock(return_value=[
            Message("1", "U1", "스레드 루트"),
            Message("2", "U2", "이전 답글", thread_ts="1", reactions=[Reaction("eyes", users=["UBOT"])]),
        ]),
    )
    recent_bot_reactions = {f"old-{index}": [] for index in range(99)}
    recent_bot_reactions["2"] = ["clap"]

    verdict = await pipeline.run_message_jev_pipeline(
        channel_id="C1",
        event={"channel": "C1", "ts": "3", "user": "U3", "text": "안녕 서소영", "thread_ts": "1"},
        jev_judge=judge,
        emoji_store=emoji_store,
        cooldown=cooldown,
        store=store,
        bot_user_id="UBOT",
        trigger_words=["서소영"],
        llm_call=AsyncMock(),
        recent_messages_count=3,
        recent_bot_reactions=recent_bot_reactions,
    )

    assert verdict.should_intervene is True
    judge.judge_message.assert_awaited_once()
    judge_kwargs = judge.judge_message.await_args.kwargs
    assert [m["ts"] for m in judge_kwargs["recent_messages"]] == ["1", "2"]
    assert judge_kwargs["recent_messages"][1]["bot_reactions"] == ["eyes", "clap"]
    mock_plugin_sdk["slack"].add_reaction.assert_awaited_once_with(
        channel="C1", ts="3", emoji="laughing"
    )
    assert len(recent_bot_reactions) == 100
    assert "old-0" not in recent_bot_reactions
    assert recent_bot_reactions["3"] == ["laughing"]
    intervention.assert_awaited_once()
    intervene_kwargs = intervention.await_args.kwargs
    assert intervene_kwargs["action"].target == "1"
    assert [m["ts"] for m in intervene_kwargs["thread_buffers"]["1"]] == ["2", "3"]
    assert intervene_kwargs["thread_buffers"]["1"][-1]["text"] == "안녕 서소영"
    cooldown.record.assert_called_once_with("C1")

    mock_plugin_sdk["slack"].add_reaction = AsyncMock(
        return_value=ReactionResult(ok=False, error="failed")
    )
    failed_reactions: dict[str, list[str]] = {}
    await pipeline.run_message_jev_pipeline(
        channel_id="C1",
        event={"channel": "C1", "ts": "4", "user": "U3", "text": "반가워"},
        jev_judge=judge,
        emoji_store=emoji_store,
        cooldown=cooldown,
        store=store,
        bot_user_id="UBOT",
        recent_bot_reactions=failed_reactions,
    )
    assert "4" not in failed_reactions


@pytest.mark.asyncio
async def test_message_pipeline_marks_bot_mentions_without_jev(monkeypatch):
    from seosoyoung_plugins.channel_observer import pipeline

    judge = MagicMock()
    judge.judge_message = AsyncMock()
    monkeypatch.setattr(pipeline.mention, "get_backend", lambda: object())
    monkeypatch.setattr(pipeline.mention, "mark", MagicMock())

    verdict = await pipeline.run_message_jev_pipeline(
        channel_id="C1",
        event={
            "channel": "C1", "ts": "2", "thread_ts": "1",
            "user": "U1", "text": "<@UBOT> 질문이 있어요",
        },
        jev_judge=judge,
        emoji_store=MagicMock(),
        cooldown=MagicMock(),
        store=MagicMock(),
        bot_user_id="UBOT",
    )

    assert verdict is None
    pipeline.mention.mark.assert_called_once_with("1")
    judge.judge_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_intervene_falls_back_to_latest_context_for_thread_root(
    monkeypatch, mock_plugin_sdk,
):
    from seosoyoung_plugins.channel_observer import pipeline
    from seosoyoung_plugins.channel_observer.intervention import InterventionAction

    context_seen = AsyncMock(return_value=("context", []))
    monkeypatch.setattr(pipeline, "_make_resolver", lambda: None)
    monkeypatch.setattr(pipeline, "_fetch_recent_context_bundle", context_seen)
    monkeypatch.setattr(pipeline, "build_intervention_prep", AsyncMock(return_value=None))
    monkeypatch.setattr(pipeline, "build_remiel_context_item", AsyncMock(return_value=None))
    store = MagicMock()
    store.get_digest.return_value = None
    store.load_judged.return_value = []
    await pipeline._execute_intervene(
        store=store,
        channel_id="C1",
        action=InterventionAction(type="message", target="THREAD_ROOT", content="jev"),
        pending_messages=[
            {"ts": "2", "user": "U2", "text": "이전"},
            {"ts": "3", "user": "U3", "text": "현재"},
        ],
        recent_messages_count=1,
    )

    mock_plugin_sdk["soulstream"].run.assert_awaited_once()
    assert mock_plugin_sdk["soulstream"].run.await_args.kwargs["thread_ts"] == "THREAD_ROOT"

