"""채널 소화/판단 파이프라인

pending 버퍼에 쌓인 메시지를 기반으로:
1. pending 토큰 확인 → threshold_A 미만이면 스킵
2. judged + pending 합산 > threshold_B이면 → digest() 호출 (judged를 digest에 편입)
3. judge() 호출 (digest + judged + pending → 메시지별 리액션 판단)
4. 리액션 처리 (이모지 일괄 + 확률 기반 개입 판단 + 슬랙 발송)
5. pending을 judged로 이동

이 모듈은 기존 테스트가 내부 정책 함수와 SDK patch surface를 직접 참조하는 호환
facade라 500줄을 초과한다. 준비 및 외부 wire의 세부 구현은 별도 모듈에 두고,
여기에는 orchestration만 둔다.
"""

import asyncio
import hashlib
import inspect
import json
import logging
import math
import os
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from seosoyoung.plugin_sdk import mention, slack, soulstream
from seosoyoung.plugin_sdk.caller_info import (
    build_bot_caller_info,
    get_host_preferred_node,
)
from seosoyoung.plugin_sdk.slack import Message, get_thread_replies as _sdk_get_thread_replies
from seosoyoung_plugins.channel_observer.collector import (
    COLLECT_EXCLUDED_SUBTYPES,
)
from seosoyoung_plugins.channel_observer.emoji_frequency import (
    EmojiFrequencyStore,
    normalize_emoji_name,
)
from seosoyoung_plugins.channel_observer.intervention import (
    InterventionAction,
    InterventionHistory,
    burst_intervention_probability,
    execute_interventions,
    intervention_probability,
    send_debug_log,
    send_intervention_probability_debug_log,
    send_multi_judge_debug_log,
)
from seosoyoung_plugins.channel_observer.intervene_memory import (
    drain_background,
    parse_record_tags,
    render_channel_memory_item,
    render_member_facts_item,
    spawn_background,
)
from seosoyoung_plugins.channel_observer.intervention_prep import (
    KST,
    InterventionPrepServices,
    build_intervention_prep,
    run_and_name_session,
)
from seosoyoung_plugins.channel_observer.jev_judge import (
    JevChannelJudge,
    JevMessageVerdict,
)
from seosoyoung_plugins.channel_observer.observer import (
    ChannelObserver,
    ChannelObserverResult,
    DigestCompressor,
    JudgeItem,
    JudgeResult,
)
from seosoyoung_plugins.channel_observer.prompts import (
    DisplayNameResolver,
)
from seosoyoung_plugins.channel_observer.remiel_context import (
    RemielContextConfig,
    build_remiel_context_item,
)
from seosoyoung_plugins.channel_observer.store import ChannelStore
from seosoyoung_plugins.memory.token_counter import TokenCounter

logger = logging.getLogger(__name__)

DEFAULT_CHANNEL_PAS_MODEL = {
    "model_preset": "gemini-4-argon",
    "reasoning_effort": "medium",
    "node_id": "eias-linegames-wsl",
}
DEFAULT_CHANNEL_PAS_FALLBACK_MODELS = [
    {
        "model_preset": "claude-opus",
        "reasoning_effort": "medium",
        "node_id": "eias-linegames-wsl",
    },
    {
        "model_preset": "codex-6.1-sol",
        "reasoning_effort": "medium",
        "node_id": "eias-linegames",
    },
]


async def _call_with_supported_kwargs(function: Callable, **kwargs):
    """구형 테스트 mock과 이전 backend가 모르는 키워드 인자를 받지 않게 합니다."""
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        accepted = kwargs
    else:
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
            accepted = kwargs
        else:
            accepted = {key: value for key, value in kwargs.items() if key in signature.parameters}
    return await function(**accepted)


def _ts_sort_key(message: dict[str, Any] | str) -> tuple[int, float, str]:
    raw_ts = str(message.get("ts") or "") if isinstance(message, dict) else str(message or "")
    try:
        return (0, float(raw_ts), raw_ts)
    except ValueError:
        return (1, 0.0, raw_ts)


def _pas_message_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        return dict(message)
    return {
        "ts": str(getattr(message, "ts", "") or ""),
        "user": getattr(message, "user", None),
        "text": getattr(message, "text", "") or "",
        "thread_ts": getattr(message, "thread_ts", None),
        "bot_id": getattr(message, "bot_id", None),
    }


