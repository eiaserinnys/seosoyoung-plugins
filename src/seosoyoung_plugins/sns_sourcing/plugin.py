"""SNS sourcing plugin entry point."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from seosoyoung.plugin_sdk import HookContext, HookResult, Plugin, PluginMeta
from seosoyoung.plugin_sdk import soulstream

from seosoyoung_plugins.sns_sourcing.collector import (
    SlackHistoryCollector,
    SourceChannel,
)
from seosoyoung_plugins.sns_sourcing.publisher import SnsPublisher
from seosoyoung_plugins.sns_sourcing.scheduler import SnsSourcingScheduler
from seosoyoung_plugins.sns_sourcing.service import SnsSourcingService
from seosoyoung_plugins.sns_sourcing.session import SnsDecisionSession
from seosoyoung_plugins.sns_sourcing.store import SnsSourcingStore

logger = logging.getLogger(__name__)


class SnsSourcingPlugin(Plugin):
    """Autonomous SNS material sourcing and draft generation."""

    meta = PluginMeta(
        name="sns_sourcing",
        version="1.0.0",
        description="Collect Slack SNS candidates, classify them, and post drafts",
    )

    async def on_load(self, config: dict[str, Any]) -> None:
        self._config = config
        self._enabled = bool(config.get("enabled", True))
        self._active = self._enabled and _node_guard_allows(config)
        self._scheduler: SnsSourcingScheduler | None = None

        if not self._enabled:
            logger.info("sns_sourcing disabled by config")
        elif not self._active:
            logger.warning("sns_sourcing node guard blocked startup; no-op")

    async def on_unload(self) -> None:
        if self._scheduler:
            self._scheduler.stop()

    def register_hooks(self) -> dict:
        return {
            "on_startup": self._on_startup,
            "on_shutdown": self._on_shutdown,
        }

    async def _on_startup(self, ctx: HookContext) -> tuple[HookResult, Any]:
        if not self._active:
            return HookResult.CONTINUE, {"sns_sourcing_active": False}

        config = self._config
        store = SnsSourcingStore(_state_dir(config))
        source_channels = _source_channels(config)
        output_channel = _output_channel(config)
        debug_channel = _debug_channel(config)
        session_config = config.get("session", {})
        scan_config = config.get("scan", {})

        collector = SlackHistoryCollector(
            store=store,
            source_channels=source_channels,
            workspace_domain=_workspace_domain(config),
            bot_user_id=ctx.args.get("bot_user_id", ""),
            page_limit=int(
                session_config.get(
                    "per_channel_history_limit",
                    scan_config.get("channel_page_limit", 100),
                )
            ),
            max_pages_per_channel=int(scan_config.get("max_pages_per_channel_per_tick", 2)),
            bootstrap=config.get("bootstrap", scan_config.get("first_run", "now")),
        )
        decision_session = SnsDecisionSession(
            output_channel=output_channel,
            debug_channel=debug_channel,
            folder_id=session_config.get(
                "folder_id",
                config.get("soulstream", {}).get("folder_id", ""),
            ),
            agent_id=session_config.get(
                "agent_id",
                config.get("soulstream", {}).get("agent_id", "seosoyoung-opus"),
            ),
            max_candidates=int(session_config.get("max_candidates_per_session", 8)),
        )
        publisher = SnsPublisher(
            output_channel=output_channel,
            debug_channel=debug_channel,
            dry_run=bool(config.get("dry_run", True)),
        )
        service = SnsSourcingService(
            store=store,
            collector=collector,
            session=decision_session,
            publisher=publisher,
        )

        schedule = config.get("schedule", {})
        slots = schedule.get("slots") or [
            schedule.get("am", "10:30"),
            schedule.get("pm", "16:30"),
        ]
        self._scheduler = SnsSourcingScheduler(
            service=service,
            store=store,
            timezone=schedule.get("tz", "Asia/Seoul"),
            slots=slots,
            poll_sec=int(schedule.get("poll_sec", 300)),
        )
        self._scheduler.start()

        return HookResult.CONTINUE, {
            "sns_sourcing_active": True,
            "sns_sourcing_store": store,
            "sns_sourcing_scheduler": self._scheduler,
        }

    async def _on_shutdown(self, ctx: HookContext) -> tuple[HookResult, Any]:
        if self._scheduler:
            self._scheduler.stop()
        return HookResult.CONTINUE, None


def _node_guard_allows(config: dict[str, Any]) -> bool:
    allowed = _allowed_nodes(config)
    current = os.environ.get("SOULSTREAM_NODE_ID") or os.environ.get(
        "SOULSTREAM_PREFERRED_NODE"
    )
    if not allowed or not current:
        logger.warning(
            "sns_sourcing node guard fail-closed: current=%r allowed=%s",
            current,
            allowed,
        )
        return False
    if current not in allowed:
        logger.warning(
            "sns_sourcing node guard mismatch: current=%s allowed=%s",
            current,
            allowed,
        )
        return False
    logger.info("sns_sourcing node guard passed: current=%s", current)
    return True


def _allowed_nodes(config: dict[str, Any]) -> list[str]:
    guard = config.get("node_guard")
    if isinstance(guard, str):
        return [guard]
    if isinstance(guard, dict):
        if guard.get("allowed_node_ids"):
            return list(guard["allowed_node_ids"])
        if guard.get("allowed"):
            return list(guard["allowed"])
        if guard.get("node_id"):
            return [guard["node_id"]]
    return []


def _source_channels(config: dict[str, Any]) -> list[SourceChannel]:
    slack_config = config.get("slack", {})
    raw = slack_config.get("source_channels") or config.get("source_channels")
    if raw is None:
        raw = config.get("scan_channels", [])
    channels: list[SourceChannel] = []
    for item in raw:
        if isinstance(item, str):
            channels.append(SourceChannel(id=item, name=item))
        else:
            channels.append(SourceChannel(id=item["id"], name=item.get("name", item["id"])))
    return channels


def _output_channel(config: dict[str, Any]) -> str:
    value = config.get("output_channel")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value["id"]
    return config.get("slack", {}).get("output_channel", {}).get("id", "")


def _debug_channel(config: dict[str, Any]) -> str:
    value = config.get("debug_channel", "")
    return value.get("id", "") if isinstance(value, dict) else value


def _workspace_domain(config: dict[str, Any]) -> str:
    slack_config = config.get("slack", {})
    return config.get("workspace_domain") or slack_config.get(
        "workspace_url",
        "thelinegames.slack.com",
    )


def _state_dir(config: dict[str, Any]) -> Path:
    if config.get("state_path"):
        return Path(config["state_path"])
    state = config.get("state", {})
    if state.get("path"):
        return Path(state["path"])
    subdir = state.get("subdir", "sns_sourcing")
    return soulstream.get_data_dir() / subdir

