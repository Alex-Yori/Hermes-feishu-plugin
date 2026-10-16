"""CardKit streaming-card transport (native streaming) for the Feishu adapter.

Ported from the dsh-im Feishu channel (xmanrui/dsh-im, ``src/channels/feishu``):

- The assistant answer renders inside a native Feishu *streaming card* (CardKit 2.0)
  that updates in place while the model generates (``cardElement.content`` with a
  per-write ``sequence``) and seals with a summary (``card.settings``,
  ``streaming_mode: false``) when the turn finishes — dsh-im's ``feishu-channel.mjs``.
- Interaction prompts (exec approval / clarify) finalize the open card first, so
  continued output re-opens a NEW card below the prompt ("process-card rotation").
  The gateway's ``GatewayStreamConsumer.close_for_approval_prompt`` machinery drives
  this through the native-stream boundary; the adapter only has to seal on
  ``finalize=True`` and seed a fresh card on the next empty frame.
- Oversized frames are truncated with a notice; an oversized final answer is split
  across several native cards — dsh-im's ``streamPreview`` / ``splitStreamContent``.
- A hard failure recalls (deletes) the turn's cards so the gateway's edit/send
  fallback does not duplicate them — dsh-im recalls the streaming cards when the
  provider errors out and the bridge resends the completed answer.

The transport plugs into the gateway via the native-stream contract:
``SUPPORTS_NATIVE_STREAMING`` + ``supports_native_streaming`` + ``send_stream_frame``
(see ``gateway/stream_consumer_transport.py``).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: CardKit element id that carries the streamed markdown body (dsh-im uses the same).
STREAM_ELEMENT_ID = "stream_md"
#: Placeholder shown between card creation and the first token.
DEFAULT_STREAM_PLACEHOLDER = "正在思考…"
_STREAM_SUMMARY_PLACEHOLDER = "正在生成…"
_STREAM_SUMMARY_FALLBACK = "回答完成"
#: CardKit content cap (dsh-im: ``MAX_STREAM_CHARS`` 28000). Frames above this are
#: truncated with a notice; a final above this is split across several cards.
MAX_STREAM_CARD_CHARS = 28000
_LONG_CONTENT_NOTICE = "\n\n> 内容较长，生成完成后将分段发送完整回答。"
#: A turn whose card was never finalized (crashed turn) is dropped after this long.
_STREAM_TURN_TTL_SECONDS = 3600.0


def _stream_text_prefix(text: str, max_chars: int) -> str:
    """``text[:max_chars]`` that never splits a UTF-16 surrogate pair in half."""
    end = min(len(text), max(0, max_chars))
    if end <= 0:
        return ""
    before = ord(text[end - 1])
    after = ord(text[end]) if end < len(text) else 0
    if 0xD800 <= before <= 0xDBFF and 0xDC00 <= after <= 0xDFFF:
        end -= 1
    return text[:end]


def stream_preview(text: str) -> str:
    """One frame, truncated to the card limit with the split notice (dsh-im ``streamPreview``)."""
    if len(text) <= MAX_STREAM_CARD_CHARS:
        return text
    return _stream_text_prefix(text, MAX_STREAM_CARD_CHARS - len(_LONG_CONTENT_NOTICE)) + _LONG_CONTENT_NOTICE


def split_stream_content(text: str) -> list[str]:
    """Split a final answer into card-sized chunks (dsh-im ``splitStreamContent``).

    Prefers the last newline past 60% of the limit; keeps all whitespace in the answer.
    """
    chunks: list[str] = []
    remaining = text
    while len(remaining) > MAX_STREAM_CARD_CHARS:
        prefix = _stream_text_prefix(remaining, MAX_STREAM_CARD_CHARS)
        end = prefix.rfind("\n") + 1
        if end < MAX_STREAM_CARD_CHARS * 0.6:
            end = len(prefix)
        chunks.append(remaining[:end])
        remaining = remaining[end:]
    if remaining:
        chunks.append(remaining)
    return chunks or [""]


def _summary_of(text: str) -> str:
    summary = " ".join((text or "").split())
    if not summary:
        return _STREAM_SUMMARY_FALLBACK
    return summary if len(summary) <= 50 else _stream_text_prefix(summary, 49) + "…"


def _streaming_card_json(initial_text: str) -> str:
    """CardKit 2.0 streaming card (dsh-im ``streamingCard``)."""
    card = {
        "schema": "2.0",
        "config": {
            "streaming_mode": True,
            "summary": {"content": _STREAM_SUMMARY_PLACEHOLDER},
            "streaming_config": {
                "print_frequency_ms": {"default": 70},
                "print_step": {"default": 1},
                "print_strategy": "fast",
            },
        },
        "body": {
            "elements": [
                {"tag": "markdown", "element_id": STREAM_ELEMENT_ID, "content": initial_text},
            ],
        },
    }
    return json.dumps(card, ensure_ascii=False)


def _finished_card_json(text: str) -> str:
    """A non-streaming card for overflow continuation chunks."""
    card = {
        "schema": "2.0",
        "config": {"summary": {"content": _summary_of(text)}},
        "body": {
            "elements": [
                {"tag": "markdown", "element_id": STREAM_ELEMENT_ID, "content": text},
            ],
        },
    }
    return json.dumps(card, ensure_ascii=False)


def _lark():
    """Resolve the adapter module that actually holds the bound ``lark_oapi`` names.

    The gateway imports the adapter under the plugin namespace
    (``hermes_plugins.platforms__feishu.adapter``) while this file is imported as
    ``plugins.platforms.feishu.streaming``. A bare ``import adapter`` here would
    create a *second*, unloaded module object whose ``BaseRequest`` is still None,
    so the raw cardkit / message_cot calls would fail with "lark_oapi is not
    loaded". Scan ``sys.modules`` for the already-loaded adapter — the one with the
    SDK actually bound — and fall back to the repo-root import for tests / direct use.
    """
    import sys

    for name, mod in list(sys.modules.items()):
        if mod is None or "feishu" not in name:
            continue
        if name.rsplit(".", 1)[-1] != "adapter":
            continue
        if getattr(mod, "BaseRequest", None) is not None:
            return mod
    from plugins.platforms.feishu import adapter as feishu_adapter

    return feishu_adapter


def _raw_json_request(
    http_method: str,
    uri: str,
    *,
    body: Optional[dict] = None,
    queries: Optional[list[tuple[str, str]]] = None,
) -> Any:
    """Raw ``BaseRequest`` for endpoints the SDK does not wrap (cardkit / message_cot).

    Mirrors ``adapter._tenant_get_request`` with GET swapped for any method and a
    JSON body attached.
    """
    feishu = _lark()
    if feishu.BaseRequest is None:
        raise RuntimeError("lark_oapi is not loaded")
    builder = feishu.BaseRequest.builder().http_method(getattr(feishu.HttpMethod, http_method)).uri(uri)
    if queries:
        builder = builder.queries(queries)
    builder = builder.token_types({feishu.AccessTokenType.TENANT})
    if body is not None:
        builder = builder.headers({"Content-Type": "application/json; charset=utf-8"})
        builder = builder.body(body)
    return builder.build()


def _response_payload(response: Any) -> tuple[int, str, dict]:
    """``(code, msg, data)`` from a raw Feishu response envelope; ``code != 0`` = failure."""
    content = getattr(getattr(response, "raw", None), "content", None)
    if not content:
        return -1, "empty response", {}
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return -1, "non-JSON response", {}
    if not isinstance(payload, dict):
        return -1, "unexpected response shape", {}
    code = payload.get("code", 0)
    return (code if isinstance(code, int) else -1), str(payload.get("msg") or ""), (payload.get("data") or {})


@dataclass
class FeishuStreamTurn:
    """One open streaming-card turn (answer) keyed by chat + gateway turn id."""

    key: str
    chat_id: str
    turn_id: Optional[str]
    card_id: str
    message_id: str
    sequence: int = 0
    content: str = ""
    reply_to: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    created_at: float = field(default_factory=time.monotonic)


class FeishuStreamMixin:
    """Native-streaming transport backed by CardKit streaming cards."""

    #: The gateway probes this class attr before offering the native-stream transport.
    SUPPORTS_NATIVE_STREAMING = True

    # --- state ---

    def _init_stream_state(self) -> None:
        self._stream_turns: dict[str, FeishuStreamTurn] = {}

    def _stream_turn_map(self) -> dict[str, FeishuStreamTurn]:
        turns = getattr(self, "_stream_turns", None)
        if turns is None:
            turns = self._stream_turns = {}
        return turns

    def _reset_stream_state(self) -> None:
        self._stream_turn_map().clear()

    def _prune_stream_turns(self) -> None:
        now = time.monotonic()
        turns = self._stream_turn_map()
        for key in [k for k, t in turns.items() if now - t.created_at > _STREAM_TURN_TTL_SECONDS]:
            turns.pop(key, None)

    # --- gateway probes / contract ---

    def _stream_cards_ready(self) -> bool:
        return bool(getattr(self, "_stream_cards_enabled", False) and getattr(self, "_client", None) is not None)

    def supports_native_streaming(self, chat_type: Optional[str] = None,
                                  metadata: Optional[dict[str, Any]] = None,
                                  chat_id: Optional[str] = None) -> bool:
        """Advertise CardKit streaming cards; WebSocket and webhook modes both qualify."""
        del chat_type, metadata, chat_id
        return self._stream_cards_ready()

    @staticmethod
    def _stream_turn_key(chat_id: str, turn_id: Any) -> str:
        return f"{chat_id}\x00{turn_id if turn_id is not None else 'single'}"

    # --- low-level cardkit calls (raw SDK requests; never raise) ---

    async def _feishu_raw_call(self, method: str, uri: str, body: Optional[dict] = None,
                                *, queries: Optional[list[tuple[str, str]]] = None) -> tuple[bool, dict, str]:
        """One raw cardkit call → ``(ok, data, error)``.  Never raises."""
        if getattr(self, "_client", None) is None:
            return False, {}, "not connected"
        try:
            request = _raw_json_request(method, uri, body=body, queries=queries)
            response = await self._run_blocking(self._client.request, request)
        except Exception as exc:  # network/SDK errors → caller falls back
            return False, {}, f"{type(exc).__name__}: {exc}"
        code, msg, data = _response_payload(response)
        if code != 0:
            return False, data, f"[{code}] {msg}"
        return True, data, ""

    async def _stream_card_send(self, chat_id: str, card_id: str, reply_to: Optional[str],
                                metadata: Optional[dict[str, Any]]) -> tuple[Optional[str], str]:
        """Send a card message (reply-anchored when ``reply_to`` is set)."""
        payload = json.dumps({"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False)
        try:
            response = await self._feishu_send_with_retry(
                chat_id=chat_id, msg_type="interactive", payload=payload, reply_to=reply_to, metadata=metadata,
            )
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"
        result = self._finalize_send_result(response, "stream card send failed")
        if result.success and result.message_id:
            return str(result.message_id), ""
        return None, result.error or "send rejected"

    async def _recall_message(self, chat_id: str, message_id: str) -> None:
        try:
            await self.delete_message(chat_id, message_id)
        except Exception:
            logger.debug("[Feishu] stream card recall failed (message=%s)", message_id, exc_info=True)

    async def _recall_stream_turn(self, turn: FeishuStreamTurn, extra_message_ids: Optional[list[str]] = None) -> None:
        for message_id in [turn.message_id, *(extra_message_ids or [])]:
            if message_id:
                await self._recall_message(turn.chat_id, message_id)

    # --- card lifecycle ---

    async def _supersede_stale_turns(self, chat_id: str, *, keep_key: str) -> None:
        """Close out cards from dead turns in this chat (crash, killed turn) so no card is
        left visibly "streaming": a placeholder-only card is recalled, content is sealed."""
        for turn in [t for t in self._stream_turn_map().values() if t.chat_id == chat_id and t.key != keep_key]:
            if turn.content == DEFAULT_STREAM_PLACEHOLDER:
                await self._recall_stream_turn(turn)
            else:
                await self._seal_stream_card(turn)
            self._finish_stream_turn(turn)

    async def _open_stream_turn(self, turn_key: str, chat_id: str, reply_to: Optional[str],
                                metadata: Optional[dict[str, Any]], turn_id: Any) -> bool:
        """Create + send the streaming card for a fresh turn (also the rotation re-seed)."""
        self._prune_stream_turns()
        await self._supersede_stale_turns(chat_id, keep_key=turn_key)
        ok, data, error = await self._feishu_raw_call(
            "POST", "/open-apis/cardkit/v1/cards",
            {"type": "card_json", "data": _streaming_card_json(DEFAULT_STREAM_PLACEHOLDER)},
        )
        card_id = str(data.get("card_id") or "") if ok else ""
        if not card_id:
            logger.warning("[Feishu] stream card create failed (chat=%s): %s", chat_id, error or "no card_id")
            return False
        message_id, send_error = await self._stream_card_send(chat_id, card_id, reply_to, metadata)
        if not message_id:
            logger.warning("[Feishu] stream card send failed (chat=%s): %s", chat_id, send_error)
            return False
        turn = FeishuStreamTurn(
            key=turn_key, chat_id=chat_id, turn_id=str(turn_id) if turn_id is not None else None,
            card_id=card_id, message_id=message_id, content=DEFAULT_STREAM_PLACEHOLDER,
            reply_to=reply_to, metadata=metadata,
        )
        self._stream_turn_map()[turn_key] = turn
        self._note_process_turn_opened(turn)
        return True

    async def _update_stream_turn_content(self, turn: FeishuStreamTurn, visible: str) -> tuple[bool, str]:
        turn.sequence += 1
        ok, _, error = await self._feishu_raw_call(
            "PUT",
            f"/open-apis/cardkit/v1/cards/{turn.card_id}/elements/{STREAM_ELEMENT_ID}/content",
            {
                "content": visible,
                "sequence": turn.sequence,
                "uuid": f"content_{turn.card_id}_{turn.sequence}",
            },
        )
        if ok:
            turn.content = visible
        return ok, error

    async def _seal_stream_card(self, turn: FeishuStreamTurn) -> tuple[bool, str]:
        """Seal (``streaming_mode: false`` + summary) — the card stops its live indicator."""
        turn.sequence += 1
        settings = json.dumps(
            {"config": {"streaming_mode": False, "summary": {"content": _summary_of(turn.content)}}},
            ensure_ascii=False,
        )
        ok, _, error = await self._feishu_raw_call(
            "PATCH",
            f"/open-apis/cardkit/v1/cards/{turn.card_id}/settings",
            {
                "settings": settings,
                "sequence": turn.sequence,
                "uuid": f"settings_{turn.card_id}_{turn.sequence}",
            },
        )
        return ok, error

    async def _send_finished_card(self, chat_id: str, text: str, reply_to: Optional[str],
                                  metadata: Optional[dict[str, Any]]) -> Optional[str]:
        """One non-streaming card carrying an overflow continuation chunk."""
        ok, data, error = await self._feishu_raw_call(
            "POST", "/open-apis/cardkit/v1/cards",
            {"type": "card_json", "data": _finished_card_json(text)},
        )
        card_id = str(data.get("card_id") or "") if ok else ""
        if not card_id:
            logger.warning("[Feishu] overflow card create failed (chat=%s): %s", chat_id, error or "no card_id")
            return None
        message_id, send_error = await self._stream_card_send(chat_id, card_id, reply_to, metadata)
        if not message_id:
            logger.warning("[Feishu] overflow card send failed (chat=%s): %s", chat_id, send_error)
        return message_id

    # --- process-lane bridge (thinking process; no-op when that mixin is absent) ---

    def _note_process_turn_opened(self, turn: FeishuStreamTurn) -> None:
        opener = getattr(self, "_process_turn_opened", None)
        if callable(opener):
            try:
                opener(turn.chat_id, turn.reply_to, turn.metadata, turn.turn_id)
            except Exception:
                logger.debug("[Feishu] process-lane open failed", exc_info=True)
        step_opener = getattr(self, "_step_turn_opened", None)
        if callable(step_opener):
            try:
                step_opener(turn.chat_id, turn.reply_to)
            except Exception:
                logger.debug("[Feishu] step-card open failed", exc_info=True)

    def _finish_stream_turn(self, turn: FeishuStreamTurn) -> None:
        logger.info("[Feishu] _finish_stream_turn: chat=%s", turn.chat_id)
        self._stream_turn_map().pop(turn.key, None)
        closer = getattr(self, "_process_turn_closed", None)
        if callable(closer):
            try:
                closer(turn.chat_id)
            except Exception:
                logger.debug("[Feishu] process-lane close failed", exc_info=True)
        step_closer = getattr(self, "_step_turn_closed", None)
        if callable(step_closer):
            try:
                step_closer(turn.chat_id)
            except Exception:
                logger.debug("[Feishu] step-card close failed", exc_info=True)

    # --- native-stream contract ---

    async def send_stream_frame(self, text: str, *, finalize: bool = False, chat_id: Optional[str] = None,
                                reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
                                **kwargs) -> bool:
        """One native-stream frame.  False → the consumer falls back to send()/edit.

        Frames: empty seed opens the card; interim frames replace its markdown body;
        ``finalize=True`` seals it (and a later empty frame seeds a NEW card — the
        rotation that keeps post-interaction output below the interaction card).
        """
        chat = str(chat_id or "").strip()
        if not chat or not self._stream_cards_ready():
            return False
        turn_key = self._stream_turn_key(chat, kwargs.get("turn_id"))
        turn = self._stream_turn_map().get(turn_key)

        if turn is None:
            if finalize:
                # Nothing open (never seeded / already sealed): the gateway's fallback
                # send owns delivery — never open a card just to seal it.
                return False
            # Open the step-card lane BEFORE the streaming card so the step card
            # appears above the main response in the chat feed.
            if hasattr(self, "_step_turn_opened"):
                self._step_turn_opened(chat, reply_to)
            if not await self._open_stream_turn(turn_key, chat, reply_to, metadata, kwargs.get("turn_id")):
                return False
            turn = self._stream_turn_map().get(turn_key)
            if not text:
                return True  # explicit seed frame: the card doubles as the typing indicator

        assert turn is not None
        raw_text = str(text or "")
        if finalize:
            # The RAW final text: splitting into card-sized chunks is _seal_stream_turn's job —
            # truncating first would silently drop the tail of a long answer.
            return await self._seal_stream_turn(turn, raw_text)
        visible = stream_preview(raw_text)
        if visible == turn.content:
            return True  # unchanged frame — skip the write (identity dedup)
        ok, error = await self._update_stream_turn_content(turn, visible)
        if not ok:
            logger.warning("[Feishu] stream card frame failed (chat=%s): %s — recalling card", chat, error)
            await self._recall_stream_turn(turn)
            self._finish_stream_turn(turn)
            return False
        return True

    async def _seal_stream_turn(self, turn: FeishuStreamTurn, text: str) -> bool:
        """Finalize: head chunk in the open card, overflow chunks as follow-up cards."""
        chunks = split_stream_content(text or turn.content or DEFAULT_STREAM_PLACEHOLDER)
        head = chunks[0]
        if head and head != turn.content:
            ok, error = await self._update_stream_turn_content(turn, head)
            if not ok:
                logger.warning("[Feishu] stream card final update failed (chat=%s): %s — recalling", turn.chat_id, error)
                await self._recall_stream_turn(turn)
                self._finish_stream_turn(turn)
                return False
        ok, error = await self._seal_stream_card(turn)
        if not ok:
            logger.warning("[Feishu] stream card seal failed (chat=%s): %s — recalling", turn.chat_id, error)
            await self._recall_stream_turn(turn)
            self._finish_stream_turn(turn)
            return False
        sent_ids: list[str] = []
        for chunk in chunks[1:]:
            message_id = await self._send_finished_card(turn.chat_id, chunk, turn.reply_to, turn.metadata)
            if not message_id:
                # Duplicate-free fallback: recall every card of the turn, then report
                # failure so the gateway's edit/send path delivers the whole answer once.
                logger.warning(
                    "[Feishu] stream card overflow chunk failed (chat=%s) — recalling the turn for the fallback resend",
                    turn.chat_id,
                )
                await self._recall_stream_turn(turn, extra_message_ids=sent_ids)
                self._finish_stream_turn(turn)
                return False
            sent_ids.append(message_id)
        self._finish_stream_turn(turn)
        return True

    async def abandon_open_draft(self, chat_id: str, text: str = "", metadata: Optional[dict[str, Any]] = None) -> None:
        """Turn death (stale exit / cancel / session reset): stop the live indicator.

        Seals the open card with whatever text was visible, or recalls a card that
        never received content (a bare ``正在思考…`` placeholder would hang forever).
        """
        del metadata
        chat = str(chat_id or "").strip()
        if not chat:
            return
        turns = [t for t in self._stream_turn_map().values() if t.chat_id == chat]
        for turn in turns:
            body = stream_preview(str(text or "")) or turn.content
            if not str(body or "").strip() or body == DEFAULT_STREAM_PLACEHOLDER:
                # Nothing real was ever shown: a sealed "正在思考…" card would hang forever.
                await self._recall_stream_turn(turn)
            else:
                if body != turn.content:
                    await self._update_stream_turn_content(turn, body)
                await self._seal_stream_card(turn)
            self._finish_stream_turn(turn)
