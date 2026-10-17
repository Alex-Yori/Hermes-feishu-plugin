"""Feishu ``/menu`` card: one entry point for the common session actions.

Ported from the dsh-im Feishu channel (xmanrui/dsh-im, ``feishu-cards.mjs``
``menuCard``), keeping its card schema and layout idioms but trimming the
actions to what Hermes actually has.

Two things this file copies from dsh-im because they decide whether a card
renders at all:

* **Schema 2.0** — ``{schema: "2.0", header: ..., body: {elements}}``. The
  sibling ``streaming.py`` and ``step_card.py`` are 2.0 as well.
* **``behaviors: [{type: "callback", value: ...}]`` on buttons**, laid out with
  ``column_set`` / ``column``.

Getting either wrong fails *silently*: the API answers ``code: 0`` and the card
renders with no controls, because Feishu drops elements it cannot place. A bare
``button`` child of ``elements``, or the 1.0 ``value`` field inside a 2.0 card,
is exactly that case.

dsh-im's menu also carries workspace, agent-preset, archive and steer controls.
Hermes has no workspace or agent-preset concept, so those are omitted rather
than shipped as dead controls; every row here forwards to a real command:

    🆕 新会话   -> /new        📋 会话列表 -> /sessions
    ⏹ 停止      -> /stop       📐 压缩     -> /compress
    🧠 切换模型 -> /model
    📊 状态     -> /status     📖 帮助     -> /help

Forwarding (rather than calling the session layer directly) is the point: the
command dispatcher already resolves config gates, busy policies and permissions,
so a click behaves exactly as if the user had typed the command.

Click routing reuses the adapter's synchronous card callback
(``_on_card_action_trigger``), which dispatches on ``action.value`` keys
(``hermes_action``, ``hermes_update_prompt_action``). Menu buttons carry
``hermes_menu_action`` so the families never collide — the *key* is read the
same way for both schemas even though 2.0 nests it under ``behaviors``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Action key on a menu button's callback value. Distinct from the approval
#: cards' ``hermes_action`` so the callback families never collide.
MENU_ACTION_KEY = "hermes_menu_action"

#: A menu is a snapshot, not a live view: buttons expire so a stale card cannot
#: run a command long after the session it described has moved on.
MENU_TTL_SECONDS = 10 * 60

#: Upper bound on tracked open menus per chat, so a chatty group cannot grow
#: this map without limit.
MAX_TRACKED_MENUS_PER_CHAT = 8

#: Action -> the command a click is forwarded as. Every target must exist in
#: ``COMMAND_REGISTRY``; a typo here would be refused by the dispatcher.
MENU_ACTIONS: dict[str, str] = {
    "new": "/new",
    "stop": "/stop",
    "compress": "/compress",
    "model": "/model",
    "status": "/status",
    "help": "/help",
}

#: Select names (dsh-im names its dropdowns the same way) -> command template.
#: A ``select_static`` callback carries ``action.name`` + ``action.option``, not
#: ``action.value``, so dropdowns are dispatched separately from buttons.
MENU_SELECTS: dict[str, str] = {
    "menu_session": "/resume {option}",
    "menu_model": "/model {option}",
}

#: How many entries a dropdown may carry. Feishu truncates long menus badly and
#: the full catalogue is always one ``/sessions`` / ``/model`` away.
MENU_SELECT_MAX_OPTIONS = 6

#: The session picker is the feature, not a convenience: it needs enough entries to
#: reach a desktop conversation started a few turns ago.
MENU_SESSION_LIMIT = 10

#: The user's own provider catalogues run large (dozens of ``cn:*`` models), so the
#: picker takes a generous slice and ``/model`` remains the complete list.
MENU_MODEL_LIMIT = 12

#: Badges for sessions that did not originate in this chat, so a desktop session is
#: recognisable before it is picked.
MENU_SOURCE_BADGES: dict[str, str] = {
    "desktop": "桌面",
    "feishu": "飞书",
    "slack": "Slack",
    "discord": "Discord",
    "telegram": "TG",
    "cli": "CLI",
}


def decode_menu_select(action: Any, action_value: Any) -> tuple[str, str]:
    """Read a dropdown pick out of whichever field the callback used.

    Feishu has moved this payload around between card versions: 1.0 reports
    ``action.name`` + ``action.option``, while a 2.0 ``select_static`` may echo
    the element's ``behaviors.value`` back (which is where the select's own name
    lives for us) and can put the choice in ``option``, ``options``, or a
    ``value`` dict. Accepting only the first shape meant a click was silently
    dropped and then reported as an expired card.

    Returns ``(name, value)``, both empty when nothing recognisable is present.
    """
    name = str(getattr(action, "name", "") or "")
    if not name and isinstance(action_value, dict):
        marker = str(action_value.get(MENU_ACTION_KEY) or "")
        if marker.startswith("select:"):
            name = marker.split(":", 1)[1]

    raw = ""
    for candidate in (
        getattr(action, "option", None),
        getattr(action, "input_value", None),
    ):
        if isinstance(candidate, str) and candidate:
            raw = candidate
            break

    if not raw:
        options = getattr(action, "options", None)
        if isinstance(options, (list, tuple)) and options:
            first = options[0]
            if isinstance(first, str) and first:
                raw = first
            elif isinstance(first, dict):
                picked = first.get("value") or first.get("text")
                if isinstance(picked, str):
                    raw = picked

    if not raw:
        value = getattr(action, "value", None)
        if isinstance(value, str):
            raw = value
        elif isinstance(value, dict):
            for key in (name, "value", "option"):
                picked = value.get(key)
                if isinstance(picked, str) and picked:
                    raw = picked
                    break
            else:
                # Single-entry dict {name: choice}: take the only value there is.
                choices = [item for item in value.values() if isinstance(item, str) and item]
                if len(choices) == 1:
                    raw = choices[0]

    if not raw:
        form_value = getattr(action, "form_value", None)
        if isinstance(form_value, dict) and name:
            picked = form_value.get(name)
            if isinstance(picked, str):
                raw = picked

    # A 2.0 callback can echo ONLY the element's behaviors.value, which names the
    # select rather than the choice. That is not a selection, and treating it as
    # one would run `/resume select:menu_session`.
    if raw.startswith("select:"):
        # The marker still names the select, so keep that much.
        name = name or raw.split(":", 1)[1]
        raw = ""
    elif raw in MENU_SELECTS:
        name = name or raw
        raw = ""
    if not raw:
        return name, ""
    # The picker encodes its own name into every option value, so the selection
    # identifies itself even when Feishu drops action.name.
    return _split_select_value(raw, name)


def _split_select_value(raw: str, name: str) -> tuple[str, str]:
    """Peel the ``<select>::`` prefix the picker writes into option values."""
    if "::" in raw:
        tag, _, rest = raw.partition("::")
        if tag in MENU_SELECTS:
            return tag, rest
    return name, raw


def _plain_text(content: str) -> dict[str, Any]:
    return {"tag": "plain_text", "content": content}


def _menu_button(action: str, label: str, *, primary: bool = False) -> dict[str, Any]:
    """A 2.0 callback button. ``behaviors`` is what makes the click fire."""
    return {
        "tag": "button",
        "text": _plain_text(label),
        "type": "primary" if primary else "default",
        "width": "fill",
        "behaviors": [{"type": "callback", "value": {MENU_ACTION_KEY: action}}],
    }


def _button_row(*buttons: dict[str, Any]) -> dict[str, Any]:
    """Buttons side by side, equal width (dsh-im's ``buttonPair``)."""
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [button]}
            for button in buttons
        ],
    }


