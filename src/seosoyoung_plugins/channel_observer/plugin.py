"""Channel Observer plugin.

Collects channel messages, runs digest/judge pipelines, and
manages periodic digest scheduling. All configuration comes
from channel_observer.yaml, not from Config singleton or
environment variables.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Coroutine

from seosoyoung.plugin_sdk import HookContext, HookResult, Plugin, PluginMeta
from seosoyoung_plugins.channel_observer import pipeline_lock
from seosoyoung_plugins.soulstream_client import SoulstreamClient

logger = logging.getLogger(__name__)


class ChannelObserverPlugin(Plugin):
    """Channel observation and digest management plugin.

    Collects messages from monitored channels via on_message hook,
    triggers digest/judge pipelines when thresholds are met, and
    runs a periodic scheduler via on_startup hook.

    No self.enabled flag — if loaded, it's active.
    PluginMeta에 dependencies 없음 — plugins.yaml depends_on이 정본.
    """

    meta = PluginMeta(
        name="channel_observer",
        version="1.0.0",
        description="Channel message observation and digest management",
    )

    async def on_load(self, config: dict[str, Any]) -> None:
        self._config = config

        # Core settings
        self._channels: list[str] = config.get("channels", [])
        self._model: str = config.get("model", "gpt-5-mini")
        self._compressor_model: str = config.get(
            "compressor_model", "gpt-5.2"
        )
        self._memory_path: str = config["memory_path"]

        # Soulstream client
        soulstream_url = config.get("soulstream_url", "")
        soulstream_token = config.get("soulstream_token", "")
        self._soulstream: SoulstreamClient | None = None
        if soulstream_url and soulstream_token:
            self._soulstream = SoulstreamClient(
                base_url=soulstream_url,
                bearer_token=soulstream_token,
            )

        # Thresholds
        self._threshold_a: int = config.get("threshold_a", 150)
        self._threshold_b: int = config.get("threshold_b", 5000)
        self._buffer_threshold: int = config.get("buffer_threshold", 150)
        self._digest_max_tokens: int = config.get("digest_max_tokens", 10000)
        self._digest_target_tokens: int = config.get(
            "digest_target_tokens", 5000
        )
        self._intervention_threshold: float = config.get(
            "intervention_threshold", 0.18
        )
        self._react_probability: float = config.get("react_probability", 1.0)
        self._recent_messages_count: int = config.get(
            "recent_messages_count", 5
        )
        self._periodic_sec: int = config.get("periodic_sec", 300)
        self._intervene_model: str | None = config.get("intervene_model", None)
        self._intervene_folder_id: str | None = config.get("folder_id", None)
        self._trigger_words: list[str] = config.get("trigger_words", [])
        self._debug_channel: str = config.get("debug_channel", "")

        # Atom channel store settings
        self._atom_channel_store: bool = config.get("atom_channel_store", False)
        self._atom_config: dict | None = None
        if self._atom_channel_store:
            import os
            api_key_env = config.get("atom_api_key_env", "CHAT_WRITE_API_KEY")
            self._atom_config = {
                "atom_base_url": config["atom_base_url"],
                "atom_api_key": os.environ[api_key_env],
                "atom_slack_root_node_id": config["atom_slack_root_node_id"],
            }

        # Runtime components (initialized in on_startup)
        self._store = None
        self._collector = None
        self._cooldown = None
        self._observer_engine = None
        self._compressor = None
        self._scheduler = None
        self._llm_call = (
            self._make_llm_call() if self._soulstream else None
        )

        logger.info(
            "ChannelObserverPlugin loaded: channels=%s, threshold_a=%d",
            self._channels,
            self._threshold_a,
        )

    async def on_unload(self) -> None:
        if self._scheduler:
            self._scheduler.stop()
            logger.info("ChannelObserverPlugin: scheduler stopped")
        if self._soulstream:
            await self._soulstream.close()

    def register_hooks(self) -> dict:
        return {
            "on_message": self._on_message,
            "on_startup": self._on_startup,
            "on_shutdown": self._on_shutdown,
            "before_execute": self._on_before_execute,
        }

    # -- Hook handlers ---------------------------------------------------------

    async def _on_startup(
        self, ctx: HookContext
    ) -> tuple[HookResult, Any]:
        """Initialize runtime components and start periodic scheduler.

        Receives runtime dependencies via ctx.args from main.py.
        """
        if not self._channels:
            logger.info("ChannelObserverPlugin: no channels configured")
            return HookResult.CONTINUE, None

        self._bot_user_id = ctx.args.get("bot_user_id", "")

        from seosoyoung_plugins.channel_observer.store import (
            ChannelStore,
        )
        from seosoyoung_plugins.channel_observer.collector import (
            ChannelMessageCollector,
        )
        from seosoyoung_plugins.channel_observer.intervention import (
            InterventionHistory,
        )
        from seosoyoung_plugins.channel_observer.observer import (
            ChannelObserver,
            DigestCompressor,
        )
        from seosoyoung_plugins.channel_observer.scheduler import (
            ChannelDigestScheduler,
        )

        if self._atom_config:
            from seosoyoung_plugins.channel_observer.atom_store import AtomChannelStore
            self._store = AtomChannelStore(config=self._atom_config)
        else:
            self._store = ChannelStore(base_dir=self._memory_path)
        self._collector = ChannelMessageCollector(
            store=self._store,
            target_channels=self._channels,
            bot_user_id=self._bot_user_id,
        )
        self._cooldown = InterventionHistory(base_dir=self._memory_path)

        if self._soulstream:
            self._observer_engine = ChannelObserver(
                soulstream_client=self._soulstream,
                model=self._model,
            )
            self._compressor = DigestCompressor(
                soulstream_client=self._soulstream,
                model=self._compressor_model,
            )

        if self._observer_engine and self._periodic_sec > 0:
            self._scheduler = ChannelDigestScheduler(
                store=self._store,
                observer=self._observer_engine,
                compressor=self._compressor,
                cooldown=self._cooldown,
                channels=self._channels,
                interval_sec=self._periodic_sec,
                buffer_threshold=self._buffer_threshold,
                digest_max_tokens=self._digest_max_tokens,
                digest_target_tokens=self._digest_target_tokens,
                debug_channel=self._debug_channel,
                intervention_threshold=self._intervention_threshold,
                react_probability=self._react_probability,
                llm_call=self._llm_call,
                bot_user_id=self._bot_user_id,
                recent_messages_count=self._recent_messages_count,
                intervene_model=self._intervene_model,
                folder_id=self._intervene_folder_id,
            )
            self._scheduler.start()

        logger.info(
            "ChannelObserverPlugin started: channels=%s, "
            "threshold=%s, periodic=%ds",
            self._channels,
            self._intervention_threshold,
            self._periodic_sec,
        )

        # Return references for handler access
        return HookResult.CONTINUE, {
            "channel_store": self._store,
            "channel_collector": self._collector,
            "channel_cooldown": self._cooldown,
            "channel_observer": self._observer_engine,
            "channel_compressor": self._compressor,
            "channel_observer_channels": self._channels,
        }

    async def _on_shutdown(
        self, ctx: HookContext
    ) -> tuple[HookResult, Any]:
        """Stop scheduler on shutdown."""
        if self._scheduler:
            self._scheduler.stop()
        return HookResult.CONTINUE, None

    async def _on_before_execute(
        self, ctx: HookContext
    ) -> tuple[HookResult, Any]:
        """멘션 세션 실행 전 atom 채널 컨텍스트 주입."""
        import asyncio as _asyncio
        _compile_fn = getattr(self._store, "compile_channel_context", None) if self._store else None
        if not callable(_compile_fn):
            return HookResult.CONTINUE, None
        if not _asyncio.iscoroutinefunction(_compile_fn):
            return HookResult.CONTINUE, None

        channel = ctx.args.get("channel", "")
        if channel not in self._channels:
            return HookResult.CONTINUE, None

        context_items = ctx.args.get("context_items", [])

        try:
            atom_context = await _compile_fn(channel, limit=20)
            if not atom_context:
                return HookResult.CONTINUE, None

            updated_items = [
                item for item in context_items if item.get("key") != "channel_digest"
            ]
            updated_items.insert(0, {
                "key": "channel_digest",
                "label": "채널 컨텍스트",
                "content": atom_context,
            })
            return HookResult.CONTINUE, {"context_items": updated_items}
        except Exception as e:
            logger.warning("ChannelObserver before_execute 실패 (무시): %s", e)
            return HookResult.CONTINUE, None

    async def _on_message(
        self, ctx: HookContext
    ) -> tuple[HookResult, Any]:
        """Collect channel messages and trigger digest pipeline.

        This runs before other on_message hooks (priority=20 in
        plugins.yaml, but runs first because higher priority goes first).
        """
        if not self._collector:
            return HookResult.SKIP, None

        event = ctx.args.get("event", {})

        channel = event.get("channel", "")
        if channel not in self._channels:
            return HookResult.SKIP, None

        try:
            collected = self._collector.collect(event)
            if collected:
                self._send_collect_log(channel, event)
                force = self._contains_trigger_word(
                    event.get("text", "")
                )
                self._maybe_trigger_digest(channel, force=force)
        except Exception as e:
            logger.error(f"채널 메시지 수집 실패: {e}")

        # Also handle reactions collection for message events
        # (actual reaction events are handled separately)

        # SKIP — don't stop the chain, let other plugins process
        return HookResult.SKIP, None

    # -- Reaction collection (called from message handler) ---------------------

    def collect_reaction(self, event: dict, action: str) -> bool:
        """Collect reaction events for channel observation.

        Called directly from message handler, not via hook dispatch.
        """
        if not self._collector:
            return False
        return self._collector.collect_reaction(event, action)

    # -- Accessors for runtime references --------------------------------------

    @property
    def store(self) -> Any:
        """ChannelStore instance (for session_context hybrid mode)."""
        return self._store

    @property
    def channels(self) -> list[str]:
        """Monitored channel IDs."""
        return self._channels

    # -- Internal helpers ------------------------------------------------------

    def _make_llm_call(
        self,
    ) -> Callable[..., Coroutine[Any, Any, str]]:
        """개입 응답 생성용 LLM 호출 함수를 반환합니다.

        pipeline.py의 llm_call 시그니처에 맞춰
        async def(system_prompt, user_prompt) -> str 를 반환합니다.
        소울스트림 프록시를 통해 호출하며,
        응답 생성에는 compressor_model(고성능 모델)을 사용합니다.
        """
        soulstream = self._soulstream
        model = self._compressor_model

        async def llm_call(
            system_prompt: str, user_prompt: str
        ) -> str:
            result = await soulstream.complete(
                provider="openai",
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                client_id="channel-observer",
            )
            return result.content

        return llm_call

    def _contains_trigger_word(self, text: str) -> bool:
        """텍스트에 트리거 워드가 포함되어 있는지 확인합니다."""
        if not self._trigger_words:
            return False
        text_lower = text.lower()
        return any(
            word.lower() in text_lower for word in self._trigger_words
        )

    def _maybe_trigger_digest(
        self, channel_id: str, *, force: bool = False
    ) -> None:
        """pending 토큰이 threshold_A 이상이면 파이프라인을 실행합니다."""
        if not all([self._store, self._observer_engine, self._cooldown]):
            return

        pending_tokens = self._store.count_pending_tokens(channel_id)
        if not force and pending_tokens < self._threshold_a:
            return

        if not pipeline_lock.try_acquire(channel_id):
            return

        threshold_a = 1 if force else self._threshold_a

        def run():
            try:
                from seosoyoung_plugins.channel_observer.pipeline import (
                    run_channel_pipeline,
                )

                # Create event loop for this thread
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                try:
                    loop.run_until_complete(
                        run_channel_pipeline(
                            store=self._store,
                            observer=self._observer_engine,
                            channel_id=channel_id,
                            cooldown=self._cooldown,
                            threshold_a=threshold_a,
                            threshold_b=self._threshold_b,
                            compressor=self._compressor,
                            digest_max_tokens=self._digest_max_tokens,
                            digest_target_tokens=self._digest_target_tokens,
                            debug_channel=self._debug_channel,
                            intervention_threshold=self._intervention_threshold,
                            react_probability=self._react_probability,
                            llm_call=self._llm_call,
                            bot_user_id=self._bot_user_id,
                            recent_messages_count=self._recent_messages_count,
                            intervene_model=self._intervene_model,
                            folder_id=self._intervene_folder_id,
                        )
                    )
                finally:
                    loop.close()
            except Exception as e:
                logger.error(
                    f"채널 파이프라인 실행 실패 ({channel_id}): {e}"
                )
            finally:
                pipeline_lock.release(channel_id)

        digest_thread = threading.Thread(target=run, daemon=True)
        digest_thread.start()

    def _send_collect_log(
        self, channel_id: str, event: dict
    ) -> None:
        """수집 디버그 로그를 전송합니다."""
        if not self._debug_channel:
            return
        try:
            from seosoyoung_plugins.channel_observer.intervention import (
                send_collect_debug_log,
            )

            if event.get("subtype") == "message_changed":
                source = event.get("message", {})
            else:
                source = event

            buffer_tokens = (
                self._store.count_pending_tokens(channel_id)
                if self._store
                else 0
            )
            send_collect_debug_log(
                debug_channel=self._debug_channel,
                source_channel=channel_id,
                buffer_tokens=buffer_tokens,
                threshold=self._threshold_a,
                message_text=source.get("text", ""),
                user=source.get("user", ""),
                is_thread=bool(
                    source.get("thread_ts") or event.get("thread_ts")
                ),
            )
        except Exception as e:
            logger.error(f"수집 디버그 로그 전송 실패: {e}")
