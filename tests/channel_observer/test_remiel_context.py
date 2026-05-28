import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from seosoyoung.plugin_sdk.slack import Message
from seosoyoung.plugin_sdk.soulstream import RunResult, RunStatus
from seosoyoung_plugins.channel_observer.intervention import InterventionAction
from seosoyoung_plugins.channel_observer.plugin import ChannelObserverPlugin


SAMPLE_CONFIG = {
    "channels": ["C_TEST"],
    "soulstream_url": "http://localhost:4105",
    "soulstream_token": "test-token",
    "model": "gpt-5-mini",
    "compressor_model": "gpt-5.2",
    "memory_path": "/tmp/test_remiel_context",
    "threshold_a": 150,
    "threshold_b": 5000,
    "intervention_threshold": 0.18,
    "periodic_sec": 300,
    "debug_channel": "C_DEBUG",
}


def _lookup_response(*, channel_enabled: bool = True, ready: int = 1) -> dict:
    ready_items = [
        {
            "ts": "1001.000000",
            "message_id": "msg-1",
            "status": "ready",
            "summary": "배포 설정을 확인해 달라는 요청",
            "intent": "request",
            "addressees": [{"id": "U_SOYOUNG", "name": "서소영"}],
            "confidence": 0.91,
            "adversarial_note": None,
            "created_at": "2026-05-29T00:00:00.000Z",
        }
    ][:ready]
    unresolved = [
        {"ts": "1002.000000", "status": "missing_interpretation", "message_id": "msg-2"}
    ]
    items = ready_items + unresolved
    return {
        "channel_id": "C_TEST",
        "channel_enabled": channel_enabled,
        "confidence_threshold": 0.75,
        "coverage": {
            "requested": len(items),
            "ready": len(ready_items),
            "needs_reasoning": len(items) - len(ready_items),
            "disabled_channel": 0 if channel_enabled else len(items),
            "missing_message": 0,
            "missing_interpretation": len(unresolved) if channel_enabled else 0,
            "low_confidence": 0,
            "stale": 0,
            "invalid_metadata": 0,
        },
        "items": items if channel_enabled else [
            {"ts": "1001.000000", "status": "disabled_channel"},
            {"ts": "1002.000000", "status": "disabled_channel"},
        ],
    }


class TestRemielLookupClient:
    @pytest.mark.asyncio
    async def test_lookup_posts_unit1_payload_and_renders_context_item(self):
        from seosoyoung_plugins.channel_observer.remiel_context import (
            RemielContextConfig,
            lookup_remiel_context,
            render_remiel_context_item,
        )

        captured: dict[str, object] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["api_key"] = request.headers.get("x-api-key")
            captured["payload"] = request.read().decode()
            return httpx.Response(200, json=_lookup_response())

        config = RemielContextConfig(
            base_url="http://remiel.test",
            api_key="secret",
            confidence_threshold=0.75,
            timeout=2.0,
        )
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(base_url=config.base_url, transport=transport) as client:
            payload = await lookup_remiel_context(
                config,
                channel_id="C_TEST",
                timestamps=["1001.000000", "1002.000000"],
                client=client,
            )

        assert captured["url"] == "http://remiel.test/api/interpretations/lookup"
        assert captured["api_key"] == "secret"
        assert '"channel_id":"C_TEST"' in captured["payload"]
        assert '"timestamps":["1001.000000","1002.000000"]' in captured["payload"]
        assert '"confidence_threshold":0.75' in captured["payload"]

        item = render_remiel_context_item(payload)
        assert item is not None
        assert item["key"] == "remiel_context"
        assert item["label"] == "remiel 해석 컨텍스트"
        assert "ready=1" in item["content"]
        assert "배포 설정을 확인해 달라는 요청" in item["content"]
        assert "missing_interpretation" in item["content"]

    @pytest.mark.asyncio
    async def test_lookup_fail_open_on_missing_config(self):
        from seosoyoung_plugins.channel_observer.remiel_context import (
            RemielContextConfig,
            build_remiel_context_item,
        )

        item = await build_remiel_context_item(
            RemielContextConfig(),
            channel_id="C_TEST",
            timestamps=["1001.000000"],
        )

        assert item is None

    @pytest.mark.asyncio
    async def test_lookup_fail_open_on_timeout(self, caplog):
        from seosoyoung_plugins.channel_observer.remiel_context import (
            RemielContextConfig,
            lookup_remiel_context,
        )

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("slow", request=request)

        config = RemielContextConfig(base_url="http://remiel.test", api_key="secret")
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(base_url=config.base_url, transport=transport) as client:
            with caplog.at_level(logging.WARNING):
                payload = await lookup_remiel_context(
                    config,
                    channel_id="C_TEST",
                    timestamps=["1001.000000"],
                    client=client,
                )

        assert payload is None
        assert "remiel lookup 실패" in caplog.text

    @pytest.mark.asyncio
    async def test_lookup_fail_open_on_non_2xx(self):
        from seosoyoung_plugins.channel_observer.remiel_context import (
            RemielContextConfig,
            lookup_remiel_context,
        )

        config = RemielContextConfig(base_url="http://remiel.test", api_key="secret")
        transport = httpx.MockTransport(lambda request: httpx.Response(503, json={"error": "down"}))
        async with httpx.AsyncClient(base_url=config.base_url, transport=transport) as client:
            payload = await lookup_remiel_context(
                config,
                channel_id="C_TEST",
                timestamps=["1001.000000"],
                client=client,
            )

        assert payload is None

    @pytest.mark.parametrize(
        "payload",
        [
            _lookup_response(channel_enabled=False),
            _lookup_response(ready=0),
        ],
    )
    def test_render_omits_disabled_or_empty_ready_payload(self, payload):
        from seosoyoung_plugins.channel_observer.remiel_context import render_remiel_context_item

        assert render_remiel_context_item(payload) is None

    @pytest.mark.asyncio
    async def test_build_context_item_fail_open_on_malformed_payload(self, monkeypatch):
        from seosoyoung_plugins.channel_observer import remiel_context
        from seosoyoung_plugins.channel_observer.remiel_context import (
            RemielContextConfig,
            build_remiel_context_item,
        )

        async def malformed_lookup(*args, **kwargs):
            return {
                "channel_id": "C_TEST",
                "channel_enabled": True,
                "coverage": {"ready": "not-a-number"},
                "items": [],
            }

        monkeypatch.setattr(remiel_context, "lookup_remiel_context", malformed_lookup)

        item = await build_remiel_context_item(
            RemielContextConfig(base_url="http://remiel.test", api_key="secret"),
            channel_id="C_TEST",
            timestamps=["1001.000000"],
        )

        assert item is None


