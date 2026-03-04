"""Tests for the translate plugin (plugins/translate/).

Tests the TranslatePlugin lifecycle, hook registration, and
message dispatch using plugin_sdk.slack backend (not raw WebClient).
"""

import pytest
from unittest.mock import patch

from seosoyoung.plugin_sdk import HookContext, HookResult, PluginMeta
from seosoyoung.plugin_sdk import slack
from seosoyoung.plugin_sdk.slack import (
    Message,
    ReactionResult,
    SendMessageResult,
    UserInfo,
)
from seosoyoung.core.plugin_manager import PluginManager
from seosoyoung_plugins.translate.plugin import TranslatePlugin
from seosoyoung_plugins.translate.detector import Language


SAMPLE_CONFIG = {
    "api_key": "test-anthropic-key",
    "openai_api_key": "test-openai-key",
    "glossary_path": "/tmp/glossary.yaml",
    "channels": ["C_TRANSLATE"],
    "backend": "openai",
    "model": "claude-sonnet-4-20250514",
    "openai_model": "gpt-5-mini",
    "context_count": 5,
    "show_glossary": False,
    "show_cost": True,
    "debug_channel": "C_DEBUG",
}


class _FakeSlackBackend:
    """Minimal in-memory Slack backend for tests."""

    def __init__(self):
        self.reactions_added = []
        self.reactions_removed = []
        self.messages_sent = []
        self.user_info_map = {}

    async def send_message(self, channel, text, thread_ts=None, **kwargs):
        self.messages_sent.append(
            {"channel": channel, "text": text, "thread_ts": thread_ts}
        )
        return SendMessageResult(ok=True, ts="1234.9999", channel=channel)

    async def update_message(self, channel, ts, text, **kwargs):
        return SendMessageResult(ok=True, ts=ts, channel=channel)

    async def add_reaction(self, channel, ts, emoji):
        self.reactions_added.append(
            {"channel": channel, "ts": ts, "emoji": emoji}
        )
        return ReactionResult(ok=True)

    async def remove_reaction(self, channel, ts, emoji):
        self.reactions_removed.append(
            {"channel": channel, "ts": ts, "emoji": emoji}
        )
        return ReactionResult(ok=True)

    async def get_user_info(self, user_id):
        return self.user_info_map.get(user_id)

    async def get_thread_replies(self, channel, thread_ts, limit=100):
        return []

    async def get_channel_history(self, channel, limit=100):
        return []

    async def open_dm(self, user_id):
        return None


@pytest.fixture(autouse=True)
def fake_slack_backend():
    """Install and clean up fake Slack backend for every test."""
    backend = _FakeSlackBackend()
    slack.set_backend(backend)
    yield backend
    slack.set_backend(None)


class TestTranslatePluginMeta:
    """Plugin identity."""

    def test_meta_is_plugin_meta(self):
        assert isinstance(TranslatePlugin.meta, PluginMeta)

    def test_meta_name(self):
        assert TranslatePlugin.meta.name == "translate"

    def test_meta_version(self):
        assert TranslatePlugin.meta.version == "1.1.0"


class TestTranslatePluginLifecycle:
    """on_load / on_unload."""

    @pytest.fixture()
    def plugin(self):
        return TranslatePlugin()

    @pytest.mark.asyncio
    async def test_on_load_sets_fields(self, plugin):
        await plugin.on_load(SAMPLE_CONFIG)
        assert plugin._channels == ["C_TRANSLATE"]
        assert plugin._backend == "openai"
        assert plugin._api_key == "test-anthropic-key"
        assert plugin._openai_api_key == "test-openai-key"
        assert plugin._context_count == 5
        assert plugin._show_glossary is False
        assert plugin._show_cost is True

    @pytest.mark.asyncio
    async def test_on_load_missing_key_raises(self, plugin):
        incomplete = {k: v for k, v in SAMPLE_CONFIG.items() if k != "api_key"}
        with pytest.raises(KeyError, match="api_key"):
            await plugin.on_load(incomplete)

    @pytest.mark.asyncio
    async def test_on_unload_succeeds(self, plugin):
        await plugin.on_load(SAMPLE_CONFIG)
        await plugin.on_unload()  # should not raise


