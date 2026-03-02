"""Tests for the trello plugin (plugins/trello/).

Tests TrelloPlugin lifecycle, hook registration, on_startup
dependency injection, on_reaction, and on_command dispatch
without importing Config or .env.
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from seosoyoung.plugin_sdk import HookContext, HookResult, PluginMeta
from seosoyoung.core.plugin_manager import PluginManager
from seosoyoung_plugins.trello.plugin import TrelloPlugin, _is_resume_command


SAMPLE_CONFIG = {
    "api_key": "test-trello-key",
    "token": "test-trello-token",
    "board_id": "test-board-id",
    "notify_channel": "C_NOTIFY",
    "dm_target_user_id": "U_DM",
    "poll_interval": 15,
    "polling_debug": False,
    "execute_emoji": "rocket",
    "watch_lists": {"to_go": "list-to-go-id"},
    "list_ids": {
        "draft": "",
        "backlog": "list-backlog-id",
        "in_progress": "list-inprogress-id",
        "blocked": "",
        "review": "list-review-id",
        "done": "list-done-id",
    },
}


class TestTrelloPluginMeta:
    """Plugin identity."""

    def test_meta_is_plugin_meta(self):
        assert isinstance(TrelloPlugin.meta, PluginMeta)

    def test_meta_name(self):
        assert TrelloPlugin.meta.name == "trello"

    def test_meta_version(self):
        assert TrelloPlugin.meta.version == "1.0.0"


class TestTrelloPluginLifecycle:
    """on_load / on_unload."""

    @pytest.fixture()
    def plugin(self):
        return TrelloPlugin()

    @pytest.mark.asyncio
    async def test_on_load_creates_client(self, plugin):
        await plugin.on_load(SAMPLE_CONFIG)
        assert plugin._trello is not None
        assert plugin._trello.api_key == "test-trello-key"
        assert plugin._trello.token == "test-trello-token"
        assert plugin._trello.board_id == "test-board-id"

    @pytest.mark.asyncio
    async def test_on_load_creates_prompt_builder(self, plugin):
        await plugin.on_load(SAMPLE_CONFIG)
        assert plugin._prompt_builder is not None

    @pytest.mark.asyncio
    async def test_on_load_initializes_runtime_as_none(self, plugin):
        await plugin.on_load(SAMPLE_CONFIG)
        assert plugin._watcher is None
        assert plugin._list_runner is None

    @pytest.mark.asyncio
    async def test_on_load_missing_key_raises(self, plugin):
        incomplete = {k: v for k, v in SAMPLE_CONFIG.items() if k != "api_key"}
        with pytest.raises(KeyError, match="api_key"):
            await plugin.on_load(incomplete)

    @pytest.mark.asyncio
    async def test_on_unload_no_watcher_noop(self, plugin):
        await plugin.on_load(SAMPLE_CONFIG)
        await plugin.on_unload()  # should not raise

    @pytest.mark.asyncio
    async def test_on_unload_stops_watcher(self, plugin):
        await plugin.on_load(SAMPLE_CONFIG)
        mock_watcher = MagicMock()
        plugin._watcher = mock_watcher
        await plugin.on_unload()
        mock_watcher.stop.assert_called_once()


class TestTrelloPluginHooks:
    """Hook registration."""

    @pytest.fixture()
    async def loaded_plugin(self):
        p = TrelloPlugin()
        await p.on_load(SAMPLE_CONFIG)
        return p

    @pytest.mark.asyncio
    async def test_register_hooks_keys(self, loaded_plugin):
        hooks = loaded_plugin.register_hooks()
        assert "on_startup" in hooks
        assert "on_shutdown" in hooks
        assert "on_reaction" in hooks
        assert "on_command" in hooks

    @pytest.mark.asyncio
    async def test_register_hooks_all_callable(self, loaded_plugin):
        hooks = loaded_plugin.register_hooks()
        for name, handler in hooks.items():
            assert callable(handler), f"{name} is not callable"


class TestTrelloOnStartup:
    """on_startup hook: watcher/list_runner creation."""

    @pytest.fixture()
    async def loaded_plugin(self):
        p = TrelloPlugin()
        await p.on_load(SAMPLE_CONFIG)
        return p

    @pytest.mark.asyncio
    async def test_on_startup_creates_watcher_and_runner(self, loaded_plugin, tmp_path):
        mock_client = MagicMock()
        mock_session_mgr = MagicMock()
        mock_runner_factory = MagicMock()
        mock_session_runtime = MagicMock()

        ctx = HookContext(
            hook_name="on_startup",
            args={
                "get_session_lock": MagicMock(),
                "restart_manager": MagicMock(),
                "data_dir": tmp_path,
            },
        )

        hooks = loaded_plugin.register_hooks()
        with patch(
            "seosoyoung_plugins.trello.watcher.TrelloWatcher"
        ) as MockWatcher:
            mock_watcher_instance = MagicMock()
            MockWatcher.return_value = mock_watcher_instance

            result, value = await hooks["on_startup"](ctx)

        assert result == HookResult.CONTINUE
        assert isinstance(value, dict)
        assert "watcher" in value
        assert "list_runner" in value
        assert value["watcher"] is mock_watcher_instance
        mock_watcher_instance.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_startup_stores_runtime_deps(self, loaded_plugin, tmp_path):
        ctx = HookContext(
            hook_name="on_startup",
            args={
                "get_session_lock": MagicMock(),
                "restart_manager": MagicMock(),
                "data_dir": tmp_path,
            },
        )

        hooks = loaded_plugin.register_hooks()
        with patch("seosoyoung_plugins.trello.watcher.TrelloWatcher"):
            await hooks["on_startup"](ctx)

        # Plugin no longer stores slack_client/session_manager
        assert loaded_plugin._get_session_lock is not None
        assert loaded_plugin._restart_manager is not None


class TestTrelloOnShutdown:
    """on_shutdown hook."""

    @pytest.mark.asyncio
    async def test_on_shutdown_stops_watcher(self):
        p = TrelloPlugin()
        await p.on_load(SAMPLE_CONFIG)
        mock_watcher = MagicMock()
        p._watcher = mock_watcher

        hooks = p.register_hooks()
        result, value = await hooks["on_shutdown"](
            HookContext(hook_name="on_shutdown")
        )

        assert result == HookResult.CONTINUE
        mock_watcher.stop.assert_called_once()


class TestTrelloOnReaction:
    """on_reaction hook: execute emoji."""

    @pytest.fixture()
    async def plugin_with_watcher(self):
        p = TrelloPlugin()
        await p.on_load(SAMPLE_CONFIG)
        p._watcher = MagicMock()
        p._restart_manager = MagicMock()
        p._restart_manager.is_pending = False
        p._get_session_lock = None
        return p

    @pytest.mark.asyncio
    async def test_skip_non_execute_emoji(self, plugin_with_watcher):
        event = {
            "reaction": "thumbsup",
            "item": {"ts": "1234.5678", "channel": "C_CH"},
            "user": "U123",
        }
        ctx = HookContext(
            hook_name="on_reaction",
            args={"event": event},
        )
        hooks = plugin_with_watcher.register_hooks()
        result, value = await hooks["on_reaction"](ctx)
        assert result == HookResult.SKIP

    @pytest.mark.asyncio
    async def test_skip_when_no_watcher(self):
        p = TrelloPlugin()
        await p.on_load(SAMPLE_CONFIG)
        # _watcher is None

        event = {
            "reaction": "rocket",
            "item": {"ts": "1234.5678", "channel": "C_CH"},
            "user": "U123",
        }
        ctx = HookContext(
            hook_name="on_reaction",
            args={"event": event, },
        )
        hooks = p.register_hooks()
        result, value = await hooks["on_reaction"](ctx)
        assert result == HookResult.SKIP

    @pytest.mark.asyncio
    async def test_skip_when_no_tracked_card(self, plugin_with_watcher):
        plugin_with_watcher._watcher.get_tracked_by_thread_ts.return_value = None

        event = {
            "reaction": "rocket",
            "item": {"ts": "1234.5678", "channel": "C_CH"},
            "user": "U123",
        }
        ctx = HookContext(
            hook_name="on_reaction",
            args={"event": event, },
        )
        hooks = plugin_with_watcher.register_hooks()
        result, value = await hooks["on_reaction"](ctx)
        assert result == HookResult.SKIP

    @pytest.mark.asyncio
    async def test_stop_when_restart_pending(self, plugin_with_watcher, mock_plugin_sdk):
        plugin_with_watcher._restart_manager.is_pending = True
        mock_tracked = MagicMock()
        mock_tracked.card_id = "card-123"
        mock_tracked.card_name = "Test Card"
        plugin_with_watcher._watcher.get_tracked_by_thread_ts.return_value = mock_tracked

        event = {
            "reaction": "rocket",
            "item": {"ts": "1234.5678", "channel": "C_CH"},
            "user": "U123",
        }
        ctx = HookContext(
            hook_name="on_reaction",
            args={"event": event},
        )
        hooks = plugin_with_watcher.register_hooks()
        result, value = await hooks["on_reaction"](ctx)
        assert result == HookResult.STOP
        mock_plugin_sdk["slack"].send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_and_start_thread_on_execute(self, plugin_with_watcher, mock_plugin_sdk):
        """Execute emoji on tracked card -> STOP and starts background thread."""
        mock_tracked = MagicMock()
        mock_tracked.card_id = "card-123"
        mock_tracked.card_name = "Test Card"
        plugin_with_watcher._watcher.get_tracked_by_thread_ts.return_value = mock_tracked
        plugin_with_watcher._watcher.build_reaction_execute_prompt.return_value = "execute prompt"

        event = {
            "reaction": "rocket",
            "item": {"ts": "1234.5678", "channel": "C_CH"},
            "user": "U123",
        }
        ctx = HookContext(
            hook_name="on_reaction",
            args={"event": event},
        )

        hooks = plugin_with_watcher.register_hooks()
        with patch("seosoyoung_plugins.trello.plugin.threading") as mock_threading:
            result, value = await hooks["on_reaction"](ctx)

        assert result == HookResult.STOP
        # A background thread should have been created
        mock_threading.Thread.assert_called_once()
        mock_threading.Thread.return_value.start.assert_called_once()


class TestTrelloOnCommand:
    """on_command hook: resume list run."""

    @pytest.fixture()
    async def plugin_with_runner(self):
        p = TrelloPlugin()
        await p.on_load(SAMPLE_CONFIG)
        p._list_runner = MagicMock()
        p._watcher = MagicMock()
        return p

    @pytest.mark.asyncio
    async def test_skip_non_resume_command(self, plugin_with_runner):
        ctx = HookContext(
            hook_name="on_command",
            args={"command": "help", "say": MagicMock(), "ts": "ts"},
        )
        hooks = plugin_with_runner.register_hooks()
        result, value = await hooks["on_command"](ctx)
        assert result == HookResult.SKIP

    @pytest.mark.asyncio
    async def test_stop_resume_no_paused(self, plugin_with_runner):
        plugin_with_runner._list_runner.get_paused_sessions.return_value = []
        mock_say = MagicMock()

        ctx = HookContext(
            hook_name="on_command",
            args={"command": "정주행 재개", "say": mock_say, "ts": "ts", "thread_ts": None},
        )
        hooks = plugin_with_runner.register_hooks()
        result, value = await hooks["on_command"](ctx)
        assert result == HookResult.STOP
        mock_say.assert_called_once()
        assert "없습니다" in mock_say.call_args[1]["text"]

    @pytest.mark.asyncio
    async def test_stop_resume_success(self, plugin_with_runner):
        mock_session = MagicMock()
        mock_session.session_id = "sess-123"
        mock_session.list_name = "Test List"
        mock_session.current_index = 2
        mock_session.card_ids = ["a", "b", "c"]
        plugin_with_runner._list_runner.get_paused_sessions.return_value = [mock_session]
        plugin_with_runner._list_runner.resume_run.return_value = True
        mock_say = MagicMock()

        ctx = HookContext(
            hook_name="on_command",
            args={
                "command": "resume list run",
                "say": mock_say,
                "ts": "ts",
                "thread_ts": None,
            },
        )
        hooks = plugin_with_runner.register_hooks()
        with patch("seosoyoung_plugins.trello.plugin.threading"):
            result, value = await hooks["on_command"](ctx)

        assert result == HookResult.STOP
        plugin_with_runner._list_runner.resume_run.assert_called_once_with("sess-123")


class TestIsResumeCommand:
    """Helper function for resume pattern matching."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "정주행 재개",
            "정주행을 재개",
            "정주행 재개해줘",
            "리스트런 재개",
            "리스트런을 재개",
            "resume list run",
            "resume run",
        ],
    )
    def test_resume_patterns_match(self, cmd):
        assert _is_resume_command(cmd) is True

    @pytest.mark.parametrize(
        "cmd",
        ["help", "status", "정주행 시작", "restart", "번역 hello"],
    )
    def test_non_resume_patterns_skip(self, cmd):
        assert _is_resume_command(cmd) is False


