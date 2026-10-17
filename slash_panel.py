"""Feishu native "/" command panel for the Feishu adapter.

Ported in spirit from the dsh-im Feishu channel (xmanrui/dsh-im,
``src/channels/feishu/slash-command-registry.mjs`` + ``slash-command-panel.mjs``),
but the command catalog comes from Hermes itself instead of a hard-coded list.

dsh-im holds a frozen 33-entry manifest and registers it through the Feishu
OpenAPI.  Hermes already owns the command catalog in ``hermes_cli.commands``
(``COMMAND_REGISTRY`` + ``PluginContext.register_command``), and the Discord
adapter mirrors that registry into its native slash picker
(``plugins/platforms/discord/adapter.py``).  This module does the same for
Feishu, so a command added to Hermes shows up in the panel on the next sync
without shipping a new manifest.

Why the panel exists at all: Feishu stores the "/" list server-side per app and
never pushes it to the bot.  Without registering, the user has to know command
names by heart.  Registration is a client-side convenience ONLY — tapping an
entry sends an ordinary text message (``im.message.receive_v1``), which the
gateway's existing command dispatcher already handles.  So this module never
touches the receive path.

Reference (official):
  https://open.feishu.cn/document/mcp_open_tools/agent-best-practices/agent-supports-slash-commands

Required app scopes (tenant):
  application:app_slash_command:read
  application:app_slash_command:write
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: OpenAPI root for slash-command management (dsh-im ``SLASH_ENDPOINT``).
SLASH_ENDPOINT = "/open-apis/application/v7/app_slash_commands"

#: Documented default icon key; per-command overrides come from the catalog.
DEFAULT_SLASH_ICON = "ai-agent_outlined"

#: Error codes meaning "the app lacks application:app_slash_command:*".
MISSING_PERMISSION_CODES = frozenset({"99991640", "99991672"})

#: Feishu returns the panel in creation order (newest first, second precision).
#: Only used when a custom order has to be recreated; see ``sync_slash_panel``.
SLASH_CREATE_INTERVAL_S = 1.1

#: Feishu caps a bot's registered slash commands well below this; the cap is a
#: guard against a pathological catalog, not a platform limit.
MAX_SLASH_COMMANDS = 100

#: Platform-lifecycle probes. These answer the transport, not the user, so their
#: descriptions ("静默确认平台的 start 探测") would be nonsense in a picker.
INTERNAL_COMMANDS = frozenset({"start"})

#: Feishu description length limit (characters).
_MAX_DESC_CHARS = 200

#: Feishu command-name limit (characters); must be a plain token.
_MAX_NAME_CHARS = 24


def _normalize_name(value: Any) -> str:
    """A registerable command token: lowercase, no leading slash, bounded."""
    if not isinstance(value, str):
        return ""
    name = value.strip().lstrip("/").lower()
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        # Feishu accepts a plain token; anything with spaces/punctuation is not
        # registerable (it would still work when typed by hand).
        return ""
    return name[:_MAX_NAME_CHARS]


def _icon_for(category: str) -> str:
    """Map a Hermes command category onto a documented icon key.

    Icons are cosmetic; an unknown category falls back to the default rather
    than failing the whole registration.
    """
    value = (category or "").strip().lower()
    if not value:
        return DEFAULT_SLASH_ICON
    if "think" in value or "reason" in value:
        return "ai-deepthink_outlined"
    if "model" in value or "config" in value or "provider" in value:
        return "ai-functions_outlined"
    if "skill" in value or "plugin" in value or "preset" in value:
        return "skill_outlined"
    if "session" in value or "chat" in value:
        return "chat-ai_outlined"
    if "workspace" in value or "file" in value:
        return "folder_outlined"
    if "clear" in value or "stop" in value or "reset" in value:
        return "clear_outlined"
    return DEFAULT_SLASH_ICON


def build_slash_catalog(extra_entries: Optional[list[tuple[str, str, str]]] = None) -> list[dict[str, str]]:
    """The panel this bot should expose: ``[{command, description, icon}]``.

    Built from Hermes' own command registry — built-in commands that are
    available on the gateway, then plugin-registered ones — so the panel tracks
    ``COMMAND_REGISTRY`` instead of a frozen manifest.

    ``extra_entries`` are ``(name, description, category)`` triples appended
    after the registry (used for adapter-local commands such as ``menu``).
    Aliases are skipped: the panel shows canonical names only, since a user who
    learns ``/background`` will not need ``/bg`` spelled out beside it.
    """
    entries: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(raw_name: Any, description: Any, category: Any) -> None:
        name = _normalize_name(raw_name)
        if not name or name in seen or name in INTERNAL_COMMANDS:
            return
        seen.add(name)
        text = str(description or "").strip() or f"/{name}"
        entries.append({
            "command": name,
            "description": text[:_MAX_DESC_CHARS],
            "icon": _icon_for(str(category or "")),
        })

    try:
        from hermes_cli.commands import (
            COMMAND_REGISTRY,
            _is_gateway_available,
            _iter_plugin_command_entries,
            _resolve_config_gates,
        )

        overrides = _resolve_config_gates()
        for cmd in COMMAND_REGISTRY:
            # cli_only commands are invisible on the gateway unless their config
            # gate is on — same rule the dispatcher applies, so the panel never
            # advertises a command that would be refused.
            if _is_gateway_available(cmd, overrides):
                _add(cmd.name, cmd.describe(), cmd.category)
    except Exception as exc:  # pragma: no cover - registry is import-light but not empty
        logger.info("[Feishu] slash catalog: built-ins unavailable (%s)", exc)

    try:
        from hermes_cli.commands import _iter_plugin_command_entries

        for name, description, _args_hint in _iter_plugin_command_entries():
            _add(name, description, "Plugin")
    except Exception as exc:
        logger.debug("[Feishu] slash catalog: plugin commands unavailable (%s)", exc)

    for name, description, category in (extra_entries or []):
        _add(name, description, category)

    return entries[:MAX_SLASH_COMMANDS]


def _description_payload(entry: dict[str, str]) -> dict[str, Any]:
    """Feishu wants i18n text plus the icon on the description object."""
    return {
        "default_value": entry["description"],
        "i18n": {"zh_cn": entry["description"], "en_us": entry["description"]},
        "icon": {"icon_key": entry.get("icon") or DEFAULT_SLASH_ICON},
    }


class FeishuSlashPanelMixin:
    """Registers (and keeps in sync) the native "/" panel for this app."""

    # Provided by FeishuStreamMixin on the composed FeishuAdapter. Declared so
    # type checkers can see the cross-mixin call this mixin relies on.
    _feishu_raw_call: Any

    # Mirrored from FeishuAdapterSettings.slash_panel_enabled by
    # ``_apply_settings`` (every settings field becomes ``self._<field>``).
    _slash_panel_enabled: bool = False

    # --- catalog ---

    def _slash_panel_active(self) -> bool:
        """Whether to touch the panel: the flag is on AND a client is connected.

        Named to avoid colliding with the ``_slash_panel_enabled`` settings
        field that ``_apply_settings`` mirrors onto ``self`` as a plain bool.
        """
        if getattr(self, "_client", None) is None:
            return False
        return bool(getattr(self, "_slash_panel_enabled", False))

    def _slash_catalog(self) -> list[dict[str, str]]:
        """The panel to sync. Adapter-local ``menu`` leads, then the registry."""
        extra = [("menu", "打开功能菜单", "Session")]
        return build_slash_catalog(extra)

    # --- sync ---

    async def _sync_slash_panel_safely(self) -> None:
        """Background wrapper for the connect-time sync: log, never propagate.

        Scheduled with ``asyncio.ensure_future``, so an escaping exception would
        surface as an unretrieved-task warning instead of failing the connect.
        """
        try:
            await self.sync_slash_panel()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("[Feishu] slash panel sync aborted: %s: %s", type(exc).__name__, exc)

    async def sync_slash_panel(self) -> dict[str, Any]:
        """Best-effort create-what-is-missing against the live panel.

        Idempotent: existing command names are skipped, and a concurrent
        "already exists" is folded into that bucket rather than reported as a
        failure.  Never raises — a failed panel sync must not affect the gateway.
        """
        report: dict[str, Any] = {"created": [], "existing": [], "failed": [], "skipped": False}
        if not self._slash_panel_active():
            report["skipped"] = True
            return report

        catalog = self._slash_catalog()
        if not catalog:
            report["skipped"] = True
            return report

        listed = await self._slash_list_registered()
        if listed is None:
            report["skipped"] = True
            return report

        existing = {str(item.get("command") or "") for item in listed}
        report["existing"] = sorted(c for c in existing if c)

        for entry in catalog:
            name = entry["command"]
            if name in existing:
                continue
            ok, data, error = await self._slash_create_one(entry)
            if ok:
                report["created"].append(name)
            else:
                if "already exists" in error.lower():
                    existing.add(name)
                    report["existing"].append(name)
                    continue
                report["failed"].append({"command": name, "error": error})
                if any(code in error for code in MISSING_PERMISSION_CODES) or "permission" in error.lower():
                    # Missing application:app_slash_command:write — a batch can
                    # only fail the same way from here, so stop.
                    break
                await asyncio.sleep(0)

        if report["created"] or report["failed"]:
            logger.info(
                "[Feishu] slash panel sync: %d created, %d failed, %d already present",
                len(report["created"]), len(report["failed"]), len(report["existing"]),
            )
        return report

    async def _slash_list_registered(self) -> Optional[list[dict[str, Any]]]:
        ok, data, error = await self._feishu_raw_call("GET", SLASH_ENDPOINT)
        if not ok:
            logger.info("[Feishu] slash panel: cannot list registered commands (%s)", error)
            return None
        items = data.get("items")
        return items if isinstance(items, list) else []

    async def _slash_create_one(self, entry: dict[str, str]) -> tuple[bool, str, str]:
        ok, data, error = await self._feishu_raw_call(
            "POST",
            SLASH_ENDPOINT,
            {"command": entry["command"], "description": _description_payload(entry)},
        )
        return ok, str(data.get("command_id") or ""), error

    async def unregister_slash_panel(self) -> dict[str, Any]:
        """Remove every command this app registered (rollback path).

        Only touches names present in the current catalog, so a command someone
        else registered under the same app is left alone.
        """
        removed: list[str] = []
        if not self._slash_panel_active():
            return {"removed": [], "skipped": True}
        listed = await self._slash_list_registered()
        if not listed:
            return {"removed": [], "skipped": True}
        ours = {entry["command"] for entry in self._slash_catalog()}
        for item in listed:
            name = str(item.get("command") or "")
            command_id = item.get("command_id")
            if name not in ours or command_id is None:
                continue
            ok, _, error = await self._feishu_raw_call(
                "DELETE", f"{SLASH_ENDPOINT}/{command_id}",
            )
            if ok:
                removed.append(name)
            else:
                logger.info("[Feishu] slash panel: delete /%s failed (%s)", name, error)
        return {"removed": removed, "skipped": False}
