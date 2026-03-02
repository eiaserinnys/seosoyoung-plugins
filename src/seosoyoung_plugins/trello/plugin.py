"""Trello plugin.

Trello watcher, list runner, reaction-based execution, and
resume command handling. All configuration comes from trello.yaml,
not from Config singleton or environment variables.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any, Optional

from seosoyoung.plugin_sdk import HookContext, HookResult, Plugin, PluginMeta
from seosoyoung.plugin_sdk import slack, soulstream

from seosoyoung_plugins.trello.client import TrelloClient
from seosoyoung_plugins.trello.prompt_builder import PromptBuilder

logger = logging.getLogger(__name__)

# Session locks (thread_ts -> Lock)
_session_locks: dict[str, threading.RLock] = {}
_locks_lock = threading.Lock()


def _get_session_lock(thread_ts: str) -> threading.RLock:
    """Get or create a session lock for the given thread.

    Args:
        thread_ts: Thread timestamp

    Returns:
        RLock for the thread
    """
    with _locks_lock:
        if thread_ts not in _session_locks:
            _session_locks[thread_ts] = threading.RLock()
        return _session_locks[thread_ts]


_RESUME_PATTERNS = [
    re.compile(r"정주행\s*(을\s*)?재개", re.IGNORECASE),
    re.compile(r"리스트런\s*(을\s*)?재개", re.IGNORECASE),
    re.compile(r"resume\s*(list\s*)?run", re.IGNORECASE),
]


class TrelloPlugin(Plugin):
    """Trello watcher and card management plugin.

    Manages TrelloWatcher lifecycle and handles reaction-based
    execution and list-run resume commands.
    """

    meta = PluginMeta(
        name="trello",
        version="1.0.0",
        description="Trello watcher and card management",
    )

    async def on_load(self, config: dict[str, Any]) -> None:
        self._config = config

        self._trello = TrelloClient(
            api_key=config["api_key"],
            token=config["token"],
            board_id=config["board_id"],
        )

        self._prompt_builder = PromptBuilder(
            self._trello,
            list_ids=config["list_ids"],
        )

        # Runtime state
        self._watcher = None
        self._list_runner = None

        # Data directory for this plugin
        self._data_dir = soulstream.get_data_dir() / "trello_watcher"

        logger.info(
            "TrelloPlugin loaded: board_id=%s, execute_emoji=%s",
            config["board_id"],
            config["execute_emoji"],
        )

    async def on_unload(self) -> None:
        if self._watcher:
            self._watcher.stop()
            logger.info("TrelloPlugin: watcher stopped")

    def register_hooks(self) -> dict:
        return {
            "on_startup": self._on_startup,
            "on_shutdown": self._on_shutdown,
            "on_reaction": self._on_reaction,
            "on_command": self._on_command,
        }

    # -- Hook handlers ---------------------------------------------------------

    async def _on_startup(self, ctx: HookContext) -> tuple[HookResult, Any]:
        """Start watcher and list runner."""
        from seosoyoung_plugins.trello.watcher import TrelloWatcher
        from seosoyoung_plugins.trello.list_runner import ListRunner

        # Ensure data directory exists
        self._data_dir.mkdir(parents=True, exist_ok=True)

        self._list_runner = ListRunner(data_dir=self._data_dir)

        self._watcher = TrelloWatcher(
            trello_client=self._trello,
            prompt_builder=self._prompt_builder,
            config=self._config,
            get_session_lock=_get_session_lock,
            data_dir=self._data_dir,
            list_runner_ref=lambda: self._list_runner,
        )
        self._watcher.start()

        logger.info("TrelloPlugin: watcher and list_runner started")

        return HookResult.CONTINUE, {
            "watcher": self._watcher,
            "list_runner": self._list_runner,
        }

    async def _on_shutdown(self, ctx: HookContext) -> tuple[HookResult, Any]:
        """Stop watcher on shutdown."""
        if self._watcher:
            self._watcher.stop()
        return HookResult.CONTINUE, None

    async def _on_reaction(self, ctx: HookContext) -> tuple[HookResult, Any]:
        """Handle execute emoji reaction on trello watcher threads."""
        import asyncio

        event = ctx.args["event"]

        reaction = event.get("reaction", "")
        item = event.get("item", {})
        item_ts = item.get("ts", "")
        item_channel = item.get("channel", "")
        user_id = event.get("user", "")

        # 1. Check if this is the execute emoji
        if reaction != self._config["execute_emoji"]:
            return HookResult.SKIP, None

        logger.info(
            "Execute reaction detected: %s on %s by %s",
            reaction, item_ts, user_id,
        )

        # 2. Check watcher is available
        if not self._watcher:
            logger.debug("Watcher not available.")
            return HookResult.SKIP, None

        # 3. Look up ThreadCardInfo
        tracked = self._watcher.get_tracked_by_thread_ts(item_ts)
        if not tracked:
            logger.debug("ThreadCardInfo not found: %s", item_ts)
            return HookResult.SKIP, None

        logger.info(
            "ThreadCardInfo found: %s (card_id=%s)",
            tracked.card_name, tracked.card_id,
        )

        # 4. Check restart pending
        if soulstream.is_restart_pending():
            try:
                await slack.send_message(
                    channel=item_channel,
                    thread_ts=item_ts,
                    text="재시작을 대기하는 중입니다. 재시작이 완료되면 다시 시도해주세요.",
                )
            except Exception as e:
                logger.error("Failed to send restart-pending message: %s", e)
            return HookResult.STOP, None

        # 5. Post start notification
        try:
            start_result = await slack.send_message(
                channel=item_channel,
                thread_ts=item_ts,
                text="`🚀 리액션으로 실행을 시작합니다. 세션을 정리하는 중...`",
            )
            start_msg_ts = start_result.ts
        except Exception as e:
            logger.error("Failed to send start notification: %s", e)
            return HookResult.STOP, None

        # 6. Build execute prompt
        prompt = self._watcher.build_reaction_execute_prompt(tracked)

        # 7. Set has_execute flag
        tracked.has_execute = True

        # 8. Get session_id for this thread
        session_id = soulstream.get_session_id(item_ts)

        # 9. Run compact + Claude in background thread
        def run_async_task():
            """Run async operations in new event loop."""
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            try:
                loop.run_until_complete(self._execute_with_compact(
                    item_channel,
                    item_ts,
                    start_msg_ts,
                    session_id,
                    prompt,
                ))
            finally:
                loop.close()

        execute_thread = threading.Thread(target=run_async_task, daemon=True)
        execute_thread.start()

        return HookResult.STOP, None

    async def _execute_with_compact(
        self,
        channel: str,
        thread_ts: str,
        start_msg_ts: str,
        session_id: str | None,
        prompt: str,
    ):
        """Execute Claude with compact in async context."""
        lock = _get_session_lock(thread_ts)
        if not lock.acquire(blocking=False):
            try:
                await slack.update_message(
                    channel=channel,
                    ts=start_msg_ts,
                    text="이전 요청을 처리 중이에요. 잠시 후 다시 시도해주세요.",
                )
            except Exception:
                pass
            return

        try:
            # Compact if session exists
            if session_id:
                try:
                    await slack.update_message(
                        channel=channel,
                        ts=start_msg_ts,
                        text="`🚀 세션 정리 중... (compact)`",
                    )
                    compact_result = await soulstream.compact(session_id)
                    if compact_result.ok:
                        logger.info("Session compact success: %s", session_id)
                        if compact_result.session_id:
                            session_id = compact_result.session_id
                    else:
                        logger.warning(
                            "Session compact failed: %s",
                            compact_result.error,
                        )
                except Exception as e:
                    logger.error("Session compact error: %s", e)

            # Run Claude
            result = await soulstream.run(
                prompt=prompt,
                channel=channel,
                thread_ts=thread_ts,
                session_id=session_id,
                role="admin",
            )

            if not result.ok:
                await slack.update_message(
                    channel=channel,
                    ts=start_msg_ts,
                    text=f"❌ 실행 오류: {result.error}",
                )

        except Exception as e:
            logger.exception("Reaction-based execution error: %s", e)
            try:
                await slack.update_message(
                    channel=channel,
                    ts=start_msg_ts,
                    text=f"❌ 실행 오류: {e}",
                )
            except Exception:
                pass
        finally:
            lock.release()

    async def _on_command(self, ctx: HookContext) -> tuple[HookResult, Any]:
        """Handle resume list-run command."""
        from seosoyoung.plugin_sdk import slack

        command = ctx.args.get("command", "")

        if not _is_resume_command(command):
            return HookResult.SKIP, None

        if not self._list_runner:
            return HookResult.SKIP, None

        channel = ctx.args.get("channel")
        ts = ctx.args.get("ts")
        thread_ts = ctx.args.get("thread_ts")

        paused = self._list_runner.get_paused_sessions()
        if not paused:
            if channel:
                await slack.send_message(
                    channel=channel,
                    text="현재 중단된 정주행 세션이 없습니다.",
                    thread_ts=thread_ts or ts,
                )
            return HookResult.STOP, None

        # Resume the most recent paused session
        session = paused[-1]
        if self._list_runner.resume_run(session.session_id):
            if channel:
                await slack.send_message(
                    channel=channel,
                    text=(
                        f"\u25b6\ufe0f 정주행 재개: "
                        f"`{session.session_id}` ({session.list_name})\n"
                        f"진행: {session.current_index}/{len(session.card_ids)}"
                    ),
                    thread_ts=thread_ts or ts,
                )
            # Trigger watcher to process next card
            if self._watcher:
                notify_channel = self._config["notify_channel"]
                t = threading.Thread(
                    target=self._watcher._process_list_run_card,
                    args=(session.session_id, thread_ts or ts, notify_channel),
                    daemon=True,
                )
                t.start()
        else:
            if channel:
                await slack.send_message(
                    channel=channel,
                    text="정주행 재개에 실패했습니다.",
                    thread_ts=thread_ts or ts,
                )

        return HookResult.STOP, None


def _is_resume_command(command: str) -> bool:
    """Check if the command matches a resume list-run pattern."""
    for pattern in _RESUME_PATTERNS:
        if pattern.search(command):
            return True
    return False