def _section(title: str) -> dict[str, Any]:
    return {"tag": "markdown", "content": f"**{title}**"}


def _select(name: str, placeholder: str, options: list[dict[str, str]]) -> dict[str, Any]:
    """A 2.0 single-select dropdown.

    Schema 2.0 rejects a ``selected`` field on options (230099 parse error);
    ``initial_index`` (1-based) is the supported way to show a current value.
    """
    return {
        "tag": "select_static",
        "name": name,
        "placeholder": _plain_text(placeholder),
        "initial_index": 1 if options else 0,
        # The value carries the select's own name so a callback identifies its
        # source even when Feishu drops ``action.name`` (it does for some 2.0
        # select payloads), which would otherwise look like a dead card.
        "options": [
            {"text": _plain_text(option["label"]), "value": f"{name}::{option['value']}"}
            for option in options
        ],
        "behaviors": [{"type": "callback", "value": {MENU_ACTION_KEY: f"select:{name}"}}],
    }


def _icon_label(icon: str, text: str) -> dict[str, Any]:
    return {"tag": "markdown", "content": f"{icon} {text}"}


def build_menu_card(
    *,
    note: str = "",
    sessions: Optional[list[dict[str, str]]] = None,
    models: Optional[list[dict[str, str]]] = None,
) -> dict[str, Any]:
    """The ``/menu`` card: session + model pickers, task control, system actions.

    ``sessions`` / ``models`` are ``{label, value}`` options. When a list comes
    back empty the matching dropdown is replaced by its button, which still
    reaches the same data through the command dispatcher — dsh-im's own
    fallback, and better than rendering a dropdown with nothing in it.
    """
    elements: list[dict[str, Any]] = []

    # Each picker carries its own ceiling: the providers above already trimmed
    # to MENU_SESSION_LIMIT / MENU_MODEL_LIMIT, and re-slicing everything to the
    # shared MAX would quietly cut both lists down to the smallest of them.
    session_options = (sessions or [])[:MENU_SESSION_LIMIT]
    if session_options:
        elements.append(_select("menu_session", "切换会话", session_options))
    else:
        elements.append(_icon_label("💬", "暂无其他会话"))
    elements.append(_button_row(_menu_button("new", "🆕 新会话", primary=True)))

    model_options = (models or [])[:MENU_MODEL_LIMIT]
    if model_options:
        elements.append(_select("menu_model", "切换模型", model_options))
    else:
        elements.append(_button_row(_menu_button("model", "🧠 切换模型")))

    elements.append({"tag": "hr"})
    elements.append(_section("任务控制"))
    elements.append(_button_row(_menu_button("stop", "⏹ 停止"), _menu_button("compress", "📐 压缩")))
    elements.append({"tag": "hr"})
    elements.append(_section("系统"))
    elements.append(_button_row(_menu_button("status", "📊 状态"), _menu_button("help", "📖 帮助")))

    if note:
        # Schema 2.0 dropped the ``note`` tag (200861 "unsupported tag note"),
        # so the footer is a plain markdown line.
        elements.append({"tag": "markdown", "content": f"<font color='grey'>{note}</font>"})
    return {
        "schema": "2.0",
        "header": {"title": _plain_text("🤖 助手中心"), "template": "blue"},
        "body": {"elements": elements},
    }


