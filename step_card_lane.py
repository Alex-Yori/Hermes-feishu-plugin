"""The 任务过程 step-card lane (dsh-im port) — adapter-side mixin.

Accumulates agent activity (tool starts, reasoning notes) into process blocks
and renders them as a single blue "⚙️ 任务过程" card that is updated in place
while the turn runs. On turn close the card is sealed with a collapsed
"📋 过程详情" panel wrapping the per-kind panels; overflow spills into
additional sealed cards.

The pure rendering + splitting logic lives in ``step_card.py``; this mixin
wires it to the gateway's ``feed_process_event`` progress hook and the Feishu
message create/patch APIs.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

from plugins.platforms.feishu.step_card import (
    STEP_STREAM_CARD_MAX_BYTES,
    split_step_stream_card_blocks,
    step_stream_card,
)

# Process events that become step-card blocks.
_STEP_EVENT_TYPES = frozenset({"tool.started", "reasoning.available"})


class _StepTurn:
    """Per-chat step-card state."""

    __slots__ = ("chat_id", "blocks", "card_ids", "live_card_id", "closed", "reply_to")

    def __init__(self, chat_id: str, reply_to: str | None = None) -> None:
        self.chat_id = chat_id
        self.blocks: list[dict[str, Any]] = []
        self.card_ids: list[str] = []
        self.live_card_id: Optional[str] = None
        self.closed = False
        self.reply_to: Optional[str] = reply_to


class FeishuStepCardMixin:
    """Feed agent activity into a 任务过程 step card (opt-in hook)."""

    # --- state ---

    def _init_step_card_lane(self) -> None:
        self._step_turns: dict[str, _StepTurn] = {}
        self._step_lock = threading.Lock()

    def _reset_step_card_lane(self) -> None:
        with self._step_lock:
            for turn in self._step_turns.values():
                turn.closed = True
            self._step_turns.clear()

    def _step_turn_map(self) -> dict[str, _StepTurn]:
        turns = getattr(self, "_step_turns", None)
        if turns is None:
            self._init_step_card_lane()
            turns = self._step_turns
        return turns

    def _step_card_enabled(self):
        return bool(getattr(self, "_step_cards_enabled", False) and getattr(self, "_client", None) is not None)

    # --- turn lifecycle (event loop; called by the streaming transport) ---

    def _step_turn_opened(self, chat_id: str, reply_to: str | None = None) -> None:
        import logging as _log
        _log.getLogger(__name__).info("[StepCard] _step_turn_opened: chat=%s reply_to=%s enabled=%s", chat_id, reply_to, self._step_card_enabled())
        if not self._step_card_enabled():
            return
        with self._step_lock:
            # Guard: if a turn already exists for this chat, don't create a duplicate.
            existing = self._step_turn_map().get(chat_id)
            if existing is not None and not existing.closed:
                logger.info("[StepCard] _step_turn_opened: turn already exists, skipping duplicate")
                return
            turn = _StepTurn(chat_id, reply_to=reply_to)
            self._step_turn_map()[chat_id] = turn
            # Create an empty step card immediately so it appears above the
            # main response (which starts streaming on the first token).
            try:
                card_json = step_stream_card([], status="running")
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop and loop.is_running():
                    # Already inside a running event loop — schedule without blocking.
                    fut = loop.create_task(self._step_create_card(chat_id, card_json, turn.reply_to))
                    def _on_done(f):
                        try:
                            card_id = f.result()
                            if card_id:
                                turn.live_card_id = card_id
                                turn.card_ids.append(card_id)
                        except Exception:
                            pass
                    fut.add_done_callback(_on_done)
                else:
                    card_id = asyncio.run(self._step_create_card(chat_id, card_json, turn.reply_to))
                    if card_id:
                        turn.live_card_id = card_id
                        turn.card_ids.append(card_id)
            except Exception:
                pass

    def _step_turn_closed(self, chat_id: str) -> None:
        turn = self._step_turn_map().get(chat_id)
        if turn is None:
            return
        turn.closed = True
        self._step_seal(chat_id)

    # --- feed (agent worker thread; must never raise) ---

    def feed_process_event(self, event_type: str, *, chat_id: Optional[str] = None,
                           tool_name: Optional[str] = None, preview: Optional[str] = None,
                           args: Optional[dict] = None, info: Optional[dict] = None) -> None:
        """Gateway progress hook (runs on the agent thread; must never raise)."""
        import logging as _log
        _log.getLogger(__name__).info("[StepCard] feed_process_event called: type=%s enabled=%s client=%s", event_type, self._step_card_enabled(), getattr(self, "_client", None) is not None)
        if self._step_card_enabled() and event_type in _STEP_EVENT_TYPES:
            chat = str(chat_id or "").strip()
            if chat:
                # Auto-open the step card turn on first process event (independent of stream card).
                with self._step_lock:
                    if chat not in self._step_turn_map():
                        self._step_turn_map()[chat] = _StepTurn(chat)
                try:
                    self._step_accumulate(chat, event_type, tool_name, preview)
                except Exception:
                    pass
        # Dispatch to the parent lane (thinking_process) so both lanes see the event.
        super().feed_process_event(
            event_type, chat_id=chat_id, tool_name=tool_name,
            preview=preview, args=args, info=info,
        )

    # --- block accumulation + rendering ---

    def _step_accumulate(self, chat: str, event_type: str,
                         tool_name: Optional[str], preview: Optional[str]) -> None:
        with self._step_lock:
            turn = self._step_turn_map().get(chat)
            if turn is None or turn.closed:
                return
            if event_type == "tool.started":
                turn.blocks.append({
                    "kind": "tools",
                    "title": f"🔧 {tool_name or '工具'}",
                    "lines": [str(preview or "").strip()] if preview else [],
                })
            elif event_type == "reasoning.available":
                turn.blocks.append({
                    "kind": "notes",
                    "title": "💭 思考",
                    "lines": [str(preview or "").strip()] if preview else [],
                })
            self._step_render_locked(turn)

    def _step_render_locked(self, turn: _StepTurn) -> None:
        """Render the step card and update it in place (caller holds the lock)."""
        if not turn.blocks:
            return
        card_json = step_stream_card(turn.blocks, status="running")
        # Update the live card or create a new one (async → sync bridge).
        import asyncio
        if turn.live_card_id:
            asyncio.run(self._step_patch_card(turn.live_card_id, card_json))
        else:
            card_id = asyncio.run(self._step_create_card(turn.chat_id, card_json, turn.reply_to))
            if card_id:
                turn.live_card_id = card_id
                turn.card_ids.append(card_id)

    def _step_seal(self, chat: str) -> None:
        """Seal the step card on turn close (过程详情 collapsed + overflow spills)."""
        import asyncio
        with self._step_lock:
            turn = self._step_turn_map().get(chat)
            if turn is None or not turn.blocks:
                logger.info("[StepCard] _step_seal: no turn or no blocks for chat=%s", chat)
                return
            logger.info("[StepCard] _step_seal: sealing chat=%s blocks=%d live_card=%s", chat, len(turn.blocks), turn.live_card_id)
            # Split blocks into card-sized chunks (byte + table limit).
            chunks = split_step_stream_card_blocks(turn.blocks, STEP_STREAM_CARD_MAX_BYTES)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            for i, chunk in enumerate(chunks):
                is_last = i == len(chunks) - 1
                status = "completed" if is_last else "sealed"
                card_json = step_stream_card(chunk, status=status)
                if is_last and turn.live_card_id:
                    if loop and loop.is_running():
                        loop.create_task(self._step_patch_card(turn.live_card_id, card_json))
                    else:
                        asyncio.run(self._step_patch_card(turn.live_card_id, card_json))
                else:
                    if loop and loop.is_running():
                        fut = loop.create_task(self._step_create_card(chat, card_json, turn.reply_to))
                        def _on_done(f, _turn=turn, _is_last=is_last):
                            try:
                                card_id = f.result()
                                if card_id:
                                    _turn.card_ids.append(card_id)
                                    if _is_last:
                                        _turn.live_card_id = card_id
                            except Exception:
                                pass
                        fut.add_done_callback(_on_done)
                    else:
                        card_id = asyncio.run(self._step_create_card(chat, card_json, turn.reply_to))
                        if card_id:
                            turn.card_ids.append(card_id)
                            if is_last:
                                turn.live_card_id = card_id

    # --- Feishu message create/patch (best-effort; never raise) ---

    async def _step_create_card(self, chat_id: str, card_json: str, reply_to: str | None = None) -> Optional[str]:
        import logging as _log
        _lg = _log.getLogger(__name__)
        try:
            if reply_to:
                from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
                request = ReplyMessageRequest.builder() \
                    .message_id(reply_to) \
                    .request_body(
                        ReplyMessageRequestBody.builder() \
                            .msg_type("interactive") \
                            .content(card_json) \
                            .build()
                    ) \
                    .build()
            else:
                from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
                request = CreateMessageRequest.builder() \
                    .receive_id_type("chat_id") \
                    .request_body(
                        CreateMessageRequestBody.builder() \
                            .receive_id(chat_id) \
                            .msg_type("interactive") \
                            .content(card_json) \
                            .build()
                    ) \
                    .build()
            response = await self._run_blocking(self._client.im.v1.message.create, request)
            if response and response.success() and response.data and response.data.message_id:
                _lg.info("[StepCard] card created: message_id=%s reply_to=%s", response.data.message_id, reply_to)
                return str(response.data.message_id)
            _lg.warning("[StepCard] card create failed: success=%s code=%s msg=%s",
                        response.success() if response else None,
                        response.code if response else None,
                        response.msg if response else None)
        except Exception as e:
            _lg.warning("[StepCard] card create exception: %s", e)
        return None

    async def _step_patch_card(self, message_id: str, card_json: str) -> None:
        import logging as _log
        _lg = _log.getLogger(__name__)
        try:
            from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
            request = PatchMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    PatchMessageRequestBody.builder() \
                        .content(card_json) \
                        .build()
                ) \
                .build()
            response = await self._run_blocking(self._client.im.v1.message.patch, request)
            if response and not response.success():
                _lg.warning("[StepCard] card patch failed: code=%s msg=%s", response.code, response.msg)
        except Exception as e:
            _lg.warning("[StepCard] card patch exception: %s", e)
