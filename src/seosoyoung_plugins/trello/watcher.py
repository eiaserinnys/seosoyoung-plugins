"""Trello 워처 - To Go 리스트 감시 및 처리

Config 싱글턴 의존성 없이, 생성자에서 설정을 직접 받습니다.

Uses plugin_sdk API instead of direct host dependencies.
"""

import asyncio
import concurrent.futures
import json
import logging
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from seosoyoung.plugin_sdk import slack, soulstream
from seosoyoung_plugins.trello.client import TrelloClient, TrelloCard
from seosoyoung_plugins.trello.prompt_builder import PromptBuilder

logger = logging.getLogger(__name__)


@dataclass
class TrackedCard:
    """추적 중인 카드 정보 (To Go 리스트 감시용)"""
    card_id: str
    card_name: str
    card_url: str
    list_id: str
    list_key: str
    thread_ts: str
    channel_id: str
    detected_at: str
    session_id: Optional[str] = None
    has_execute: bool = False
    dm_thread_ts: Optional[str] = None


@dataclass
class ThreadCardInfo:
    """스레드 ↔ 카드 매핑 정보 (리액션 처리용)"""
    thread_ts: str
    channel_id: str
    card_id: str
    card_name: str
    card_url: str
    session_id: Optional[str] = None
    has_execute: bool = False
    created_at: str = ""


