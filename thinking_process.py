"""Native Feishu thinking-process lane (``message_cot``) for the Feishu adapter.

Ported from the dsh-im Feishu channel (xmanrui/dsh-im, ``src/channels/feishu/live-cot.mjs``):
a turn's tool/thinking activity is mirrored into a native Feishu "thinking process"
message (``/open-apis/im/v1/message_cot``) that receives AG-UI-style events:

    RUN_STARTED → [REASONING_MESSAGE_* | TOOL_CALL_START/ARGS/END | TOOL_CALL_RESULT]* → RUN_FINISHED

Feed: ``feed_process_event`` is called from the gateway progress pipeline
(``gateway/run_turn_runner.py`` ``_feed_process_lane`` — opt-in via this method's
presence) on the agent worker thread.  Events are buffered per chat and drained on
the event loop, batched ≤50 events per write with ≤4096-char event payloads —
the same bounds dsh-im applies.

The lane only runs while a streaming-card turn is open for the chat (the streaming
transport opens/closes the turn); without it there is no anchor and feeds are dropped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: dsh-im bounds: ≤50 events per write, ≤4096 chars per event content.
MAX_EVENTS_PER_WRITE = 50
MAX_EVENT_CONTENT_CHARS = 4096
MAX_TOOL_RESULT_CHARS = 1500

#: Event classes the lane consumes (everything else on the progress bus is ignored).
_PROCESS_EVENT_TYPES = frozenset({"tool.started", "tool.completed", "_thinking", "reasoning.available"})

#: Tool-icon vocabulary of the native process message (dsh-im ``TOOL_ICONS``).
TOOL_ICONS = {
    "read": "read",
    "edit": "write",
    "delete": "write",
    "move": "write",
    "search": "search",
    "fetch": "search",
    "execute": "bash",
}


def tool_kind(name: str) -> str:
    """Map a tool name onto the icon vocabulary (dsh-im ``toolKind``)."""
    value = str(name or "").lower()
    if re.search(r"read|view|list|get", value):
        return "read"
    if re.search(r"edit|write|create|patch|update", value):
        return "edit"
    if re.search(r"delete|remove", value):
        return "delete"
    if re.search(r"move|rename", value):
        return "move"
    if re.search(r"search|grep|glob|find", value):
        return "search"
    if re.search(r"fetch|web|http|download", value):
        return "fetch"
    if re.search(r"shell|bash|command|exec|terminal", value):
        return "execute"
    return ""


def _bounded_event_content(content: Any) -> str:
    """JSON-encode one event payload, shrunk to ``MAX_EVENT_CONTENT_CHARS`` (dsh-im ``boundedEventContent``)."""
    encoded = json.dumps(content, ensure_ascii=False, default=str)
    if len(encoded) <= MAX_EVENT_CONTENT_CHARS:
        return encoded
    if not isinstance(content, dict):
        return json.dumps({"truncated": True}, ensure_ascii=False)
    for key in ("delta", "message", "code"):
        if isinstance(content.get(key), str) and content[key]:
            truncated = {**content, "truncated": True}
            while truncated[key] and len(json.dumps(truncated, ensure_ascii=False, default=str)) > MAX_EVENT_CONTENT_CHARS:
                truncated[key] = truncated[key][: max(0, len(truncated[key]) - 256)]
            if len(json.dumps(truncated, ensure_ascii=False, default=str)) <= MAX_EVENT_CONTENT_CHARS:
                return json.dumps(truncated, ensure_ascii=False, default=str)
    return json.dumps({"truncated": True}, ensure_ascii=False)


def _bounded_result(value: Any) -> str:
    text = "" if value is None else str(value)
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return text[: MAX_TOOL_RESULT_CHARS - 1] + "…"


@dataclass
class _ProcessTurn:
    """Per-chat context for the active streaming turn's thinking process."""

    chat_id: str
    turn_id: Optional[str]
    reply_to: Optional[str]
    metadata: Optional[dict[str, Any]]
    session: Optional["FeishuCotSession"] = None
    open_calls: dict[str, list[str]] = field(default_factory=dict)
    call_seq: int = 0
    reasoning_seq: int = 0
    last_ts: int = 0
    closing: bool = False
    broken: bool = False

    def run_id(self) -> str:
        return f"turn-{self.turn_id or 'single'}"


