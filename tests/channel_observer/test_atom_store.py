"""AtomChannelStore 단위 테스트.

테스트 범위:
1. 날짜 경계: 새벽 3:59 KST → 전날 날짜, 04:00 KST → 당일 날짜
2. 노드 캐시: 같은 channel_id로 두 번 호출 시 HTTP POST 1회만 발생
3. retry 로직: HTTP 실패 시 3회 재시도, 3회 모두 실패 시 경고 후 드롭
4. append_pending + load_pending: in-memory buffer 동작
5. move_snapshot_to_judged: pending 제거 + staleness 업데이트 fire-and-forget
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from seosoyoung_plugins.channel_observer.atom_store import AtomChannelStore

SAMPLE_CONFIG = {
    "atom_base_url": "http://localhost:3202",
    "atom_api_key": "test-key",
    "atom_slack_root_node_id": "root-node-id",
}


def make_store() -> AtomChannelStore:
    return AtomChannelStore(config=SAMPLE_CONFIG)


# ── 날짜 경계 ─────────────────────────────────────────────────────────────


class TestDateKey:
    """_get_date_key: KST 새벽 4시 기준으로 날짜가 바뀐다."""

    def test_before_4am_returns_previous_day(self):
        # 2024-01-15 03:59:59 KST → 이전 날 2024-01-14
        # KST offset = +9h → UTC 2024-01-14 18:59:59
        from zoneinfo import ZoneInfo
        kst = datetime(2024, 1, 15, 3, 59, 59, tzinfo=ZoneInfo("Asia/Seoul"))
        ts = kst.timestamp()
        result = AtomChannelStore._get_date_key(ts)
        assert result == "2024-01-14"

    def test_at_4am_returns_current_day(self):
        # 2024-01-15 04:00:00 KST → 당일 2024-01-15
        from zoneinfo import ZoneInfo
        kst = datetime(2024, 1, 15, 4, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        ts = kst.timestamp()
        result = AtomChannelStore._get_date_key(ts)
        assert result == "2024-01-15"

    def test_midnight_returns_previous_day(self):
        # 2024-01-15 00:00:00 KST → 이전 날 2024-01-14
        from zoneinfo import ZoneInfo
        kst = datetime(2024, 1, 15, 0, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        ts = kst.timestamp()
        result = AtomChannelStore._get_date_key(ts)
        assert result == "2024-01-14"

    def test_noon_returns_current_day(self):
        # 2024-01-15 12:00:00 KST → 당일 2024-01-15
        from zoneinfo import ZoneInfo
        kst = datetime(2024, 1, 15, 12, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        ts = kst.timestamp()
        result = AtomChannelStore._get_date_key(ts)
        assert result == "2024-01-15"


# ── 노드 캐시 ────────────────────────────────────────────────────────────


class TestNodeCache:
    """같은 channel_id로 두 번 호출 시 HTTP POST는 1회만 발생한다."""

    @pytest.mark.asyncio
    async def test_channel_node_created_once(self):
        store = make_store()
        post_result = {"node_id": "channel-node-1"}
        mock_post = AsyncMock(return_value=post_result)

        with patch.object(store, "_post_with_retry", new=mock_post):
            # 첫 호출
            node1 = await store._get_or_create_channel_node("C123")
            # 두 번째 호출
            node2 = await store._get_or_create_channel_node("C123")

        assert node1 == "channel-node-1"
        assert node2 == "channel-node-1"
        # POST는 1회만 호출
        mock_post.assert_called_once()

    @pytest.mark.asyncio
    async def test_thread_node_created_once(self):
        store = make_store()
        call_count = 0

        async def fake_post(path, body):
            nonlocal call_count
            call_count += 1
            return {"node_id": f"node-{call_count}"}

        with patch.object(store, "_post_with_retry", new=fake_post):
            thread_ts = "1700000000.000000"
            node1 = await store._get_or_create_thread_node("C123", thread_ts)
            node2 = await store._get_or_create_thread_node("C123", thread_ts)

        assert node1 == node2
        # channel + date + thread = 3회 POST, 두 번째 thread 호출은 캐시에서 처리
        assert call_count == 3  # channel, date, thread 각 1회


# ── retry 로직 ──────────────────────────────────────────────────────────


class TestRetryLogic:
    """HTTP 실패 시 3회 재시도, 3회 모두 실패 시 None 반환 + 경고 로그."""

    @pytest.mark.asyncio
    async def test_post_retries_on_failure_and_returns_none(self):
        store = make_store()
        call_count = 0

        async def failing_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise Exception("connection refused")

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=Exception("connection refused"))
            mock_client_cls.return_value = mock_client

            with patch("asyncio.sleep", new=AsyncMock()):
                result = await store._post_with_retry("/api/chat/cards", {"title": "test"})

        assert result is None
        assert mock_client.post.call_count == 3

    @pytest.mark.asyncio
    async def test_post_succeeds_on_second_attempt(self):
        store = make_store()
        attempt = 0

        async def flaky_post(*args, **kwargs):
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise Exception("temporary failure")
            mock_resp = MagicMock()
            mock_resp.status_code = 201
            mock_resp.json.return_value = {"node_id": "new-node", "id": "card-1"}
            return mock_resp

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=flaky_post)
            mock_client_cls.return_value = mock_client

            with patch("asyncio.sleep", new=AsyncMock()):
                result = await store._post_with_retry("/api/chat/cards", {"title": "test"})

        assert result is not None
        assert result["node_id"] == "new-node"
        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_post_returns_none_on_http_error_status(self):
        store = make_store()

        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            with patch("asyncio.sleep", new=AsyncMock()):
                result = await store._post_with_retry("/api/chat/cards", {"title": "test"})

        assert result is None
        assert mock_client.post.call_count == 3


# ── append_pending + load_pending ───────────────────────────────────────


class TestPendingBuffer:
    """in-memory pending buffer 동작."""

    def test_append_and_load_pending(self):
        store = make_store()
        store._fire_and_forget = MagicMock()  # background write 억제

        msg = {"ts": "1700000001.000001", "text": "hello", "user": "U123"}
        store.append_pending("C123", msg)

        result = store.load_pending("C123")
        assert len(result) == 1
        assert result[0]["ts"] == "1700000001.000001"

    def test_load_pending_empty_channel(self):
        store = make_store()
        result = store.load_pending("C_NONEXISTENT")
        assert result == []

    def test_upsert_replaces_existing_message(self):
        store = make_store()
        store._fire_and_forget = MagicMock()

        msg1 = {"ts": "1700000001.000001", "text": "original"}
        msg2 = {"ts": "1700000001.000001", "text": "updated"}
        store.append_pending("C123", msg1)
        store.upsert_pending("C123", msg2)

        result = store.load_pending("C123")
        assert len(result) == 1
        assert result[0]["text"] == "updated"

    def test_fire_and_forget_called_on_append(self):
        store = make_store()
        store._fire_and_forget = MagicMock()

        msg = {"ts": "1700000001.000001", "text": "hello"}
        store.append_pending("C123", msg)

        store._fire_and_forget.assert_called_once()

    def test_append_channel_message_alias(self):
        store = make_store()
        store._fire_and_forget = MagicMock()

        msg = {"ts": "1700000001.000002", "text": "via alias"}
        store.append_channel_message("C123", msg)

        result = store.load_pending("C123")
        assert len(result) == 1


# ── move_snapshot_to_judged ─────────────────────────────────────────────


class TestMoveSnapshotToJudged:
    """pending 제거 + staleness 업데이트 fire-and-forget."""

    def test_moves_snapshot_messages_to_judged(self):
        store = make_store()
        store._fire_and_forget = MagicMock()

        msg1 = {"ts": "1700000001.000001", "text": "msg1"}
        msg2 = {"ts": "1700000001.000002", "text": "msg2"}
        msg3 = {"ts": "1700000001.000003", "text": "msg3"}

        store.append_pending("C123", msg1)
        store.append_pending("C123", msg2)
        store.append_pending("C123", msg3)

        snapshot_ts = {"1700000001.000001", "1700000001.000002"}
        store.move_snapshot_to_judged("C123", snapshot_ts)

        # snapshot_ts 메시지는 pending에서 제거
        remaining = store.load_pending("C123")
        remaining_ts = {m["ts"] for m in remaining}
        assert remaining_ts == {"1700000001.000003"}

        # judged에 추가됨
        judged = store.load_judged("C123")
        judged_ts = {m["ts"] for m in judged}
        assert judged_ts == {"1700000001.000001", "1700000001.000002"}

    def test_fires_staleness_patch_for_known_card_ids(self):
        store = make_store()
        fire_calls = []

        def capture_fire(coro):
            fire_calls.append(coro)

        store._fire_and_forget = capture_fire

        msg = {"ts": "1700000001.000001", "text": "msg"}
        store.append_pending("C123", msg)
        # 카드 ID 수동 주입
        store._pending_card_ids["C123"] = {"1700000001.000001": "card-id-1"}

        store.move_snapshot_to_judged("C123", {"1700000001.000001"})

        # fire-and-forget이 호출됨: append_pending 1회 + staleness patch 1회
        assert len(fire_calls) >= 1

    def test_thread_snapshot_moves_to_judged(self):
        store = make_store()
        store._fire_and_forget = MagicMock()

        thread_ts = "1700000000.000000"
        msg = {"ts": "1700000001.000001", "text": "thread reply"}
        store.append_thread_message("C123", thread_ts, msg)

        store.move_snapshot_to_judged(
            "C123",
            snapshot_ts=set(),
            snapshot_thread_ts={thread_ts},
        )

        # 스레드 버퍼에서 제거
        buffers = store.load_all_thread_buffers("C123")
        assert thread_ts not in buffers

        # judged에 추가
        judged = store.load_judged("C123")
        assert len(judged) == 1
        assert judged[0]["ts"] == "1700000001.000001"

    def test_nonexistent_snapshot_ts_does_not_crash(self):
        store = make_store()
        store._fire_and_forget = MagicMock()

        # pending에 없는 ts로 호출해도 에러 없음
        store.move_snapshot_to_judged("C123", {"non-existent-ts"})

        assert store.load_judged("C123") == []


# ── reactions ────────────────────────────────────────────────────────────


class TestUpdateReactions:
    """in-memory reactions 업데이트."""

    def test_add_reaction_to_pending_message(self):
        store = make_store()
        store._fire_and_forget = MagicMock()

        msg = {"ts": "1700000001.000001", "text": "hello"}
        store.append_pending("C123", msg)

        store.update_reactions("C123", ts="1700000001.000001", emoji="thumbsup", user="U1", action="added")

        msgs = store.load_pending("C123")
        reactions = msgs[0].get("reactions", [])
        assert any(r["name"] == "thumbsup" and "U1" in r["users"] for r in reactions)

    def test_remove_reaction(self):
        store = make_store()
        store._fire_and_forget = MagicMock()

        msg = {"ts": "1700000001.000001", "text": "hello", "reactions": [
            {"name": "thumbsup", "users": ["U1"], "count": 1}
        ]}
        store._pending["C123"] = {"1700000001.000001": msg}

        store.update_reactions("C123", ts="1700000001.000001", emoji="thumbsup", user="U1", action="removed")

        msgs = store.load_pending("C123")
        reactions = msgs[0].get("reactions", [])
        # count=0이면 제거됨
        assert not any(r["name"] == "thumbsup" for r in reactions)


# ── judged 버퍼 ──────────────────────────────────────────────────────────


class TestJudgedBuffer:
    def test_append_and_load_judged(self):
        store = make_store()
        msgs = [{"ts": "1.1", "text": "a"}, {"ts": "1.2", "text": "b"}]
        store.append_judged("C123", msgs)

        result = store.load_judged("C123")
        assert len(result) == 2

    def test_clear_judged(self):
        store = make_store()
        store.append_judged("C123", [{"ts": "1.1", "text": "a"}])
        store.clear_judged("C123")

        result = store.load_judged("C123")
        assert result == []


# ── compile_channel_context ──────────────────────────────────────────────


class TestCompileChannelContext:
    @pytest.mark.asyncio
    async def test_returns_empty_string_when_no_channel_node(self):
        store = make_store()
        result = await store.compile_channel_context("C_NO_NODE")
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_markdown_from_api(self):
        store = make_store()
        store._channel_nodes["C123"] = "channel-node-1"
        expected_md = "# 채널 대화\n\n내용입니다."

        with patch.object(
            store, "_get_with_retry",
            new=AsyncMock(return_value={"markdown": expected_md})
        ):
            result = await store.compile_channel_context("C123", limit=10)

        assert result == expected_md

    @pytest.mark.asyncio
    async def test_returns_empty_string_on_api_failure(self):
        store = make_store()
        store._channel_nodes["C123"] = "channel-node-1"

        with patch.object(
            store, "_get_with_retry",
            new=AsyncMock(return_value=None)
        ):
            result = await store.compile_channel_context("C123")

        assert result == ""
