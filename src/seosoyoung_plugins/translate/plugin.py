"""Translate plugin.

Automatically translates messages in configured channels.
Detects language (Korean/English) and translates to the other.

Uses plugin_sdk.slack backend for all Slack API interactions.
"""

from __future__ import annotations

import logging
from typing import Any

from seosoyoung.plugin_sdk import HookContext, HookResult, Plugin, PluginMeta
from seosoyoung.plugin_sdk import slack

from seosoyoung_plugins.translate.detector import detect_language, Language
from seosoyoung_plugins.translate.translator import translate
from seosoyoung_plugins.translate.glossary import GlossaryMatchResult

logger = logging.getLogger(__name__)


class TranslatePlugin(Plugin):
    """자동 번역 플러그인.

    설정된 채널의 메시지를 자동 감지하여 한↔영 번역합니다.
    """

    meta = PluginMeta(
        name="translate",
        version="1.1.0",
        description="채널 메시지 자동 번역 (한↔영)",
    )

    async def on_load(self, config: dict[str, Any]) -> None:
        self._channels: list[str] = config["channels"]
        self._backend: str = config["backend"]
        self._model: str = config["model"]
        self._openai_model: str = config["openai_model"]
        self._api_key: str = config["api_key"]
        self._openai_api_key: str = config["openai_api_key"]
        self._context_count: int = config["context_count"]
        self._show_glossary: bool = config["show_glossary"]
        self._show_cost: bool = config["show_cost"]
        self._debug_channel: str = config["debug_channel"]
        self._glossary_path: str = config["glossary_path"]

        logger.info(
            "TranslatePlugin loaded: channels=%s, backend=%s",
            self._channels,
            self._backend,
        )

    async def on_unload(self) -> None:
        pass

    def register_hooks(self) -> dict:
        async def on_message(ctx: HookContext) -> tuple[HookResult, Any]:
            event = ctx.args["event"]

            channel = event.get("channel")
            if channel not in self._channels:
                return HookResult.SKIP, None

            handled = await self._process_translate(event)
            if handled:
                return HookResult.STOP, True
            return HookResult.SKIP, None

        return {"on_message": on_message}

    # -- public API --

    def translate_text(
        self, text: str
    ) -> tuple[str, float, list[tuple[str, str]], Language]:
        """텍스트를 번역합니다 (플러그인 설정 사용).

        명령어 핸들러 등 외부에서 직접 번역을 요청할 때 사용합니다.

        Args:
            text: 번역할 텍스트

        Returns:
            (번역된 텍스트, 비용 USD, 용어 목록, 원본 언어)
        """
        source_lang = detect_language(text)

        if self._backend == "openai":
            model, api_key = self._openai_model, self._openai_api_key
        else:
            model, api_key = self._model, self._api_key

        translated, cost, glossary_terms, _ = translate(
            text,
            source_lang,
            backend=self._backend,
            model=model,
            api_key=api_key,
            glossary_path=self._glossary_path,
        )
        return translated, cost, glossary_terms, source_lang

    # -- internal helpers (async, using plugin_sdk.slack) --

    async def _get_user_display_name(self, user_id: str) -> str:
        """사용자의 표시 이름을 가져옵니다."""
        try:
            user_info = await slack.get_user_info(user_id)
            if user_info is None:
                return user_id
            return (
                user_info.display_name
                or user_info.real_name
                or user_info.name
                or user_id
            )
        except Exception as e:
            logger.warning("사용자 정보 조회 실패: %s, %s", user_id, e)
            return user_id

    async def _get_context_messages(
        self, channel: str, thread_ts: str | None, limit: int
    ) -> list[dict]:
        """이전 메시지들을 컨텍스트로 가져옵니다."""
        try:
            if thread_ts:
                messages = await slack.get_thread_replies(
                    channel, thread_ts, limit=limit + 1
                )
            else:
                messages = await slack.get_channel_history(
                    channel, limit=limit + 1
                )

            # get_channel_history returns newest-first; reverse to chronological
            if not thread_ts:
                messages = list(reversed(messages))

            context = []
            for msg in messages[-limit:]:
                text = msg.text
                if text:
                    user_name = await self._get_user_display_name(msg.user)
                    context.append({"user": user_name, "text": text})

            return context

        except Exception as e:
            logger.warning("컨텍스트 메시지 조회 실패: %s", e)
            return []

    def _format_response(
        self,
        user_name: str,
        translated: str,
        source_lang: Language,
        cost: float,
        glossary_terms: list[tuple[str, str]] | None = None,
    ) -> str:
        """응답 메시지를 포맷팅합니다."""
        glossary_line = ""
        if self._show_glossary and glossary_terms:
            term_strs = [f"{src} ({tgt})" for src, tgt in glossary_terms]
            glossary_line = f"\n`📖 {', '.join(term_strs)}`"

        cost_line = f"\n`~💵${cost:.4f}`" if self._show_cost else ""

        if source_lang == Language.KOREAN:
            return f"`{user_name} said,`\n\"{translated}\"{glossary_line}{cost_line}"
        else:
            return f"`{user_name}님이`\n\"{translated}\"\n`라고 하셨습니다.`{glossary_line}{cost_line}"

    async def _send_debug_log(
        self,
        original_text: str,
        source_lang: Language,
        match_result: GlossaryMatchResult | None,
    ) -> None:
        """디버그 로그를 지정된 슬랙 채널에 전송합니다."""
        if not self._debug_channel or not match_result:
            return

        try:
            debug_info = match_result.debug_info

            lines = [
                f"*🔍 번역 디버그 로그* ({source_lang.value} → "
                f"{'en' if source_lang == Language.KOREAN else 'ko'})",
                f"```원문: {original_text[:100]}"
                f"{'...' if len(original_text) > 100 else ''}```",
                "",
                f"*추출된 단어 ({len(match_result.extracted_words)}개):*",
                f"`{', '.join(match_result.extracted_words[:20])}"
                f"{'...' if len(match_result.extracted_words) > 20 else ''}`",
                "",
            ]

            exact_matches = debug_info.get("exact_matches", [])
            if exact_matches:
                lines.append(f"*✅ 정확한 매칭 ({len(exact_matches)}개):*")
                for match in exact_matches[:10]:
                    lines.append(f"  • {match}")
                if len(exact_matches) > 10:
                    lines.append(f"  ... 외 {len(exact_matches) - 10}개")
                lines.append("")

            substring_matches = debug_info.get("substring_matches", [])
            if substring_matches:
                lines.append(f"*📎 부분 매칭 ({len(substring_matches)}개):*")
                for match in substring_matches[:10]:
                    lines.append(f"  • {match}")
                if len(substring_matches) > 10:
                    lines.append(f"  ... 외 {len(substring_matches) - 10}개")
                lines.append("")

            fuzzy_matches = debug_info.get("fuzzy_matches", [])
            if fuzzy_matches:
                lines.append(f"*🔮 퍼지 매칭 ({len(fuzzy_matches)}개):*")
                for match in fuzzy_matches[:10]:
                    lines.append(f"  • {match}")
                if len(fuzzy_matches) > 10:
                    lines.append(f"  ... 외 {len(fuzzy_matches) - 10}개")
                lines.append("")

            lines.append(
                f"*📖 최종 용어집 포함 ({len(match_result.matched_terms)}개):*"
            )
            if match_result.matched_terms:
                for src, tgt in match_result.matched_terms[:10]:
                    lines.append(f"  • {src} → {tgt}")
                if len(match_result.matched_terms) > 10:
                    lines.append(
                        f"  ... 외 {len(match_result.matched_terms) - 10}개"
                    )
            else:
                lines.append("  (없음)")

            await slack.send_message(
                self._debug_channel, "\n".join(lines)
            )

        except Exception as e:
            logger.warning("디버그 로그 전송 실패: %s", e)

    async def _process_translate(self, event: dict) -> bool:
        """메시지를 번역 처리합니다.

        Args:
            event: 슬랙 메시지 이벤트

        Returns:
            처리 여부 (True: 처리됨, False: 처리하지 않음)
        """
        # 봇 메시지 무시
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return False

        # 메시지 수정/삭제 이벤트 무시
        subtype = event.get("subtype")
        if subtype in ("message_changed", "message_deleted"):
            return False

        text = event.get("text", "").strip()
        if not text:
            return False

        channel = event.get("channel")
        user_id = event.get("user")
        thread_ts = event.get("thread_ts")
        message_ts = event.get("ts")

        try:
            # 번역 시작 리액션
            await slack.add_reaction(channel, message_ts, "hn-curious")

            # 언어 감지
            source_lang = detect_language(text)
            logger.info("번역 요청: %s -> %s...", source_lang.value, text[:30])

            # 컨텍스트 메시지 수집
            context_messages = await self._get_context_messages(
                channel, thread_ts, self._context_count
            )

            # 백엔드별 모델/키 선택
            if self._backend == "openai":
                model = self._openai_model
                api_key = self._openai_api_key
            else:
                model = self._model
                api_key = self._api_key

            # 번역
            translated, cost, glossary_terms, match_result = translate(
                text,
                source_lang,
                backend=self._backend,
                model=model,
                api_key=api_key,
                glossary_path=self._glossary_path,
                context_messages=context_messages,
            )

            # 디버그 로그 전송 (설정된 경우)
            await self._send_debug_log(text, source_lang, match_result)

            # 사용자 이름 조회
            user_name = await self._get_user_display_name(user_id)

            # 응답 포맷
            response = self._format_response(
                user_name, translated, source_lang, cost, glossary_terms
            )

            # 응답 위치: 스레드면 스레드에, 채널이면 채널에 (스레드 열지 않음)
            await slack.send_message(channel, response, thread_ts=thread_ts)

            # 번역 완료: 리액션 교체
            await slack.remove_reaction(channel, message_ts, "hn-curious")
            await slack.add_reaction(
                channel, message_ts, "hn_deal_rainbow"
            )

            logger.info("번역 응답 완료: %s", user_name)
            return True

        except Exception as e:
            logger.exception("번역 실패: %s", e)
            # 실패 시 리액션 교체
            try:
                await slack.remove_reaction(
                    channel, message_ts, "hn-curious"
                )
            except Exception:
                pass
            try:
                await slack.add_reaction(
                    channel, message_ts, "hn-embarrass"
                )
            except Exception:
                pass
            # 실패 이유를 같은 위치에 알림
            try:
                await slack.send_message(
                    channel, f"번역 실패: `{e}`", thread_ts=thread_ts
                )
            except Exception:
                pass
            return False