class TestPipelineRemielContextInjection:
    @pytest.fixture
    def fake_store(self):
        store = MagicMock()
        store.get_digest.return_value = None
        store.load_judged.return_value = []
        store.append_judged = MagicMock()
        return store

    @pytest.fixture
    def action(self):
        return InterventionAction(type="message", target="channel", content="reason")

    @pytest.fixture
    def pending_messages(self):
        return [
            {"ts": "1001.000000", "user": "U1", "text": "이전 메시지"},
            {"ts": "1002.000000", "user": "U2", "text": "트리거"},
        ]

    @pytest.mark.asyncio
    async def test_execute_intervene_injects_remiel_context_from_same_history(
        self, mock_plugin_sdk, fake_store, action, pending_messages, monkeypatch,
    ):
        from seosoyoung_plugins.channel_observer import pipeline
        from seosoyoung_plugins.channel_observer.remiel_context import RemielContextConfig

        history_newest_first = [
            Message(ts="1002.000000", text="트리거", user="U2"),
            Message(ts="1001.000000", text="이전 메시지", user="U1"),
        ]
        mock_plugin_sdk["slack"].get_channel_history = AsyncMock(return_value=history_newest_first)
        mock_plugin_sdk["soulstream"].run = AsyncMock(
            return_value=RunResult(
                ok=True,
                status=RunStatus.COMPLETED,
                output="",
                utterances=["좋아요, 제가 설정을 볼게요."],
            )
        )
        build_mock = AsyncMock(
            return_value={
                "key": "remiel_context",
                "label": "remiel 해석 컨텍스트",
                "content": "remiel ready=2",
            }
        )
        monkeypatch.setattr(pipeline, "build_remiel_context_item", build_mock)

        config = RemielContextConfig(base_url="http://remiel.test", api_key="secret")
        await pipeline._execute_intervene(
            store=fake_store,
            channel_id="C_TEST",
            action=action,
            pending_messages=pending_messages,
            bot_user_id="U_BOT",
            remiel_config=config,
        )

        build_mock.assert_awaited_once_with(
            config,
            channel_id="C_TEST",
            timestamps=["1001.000000", "1002.000000"],
        )
        context = mock_plugin_sdk["soulstream"].run.call_args.kwargs["context"]
        assert [item["key"] for item in context] == ["thread_context", "remiel_context"]
        assert "C_TEST:1001.000000" in context[0]["content"]

    @pytest.mark.asyncio
    async def test_execute_intervene_fail_open_without_remiel_item(
        self, mock_plugin_sdk, fake_store, action, pending_messages, monkeypatch,
    ):
        from seosoyoung_plugins.channel_observer import pipeline
        from seosoyoung_plugins.channel_observer.remiel_context import RemielContextConfig

        mock_plugin_sdk["slack"].get_channel_history = AsyncMock(
            return_value=[Message(ts="1001.000000", text="이전 메시지", user="U1")]
        )
        mock_plugin_sdk["soulstream"].run = AsyncMock(
            return_value=RunResult(
                ok=True,
                status=RunStatus.COMPLETED,
                output="",
                utterances=["기존 경로 유지"],
            )
        )
        monkeypatch.setattr(pipeline, "build_remiel_context_item", AsyncMock(return_value=None))

        await pipeline._execute_intervene(
            store=fake_store,
            channel_id="C_TEST",
            action=action,
            pending_messages=pending_messages,
            remiel_config=RemielContextConfig(base_url="http://remiel.test", api_key="secret"),
        )

        context = mock_plugin_sdk["soulstream"].run.call_args.kwargs["context"]
        assert [item["key"] for item in context] == ["thread_context"]


