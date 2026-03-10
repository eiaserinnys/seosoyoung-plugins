"""Memory plugin.

Observational Memory injection and observation triggering.
All configuration comes from memory.yaml, not from Config
singleton or environment variables.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from seosoyoung.plugin_sdk import HookContext, HookResult, Plugin, PluginMeta
from seosoyoung_plugins.soulstream_client import SoulstreamClient

logger = logging.getLogger(__name__)


class MemoryPlugin(Plugin):
    """Observational Memory plugin.

    Injects memory context before Claude execution (before_execute)
    and triggers observation pipeline after execution (after_execute).

    No self.enabled flag -- if loaded, it's active.
    """

    meta = PluginMeta(
        name="memory",
        version="1.0.0",
        description="Observational memory injection and observation",
    )

    async def on_load(self, config: dict[str, Any]) -> None:
        self._config = config

        # Core settings
        self._soulstream = SoulstreamClient(
            base_url=config["soulstream_url"],
            bearer_token=config["soulstream_token"],
        )
        self._model: str = config.get("model", "gpt-5-mini")
        self._memory_path: str = config["memory_path"]
        self._debug_channel: str = config.get("debug_channel", "")
        self._slack_bot_token: str = config.get("slack_bot_token", "")

        # Injection settings
        self._max_observation_tokens: int = config.get(
            "max_observation_tokens", 30000
        )

        # Observation pipeline settings
        self._min_turn_tokens: int = config.get("min_turn_tokens", 200)
        self._reflection_threshold: int = config.get(
            "reflection_threshold", 20000
        )
        self._promoter_model: str = config.get("promoter_model", "gpt-5.2")
        self._promotion_threshold: int = config.get(
            "promotion_threshold", 5000
        )
        self._persistent_compaction_threshold: int = config.get(
            "persistent_compaction_threshold", 15000
        )
        self._persistent_compaction_target: int = config.get(
            "persistent_compaction_target", 8000
        )

        # Emoji settings for debug logs
        self._emoji = config.get("emoji", {})

        logger.info(
            "MemoryPlugin loaded: model=%s, memory_path=%s (soulstream)",
            self._model,
            self._memory_path,
        )

    async def on_unload(self) -> None:
        await self._soulstream.close()
        logger.info("MemoryPlugin unloaded")

    def register_hooks(self) -> dict:
        return {
            "before_execute": self._on_before_execute,
            "after_execute": self._on_after_execute,
        }

    # -- Hook handlers ---------------------------------------------------------

    async def _on_before_execute(
        self, ctx: HookContext
    ) -> tuple[HookResult, Any]:
        """Inject memory context into the prompt before Claude execution.

        Expects ctx.args:
            thread_ts: str
            channel: str | None
            session_id: str | None
            prompt: str
            channel_observer_channels: list[str]  (from ChannelObserverPlugin)

        Returns:
            dict with "prompt" (modified) and "anchor_ts"
        """
        thread_ts = ctx.args.get("thread_ts", "")
        if not thread_ts:
            return HookResult.CONTINUE, None

        channel = ctx.args.get("channel")
        session_id = ctx.args.get("session_id")
        prompt = ctx.args.get("prompt", "")

        try:
            memory_prompt, anchor_ts = self._prepare_injection(
                thread_ts, channel, session_id, prompt,
                channel_observer_channels=ctx.args.get(
                    "channel_observer_channels", []
                ),
            )

            result = {"anchor_ts": anchor_ts}
            if memory_prompt:
                result["prompt"] = (
                    f"{memory_prompt}\n\n"
                    f"위 컨텍스트를 참고하여 질문에 답변해주세요.\n\n"
                    f"사용자의 질문: {prompt}"
                )

            return HookResult.CONTINUE, result

        except Exception as e:
            logger.warning(f"MemoryPlugin before_execute 실패 (무시): {e}")
            return HookResult.CONTINUE, None

    async def _on_after_execute(
        self, ctx: HookContext
    ) -> tuple[HookResult, Any]:
        """Trigger observation pipeline after Claude execution.

        Expects ctx.args:
            thread_ts: str
            user_id: str
            prompt: str
            collected_messages: list[dict]
            anchor_ts: str
        """
        thread_ts = ctx.args.get("thread_ts", "")
        user_id = ctx.args.get("user_id", "")
        prompt = ctx.args.get("prompt", "")
        collected_messages = ctx.args.get("collected_messages", [])
        anchor_ts = ctx.args.get("anchor_ts", "")

        if not thread_ts or not user_id:
            return HookResult.CONTINUE, None

        try:
            self._trigger_observation(
                thread_ts, user_id, prompt,
                collected_messages, anchor_ts,
            )
        except Exception as e:
            logger.warning(f"MemoryPlugin after_execute 실패 (무시): {e}")

        return HookResult.CONTINUE, None

    # -- on_compact_om_flag callback -------------------------------------------

    def on_compact_flag(self, thread_ts: str) -> None:
        """PreCompact 훅에서 OM inject 플래그 설정.

        Plugin hook이 아닌 직접 호출용 (on_compact 콜백에서 사용).
        """
        try:
            from seosoyoung_plugins.memory.store import MemoryStore

            store = MemoryStore(self._memory_path)
            record = store.get_record(thread_ts)
            if record and record.observations:
                store.set_inject_flag(thread_ts)
        except Exception as e:
            logger.warning(f"OM inject 플래그 설정 실패 (무시): {e}")

    # -- Internal logic --------------------------------------------------------

    def _prepare_injection(
        self,
        thread_ts: str,
        channel: str | None,
        session_id: str | None,
        prompt: str | None,
        channel_observer_channels: list[str] | None = None,
    ) -> tuple[str | None, str]:
        """OM 메모리 주입을 준비합니다.

        Returns:
            (memory_prompt, anchor_ts) 튜플
        """
        from seosoyoung_plugins.memory.context_builder import (
            ContextBuilder,
            InjectionResult,
        )
        from seosoyoung_plugins.memory.store import MemoryStore

        store = MemoryStore(self._memory_path)
        is_new_session = session_id is None
        should_inject_session = store.check_and_clear_inject_flag(thread_ts)

        # 채널 관찰: 관찰 대상 채널에서 멘션될 때만 주입
        channel_store = None
        include_channel_obs = False
        if (
            is_new_session
            and channel
            and channel_observer_channels
            and channel in channel_observer_channels
        ):
            from seosoyoung_plugins.channel_observer.store import (
                ChannelStore,
            )

            channel_store = ChannelStore(base_dir=self._memory_path)
            include_channel_obs = True

        builder = ContextBuilder(store, channel_store=channel_store)
        result: InjectionResult = builder.build_memory_prompt(
            thread_ts,
            max_tokens=self._max_observation_tokens,
            include_persistent=is_new_session,
            include_session=should_inject_session,
            include_channel_observation=include_channel_obs,
            channel_id=channel,
            include_new_observations=True,
        )

        memory_prompt: str | None = None
        if result.prompt:
            memory_prompt = result.prompt
            logger.info(
                f"OM 주입 준비 완료 (thread={thread_ts}, "
                f"LTM={result.persistent_tokens} tok, "
                f"새관찰={result.new_observation_tokens} tok, "
                f"세션={result.session_tokens} tok, "
                f"채널={result.channel_digest_tokens}"
                f"+{result.channel_buffer_tokens} tok)"
            )

        # 앵커 ts: 새 세션이면 생성, 기존 세션이면 MemoryRecord에서 로드
        anchor_ts = self._create_or_load_debug_anchor(
            thread_ts, session_id, store, prompt,
        )

        # 디버그 로그: 주입 정보
        self._send_injection_debug_log(thread_ts, result, anchor_ts)

        return memory_prompt, anchor_ts

    def _create_or_load_debug_anchor(
        self,
        thread_ts: str,
        session_id: str | None,
        store: Any,
        prompt: str | None,
    ) -> str:
        """디버그 앵커 메시지를 생성하거나 기존 앵커를 로드합니다."""
        if not self._debug_channel:
            return ""

        if session_id is not None:
            record = store.get_record(thread_ts)
            return getattr(record, "anchor_ts", "") or ""

        try:
            from seosoyoung_plugins.memory.observation_pipeline import (
                _send_debug_log,
            )
            from seosoyoung_plugins.memory.store import MemoryRecord

            safe_prompt = prompt or ""
            preview = safe_prompt[:80]
            if len(safe_prompt) > 80:
                preview += "\u2026"

            emoji = self._emoji.get(
                "text_session_start", ":ssy-surprise:"
            )
            anchor_ts = _send_debug_log(
                self._debug_channel,
                f"{emoji} *OM | 세션 시작 감지* "
                f"`{thread_ts}`\n>{preview}",
                bot_token=self._slack_bot_token,
            )
            if anchor_ts:
                record = store.get_record(thread_ts)
                if record is None:
                    record = MemoryRecord(thread_ts=thread_ts)
                record.anchor_ts = anchor_ts
                store.save_record(record)
            return anchor_ts or ""
        except Exception as e:
            logger.warning(f"OM 앵커 메시지 생성 실패 (무시): {e}")
            return ""

    def _send_injection_debug_log(
        self,
        thread_ts: str,
        result: Any,
        anchor_ts: str,
    ) -> None:
        """디버그 이벤트: 주입 정보를 슬랙에 발송."""
        if not self._debug_channel or not anchor_ts:
            return

        has_any = (
            result.persistent_tokens
            or result.session_tokens
            or result.channel_digest_tokens
            or result.channel_buffer_tokens
            or result.new_observation_tokens
        )
        if not has_any:
            return

        try:
            from seosoyoung_plugins.memory.observation_pipeline import (
                _blockquote,
                _format_tokens,
                _send_debug_log,
            )

            sid = thread_ts
            emoji = self._emoji

            if result.persistent_tokens:
                ltm_quote = _blockquote(result.persistent_content)
                _send_debug_log(
                    self._debug_channel,
                    f"{emoji.get('text_ltm_inject', ':ssy-thinking:')} "
                    f"*OM 장기 기억 주입* `{sid}`\n"
                    f">`LTM {_format_tokens(result.persistent_tokens)} tok`\n"
                    f"{ltm_quote}",
                    thread_ts=anchor_ts,
                    bot_token=self._slack_bot_token,
                )

            if result.new_observation_tokens:
                new_obs_quote = _blockquote(result.new_observation_content)
                _send_debug_log(
                    self._debug_channel,
                    f"{emoji.get('text_new_obs_inject', ':ssy-curious:')} "
                    f"*OM 새 관찰 주입* `{sid}`\n"
                    f">`새관찰 "
                    f"{_format_tokens(result.new_observation_tokens)} tok`\n"
                    f"{new_obs_quote}",
                    thread_ts=anchor_ts,
                    bot_token=self._slack_bot_token,
                )

            if result.session_tokens:
                session_quote = _blockquote(result.session_content)
                _send_debug_log(
                    self._debug_channel,
                    f"{emoji.get('text_session_obs_inject', ':ssy-thinking:')} "
                    f"*OM 세션 관찰 주입* `{sid}`\n"
                    f">`세션 "
                    f"{_format_tokens(result.session_tokens)} tok`\n"
                    f"{session_quote}",
                    thread_ts=anchor_ts,
                    bot_token=self._slack_bot_token,
                )

            if result.channel_digest_tokens or result.channel_buffer_tokens:
                ch_total = (
                    result.channel_digest_tokens + result.channel_buffer_tokens
                )
                _send_debug_log(
                    self._debug_channel,
                    f"{emoji.get('text_channel_obs_inject', ':ssy-curious:')} "
                    f"*채널 관찰 주입* `{sid}`\n"
                    f">`digest "
                    f"{_format_tokens(result.channel_digest_tokens)} tok + "
                    f"buffer "
                    f"{_format_tokens(result.channel_buffer_tokens)} tok = "
                    f"총 {_format_tokens(ch_total)} tok`",
                    thread_ts=anchor_ts,
                    bot_token=self._slack_bot_token,
                )
        except Exception as e:
            logger.warning(f"OM 주입 디버그 로그 실패 (무시): {e}")

    def _trigger_observation(
        self,
        thread_ts: str,
        user_id: str,
        prompt: str,
        collected_messages: list[dict],
        anchor_ts: str = "",
    ) -> None:
        """관찰 파이프라인을 별도 스레드에서 비동기로 트리거."""
        # tool_use/tool_result 메시지 필터링
        text_messages = [
            m
            for m in collected_messages
            if m.get("role") != "tool"
            and not (m.get("content", "").startswith("[tool_use:"))
        ]
        messages = [{"role": "user", "content": prompt}] + text_messages

        def _run_in_thread():
            try:
                from seosoyoung_plugins.memory.observation_pipeline import (
                    observe_conversation,
                )
                from seosoyoung_plugins.memory.observer import (
                    Observer,
                )
                from seosoyoung_plugins.memory.promoter import (
                    Compactor,
                    Promoter,
                )
                from seosoyoung_plugins.memory.reflector import (
                    Reflector,
                )
                from seosoyoung_plugins.memory.store import (
                    MemoryStore,
                )

                store = MemoryStore(self._memory_path)
                observer = Observer(
                    soulstream_client=self._soulstream,
                    model=self._model,
                )
                reflector = Reflector(
                    soulstream_client=self._soulstream,
                    model=self._model,
                )
                promoter = Promoter(
                    soulstream_client=self._soulstream,
                    model=self._promoter_model,
                )
                compactor = Compactor(
                    soulstream_client=self._soulstream,
                    model=self._promoter_model,
                )
                asyncio.run(
                    observe_conversation(
                        store=store,
                        observer=observer,
                        thread_ts=thread_ts,
                        user_id=user_id,
                        messages=messages,
                        min_turn_tokens=self._min_turn_tokens,
                        reflector=reflector,
                        reflection_threshold=self._reflection_threshold,
                        promoter=promoter,
                        promotion_threshold=self._promotion_threshold,
                        compactor=compactor,
                        compaction_threshold=(
                            self._persistent_compaction_threshold
                        ),
                        compaction_target=(
                            self._persistent_compaction_target
                        ),
                        debug_channel=self._debug_channel,
                        anchor_ts=anchor_ts,
                        slack_bot_token=self._slack_bot_token,
                        emoji_obs_complete=self._emoji.get(
                            "text_obs_complete", ":ssy-happy:"
                        ),
                    )
                )
            except Exception as e:
                logger.error(
                    f"OM 관찰 파이프라인 비동기 실행 오류 (무시): {e}"
                )
                try:
                    from seosoyoung_plugins.memory.observation_pipeline import (
                        _send_debug_log,
                    )

                    if self._debug_channel:
                        _send_debug_log(
                            self._debug_channel,
                            f"\u274c *OM 스레드 오류*\n"
                            f"\u2022 user: `{user_id}`\n"
                            f"\u2022 thread: `{thread_ts}`\n"
                            f"\u2022 error: `{e}`",
                            thread_ts=anchor_ts,
                            bot_token=self._slack_bot_token,
                        )
                except Exception:
                    pass

        thread = threading.Thread(target=_run_in_thread, daemon=True)
        thread.start()
        logger.info(
            f"OM 관찰 파이프라인 트리거됨 "
            f"(user={user_id}, thread={thread_ts})"
        )