async def _build_channel_pas_delta(
    *,
    store: ChannelStore,
    channel_id: str,
    pas_state: dict[str, Any],
    target_ts: str | None,
    target_message: dict[str, Any] | None,
    current_thread_ts: str | None,
    pending_messages: list[dict[str, Any]],
    thread_buffers: dict[str, list[dict]] | None,
    bot_user_id: str | None,
    recent_messages_count: int,
    judgment_reason: str | None,
    buffer_threshold: int,
    compressor: Any = None,
) -> dict[str, Any]:
    """읽음 커서 이후 채널 메시지와 스레드 답글을 시간순 Delta로 묶습니다."""
    read_ts = str(pas_state.get("read_ts") or "")
    read_thread_ts_map = dict(pas_state.get("read_thread_ts_map") or {})
    try:
        history_raw = await slack.get_channel_history(
            channel_id, limit=max(recent_messages_count, 1)
        )
    except Exception as error:
        logger.warning("PAS Delta 최근 이력 조회 실패 (%s): %s", channel_id, error)
        history_raw = []

    roots_by_ts: dict[str, dict[str, Any]] = {}
    reply_candidates: dict[str, dict[str, dict[str, Any]]] = {}
    for source in (
        store.load_pending(channel_id),
        store.load_judged(channel_id),
        pending_messages,
        [_pas_message_dict(item) for item in history_raw],
        [target_message] if target_message else [],
    ):
        for raw in source:
            message = _pas_message_dict(raw)
            ts = str(message.get("ts") or "")
            if not ts:
                continue
            thread_ts = str(message.get("thread_ts") or "")
            is_reply = bool(thread_ts and thread_ts != ts)
            if is_reply:
                reply_candidates.setdefault(thread_ts, {})[ts] = message
                continue
            if message.get("bot_id") or (
                bot_user_id and message.get("user") == bot_user_id
            ):
                continue
            roots_by_ts[ts] = {**roots_by_ts.get(ts, {}), **message}

    if read_ts:
        root_delta = [
            message for ts, message in roots_by_ts.items()
            if _ts_sort_key(ts) > _ts_sort_key(read_ts)
        ]
    else:
        history_ts = {
            str(message.get("ts") or "")
            for message in [_pas_message_dict(item) for item in history_raw]
            if message.get("ts")
        }
        latest_history = sorted(
            (message for ts, message in roots_by_ts.items() if ts in history_ts),
            key=_ts_sort_key,
        )[-recent_messages_count:]
        recent_ts = {str(message.get("ts")) for message in latest_history}
        pending_ts = {
            str(message.get("ts") or "")
            for message in [*store.load_pending(channel_id), *pending_messages]
            if message.get("ts")
        }
        root_delta = [
            message for ts, message in roots_by_ts.items()
            if ts in recent_ts or ts in pending_ts
        ]
    root_delta.sort(key=_ts_sort_key)

    if inspect.getattr_static(store, "load_all_buffered_threads", None) is not None:
        reply_sources = store.load_all_buffered_threads(channel_id)
    elif inspect.getattr_static(store, "load_all_thread_buffers", None) is not None:
        reply_sources = store.load_all_thread_buffers(channel_id)
    else:
        reply_sources = {}
    if not isinstance(reply_sources, dict):
        reply_sources = {}
    for thread_ts, messages_by_ts in reply_candidates.items():
        existing = {str(message.get("ts") or ""): message for message in reply_sources.get(thread_ts, [])}
        existing.update(messages_by_ts)
        reply_sources[thread_ts] = list(existing.values())
    buffered_thread_ts = {str(current_thread_ts)} if current_thread_ts else set()
    if target_ts and thread_buffers and target_ts in thread_buffers:
        buffered_thread_ts.add(str(target_ts))
    if thread_buffers:
        buffered_thread_ts.update(
            thread_ts
            for thread_ts in reply_candidates
            if thread_ts in thread_buffers
        )
    selected_buffers = {
        thread_ts: thread_buffers.get(thread_ts, [])
        for thread_ts in buffered_thread_ts
    } if thread_buffers else {}
    for thread_ts, messages in selected_buffers.items():
        current = reply_sources.setdefault(str(thread_ts), [])
        by_ts = {str(message.get("ts") or ""): message for message in current}
        for message in messages:
            ts = str(message.get("ts") or "")
            if ts:
                by_ts[ts] = {**by_ts.get(ts, {}), **message}
        reply_sources[str(thread_ts)] = list(by_ts.values())

    reply_delta: dict[str, list[dict[str, Any]]] = {}
    for thread_ts, messages in reply_sources.items():
        cursor = str(read_thread_ts_map.get(thread_ts) or read_ts)
        selected = []
        for raw in messages:
            message = _pas_message_dict(raw)
            ts = str(message.get("ts") or "")
            if not ts or (cursor and _ts_sort_key(ts) <= _ts_sort_key(cursor)):
                continue
            if message.get("bot_id") or (
                bot_user_id and message.get("user") == bot_user_id
            ):
                continue
            selected.append(message)
        if selected:
            reply_delta[thread_ts] = sorted(selected, key=_ts_sort_key)

    if not root_delta and not reply_delta:
        return {"empty": True}

    resolver = _make_resolver()

    def format_message(message: dict[str, Any]) -> str:
        user_id = str(message.get("user") or "unknown")
        display_name = resolver.resolve(user_id) if resolver else None
        speaker = (
            display_name
            if display_name and (user_id in display_name or display_name == user_id)
            else f"{display_name} ({user_id})" if display_name else user_id
        )
        return f"[{channel_id}:{message.get('ts')}] <{speaker}>: {message.get('text') or ''}"

    lines = [format_message(message) for message in root_delta]
    roots_by_ts_full = dict(roots_by_ts)
    if target_message:
        roots_by_ts_full[str(target_message.get("ts") or "")] = target_message
    if reply_delta:
        lines.append("## 스레드 답글")
        for thread_ts in sorted(reply_delta, key=_ts_sort_key):
            root = roots_by_ts_full.get(thread_ts, {})
            lines.append(
                f"### [{channel_id}:{thread_ts}] 스레드 "
                f"(원문: {root.get('text') or '원문 없음'})"
            )
            lines.extend(
                f"  {format_message(reply)}" for reply in reply_delta[thread_ts]
            )

    target_label = target_ts or (
        str(target_message.get("ts")) if target_message else
        (str(max(root_delta, key=_ts_sort_key).get("ts")) if root_delta else "")
    )
    lines.append(f"이번 개입 트리거: [{channel_id}:{target_label}]")
    lines.append(f"Jev 판단 사유: {judgment_reason or '(없음)'}")
    delta_text = "\n".join(lines)
    messages_for_snapshot = [*root_delta, *(item for group in reply_delta.values() for item in group)]
    messages_for_snapshot.sort(key=_ts_sort_key)
    if TokenCounter().count_string(delta_text) > buffer_threshold and len(messages_for_snapshot) > recent_messages_count:
        keep = set(id(item) for item in messages_for_snapshot[-recent_messages_count:])
        older_text = "\n".join(
            format_message(item) for item in messages_for_snapshot if id(item) not in keep
        )
        compressed = None
        if compressor is not None:
            try:
                compressed = await compressor.compress(
                    digest=older_text,
                    target_tokens=max(buffer_threshold // 2, 1),
                )
            except Exception as error:
                logger.warning("PAS Delta 압축 실패 (%s): %s", channel_id, error)
        if compressed:
            summary = compressed.digest if hasattr(compressed, "digest") else str(compressed)
        else:
            summary = f"이전 대화 {len(messages_for_snapshot) - recent_messages_count}건은 생략했습니다."
        recent_text = "\n".join(format_message(item) for item in messages_for_snapshot[-recent_messages_count:])
        delta_text = f"## 이전 대화 요약\n{summary}\n## 최근 대화\n{recent_text}\n" + "\n".join(lines[-2:])

    snapshot_ts = {str(message.get("ts")) for message in root_delta if message.get("ts")}
    snapshot_thread_msg_ts = {
        thread_ts: {str(message.get("ts")) for message in messages if message.get("ts")}
        for thread_ts, messages in reply_delta.items()
    }
    snapshot_thread_ts = set(snapshot_thread_msg_ts)
    all_ts = [*snapshot_ts, *(ts for values in snapshot_thread_msg_ts.values() for ts in values)]
    batch_payload = {
        "channel_id": channel_id,
        "root_ts": max(snapshot_ts, key=_ts_sort_key) if snapshot_ts else "",
        "thread_ts": {
            key: max(value, key=_ts_sort_key)
            for key, value in sorted(snapshot_thread_msg_ts.items()) if value
        },
        "target_ts": target_label,
    }
    digest = hashlib.sha1(
        json.dumps(batch_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "empty": False,
        "text": delta_text,
        "roots": root_delta,
        "replies": reply_delta,
        "messages": messages_for_snapshot,
        "snapshot_ts": snapshot_ts,
        "snapshot_thread_ts": snapshot_thread_ts,
        "snapshot_thread_msg_ts": snapshot_thread_msg_ts,
        "batch_id": f"chan-pas-{channel_id}-{digest}",
        "target_ts": target_label,
    }


def _intervention_thinking_emoji() -> str:
    """개입 '생각 중' 이모지 — 호출 시점에 환경변수를 읽어 dotenv 로딩 순서에 무관하게 동작"""
    return os.environ.get("EMOJI_INTERVENTION_THINKING", "thinking_face")


def _intervention_complete_emoji() -> str:
    """개입 완료 이모지 — 호출 시점에 환경변수를 읽어 dotenv 로딩 순서에 무관하게 동작"""
    return os.environ.get("EMOJI_INTERVENTION_COMPLETE", "white_check_mark")


def _judge_result_to_observer_result(
    judge: JudgeResult, digest: str = "",
) -> ChannelObserverResult:
    """JudgeResult를 ChannelObserverResult로 변환 (하위호환 인터페이스용)"""
    return ChannelObserverResult(
        digest=digest,
        importance=judge.importance,
        reaction_type=judge.reaction_type,
        reaction_target=judge.reaction_target,
        reaction_content=judge.reaction_content,
    )


def _parse_judge_item_action(item: JudgeItem) -> InterventionAction | None:
    """JudgeItem에서 InterventionAction을 생성합니다. 반응이 없으면 None."""
    if item.reaction_type == "none":
        return None

    if item.reaction_type == "react" and item.reaction_target and item.reaction_content:
        return InterventionAction(
            type="react",
            target=item.reaction_target,
            content=item.reaction_content,
        )

    if item.reaction_type == "intervene" and item.reaction_target and item.reaction_content:
        return InterventionAction(
            type="message",
            target=item.reaction_target,
            content=item.reaction_content,
        )

    return None


def _parse_judge_actions(judge_result: JudgeResult) -> list[InterventionAction]:
    """JudgeResult에서 InterventionAction 리스트를 생성합니다.

    items가 있으면 각 JudgeItem에서 액션을 추출합니다.
    없으면 하위호환 단일 필드에서 추출합니다.
    """
    if judge_result.items:
        actions = []
        for item in judge_result.items:
            action = _parse_judge_item_action(item)
            if action:
                actions.append(action)
        return actions

    # 하위호환: 단일 필드
    if judge_result.reaction_type == "none":
        return []

    if judge_result.reaction_type == "react" and judge_result.reaction_target:
        return [InterventionAction(
            type="react",
            target=judge_result.reaction_target,
            content=judge_result.reaction_content,
        )]

    if judge_result.reaction_type == "intervene" and judge_result.reaction_target:
        return [InterventionAction(
            type="message",
            target=judge_result.reaction_target,
            content=judge_result.reaction_content,
        )]

    return []


def _apply_importance_modifiers(
    judge_result: JudgeResult,
    pending_messages: list[dict],
) -> None:
    """related_to_me 가중치와 addressed_to_me 강제 반응을 적용합니다.

    - related_to_me == True → importance × 2 (상한 10)
    - addressed_to_me == True && 발신자가 사람 → importance 최소 7, intervene 전환
    """
    msg_by_ts: dict[str, dict] = {}
    for msg in pending_messages:
        ts = msg.get("ts", "")
        if ts:
            msg_by_ts[ts] = msg

    for item in judge_result.items:
        # related_to_me 가중치
        if item.related_to_me:
            item.importance = min(item.importance * 2, 10)

        # addressed_to_me 강제 반응: 발신자가 사람(bot_id 없음)일 때
        if item.addressed_to_me:
            orig_msg = msg_by_ts.get(item.ts, {})
            is_bot = bool(orig_msg.get("bot_id"))
            if not is_bot:
                item.importance = max(item.importance, 7)
                if item.reaction_type != "intervene":
                    item.reaction_type = "intervene"
                    item.reaction_target = item.reaction_target or item.ts
                    if not item.reaction_content:
                        item.reaction_content = "(addressed)"


def _validate_linked_messages(
    judge_result: JudgeResult,
    judged_messages: list[dict],
    pending_messages: list[dict],
    thread_buffers: dict[str, list[dict]] | None = None,
) -> None:
    """linked_message_ts가 실제 존재하는 ts인지 검증하고, 환각된 ts를 제거합니다.

    자기 자신을 가리키는 링크도 제거합니다.
    """
    known_ts: set[str] = set()
    for msg in judged_messages:
        ts = msg.get("ts", "")
        if ts:
            known_ts.add(ts)
    for msg in pending_messages:
        ts = msg.get("ts", "")
        if ts:
            known_ts.add(ts)
    # Bug B: thread_buffers 메시지 ts도 known_ts에 포함
    if thread_buffers:
        for msgs in thread_buffers.values():
            for msg in msgs:
                ts = msg.get("ts", "")
                if ts:
                    known_ts.add(ts)

    for item in judge_result.items:
        if item.linked_message_ts is None:
            continue
        # 자기 자신을 가리키거나 존재하지 않는 ts → 제거
        if item.linked_message_ts == item.ts or item.linked_message_ts not in known_ts:
            logger.debug(
                f"linked_message_ts 환각 제거: {item.ts} → {item.linked_message_ts}"
            )
            item.linked_message_ts = None
            item.link_reason = None


def _get_max_importance_item(judge_result: JudgeResult) -> JudgeItem | None:
    """JudgeResult에서 가장 높은 중요도의 JudgeItem을 반환합니다."""
    if not judge_result.items:
        return None
    return max(judge_result.items, key=lambda item: item.importance)


def _filter_already_reacted(
    actions: list[InterventionAction],
    pending_messages: list[dict],
    bot_user_id: str | None,
) -> list[InterventionAction]:
    """봇이 이미 리액션한 메시지에 대한 react 액션을 필터링합니다.

    pending_messages의 reactions 필드에 봇이 같은 이모지로 이미 리액션한 경우 스킵합니다.

    Args:
        actions: react 타입 InterventionAction 리스트
        pending_messages: pending 메시지 리스트 (reactions 필드 포함 가능)
        bot_user_id: 봇의 사용자 ID

    Returns:
        중복이 아닌 액션만 남긴 리스트
    """
    if not bot_user_id or not actions:
        return actions

    # ts → reactions 인덱스 빌드
    reactions_by_ts: dict[str, list[dict]] = {}
    for msg in pending_messages:
        ts = msg.get("ts", "")
        reactions = msg.get("reactions")
        if ts and reactions:
            reactions_by_ts[ts] = reactions

    filtered = []
    for action in actions:
        reactions = reactions_by_ts.get(action.target, [])
        already = any(
            r.get("name") == action.content and bot_user_id in r.get("users", [])
            for r in reactions
        )
        if already:
            logger.debug(
                f"react 스킵 (이미 리액션함): ts={action.target} emoji={action.content}"
            )
        else:
            filtered.append(action)
    return filtered


def _filter_mention_thread_actions(
    actions: list[InterventionAction],
    mention_handled_ts: set[str],
) -> list[InterventionAction]:
    """멘션으로 처리 중인 스레드에 대한 액션을 필터링합니다.

    멘션 스레드 메시지는 소화(consume)는 정상 처리하되,
    리액션이나 개입은 수행하지 않습니다.

    Args:
        actions: InterventionAction 리스트
        mention_handled_ts: 멘션으로 처리 중인 메시지 ts 집합

    Returns:
        멘션 스레드 대상을 제외한 액션 리스트
    """
    if not mention_handled_ts or not actions:
        return actions

    filtered = []
    for action in actions:
        if action.target in mention_handled_ts:
            logger.debug(
                f"멘션 스레드 액션 필터링: type={action.type}, target={action.target}"
            )
        else:
            filtered.append(action)
    return filtered


async def run_channel_pipeline(
    store: ChannelStore,
    observer: ChannelObserver,
    channel_id: str,
    cooldown: InterventionHistory,
    threshold_a: int = 150,
    threshold_b: int = 5000,
    compressor: Optional[DigestCompressor] = None,
    digest_max_tokens: int = 10_000,
    digest_target_tokens: int = 5_000,
    debug_channel: str = "",
    intervention_threshold: float = 0.3,
    react_probability: float = 1.0,
    llm_call: Optional[Callable] = None,
    bot_user_id: str | None = None,
    recent_messages_count: int = 5,
    buffer_threshold: int = 150,
    intervene_model: str | None = None,
    folder_id: str | None = None,
    agent_id: str | None = None,
    pas_default_model: dict[str, Any] | None = None,
    pas_fallback_models: list[dict[str, Any]] | None = None,
    pas_folder_id: str | None = None,
    remiel_config: RemielContextConfig | None = None,
    prep_services: InterventionPrepServices | None = None,
    **kwargs,
) -> None:
    """소화/판단 분리 파이프라인을 실행합니다.

    흐름:
    a) pending 토큰 확인 → threshold_A 미만이면 스킵
    b) judged + pending 합산 > threshold_B이면 → digest() 호출 (judged를 편입)
    c) judge() 호출 (digest + judged + pending → 메시지별 판단)
    d) 리액션 처리 (이모지 일괄 + 확률 기반 개입 판단 + 슬랙 발송)
    e) pending을 judged로 이동
    """
    token_counter = TokenCounter()

    # a) pending 토큰 확인
    pending_tokens = store.count_pending_tokens(channel_id)
    if pending_tokens <= 0:
        logger.debug(f"파이프라인 스킵 ({channel_id}): pending 토큰 없음")
        return
    if pending_tokens < threshold_a:
        logger.debug(
            f"파이프라인 스킵 ({channel_id}): "
            f"pending {pending_tokens} tok < threshold_A {threshold_a}"
        )
        return

    # b) judged + pending 합산 > threshold_B이면 → digest 편입
    judged_plus_pending = store.count_judged_plus_pending_tokens(channel_id)
    if judged_plus_pending > threshold_b:
        judged_messages = store.load_judged(channel_id)
        if judged_messages:
            digest_data = store.get_digest(channel_id)
            existing_digest = digest_data["content"] if digest_data else None

            logger.info(
                f"digest 편입 시작 ({channel_id}): "
                f"judged+pending {judged_plus_pending} tok > threshold_B {threshold_b}"
            )

            digest_result = await observer.digest(
                channel_id=channel_id,
                existing_digest=existing_digest,
                judged_messages=judged_messages,
            )

            if digest_result:
                store.save_digest(
                    channel_id,
                    content=digest_result.digest,
                    meta={
                        "token_count": digest_result.token_count,
                        "last_digested_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                store.clear_judged(channel_id)

                logger.info(
                    f"digest 편입 완료 ({channel_id}): "
                    f"digest {digest_result.token_count} tok"
                )

                # digest 압축 트리거
                if compressor and digest_result.token_count > digest_max_tokens:
                    compress_result = await compressor.compress(
                        digest=digest_result.digest,
                        target_tokens=digest_target_tokens,
                    )
                    if compress_result:
                        store.save_digest(
                            channel_id,
                            content=compress_result.digest,
                            meta={
                                "token_count": compress_result.token_count,
                                "last_digested_at": datetime.now(timezone.utc).isoformat(),
                                "last_compressed_at": datetime.now(timezone.utc).isoformat(),
                            },
                        )
            else:
                logger.warning(f"digest 편입 실패 ({channel_id})")

    # c) judge() 호출 — 스냅샷 시점의 pending/thread를 기록해두고,
    #    파이프라인 완료 후에는 스냅샷에 포함된 메시지만 judged로 이동합니다.
    digest_data = store.get_digest(channel_id)
    current_digest = digest_data["content"] if digest_data else None
    judged_messages = store.load_judged(channel_id)
    pending_messages = store.load_pending(channel_id)
    thread_buffers = store.load_all_thread_buffers(channel_id)

    # 스냅샷 ts 기록 — 멘션 여부와 무관하게 모든 pending을 포함
    # 멘션 스레드 메시지도 정상적으로 judged로 이동(소화)되어야 합니다.
    snapshot_ts = {m.get("ts", "") for m in pending_messages if m.get("ts")}
    snapshot_thread_ts = set(thread_buffers.keys()) if thread_buffers else None
    snapshot_thread_msg_ts = {
        thread_ts: {
            str(message.get("ts"))
            for message in messages
            if message.get("ts")
        }
        for thread_ts, messages in (thread_buffers or {}).items()
    }

    # 멘션 스레드 식별: 리액션/개입 필터링에 사용 (소화는 정상 처리)
    mention_handled_ts: set[str] = set()
    _mention_available = mention.get_backend() is not None
    if _mention_available:
        for m in pending_messages:
            thread = m.get("thread_ts", "")
            ts = m.get("ts", "")
            if mention.is_handled(thread) or mention.is_handled(ts):
                mention_handled_ts.add(ts)

        # judge에는 멘션 스레드를 제외한 pending만 전달 (토큰 절약)
        judge_pending = [
            m for m in pending_messages
            if m.get("ts", "") not in mention_handled_ts
        ]
        judge_thread_buffers = (
            {ts: msgs for ts, msgs in thread_buffers.items()
             if not mention.is_handled(ts)}
            if thread_buffers else thread_buffers
        )
        filtered_count = len(pending_messages) - len(judge_pending)
        if filtered_count > 0:
            logger.info(
                f"멘션 스레드 필터링 ({channel_id}): "
                f"judge에서 pending {filtered_count}건 제외 (소화는 정상 처리)"
            )
    else:
        judge_pending = pending_messages
        judge_thread_buffers = thread_buffers

    logger.info(
        f"리액션 판단 시작 ({channel_id}): "
        f"pending {len(judge_pending)}건 (전체 {len(pending_messages)}건), "
        f"judged {len(judged_messages)}건, "
        f"threads {len(judge_thread_buffers) if judge_thread_buffers else 0}건"
    )

    # Bug 2 fix: 필터링 후 판단할 메시지가 없으면 judge 호출을 건너뛴다.
    # count_pending_tokens()는 스레드 버퍼를 합산하므로 threshold를 통과하지만
    # 멘션 필터링이나 실제 load_pending이 빈 경우 빈 LLM 호출이 발생한다.
    # thread_buffers는 컨텍스트일 뿐 판단 대상은 judge_pending이다.
    if not judge_pending:
        logger.debug(
            f"판단할 메시지 없음, judge 스킵 ({channel_id}): "
            f"pending {len(pending_messages)}건은 모두 필터링됨"
        )
        store.move_snapshot_to_judged(
            channel_id, snapshot_ts, snapshot_thread_ts, snapshot_thread_msg_ts
        )
        return

    judge_result = await observer.judge(
        channel_id=channel_id,
        digest=current_digest,
        judged_messages=judged_messages,
        pending_messages=judge_pending,
        thread_buffers=judge_thread_buffers,
        bot_user_id=bot_user_id,

    )

    # Bug 1 fix: judge가 None이어도 스냅샷을 judged로 이동해야 한다.
    # 이동하지 않으면 스레드 버퍼가 pending에 영원히 남아 무한 루프를 유발한다.
    if judge_result is None:
        logger.warning(f"judge가 None 반환 ({channel_id})")
        store.move_snapshot_to_judged(
            channel_id, snapshot_ts, snapshot_thread_ts, snapshot_thread_msg_ts
        )
        return

    # d-0a) Bug D: pending ts에 없는 JudgeItem 필터링
    #   AI가 THREAD CONVERSATIONS 섹션 메시지에 대해서도 판단을 생성할 수 있음
    if judge_result.items:
        judge_pending_ts = {m.get("ts", "") for m in judge_pending if m.get("ts")}
        filtered_items = [item for item in judge_result.items if item.ts in judge_pending_ts]
        if len(filtered_items) < len(judge_result.items):
            removed = len(judge_result.items) - len(filtered_items)
            logger.info(
                f"non-pending JudgeItem {removed}건 필터링 ({channel_id}): "
                f"judge_pending_ts={len(judge_pending_ts)}건"
            )
        judge_result.items = filtered_items

    # d-0b) linked_message_ts 환각 검증
    _validate_linked_messages(judge_result, judged_messages, judge_pending, judge_thread_buffers)

    # d-0c) 가중치 적용: related_to_me, addressed_to_me 강제 반응
    _apply_importance_modifiers(judge_result, judge_pending)

    # d) 리액션 처리
    # e) 스냅샷 메시지는 예외 발생 여부와 무관하게 반드시 judged로 이동해야 함
    #    (이동하지 않으면 pending에 영원히 남아 중복 응답 발생 — Bug A)
    try:
        if judge_result.items:
            # 복수 판단 경로
            await _handle_multi_judge(
                judge_result=judge_result,
                store=store,
                channel_id=channel_id,

                cooldown=cooldown,
                pending_messages=judge_pending,
                current_digest=current_digest,
                debug_channel=debug_channel,
                intervention_threshold=intervention_threshold,
                react_probability=react_probability,
                llm_call=llm_call,
                bot_user_id=bot_user_id,
                session_manager=kwargs.get("session_manager"),
                thread_buffers=judge_thread_buffers,
                mention_handled_ts=mention_handled_ts,
                dispatch=kwargs.get("dispatch"),
                recent_messages_count=recent_messages_count,
                intervene_model=intervene_model,
                folder_id=folder_id,
                agent_id=agent_id,
                pas_default_model=pas_default_model or DEFAULT_CHANNEL_PAS_MODEL,
                pas_fallback_models=pas_fallback_models or DEFAULT_CHANNEL_PAS_FALLBACK_MODELS,
                pas_folder_id=pas_folder_id if pas_folder_id is not None else folder_id,
                buffer_threshold=buffer_threshold,
                compressor=compressor,
                remiel_config=remiel_config,
                prep_services=prep_services,
            )
        else:
            # 하위호환: 단일 판단 경로
            await _handle_single_judge(
                judge_result=judge_result,
                store=store,
                channel_id=channel_id,

                cooldown=cooldown,
                pending_messages=judge_pending,
                current_digest=current_digest,
                debug_channel=debug_channel,
                intervention_threshold=intervention_threshold,
                react_probability=react_probability,
                llm_call=llm_call,
                bot_user_id=bot_user_id,
                session_manager=kwargs.get("session_manager"),
                thread_buffers=judge_thread_buffers,
                mention_handled_ts=mention_handled_ts,
                dispatch=kwargs.get("dispatch"),
                recent_messages_count=recent_messages_count,
                intervene_model=intervene_model,
                folder_id=folder_id,
                agent_id=agent_id,
                pas_default_model=pas_default_model or DEFAULT_CHANNEL_PAS_MODEL,
                pas_fallback_models=pas_fallback_models or DEFAULT_CHANNEL_PAS_FALLBACK_MODELS,
                pas_folder_id=pas_folder_id if pas_folder_id is not None else folder_id,
                buffer_threshold=buffer_threshold,
                compressor=compressor,
                remiel_config=remiel_config,
                prep_services=prep_services,
            )
    finally:
        # 스냅샷에 포함된 메시지만 judged로 이동 (파이프라인 중 새로 도착한 메시지는 pending에 잔류)
        store.move_snapshot_to_judged(
            channel_id, snapshot_ts, snapshot_thread_ts, snapshot_thread_msg_ts
        )


async def _handle_multi_judge(
    judge_result: JudgeResult,
    store: ChannelStore,
    channel_id: str,
    cooldown: InterventionHistory,
    pending_messages: list[dict],
    current_digest: str | None,
    debug_channel: str,
    intervention_threshold: float,
    llm_call: Optional[Callable],
    bot_user_id: str | None = None,
    react_probability: float = 1.0,
    session_manager=None,
    thread_buffers: dict[str, list[dict]] | None = None,
    mention_handled_ts: set[str] | None = None,
    recent_messages_count: int = 5,
    intervene_model: str | None = None,
    folder_id: str | None = None,
    agent_id: str | None = None,
    remiel_config: RemielContextConfig | None = None,
    prep_services: InterventionPrepServices | None = None,
    **kwargs,
) -> None:
    """복수 JudgeItem 처리: 이모지 일괄 + 개입 확률 판단"""
    actions = _parse_judge_actions(judge_result)

    # 멘션 스레드 대상 액션 필터링 (소화는 정상 처리, 리액션/개입만 제외)
    actions = _filter_mention_thread_actions(actions, mention_handled_ts or set())

    react_actions = [a for a in actions if a.type == "react"]
    message_actions = [a for a in actions if a.type == "message"]

    # 봇이 이미 리액션한 메시지 필터링
    react_actions = _filter_already_reacted(
        react_actions, pending_messages, bot_user_id,
    )

    # 이모지 리액션 확률 필터링
    if react_probability < 1.0:
        react_actions = [a for a in react_actions if random.random() < react_probability]

    # 이모지 리액션 일괄 실행
    if react_actions:
        await execute_interventions(channel_id, react_actions)

    # 개입 처리 (burst/cooldown 확률 기반)
    executed_messages: list[InterventionAction] = []
    if message_actions:
        # 개입에 사용할 importance는 가장 높은 item의 것
        max_item = _get_max_importance_item(judge_result)
        importance_for_prob = max_item.importance if max_item else 5

        prob = cooldown.burst_probability(channel_id, importance_for_prob)

        # burst 진행 중 여부에 따라 판정 분기
        mins_since = cooldown.minutes_since_last(channel_id)
        BURST_GAP = 5  # burst_intervention_probability와 동일한 상수
        if mins_since <= BURST_GAP:
            # burst 내에서는 prob 자체가 판정 기준
            final_score = prob
            passed = final_score >= 0.35
            # 디버그 호환: burst 내에서는 time/freq를 burst 정보로 대체
            time_factor = prob
            freq_factor = 1.0
        else:
            # cooldown 구간에서는 importance 가중
            final_score = (importance_for_prob / 10.0) * prob
            passed = final_score >= intervention_threshold
            time_factor = prob
            freq_factor = importance_for_prob / 10.0

        await send_intervention_probability_debug_log(
            debug_channel=debug_channel,
            source_channel=channel_id,
            importance=importance_for_prob,
            time_factor=time_factor,
            freq_factor=freq_factor,
            probability=prob,
            final_score=final_score,
            threshold=0.35 if mins_since <= BURST_GAP else intervention_threshold,
            passed=passed,
        )

        if passed:
            # 개입은 1건만 (가장 중요한 것)
            # message_actions는 이미 멘션 필터를 거쳤으므로,
            # 필터된 target 집합에 속하는 항목만 선택합니다.
            filtered_targets = {a.target for a in message_actions}
            intervene_item = None
            for item in judge_result.items:
                if item.reaction_type == "intervene" and item.reaction_target in filtered_targets:
                    if intervene_item is None or item.importance > intervene_item.importance:
                        intervene_item = item

            if intervene_item:
                action = _parse_judge_item_action(intervene_item)
                if action:
                    if llm_call:
                        await _execute_intervene(
                            store=store,
                            channel_id=channel_id,

                            action=action,
                            pending_messages=pending_messages,
                            observer_reason=intervene_item.reaction_content,
                            llm_call=llm_call,
                            bot_user_id=bot_user_id,

                            thread_buffers=thread_buffers,
                            dispatch=kwargs.get("dispatch"),
                            session_manager=session_manager,
                            recent_messages_count=recent_messages_count,
                            intervene_model=intervene_model,
                            folder_id=folder_id,
                            agent_id=agent_id,
                            pas_default_model=kwargs.get("pas_default_model"),
                            pas_fallback_models=kwargs.get("pas_fallback_models"),
                            pas_folder_id=kwargs.get("pas_folder_id", folder_id),
                            buffer_threshold=kwargs.get("buffer_threshold", 150),
                            compressor=kwargs.get("compressor"),
                            remiel_config=remiel_config,
                            prep_services=prep_services,
                        )
                    else:
                        await execute_interventions(channel_id, [action])
                    cooldown.record(channel_id)
                    executed_messages = [action]

    # 디버그 로그: 메시지별 독립 블록
    await send_multi_judge_debug_log(
        debug_channel=debug_channel,
        source_channel=channel_id,
        items=judge_result.items,
        react_actions=react_actions,
        message_actions_executed=executed_messages,
        pending_count=len(pending_messages),
        pending_messages=pending_messages,

    )


async def _handle_single_judge(
    judge_result: JudgeResult,
    store: ChannelStore,
    channel_id: str,
    cooldown: InterventionHistory,
    pending_messages: list[dict],
    current_digest: str | None,
    debug_channel: str,
    intervention_threshold: float,
    llm_call: Optional[Callable],
    bot_user_id: str | None = None,
    react_probability: float = 1.0,
    session_manager=None,
    thread_buffers: dict[str, list[dict]] | None = None,
    mention_handled_ts: set[str] | None = None,
    recent_messages_count: int = 5,
    intervene_model: str | None = None,
    folder_id: str | None = None,
    agent_id: str | None = None,
    remiel_config: RemielContextConfig | None = None,
    prep_services: InterventionPrepServices | None = None,
    **kwargs,
) -> None:
    """하위호환: 단일 JudgeResult 처리"""
    logger.info(
        f"리액션 판단 완료 ({channel_id}): "
        f"중요도 {judge_result.importance}, "
        f"반응 {judge_result.reaction_type}"
    )

    observer_result = _judge_result_to_observer_result(
        judge_result, digest=current_digest or ""
    )

    reaction_detail = None
    if judge_result.reaction_type != "none" and judge_result.reaction_target:
        reaction_detail = (
            f"{judge_result.reaction_type}: "
            f"`{judge_result.reaction_target}` → "
            f"{judge_result.reaction_content or '(없음)'}"
        )

    actions = _parse_judge_actions(judge_result)
    # 멘션 스레드 대상 액션 필터링 (소화는 정상 처리, 리액션/개입만 제외)
    actions = _filter_mention_thread_actions(actions, mention_handled_ts or set())
    if not actions:
        await send_debug_log(
            
            debug_channel=debug_channel,
            source_channel=channel_id,
            observer_result=observer_result,
            actions=[],
            actions_filtered=[],
            reasoning=judge_result.reasoning,
            emotion=judge_result.emotion,
            pending_count=len(pending_messages),
            reaction_detail=reaction_detail,
        )
    else:
        react_actions = [a for a in actions if a.type == "react"]
        message_actions = [a for a in actions if a.type == "message"]

        # 봇이 이미 리액션한 메시지 필터링
        react_actions = _filter_already_reacted(
            react_actions, pending_messages, bot_user_id,
        )

        # 이모지 리액션 확률 필터링
        if react_probability < 1.0:
            react_actions = [a for a in react_actions if random.random() < react_probability]

        if react_actions:
            await execute_interventions(channel_id, react_actions)

        executed_messages: list[InterventionAction] = []
        if message_actions:
            prob = cooldown.burst_probability(channel_id, judge_result.importance)

            # burst 진행 중 여부에 따라 판정 분기
            mins_since = cooldown.minutes_since_last(channel_id)
            BURST_GAP = 5  # burst_intervention_probability와 동일한 상수
            if mins_since <= BURST_GAP:
                final_score = prob
                passed = final_score >= 0.35
                time_factor = prob
                freq_factor = 1.0
            else:
                final_score = (judge_result.importance / 10.0) * prob
                passed = final_score >= intervention_threshold
                time_factor = prob
                freq_factor = judge_result.importance / 10.0

            await send_intervention_probability_debug_log(
                debug_channel=debug_channel,
                source_channel=channel_id,
                importance=judge_result.importance,
                time_factor=time_factor,
                freq_factor=freq_factor,
                probability=prob,
                final_score=final_score,
                threshold=0.35 if mins_since <= BURST_GAP else intervention_threshold,
                passed=passed,
            )

            if passed:
                if llm_call:
                    for action in message_actions:
                        await _execute_intervene(
                            store=store,
                            channel_id=channel_id,

                            action=action,
                            pending_messages=pending_messages,
                            observer_reason=judge_result.reaction_content,
                            llm_call=llm_call,
                            bot_user_id=bot_user_id,

                            thread_buffers=thread_buffers,
                            dispatch=kwargs.get("dispatch"),
                            session_manager=session_manager,
                            recent_messages_count=recent_messages_count,
                            intervene_model=intervene_model,
                            folder_id=folder_id,
                            agent_id=agent_id,
                            pas_default_model=kwargs.get("pas_default_model"),
                            pas_fallback_models=kwargs.get("pas_fallback_models"),
                            pas_folder_id=kwargs.get("pas_folder_id", folder_id),
                            buffer_threshold=kwargs.get("buffer_threshold", 150),
                            compressor=kwargs.get("compressor"),
                            remiel_config=remiel_config,
                            prep_services=prep_services,
                        )
                else:
                    await execute_interventions(channel_id, message_actions)
                cooldown.record(channel_id)
                executed_messages = message_actions

        filtered = react_actions + executed_messages

        await send_debug_log(
            
            debug_channel=debug_channel,
            source_channel=channel_id,
            observer_result=observer_result,
            actions=actions,
            actions_filtered=filtered,
            reasoning=judge_result.reasoning,
            emotion=judge_result.emotion,
            pending_count=len(pending_messages),
            reaction_detail=reaction_detail,
        )


def _dedupe_utterances(raw_matches: list[str]) -> list[str]:
    """블록별 ``<utterance>`` 매치 list를 strip 동일성으로 dedupe합니다.

    backend가 thinking 블록 / text_start~text_end 트리오 / complete 블록 각각에서
    추출하여 list로 누적한 매치 결과를 받아, 본 함수가 *호출자 측 정책*을 적용한다:

    - ``strip()`` 후 빈 문자열은 제외 (빈 ``<utterance></utterance>`` 케이스).
    - 같은 본문이 여러 블록에서 매치돼도(예: text 블록과 complete 블록 둘 다에서)
      등장 순서를 보존하며 *첫 등장만* 보존한다.

    backend는 dedupe·필터 책임을 지지 않는다 — 정책이 호출자마다 다를 수 있다 (책임 분리).
    """
    seen: set[str] = set()
    unique: list[str] = []
    for m in raw_matches:
        s = (m or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        unique.append(s)
    return unique


def _make_resolver() -> DisplayNameResolver | None:
    """현재 Slack 백엔드에서 WebClient를 추출해 DisplayNameResolver를 생성합니다.

    백엔드가 없거나 _client 속성이 없으면 None을 반환합니다.
    반환된 None은 호출자에서 안전하게 처리됩니다(resolver=None → user_id 그대로 사용).
    """
    backend = slack.get_backend()
    raw_client = getattr(backend, "_client", None)
    return DisplayNameResolver(raw_client) if raw_client else None


async def _no_intervene_memory() -> None:
    return None


async def _fetch_latest_thread_replies(
    channel_id: str, thread_root_ts: str, recent_messages_count: int,
) -> list[Any]:
    """Read the thread's pages and retain its root and recent replies."""
    get_replies = slack.get_thread_replies
    backend = slack.get_backend() if get_replies is _sdk_get_thread_replies else None
    client = getattr(backend, "_client", None)
    use_client = client is not None and hasattr(client, "conversations_replies")
    try:
        supports_cursor = "cursor" in inspect.signature(get_replies).parameters
    except (TypeError, ValueError):
        supports_cursor = False

    if not use_client and not supports_cursor:
        return await get_replies(
            channel_id, thread_root_ts, limit=max(recent_messages_count, 200)
        )

    messages: list[Any] = []
    cursor = None
    for _ in range(20):
        if use_client:
            page = await asyncio.to_thread(
                client.conversations_replies,
                channel=channel_id, ts=thread_root_ts, limit=200, cursor=cursor,
            )
        else:
            page = await get_replies(
                channel_id, thread_root_ts,
                limit=max(recent_messages_count, 200), cursor=cursor,
            )
        if isinstance(page, list):
            messages.extend(page)
            break
        if isinstance(page, dict) or use_client:
            messages.extend(page.get("messages", []))
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
        else:
            messages.extend(page.messages)
            cursor = page.next_cursor
        if not cursor:
            break
    else:
        logger.warning("Jev 스레드 답글 조회가 페이지 상한에 도달했습니다 (%s, %s)", channel_id, thread_root_ts)

    def message_ts(message: Any) -> str:
        return str(message.get("ts", "") if isinstance(message, dict) else message.ts)

    root = [message for message in messages if message_ts(message) == thread_root_ts]
    replies = [message for message in messages if message_ts(message) != thread_root_ts]
    return root[:1] + replies[-max(recent_messages_count * 4, 50):]


async def run_message_jev_pipeline(
    *,
    channel_id: str,
    event: dict,
    jev_judge: JevChannelJudge,
    emoji_store: EmojiFrequencyStore,
    cooldown: InterventionHistory,
    store: ChannelStore,
    bot_user_id: str | None = None,
    trigger_words: list[str] | None = None,
    debug_channel: str = "",
    recent_messages_count: int = 15,
    emoji_candidate_limit: int = 30,
    llm_call: Optional[Callable] = None,
    intervene_model: str | None = None,
    folder_id: str | None = None,
    agent_id: str | None = None,
    remiel_config: RemielContextConfig | None = None,
    prep_services: InterventionPrepServices | None = None,
    recent_bot_reactions: dict[str, list[str]] | None = None,
    **kwargs,
) -> JevMessageVerdict | None:
    """Judge and handle one newly received Slack message."""
    subtype = event.get("subtype")
    if subtype in COLLECT_EXCLUDED_SUBTYPES:
        return None
    if (
        event.get("bot_id")
        or not event.get("user")
        or (bot_user_id and event.get("user") == bot_user_id)
    ):
        return None

    text = (event.get("text") or "").strip()
    ts = str(event.get("ts") or "").strip()
    if not text or not ts:
        return None
    thread_ts = event.get("thread_ts")
    thread_root_ts = str(thread_ts or "").strip()
    is_thread_reply = bool(thread_root_ts and thread_root_ts != ts)
    target_message = {
        "ts": ts,
        "user": event["user"],
        "text": text,
        "thread_ts": thread_ts,
    }
    if not is_thread_reply and store is not None:
        store.upsert_pending(channel_id, target_message)

    mention_backend = mention.get_backend()
    if bot_user_id and f"<@{bot_user_id}>" in text and mention_backend is not None:
        mention.mark(thread_ts or ts)
        return None
    if mention_backend is not None:
        if (thread_ts and mention.is_handled(thread_ts)) or mention.is_handled(ts):
            return None

    history_loaded = False
    try:
        history = await slack.get_channel_history(
            channel_id, limit=recent_messages_count
        )
        history_loaded = True
    except Exception as error:
        logger.warning("Jev 최근 메시지 조회 실패 (%s): %s", channel_id, error)
        history = []

    if history_loaded:
        emoji_store.seed_from_messages(
            channel_id, history, bot_user_id=bot_user_id
        )

    thread_history: list[Any] = []
    if is_thread_reply:
        try:
            thread_history = await _fetch_latest_thread_replies(
                channel_id, thread_root_ts, recent_messages_count,
            )
        except Exception as error:
            logger.warning(
                "Jev 스레드 답글 조회 실패 (%s, %s): %s",
                channel_id,
                thread_root_ts,
                error,
            )
            thread_history = []

    channel_reactions = (
        recent_bot_reactions if recent_bot_reactions is not None else {}
    )

    def _to_jev_message_dict(
        message: Any, *, default_thread_ts: str | None = None
    ) -> dict[str, Any]:
        def field(value: Any, name: str, default: Any = None) -> Any:
            return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)

        message_ts = str(field(message, "ts") or "")
        reactions = field(message, "reactions") or []
        msg_reactions = []
        msg_bot_reactions = []
        for reaction in reactions:
            name = reaction if isinstance(reaction, str) else field(reaction, "name", "")
            if not name:
                continue
            normalized = normalize_emoji_name(name)
            msg_reactions.append(normalized)
            if bot_user_id and bot_user_id in (field(reaction, "users") or []):
                msg_bot_reactions.append(normalized)
        for emoji in field(message, "bot_reactions", []) or []:
            normalized = normalize_emoji_name(emoji)
            if normalized and normalized not in msg_bot_reactions:
                msg_bot_reactions.append(normalized)
        for emoji in channel_reactions.get(message_ts, []):
            normalized = normalize_emoji_name(emoji)
            if normalized and normalized not in msg_reactions:
                msg_reactions.append(normalized)
            if normalized and normalized not in msg_bot_reactions:
                msg_bot_reactions.append(normalized)
        return {
            "ts": message_ts,
            "user": field(message, "user"),
            "text": field(message, "text") or "",
            "thread_ts": field(message, "thread_ts") or default_thread_ts,
            "reactions": msg_reactions,
            "bot_reactions": msg_bot_reactions,
        }

    def _ts_sort_key(item: dict[str, Any]) -> tuple[int, float, str]:
        raw_ts = str(item.get("ts") or "")
        try:
            return (0, float(raw_ts), raw_ts)
        except ValueError:
            return (1, 0.0, raw_ts)

    merged_by_ts: dict[str, dict[str, Any]] = {}
    for message in reversed(history):
        item = _to_jev_message_dict(message)
        if item["ts"] and item["ts"] != ts:
            merged_by_ts[item["ts"]] = item

    thread_replies_by_ts: dict[str, dict[str, Any]] = {}
    for message in thread_history:
        item = _to_jev_message_dict(message, default_thread_ts=thread_root_ts)
        if not item["ts"]:
            continue
        if item["ts"] != ts:
            merged_by_ts[item["ts"]] = item
        if item["ts"] != thread_root_ts:
            thread_replies_by_ts[item["ts"]] = item

    if is_thread_reply:
        if store is not None:
            for message in store.load_thread_buffer(channel_id, thread_root_ts):
                item = _to_jev_message_dict(message, default_thread_ts=thread_root_ts)
                if item["ts"] and item["ts"] != thread_root_ts:
                    thread_replies_by_ts.setdefault(item["ts"], item)
                    if item["ts"] != ts:
                        merged_by_ts.setdefault(item["ts"], item)
            store.upsert_thread_message(channel_id, thread_root_ts, target_message)
        thread_replies_by_ts[ts] = dict(target_message)
        root_key = _ts_sort_key({"ts": thread_root_ts})
        current_key = _ts_sort_key({"ts": ts})
        for reaction_ts in channel_reactions:
            reaction_key = _ts_sort_key({"ts": reaction_ts})
            if root_key < reaction_key < current_key and reaction_ts not in merged_by_ts:
                merged_by_ts[reaction_ts] = _to_jev_message_dict(
                    {"ts": reaction_ts}, default_thread_ts=thread_root_ts,
                )

    thread_reply_dicts = sorted(thread_replies_by_ts.values(), key=_ts_sort_key)
    recent_messages = sorted(merged_by_ts.values(), key=_ts_sort_key)[
        -recent_messages_count:
    ]

    text_lower = text.lower()
    matched_triggers = [
        word for word in (trigger_words or []) if word.lower() in text_lower
    ]
    candidates = emoji_store.get_top_candidates(limit=emoji_candidate_limit)
    verdict = await jev_judge.judge_message(
        channel_id=channel_id,
        target_message=target_message,
        recent_messages=recent_messages,
        emoji_candidates=candidates,
        trigger_words_matched=matched_triggers,
        bot_user_id=bot_user_id,
    )
    if verdict is None:
        return None

    if verdict.should_react and verdict.selected_emoji:
        already_bot_emojis = channel_reactions.get(ts, [])
        if verdict.selected_emoji not in already_bot_emojis:
            try:
                reaction_result = await slack.add_reaction(
                    channel=channel_id,
                    ts=ts,
                    emoji=verdict.selected_emoji,
                )
            except Exception as error:
                logger.warning(
                    "Jev 이모지 리액션 실패 (%s, %s): %s",
                    channel_id,
                    ts,
                    error,
                )
            else:
                if (
                    recent_bot_reactions is not None
                    and getattr(reaction_result, "ok", True) is not False
                ):
                    if (
                        ts not in recent_bot_reactions
                        and len(recent_bot_reactions) >= 100
                    ):
                        oldest_ts = next(iter(recent_bot_reactions))
                        recent_bot_reactions.pop(oldest_ts)
                    bot_emojis = recent_bot_reactions.setdefault(ts, [])
                    if verdict.selected_emoji not in bot_emojis:
                        bot_emojis.append(verdict.selected_emoji)

    if verdict.should_intervene:
        force_intervene = bool(matched_triggers)
        importance_equiv = max(
            1, min(10, round(verdict.intervene_score * 10))
        )
        prob = cooldown.burst_probability(channel_id, importance_equiv)
        mins_since = cooldown.minutes_since_last(channel_id)
        BURST_GAP = 5
        if force_intervene:
            passed = True
        elif mins_since <= BURST_GAP:
            passed = prob >= 0.35
        else:
            passed = (
                verdict.intervene_score * prob
            ) >= kwargs.get("intervention_threshold", 0.18)

        if passed:
            reply_target = thread_root_ts or ts
            action = InterventionAction(
                type="message",
                target=reply_target,
                content=f"jev_intervene={verdict.intervene_score:.2f}",
            )
            context_messages = [*recent_messages, target_message]
            forwarded_kwargs = dict(kwargs)
            forwarded_kwargs.update({
                "target_message": target_message,
                "target_ts": ts,
                "current_thread_ts": thread_root_ts if is_thread_reply else None,
                "channel_name": event.get("channel_name"),
                "pas_default_model": kwargs.get("pas_default_model"),
                "pas_fallback_models": kwargs.get("pas_fallback_models"),
                "pas_folder_id": kwargs.get("pas_folder_id", folder_id),
                "buffer_threshold": kwargs.get("buffer_threshold", 150),
                "compressor": kwargs.get("compressor"),
            })
            if is_thread_reply:
                existing_reply_ts = {
                    str(reply.get("ts") or "") for reply in thread_reply_dicts
                }
                if ts not in existing_reply_ts:
                    thread_reply_dicts.append(dict(target_message))
                merged_thread_buffers = dict(
                    forwarded_kwargs.get("thread_buffers") or {}
                )
                merged_thread_buffers[thread_root_ts] = thread_reply_dicts
                forwarded_kwargs["thread_buffers"] = merged_thread_buffers
            if llm_call:
                await _execute_intervene(
                    store=store,
                    channel_id=channel_id,
                    action=action,
                    pending_messages=context_messages,
                    observer_reason=action.content,
                    llm_call=llm_call,
                    bot_user_id=bot_user_id,
                    recent_messages_count=recent_messages_count,
                    intervene_model=intervene_model,
                    folder_id=folder_id,
                    agent_id=agent_id,
                    remiel_config=remiel_config,
                    prep_services=prep_services,
                    **forwarded_kwargs,
                )
            else:
                await execute_interventions(channel_id, [action])
            cooldown.record(channel_id)

    return verdict


async def _execute_intervene(
    store: ChannelStore,
    channel_id: str,
    action: InterventionAction,
    pending_messages: list[dict],
    observer_reason: str | None = None,
    llm_call: Optional[Callable] = None,
    bot_user_id: str | None = None,
    thread_buffers: dict[str, list[dict]] | None = None,
    recent_messages_count: int = 5,
    intervene_model: str | None = None,
    folder_id: str | None = None,
    agent_id: str | None = None,
    remiel_config: RemielContextConfig | None = None,
    prep_services: InterventionPrepServices | None = None,
    **kwargs,
) -> None:
    """채널별 실행 락 안에서 개입을 수행합니다."""
    if inspect.getattr_static(store, "pas_run_lock", None) is not None:
        run_lock = store.pas_run_lock(channel_id)
        await asyncio.to_thread(run_lock.acquire)
        try:
            await _execute_intervene_locked(
                store=store,
                channel_id=channel_id,
                action=action,
                pending_messages=pending_messages,
                observer_reason=observer_reason,
                llm_call=llm_call,
                bot_user_id=bot_user_id,
                thread_buffers=thread_buffers,
                recent_messages_count=recent_messages_count,
                intervene_model=intervene_model,
                folder_id=folder_id,
                agent_id=agent_id,
                remiel_config=remiel_config,
                prep_services=prep_services,
                **kwargs,
            )
        finally:
            run_lock.release()
    else:
        await _execute_intervene_locked(
            store=store,
            channel_id=channel_id,
            action=action,
            pending_messages=pending_messages,
            observer_reason=observer_reason,
            llm_call=llm_call,
            bot_user_id=bot_user_id,
            thread_buffers=thread_buffers,
            recent_messages_count=recent_messages_count,
            intervene_model=intervene_model,
            folder_id=folder_id,
            agent_id=agent_id,
            remiel_config=remiel_config,
            prep_services=prep_services,
            **kwargs,
        )


async def _execute_intervene_locked(
    store: ChannelStore,
    channel_id: str,
    action: InterventionAction,
    pending_messages: list[dict],
    observer_reason: str | None = None,
    llm_call: Optional[Callable] = None,
    bot_user_id: str | None = None,
    thread_buffers: dict[str, list[dict]] | None = None,
    recent_messages_count: int = 5,
    intervene_model: str | None = None,
    folder_id: str | None = None,
    agent_id: str | None = None,
    remiel_config: RemielContextConfig | None = None,
    prep_services: InterventionPrepServices | None = None,
    **kwargs,
) -> None:
    """서소영의 개입 응답을 생성하고 발송합니다."""
    default_state = ChannelStore._default_pas_state(channel_id)
    if inspect.getattr_static(store, "load_pas_state", None) is not None:
        pas_state = store.load_pas_state(channel_id)
    else:
        pas_state = default_state

    ensure_channel_pas = (
        getattr(soulstream, "ensure_channel_pas", None)
        if inspect.getattr_static(soulstream, "ensure_channel_pas", None) is not None
        else None
    )
    pas_session_id = pas_state.get("session_id")
    pas_default_model = kwargs.get("pas_default_model") or DEFAULT_CHANNEL_PAS_MODEL
    pas_fallback_models = kwargs.get("pas_fallback_models") or DEFAULT_CHANNEL_PAS_FALLBACK_MODELS
    pas_folder_id = kwargs.get("pas_folder_id", folder_id)
    if ensure_channel_pas is not None:
        pas_session_id = await _call_with_supported_kwargs(
            ensure_channel_pas,
            channel_id=channel_id,
            agent_id="seosoyoung-intervene",
            folder_id=pas_folder_id,
            default_model=pas_default_model,
            fallback_models=pas_fallback_models,
            session_id=pas_state.get("session_id"),
            display_name=f"💬 채널개입 PAS: #{kwargs.get('channel_name') or channel_id}",
            caller_info=build_bot_caller_info(
                source="channel_observer",
                display_name="채널 관찰자",
                agent_node=get_host_preferred_node(),
            ),
        )
        if isinstance(pas_session_id, dict):
            pas_session_id = pas_session_id.get("session_id")
        if pas_session_id != pas_state.get("session_id"):
            pas_state["session_id"] = pas_session_id
            if inspect.getattr_static(store, "save_pas_state", None) is not None:
                pas_state = store.save_pas_state(channel_id, pas_state)

    # 0. 트리거 메시지에 :ssy-thinking: 이모지 추가 (응답 생성 중 피드백)
    reaction_ts = action.target if action.target and action.target != "channel" else None
    if reaction_ts:
        try:
            await slack.add_reaction(
                channel=channel_id, ts=reaction_ts, emoji=_intervention_thinking_emoji(),
            )
        except Exception as e:
            logger.debug(f"thinking 이모지 추가 실패: {e}")

    target_message = kwargs.get("target_message")
    target_ts = kwargs.get("target_ts") or (
        str(target_message.get("ts")) if target_message else
        (action.target if action.target and action.target != "channel" else None)
    )
    delta = await _build_channel_pas_delta(
        store=store,
        channel_id=channel_id,
        pas_state=pas_state,
        target_ts=target_ts,
        target_message=target_message,
        current_thread_ts=kwargs.get("current_thread_ts"),
        pending_messages=pending_messages,
        thread_buffers=thread_buffers,
        bot_user_id=bot_user_id,
        recent_messages_count=recent_messages_count,
        judgment_reason=observer_reason,
        buffer_threshold=int(kwargs.get("buffer_threshold", 150)),
        compressor=kwargs.get("compressor"),
    )
    if delta.get("empty"):
        logger.info("PAS 개입 스킵 — 미전달 Delta 없음 (%s, %s)", channel_id, target_ts)
        await _remove_thinking_reaction(channel_id, reaction_ts)
        return

    thread_context = delta["text"]
    message_timestamps = [
        str(message.get("ts")) for message in delta["messages"] if message.get("ts")
    ]
    remiel_timestamps = [
        str(message.get("ts")) for message in delta["roots"] if message.get("ts")
    ]
    trigger_message = next(
        (message for message in delta["messages"] if str(message.get("ts")) == delta["target_ts"]),
        target_message,
    )
    recent_messages = delta["messages"][-recent_messages_count:]
    run_thread_ts = (
        action.target
        if action.target and action.target != "channel"
        else (delta["target_ts"] or (trigger_message or {}).get("ts"))
    )
    prompt = "(채널 개입 트리거)"

    context_items = [
        {
            "key": "thread_context",
            "label": "스레드 맥락",
            "content": thread_context,
        },
        # NOTE: "관찰자 판단 근거" 섹션을 비활성화 — 자연스러운 대화 개입에 방해가 된다고 판단
        # {
        #     "key": "observer_reason",
        #     "label": "관찰자 판단 근거",
        #     "content": observer_reason or "",
        # },
    ]

    services = prep_services or InterventionPrepServices()
    memory = getattr(services, "memory", None)
    slug = datetime.now(KST).strftime("%Y%m%d-%H%M%S")
    prep_task = asyncio.create_task(
        build_intervention_prep(
            llm_call=llm_call,
            channel_id=channel_id,
            thread_context=thread_context,
            output_dir=services.output_dir,
            slug=slug,
            message_timestamps=list(dict.fromkeys(message_timestamps)),
        )
    )

    participant_ids: list[str] = []

    def _add_participant(user_id: str | None) -> None:
        if (
            user_id
            and user_id != bot_user_id
            and user_id not in participant_ids
            and len(participant_ids) < 10
        ):
            participant_ids.append(user_id)

    for message in delta["messages"]:
        _add_participant(message.get("user"))

    preload_started = time.monotonic()
    (
        channel_memory,
        member_facts,
        remiel_context_item,
    ) = await asyncio.gather(
        (
            memory.load_channel_memory(channel_id)
            if memory is not None
            else _no_intervene_memory()
        ),
        (
            memory.load_member_facts(channel_id, participant_ids)
            if memory is not None and participant_ids
            else _no_intervene_memory()
        ),
        build_remiel_context_item(
            remiel_config,
            channel_id=channel_id,
            timestamps=remiel_timestamps,
        ),
    )
    logger.info(
        "intervene preload (%s): channel=%s, members=%d/%d, %dms",
        channel_id,
        "hit" if channel_memory else "none",
        len((member_facts or {}).get("members") or []),
        len(participant_ids),
        int((time.monotonic() - preload_started) * 1000),
    )

    if channel_memory:
        context_items.append(render_channel_memory_item(channel_memory))
    member_facts_item = render_member_facts_item(member_facts)
    if member_facts_item:
        context_items.append(member_facts_item)
    if remiel_context_item:
        context_items.append(remiel_context_item)

    # 4. 응답 생성 (Soulstream 경유 Claude Code)
    run_result = None
    run_kwargs = {
        "prompt": prompt,
        "channel": channel_id,
        "thread_ts": run_thread_ts,
        "text_only": True,
        "context": context_items,
        "model": intervene_model,
        "folder_id": pas_folder_id,
        "agent_id": agent_id,
        "caller_info": build_bot_caller_info(
            source="channel_observer",
            display_name="채널 관찰자",
            agent_node=get_host_preferred_node(),
        ),
    }
    if pas_session_id:
        run_kwargs.update({
            "session_id": pas_session_id,
            "use_persistent_session": True,
            "delivery_id": delta["batch_id"],
            "agent_id": "seosoyoung-intervene",
        })
    try:
        if pas_session_id:
            run_result = await _call_with_supported_kwargs(soulstream.run, **run_kwargs)
            await prep_task
        else:
            run_result = await run_and_name_session(
                _call_with_supported_kwargs(soulstream.run, **run_kwargs),
                thread_ts=run_thread_ts,
                prep_task=prep_task,
                get_session_id=soulstream.get_session_id,
                set_session_name=services.set_session_name,
            )
    except Exception as e:
        logger.error(f"intervene 응답 생성 실패 ({channel_id}): {e}")
        await _remove_thinking_reaction(channel_id, reaction_ts)

    response_text: str | None = None
    if run_result is not None:
        if not run_result.ok:
            logger.error(f"intervene soulstream 실패 ({channel_id}): {run_result.error}")
            await _remove_thinking_reaction(channel_id, reaction_ts)
        else:
            old_read_ts = str(pas_state.get("read_ts") or "")
            root_ts = delta["snapshot_ts"]
            if root_ts:
                newest_root_ts = max(root_ts, key=_ts_sort_key)
                if not old_read_ts or _ts_sort_key(newest_root_ts) > _ts_sort_key(old_read_ts):
                    pas_state["read_ts"] = newest_root_ts
            thread_cursors = dict(pas_state.get("read_thread_ts_map") or {})
            for thread_ts, message_ts in delta["snapshot_thread_msg_ts"].items():
                if message_ts:
                    newest_reply_ts = max(message_ts, key=_ts_sort_key)
                    previous = str(thread_cursors.get(thread_ts) or "")
                    if not previous or _ts_sort_key(newest_reply_ts) > _ts_sort_key(previous):
                        thread_cursors[thread_ts] = newest_reply_ts
            pas_state["read_thread_ts_map"] = thread_cursors
            pas_state["initialized_context"] = True
            if inspect.getattr_static(store, "save_pas_state", None) is not None:
                pas_state = store.save_pas_state(channel_id, pas_state)
            store.move_snapshot_to_judged(
                channel_id,
                delta["snapshot_ts"],
                delta["snapshot_thread_ts"],
                delta["snapshot_thread_msg_ts"],
            )

            # 게시 정책 (사용자 결정 2026-05-18, 사이클 260518.01):
            #   - backend가 thinking / text_start~text_end / complete 각 *블록의 텍스트만*에서
            #     ``<utterance>(.*?)</utterance>`` 매치를 추출하여 ``RunResult.utterances`` list로
            #     반환한다 (책임: 블록 단위 추출).
            #   - 본 호출자는 그 list에 strip 동일성 dedupe를 적용한 뒤(중복 차단),
            #     남은 본문들을 줄바꿈으로 합쳐 채널에 게시한다.
            #   - 매치 list가 비어 있거나 strip 후 모두 공백이면 게시하지 않는다 —
            #     sub-agent 작업 보고나 분석/메모가 채널에 누출되지 않도록 차단.
            #
            # *직전 사이클*(260513.01)이 사용한 ``transcript`` 누적 검색 정책은 폐기.
            # 누적 transcript에 thinking 묶음의 우발 ``<utterance>`` 토큰이 text 묶음의
            # 정상 ``</utterance>``와 인접하여 한 덩이 매치로 분석 텍스트 전체를 흘리는
            # 사고(채널 C08KT1HDU5U, ts 1779069595.658659, 2026-05-18)가 본 회귀의 계기.
            raw_matches = list(getattr(run_result, "utterances", None) or [])
            unique = _dedupe_utterances(raw_matches)
            if not unique:
                logger.warning(
                    f"intervene 게시 skip — <utterance> 매치 없음 ({channel_id}): "
                    f"raw_matches={len(raw_matches)}"
                )
                await _remove_thinking_reaction(channel_id, reaction_ts)
            elif pas_state.get("last_posted_batch_id") != delta["batch_id"]:
                response_text = "\n".join(unique)

    # 5. 슬랙 발송 + 봇 응답 ts를 judged에 기록
    if response_text is not None:
        try:
            if action.target == "channel":
                send_result = await slack.send_message(
                    channel=channel_id,
                    text=response_text.strip(),
                )
            else:
                send_result = await slack.send_message(
                    channel=channel_id,
                    text=response_text.strip(),
                    thread_ts=action.target,
                )

            # 봇 응답을 judged에 기록하여 후속 judge에서 맥락으로 사용
            resp_ts = send_result.ts if send_result.ok else None

            if resp_ts:
                pas_state["last_posted_ts"] = resp_ts
                pas_state["last_posted_trigger_ts"] = delta["target_ts"]
                pas_state["last_posted_batch_id"] = delta["batch_id"]
                if inspect.getattr_static(store, "save_pas_state", None) is not None:
                    pas_state = store.save_pas_state(channel_id, pas_state)
                bot_msg = {
                    "ts": resp_ts,
                    "user": bot_user_id or "bot",
                    "text": response_text.strip(),
                }
                store.append_judged(channel_id, [bot_msg])
                logger.info(f"봇 응답 judged 기록 ({channel_id}): ts={resp_ts}")

                # 스레드 대상 개입이면 세션 생성 (후속 멘션 대화 대비)
                # dispatch 콜백이 전달된 경우 훅으로 요청, 아니면 session_manager 직접 호출
                if action.target != "channel":
                    dispatch = kwargs.get("dispatch")
                    session_manager = kwargs.get("session_manager")
                    if dispatch:
                        try:
                            from seosoyoung.plugin_sdk import HookContext
                            await dispatch(
                                "on_soulstream_session_request",
                                HookContext(
                                    hook_name="on_soulstream_session_request",
                                    args={
                                        "thread_ts": resp_ts,
                                        "channel_id": channel_id,
                                        "source_type": "hybrid",
                                    },
                                ),
                            )
                            logger.info(f"세션 생성 훅 디스패치 ({channel_id}): ts={resp_ts}")
                        except Exception as e:
                            logger.error(f"세션 생성 훅 디스패치 실패 ({channel_id}): {e}")
                    elif session_manager:
                        try:
                            session_manager.create(
                                thread_ts=resp_ts,
                                channel_id=channel_id,
                                source_type="hybrid",
                            )
                            logger.info(f"개입 세션 생성 ({channel_id}): ts={resp_ts}")
                        except Exception as e:
                            logger.error(f"개입 세션 생성 실패 ({channel_id}): {e}")

                if channel_memory is not None and memory is not None:
                    topics, summary_line, compressed_summary = parse_record_tags(
                        run_result.output
                    )
                    spawn_background(
                        memory.record_utterance(
                            channel_id=channel_id,
                            body=response_text.strip(),
                            slug=slug,
                            topics=topics,
                            summary_line=summary_line,
                            compressed_summary=compressed_summary,
                            channel_memory=channel_memory,
                            output_dir=services.output_dir,
                        )
                    )

            # 발송 성공: :ssy-thinking: → :ssy-happy: 교체
            await _swap_thinking_to_happy(channel_id, reaction_ts)

        except Exception as e:
            logger.error(f"intervene 슬랙 발송 실패 ({channel_id}): {e}")
            await _remove_thinking_reaction(channel_id, reaction_ts)

    if memory is not None:
        try:
            prep = await prep_task
        except Exception:
            prep = None
        if prep is not None:
            spawn_background(memory.archive_threads(channel_id, prep.threads_file))

    await drain_background()


async def _fetch_recent_context(
    channel_id: str,
    count: int = 15,
    bot_user_id: str | None = None,
    resolver: DisplayNameResolver | None = None,
) -> str:
    """슬랙 API로 채널의 최근 메시지를 가져와 포맷합니다.

    API 호출 실패 시 빈 문자열을 반환합니다.
    """
    context, _timestamps = await _fetch_recent_context_bundle(
        channel_id,
        count=count,
        bot_user_id=bot_user_id,
        resolver=resolver,
    )
    return context


async def _fetch_recent_context_bundle(
    channel_id: str,
    count: int = 15,
    bot_user_id: str | None = None,
    resolver: DisplayNameResolver | None = None,
) -> tuple[str, list[str]]:
    """Fetch recent Slack context once and return formatted text plus ts list."""
    try:
        history = await slack.get_channel_history(channel_id, limit=count)
        # Slack API는 최신순 반환 → 시간순으로 뒤집기
        messages = list(reversed(history))  # list[Message] 그대로 전달 (rich 필드 보존)
        context = _format_recent_context(
            messages,
            bot_user_id=bot_user_id,
            resolver=resolver,
            channel_id=channel_id,
        )
        timestamps = [m.ts for m in messages if getattr(m, "ts", "")]
        return context, timestamps
    except Exception as e:
        logger.debug(f"최근 메시지 조회 실패 ({channel_id}): {e}")
        return "", []


def _format_recent_context(
    messages: list[Message],
    bot_user_id: str | None = None,
    resolver: DisplayNameResolver | None = None,
    channel_id: str | None = None,
) -> str:
    """메시지 리스트를 [channel_id:ts] <user>: text 형식의 텍스트로 변환합니다.

    채널 개입 세션의 '스레드 맥락'에 최근 대화 흐름을 제공하기 위해 사용합니다.
    잘라내기(truncation)는 호출자가 담당합니다.

    bot_user_id가 주어지면 해당 봇을 @멘션하는 루트 메시지에 [BOT MENTION THREAD] 태그를
    삽입하여, 개입 세션이 이미 멘션 핸들러가 처리 중인 스레드에 중복 답변하지 않도록 한다.
    이 함수는 get_channel_history(채널 루트만 반환)의 결과를 받으므로
    스레드 중간 메시지는 이미 제외된 상태다. 별도 thread_ts 필터는 불필요하다.

    channel_id가 주어지면 mention 세션과 동일한 [channel_id:ts] <user>: text 포맷을 사용한다.
    channel_id가 없으면 [ts] <user>: text 형식으로 폴백한다.

    reactions, files, blocks 등 rich 필드가 있으면 들여쓰기 보조 줄로 추가합니다.
    """
    if not messages:
        return ""
    mention_pattern = f"<@{bot_user_id}>" if bot_user_id else None
    lines = []
    for m in messages:
        tag = " [BOT MENTION THREAD]" if (mention_pattern and mention_pattern in (m.text or "")) else ""
        user_label = (resolver.resolve(m.user) if resolver and m.user else m.user) or "unknown"
        prefix = f"[{channel_id}:{m.ts}] " if channel_id else f"[{m.ts}] "
        line = f"{prefix}<{user_label}>: {m.text or ''}{tag}"

        # Rich data (없으면 생략)
        rich_parts = []
        if m.reactions:
            reaction_strs = [
                f":{r.name}: ×{r.count} (눌린 사람: {', '.join(r.users)})"
                for r in m.reactions
            ]
            rich_parts.append("리액션: " + " / ".join(reaction_strs))
        if m.files:
            file_strs = [f"{f.title or f.name} ({f.mimetype})" for f in m.files]
            rich_parts.append("첨부: " + ", ".join(file_strs))
        if m.blocks:
            rich_parts.append("[블록 포함]")

        if rich_parts:
            line += "\n  → " + " | ".join(rich_parts)

        lines.append(line)
    return "\n".join(lines)


def _format_thread_buffers(
    thread_buffers: dict | None,
    *,
    thread_timestamps: list[str] | None = None,
    channel_id: str | None = None,
    resolver: DisplayNameResolver | None = None,
    reply_limit: int = 20,
) -> str:
    """thread_buffers를 사람이 읽기 좋은 텍스트로 변환합니다.

    dict[str, list[dict]] 구조를 스레드별 대화 블록으로 포맷합니다. 선택된
    스레드는 최근 ``reply_limit``개 답글만 포함합니다. 빈 buffers 또는 선택된
    답글이 없으면 빈 문자열을 반환합니다.

    예시 출력:
        [1774317667.361259]
          user1: 안녕하세요
          user2: 반갑습니다
    """
    if not thread_buffers:
        return ""
    selected = list(thread_buffers) if thread_timestamps is None else thread_timestamps
    blocks = []
    for tid in selected:
        msgs = thread_buffers.get(tid, [])[-reply_limit:]
        if not msgs:
            continue
        root_prefix = f"[{channel_id}:{tid}]" if channel_id else f"[{tid}]"
        lines = [f"{root_prefix} 스레드"]
        for m in msgs:
            user = m.get("user", "")
            resolved_user = (
                resolver.resolve(user) if resolver and user else user
            ) or "unknown"
            text = m.get("text", "")
            if channel_id:
                reply_prefix = f"[{channel_id}:{m.get('ts', '')}]"
                lines.append(f"  {reply_prefix} <{resolved_user}>: {text}")
            else:
                lines.append(f"  {resolved_user}: {text}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


async def _remove_thinking_reaction(channel_id: str, ts: str | None) -> None:
    """트리거 메시지에서 :ssy-thinking: 이모지를 제거합니다."""
    if not ts:
        return
    try:
        await slack.remove_reaction(channel=channel_id, ts=ts, emoji=_intervention_thinking_emoji())
    except Exception:
        pass


async def _swap_thinking_to_happy(channel_id: str, ts: str | None) -> None:
    """thinking 이모지를 complete 이모지로 교체합니다."""
    if not ts:
        return
    try:
        await slack.remove_reaction(channel=channel_id, ts=ts, emoji=_intervention_thinking_emoji())
    except Exception:
        pass
    try:
        await slack.add_reaction(channel=channel_id, ts=ts, emoji=_intervention_complete_emoji())
    except Exception as e:
        logger.debug(f"complete 이모지 추가 실패: {e}")