class TestTranslatePluginHooks:
    """Hook registration and dispatch."""

    @pytest.fixture()
    async def loaded_plugin(self):
        p = TranslatePlugin()
        await p.on_load(SAMPLE_CONFIG)
        return p

    @pytest.mark.asyncio
    async def test_register_hooks_returns_on_message(self, loaded_plugin):
        hooks = loaded_plugin.register_hooks()
        assert "on_message" in hooks
        assert callable(hooks["on_message"])

    @pytest.mark.asyncio
    async def test_on_message_skip_for_other_channel(self, loaded_plugin):
        ctx = HookContext(
            hook_name="on_message",
            args={"event": {"channel": "C_OTHER", "text": "hello"}},
        )
        hooks = loaded_plugin.register_hooks()
        result, value = await hooks["on_message"](ctx)
        assert result == HookResult.SKIP

    @pytest.mark.asyncio
    async def test_on_message_stop_for_translate_channel(
        self, loaded_plugin, fake_slack_backend
    ):
        """Matching channel triggers translation (mocked) and returns STOP."""
        fake_slack_backend.user_info_map["U123"] = UserInfo(
            id="U123",
            name="testuser",
            real_name="Test User",
            display_name="Test User",
        )

        event = {
            "channel": "C_TRANSLATE",
            "text": "Hello world",
            "user": "U123",
            "ts": "1234.5678",
        }

        ctx = HookContext(
            hook_name="on_message",
            args={"event": event},
        )

        hooks = loaded_plugin.register_hooks()
        with patch(
            "seosoyoung_plugins.translate.plugin.translate"
        ) as mock_translate:
            mock_translate.return_value = ("안녕 세계", 0.001, [], None)
            result, value = await hooks["on_message"](ctx)

        assert result == HookResult.STOP
        assert value is True

        # Verify reactions were added/removed via backend
        emoji_names = [r["emoji"] for r in fake_slack_backend.reactions_added]
        assert "hn-curious" in emoji_names
        assert "hn_deal_rainbow" in emoji_names
        assert any(
            r["emoji"] == "hn-curious"
            for r in fake_slack_backend.reactions_removed
        )

        # Verify message was sent
        assert len(fake_slack_backend.messages_sent) >= 1
        assert fake_slack_backend.messages_sent[0]["channel"] == "C_TRANSLATE"

    @pytest.mark.asyncio
    async def test_on_message_skip_bot_message(self, loaded_plugin):
        """Bot messages in translate channel should not be translated."""
        event = {
            "channel": "C_TRANSLATE",
            "text": "bot reply",
            "bot_id": "B123",
            "ts": "1234.5678",
        }
        ctx = HookContext(
            hook_name="on_message",
            args={"event": event},
        )
        hooks = loaded_plugin.register_hooks()
        result, value = await hooks["on_message"](ctx)
        # _process_translate returns False for bot messages -> SKIP
        assert result == HookResult.SKIP

    @pytest.mark.asyncio
    async def test_on_message_no_client_in_context_required(
        self, loaded_plugin
    ):
        """Verify that ctx.args does NOT need 'client' key."""
        event = {
            "channel": "C_OTHER",
            "text": "test",
        }
        ctx = HookContext(
            hook_name="on_message",
            args={"event": event},
        )
        hooks = loaded_plugin.register_hooks()
        # Should not raise KeyError for missing 'client'
        result, _ = await hooks["on_message"](ctx)
        assert result == HookResult.SKIP


