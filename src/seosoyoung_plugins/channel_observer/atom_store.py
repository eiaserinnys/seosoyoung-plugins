"""atom HTTP 기반 채널 관찰 데이터 저장소

pending/thread 버퍼는 in-memory + atom write-through (fire-and-forget).
judged 버퍼는 in-memory ephemeral.
digest는 atom 지난 주요 사건 노드에 저장.

atom 트리 구조:
    슬랙 대화 (config.atom_slack_root_node_id)
      └── {channel_id} (structure)
            ├── {YYYY-MM-DD} (structure, 날짜 경계 새벽4시 KST)
            │     └── 스레드 {thread_ts} (structure)
            │           ├── 원문 {ts} (knowledge, staleness=uninterpreted)
            │           └── 답글 {ts} (knowledge, staleness=uninterpreted)
            └── 지난 주요 사건 (structure)
                  └── {date} {channel_id} 요약 (knowledge)
"""

import asyncio
import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

_RETRY_DELAYS = [1.0, 2.0, 4.0]


class AtomChannelStore:
    """atom HTTP API 기반 채널 관찰 데이터 저장소.

    pending/thread 버퍼: in-memory + atom write-through (fire-and-forget).
    judged 버퍼: in-memory ephemeral (atom에 interpreted staleness로 표현).
    digest: atom 지난 주요 사건 노드에 저장.
    """

    def __init__(self, config: dict) -> None:
        self._base_url: str = config["atom_base_url"].rstrip("/")
        self._api_key: str = config["atom_api_key"]
        self._slack_root_node_id: str = config["atom_slack_root_node_id"]

        # In-memory buffers
        self._pending: dict[str, dict[str, dict]] = {}       # {ch_id: {ts: msg}}
        self._pending_card_ids: dict[str, dict[str, str]] = {}  # {ch_id: {ts: card_id}}
        self._thread_buffers: dict[str, dict[str, dict[str, dict]]] = {}
        self._thread_card_ids: dict[str, dict[str, dict[str, str]]] = {}
        self._judged: dict[str, list] = {}

        # Node caches
        self._channel_nodes: dict[str, str] = {}          # ch_id → node_id
        self._date_nodes: dict[tuple, str] = {}           # (ch_id, date_key) → node_id
        self._thread_nodes: dict[tuple, str] = {}         # (ch_id, thread_ts) → node_id
        self._digest_nodes: dict[str, str] = {}           # ch_id → digest_folder_node_id
        self._digest_card_ids: dict[tuple, str] = {}      # (ch_id, date_key) → card_id

    # ── 날짜 경계 (KST 새벽 4시) ────────────────────────────────────────

    @staticmethod
    def _get_date_key(ts: float) -> str:
        """타임스탬프로 날짜 키를 반환. KST 새벽 4시 기준으로 날짜가 바뀐다."""
        kst = datetime.fromtimestamp(ts, tz=ZoneInfo("Asia/Seoul"))
        if kst.hour < 4:
            kst = kst - timedelta(days=1)
        return kst.strftime("%Y-%m-%d")

    # ── HTTP 헬퍼 ──────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {"x-api-key": self._api_key, "Content-Type": "application/json"}

    async def _post_with_retry(self, path: str, body: dict) -> dict | None:
        """POST 요청, 실패 시 3회 exponential backoff retry."""
        url = f"{self._base_url}{path}"
        for attempt, delay in enumerate(_RETRY_DELAYS):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(url, json=body, headers=self._headers())
                    if resp.status_code < 300:
                        return resp.json()
                    logger.warning(
                        "atom POST %s 실패 (status=%d, attempt=%d)",
                        path, resp.status_code, attempt + 1,
                    )
            except Exception as e:
                logger.warning("atom POST %s 예외 (attempt=%d): %s", path, attempt + 1, e)
            if attempt < len(_RETRY_DELAYS) - 1:
                await asyncio.sleep(delay)
        logger.warning("atom POST %s 최종 실패 (3회 재시도 소진)", path)
        return None

    async def _patch_with_retry(self, path: str, body: dict) -> dict | None:
        """PATCH 요청, 실패 시 3회 exponential backoff retry."""
        url = f"{self._base_url}{path}"
        for attempt, delay in enumerate(_RETRY_DELAYS):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.patch(url, json=body, headers=self._headers())
                    if resp.status_code < 300:
                        return resp.json()
                    logger.warning(
                        "atom PATCH %s 실패 (status=%d, attempt=%d)",
                        path, resp.status_code, attempt + 1,
                    )
            except Exception as e:
                logger.warning("atom PATCH %s 예외 (attempt=%d): %s", path, attempt + 1, e)
            if attempt < len(_RETRY_DELAYS) - 1:
                await asyncio.sleep(delay)
        logger.warning("atom PATCH %s 최종 실패 (3회 재시도 소진)", path)
        return None

    async def _get_with_retry(self, path: str, params: dict | None = None) -> dict | None:
        """GET 요청, 실패 시 3회 exponential backoff retry."""
        url = f"{self._base_url}{path}"
        for attempt, delay in enumerate(_RETRY_DELAYS):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(url, params=params, headers=self._headers())
                    if resp.status_code < 300:
                        return resp.json()
                    logger.warning(
                        "atom GET %s 실패 (status=%d, attempt=%d)",
                        path, resp.status_code, attempt + 1,
                    )
            except Exception as e:
                logger.warning("atom GET %s 예외 (attempt=%d): %s", path, attempt + 1, e)
            if attempt < len(_RETRY_DELAYS) - 1:
                await asyncio.sleep(delay)
        logger.warning("atom GET %s 최종 실패 (3회 재시도 소진)", path)
        return None

    # ── fire-and-forget ──────────────────────────────────────────────

    def _fire_and_forget(self, coro) -> None:
        """코루틴을 백그라운드 스레드에서 실행. 실패 시 경고 로그 후 드롭."""
        def run():
            try:
                asyncio.run(coro)
            except Exception as e:
                logger.warning("atom 백그라운드 쓰기 실패: %s", e)

        threading.Thread(target=run, daemon=True).start()

    # ── 노드 생성/조회 헬퍼 ──────────────────────────────────────────

    async def _create_card(self, title: str, parent_node_id: str, card_type: str = "structure",
                           content: str | None = None, tags: list[str] | None = None) -> str | None:
        """카드를 생성하고 node_id를 반환."""
        body: dict = {
            "card_type": card_type,
            "title": title,
            "parent_node_id": parent_node_id,
        }
        if content is not None:
            body["content"] = content
        if tags:
            body["tags"] = tags
        result = await self._post_with_retry("/api/cards", body)
        if result:
            return result.get("node_id")
        return None

    async def _create_knowledge_card(
        self, title: str, parent_node_id: str, content: str = "",
        staleness: str = "uninterpreted",
    ) -> str | None:
        """knowledge 카드를 생성하고 card_id를 반환.

        CreateCardInput에 staleness가 없으므로 POST 후 PATCH로 staleness 설정.
        """
        body = {
            "card_type": "knowledge",
            "title": title,
            "parent_node_id": parent_node_id,
            "content": content,
        }
        result = await self._post_with_retry("/api/cards", body)
        if not result:
            return None
        card_id = result.get("id") or result.get("card_id")
        if card_id and staleness:
            await self._patch_with_retry(
                f"/api/cards/{card_id}",
                {"staleness": staleness},
            )
        return card_id

    async def _get_or_create_channel_node(self, channel_id: str) -> str | None:
        """채널 구조 노드를 조회하거나 생성."""
        if channel_id in self._channel_nodes:
            return self._channel_nodes[channel_id]
        if not self._slack_root_node_id:
            return None
        node_id = await self._create_card(channel_id, self._slack_root_node_id)
        if node_id:
            self._channel_nodes[channel_id] = node_id
        return node_id

    async def _get_or_create_date_node(self, channel_id: str, date_key: str) -> str | None:
        """날짜 구조 노드를 조회하거나 생성."""
        key = (channel_id, date_key)
        if key in self._date_nodes:
            return self._date_nodes[key]
        channel_node = await self._get_or_create_channel_node(channel_id)
        if not channel_node:
            return None
        node_id = await self._create_card(date_key, channel_node)
        if node_id:
            self._date_nodes[key] = node_id
        return node_id

    async def _get_or_create_thread_node(
        self, channel_id: str, thread_ts: str
    ) -> str | None:
        """스레드 구조 노드를 조회하거나 생성."""
        key = (channel_id, thread_ts)
        if key in self._thread_nodes:
            return self._thread_nodes[key]
        ts_float = float(thread_ts)
        date_key = self._get_date_key(ts_float)
        date_node = await self._get_or_create_date_node(channel_id, date_key)
        if not date_node:
            return None
        node_id = await self._create_card(f"스레드 {thread_ts}", date_node)
        if node_id:
            self._thread_nodes[key] = node_id
        return node_id

    async def _get_or_create_digest_node(self, channel_id: str) -> str | None:
        """지난 주요 사건 구조 노드를 조회하거나 생성."""
        if channel_id in self._digest_nodes:
            return self._digest_nodes[channel_id]
        channel_node = await self._get_or_create_channel_node(channel_id)
        if not channel_node:
            return None
        node_id = await self._create_card("지난 주요 사건", channel_node)
        if node_id:
            self._digest_nodes[channel_id] = node_id
        return node_id

    # ── atom 쓰기 코루틴 ─────────────────────────────────────────────

    async def _write_pending_card(self, channel_id: str, message: dict) -> None:
        """pending 메시지를 atom에 기록."""
        ts = message.get("ts", "")
        thread_ts = message.get("thread_ts", ts) or ts
        thread_node = await self._get_or_create_thread_node(channel_id, thread_ts)
        if not thread_node:
            return
        title = f"원문 {ts}" if thread_ts == ts else f"답글 {ts}"
        content = message.get("text", "")
        card_id = await self._create_knowledge_card(
            title=title,
            parent_node_id=thread_node,
            content=content,
            staleness="uninterpreted",
        )
        if card_id:
            if channel_id not in self._pending_card_ids:
                self._pending_card_ids[channel_id] = {}
            self._pending_card_ids[channel_id][ts] = card_id

    async def _write_thread_card(
        self, channel_id: str, thread_ts: str, message: dict
    ) -> None:
        """스레드 메시지를 atom에 기록."""
        ts = message.get("ts", "")
        thread_node = await self._get_or_create_thread_node(channel_id, thread_ts)
        if not thread_node:
            return
        title = f"답글 {ts}" if ts != thread_ts else f"원문 {ts}"
        content = message.get("text", "")
        card_id = await self._create_knowledge_card(
            title=title,
            parent_node_id=thread_node,
            content=content,
            staleness="uninterpreted",
        )
        if card_id:
            if channel_id not in self._thread_card_ids:
                self._thread_card_ids[channel_id] = {}
            if thread_ts not in self._thread_card_ids[channel_id]:
                self._thread_card_ids[channel_id][thread_ts] = {}
            self._thread_card_ids[channel_id][thread_ts][ts] = card_id

    async def _write_digest_card(
        self, channel_id: str, content: str, meta: dict
    ) -> None:
        """digest를 atom에 기록하거나 업데이트."""
        date_key = meta.get("date", datetime.now(tz=ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d"))
        cache_key = (channel_id, date_key)
        title = f"{date_key} {channel_id} 요약"

        existing_card_id = self._digest_card_ids.get(cache_key)
        if existing_card_id:
            await self._patch_with_retry(
                f"/api/cards/{existing_card_id}",
                {"content": content, "title": title},
            )
        else:
            digest_node = await self._get_or_create_digest_node(channel_id)
            if not digest_node:
                return
            body = {
                "card_type": "knowledge",
                "title": title,
                "parent_node_id": digest_node,
                "content": content,
            }
            result = await self._post_with_retry("/api/cards", body)
            if result:
                card_id = result.get("id") or result.get("card_id")
                if card_id:
                    self._digest_card_ids[cache_key] = card_id

    async def _patch_card_staleness(self, card_id: str, staleness: str) -> None:
        """카드 staleness를 업데이트."""
        await self._patch_with_retry(
            f"/api/cards/{card_id}",
            {"staleness": staleness},
        )

    # ── pending 버퍼 ─────────────────────────────────────────────────

    def append_pending(self, channel_id: str, message: dict) -> None:
        """채널 루트 메시지를 pending 버퍼에 추가."""
        ts = message.get("ts", "")
        if channel_id not in self._pending:
            self._pending[channel_id] = {}
        self._pending[channel_id][ts] = message
        self._fire_and_forget(self._write_pending_card(channel_id, message))

    def upsert_pending(self, channel_id: str, message: dict) -> None:
        """같은 ts의 메시지가 있으면 교체, 없으면 추가."""
        ts = message.get("ts", "")
        if channel_id not in self._pending:
            self._pending[channel_id] = {}
        self._pending[channel_id][ts] = message
        self._fire_and_forget(self._write_pending_card(channel_id, message))

    def append_channel_message(self, channel_id: str, message: dict) -> None:
        """append_pending의 하위호환 별칭."""
        return self.append_pending(channel_id, message)

    def load_pending(self, channel_id: str) -> list[dict]:
        """pending 버퍼 로드."""
        return list(self._pending.get(channel_id, {}).values())

    def clear_pending(self, channel_id: str) -> None:
        """pending 버퍼 비우기."""
        self._pending.pop(channel_id, None)
        self._pending_card_ids.pop(channel_id, None)

    def load_channel_buffer(self, channel_id: str) -> list[dict]:
        """load_pending의 하위호환 별칭."""
        return self.load_pending(channel_id)

    # ── 스레드 버퍼 ──────────────────────────────────────────────────

    def append_thread_message(
        self, channel_id: str, thread_ts: str, message: dict
    ) -> None:
        """스레드 메시지를 버퍼에 추가."""
        ts = message.get("ts", "")
        if channel_id not in self._thread_buffers:
            self._thread_buffers[channel_id] = {}
        if thread_ts not in self._thread_buffers[channel_id]:
            self._thread_buffers[channel_id][thread_ts] = {}
        self._thread_buffers[channel_id][thread_ts][ts] = message
        self._fire_and_forget(
            self._write_thread_card(channel_id, thread_ts, message)
        )

    def upsert_thread_message(
        self, channel_id: str, thread_ts: str, message: dict
    ) -> None:
        """같은 ts의 스레드 메시지가 있으면 교체, 없으면 추가."""
        ts = message.get("ts", "")
        if channel_id not in self._thread_buffers:
            self._thread_buffers[channel_id] = {}
        if thread_ts not in self._thread_buffers[channel_id]:
            self._thread_buffers[channel_id][thread_ts] = {}
        self._thread_buffers[channel_id][thread_ts][ts] = message
        self._fire_and_forget(
            self._write_thread_card(channel_id, thread_ts, message)
        )

    def load_all_thread_buffers(self, channel_id: str) -> dict[str, list[dict]]:
        """{thread_ts: [messages]} 형태로 반환."""
        result = {}
        for thread_ts, msgs in self._thread_buffers.get(channel_id, {}).items():
            if msgs:
                result[thread_ts] = list(msgs.values())
        return result

    # ── judged 버퍼 ─────────────────────────────────────────────────

    def append_judged(self, channel_id: str, messages: list[dict]) -> None:
        """judged 버퍼에 메시지들을 추가."""
        if channel_id not in self._judged:
            self._judged[channel_id] = []
        self._judged[channel_id].extend(messages)

    def load_judged(self, channel_id: str) -> list[dict]:
        """judged 버퍼 로드."""
        return list(self._judged.get(channel_id, []))

    def clear_judged(self, channel_id: str) -> None:
        """judged 버퍼 비우기."""
        self._judged[channel_id] = []

    # ── pending → judged 이동 ─────────────────────────────────────

    def move_snapshot_to_judged(
        self,
        channel_id: str,
        snapshot_ts: set[str],
        snapshot_thread_ts: set[str] | None = None,
    ) -> None:
        """스냅샷에 포함된 메시지를 judged로 이동하고 staleness를 interpreted로 업데이트."""
        pending = self._pending.get(channel_id, {})

        to_judged = []
        for ts in list(snapshot_ts):
            msg = pending.pop(ts, None)
            if msg is not None:
                to_judged.append(msg)
            # fire-and-forget: atom 카드 staleness 업데이트
            card_id = (self._pending_card_ids.get(channel_id) or {}).get(ts)
            if card_id:
                self._fire_and_forget(
                    self._patch_card_staleness(card_id, "interpreted")
                )

        if to_judged:
            self.append_judged(channel_id, to_judged)

        if snapshot_thread_ts:
            thread_bufs = self._thread_buffers.get(channel_id, {})
            for thread_ts in list(snapshot_thread_ts):
                msgs_dict = thread_bufs.pop(thread_ts, {})
                if msgs_dict:
                    self.append_judged(channel_id, list(msgs_dict.values()))
                # fire-and-forget: 스레드 카드 staleness 업데이트
                thread_card_ids = (
                    (self._thread_card_ids.get(channel_id) or {}).get(thread_ts) or {}
                )
                for ts, card_id in thread_card_ids.items():
                    self._fire_and_forget(
                        self._patch_card_staleness(card_id, "interpreted")
                    )

    # ── digest ────────────────────────────────────────────────────

    def get_digest(self, channel_id: str) -> dict | None:
        """digest를 반환.

        TODO: 초기 구현에서는 None을 반환하여 파이프라인이 새 digest로 시작하도록 한다.
        향후 atom에서 최신 digest 카드를 조회하는 방식으로 구현 가능.
        """
        return None

    def save_digest(self, channel_id: str, content: str, meta: dict) -> None:
        """digest를 atom에 저장."""
        self._fire_and_forget(
            self._write_digest_card(channel_id, content, meta)
        )

    # ── atom compile ──────────────────────────────────────────────

    async def compile_channel_context(
        self, channel_id: str, limit: int = 20
    ) -> str:
        """채널 노드의 subtree를 compile하여 markdown을 반환."""
        channel_node = self._channel_nodes.get(channel_id)
        if not channel_node:
            return ""
        result = await self._get_with_retry(
            f"/tree/{channel_node}/compile",
            params={"depth": 10, "limit": limit},
        )
        if result:
            return result.get("markdown", "")
        return ""

    # ── 토큰 카운팅 ──────────────────────────────────────────────

    def _count_messages_tokens(self, messages: list[dict]) -> int:
        from seosoyoung_plugins.memory.token_counter import TokenCounter
        counter = TokenCounter()
        total = 0
        for msg in messages:
            total += counter.count_string(msg.get("text", ""))
        return total

    def count_pending_tokens(self, channel_id: str) -> int:
        """pending 버퍼 총 토큰 수 (채널 + 스레드 합산)."""
        total = self._count_messages_tokens(self.load_pending(channel_id))
        for thread_msgs in self.load_all_thread_buffers(channel_id).values():
            total += self._count_messages_tokens(thread_msgs)
        return total

    def count_judged_plus_pending_tokens(self, channel_id: str) -> int:
        """judged + pending 합산 토큰 수."""
        judged_tokens = self._count_messages_tokens(self.load_judged(channel_id))
        pending_tokens = self.count_pending_tokens(channel_id)
        return judged_tokens + pending_tokens

    def count_buffer_tokens(self, channel_id: str) -> int:
        """count_pending_tokens의 하위호환 별칭."""
        return self.count_pending_tokens(channel_id)

    def load_thread_buffer(self, channel_id: str, thread_ts: str) -> list[dict]:
        """특정 스레드 버퍼 로드."""
        return list(
            self._thread_buffers.get(channel_id, {}).get(thread_ts, {}).values()
        )

    def clear_buffers(self, channel_id: str) -> None:
        """pending + judged + 스레드 버퍼를 모두 비운다."""
        self.clear_pending(channel_id)
        self.clear_judged(channel_id)
        self._thread_buffers.pop(channel_id, None)
        self._thread_card_ids.pop(channel_id, None)

    # ── reactions ────────────────────────────────────────────────

    def update_reactions(
        self, channel_id: str, *, ts: str, emoji: str, user: str, action: str,
    ) -> None:
        """in-memory 버퍼에서 reactions 갱신."""
        # pending 버퍼 검색
        pending = self._pending.get(channel_id, {})
        if ts in pending:
            self._apply_reaction(pending[ts], emoji, user, action)
            return

        # judged 버퍼 검색
        for msg in self._judged.get(channel_id, []):
            if msg.get("ts") == ts:
                self._apply_reaction(msg, emoji, user, action)
                return

        # 스레드 버퍼 검색
        for thread_msgs in self._thread_buffers.get(channel_id, {}).values():
            if ts in thread_msgs:
                self._apply_reaction(thread_msgs[ts], emoji, user, action)
                return

    @staticmethod
    def _apply_reaction(msg: dict, emoji: str, user: str, action: str) -> None:
        """메시지 dict에 reaction을 적용."""
        reactions = msg.setdefault("reactions", [])
        entry = next((r for r in reactions if r["name"] == emoji), None)
        if action == "added":
            if entry is None:
                reactions.append({"name": emoji, "users": [user], "count": 1})
            elif user not in entry["users"]:
                entry["users"].append(user)
                entry["count"] = len(entry["users"])
        elif action == "removed":
            if entry is not None and user in entry["users"]:
                entry["users"].remove(user)
                entry["count"] = len(entry["users"])
                if entry["count"] == 0:
                    reactions.remove(entry)
