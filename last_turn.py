"""Feishu ``/lastturn`` — show only the most recent round of conversation.

The gateway has no command for "what was the last exchange": ``/history`` is
terminal command history (``cli_only``), ``/sessions`` lists session metadata,
and nothing prints the previous question + answer. This mixin adds a
platform-local, read-only view over this chat's own lane: it peeks the current
session id from the routing index, loads the transcript, and renders the last
user message plus the assistant reply that followed it as one Feishu message.

Read-only by design — it never writes to the session store, so running it
cannot disturb an in-flight turn.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The exact command text (case-insensitive) this handler intercepts.
LAST_TURN_COMMAND = "/lastturn"

#: Per-part display limits. Feishu splits long messages anyway; these keep the
#: card readable instead of dumping a whole analysis into the chat.
PART_CHAR_LIMIT = 2000
_ELLIPSIS = "…（已截断）"


def is_last_turn_command(text: str) -> bool:
    """Whether the text is exactly ``/lastturn`` (no arguments).

    A strict match keeps lookalikes such as ``/lastturns`` out of the way —
    they must reach the normal dispatcher untouched.
    """
    return (text or "").strip().lower() == LAST_TURN_COMMAND


def _normalize_content(content: Any) -> str:
    """Flatten OpenAI-format ``content`` (str or parts list) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, (list, tuple)):
        pieces = []
        for part in content:
            if isinstance(part, dict):
                piece = part.get("text")
                if isinstance(piece, str):
                    pieces.append(piece)
            elif isinstance(part, str):
                pieces.append(part)
        return "\n".join(p.strip() for p in pieces if p and p.strip())
    return str(content).strip()


def extract_last_turn(messages: list[dict[str, Any]]) -> Optional[tuple[str, list[str]]]:
    """The last user message and the assistant texts that answered it.

    Returns ``(user_text, assistant_texts)`` where ``assistant_texts`` may be
    empty (the round is still pending), or ``None`` when there is no user
    message at all. Tool/tool-call rows are skipped: a *round* is what a human
    sees — their question and the prose answers, not the machinery between.
    """
    if not messages:
        return None
    last_user_index: Optional[int] = None
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        if _normalize_content(msg.get("content")):
            last_user_index = index
            break
    if last_user_index is None:
        return None
    user_text = _normalize_content(messages[last_user_index].get("content"))
    assistant_texts: list[str] = []
    for msg in messages[last_user_index + 1:]:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "user":
            break  # next round starts; the last turn ends here
        if role == "assistant":
            text = _normalize_content(msg.get("content"))
            if text:
                assistant_texts.append(text)
    return user_text, assistant_texts


def _truncate_part(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + _ELLIPSIS


def render_last_turn(
    user_text: str,
    assistant_texts: list[str],
    *,
    char_limit: int = PART_CHAR_LIMIT,
) -> str:
    """Format one round for display: the question, then every answer part."""
    lines = ["🙋 你：", _truncate_part(user_text, char_limit)]
    for i, text in enumerate(assistant_texts):
        lines += [f"🤖 我{' ' + str(i + 1) if len(assistant_texts) > 1 else ''}：",
                  _truncate_part(text, char_limit)]
    if not assistant_texts:
        lines.append("🤖 我：（还没有回复——这一轮可能正在进行中）")
    return "\n\n".join(lines)


class FeishuLastTurnMixin:
    """Adapter methods serving ``/lastturn`` (see module docstring)."""

    async def handle_last_turn(self, event: Any) -> None:
        """Render the most recent round of THIS chat's session as one message."""
        chat_id = event.source.chat_id
        chat_type = str(getattr(event.source, "chat_type", "") or "dm")
        sent = getattr(self, "send", None)
        if sent is None:
            return
        store = getattr(self, "_session_store", None)
        if store is None:
            await sent(chat_id, "⚠️ 会话存储不可用，无法查看上一轮对话。")
            return
        try:
            session_key = self._menu_session_key(chat_id, chat_type)
            session_id = await asyncio.to_thread(store.peek_session_id, session_key)
        except Exception as exc:
            logger.info("[Feishu] /lastturn: session lookup failed (%s: %s)", type(exc).__name__, exc)
            session_id = None
        if not session_id:
            await sent(chat_id, "当前还没有会话记录——先随便发条消息吧。")
            return
        try:
            messages = await asyncio.to_thread(store.load_transcript, session_id)
        except Exception as exc:
            logger.info("[Feishu] /lastturn: transcript read failed (%s: %s)", type(exc).__name__, exc)
            await sent(chat_id, "⚠️ 会话记录读取失败，稍后再试。")
            return
        turn = extract_last_turn(list(messages) if isinstance(messages, (list, tuple)) else [])
        if turn is None:
            await sent(chat_id, "这个会话里还没有对话内容。")
            return
        user_text, assistant_texts = turn
        reply_to = getattr(event, "message_id", None)
        await sent(chat_id, render_last_turn(user_text, assistant_texts),
                   reply_to=reply_to if reply_to else None)