def _cot_event(turn: _ProcessTurn, event_type: str, payload: dict) -> dict:
    """One AG-UI event with a strictly increasing millisecond timestamp (dsh-im ``cotEvent``)."""
    turn.last_ts = max(int(time.time() * 1000), turn.last_ts + 1)
    return {
        "event_type": event_type,
        "content": _bounded_event_content(payload),
        "timestamp": str(turn.last_ts),
    }


class FeishuCotSession:
    """One native thinking-process message (``message_cot`` create + event writes)."""

    def __init__(self, lane: Any, chat_id: str, *, reply_to: Optional[str] = None,
                 turn_id: Optional[str] = None, hidden: bool = False) -> None:
        self._lane = lane
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.turn_id = turn_id
        self.hidden = bool(hidden)
        self.cot_id: Optional[str] = None
        self.message_id: Optional[str] = None
        self.broken = False
        self.closed = False

    async def open(self) -> bool:
        """Create the process message; False (mark broken) when the app can't host one."""
        body: dict[str, Any] = {
            "receive_id": self.chat_id,
            "cot_hidden": self.hidden,
            "enable_badge": False,
            "update_feed_rank": False,
        }
        if self.reply_to:
            body["origin_message_id"] = self.reply_to
        ok, data, error = await self._lane._feishu_raw_call(
            "POST", "/open-apis/im/v1/message_cot", body, queries=[("receive_id_type", "chat_id")],
        )
        cot_id = str(data.get("cot_id") or "") if ok else ""
        message_id = str(data.get("message_id") or "") if ok else ""
        if not cot_id or not message_id:
            # One INFO per broken turn; the answer path is unaffected.
            logger.info("[Feishu] thinking process unavailable for chat %s: %s", self.chat_id, error or "no cot handle")
            self.broken = True
            return False
        self.cot_id, self.message_id = cot_id, message_id
        return True

    async def write(self, events: list[dict]) -> bool:
        if self.broken or self.closed or not events or not self.cot_id:
            return False
        ok, _, error = await self._lane._feishu_raw_call(
            "PUT",
            "/open-apis/im/v1/message_cot",
            {"events": events, "message_id": self.message_id, "cot_id": self.cot_id},
        )
        if not ok:
            # Contain the failure: the process lane stops for this turn, never the answer.
            self.broken = True
            logger.info("[Feishu] thinking process stopped updating (chat=%s): %s", self.chat_id, error)
        return ok


