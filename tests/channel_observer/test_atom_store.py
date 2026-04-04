"""AtomChannelStore 단위 테스트."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from seosoyoung_plugins.channel_observer.atom_store import AtomChannelStore


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def _mock_channel_store(monkeypatch):
    """ChannelStore를 Mock으로 교체하여 파일 I/O 없이 테스트."""
    mock_cls = MagicMock()
    monkeypatch.setattr(
        "seosoyoung_plugins.channel_observer.atom_store.ChannelStore", mock_cls
    )
    return mock_cls


def make_store(
    user_name_resolver=None,
    base_url="http://atom.test",
    api_key="test-key",
    slack_root="root-node-id",
    base_dir="/tmp/test-channel-store",
) -> AtomChannelStore:
    config = {
        "atom_base_url": base_url,
        "atom_api_key": api_key,
        "atom_slack_root_node_id": slack_root,
        "base_dir": base_dir,
    }
    if user_name_resolver is not None:
        config["user_name_resolver"] = user_name_resolver
    return AtomChannelStore(config)


def make_child(node_id: str, title: str) -> dict:
    return {"id": node_id, "card": {"title": title}}


# ============================================================================
# TestCreateStructureCard
# ============================================================================


class TestCreateStructureCard:
    """_create_structure_card 반환 타입 (node_id, card_id) 검증."""

    @pytest.mark.asyncio
    async def test_returns_node_id_and_card_id(self):
        store = make_store()
        mock_response = {"node_id": "n-001", "id": "c-001"}
        store._post_with_retry = AsyncMock(return_value=mock_response)

        node_id, card_id = await store._create_structure_card("title", "parent-node")

        assert node_id == "n-001"
        assert card_id == "c-001"

    @pytest.mark.asyncio
    async def test_card_id_falls_back_to_card_id_field(self):
        store = make_store()
        mock_response = {"node_id": "n-002", "card_id": "c-002"}
        store._post_with_retry = AsyncMock(return_value=mock_response)

        node_id, card_id = await store._create_structure_card("title", "parent-node")

        assert node_id == "n-002"
        assert card_id == "c-002"

    @pytest.mark.asyncio
    async def test_returns_none_none_on_failure(self):
        store = make_store()
        store._post_with_retry = AsyncMock(return_value=None)

        node_id, card_id = await store._create_structure_card("title", "parent-node")

        assert node_id is None
        assert card_id is None

    @pytest.mark.asyncio
    async def test_content_included_when_provided(self):
        store = make_store()
        store._post_with_retry = AsyncMock(return_value={"node_id": "n", "id": "c"})

        await store._create_structure_card("title", "parent", content="hello")

        call_body = store._post_with_retry.call_args[0][1]
        assert call_body["content"] == "hello"

    @pytest.mark.asyncio
    async def test_content_omitted_when_none(self):
        store = make_store()
        store._post_with_retry = AsyncMock(return_value={"node_id": "n", "id": "c"})

        await store._create_structure_card("title", "parent", content=None)

        call_body = store._post_with_retry.call_args[0][1]
        assert "content" not in call_body


# ============================================================================
# TestNodeCache — channel / date / thread node caching
# ============================================================================


class TestNodeCache:
    """노드 캐시 및 재사용 검증."""

    @pytest.mark.asyncio
    async def test_channel_node_created_once(self):
        store = make_store()
        store._list_children = AsyncMock(return_value=[])
        store._create_card = AsyncMock(return_value="ch-node-1")

        node1 = await store._get_or_create_channel_node("C123")
        node2 = await store._get_or_create_channel_node("C123")

        assert node1 == "ch-node-1"
        assert node2 == "ch-node-1"
        assert store._create_card.call_count == 1

    @pytest.mark.asyncio
    async def test_channel_node_reuses_existing_by_id_pattern(self):
        store = make_store()
        existing = make_child("ch-node-existing", "[#general](C123)")
        store._list_children = AsyncMock(return_value=[existing])
        store._create_card = AsyncMock()

        node = await store._get_or_create_channel_node("C123")

        assert node == "ch-node-existing"
        store._create_card.assert_not_called()

    @pytest.mark.asyncio
    async def test_channel_node_uses_display_name(self):
        store = make_store()
        store.set_channel_name("C123", "general")
        store._list_children = AsyncMock(return_value=[])
        store._create_card = AsyncMock(return_value="n")

        await store._get_or_create_channel_node("C123")

        call_title = store._create_card.call_args[0][0]
        assert call_title == "[#general](C123)"

    @pytest.mark.asyncio
    async def test_date_node_created_once(self):
        store = make_store()
        store._get_or_create_channel_node = AsyncMock(return_value="ch-node")
        store._list_children = AsyncMock(return_value=[])
        store._create_card = AsyncMock(return_value="date-node-1")

        n1 = await store._get_or_create_date_node("C123", "2026-04-04")
        n2 = await store._get_or_create_date_node("C123", "2026-04-04")

        assert n1 == "date-node-1"
        assert n2 == "date-node-1"
        assert store._create_card.call_count == 1

    @pytest.mark.asyncio
    async def test_thread_node_created_once(self):
        """반환 타입이 tuple[str|None, str|None]임을 검증."""
        store = make_store()
        store._get_or_create_date_node = AsyncMock(return_value="date-node")
        store._list_children = AsyncMock(return_value=[])
        store._create_structure_card = AsyncMock(return_value=("thread-node-1", "card-id-1"))

        result1 = await store._get_or_create_thread_node("C123", "1712345678.000000")
        result2 = await store._get_or_create_thread_node("C123", "1712345678.000000")

        # first call: new node → (node_id, card_id)
        assert result1 == ("thread-node-1", "card-id-1")
        # second call: cached → (node_id, None)
        assert result2 == ("thread-node-1", None)
        assert store._create_structure_card.call_count == 1

    @pytest.mark.asyncio
    async def test_thread_node_uses_get_date_key(self):
        """_get_or_create_date_node는 raw thread_ts가 아닌 date_key를 받아야 한다."""
        store = make_store()
        store._get_or_create_date_node = AsyncMock(return_value="date-node")
        store._list_children = AsyncMock(return_value=[])
        store._create_structure_card = AsyncMock(return_value=("tn", "ci"))

        ts = "1712345678.000000"
        expected_date_key = AtomChannelStore._get_date_key(float(ts))

        await store._get_or_create_thread_node("C123", ts)

        store._get_or_create_date_node.assert_called_once_with("C123", expected_date_key)

    @pytest.mark.asyncio
    async def test_thread_node_reuses_existing(self):
        """기존 스레드 노드가 있으면 (node_id, None) 반환."""
        store = make_store()
        existing = make_child("thread-node-existing", "1712345678.000000")
        store._get_or_create_date_node = AsyncMock(return_value="date-node")
        store._list_children = AsyncMock(return_value=[existing])
        store._create_structure_card = AsyncMock()

        node_id, card_id = await store._get_or_create_thread_node("C123", "1712345678.000000")

        assert node_id == "thread-node-existing"
        assert card_id is None
        store._create_structure_card.assert_not_called()


# ============================================================================
# TestWritePendingCard
# ============================================================================


class TestWritePendingCard:
    """_write_pending_card: pending_card_ids → node_id, pending_staleness_ids → card_id."""

    @pytest.mark.asyncio
    async def test_root_message_stores_node_id_and_card_id(self):
        store = make_store()
        store._get_or_create_thread_node = AsyncMock(
            return_value=("thread-node", "card-id-001")
        )
        store._format_message_content = AsyncMock(return_value="content")

        message = {"ts": "1712345678.000001", "thread_ts": "1712345678.000001", "user": "U1"}
        await store._write_pending_card("C123", message)

        ts = "1712345678.000001"
        assert store._pending_card_ids["C123"][ts] == "thread-node"
        assert store._pending_staleness_ids["C123"][ts] == "card-id-001"
        assert store._thread_card_ids["C123"]["1712345678.000001"][ts] == "card-id-001"

    @pytest.mark.asyncio
    async def test_reply_stores_node_id_and_card_id(self):
        store = make_store()
        store._get_or_create_thread_node = AsyncMock(return_value=("thread-node", None))
        store._create_structure_card = AsyncMock(return_value=("reply-node", "reply-card-id"))
        store._format_message_content = AsyncMock(return_value="content")

        message = {
            "ts": "1712345678.000002",
            "thread_ts": "1712345678.000001",
            "user": "U2",
        }
        await store._write_pending_card("C123", message)

        ts = "1712345678.000002"
        thread_ts = "1712345678.000001"
        assert store._pending_card_ids["C123"][ts] == "reply-node"
        assert store._pending_staleness_ids["C123"][ts] == "reply-card-id"
        assert store._thread_card_ids["C123"][thread_ts][ts] == "reply-card-id"

    @pytest.mark.asyncio
    async def test_root_message_no_card_id_skips_staleness(self):
        """thread_node 신규 아니면 card_id=None → staleness dict에 추가 안 됨."""
        store = make_store()
        store._get_or_create_thread_node = AsyncMock(
            return_value=("thread-node", None)  # existing node
        )
        store._format_message_content = AsyncMock(return_value="content")

        message = {"ts": "1712345678.000001", "thread_ts": "1712345678.000001"}
        await store._write_pending_card("C123", message)

        ts = "1712345678.000001"
        assert store._pending_card_ids["C123"][ts] == "thread-node"
        assert store._pending_staleness_ids.get("C123", {}).get(ts) is None


# ============================================================================
# TestMoveSnapshotToJudged
# ============================================================================


class TestMoveSnapshotToJudged:
    """staleness PATCH + file store 위임 검증."""

    def test_delegates_to_file_store_and_fires_staleness_patch(self):
        store = make_store()
        store._pending_card_ids.setdefault("C123", {})["ts-001"] = "node-001"
        store._pending_staleness_ids.setdefault("C123", {})["ts-001"] = "card-001"

        patched_calls = []

        async def fake_patch(card_id: str, staleness: str):
            patched_calls.append((card_id, staleness))

        store._patch_card_staleness = fake_patch
        fired = []

        def fake_fire(coro):
            fired.append(coro)

        store._fire_and_forget = fake_fire

        store.move_snapshot_to_judged("C123", {"ts-001"}, None)

        # file store delegation
        store._file_store.move_snapshot_to_judged.assert_called_once_with(
            "C123", {"ts-001"}, None
        )

        assert len(fired) >= 1
        # Run the coroutine synchronously to verify the patch call
        asyncio.run(fired[0])
        assert patched_calls == [("card-001", "judged")]

    def test_no_patch_when_staleness_id_missing(self):
        store = make_store()
        fired = []
        store._fire_and_forget = lambda c: fired.append(c)

        store.move_snapshot_to_judged("C123", {"ts-unknown"}, None)

        # file store should still be called
        store._file_store.move_snapshot_to_judged.assert_called_once()
        assert len(fired) == 0


# ============================================================================
# TestFormatMessageContent
# ============================================================================


class TestFormatMessageContent:
    """_format_message_content 기본 동작."""

    @pytest.mark.asyncio
    async def test_uses_display_name_from_resolver(self):
        async def resolver(user_id: str) -> str:
            return "서소영"

        store = make_store(user_name_resolver=resolver)

        message = {"user": "U123", "text": "안녕하세요"}
        content = await store._format_message_content(message)

        assert "**서소영**" in content
        assert "안녕하세요" in content

    @pytest.mark.asyncio
    async def test_falls_back_to_user_id_without_resolver(self):
        store = make_store()

        message = {"user": "U123", "text": "hello"}
        content = await store._format_message_content(message)

        assert "**U123**" in content

    @pytest.mark.asyncio
    async def test_parses_user_mention(self):
        store = make_store()

        message = {"user": "", "text": "<@UABC>님 안녕하세요"}
        content = await store._format_message_content(message)

        assert "[UABC](UABC)" in content

    @pytest.mark.asyncio
    async def test_includes_file_attachment(self):
        store = make_store()

        message = {
            "user": "",
            "text": "",
            "files": [
                {"name": "image.png", "filetype": "png", "permalink": "https://slack.com/files/img"}
            ],
        }
        content = await store._format_message_content(message)

        assert "[첨부: image.png (png)](https://slack.com/files/img)" in content

    @pytest.mark.asyncio
    async def test_includes_reactions(self):
        store = make_store()

        message = {
            "user": "",
            "text": "",
            "reactions": [{"name": "thumbsup", "count": 3, "users": ["U1", "U2", "U3"]}],
        }
        content = await store._format_message_content(message)

        assert ":thumbsup: 3" in content


# ============================================================================
# TestParseSlackMarkup
# ============================================================================


class TestParseSlackMarkup:
    def test_user_mention(self):
        result = AtomChannelStore._parse_slack_markup("<@UABC123>")
        assert result == "[UABC123](UABC123)"

    def test_channel_mention(self):
        result = AtomChannelStore._parse_slack_markup("<#C123|general>")
        assert result == "[#general](C123)"

    def test_link_with_label(self):
        result = AtomChannelStore._parse_slack_markup("<https://example.com|Example>")
        assert result == "[Example](https://example.com)"

    def test_special_here(self):
        result = AtomChannelStore._parse_slack_markup("<!here>")
        assert result == "@here"

    def test_special_channel(self):
        result = AtomChannelStore._parse_slack_markup("<!channel>")
        assert result == "@channel"

    def test_special_everyone(self):
        result = AtomChannelStore._parse_slack_markup("<!everyone>")
        assert result == "@everyone"


# ============================================================================
# TestResolveUserName (plugin.py 통합 - _resolve_user_name)
# ============================================================================


class TestResolveUserNameViaStore:
    """_resolve_user: UserInfo.display_name 속성 접근 검증."""

    @pytest.mark.asyncio
    async def test_returns_display_name(self):
        from seosoyoung.plugin_sdk.slack import UserInfo

        mock_info = UserInfo(id="U123", name="soy", real_name="서소영", display_name="소영")

        async def resolver(user_id: str) -> str:
            return mock_info.display_name or mock_info.real_name or user_id

        store = make_store(user_name_resolver=resolver)
        name = await store._resolve_user("U123")
        assert name == "소영"

    @pytest.mark.asyncio
    async def test_falls_back_to_real_name_when_display_name_empty(self):
        async def resolver(user_id: str) -> str:
            return "서소영"

        store = make_store(user_name_resolver=resolver)
        name = await store._resolve_user("U123")
        assert name == "서소영"

    @pytest.mark.asyncio
    async def test_falls_back_to_user_id_when_no_resolver(self):
        store = make_store()
        name = await store._resolve_user("U999")
        assert name == "U999"


# ============================================================================
# TestWriteInterpretation
# ============================================================================


class TestWriteInterpretation:
    """write_interpretation: _pending_card_ids에서 node_id를 찾아 knowledge 카드 생성."""

    def test_skips_when_no_node_id(self, caplog):
        store = make_store()
        fired = []
        store._fire_and_forget = lambda c: fired.append(c)

        store.write_interpretation("C123", "ts-unknown", 0, "content")

        assert len(fired) == 0

    def test_fires_when_node_id_exists(self):
        store = make_store()
        store._pending_card_ids.setdefault("C123", {})["ts-001"] = "message-node"

        fired = []
        store._fire_and_forget = lambda c: fired.append(c)

        store.write_interpretation("C123", "ts-001", 0, "해석 내용")

        assert len(fired) == 1

    @pytest.mark.asyncio
    async def test_interpretation_card_title_default(self):
        store = make_store()
        store._create_knowledge_card = AsyncMock(return_value="interp-node")

        await store._write_interpretation_card("msg-node", 0, "content")

        store._create_knowledge_card.assert_called_once()
        call_kwargs = store._create_knowledge_card.call_args
        title = call_kwargs[1].get("title") or call_kwargs[0][0]
        assert title == "첨부 해석"

    @pytest.mark.asyncio
    async def test_interpretation_card_title_nth(self):
        store = make_store()
        store._create_knowledge_card = AsyncMock(return_value="interp-node")

        await store._write_interpretation_card("msg-node", 2, "content")

        call_kwargs = store._create_knowledge_card.call_args
        title = call_kwargs[1].get("title") or call_kwargs[0][0]
        assert title == "2차 해석"

    @pytest.mark.asyncio
    async def test_interpretation_card_staleness_judged(self):
        store = make_store()
        store._create_knowledge_card = AsyncMock(return_value="interp-node")

        await store._write_interpretation_card("msg-node", 0, "content")

        call_kwargs = store._create_knowledge_card.call_args
        staleness = call_kwargs[1].get("staleness") or call_kwargs[0][-1]
        assert staleness == "judged"


# ============================================================================
# TestDualWrite — append/upsert 이중 기록 검증
# ============================================================================


class TestDualWrite:
    """메시지 쓰기 메서드가 file store + atom 양쪽 모두에 기록하는지 검증."""

    def test_append_channel_message_dual_writes(self):
        store = make_store()
        fired = []
        store._fire_and_forget = lambda c: fired.append(c)

        msg = {"ts": "1.0", "user": "U1", "text": "hello"}
        store.append_channel_message("C123", msg)

        store._file_store.append_channel_message.assert_called_once_with("C123", msg)
        assert len(fired) == 1

    def test_upsert_pending_dual_writes(self):
        store = make_store()
        fired = []
        store._fire_and_forget = lambda c: fired.append(c)

        msg = {"ts": "1.0", "user": "U1", "text": "edited"}
        store.upsert_pending("C123", msg)

        store._file_store.upsert_pending.assert_called_once_with("C123", msg)
        assert len(fired) == 1

    def test_append_thread_message_dual_writes(self):
        store = make_store()
        fired = []
        store._fire_and_forget = lambda c: fired.append(c)

        msg = {"ts": "2.0", "thread_ts": "1.0", "user": "U1", "text": "reply"}
        store.append_thread_message("C123", "1.0", msg)

        store._file_store.append_thread_message.assert_called_once_with(
            "C123", "1.0", msg
        )
        assert len(fired) == 1

    def test_upsert_thread_message_dual_writes(self):
        store = make_store()
        fired = []
        store._fire_and_forget = lambda c: fired.append(c)

        msg = {"ts": "2.0", "thread_ts": "1.0", "user": "U1", "text": "edit reply"}
        store.upsert_thread_message("C123", "1.0", msg)

        store._file_store.upsert_thread_message.assert_called_once_with(
            "C123", "1.0", msg
        )
        assert len(fired) == 1


# ============================================================================
# TestPipelineDelegation — 파이프라인 메서드 위임 검증
# ============================================================================


class TestPipelineDelegation:
    """파이프라인 버퍼 메서드가 내부 _file_store로 위임되는지 검증."""

    def test_load_pending_delegates(self):
        store = make_store()
        store._file_store.load_pending.return_value = [{"ts": "1"}]
        result = store.load_pending("C123")
        store._file_store.load_pending.assert_called_once_with("C123")
        assert result == [{"ts": "1"}]

    def test_load_judged_delegates(self):
        store = make_store()
        store._file_store.load_judged.return_value = []
        result = store.load_judged("C123")
        store._file_store.load_judged.assert_called_once_with("C123")
        assert result == []

    def test_count_pending_tokens_delegates(self):
        store = make_store()
        store._file_store.count_pending_tokens.return_value = 42
        result = store.count_pending_tokens("C123")
        store._file_store.count_pending_tokens.assert_called_once_with("C123")
        assert result == 42

    def test_count_judged_plus_pending_tokens_delegates(self):
        store = make_store()
        store._file_store.count_judged_plus_pending_tokens.return_value = 100
        result = store.count_judged_plus_pending_tokens("C123")
        assert result == 100

    def test_clear_judged_delegates(self):
        store = make_store()
        store.clear_judged("C123")
        store._file_store.clear_judged.assert_called_once_with("C123")

    def test_append_judged_delegates(self):
        store = make_store()
        msgs = [{"ts": "1"}, {"ts": "2"}]
        store.append_judged("C123", msgs)
        store._file_store.append_judged.assert_called_once_with("C123", msgs)

    def test_get_digest_delegates(self):
        store = make_store()
        store._file_store.get_digest.return_value = {"content": "digest"}
        result = store.get_digest("C123")
        assert result == {"content": "digest"}

    def test_save_digest_delegates(self):
        store = make_store()
        store.save_digest("C123", "content", {"key": "val"})
        store._file_store.save_digest.assert_called_once_with(
            "C123", "content", {"key": "val"}
        )

    def test_update_reactions_delegates(self):
        store = make_store()
        store.update_reactions("C123", ts="1.0", emoji="thumbsup", user="U1", action="added")
        store._file_store.update_reactions.assert_called_once_with(
            "C123", ts="1.0", emoji="thumbsup", user="U1", action="added"
        )


# ============================================================================
# TestCompileChannelContext — atom compile API 검증
# ============================================================================


class TestCompileChannelContext:
    """compile_channel_context: atom HTTP API 호출 검증."""

    @pytest.mark.asyncio
    async def test_returns_markdown_from_api(self):
        store = make_store()
        store._channel_nodes["C123"] = "ch-node-id"
        store._get_with_retry = AsyncMock(
            return_value={"markdown": "# Channel Context\nsome content"}
        )

        result = await store.compile_channel_context("C123")

        store._get_with_retry.assert_called_once_with(
            "/api/tree/ch-node-id/compile", params=None
        )
        assert result == "# Channel Context\nsome content"

    @pytest.mark.asyncio
    async def test_passes_limit_param(self):
        store = make_store()
        store._channel_nodes["C123"] = "ch-node-id"
        store._get_with_retry = AsyncMock(return_value={"markdown": "limited"})

        result = await store.compile_channel_context("C123", limit=20)

        store._get_with_retry.assert_called_once_with(
            "/api/tree/ch-node-id/compile", params={"limit": 20}
        )
        assert result == "limited"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_channel_node(self):
        store = make_store()
        result = await store.compile_channel_context("C123")
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_when_api_fails(self):
        store = make_store()
        store._channel_nodes["C123"] = "ch-node-id"
        store._get_with_retry = AsyncMock(return_value=None)

        result = await store.compile_channel_context("C123")
        assert result == ""


# ============================================================================
# TestBaseDirRequired — base_dir 누락 시 ValueError 검증
# ============================================================================


class TestBaseDirRequired:
    """config에 base_dir 없으면 ValueError 발생."""

    def test_raises_without_base_dir(self, _mock_channel_store):
        config = {
            "atom_base_url": "http://atom.test",
            "atom_api_key": "test-key",
            "atom_slack_root_node_id": "root-node-id",
        }
        with pytest.raises(ValueError, match="base_dir"):
            AtomChannelStore(config)