class TrelloWatcher:
    """Trello 리스트 감시자

    모든 설정은 생성자에서 직접 전달받습니다.
    Config 싱글턴에 의존하지 않습니다.

    동시 처리 설계:
    - _state_lock: _tracked, _thread_cards 등 공유 상태 접근 보호
    - _card_executor: 새 카드 처리를 병렬화하는 ThreadPoolExecutor
    - adaptive polling: 새 카드 감지 시 burst 모드로 짧은 간격 재폴링
    """

    def __init__(
        self,
        *,
        trello_client: TrelloClient,
        prompt_builder: PromptBuilder,
        config: dict,
        get_session_lock: Optional[Callable[[str], threading.Lock]] = None,
        data_dir: Optional[Path] = None,
        list_runner_ref: Optional[Callable] = None,
    ):
        """
        Args:
            trello_client: TrelloClient 인스턴스
            prompt_builder: PromptBuilder 인스턴스
            config: 플러그인 설정 dict (YAML에서 로드)
                필수 키: notify_channel, poll_interval, watch_lists,
                         dm_target_user_id, polling_debug, list_ids
                선택 키: burst_interval (기본 3), max_burst_count (기본 5),
                         max_card_workers (기본 3)
            get_session_lock: 스레드별 락 반환 함수
            data_dir: 상태 파일 저장 디렉토리
            list_runner_ref: ListRunner 참조 함수
        """
        self.get_session_lock = get_session_lock
        self.list_runner_ref = list_runner_ref

        self.trello = trello_client
        self.prompt_builder = prompt_builder

        # config에서 설정값 직접 읽기 (기본값 없이 — yaml에 명시)
        self.notify_channel = config["notify_channel"]
        self.poll_interval = config["poll_interval"]
        self.watch_lists = config["watch_lists"]
        self.dm_target_user_id = config["dm_target_user_id"]
        self.polling_debug = config["polling_debug"]

        # Adaptive polling 설정 (선택적 — 없으면 기본값 사용)
        self.burst_interval = config.get("burst_interval", 3)
        self.max_burst_count = config.get("max_burst_count", 5)

        # 카드 병렬 처리 설정
        self._max_card_workers = config.get("max_card_workers", 3)

        # 리스트 IDs
        self._list_ids = config["list_ids"]

        # 상태 저장 경로
        self.data_dir = data_dir or Path.cwd() / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.tracked_file = self.data_dir / "tracked_cards.json"
        self.thread_cards_file = self.data_dir / "thread_cards.json"

        # 공유 상태 보호 락
        self._state_lock = threading.Lock()

        # 추적 중인 카드
        self._tracked: dict[str, TrackedCard] = {}
        self._load_tracked()

        # 스레드 ↔ 카드 매핑
        self._thread_cards: dict[str, ThreadCardInfo] = {}
        self._load_thread_cards()

        # 워처 스레드
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._paused = False
        self._pause_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # 리스트 정주행 직렬화 락
        self._list_run_lock = threading.Lock()

        # 카드 처리용 ThreadPoolExecutor
        self._card_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None

    # -- 상태 관리 메서드 --

    def _load_tracked(self):
        """추적 상태 로드"""
        if self.tracked_file.exists():
            try:
                data = json.loads(self.tracked_file.read_text(encoding="utf-8"))
                for card_id, card_data in data.items():
                    if "card_url" not in card_data:
                        card_data["card_url"] = ""
                    if "session_id" not in card_data:
                        card_data["session_id"] = None
                    if "has_execute" not in card_data:
                        card_data["has_execute"] = False
                    if "dm_thread_ts" not in card_data:
                        card_data["dm_thread_ts"] = None
                    self._tracked[card_id] = TrackedCard(**card_data)
                logger.info(f"추적 상태 로드: {len(self._tracked)}개 카드")
            except Exception as e:
                logger.error(f"추적 상태 로드 실패: {e}")

    def _save_tracked(self):
        """추적 상태 저장"""
        try:
            data = {k: asdict(v) for k, v in self._tracked.items()}
            self.tracked_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"추적 상태 저장 실패: {e}")

    _THREAD_CARD_REQUIRED_FIELDS = {"thread_ts", "channel_id", "card_id", "card_name", "card_url"}

    def _load_thread_cards(self):
        """스레드-카드 매핑 로드

        필수 필드가 누락된 항목은 기본값으로 보완하여 로드한다.
        """
        if self.thread_cards_file.exists():
            try:
                data = json.loads(self.thread_cards_file.read_text(encoding="utf-8"))
                for thread_ts, info_data in data.items():
                    # 필수 필드 기본값 보완
                    for field in self._THREAD_CARD_REQUIRED_FIELDS:
                        if field not in info_data:
                            info_data[field] = ""
                    if "session_id" not in info_data:
                        info_data["session_id"] = None
                    if "has_execute" not in info_data:
                        info_data["has_execute"] = False
                    if "created_at" not in info_data:
                        info_data["created_at"] = ""
                    self._thread_cards[thread_ts] = ThreadCardInfo(**info_data)
                logger.info(f"스레드-카드 매핑 로드: {len(self._thread_cards)}개")
            except Exception as e:
                logger.error(f"스레드-카드 매핑 로드 실패: {e}")

    def _save_thread_cards(self):
        """스레드-카드 매핑 저장"""
        try:
            data = {k: asdict(v) for k, v in self._thread_cards.items()}
            self.thread_cards_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"스레드-카드 매핑 저장 실패: {e}")

    def _register_thread_card(self, tracked: TrackedCard):
        """스레드-카드 매핑 등록"""
        info = ThreadCardInfo(
            thread_ts=tracked.thread_ts,
            channel_id=tracked.channel_id,
            card_id=tracked.card_id,
            card_name=tracked.card_name,
            card_url=tracked.card_url,
            session_id=tracked.session_id,
            has_execute=tracked.has_execute,
            created_at=tracked.detected_at,
        )
        self._thread_cards[tracked.thread_ts] = info
        self._save_thread_cards()
        logger.debug(f"스레드-카드 매핑 등록: {tracked.thread_ts} -> {tracked.card_name}")

    def _untrack_card(self, card_id: str):
        """카드 추적 해제

        _tracked와 _thread_cards를 함께 정리하여 orphan 데이터를 방지한다.
        """
        with self._state_lock:
            if card_id in self._tracked:
                tracked = self._tracked.pop(card_id)
                self._save_tracked()
                # _thread_cards에서도 해당 카드의 매핑을 제거
                if tracked.thread_ts in self._thread_cards:
                    del self._thread_cards[tracked.thread_ts]
                    self._save_thread_cards()
                logger.info(f"카드 추적 해제: {tracked.card_name}")

    def update_thread_card_session_id(self, thread_ts: str, session_id: str) -> bool:
        """ThreadCardInfo의 session_id 업데이트"""
        with self._state_lock:
            if thread_ts in self._thread_cards:
                self._thread_cards[thread_ts].session_id = session_id
                self._save_thread_cards()
                return True
            return False

    def get_tracked_by_thread_ts(self, thread_ts: str) -> Optional[ThreadCardInfo]:
        """thread_ts로 ThreadCardInfo 조회"""
        return self._thread_cards.get(thread_ts)

    def update_tracked_session_id(self, card_id: str, session_id: str) -> bool:
        """TrackedCard의 session_id 업데이트"""
        with self._state_lock:
            if card_id in self._tracked:
                self._tracked[card_id].session_id = session_id
                self._save_tracked()
                return True
            return False

    # -- 워처 라이프사이클 --

    def start(self):
        """워처 시작"""
        if not self.trello.is_configured():
            logger.warning("Trello API가 설정되지 않아 워처를 시작하지 않습니다.")
            return

        if self._thread and self._thread.is_alive():
            logger.warning("워처가 이미 실행 중입니다.")
            return

        self._stop_event.clear()
        self._card_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._max_card_workers,
            thread_name_prefix="watcher-card",
        )
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info(
            f"Trello 워처 시작: {self.poll_interval}초 간격 "
            f"(burst: {self.burst_interval}초, 최대 {self.max_burst_count}회)"
        )

    def stop(self):
        """워처 중지"""
        self._stop_event.set()
        if self._card_executor:
            self._card_executor.shutdown(wait=False)
            self._card_executor = None
        if self._thread:
            self._thread.join(timeout=5)
            logger.info("Trello 워처 중지")

    def pause(self):
        """워처 일시 중단"""
        with self._pause_lock:
            self._paused = True
            logger.info("Trello 워처 일시 중단")

    def resume(self):
        """워처 재개"""
        with self._pause_lock:
            self._paused = False
            logger.info("Trello 워처 재개")

    @property
    def is_paused(self) -> bool:
        with self._pause_lock:
            return self._paused

    def _run(self):
        """워처 메인 루프 (adaptive polling)

        기본적으로 poll_interval 간격으로 폴링하되,
        새 카드가 감지되면 burst_interval 간격으로 최대 max_burst_count회 재폴링한다.
        연속으로 새 카드가 없으면 기존 간격으로 복귀한다.
        """
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        burst_remaining = 0

        try:
            while not self._stop_event.is_set():
                found_new = False
                try:
                    found_new = self._poll()
                except Exception as e:
                    logger.exception(f"워처 폴링 오류: {e}")

                if found_new and burst_remaining == 0:
                    burst_remaining = self.max_burst_count
                    logger.debug(
                        f"Burst 모드 진입: {self.burst_interval}초 간격, "
                        f"최대 {burst_remaining}회"
                    )

                if burst_remaining > 0:
                    if not found_new:
                        burst_remaining = 0
                        logger.debug("Burst 모드 종료: 새 카드 없음")
                        wait_time = self.poll_interval
                    else:
                        burst_remaining -= 1
                        wait_time = self.burst_interval
                        if burst_remaining == 0:
                            logger.debug("Burst 모드 종료: 최대 횟수 도달")
                else:
                    wait_time = self.poll_interval

                self._stop_event.wait(timeout=wait_time)
        finally:
            self._loop.close()

    def _poll(self) -> bool:
        """리스트 폴링

        Returns:
            새 카드가 감지되었으면 True, 아니면 False
        """
        if self.is_paused:
            logger.debug("Trello 워처 일시 중단 상태 - 폴링 스킵")
            return False

        if self.polling_debug:
            logger.debug("Trello 폴링 시작")

        current_cards: dict[str, tuple[TrelloCard, str]] = {}
        for list_key, list_id in self.watch_lists.items():
            cards = self.trello.get_cards_in_list(list_id)
            for card in cards:
                current_cards[card.id] = (card, list_key)

        self._cleanup_stale_tracked(current_cards)

        new_cards: list[tuple[TrelloCard, str]] = []
        with self._state_lock:
            for card_id, (card, list_key) in current_cards.items():
                if card_id not in self._tracked:
                    logger.info(f"새 카드 감지: [{list_key}] {card.name}")
                    new_cards.append((card, list_key))

        if new_cards:
            self._handle_new_cards(new_cards)

        self._check_review_list_for_completion()
        self._check_run_list_labels()

        return len(new_cards) > 0

    STALE_THRESHOLD = timedelta(hours=2)

    def _cleanup_stale_tracked(self, current_cards: dict[str, tuple]):
        """만료된 _tracked 항목 정리"""
        now = datetime.now()
        stale_ids = []
        with self._state_lock:
            for card_id, tracked in self._tracked.items():
                try:
                    detected = datetime.fromisoformat(tracked.detected_at)
                except (ValueError, TypeError):
                    detected = now
                if now - detected >= self.STALE_THRESHOLD:
                    stale_ids.append(card_id)

        for card_id in stale_ids:
            with self._state_lock:
                if card_id not in self._tracked:
                    continue
                in_watch_list = card_id in current_cards
                tracked = self._tracked[card_id]
                logger.info(
                    f"stale 카드 정리: {tracked.card_name} "
                    f"(감시 리스트 {'내' if in_watch_list else '외'}, "
                    f"경과: {now - datetime.fromisoformat(tracked.detected_at)})"
                )
            self._untrack_card(card_id)

    def _check_review_list_for_completion(self):
        """Review 리스트에서 dueComplete된 카드를 Done으로 자동 이동"""
        review_list_id = self._list_ids.get("review")
        done_list_id = self._list_ids.get("done")

        if not review_list_id or not done_list_id:
            return

        cards = self.trello.get_cards_in_list(review_list_id)
        for card in cards:
            if card.due_complete:
                logger.info(f"dueComplete 카드 감지: {card.name} -> Done으로 이동")
                if self.trello.move_card(card.id, done_list_id):
                    logger.info(f"카드 이동 완료: {card.name}")
                    try:
                        channel = self._get_dm_or_notify_channel()
                        self._loop.run_until_complete(
                            slack.send_message(
                                channel=channel,
                                text=f"✅ <{card.url}|*{card.name}*>"
                            )
                        )
                    except Exception as e:
                        logger.error(f"완료 알림 전송 실패: {e}")
                else:
                    logger.error(f"카드 이동 실패: {card.name}")

    # -- 유틸리티 메서드 --

    def _add_spinner_prefix(self, card: TrelloCard) -> bool:
        if card.name.startswith("🌀"):
            return True
        new_name = f"🌀 {card.name}"
        return self.trello.update_card_name(card.id, new_name)

    def _remove_spinner_prefix(self, card_id: str, card_name: str) -> bool:
        if not card_name.startswith("🌀"):
            return True
        new_name = card_name.lstrip("🌀").lstrip()
        return self.trello.update_card_name(card_id, new_name)

    def _has_execute_label(self, card: TrelloCard) -> bool:
        for label in card.labels:
            if label.get("name", "").lower() == "execute":
                return True
        return False

    def _has_run_list_label(self, card: TrelloCard) -> bool:
        for label in card.labels:
            if label.get("name", "") == "🏃 Run List":
                return True
        return False

    def _get_run_list_label_id(self, card: TrelloCard) -> Optional[str]:
        for label in card.labels:
            if label.get("name", "") == "🏃 Run List":
                return label.get("id")
        return None

    def _build_header(self, card_name: str, card_url: str, session_id: str = "") -> str:
        session_display = f" | #️⃣ {session_id[:8]}" if session_id else ""
        return f"*🎫 <{card_url}|{card_name}>{session_display}*"

    def _get_dm_or_notify_channel(self) -> str:
        if self.dm_target_user_id:
            try:
                dm_channel_id = self._loop.run_until_complete(
                    slack.open_dm(self.dm_target_user_id)
                )
                if dm_channel_id:
                    return dm_channel_id
            except Exception as e:
                logger.warning(f"DM 채널 열기 실패 (notify_channel로 폴백): {e}")
        return self.notify_channel

    def _open_dm_thread(self, card_name: str, card_url: str) -> tuple[Optional[str], Optional[str]]:
        if not self.dm_target_user_id:
            return None, None
        try:
            dm_channel_id = self._loop.run_until_complete(
                slack.open_dm(self.dm_target_user_id)
            )
            if not dm_channel_id:
                return None, None

            anchor_text = f"🎫 *<{card_url}|{card_name}>*\n`사고 과정을 기록합니다...`"
            result = self._loop.run_until_complete(
                slack.send_message(
                    channel=dm_channel_id,
                    text=anchor_text,
                    blocks=[{
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": anchor_text}
                    }]
                )
            )
            if not result.ok:
                logger.warning(f"DM 앵커 메시지 전송 실패: {result.error}")
                return None, None

            dm_thread_ts = result.ts
            logger.info(f"DM 스레드 생성: channel={dm_channel_id}, thread_ts={dm_thread_ts}")
            return dm_channel_id, dm_thread_ts
        except Exception as e:
            logger.warning(f"DM 스레드 생성 실패: {e}")
            return None, None

    # -- 카드 처리 --

    def _handle_new_cards(self, new_cards: list[tuple[TrelloCard, str]]):
        """새 카드들을 병렬로 처리

        _card_executor가 있으면 ThreadPoolExecutor로 병렬 실행,
        없으면 (테스트 등) 순차 실행한다.
        """
        if self._card_executor and len(new_cards) > 1:
            futures = []
            for card, list_key in new_cards:
                future = self._card_executor.submit(
                    self._handle_new_card, card, list_key
                )
                futures.append(future)

            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logger.exception(f"카드 병렬 처리 오류: {e}")
        else:
            for card, list_key in new_cards:
                self._handle_new_card(card, list_key)

    def _handle_new_card(self, card: TrelloCard, list_key: str):
        """새 카드 처리: 추적 등록 → 알림 → 🌀 추가 → Claude 실행 (이동은 실행 직전)

        카드를 In Progress로 이동하는 것은 Claude 세션이 실제로
        시작되는 시점까지 지연한다. 워커가 꽉 차 있는 등의 이유로
        세션 할당이 실패하면 카드만 옮겨놓고 아무 일도 안 일어나는
        상태를 방지한다.
        """
        has_execute = self._has_execute_label(card)
        dm_channel_id, dm_thread_ts = self._open_dm_thread(card.name, card.url)

        if dm_channel_id and dm_thread_ts:
            thread_ts = dm_thread_ts
            msg_channel = dm_channel_id
            logger.info(f"DM 모드: channel={dm_channel_id}, thread_ts={dm_thread_ts}")
        else:
            header = self._build_header(card.name, card.url)
            initial_text = f"{header}\n\n`소영이 생각합니다...`"
            try:
                result = self._loop.run_until_complete(
                    slack.send_message(
                        channel=self.notify_channel,
                        text=initial_text
                    )
                )
                if not result.ok:
                    logger.error(f"알림 전송 실패: {result.error}")
                    return

                thread_ts = result.ts
                msg_channel = self.notify_channel
                logger.info(f"알림 전송 완료 (폴백): thread_ts={thread_ts}")
                reaction = "arrow_forward" if has_execute else "thought_balloon"
                try:
                    self._loop.run_until_complete(
                        slack.add_reaction(
                            channel=self.notify_channel,
                            ts=thread_ts,
                            emoji=reaction
                        )
                    )
                except Exception as e:
                    logger.debug(f"초기 상태 리액션 추가 실패: {e}")
            except Exception as e:
                logger.error(f"알림 전송 실패: {e}")
                return

        if self._add_spinner_prefix(card):
            logger.info(f"🌀 prefix 추가: {card.name}")
        else:
            logger.warning(f"🌀 prefix 추가 실패: {card.name}")

        tracked = TrackedCard(
            card_id=card.id, card_name=card.name, card_url=card.url,
            list_id=card.list_id, list_key=list_key,
            thread_ts=thread_ts, channel_id=msg_channel,
            detected_at=datetime.now().isoformat(), has_execute=has_execute,
        )
        tracked.dm_thread_ts = dm_thread_ts

        with self._state_lock:
            self._tracked[card.id] = tracked
            self._save_tracked()

        self._register_thread_card(tracked)

        prompt = self.prompt_builder.build_to_go(card, has_execute)
        card_id_for_cleanup = card.id
        card_name_with_spinner = f"🌀 {card.name}"
        original_list_id = card.list_id
        in_progress_list_id = self._list_ids.get("in_progress")

        def on_start():
            # Claude 세션이 시작되는 시점에 카드를 In Progress로 이동
            if in_progress_list_id:
                if self.trello.move_card(card_id_for_cleanup, in_progress_list_id):
                    logger.info(f"카드 In Progress로 이동: {card.name}")
                else:
                    logger.warning(f"카드 In Progress 이동 실패: {card.name}")

        def on_error(e):
            # Claude 실행 실패 시 카드를 원래 리스트로 롤백
            if original_list_id:
                if self.trello.move_card(card_id_for_cleanup, original_list_id):
                    logger.info(
                        f"카드 원래 리스트로 롤백: {card.name} -> {original_list_id}"
                    )
                else:
                    logger.warning(f"카드 롤백 실패: {card.name}")

        def on_finally():
            if self._remove_spinner_prefix(card_id_for_cleanup, card_name_with_spinner):
                logger.info(f"🌀 prefix 제거: {card.name}")
            else:
                logger.warning(f"🌀 prefix 제거 실패: {card.name}")
            self._untrack_card(card_id_for_cleanup)

        self._spawn_claude_thread(
            prompt=prompt, thread_ts=thread_ts,
            channel=msg_channel, tracked=tracked,
            dm_channel_id=dm_channel_id, dm_thread_ts=dm_thread_ts,
            on_start=on_start, on_error=on_error, on_finally=on_finally,
        )

    def build_reaction_execute_prompt(self, info: ThreadCardInfo) -> str:
        """하위 호환: PromptBuilder에 위임"""
        return self.prompt_builder.build_reaction_execute(info)

    def _spawn_claude_thread(
        self,
        *,
        prompt: str,
        thread_ts: str,
        channel: str,
        tracked: TrackedCard,
        dm_channel_id: Optional[str] = None,
        dm_thread_ts: Optional[str] = None,
        on_start: Optional[Callable] = None,
        on_success: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
        on_finally: Optional[Callable] = None,
    ):
        """Claude 실행 스레드 스포닝 (plugin_sdk 사용)

        Args:
            on_start: soulstream.run() 직전에 호출되는 콜백.
                      카드 이동 등 실행 확정 시점에 수행할 작업에 사용.
        """

        def run_claude():
            claude_succeeded = False
            try:
                # Get existing session_id if available
                session_id = soulstream.get_session_id(thread_ts)

                # on_start: 실행 직전 콜백 (카드 이동 등)
                if on_start:
                    try:
                        on_start()
                    except Exception as e:
                        logger.exception(f"on_start 콜백 오류: {e}")

                # Run Claude using plugin_sdk
                result = self._loop.run_until_complete(
                    soulstream.run(
                        prompt=prompt,
                        channel=channel,
                        thread_ts=thread_ts,
                        role="admin",
                        session_id=session_id,
                        dm_channel_id=dm_channel_id,
                        dm_thread_ts=dm_thread_ts,
                    )
                )

                if result.ok:
                    claude_succeeded = True
                    # Update tracked card with session_id
                    if result.session_id:
                        tracked.session_id = result.session_id
                        self.update_tracked_session_id(tracked.card_id, result.session_id)
                else:
                    logger.error(f"Claude 실행 실패 (워처): {result.error}")
                    if on_error:
                        on_error(Exception(result.error))

            except Exception as e:
                logger.exception(f"Claude 실행 오류 (워처): {e}")
                if on_error:
                    on_error(e)

            if on_finally:
                try:
                    on_finally()
                except Exception as e:
                    logger.exception(f"on_finally 콜백 오류: {e}")

            if claude_succeeded and on_success:
                try:
                    on_success()
                except Exception as e:
                    logger.exception(f"on_success 콜백 오류: {e}")

        claude_thread = threading.Thread(target=run_claude, daemon=True)
        claude_thread.start()

    # -- 리스트 정주행 --

    def _get_operational_list_ids(self) -> set[str]:
        """운영 리스트 ID 집합 반환"""
        ids = set()
        for list_id in self.watch_lists.values():
            if list_id:
                ids.add(list_id)
        for list_id in self._list_ids.values():
            if list_id:
                ids.add(list_id)
        return ids

    def _check_run_list_labels(self):
        """🏃 Run List 레이블을 가진 카드 감지 및 리스트 정주행 시작

        레이블 제거는 _start_list_run() 내부에서 세션 생성 후 수행합니다.
        이렇게 하면 세션 시작 실패 시 레이블이 유지되어
        다음 폴링 사이클에서 재감지할 수 있습니다.
        """
        lists = self.trello.get_lists()
        operational_ids = self._get_operational_list_ids()

        for lst in lists:
            list_id = lst["id"]
            list_name = lst["name"]
            if list_id in operational_ids:
                continue

            cards = self.trello.get_cards_in_list(list_id)
            if not cards:
                continue

            first_card = cards[0]
            if not self._has_run_list_label(first_card):
                continue

            logger.info(f"🏃 Run List 레이블 감지: {list_name} - {first_card.name}")

            with self._list_run_lock:
                list_runner = self.list_runner_ref() if self.list_runner_ref else None
                if list_runner:
                    active_sessions = list_runner.get_active_sessions()
                    already_running = any(s.list_id == list_id for s in active_sessions)
                    if already_running:
                        logger.warning(f"이미 활성 정주행 세션이 있어 스킵: {list_name}")
                        continue

                self._start_list_run(list_id, list_name, cards, first_card)

    COMPACT_TIMEOUT_SECONDS = 60

    def _preemptive_compact(self, thread_ts: str, channel: str, card_name: str):
        """카드 완료 후 선제적 컨텍스트 컴팩트

        Uses plugin_sdk soulstream.compact() API.
        """
        session_id = soulstream.get_session_id(thread_ts)
        if not session_id:
            logger.warning(f"선제적 컴팩트 스킵: 세션 없음 (card={card_name})")
            return

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    self._loop.run_until_complete,
                    soulstream.compact(session_id)
                )
                try:
                    result = future.result(timeout=self.COMPACT_TIMEOUT_SECONDS)
                except concurrent.futures.TimeoutError:
                    logger.warning(
                        f"선제적 컴팩트 타임아웃 ({self.COMPACT_TIMEOUT_SECONDS}s): card={card_name}"
                    )
                    return

            if result.ok:
                logger.info(f"선제적 컴팩트 완료: card={card_name}")
                # Note: session_id 업데이트는 host에서 자동으로 처리됨
            else:
                logger.warning(f"선제적 컴팩트 실패: card={card_name}, error={result.error}")
        except Exception as e:
            logger.warning(f"선제적 컴팩트 예외: card={card_name}, {e}")

    def _start_list_run(
        self,
        list_id: str,
        list_name: str,
        cards: list[TrelloCard],
        trigger_card: Optional[TrelloCard] = None,
    ):
        """리스트 정주행 시작

        세션을 RUNNING 상태로 즉시 생성한 후, 트리거 카드에서
        🏃 Run List 레이블을 제거합니다. 이 순서를 통해:
        1. 세션 생성 실패 시 레이블이 유지되어 다음 폴링에서 재감지
        2. 세션이 RUNNING으로 즉시 등록되어 get_active_sessions()에 반영

        Args:
            list_id: 트렐로 리스트 ID
            list_name: 리스트 이름
            cards: 리스트 내 전체 카드 목록
            trigger_card: 🏃 Run List 레이블이 붙어있는 카드.
                None이면 레이블 제거를 건너뜁니다.
        """
        logger.info(f"리스트 정주행 시작: {list_name} ({len(cards)}개 카드)")

        list_runner = self.list_runner_ref() if self.list_runner_ref else None
        if not list_runner:
            logger.warning("ListRunner가 설정되지 않아 정주행을 시작할 수 없습니다.")
            return

        card_ids = [card.id for card in cards]
        session = list_runner.create_session(
            list_id=list_id, list_name=list_name, card_ids=card_ids,
        )

        # 세션 생성 성공 후 레이블 제거 (실패해도 정주행은 계속 진행)
        if trigger_card:
            label_id = self._get_run_list_label_id(trigger_card)
            if label_id:
                if self.trello.remove_label_from_card(trigger_card.id, label_id):
                    logger.info(f"🏃 Run List 레이블 제거: {trigger_card.name}")
                else:
                    logger.warning(
                        f"레이블 제거 실패 (정주행은 계속 진행): {trigger_card.name}"
                    )

        dm_channel_id, dm_thread_ts = self._open_dm_thread(f"📋 {list_name} 정주행", "")

        if dm_channel_id and dm_thread_ts:
            run_channel = dm_channel_id
            run_thread_ts = dm_thread_ts
        else:
            run_channel = self.notify_channel
            try:
                card_preview = "\n".join([f"  • {c.name}" for c in cards[:5]])
                if len(cards) > 5:
                    card_preview += f"\n  ... 외 {len(cards) - 5}개"
                result = self._loop.run_until_complete(
                    slack.send_message(
                        channel=self.notify_channel,
                        text=(
                            f"🚀 *리스트 정주행 시작*\n"
                            f"📋 리스트: *{list_name}*\n"
                            f"🎫 카드 수: {len(cards)}개\n"
                            f"🔖 세션 ID: `{session.session_id}`\n\n"
                            f"*처리할 카드:*\n{card_preview}"
                        )
                    )
                )
                if not result.ok:
                    logger.error(f"정주행 시작 알림 전송 실패: {result.error}")
                    return
                run_thread_ts = result.ts
            except Exception as e:
                logger.error(f"정주행 시작 알림 전송 실패: {e}")
                return

        self._process_list_run_card(session.session_id, run_thread_ts, run_channel)

    def _process_list_run_card(self, session_id: str, thread_ts: str, run_channel: str = None):
        """리스트 정주행 카드 처리"""
        list_runner = self.list_runner_ref() if self.list_runner_ref else None
        if not list_runner:
            return

        channel = run_channel or self.notify_channel

        try:
            self._process_list_run_card_inner(
                list_runner, session_id, thread_ts, channel, run_channel
            )
        except Exception as e:
            logger.exception(f"정주행 카드 처리 중 미처리 예외: session={session_id}, error={e}")
            try:
                from seosoyoung_plugins.trello.list_runner import SessionStatus
                list_runner.pause_run(session_id, f"미처리 예외: {e}")
            except Exception:
                pass
            try:
                self._loop.run_until_complete(
                    slack.send_message(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=f"⚠️ 정주행 카드 처리 중 오류.\n세션 ID: `{session_id}`\n오류: {e}"
                    )
                )
            except Exception:
                pass

    def _process_list_run_card_inner(
        self, list_runner, session_id: str, thread_ts: str,
        channel: str, run_channel: str = None,
    ):
        """리스트 정주행 카드 처리 내부 로직"""
        from seosoyoung_plugins.trello.list_runner import SessionStatus

        session = list_runner.get_session(session_id)
        if not session:
            logger.error(f"정주행 세션을 찾을 수 없습니다: {session_id}")
            return

        next_card_id = list_runner.get_next_card_id(session_id)
        if not next_card_id:
            list_runner.update_session_status(session_id, SessionStatus.COMPLETED)
            self._loop.run_until_complete(
                slack.send_message(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"✅ *리스트 정주행 완료*\n세션 ID: `{session_id}`"
                )
            )
            logger.info(f"리스트 정주행 완료: {session_id}")
            return

        list_runner.update_session_status(session_id, SessionStatus.RUNNING)

        skip_duplicate = False
        with self._state_lock:
            if next_card_id in self._tracked:
                existing = self._tracked[next_card_id]
                if existing.thread_ts != thread_ts:
                    skip_duplicate = True

        if skip_duplicate:
            logger.warning(f"카드가 다른 세션에서 이미 처리 중이므로 스킵: {next_card_id}")
            list_runner.mark_card_processed(session_id, next_card_id, "skipped_duplicate")
            self._process_list_run_card(session_id, thread_ts, run_channel)
            return

        card = self.trello.get_card(next_card_id)
        if not card:
            logger.error(f"카드를 찾을 수 없습니다: {next_card_id}")
            list_runner.mark_card_processed(session_id, next_card_id, "skipped")
            self._process_list_run_card(session_id, thread_ts, run_channel)
            return

        in_progress_list_id = self._list_ids.get("in_progress")
        if in_progress_list_id:
            self.trello.move_card(card.id, in_progress_list_id)

        self._add_spinner_prefix(card)

        progress = f"{session.current_index + 1}/{len(session.card_ids)}"
        self._loop.run_until_complete(
            slack.send_message(
                channel=channel,
                thread_ts=thread_ts,
                text=f"▶️ [{progress}] <{card.url}|{card.name}>"
            )
        )

        prompt = self.prompt_builder.build_list_run(
            card, session_id, session.current_index + 1, len(session.card_ids)
        )

        if channel != self.notify_channel:
            dm_channel_id, dm_thread_ts = channel, thread_ts
        else:
            dm_channel_id, dm_thread_ts = self._open_dm_thread(card.name, card.url)

        tracked = TrackedCard(
            card_id=card.id, card_name=card.name, card_url=card.url,
            list_id=card.list_id, list_key="list_run",
            thread_ts=thread_ts, channel_id=channel,
            detected_at=datetime.now().isoformat(), has_execute=True,
        )

        with self._state_lock:
            self._tracked[card.id] = tracked
            self._save_tracked()

        def on_success():
            list_runner.mark_card_processed(session_id, card.id, "completed")
            self._remove_spinner_prefix(card.id, f"🌀 {card.name}")
            self._untrack_card(card.id)
            try:
                self._preemptive_compact(thread_ts, channel, card.name)
            except Exception as compact_err:
                logger.warning(f"선제적 컴팩트 실패: card={card.name}, error={compact_err}")
            next_thread = threading.Thread(
                target=self._process_list_run_card,
                args=(session_id, thread_ts, run_channel), daemon=True
            )
            next_thread.start()

        def on_error(e):
            list_runner.mark_card_processed(session_id, card.id, "failed")
            list_runner.pause_run(session_id, str(e))
            self._remove_spinner_prefix(card.id, f"🌀 {card.name}")
            self._untrack_card(card.id)
            logger.error(f"정주행 카드 실패: card={card.name}, session={session_id}")
            self._loop.run_until_complete(
                slack.send_message(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"❌ 카드 처리 실패: {card.name}\n세션: `{session_id}` | 오류: {e}"
                )
            )

        self._spawn_claude_thread(
            prompt=prompt, thread_ts=thread_ts,
            channel=channel, tracked=tracked,
            dm_channel_id=dm_channel_id, dm_thread_ts=dm_thread_ts,
            on_success=on_success, on_error=on_error,
        )
