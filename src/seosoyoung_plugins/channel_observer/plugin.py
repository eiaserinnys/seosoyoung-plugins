"""Channel Observer plugin with per-message Jev judgment."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Coroutine

from seosoyoung.plugin_sdk import HookContext, HookResult, Plugin, PluginMeta
from seosoyoung_plugins.channel_observer import pipeline_lock
from seosoyoung_plugins.channel_observer.emoji_frequency import EmojiFrequencyStore
from seosoyoung_plugins.channel_observer.intervene_memory import InterveneMemory
from seosoyoung_plugins.channel_observer.intervention_prep import (
    InterventionPrepServices,
    default_prep_output_dir,
)
from seosoyoung_plugins.channel_observer.mcp_http import (
    make_session_name_setter,
)
from seosoyoung_plugins.channel_observer.jev_judge import JevChannelJudge
from seosoyoung_plugins.channel_observer.pipeline import run_message_jev_pipeline
from seosoyoung_plugins.channel_observer.remiel_context import RemielContextConfig
from seosoyoung_plugins.soulstream_client import SoulstreamClient

logger = logging.getLogger(__name__)
PREP_MAX_TOKENS = 4096


class ChannelObserverPlugin(Plugin):
    """Judge monitored channel messages and manage intervention context.

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
        self._memory_dir = Path(self._memory_path)
        self._jev_api_key: str = str(
            config.get("jev_api_key")
            or os.environ.get("JEV_API_KEY")
            or os.environ.get("TYPESAFE_API_KEY")
            or ""
        ).strip()
        self._jev_base_url: str = str(
            config.get("jev_base_url")
            or os.environ.get("JEV_BASE_URL")
            or os.environ.get("TYPESAFE_BASE_URL")
            or "https://api.typesafe.ai"
        ).strip()
        self._jev_model: str = str(config.get("jev_model", "jev-latest"))
        self._jev_timeout: float = float(config.get("jev_timeout", 5.0))
        self._emoji_threshold: float = float(config.get("emoji_threshold", 0.50))
        self._intervene_jev_threshold: float = float(
            config.get("intervene_jev_threshold", 0.65)
        )
        self._trigger_intervene_jev_threshold: float = float(
            config.get("trigger_intervene_jev_threshold", 0.35)
        )
        self._emoji_candidate_limit: int = int(
            config.get("emoji_candidate_limit", 30)
        )

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
        self._intervene_agent_id: str | None = config.get("agent_id", None)
        self._trigger_words: list[str] = config.get("trigger_words", [])
        self._debug_channel: str = config.get("debug_channel", "")
        self._remiel_config = RemielContextConfig.from_plugin_config(config)

        if self._intervene_agent_id and self._intervene_model:
            logger.warning(
                "agent_id=%s가 agent profile 모델을 우선하므로 "
                "intervene_model=%s 설정은 무효입니다",
                self._intervene_agent_id,
                self._intervene_model,
            )

        # Runtime components (initialized in on_startup)
        self._store = None
        self._collector = None
        self._cooldown = None
        self._observer_engine = None
        self._compressor = None
        self._scheduler = None
        self._emoji_store: EmojiFrequencyStore | None = None
        self._jev_judge: JevChannelJudge | None = None
        self._recent_bot_reactions: dict[str, dict[str, list[str]]] = {}
        self._bot_user_id: str | None = None
        self._llm_call = (
            self._make_llm_call() if self._soulstream else None
        )
        self._prep_services = InterventionPrepServices(
            output_dir=default_prep_output_dir(),
            set_session_name=make_session_name_setter(
                soulstream_url, soulstream_token,
            ),
            memory=InterveneMemory.from_config(config),
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
        }

    # -- Hook handlers ---------------------------------------------------------

    async def _on_startup(
        self, ctx: HookContext
    ) -> tuple[HookResult, Any]:
        """Initialize the stores and components used by message-level Jev.

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
        self._store = ChannelStore(base_dir=self._memory_dir)
        self._collector = ChannelMessageCollector(
            store=self._store,
            target_channels=self._channels,
            bot_user_id=self._bot_user_id,
        )
        self._cooldown = InterventionHistory(base_dir=self._memory_dir)
        self._emoji_store = EmojiFrequencyStore(
            self._memory_dir / "emoji_frequency.json"
        )
        self._jev_judge = JevChannelJudge(
            api_key=self._jev_api_key,
            base_url=self._jev_base_url,
            model=self._jev_model,
            timeout=self._jev_timeout,
            emoji_threshold=self._emoji_threshold,
            intervene_threshold=self._intervene_jev_threshold,
            trigger_intervene_threshold=self._trigger_intervene_jev_threshold,
        )

        if self._soulstream:
            self._observer_engine = ChannelObserver(
                soulstream_client=self._soulstream,
                model=self._model,
            )
            self._compressor = DigestCompressor(
                soulstream_client=self._soulstream,
                model=self._compressor_model,
            )

        self._scheduler = None

        logger.info(
            "ChannelObserverPlugin started: channels=%s, "
            "threshold=%s, message-level Jev enabled",
            self._channels,
            self._intervention_threshold,
        )

        # Return references for handler access
        return HookResult.CONTINUE, {
            "channel_store": self._store,
            "channel_collector": self._collector,
            "channel_cooldown": self._cooldown,
            "channel_observer": self._observer_engine,
            "channel_compressor": self._compressor,
            "channel_observer_channels": self._channels,
            "emoji_frequency_store": self._emoji_store,
            "jev_judge": self._jev_judge,
        }

    async def _on_shutdown(
        self, ctx: HookContext
    ) -> tuple[HookResult, Any]:
        """Stop scheduler on shutdown."""
        if self._scheduler:
            self._scheduler.stop()
        return HookResult.CONTINUE, None

    async def _on_message(
        self, ctx: HookContext
    ) -> tuple[HookResult, Any]:
        """Run message-level Jev judgment for a monitored channel message.

        This runs before other on_message hooks (priority=20 in
        plugins.yaml, but runs first because higher priority goes first).
        """
        if (
            self._jev_judge is None
            or self._emoji_store is None
            or self._store is None
            or self._cooldown is None
        ):
            return HookResult.SKIP, None

        event = ctx.args.get("event", {})

        channel_id = event.get("channel", "")
        if channel_id not in self._channels:
            return HookResult.SKIP, None

        try:
            await self._judge_single_message(channel_id, event)
        except Exception as e:
            logger.error("단일 메시지 Jev 판단 실패: %s", e)

        # SKIP — don't stop the chain, let other plugins process
        return HookResult.SKIP, None

    # -- Reaction collection (called from message handler) ---------------------

    def collect_reaction(self, event: dict, action: str) -> bool:
        """Collect reaction events for channel observation.

        Called directly from message handler, not via hook dispatch.
        """
        item = event.get("item", {})
        channel_id = item.get("channel", "")
        recorded = False
        if (
            self._emoji_store is not None
            and channel_id in self._channels
            and item.get("type", "message") == "message"
        ):
            recorded = self._emoji_store.record_reaction(
                event.get("reaction", ""),
                action,
                user_id=event.get("user"),
                bot_user_id=self._bot_user_id,
            )
            if self._collector is not None:
                recorded = self._collector.collect_reaction(event, action) or recorded
        return recorded

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

    async def _judge_single_message(self, channel_id: str, event: dict) -> Any:
        return await run_message_jev_pipeline(
            channel_id=channel_id,
            event=event,
            jev_judge=self._jev_judge,
            emoji_store=self._emoji_store,
            cooldown=self._cooldown,
            store=self._store,
            bot_user_id=self._bot_user_id,
            trigger_words=self._trigger_words,
            debug_channel=self._debug_channel,
            recent_messages_count=self._recent_messages_count,
            emoji_candidate_limit=self._emoji_candidate_limit,
            llm_call=self._llm_call,
            intervene_model=self._intervene_model,
            folder_id=self._intervene_folder_id,
            agent_id=self._intervene_agent_id,
            remiel_config=self._remiel_config,
            prep_services=self._prep_services,
            recent_bot_reactions=self._recent_bot_reactions.setdefault(
                channel_id, {}
            ),
            intervention_threshold=self._intervention_threshold,
        )

    def _make_llm_call(
        self,
    ) -> Callable[..., Coroutine[Any, Any, str]]:
        """개입 응답 생성용 LLM 호출 함수를 반환합니다.

        pipeline.py의 llm_call 시그니처에 맞춰
        async def(system_prompt, user_prompt) -> str 를 반환합니다.
        소울스트림 프록시를 통해 호출하며,
        준비물 생성에는 channel observer model(gpt-5-mini 기본값)을 사용합니다.
        """
        soulstream = self._soulstream
        model = self._model

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
                max_tokens=PREP_MAX_TOKENS,
                client_id="channel-observer",
            )
            content = result.content
            if not isinstance(content, str) or not content.strip():
                raise ValueError(
                    "prep LLM proxy returned empty content "
                    f"(output_tokens={result.output_tokens}, "
                    f"max_tokens={PREP_MAX_TOKENS})"
                )
            return content

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
                            agent_id=self._intervene_agent_id,
                            remiel_config=self._remiel_config,
                            prep_services=self._prep_services,
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

    async def _send_collect_log(
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
            await send_collect_debug_log(
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