class TestTrelloPluginManagerIntegration:
    """Integration test: load TrelloPlugin via PluginManager."""

    @pytest.mark.asyncio
    async def test_load_and_hook_registration(self):
        pm = PluginManager()
        plugin = await pm.load(
            module="seosoyoung_plugins.trello.plugin",
            config=SAMPLE_CONFIG,
            priority=100,
        )
        assert plugin.meta.name == "trello"
        assert "trello" in pm.plugins

    @pytest.mark.asyncio
    async def test_dispatch_on_reaction_skip_for_non_execute(self):
        pm = PluginManager()
        await pm.load(
            module="seosoyoung_plugins.trello.plugin",
            config=SAMPLE_CONFIG,
            priority=100,
        )

        ctx = HookContext(
            hook_name="on_reaction",
            args={
                "event": {
                    "reaction": "thumbsup",
                    "item": {"ts": "1234.5678", "channel": "C_CH"},
                    "user": "U123",
                },
            },
        )
        ctx = await pm.dispatch("on_reaction", ctx)
        assert not ctx.stopped

    @pytest.mark.asyncio
    async def test_dispatch_on_command_skip_for_unknown(self):
        pm = PluginManager()
        await pm.load(
            module="seosoyoung_plugins.trello.plugin",
            config=SAMPLE_CONFIG,
            priority=100,
        )

        ctx = HookContext(
            hook_name="on_command",
            args={
                "command": "help",
                "say": MagicMock(),
                "ts": "ts",
            },
        )
        ctx = await pm.dispatch("on_command", ctx)
        assert not ctx.stopped

    @pytest.mark.asyncio
    async def test_unload(self):
        pm = PluginManager()
        await pm.load(
            module="seosoyoung_plugins.trello.plugin",
            config=SAMPLE_CONFIG,
        )
        await pm.unload("trello")
        assert "trello" not in pm.plugins