class FeishuMenuMixin:
    """Serves ``/menu`` and handles the clicks on the card it sends."""

    # Provided by FeishuAdapter / other mixins. Declared so type checkers can
    # see the cross-mixin calls this one relies on.
    _feishu_send_with_retry: Any
    _finalize_send_result: Any
    _submit_on_loop: Any
    _card_response: Any
    _is_card_action_duplicate: Any
    _dispatch_synthetic_event: Any

    def _init_menu_cards(self) -> None:
        """Menu tracking. Called from the adapter's __init__."""
        self._menu_cards: dict[str, dict[str, Any]] = {}

    def _menu_map(self) -> dict[str, dict[str, Any]]:
        """The tracking map, created lazily so a host without __init__ still works."""
        menus = getattr(self, "_menu_cards", None)
        if menus is None:
            menus = self._menu_cards = {}
        return menus

    # --- data for the dropdowns ---

    def _menu_session_key(self, chat_id: str, chat_type: str) -> str:
        """The gateway lane this chat routes through (same shape the dispatcher uses)."""
        chat_type = (chat_type or "dm").strip().lower()
        return f"agent:main:feishu:{'dm' if chat_type in {'dm', 'p2p'} else 'group'}:{chat_id}"

    def _menu_caller_is_admin(self, user_id: str, chat_type: str) -> bool:
        """Whether this caller is an EXPLICITLY configured slash admin.

        Mirrors the gateway's own rule (``_resume_caller_is_admin``): the gate must be
        enabled AND the user on the admin list. Gating disabled means "no admins", not
        "everyone is an admin" — cross-origin session enumeration is deliberately
        admin-only (it would otherwise leak ids and previews across origins).
        """
        if not user_id:
            return False
        try:
            from gateway.slash_access import policy_from_extra
        except Exception:
            return False
        extra = getattr(getattr(self, "config", None), "extra", None) or {}
        if not isinstance(extra, dict):
            return False
        scope = "group" if str(chat_type or "").strip().lower() not in {"dm", "p2p"} else "dm"
        try:
            policy = policy_from_extra(extra, scope)
            return bool(policy.enabled and policy.is_admin(user_id))
        except Exception:
            return False

    async def _menu_session_options(
        self, chat_id: str, *, user_id: str = "", chat_type: str = "dm",
    ) -> list[dict[str, str]]:
        """Hermes sessions as dropdown options, newest first.

        Reads the session DATABASE through ``query_session_listing`` — the same
        helper ``/sessions`` uses — because desktop sessions never enter the
        gateway routing index (they don't route anywhere), yet the user's whole
        point is resuming a desktop conversation from Feishu.

        Cross-origin listing is admin-only, exactly as it is for ``/sessions all``:
        a non-admin sees only this chat's lane rather than everyone's session ids.

        The option value is the argument ``/resume`` takes. ``--all`` is prepended
        only for an admin, so a non-admin is never handed a flag their own access
        check would reject.
        """
        store = getattr(self, "_session_store", None)
        db = getattr(store, "_db", None) or store
        if db is None or not hasattr(db, "list_sessions_rich"):
            return []
        admin = self._menu_caller_is_admin(user_id, chat_type)
        session_key = self._menu_session_key(chat_id, chat_type)
        try:
            from hermes_cli.session_listing import query_session_listing

            rows = await asyncio.to_thread(
                query_session_listing, db,
                source="feishu",
                session_key=None if admin else session_key,
                include_all_sources=admin,
                # Unnamed sessions are the common case on desktop; hiding them would
                # leave the dropdown empty for the exact sessions worth resuming.
                include_unnamed=True,
                limit=MENU_SESSION_LIMIT,
                # Subagent/tool lanes are not conversations anyone resumes.
                exclude_sources=["tool", "subagent"],
            )
        except Exception as exc:
            logger.info("[Feishu] menu: session list unavailable (%s: %s)", type(exc).__name__, exc)
            return []
        return self._menu_session_rows_to_options(rows, admin=admin)

    @staticmethod
    def _menu_session_rows_to_options(rows: Any, *, admin: bool) -> list[dict[str, str]]:
        """Turn listing rows into ``{label, value}`` options with readable labels."""
        options: list[dict[str, str]] = []
        for row in rows if isinstance(rows, (list, tuple)) else []:
            if not isinstance(row, dict):
                continue
            session_id = str(row.get("id") or "")
            if not session_id:
                continue
            title = str(row.get("title") or "").strip()
            preview = str(row.get("preview") or "").strip()
            source = str(row.get("source") or "").strip()
            label = title or preview[:20] or session_id[:8]
            if source and source != "feishu":
                badge = MENU_SOURCE_BADGES.get(source, source)
                label = f"[{badge}] {label}"
            if row.get("is_current_session"):
                label = f"✓ {label}"
            value = f"--all {session_id}" if admin else session_id
            options.append({"label": label[:40], "value": value})
        return options

    @staticmethod
    def _menu_model_options() -> list[dict[str, str]]:
        """The models configured under ``providers.<name>.models``.

        This is the user's own catalogue (what the desktop picker lists), not the
        provider-agnostic curated list — showing models an account cannot reach
        just produces picks that fail. The active provider leads.
        """
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly() or {}
        except Exception as exc:
            logger.info("[Feishu] menu: model list unavailable (%s: %s)", type(exc).__name__, exc)
            return []
        providers = config.get("providers")
        if not isinstance(providers, dict):
            return []
        active_provider = str((config.get("model") or {}).get("provider") or "")
        options: list[dict[str, str]] = []
        ordered = sorted(
            ((name, cfg) for name, cfg in providers.items() if isinstance(cfg, dict)),
            key=lambda item: item[0] != active_provider,
        )
        for name, provider in ordered:
            models = provider.get("models")
            model_ids = list(models.keys()) if isinstance(models, dict) else []
            default = provider.get("model")
            if isinstance(default, str) and default and default not in model_ids:
                model_ids.insert(0, default)
            if not model_ids:
                continue
            label_prefix = str(provider.get("name") or name)
            for model_id in model_ids:
                text = str(model_id)
                options.append({"label": f"{label_prefix} · {text}"[:40], "value": text})
                if len(options) >= MENU_MODEL_LIMIT:
                    return options
        return options

    async def _build_menu_card(
        self, chat_id: str, *, note: str = "", user_id: str = "", chat_type: str = "dm",
    ) -> dict[str, Any]:
        return build_menu_card(
            note=note,
            sessions=await self._menu_session_options(chat_id, user_id=user_id, chat_type=chat_type),
            models=self._menu_model_options(),
        )

    # --- serving ---

    async def send_menu_card(
        self, chat_id: str, *, reply_to: Optional[str] = None,
        user_id: str = "", chat_type: str = "dm",
    ) -> Optional[str]:
        """Send the menu card and remember it so clicks can be validated.

        ``user_id``/``chat_type`` decide the session picker's scope: only an
        explicitly configured slash admin may see sessions from other origins.

        Returns the message id, or None when the send was rejected — the caller
        then falls back to plain text so the user still gets something.
        """
        # Remember the scope this chat speaks in, so a later click synthesises
        # its command in the same one (see _menu_chat_type). Recorded even when
        # the send is refused: the scope is a property of the chat, not the card.
        # Stored as "p2p"/"group": "p2p" is what _resolve_source_chat_type needs
        # to build a DM source (see _menu_chat_type).
        self._menu_chat_types()[chat_id] = (
            "p2p" if str(chat_type).strip().lower() in {"dm", "p2p"} else "group"
        )
        if getattr(self, "_client", None) is None:
            return None
        card = await self._build_menu_card(
            chat_id, note="按钮 10 分钟内有效，过期请重新发送 /menu", user_id=user_id,
            chat_type=chat_type,
        )
        try:
            response = await self._feishu_send_with_retry(
                chat_id=chat_id, msg_type="interactive",
                payload=json.dumps(card, ensure_ascii=False), reply_to=reply_to, metadata=None,
            )
        except Exception as exc:
            logger.info(
                "[Feishu] menu card send failed (chat=%s): %s: %s", chat_id, type(exc).__name__, exc,
            )
            return None
        result = self._finalize_send_result(response, "send_menu_card failed")
        message_id = str(result.message_id or "")
        if not result.success or not message_id:
            return None
        self._remember_menu(chat_id, message_id)
        return message_id

    def _menu_chat_type(self, chat_id: str) -> str:
        """The chat type this chat's menu was served in, in the adapter's own
        vocabulary: ``"p2p"`` or ``"group"``.

        ``_resolve_source_chat_type`` only produces a DM source for the literal
        ``"p2p"`` — passing ``"dm"`` silently yields a GROUP source. That is not
        cosmetic: slash access resolves the dm and group admin lists separately,
        so a DM command dispatched as a group loses its admin rights and a
        cross-origin ``/resume --all`` is refused as if the caller were a
        stranger.
        """
        mapping = self._menu_chat_types()
        value = str(mapping.get(chat_id) or "").strip().lower()
        # Defaults to "group": the narrower slash scope, so an unknown chat can
        # never inherit DM admin rights.
        return value or "group"

    def _menu_chat_types(self) -> dict[str, str]:
        store = getattr(self, "_menu_chat_type_map", None)
        if store is None:
            store = {}
            self._menu_chat_type_map = store
        return store

    def _remember_menu(self, chat_id: str, message_id: str) -> None:
        """Track the newest menu for a chat, dropping older ones past the cap.

        Keeping a small ring (rather than just the latest) means a user who
        opens ``/menu`` twice can still click the older card for its TTL.
        """
        menus = self._menu_map()
        state = menus.setdefault(chat_id, {"ids": [], "served_at": 0.0})
        ids = state.setdefault("ids", [])
        ids.append(message_id)
        del ids[:-MAX_TRACKED_MENUS_PER_CHAT]
        state["served_at"] = time.time()

    def _menu_is_live(self, chat_id: str, message_id: str) -> bool:
        """Whether a click targets a menu this adapter served recently.

        Binds the click to the chat it was served in and to its TTL, so a
        replayed click from an old card cannot run a command.
        """
        state = self._menu_map().get(chat_id)
        if not state:
            return False
        if message_id and message_id not in (state.get("ids") or []):
            return False
        return (time.time() - float(state.get("served_at") or 0.0)) <= MENU_TTL_SECONDS

    def _forget_menu(self, chat_id: str) -> None:
        self._menu_map().pop(chat_id, None)

    # --- click handling ---

    def resolve_menu_action(
        self, *, action_value: Any, chat_id: str, message_id: str = "",
    ) -> Optional[str]:
        """Map a validated menu click onto the command it should run.

        Returns the command text, or None when the click is not a live menu
        action — the caller then lets normal card routing take over.
        """
        if not isinstance(action_value, dict):
            return None
        action = action_value.get(MENU_ACTION_KEY)
        if not isinstance(action, str) or action not in MENU_ACTIONS:
            return None
        if not self._menu_is_live(chat_id, message_id):
            logger.info(
                "[Feishu] menu click ignored: stale or unknown menu (chat=%s, msg=%s)",
                chat_id, message_id,
            )
            return None
        return MENU_ACTIONS[action]

    def resolve_menu_select(self, *, select_name: str, option: str, chat_id: str) -> Optional[str]:
        """Map a dropdown pick onto the command it should run.

        Returns None for an unknown select, an empty option, or a pick on a menu
        the adapter no longer considers live.
        """
        template = MENU_SELECTS.get(select_name)
        if not template or not option:
            logger.warning(
                "[Feishu] menu select unresolved: name=%r option=%r (chat=%s)",
                select_name, option, chat_id,
            )
            return None
        if not self._menu_is_live(chat_id, ""):
            logger.info("[Feishu] menu select ignored: stale menu (chat=%s)", chat_id)
            return None
        return template.format(option=option)

    def handle_menu_card_action(self, *, event: Any, action_value: dict[str, Any], loop: Any) -> Any:
        """Synchronous card callback for a menu click; returns the card response.

        Buttons carry their action in ``action.value``; a ``select_static`` pick
        carries the select's name in ``action.name`` and the chosen entry in
        ``action.option`` instead, so both arrive through here.
        """
        context = getattr(event, "context", None)
        chat_id = str(getattr(context, "open_chat_id", "") or "")
        message_id = str(getattr(context, "open_message_id", "") or "")
        token = str(getattr(event, "token", "") or "")
        action = getattr(event, "action", None)
        if not chat_id:
            logger.debug("[Feishu] menu click missing chat id, dropping")
            return self._card_response(toast="无法识别会话，已忽略")
        if token and self._is_card_action_duplicate(token):
            logger.debug("[Feishu] Dropping duplicate menu click token: %s", token)
            return self._card_response()

        select_name, option = decode_menu_select(action, action_value)
        if select_name:
            command = self.resolve_menu_select(
                select_name=select_name, option=option, chat_id=chat_id,
            )
        else:
            command = self.resolve_menu_action(
                action_value=action_value, chat_id=chat_id, message_id=message_id,
            )
        if not command:
            # Distinguish "the card lapsed" from "I could not read this click":
            # reporting the second as the first sends the user chasing the wrong
            # thing, and a silent return left it undiagnosable from the logs.
            if not self._menu_is_live(chat_id, message_id):
                return self._card_response(toast="!菜单已过期，请重新发送 /menu")
            logger.warning(
                "[Feishu] menu action not understood: name=%r option=%r options=%r "
                "value=%r tag=%r (chat=%s)",
                getattr(action, "name", None), getattr(action, "option", None),
                getattr(action, "options", None), getattr(action, "value", None),
                getattr(action, "tag", None), chat_id,
            )
            return self._card_response(toast="无法识别该操作，请重新发送 /menu")
        logger.info("[Feishu] menu click -> %s (chat=%s)", command, chat_id)
        # `token` is the callback token (``c-…``), NOT a message id: replying to it
        # is rejected by Feishu with 99992354 and the reply never arrives, which
        # reads as "the button does nothing". The card's own open_message_id is
        # the only value the send path can thread under.
        self._submit_on_loop(loop, self._dispatch_menu_command(event=event, command=command,
                                                              chat_id=chat_id,
                                                              message_id=message_id))
        return self._card_response(toast=f"已执行 {command}")

    async def _dispatch_menu_command(
        self, *, event: Any, command: str, chat_id: str, message_id: str = "",
    ) -> None:
        """Run the menu's command through the normal synthetic-event pipeline.

        ``message_id`` must be the card's ``open_message_id``; the send path uses
        it as the reply target, and a callback token there fails with 99992354.

        Failures are logged rather than swallowed: a button that silently does
        nothing is the hardest possible thing to debug from the chat side.
        """
        from types import SimpleNamespace
        from gateway.platforms.event import MessageType

        operator = getattr(event, "operator", None)
        open_id = str(getattr(operator, "open_id", "") or "")
        if not open_id:
            logger.info("[Feishu] menu click dropped: no operator open_id (chat=%s)", chat_id)
            return

        try:
            await self._dispatch_synthetic_event(
                text=command, message_type=MessageType.COMMAND, chat_id=chat_id,
                sender_id=SimpleNamespace(open_id=open_id, user_id=None, union_id=None),
                # Empty, never a synthetic id: the send path parents the reply on
                # whatever is here, and a made-up id is rejected with 99992354.
                event_chat_type=self._menu_chat_type(chat_id),
                raw_message=event, message_id=message_id or "",
            )
        except Exception as exc:
            logger.warning(
                "[Feishu] menu command %s failed (chat=%s): %s: %s",
                command, chat_id, type(exc).__name__, exc,
            )
            return
        logger.info("[Feishu] menu command %s dispatched (chat=%s)", command, chat_id)