class TestTranslatePluginSlackBackendUsage:
    """Verify plugin correctly uses plugin_sdk.slack functions."""

    @pytest.fixture()
    async def loaded_plugin(self):
        p = TranslatePlugin()
        await p.on_load(SAMPLE_CONFIG)
        return p

    @pytest.mark.asyncio
    async def test_get_user_display_name(
        self, loaded_plugin, fake_slack_backend
    ):
        fake_slack_backend.user_info_map["U999"] = UserInfo(
            id="U999",
            name="john",
            real_name="John Doe",
            display_name="Johnny",
        )
        name = await loaded_plugin._get_user_display_name("U999")
        assert name == "Johnny"

    @pytest.mark.asyncio
    async def test_get_user_display_name_fallback_to_real_name(
        self, loaded_plugin, fake_slack_backend
    ):
        fake_slack_backend.user_info_map["U999"] = UserInfo(
            id="U999",
            name="john",
            real_name="John Doe",
            display_name="",
        )
        name = await loaded_plugin._get_user_display_name("U999")
        assert name == "John Doe"

    @pytest.mark.asyncio
    async def test_get_user_display_name_fallback_to_user_id(
        self, loaded_plugin
    ):
        """Unknown user returns user_id as fallback."""
        name = await loaded_plugin._get_user_display_name("UUNKNOWN")
        assert name == "UUNKNOWN"

    @pytest.mark.asyncio
    async def test_get_context_messages_channel(
        self, loaded_plugin, fake_slack_backend
    ):
        """Channel history messages are reversed to chronological order."""
        fake_slack_backend.user_info_map["U1"] = UserInfo(
            id="U1", name="alice", display_name="Alice"
        )

        # Override get_channel_history to return messages
        async def mock_history(channel, limit=100):
            return [
                Message(
                    ts="3.0", text="newest", user="U1", channel=channel
                ),
                Message(
                    ts="2.0", text="middle", user="U1", channel=channel
                ),
                Message(
                    ts="1.0", text="oldest", user="U1", channel=channel
                ),
            ]

        fake_slack_backend.get_channel_history = mock_history

        msgs = await loaded_plugin._get_context_messages("C_TEST", None, 3)
        # Should be in chronological order (oldest first)
        assert len(msgs) == 3
        assert msgs[0]["text"] == "oldest"
        assert msgs[-1]["text"] == "newest"
        assert msgs[0]["user"] == "Alice"

    @pytest.mark.asyncio
    async def test_send_debug_log_skips_without_debug_channel(
        self, loaded_plugin, fake_slack_backend
    ):
        """Debug log is not sent when debug_channel is empty."""
        loaded_plugin._debug_channel = ""
        await loaded_plugin._send_debug_log("test", Language.KOREAN, None)
        assert len(fake_slack_backend.messages_sent) == 0

    @pytest.mark.asyncio
    async def test_send_debug_log_skips_without_match_result(
        self, loaded_plugin, fake_slack_backend
    ):
        """Debug log is not sent when match_result is None."""
        await loaded_plugin._send_debug_log("test", Language.KOREAN, None)
        assert len(fake_slack_backend.messages_sent) == 0

    @pytest.mark.asyncio
    async def test_translation_failure_sends_error_message(
        self, loaded_plugin, fake_slack_backend
    ):
        """Translation failure sends error message and failure reaction."""
        event = {
            "channel": "C_TRANSLATE",
            "text": "Hello world",
            "user": "U123",
            "ts": "1234.5678",
        }

        with patch(
            "seosoyoung_plugins.translate.plugin.translate"
        ) as mock_translate:
            mock_translate.side_effect = RuntimeError("API error")
            result = await loaded_plugin._process_translate(event)

        assert result is False

        # Failure reaction should be added
        emoji_names = [
            r["emoji"] for r in fake_slack_backend.reactions_added
        ]
        assert "hn-embarrass" in emoji_names

        # Error message should be sent
        error_msgs = [
            m
            for m in fake_slack_backend.messages_sent
            if "번역 실패" in m["text"]
        ]
        assert len(error_msgs) == 1

    @pytest.mark.asyncio
    async def test_thread_message_response_has_thread_ts(
        self, loaded_plugin, fake_slack_backend
    ):
        """Thread messages get replied in the same thread."""
        fake_slack_backend.user_info_map["U123"] = UserInfo(
            id="U123", name="test", display_name="Tester"
        )

        event = {
            "channel": "C_TRANSLATE",
            "text": "Hello",
            "user": "U123",
            "ts": "1234.5678",
            "thread_ts": "1234.0000",
        }

        with patch(
            "seosoyoung_plugins.translate.plugin.translate"
        ) as mock_translate:
            mock_translate.return_value = ("안녕", 0.001, [], None)
            await loaded_plugin._process_translate(event)

        # Response should be in the thread
        response_msgs = [
            m
            for m in fake_slack_backend.messages_sent
            if "번역 실패" not in m["text"]
        ]
        assert len(response_msgs) >= 1
        assert response_msgs[0]["thread_ts"] == "1234.0000"


class TestTranslatePluginManagerIntegration:
    """Integration test: load TranslatePlugin via PluginManager."""

    @pytest.mark.asyncio
    async def test_load_and_dispatch(self):
        pm = PluginManager()
        plugin = await pm.load(
            module="seosoyoung_plugins.translate.plugin",
            config=SAMPLE_CONFIG,
            priority=50,
        )
        assert plugin.meta.name == "translate"
        assert "translate" in pm.plugins

        # Dispatch to non-matching channel -> no STOP
        ctx = HookContext(
            hook_name="on_message",
            args={"event": {"channel": "C_OTHER", "text": "hi"}},
        )
        ctx = await pm.dispatch("on_message", ctx)
        assert not ctx.stopped

    @pytest.mark.asyncio
    async def test_unload(self):
        pm = PluginManager()
        await pm.load(
            module="seosoyoung_plugins.translate.plugin",
            config=SAMPLE_CONFIG,
        )
        await pm.unload("translate")
        assert "translate" not in pm.plugins
