"""Atom knowledge store for channel observer.

Stores Slack channel messages as a structured tree in the Atom knowledge base:
  slack_root → channel nodes → date nodes → thread nodes → reply nodes

Design constraints:
- _fire_and_forget uses threading.Thread(asyncio.run(...)) — no asyncio.Lock sharing
- Deduplication via in-memory dict checks + atom-level idempotency
- No asyncio.Lock — dict in-memory check is the guard
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from datetime import datetime, timezone
from typing import Awaitable, Callable

import aiohttp

logger = logging.getLogger(__name__)


class AtomChannelStore:
    """Stores Slack messages as structured cards in the Atom knowledge tree.

    Tree structure:
      slack_root_node
        └── [#channel-name](channel_id)  (channel node)
              └── YYYY-MM-DD              (date node)
                    └── {thread_ts}       (thread/root-message node)
                          └── {reply_ts}  (reply node)

    Usage:
        store = AtomChannelStore(config)
        store.append_thread_message(channel_id, thread_ts, message)
        store.move_snapshot_to_judged(channel_id, ts, staleness)
        store.write_interpretation(channel_id, ts, order, content)
    """

    def __init__(self, config: dict) -> None:
        self._base_url: str = config["atom_base_url"].rstrip("/")
        self._api_key: str = config["atom_api_key"]
        self._slack_root_node_id: str = config["atom_slack_root_node_id"]

        # Callable[[user_id: str], Awaitable[str]] | None
        self._user_name_resolver: Callable[[str], Awaitable[str]] | None = config.get(
            "user_name_resolver"
        )

        # In-memory caches (dict key → node_id / nested dicts)
        self._channel_nodes: dict[str, str] = {}               # {ch_id: node_id}
        self._channel_names: dict[str, str] = {}               # {ch_id: display_name}
        self._date_nodes: dict[str, dict[str, str]] = {}        # {ch_id: {date_key: node_id}}
        self._thread_nodes: dict[str, dict[str, str]] = {}      # {ch_id: {thread_ts: node_id}}

        # card-id caches (DB record IDs — used for PATCH staleness)
        self._pending_card_ids: dict[str, dict[str, str]] = {}  # {ch: {ts: node_id}}
        self._pending_staleness_ids: dict[str, dict[str, str]] = {}  # {ch: {ts: card_id}}
        self._thread_card_ids: dict[str, dict[str, dict[str, str]]] = {}  # {ch: {tts: {ts: card_id}}}

        # HTTP session (created lazily per-thread via asyncio.run)
        self._headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    # =========================================================================
    # HTTP helpers
    # =========================================================================

    async def _get_with_retry(
        self, path: str, params: dict | None = None
    ) -> dict | list | None:
        url = f"{self._base_url}{path}"
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession(headers=self._headers) as session:
                    async with session.get(url, params=params) as resp:
                        if resp.status == 200:
                            return await resp.json()
                        if resp.status == 404:
                            return None
                        logger.warning("GET %s → %d (attempt %d)", path, resp.status, attempt + 1)
            except Exception as exc:
                logger.warning("GET %s error (attempt %d): %s", path, attempt + 1, exc)
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
        return None

    async def _post_with_retry(self, path: str, body: dict) -> dict | None:
        url = f"{self._base_url}{path}"
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession(headers=self._headers) as session:
                    async with session.post(url, json=body) as resp:
                        if resp.status in (200, 201):
                            return await resp.json()
                        logger.warning("POST %s → %d (attempt %d)", path, resp.status, attempt + 1)
            except Exception as exc:
                logger.warning("POST %s error (attempt %d): %s", path, attempt + 1, exc)
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
        return None

    async def _patch_with_retry(self, path: str, body: dict) -> dict | None:
        url = f"{self._base_url}{path}"
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession(headers=self._headers) as session:
                    async with session.patch(url, json=body) as resp:
                        if resp.status in (200, 201):
                            return await resp.json()
                        logger.warning("PATCH %s → %d (attempt %d)", path, resp.status, attempt + 1)
            except Exception as exc:
                logger.warning("PATCH %s error (attempt %d): %s", path, attempt + 1, exc)
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
        return None

    def _fire_and_forget(self, coro: Awaitable) -> None:
        """Run coroutine in a daemon thread with its own event loop.

        asyncio.Lock cannot be shared across event loops created by asyncio.run().
        All deduplication must use dict in-memory checks only.
        """
        def _run():
            try:
                asyncio.run(coro)
            except Exception as exc:
                logger.error("fire_and_forget error: %s", exc)

        threading.Thread(target=_run, daemon=True).start()

    # =========================================================================
    # Card creation helpers
    # =========================================================================

    async def _create_card(self, title: str, parent_node_id: str) -> str | None:
        """Create a structure card and return node_id only."""
        body = {
            "card_type": "structure",
            "title": title,
            "parent_node_id": parent_node_id,
        }
        result = await self._post_with_retry("/api/cards", body)
        if result and isinstance(result, dict):
            return result.get("node_id")
        return None

    async def _create_structure_card(
        self, title: str, parent_node_id: str, content: str | None = None
    ) -> tuple[str | None, str | None]:
        """Create a structure card and return (node_id, card_id)."""
        body: dict = {
            "card_type": "structure",
            "title": title,
            "parent_node_id": parent_node_id,
        }
        if content is not None:
            body["content"] = content
        result = await self._post_with_retry("/api/cards", body)
        if result and isinstance(result, dict):
            node_id = result.get("node_id")
            card_id = result.get("id") or result.get("card_id")
            return node_id, card_id
        return None, None

    async def _create_knowledge_card(
        self,
        title: str,
        parent_node_id: str,
        content: str,
        staleness: str = "pending",
    ) -> str | None:
        """Create a knowledge card and return node_id."""
        body = {
            "card_type": "knowledge",
            "title": title,
            "parent_node_id": parent_node_id,
            "content": content,
            "staleness": staleness,
        }
        result = await self._post_with_retry("/api/cards", body)
        if result and isinstance(result, dict):
            return result.get("node_id")
        return None

    # =========================================================================
    # Tree navigation helpers
    # =========================================================================

    async def _list_children(self, parent_node_id: str) -> list[dict]:
        result = await self._get_with_retry(f"/api/tree/{parent_node_id}/children")
        return result if isinstance(result, list) else []

    async def _get_or_create_channel_node(self, channel_id: str) -> str | None:
        if channel_id in self._channel_nodes:
            return self._channel_nodes[channel_id]
        children = await self._list_children(self._slack_root_node_id)
        for child in children:
            if f"]({channel_id})" in child.get("card", {}).get("title", ""):
                node_id = child["id"]
                self._channel_nodes[channel_id] = node_id
                return node_id
        display_name = self._channel_names.get(channel_id, channel_id)
        node_id = await self._create_card(
            f"[#{display_name}]({channel_id})", self._slack_root_node_id
        )
        if node_id:
            self._channel_nodes[channel_id] = node_id
        return node_id

    async def _get_or_create_date_node(self, channel_id: str, date_key: str) -> str | None:
        cache = self._date_nodes.setdefault(channel_id, {})
        if date_key in cache:
            return cache[date_key]
        channel_node = await self._get_or_create_channel_node(channel_id)
        if not channel_node:
            return None
        children = await self._list_children(channel_node)
        for child in children:
            if child.get("card", {}).get("title") == date_key:
                node_id = child["id"]
                cache[date_key] = node_id
                return node_id
        node_id = await self._create_card(date_key, channel_node)
        if node_id:
            cache[date_key] = node_id
        return node_id

    async def _get_or_create_thread_node(
        self, channel_id: str, thread_ts: str, content: str | None = None
    ) -> tuple[str | None, str | None]:
        """Return (node_id, card_id).

        Existing node: (node_id, None).
        New node: (node_id, card_id) — card_id is the DB record ID for staleness tracking.
        content is only set on initial creation; not updated for existing nodes.
        """
        cached = (self._thread_nodes.get(channel_id) or {}).get(thread_ts)
        if cached:
            return cached, None
        date_key = self._get_date_key(float(thread_ts))
        date_node = await self._get_or_create_date_node(channel_id, date_key)
        if not date_node:
            return None, None
        children = await self._list_children(date_node)
        for child in children:
            if child.get("card", {}).get("title") == thread_ts:
                node_id = child["id"]
                self._thread_nodes.setdefault(channel_id, {})[thread_ts] = node_id
                return node_id, None
        node_id, card_id = await self._create_structure_card(thread_ts, date_node, content)
        if node_id:
            self._thread_nodes.setdefault(channel_id, {})[thread_ts] = node_id
        return node_id, card_id

    # =========================================================================
    # Date key helper
    # =========================================================================

    @staticmethod
    def _get_date_key(ts: float) -> str:
        """Convert Unix timestamp to YYYY-MM-DD string (UTC)."""
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    # =========================================================================
    # Channel name cache
    # =========================================================================

    def set_channel_name(self, channel_id: str, display_name: str) -> None:
        self._channel_names[channel_id] = display_name

    # =========================================================================
    # User resolution
    # =========================================================================

    async def _resolve_user(self, user_id: str) -> str:
        if self._user_name_resolver:
            return await self._user_name_resolver(user_id)
        return user_id

    # =========================================================================
    # Message formatting helpers
    # =========================================================================

    @staticmethod
    def _parse_slack_markup(text: str) -> str:
        """Convert Slack mrkdwn markup to readable text.

        Patterns handled:
          <@U...>        → [user_id](user_id)
          <#C...|name>   → [#name](C...)
          <URL|text>     → [text](URL)
          <!here>        → @here
          <!channel>     → @channel
          <!everyone>    → @everyone
        """
        # user mention: <@UABC123>
        text = re.sub(r"<@(U[A-Z0-9]+)>", lambda m: f"[{m.group(1)}]({m.group(1)})", text)
        # channel mention: <#C123|name>
        text = re.sub(
            r"<#(C[A-Z0-9]+)\|([^>]+)>",
            lambda m: f"[#{m.group(2)}]({m.group(1)})",
            text,
        )
        # special mentions
        text = re.sub(r"<!here>", "@here", text)
        text = re.sub(r"<!channel>", "@channel", text)
        text = re.sub(r"<!everyone>", "@everyone", text)
        # link with label: <URL|text>
        text = re.sub(r"<([^|>]+)\|([^>]+)>", lambda m: f"[{m.group(2)}]({m.group(1)})", text)
        # bare link: <URL>
        text = re.sub(r"<([^>]+)>", lambda m: m.group(1), text)
        return text

    @staticmethod
    def _extract_rich_text(elements: list[dict]) -> str:
        """Extract text from rich_text_section elements."""
        parts = []
        for elem in elements:
            t = elem.get("type", "")
            if t == "text":
                parts.append(elem.get("text", ""))
            elif t == "link":
                url = elem.get("url", "")
                label = elem.get("text", url)
                parts.append(f"[{label}]({url})" if label != url else url)
            elif t == "user":
                uid = elem.get("user_id", "")
                parts.append(f"[{uid}]({uid})")
            elif t == "channel":
                cid = elem.get("channel_id", "")
                parts.append(f"[#{cid}]({cid})")
            elif t == "emoji":
                parts.append(f":{elem.get('name', '')}:")
            else:
                parts.append(elem.get("text", ""))
        return "".join(parts)

    def _parse_blocks(self, blocks: list[dict]) -> str:
        """Parse Slack Block Kit blocks to text. Only rich_text blocks."""
        parts = []
        for block in blocks:
            if block.get("type") != "rich_text":
                continue
            for element in block.get("elements", []):
                if element.get("type") == "rich_text_section":
                    text = self._extract_rich_text(element.get("elements", []))
                    if text:
                        parts.append(text)
        return "\n".join(parts)

    async def _format_message_content(self, message: dict) -> str:
        """Format a Slack message dict into a human-readable string."""
        parts = []

        user_id = message.get("user", "")
        if user_id:
            name = await self._resolve_user(user_id)
            parts.append(f"**{name}**")

        text = message.get("text", "")
        if text:
            parts.append(self._parse_slack_markup(text))

        blocks = message.get("blocks", [])
        if blocks:
            parsed = self._parse_blocks(blocks)
            if parsed:
                parts.append(parsed)

        files = message.get("files", [])
        for f in files:
            name = f.get("name", "")
            filetype = f.get("filetype", "")
            permalink = f.get("permalink", "")
            if permalink:
                parts.append(f"[첨부: {name} ({filetype})]({permalink})")
            else:
                parts.append(f"[첨부: {name} ({filetype})]")

        reactions = message.get("reactions", [])
        for r in reactions:
            emoji_name = r.get("name", "")
            count = r.get("count", 0)
            users = r.get("users", [])
            user_str = ", ".join(users[:3])
            parts.append(f":{emoji_name}: {count} ({user_str})")

        return "\n".join(parts)

    # =========================================================================
    # Message write helpers
    # =========================================================================

    async def _write_pending_card(self, channel_id: str, message: dict) -> None:
        """Write a pending (unjudged) message card to the atom tree.

        Root message (ts == thread_ts): stored as the thread node itself.
        Reply (ts != thread_ts): stored as a child of the thread node.

        _pending_card_ids[ch][ts]      → node_id  (parent for write_interpretation)
        _pending_staleness_ids[ch][ts] → card_id  (DB record for staleness PATCH)
        """
        ts = message.get("ts", "")
        thread_ts = message.get("thread_ts", ts) or ts
        content = await self._format_message_content(message)

        if ts == thread_ts:
            # Root message — the thread node IS the message card
            thread_node, thread_card_id = await self._get_or_create_thread_node(
                channel_id, thread_ts, content
            )
            if thread_node:
                self._pending_card_ids.setdefault(channel_id, {})[ts] = thread_node
                if thread_card_id:
                    self._pending_staleness_ids.setdefault(channel_id, {})[ts] = thread_card_id
                    self._thread_card_ids.setdefault(channel_id, {}).setdefault(thread_ts, {})[ts] = thread_card_id
        else:
            # Reply — child of the thread node
            thread_node, _ = await self._get_or_create_thread_node(channel_id, thread_ts)
            if not thread_node:
                return
            node_id, card_id = await self._create_structure_card(ts, thread_node, content)
            if node_id:
                self._pending_card_ids.setdefault(channel_id, {})[ts] = node_id
                if card_id:
                    self._pending_staleness_ids.setdefault(channel_id, {})[ts] = card_id
                    self._thread_card_ids.setdefault(channel_id, {}).setdefault(thread_ts, {})[ts] = card_id

    async def _patch_card_staleness(self, card_id: str, staleness: str) -> None:
        await self._patch_with_retry(f"/api/cards/{card_id}", {"staleness": staleness})

    # =========================================================================
    # Public API
    # =========================================================================

    def append_thread_message(
        self, channel_id: str, thread_ts: str, message: dict
    ) -> None:
        """Fire-and-forget: write a new thread message card to atom."""
        msg = message if "thread_ts" in message else {**message, "thread_ts": thread_ts}
        self._fire_and_forget(self._write_pending_card(channel_id, msg))

    def upsert_thread_message(
        self, channel_id: str, thread_ts: str, message: dict
    ) -> None:
        """Fire-and-forget: create or update a thread message card in atom."""
        msg = message if "thread_ts" in message else {**message, "thread_ts": thread_ts}
        self._fire_and_forget(self._write_pending_card(channel_id, msg))

    def move_snapshot_to_judged(
        self, channel_id: str, ts: str, staleness: str
    ) -> None:
        """Fire-and-forget: update staleness of a message card.

        Uses _pending_staleness_ids (card DB ID) for the PATCH call.
        Also updates all reply card IDs in the same thread.
        """
        card_id = (self._pending_staleness_ids.get(channel_id) or {}).get(ts)
        if not card_id:
            logger.debug(
                "move_snapshot_to_judged: no staleness card_id (channel=%s, ts=%s)",
                channel_id,
                ts,
            )
            return
        self._fire_and_forget(self._patch_card_staleness(card_id, staleness))

        thread_cards = (self._thread_card_ids.get(channel_id) or {}).get(ts) or {}
        for reply_ts, reply_card_id in thread_cards.items():
            if reply_ts != ts:
                self._fire_and_forget(self._patch_card_staleness(reply_card_id, staleness))

    def write_interpretation(
        self,
        channel_id: str,
        ts: str,
        order: int,
        content: str,
        title: str | None = None,
    ) -> None:
        """Fire-and-forget: attach an interpretation knowledge card to a message node."""
        node_id = (self._pending_card_ids.get(channel_id) or {}).get(ts)
        if not node_id:
            logger.warning(
                "write_interpretation: node_id 없음 (channel=%s, ts=%s)", channel_id, ts
            )
            return
        self._fire_and_forget(
            self._write_interpretation_card(node_id, order, content, title)
        )

    async def _write_interpretation_card(
        self,
        message_node_id: str,
        order: int,
        content: str,
        title: str | None = None,
    ) -> None:
        if title is None:
            title = "첨부 해석" if order == 0 else f"{order}차 해석"
        await self._create_knowledge_card(
            title=title,
            parent_node_id=message_node_id,
            content=content,
            staleness="judged",
        )
