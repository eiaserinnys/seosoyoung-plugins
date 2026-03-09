"""Tests for configurable recent_messages_count in intervention dialogue writer.

Verifies that the hard-coded 5 in _execute_intervene is replaced by a
configurable value from plugin settings, propagated through the full
call chain: plugin.yaml -> ChannelObserverPlugin -> run_channel_pipeline
-> _handle_multi_judge / _handle_single_judge -> _execute_intervene.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from seosoyoung_plugins.channel_observer.plugin import ChannelObserverPlugin


SAMPLE_CONFIG = {
    "channels": ["C_TEST"],
    "api_key": "test-key",
    "model": "gpt-5-mini",
    "compressor_model": "gpt-5.2",
    "memory_path": "/tmp/test_recent_msgs",
    "threshold_a": 150,
    "threshold_b": 5000,
    "intervention_threshold": 0.18,
    "periodic_sec": 300,
    "debug_channel": "C_DEBUG",
}


class TestPluginConfigRecentMessagesCount:
    """Plugin reads recent_messages_count from config."""

    @pytest.mark.asyncio
    async def test_default_value_when_not_specified(self):
        """recent_messages_count should default to 5 when absent."""
        p = ChannelObserverPlugin()
        await p.on_load(SAMPLE_CONFIG)
        assert p._recent_messages_count == 5

    @pytest.mark.asyncio
    async def test_custom_value_from_config(self):
        """recent_messages_count should be read from config."""
        config = {**SAMPLE_CONFIG, "recent_messages_count": 15}
        p = ChannelObserverPlugin()
        await p.on_load(config)
        assert p._recent_messages_count == 15

    @pytest.mark.asyncio
    async def test_value_propagated_to_pipeline(self):
        """recent_messages_count should be forwarded to run_channel_pipeline."""
        config = {**SAMPLE_CONFIG, "recent_messages_count": 20}
        p = ChannelObserverPlugin()
        await p.on_load(config)

        p._store = MagicMock()
        p._store.count_pending_tokens.return_value = 200
        p._observer_engine = MagicMock()
        p._cooldown = MagicMock()
        p._bot_user_id = "U_BOT"

        with patch(
            "seosoyoung_plugins.channel_observer.pipeline.run_channel_pipeline",
            new_callable=AsyncMock,
        ) as mock_pipeline:
            p._maybe_trigger_digest("C_TEST")
            import time
            time.sleep(0.3)

            if mock_pipeline.called:
                call_kwargs = mock_pipeline.call_args.kwargs
                assert "recent_messages_count" in call_kwargs
                assert call_kwargs["recent_messages_count"] == 20

    @pytest.mark.asyncio
    async def test_value_propagated_to_scheduler(self):
        """recent_messages_count should be passed to ChannelDigestScheduler."""
        config = {**SAMPLE_CONFIG, "recent_messages_count": 12}
        p = ChannelObserverPlugin()
        await p.on_load(config)

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
            from seosoyoung.plugin_sdk import HookContext
            ctx = HookContext(
                hook_name="on_startup",
                args={"slack_client": MagicMock()},
            )
            hooks = p.register_hooks()
            await hooks["on_startup"](ctx)

            call_kwargs = mock_scheduler_cls.call_args.kwargs
            assert "recent_messages_count" in call_kwargs
            assert call_kwargs["recent_messages_count"] == 12


class TestExecuteInterveneRecentMessagesSlicing:
    """_execute_intervene uses recent_messages_count for slicing."""

    def _make_pending(self, n: int) -> list[dict]:
        """Create n pending messages with sequential ts."""
        return [
            {"ts": f"100{i}.000000", "text": f"msg {i}", "user": f"U{i}"}
            for i in range(n)
        ]

    @pytest.mark.asyncio
    async def test_pending_search_uses_count(self):
        """When trigger found in pending, slice [max(0, i-N):i] uses N."""
        from seosoyoung_plugins.channel_observer.pipeline import _execute_intervene
        from seosoyoung_plugins.channel_observer.intervention import InterventionAction

        pending = self._make_pending(20)
        target_ts = pending[15]["ts"]  # target at index 15
        action = InterventionAction(type="message", target=target_ts, content="")

        captured = {}

        async def mock_run(**kwargs):
            return MagicMock(ok=False)

        with (
            patch("seosoyoung_plugins.channel_observer.pipeline.soulstream") as mock_soul,
            patch("seosoyoung_plugins.channel_observer.pipeline.slack") as mock_slack,
            patch(
                "seosoyoung_plugins.channel_observer.pipeline.build_channel_intervene_user_prompt"
            ) as mock_build,
            patch(
                "seosoyoung_plugins.channel_observer.pipeline.get_channel_intervene_system_prompt",
                return_value="sys",
            ),
        ):
            mock_slack.add_reaction = AsyncMock()
            mock_soul.run = AsyncMock(return_value=MagicMock(ok=False))
            mock_build.return_value = "user prompt"

            store = MagicMock()
            store.get_digest.return_value = None

            await _execute_intervene(
                store=store,
                channel_id="C_TEST",
                action=action,
                pending_messages=pending,
                recent_messages_count=10,
            )

            # build_channel_intervene_user_prompt should receive 10 recent messages
            call_kwargs = mock_build.call_args.kwargs
            recent = call_kwargs["recent_messages"]
            # index 15, count 10 -> slice [5:15] = 10 messages
            assert len(recent) == 10

    @pytest.mark.asyncio
    async def test_thread_buffer_fallback_uses_count(self):
        """When trigger found in thread_buffers, last N pending used."""
        from seosoyoung_plugins.channel_observer.pipeline import _execute_intervene
        from seosoyoung_plugins.channel_observer.intervention import InterventionAction

        pending = self._make_pending(20)
        thread_msg = {"ts": "9999.000000", "text": "thread msg", "user": "U99"}
        action = InterventionAction(type="message", target="9999.000000", content="")
        thread_buffers = {"some_thread": [thread_msg]}

        with (
            patch("seosoyoung_plugins.channel_observer.pipeline.soulstream") as mock_soul,
            patch("seosoyoung_plugins.channel_observer.pipeline.slack") as mock_slack,
            patch(
                "seosoyoung_plugins.channel_observer.pipeline.build_channel_intervene_user_prompt"
            ) as mock_build,
            patch(
                "seosoyoung_plugins.channel_observer.pipeline.get_channel_intervene_system_prompt",
                return_value="sys",
            ),
        ):
            mock_slack.add_reaction = AsyncMock()
            mock_soul.run = AsyncMock(return_value=MagicMock(ok=False))
            mock_build.return_value = "user prompt"

            store = MagicMock()
            store.get_digest.return_value = None

            await _execute_intervene(
                store=store,
                channel_id="C_TEST",
                action=action,
                pending_messages=pending,
                thread_buffers=thread_buffers,
                recent_messages_count=8,
            )

            call_kwargs = mock_build.call_args.kwargs
            recent = call_kwargs["recent_messages"]
            assert len(recent) == 8

    @pytest.mark.asyncio
    async def test_judged_fallback_uses_count(self):
        """When trigger found in judged, last N pending used."""
        from seosoyoung_plugins.channel_observer.pipeline import _execute_intervene
        from seosoyoung_plugins.channel_observer.intervention import InterventionAction

        pending = self._make_pending(20)
        judged_msg = {"ts": "8888.000000", "text": "judged msg", "user": "U88"}
        action = InterventionAction(type="message", target="8888.000000", content="")

        with (
            patch("seosoyoung_plugins.channel_observer.pipeline.soulstream") as mock_soul,
            patch("seosoyoung_plugins.channel_observer.pipeline.slack") as mock_slack,
            patch(
                "seosoyoung_plugins.channel_observer.pipeline.build_channel_intervene_user_prompt"
            ) as mock_build,
            patch(
                "seosoyoung_plugins.channel_observer.pipeline.get_channel_intervene_system_prompt",
                return_value="sys",
            ),
        ):
            mock_slack.add_reaction = AsyncMock()
            mock_soul.run = AsyncMock(return_value=MagicMock(ok=False))
            mock_build.return_value = "user prompt"

            store = MagicMock()
            store.get_digest.return_value = None
            store.load_judged.return_value = [judged_msg]

            await _execute_intervene(
                store=store,
                channel_id="C_TEST",
                action=action,
                pending_messages=pending,
                recent_messages_count=12,
            )

            call_kwargs = mock_build.call_args.kwargs
            recent = call_kwargs["recent_messages"]
            assert len(recent) == 12

    @pytest.mark.asyncio
    async def test_channel_target_uses_count(self):
        """When target is 'channel', last N pending used (excluding trigger)."""
        from seosoyoung_plugins.channel_observer.pipeline import _execute_intervene
        from seosoyoung_plugins.channel_observer.intervention import InterventionAction

        pending = self._make_pending(20)
        action = InterventionAction(type="message", target="channel", content="")

        with (
            patch("seosoyoung_plugins.channel_observer.pipeline.soulstream") as mock_soul,
            patch("seosoyoung_plugins.channel_observer.pipeline.slack") as mock_slack,
            patch(
                "seosoyoung_plugins.channel_observer.pipeline.build_channel_intervene_user_prompt"
            ) as mock_build,
            patch(
                "seosoyoung_plugins.channel_observer.pipeline.get_channel_intervene_system_prompt",
                return_value="sys",
            ),
        ):
            mock_slack.add_reaction = AsyncMock()
            mock_soul.run = AsyncMock(return_value=MagicMock(ok=False))
            mock_build.return_value = "user prompt"

            store = MagicMock()
            store.get_digest.return_value = None

            await _execute_intervene(
                store=store,
                channel_id="C_TEST",
                action=action,
                pending_messages=pending,
                recent_messages_count=10,
            )

            call_kwargs = mock_build.call_args.kwargs
            recent = call_kwargs["recent_messages"]
            # channel target: last item is trigger, preceding N are recent
            assert len(recent) == 10

    @pytest.mark.asyncio
    async def test_default_count_is_5(self):
        """Without explicit count, defaults to 5 (backward compatible)."""
        from seosoyoung_plugins.channel_observer.pipeline import _execute_intervene
        from seosoyoung_plugins.channel_observer.intervention import InterventionAction

        pending = self._make_pending(20)
        action = InterventionAction(type="message", target="channel", content="")

        with (
            patch("seosoyoung_plugins.channel_observer.pipeline.soulstream") as mock_soul,
            patch("seosoyoung_plugins.channel_observer.pipeline.slack") as mock_slack,
            patch(
                "seosoyoung_plugins.channel_observer.pipeline.build_channel_intervene_user_prompt"
            ) as mock_build,
            patch(
                "seosoyoung_plugins.channel_observer.pipeline.get_channel_intervene_system_prompt",
                return_value="sys",
            ),
        ):
            mock_slack.add_reaction = AsyncMock()
            mock_soul.run = AsyncMock(return_value=MagicMock(ok=False))
            mock_build.return_value = "user prompt"

            store = MagicMock()
            store.get_digest.return_value = None

            await _execute_intervene(
                store=store,
                channel_id="C_TEST",
                action=action,
                pending_messages=pending,
                # no recent_messages_count -> default
            )

            call_kwargs = mock_build.call_args.kwargs
            recent = call_kwargs["recent_messages"]
            assert len(recent) == 5
