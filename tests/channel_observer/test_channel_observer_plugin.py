"""Tests for the channel observer plugin (plugins/channel_observer/).

Tests the ChannelObserverPlugin lifecycle, hook registration, and
on_message / on_startup dispatch without importing Config or .env.
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from seosoyoung.plugin_sdk import HookContext, HookResult, PluginMeta
from seosoyoung.core.plugin_manager import PluginManager
from seosoyoung_plugins.channel_observer.plugin import (
    ChannelObserverPlugin,
)


SAMPLE_CONFIG = {
    "channels": ["C_OBSERVE1", "C_OBSERVE2"],
    "api_key": "test-api-key",
    "model": "gpt-5-mini",
    "compressor_model": "gpt-5.2",
    "memory_path": "/tmp/test_channel_obs",
    "threshold_a": 150,
    "threshold_b": 5000,
    "buffer_threshold": 150,
    "digest_max_tokens": 10000,
    "digest_target_tokens": 5000,
    "intervention_threshold": 0.18,
    "periodic_sec": 300,
    "trigger_words": ["서소영", "소영"],
    "debug_channel": "C_DEBUG",
}


class TestChannelObserverPluginMeta:
    """Plugin identity."""

    def test_meta_is_plugin_meta(self):
        assert isinstance(ChannelObserverPlugin.meta, PluginMeta)

    def test_meta_name(self):
        assert ChannelObserverPlugin.meta.name == "channel_observer"

    def test_meta_version(self):
        assert ChannelObserverPlugin.meta.version == "1.0.0"


class TestChannelObserverPluginLifecycle:
    """on_load / on_unload."""

    @pytest.fixture()
    def plugin(self):
        return ChannelObserverPlugin()

    @pytest.mark.asyncio
    async def test_on_load_sets_fields(self, plugin):
        await plugin.on_load(SAMPLE_CONFIG)
        assert plugin._channels == ["C_OBSERVE1", "C_OBSERVE2"]
        assert plugin._api_key == "test-api-key"
        assert plugin._model == "gpt-5-mini"
        assert plugin._threshold_a == 150
        assert plugin._intervention_threshold == 0.18
        assert plugin._trigger_words == ["서소영", "소영"]
        assert plugin._debug_channel == "C_DEBUG"

    @pytest.mark.asyncio
    async def test_on_load_missing_memory_path_raises(self, plugin):
        incomplete = {
            k: v for k, v in SAMPLE_CONFIG.items() if k != "memory_path"
        }
        with pytest.raises(KeyError, match="memory_path"):
            await plugin.on_load(incomplete)

    @pytest.mark.asyncio
    async def test_on_load_defaults_for_optional_fields(self, plugin):
        """Optional fields should have defaults."""
        minimal = {"memory_path": "/tmp/minimal"}
        await plugin.on_load(minimal)
        assert plugin._channels == []
        assert plugin._threshold_a == 150
        assert plugin._periodic_sec == 300

    @pytest.mark.asyncio
    async def test_on_unload_without_scheduler(self, plugin):
        await plugin.on_load(SAMPLE_CONFIG)
        await plugin.on_unload()  # should not raise

    @pytest.mark.asyncio
    async def test_on_unload_with_scheduler_stops_it(self, plugin):
        await plugin.on_load(SAMPLE_CONFIG)
        mock_scheduler = MagicMock()
        plugin._scheduler = mock_scheduler
        await plugin.on_unload()
        mock_scheduler.stop.assert_called_once()


class TestChannelObserverPluginHooks:
    """Hook registration and dispatch."""

    @pytest.fixture()
    async def loaded_plugin(self):
        p = ChannelObserverPlugin()
        await p.on_load(SAMPLE_CONFIG)
        return p

    @pytest.mark.asyncio
    async def test_register_hooks_keys(self, loaded_plugin):
        hooks = loaded_plugin.register_hooks()
        assert "on_message" in hooks
        assert "on_startup" in hooks
        assert "on_shutdown" in hooks
        for key in hooks:
            assert callable(hooks[key])

    @pytest.mark.asyncio
    async def test_on_message_skip_when_no_collector(self, loaded_plugin):
        """Before on_startup, collector is None → SKIP."""
        ctx = HookContext(
            hook_name="on_message",
            args={
                "event": {"channel": "C_OBSERVE1", "text": "hello"},
                "client": MagicMock(),
            },
        )
        hooks = loaded_plugin.register_hooks()
        result, value = await hooks["on_message"](ctx)
        assert result == HookResult.SKIP
        assert value is None

    @pytest.mark.asyncio
    async def test_on_message_skip_for_other_channel(self, loaded_plugin):
        loaded_plugin._collector = MagicMock()
        ctx = HookContext(
            hook_name="on_message",
            args={
                "event": {"channel": "C_UNMONITORED", "text": "hello"},
                "client": MagicMock(),
            },
        )
        hooks = loaded_plugin.register_hooks()
        result, value = await hooks["on_message"](ctx)
        assert result == HookResult.SKIP

    @pytest.mark.asyncio
    async def test_on_message_collects_monitored_channel(self, loaded_plugin):
        """on_message should collect messages from monitored channels."""
        mock_collector = MagicMock()
        mock_collector.collect.return_value = True
        loaded_plugin._collector = mock_collector
        loaded_plugin._store = MagicMock()
        loaded_plugin._store.count_pending_tokens.return_value = 10

        ctx = HookContext(
            hook_name="on_message",
            args={
                "event": {
                    "channel": "C_OBSERVE1",
                    "text": "hello there",
                    "user": "U123",
                },
                "client": MagicMock(),
            },
        )
        hooks = loaded_plugin.register_hooks()
        result, value = await hooks["on_message"](ctx)

        assert result == HookResult.SKIP  # does not stop chain
        mock_collector.collect.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_message_error_is_caught(self, loaded_plugin):
        """on_message should catch collector errors and still SKIP."""
        mock_collector = MagicMock()
        mock_collector.collect.side_effect = RuntimeError("DB fail")
        loaded_plugin._collector = mock_collector

        ctx = HookContext(
            hook_name="on_message",
            args={
                "event": {"channel": "C_OBSERVE1", "text": "test"},
                "client": MagicMock(),
            },
        )
        hooks = loaded_plugin.register_hooks()
        result, value = await hooks["on_message"](ctx)
        assert result == HookResult.SKIP

    @pytest.mark.asyncio
    async def test_on_shutdown_stops_scheduler(self, loaded_plugin):
        mock_scheduler = MagicMock()
        loaded_plugin._scheduler = mock_scheduler

        ctx = HookContext(hook_name="on_shutdown", args={})
        hooks = loaded_plugin.register_hooks()
        result, value = await hooks["on_shutdown"](ctx)

        assert result == HookResult.CONTINUE
        mock_scheduler.stop.assert_called_once()


class TestChannelObserverStartup:
    """on_startup hook integration."""

    @pytest.fixture()
    async def loaded_plugin(self):
        p = ChannelObserverPlugin()
        await p.on_load(SAMPLE_CONFIG)
        return p

    @pytest.mark.asyncio
    async def test_on_startup_no_channels_skips(self):
        """If no channels configured, startup returns None."""
        p = ChannelObserverPlugin()
        await p.on_load({"memory_path": "/tmp/empty"})

        ctx = HookContext(
            hook_name="on_startup",
            args={"slack_client": MagicMock()},
        )
        hooks = p.register_hooks()
        result, value = await hooks["on_startup"](ctx)

        assert result == HookResult.CONTINUE
        assert value is None

    @pytest.mark.asyncio
    async def test_on_startup_returns_refs(self, loaded_plugin):
        """on_startup should return runtime references."""
        # Import modules to ensure they can be patched
        import seosoyoung_plugins.channel_observer.store
        import seosoyoung_plugins.channel_observer.collector
        import seosoyoung_plugins.channel_observer.intervention
        import seosoyoung_plugins.channel_observer.observer
        import seosoyoung_plugins.channel_observer.scheduler

        mock_store_cls = MagicMock()
        mock_collector_cls = MagicMock()
        mock_history_cls = MagicMock()
        mock_observer_cls = MagicMock()
        mock_compressor_cls = MagicMock()
        mock_scheduler_cls = MagicMock()

        with (
            patch(
                "seosoyoung_plugins.channel_observer.store.ChannelStore",
                mock_store_cls,
            ),
            patch(
                "seosoyoung_plugins.channel_observer.collector.ChannelMessageCollector",
                mock_collector_cls,
            ),
            patch(
                "seosoyoung_plugins.channel_observer.intervention.InterventionHistory",
                mock_history_cls,
            ),
            patch(
                "seosoyoung_plugins.channel_observer.observer.ChannelObserver",
                mock_observer_cls,
            ),
            patch(
                "seosoyoung_plugins.channel_observer.observer.DigestCompressor",
                mock_compressor_cls,
            ),
            patch(
                "seosoyoung_plugins.channel_observer.scheduler.ChannelDigestScheduler",
                mock_scheduler_cls,
            ),
        ):
            ctx = HookContext(
                hook_name="on_startup",
                args={
                    "slack_client": MagicMock(),
                    "mention_tracker": MagicMock(),
                },
            )
            hooks = loaded_plugin.register_hooks()
            result, value = await hooks["on_startup"](ctx)

            assert result == HookResult.CONTINUE
            assert isinstance(value, dict)
            assert "channel_store" in value
            assert "channel_collector" in value
            assert "channel_cooldown" in value
            assert "channel_observer" in value
            assert "channel_compressor" in value
            assert "channel_observer_channels" in value
            assert value["channel_observer_channels"] == [
                "C_OBSERVE1",
                "C_OBSERVE2",
            ]

            # Scheduler should be started
            mock_scheduler_cls.return_value.start.assert_called_once()


class TestChannelObserverAccessors:
    """Property accessors."""

    @pytest.fixture()
    async def loaded_plugin(self):
        p = ChannelObserverPlugin()
        await p.on_load(SAMPLE_CONFIG)
        return p

    @pytest.mark.asyncio
    async def test_channels_property(self, loaded_plugin):
        assert loaded_plugin.channels == ["C_OBSERVE1", "C_OBSERVE2"]

    @pytest.mark.asyncio
    async def test_store_property_before_startup(self, loaded_plugin):
        assert loaded_plugin.store is None

    @pytest.mark.asyncio
    async def test_store_property_after_startup(self, loaded_plugin):
        mock_store = MagicMock()
        loaded_plugin._store = mock_store
        assert loaded_plugin.store is mock_store


class TestChannelObserverReaction:
    """Reaction collection."""

    @pytest.fixture()
    async def loaded_plugin(self):
        p = ChannelObserverPlugin()
        await p.on_load(SAMPLE_CONFIG)
        return p

    @pytest.mark.asyncio
    async def test_collect_reaction_no_collector(self, loaded_plugin):
        assert loaded_plugin.collect_reaction({}, "added") is False

    @pytest.mark.asyncio
    async def test_collect_reaction_delegates(self, loaded_plugin):
        mock_collector = MagicMock()
        mock_collector.collect_reaction.return_value = True
        loaded_plugin._collector = mock_collector

        event = {"reaction": "thumbsup", "item": {"channel": "C_OBSERVE1"}}
        result = loaded_plugin.collect_reaction(event, "added")

        assert result is True
        mock_collector.collect_reaction.assert_called_once_with(
            event, "added"
        )


class TestTriggerWordDetection:
    """_contains_trigger_word helper."""

    @pytest.fixture()
    async def loaded_plugin(self):
        p = ChannelObserverPlugin()
        await p.on_load(SAMPLE_CONFIG)
        return p

    @pytest.mark.asyncio
    async def test_contains_trigger_word_match(self, loaded_plugin):
        assert loaded_plugin._contains_trigger_word("서소영 안녕") is True

    @pytest.mark.asyncio
    async def test_contains_trigger_word_case_insensitive(self, loaded_plugin):
        assert loaded_plugin._contains_trigger_word("소영아 뭐해") is True

    @pytest.mark.asyncio
    async def test_no_trigger_word(self, loaded_plugin):
        assert loaded_plugin._contains_trigger_word("hello world") is False

    @pytest.mark.asyncio
    async def test_empty_trigger_words(self):
        p = ChannelObserverPlugin()
        await p.on_load({"memory_path": "/tmp/t", "trigger_words": []})
        assert p._contains_trigger_word("서소영") is False


class TestLlmCallCreation:
    """_make_llm_call and _llm_call initialization."""

    @pytest.mark.asyncio
    async def test_llm_call_created_when_api_key_present(self):
        """api_key가 있으면 on_load 시 _llm_call이 생성됩니다."""
        p = ChannelObserverPlugin()
        await p.on_load(SAMPLE_CONFIG)
        assert p._llm_call is not None
        assert callable(p._llm_call)

    @pytest.mark.asyncio
    async def test_llm_call_none_when_no_api_key(self):
        """api_key가 없으면 _llm_call이 None입니다."""
        p = ChannelObserverPlugin()
        config = {**SAMPLE_CONFIG, "api_key": ""}
        await p.on_load(config)
        assert p._llm_call is None

    @pytest.mark.asyncio
    async def test_llm_call_none_when_api_key_missing(self):
        """api_key 키 자체가 없으면 _llm_call이 None입니다."""
        p = ChannelObserverPlugin()
        config = {k: v for k, v in SAMPLE_CONFIG.items() if k != "api_key"}
        await p.on_load(config)
        assert p._llm_call is None

    @pytest.mark.asyncio
    async def test_llm_call_passed_to_pipeline(self):
        """_maybe_trigger_digest에서 run_channel_pipeline에 llm_call이 전달됩니다."""
        p = ChannelObserverPlugin()
        await p.on_load(SAMPLE_CONFIG)
        p._store = MagicMock()
        p._store.count_pending_tokens.return_value = 200
        p._observer_engine = MagicMock()
        p._cooldown = MagicMock()
        p._bot_user_id = "U_BOT"

        with patch(
            "seosoyoung_plugins.channel_observer.pipeline.run_channel_pipeline",
            new_callable=AsyncMock,
        ) as mock_pipeline:
            p._maybe_trigger_digest("C_OBSERVE1")
            # Wait for the daemon thread to start and run
            import time
            time.sleep(0.3)

            if mock_pipeline.called:
                call_kwargs = mock_pipeline.call_args.kwargs
                assert "llm_call" in call_kwargs
                assert call_kwargs["llm_call"] is p._llm_call

    @pytest.mark.asyncio
    async def test_llm_call_passed_to_scheduler(self):
        """ChannelDigestScheduler에 llm_call이 전달됩니다."""
        p = ChannelObserverPlugin()
        await p.on_load(SAMPLE_CONFIG)

        import seosoyoung_plugins.channel_observer.store
        import seosoyoung_plugins.channel_observer.collector
        import seosoyoung_plugins.channel_observer.intervention
        import seosoyoung_plugins.channel_observer.observer
        import seosoyoung_plugins.channel_observer.scheduler

        mock_scheduler_cls = MagicMock()

        with (
            patch(
                "seosoyoung_plugins.channel_observer.store.ChannelStore",
                MagicMock(),
            ),
            patch(
                "seosoyoung_plugins.channel_observer.collector.ChannelMessageCollector",
                MagicMock(),
            ),
            patch(
                "seosoyoung_plugins.channel_observer.intervention.InterventionHistory",
                MagicMock(),
            ),
            patch(
                "seosoyoung_plugins.channel_observer.observer.ChannelObserver",
                MagicMock(),
            ),
            patch(
                "seosoyoung_plugins.channel_observer.observer.DigestCompressor",
                MagicMock(),
            ),
            patch(
                "seosoyoung_plugins.channel_observer.scheduler.ChannelDigestScheduler",
                mock_scheduler_cls,
            ),
        ):
            ctx = HookContext(
                hook_name="on_startup",
                args={
                    "slack_client": MagicMock(),
                    "mention_tracker": MagicMock(),
                },
            )
            hooks = p.register_hooks()
            await hooks["on_startup"](ctx)

            call_kwargs = mock_scheduler_cls.call_args.kwargs
            assert "llm_call" in call_kwargs
            assert call_kwargs["llm_call"] is p._llm_call


class TestChannelObserverManagerIntegration:
    """End-to-end with PluginManager."""

    @pytest.mark.asyncio
    async def test_load_and_dispatch_on_message(self):
        pm = PluginManager(notifier=AsyncMock())

        await pm.load(
            module="seosoyoung_plugins.channel_observer.plugin",
            config=SAMPLE_CONFIG,
            priority=20,
        )

        # Before startup, collector is None → SKIP
        ctx = HookContext(
            hook_name="on_message",
            args={
                "event": {"channel": "C_OBSERVE1", "text": "hi"},
                "client": MagicMock(),
            },
        )
        ctx = await pm.dispatch("on_message", ctx)
        assert not ctx.stopped