class TestPluginRemielConfigPropagation:
    @pytest.mark.asyncio
    async def test_plugin_defaults_remiel_config(self):
        p = ChannelObserverPlugin()
        await p.on_load(SAMPLE_CONFIG)

        assert p._remiel_config.base_url == ""
        assert p._remiel_config.api_key == ""
        assert p._remiel_config.confidence_threshold == 0.75
        assert p._remiel_config.timeout == 2.0

    @pytest.mark.asyncio
    async def test_plugin_reads_remiel_config(self):
        p = ChannelObserverPlugin()
        await p.on_load({
            **SAMPLE_CONFIG,
            "remiel_base_url": "http://remiel.test",
            "remiel_api_key": "secret",
            "remiel_confidence_threshold": 0.8,
            "remiel_lookup_timeout": 3.5,
        })

        assert p._remiel_config.base_url == "http://remiel.test"
        assert p._remiel_config.api_key == "secret"
        assert p._remiel_config.confidence_threshold == 0.8
        assert p._remiel_config.timeout == 3.5

    @pytest.mark.asyncio
    async def test_plugin_passes_remiel_config_to_direct_pipeline(self):
        p = ChannelObserverPlugin()
        await p.on_load({**SAMPLE_CONFIG, "remiel_base_url": "http://remiel.test", "remiel_api_key": "secret"})
        p._store = MagicMock()
        p._store.count_pending_tokens.return_value = 200
        p._observer_engine = MagicMock()
        p._cooldown = MagicMock()
        p._bot_user_id = "U_BOT"

        with pytest.MonkeyPatch.context() as mp:
            mock_pipeline = AsyncMock()
            mp.setattr(
                "seosoyoung_plugins.channel_observer.pipeline.run_channel_pipeline",
                mock_pipeline,
            )
            p._maybe_trigger_digest("C_TEST")
            import time
            time.sleep(0.3)

        assert mock_pipeline.called
        assert mock_pipeline.call_args.kwargs["remiel_config"] is p._remiel_config

    @pytest.mark.asyncio
    async def test_plugin_passes_remiel_config_to_scheduler(self):
        p = ChannelObserverPlugin()
        await p.on_load({**SAMPLE_CONFIG, "remiel_base_url": "http://remiel.test", "remiel_api_key": "secret"})
        mock_scheduler_cls = MagicMock()

        with (
            pytest.MonkeyPatch.context() as mp,
        ):
            mp.setattr("seosoyoung_plugins.channel_observer.store.ChannelStore", MagicMock())
            mp.setattr("seosoyoung_plugins.channel_observer.collector.ChannelMessageCollector", MagicMock())
            mp.setattr("seosoyoung_plugins.channel_observer.intervention.InterventionHistory", MagicMock())
            mp.setattr("seosoyoung_plugins.channel_observer.observer.ChannelObserver", MagicMock())
            mp.setattr("seosoyoung_plugins.channel_observer.observer.DigestCompressor", MagicMock())
            mp.setattr("seosoyoung_plugins.channel_observer.scheduler.ChannelDigestScheduler", mock_scheduler_cls)

            from seosoyoung.plugin_sdk import HookContext

            hooks = p.register_hooks()
            await hooks["on_startup"](HookContext(hook_name="on_startup", args={"slack_client": MagicMock()}))

        assert mock_scheduler_cls.call_args.kwargs["remiel_config"] is p._remiel_config