class FeishuThinkingProcessMixin:
    """Feed agent activity into a native thinking-process message (opt-in hook)."""

    # --- state ---

    def _init_process_lane(self) -> None:
        self._process_turns: dict[str, _ProcessTurn] = {}
        self._process_closing: dict[str, _ProcessTurn] = {}
        self._process_events: dict[str, list[tuple]] = {}
        self._process_queue_lock = threading.Lock()
        self._process_drainers: dict[str, asyncio.Task] = {}

    def _reset_process_lane(self) -> None:
        for turn in list(self._process_turns.values()) + list(self._process_closing.values()):
            if turn.session is not None:
                turn.session.broken = True  # stop writes; no finalize call on teardown
        self._process_turns.clear()
        self._process_closing.clear()
        with self._process_queue_lock:
            self._process_events.clear()

    def _process_turn_map(self) -> dict[str, _ProcessTurn]:
        turns = getattr(self, "_process_turns", None)
        if turns is None:
            self._init_process_lane()
            turns = self._process_turns
        return turns

    def _process_enabled(self) -> bool:
        return bool(getattr(self, "_thinking_process", False) and getattr(self, "_client", None) is not None)

    # --- turn lifecycle (event loop; called by the streaming transport) ---

    def _process_turn_opened(self, chat_id: str, reply_to: Optional[str],
                             metadata: Optional[dict[str, Any]], turn_id: Optional[str]) -> None:
        if not self._process_enabled():
            return
        self._process_turn_map()[chat_id] = _ProcessTurn(
            chat_id=chat_id, turn_id=turn_id, reply_to=reply_to, metadata=metadata,
        )

    def _process_turn_closed(self, chat_id: str) -> None:
        turn = self._process_turn_map().pop(chat_id, None)
        if turn is None:
            return
        turn.closing = True
        self._process_closing[chat_id] = turn
        self._schedule_process_drain(chat_id)

    # --- feed (agent worker thread; buffered + drained on the loop) ---

    def feed_process_event(self, event_type: str, *, chat_id: Optional[str] = None,
                           tool_name: Optional[str] = None, preview: Optional[str] = None,
                           args: Optional[dict] = None, info: Optional[dict] = None) -> None:
        """Gateway progress hook (runs on the agent thread; must never raise)."""
        if not self._process_enabled() or event_type not in _PROCESS_EVENT_TYPES:
            return
        chat = str(chat_id or "").strip()
        if not chat:
            return
        if chat not in getattr(self, "_process_turns", {}) and chat not in getattr(self, "_process_closing", {}):
            return  # no open streaming turn → no anchor for a process message
        try:
            with self._process_queue_lock:
                self._process_events.setdefault(chat, []).append(
                    (str(event_type), tool_name, preview, args, dict(info or {})),
                )
        except Exception:
            return
        self._schedule_process_drain(chat)

    # --- drain scheduling ---

    def _schedule_process_drain(self, chat_id: str) -> None:
        loop = getattr(self, "_loop", None)
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._start_process_drain, chat_id)
        except RuntimeError:
            pass

    def _start_process_drain(self, chat_id: str) -> None:
        """Runs on the event loop; single-flight per chat."""
        drainers = getattr(self, "_process_drainers", None)
        if drainers is None:
            self._init_process_lane()
            drainers = self._process_drainers
        if chat_id in drainers:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        drainers[chat_id] = loop.create_task(self._drain_process_events(chat_id))

    async def _drain_process_events(self, chat_id: str) -> None:
        try:
            while True:
                with self._process_queue_lock:
                    batch = self._process_events.pop(chat_id, [])
                if batch:
                    turn = self._process_turn_map().get(chat_id) or self._process_closing.get(chat_id)
                    if turn is not None and not turn.broken:
                        # Session first: RUN_STARTED must carry the smallest timestamp, and event
                        # timestamps are assigned in creation order.
                        if await self._process_ensure_session(turn):
                            events = self._process_map_events(turn, batch)
                            if events:
                                await self._process_write_batches(turn, events)
                    continue
                turn = self._process_closing.get(chat_id)
                if turn is not None:
                    await self._process_finish_turn(turn)
                    self._process_closing.pop(chat_id, None)
                    with self._process_queue_lock:
                        again = bool(self._process_events.get(chat_id))
                    if again:
                        continue
                break
        except Exception:
            logger.debug("[Feishu] process-lane drain failed (chat=%s)", chat_id, exc_info=True)
        finally:
            getattr(self, "_process_drainers", {}).pop(chat_id, None)
            # Lost-race guard: an event may have landed after the final empty check.
            with self._process_queue_lock:
                pending = bool(self._process_events.get(chat_id))
            if pending or chat_id in getattr(self, "_process_closing", {}):
                self._schedule_process_drain(chat_id)

    async def _process_ensure_session(self, turn: _ProcessTurn) -> bool:
        """Open the process message on first use; False once the lane is broken for this turn."""
        if turn.session is not None:
            return not turn.broken
        session = turn.session = FeishuCotSession(
            self, turn.chat_id, reply_to=turn.reply_to, turn_id=turn.turn_id,
            hidden=bool(getattr(self, "_thinking_process_hidden", False)),
        )
        if not await session.open():
            turn.session = session  # keep the broken marker; avoids a retry loop
            turn.broken = True
            return False
        await session.write([_cot_event(turn, "RUN_STARTED",
                                        {"threadId": turn.chat_id, "runId": turn.run_id()})])
        return True

    async def _process_write_batches(self, turn: _ProcessTurn, events: list[dict]) -> None:
        session = turn.session
        if session is None or turn.broken:
            return
        for index in range(0, len(events), MAX_EVENTS_PER_WRITE):
            if not await session.write(events[index:index + MAX_EVENTS_PER_WRITE]):
                turn.broken = True
                return

    async def _process_finish_turn(self, turn: _ProcessTurn) -> None:
        session = turn.session
        if session is None or turn.broken or session.closed:
            return
        await session.write([_cot_event(turn, "RUN_FINISHED",
                                        {"threadId": turn.chat_id, "runId": turn.run_id(), "status": "done"})])
        session.closed = True

    # --- event mapping (dsh-im live-cot.mjs handle()) ---

    def _process_map_events(self, turn: _ProcessTurn, batch: list[tuple]) -> list[dict]:
        events: list[dict] = []
        for event_type, tool_name, preview, args, info in batch:
            if event_type == "tool.started":
                events.extend(self._process_tool_started(turn, tool_name, preview, args))
            elif event_type == "tool.completed":
                events.extend(self._process_tool_completed(turn, tool_name, info))
            else:  # "_thinking" / "reasoning.available": reasoning excerpt rides preview or tool_name
                events.extend(self._process_reasoning(turn, preview or tool_name))
        return events

    @staticmethod
    def _process_tool_started(turn: _ProcessTurn, tool_name: Optional[str],
                              preview: Optional[str], args: Optional[dict]) -> list[dict]:
        name = str(tool_name or "").strip()
        if not name or name == "_thinking":
            return []
        turn.call_seq += 1
        call_id = f"{name}-{turn.call_seq}"
        turn.open_calls.setdefault(name, []).append(call_id)
        delta = str(preview or "")
        if not delta and args:
            try:
                delta = json.dumps(args, ensure_ascii=False, default=str)
            except Exception:
                delta = ""
        return [
            _cot_event(turn, "TOOL_CALL_START", {
                "toolCallId": call_id, "icon": TOOL_ICONS.get(tool_kind(name), "default"),
                "title": name, "toolCallName": name,
            }),
            _cot_event(turn, "TOOL_CALL_ARGS", {"toolCallId": call_id, "delta": delta}),
            _cot_event(turn, "TOOL_CALL_END", {"toolCallId": call_id}),
        ]

    @staticmethod
    def _process_tool_completed(turn: _ProcessTurn, tool_name: Optional[str], info: dict) -> list[dict]:
        name = str(tool_name or "").strip()
        if not name or name == "_thinking":
            return []
        open_calls = turn.open_calls.get(name) or []
        if open_calls:
            call_id = open_calls.pop(0)
        else:  # completion without a seen start (e.g. call fired while the lane was closed)
            turn.call_seq += 1
            call_id = f"{name}-{turn.call_seq}"
        payload: dict[str, Any] = {
            "messageId": f"result-{call_id}",
            "toolCallId": call_id,
            "role": "tool",
            "content": {"type": "code", "code": _bounded_result(info.get("result"))},
        }
        if info.get("is_error"):
            payload["error"] = str(info.get("is_error"))[:200]
        return [_cot_event(turn, "TOOL_CALL_RESULT", payload)]

    @staticmethod
    def _process_reasoning(turn: _ProcessTurn, text: Optional[str]) -> list[dict]:
        excerpt = str(text or "").strip()
        if not excerpt:
            return []
        turn.reasoning_seq += 1
        message_id = f"reasoning-{turn.reasoning_seq}"
        return [
            _cot_event(turn, "REASONING_MESSAGE_START", {"messageId": message_id, "role": "reasoning"}),
            _cot_event(turn, "REASONING_MESSAGE_CONTENT", {"messageId": message_id, "delta": excerpt}),
            _cot_event(turn, "REASONING_MESSAGE_END", {"messageId": message_id}),
        ]
