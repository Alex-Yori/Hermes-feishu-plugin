"""Port of dsh-im's streaming step card (任务过程 + 过程详情).

The dsh-im bridge renders the agent's working process as a single blue
"⚙️ 任务过程" card that accumulates blocks (interim notes, tool summaries,
the answer) and is updated in place while running. On completion the card is
sealed: the tool/thinking panels nest inside one collapsed "📋 过程详情"
wrapper, and any overflow spills into additional sealed cards.

This module ports the pure rendering + block-splitting logic so it can be
exercised in isolation and wired into the adapter's process lane. The
functions are faithful to ``dsh-feishu/feishu-cards.mjs``; the i18n ``t()``
helper is inlined as the Chinese strings dsh-im already ships.
"""

from __future__ import annotations

import json
import re
from typing import Any

# A single card accepts only this many bytes of content (Feishu's card content
# limit is ~30KB; 24KB leaves headroom for the header + status line).
STEP_STREAM_CARD_MAX_BYTES = 24_000

# A single card accepts only this many markdown tables (Feishu rejects the whole
# card when the table count exceeds this).
STEP_STREAM_CARD_MAX_TABLES = 5


def _plain_text(content: Any) -> dict[str, str]:
    """The ``plainText`` helper: a plain_text element."""
    return {"tag": "plain_text", "content": str(content)}


def normalize_feishu_step_card_panels(config: Any) -> dict[str, bool]:
    """Normalize the step-card panel config to ``{tools_expanded, thinking_expanded}``.

    The dsh-im normalizer (``step-push-mode.mjs``) is not in the extracted source,
    so this infers its contract from usage: the input is a mapping whose ``tools``
    / ``thinking`` entries are booleans (or the strings ``"expanded"`` /
    ``"collapsed"``), and the output is two booleans. The default is both
    collapsed, matching the "过程详情折叠" behaviour.
    """
    def _flag(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"expanded", "true", "1", "yes", "open"}
        return False

    if not isinstance(config, dict):
        return {"tools_expanded": False, "thinking_expanded": False}
    return {
        "tools_expanded": _flag(config.get("tools", config.get("toolsExpanded"))),
        "thinking_expanded": _flag(config.get("thinking", config.get("thinkingExpanded"))),
    }


def step_status_text(status: str) -> str:
    """The italic status line for a step card."""
    if status == "completed":
        return "已完成"
    if status == "stopped":
        return "已停止"
    return "运行中"


def step_panel(lines: list[str], title: str, expanded: bool) -> dict[str, Any]:
    """A collapsible panel holding the per-kind lines (dsh-im ``stepPanel``)."""
    return {
        "tag": "collapsible_panel",
        "expanded": bool(expanded),
        "background_color": "grey-50",
        "border": {"color": "grey", "corner_radius": "8px"},
        "padding": "8px 8px 8px 8px",
        "header": {
            "title": _plain_text(title),
            "vertical_align": "center",
            "padding": "8px 8px 8px 8px",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "color": "grey",
                "size": "16px 16px",
            },
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "elements": [{"tag": "markdown", "content": "\n".join(lines)}],
    }


def process_details_panel(children: list[dict[str, Any]]) -> dict[str, Any]:
    """The collapsed wrapper that holds the per-kind panels on finished turns."""
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "background_color": "grey-50",
        "border": {"color": "grey", "corner_radius": "8px"},
        "padding": "8px 8px 8px 8px",
        "header": {
            "title": _plain_text("📋 过程详情"),
            "vertical_align": "center",
            "padding": "8px 8px 8px 8px",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "color": "grey",
                "size": "16px 16px",
            },
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "elements": children,
    }


def step_stream_card(
    blocks: list[dict[str, Any]],
    status: str = "running",
    panels: dict[str, bool] | None = None,
) -> str:
    """Render the 任务过程 card (dsh-im ``stepStreamCard``).

    ``blocks`` is a list of ``{"kind": "tools"|"notes", "lines": [...],
    "omitted": int}`` or ``{"text": "..."}`` entries. While ``status`` is
    ``running`` the tool/thinking panels are pushed flat into the body; on a
    finished turn they nest inside one collapsed "📋 过程详情" wrapper. A
    ``sealed`` card carries no status line (it is an overflow spill).
    """
    panel_cfg = panels or normalize_feishu_step_card_panels(None)
    running = status == "running"
    elements: list[dict[str, Any]] = []
    panel_elements: list[dict[str, Any]] = []
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("kind") in {"tools", "notes"}:
            raw_lines = block.get("lines")
            lines = [
                line
                for line in (raw_lines if isinstance(raw_lines, list) else [])
                if isinstance(line, str) and line.strip()
            ]
            if not lines:
                continue
            omitted = block.get("omitted")
            count = len(lines) + (int(omitted) if isinstance(omitted, (int, float)) else 0)
            title = (
                f"🛠️ 工具摘要（{count}）"
                if block["kind"] == "tools"
                else f"💭 思考过程（{count}）"
            )
            expanded = running and (
                panel_cfg["tools_expanded"]
                if block["kind"] == "tools"
                else panel_cfg["thinking_expanded"]
            )
            panel = step_panel(lines, title, expanded)
            if running:
                elements.append(panel)
            else:
                panel_elements.append(panel)
            continue
        text = block.get("text")
        text = text.strip() if isinstance(text, str) else ""
        if text:
            elements.append({"tag": "markdown", "content": text})
    if not running and panel_elements:
        elements.append(process_details_panel(panel_elements))
    if not elements:
        elements.append({"tag": "markdown", "content": " "})
    if status != "sealed":
        elements.append({"tag": "markdown", "content": f"_{step_status_text(status)}_"})
    card = {
        "schema": "2.0",
        "header": {"title": _plain_text("⚙️ 任务过程"), "template": "blue"},
        "body": {"elements": elements},
    }
    return json.dumps(card, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Markdown table counting + block splitting (dsh-im ``splitStepStreamCardBlocks``)
# ---------------------------------------------------------------------------

_GFM_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_GFM_TABLE_DELIM = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")


def gfm_table_cells(line: str) -> list[str] | None:
    """Split a GFM table row into cells, or ``None`` when the line is not a row."""
    stripped = line.strip()
    if not (stripped.startswith("|") or stripped.endswith("|")):
        return None
    if "|" not in stripped[1:-1] and not stripped.startswith("|"):
        return None
    inner = stripped.strip("|")
    cells = [cell.strip() for cell in inner.split("|")]
    return cells


def markdown_table_starts(text: str) -> list[int]:
    """Offsets of the first line of each GFM table in ``text``."""
    lines = text.splitlines()
    starts: list[int] = []
    offset = 0
    for index, line in enumerate(lines):
        if gfm_table_cells(line) is not None and index + 1 < len(lines):
            if gfm_table_cells(lines[index + 1]) is not None and _GFM_TABLE_DELIM.match(
                lines[index + 1]
            ):
                starts.append(offset)
        offset += len(line) + 1
    return starts


def count_markdown_tables(text: str) -> int:
    """The number of GFM tables in ``text``."""
    return len(markdown_table_starts(text))


def split_markdown_by_table_limit(text: str, max_tables: int) -> list[str]:
    """Split ``text`` so each piece holds at most ``max_tables`` tables."""
    if max_tables <= 0 or count_markdown_tables(text) <= max_tables:
        return [text] if text else []
    starts = markdown_table_starts(text)
    pieces: list[str] = []
    cursor = 0
    table_index = 0
    for start in starts:
        table_index += 1
        if table_index > max_tables:
            pieces.append(text[cursor:start])
            cursor = start
    pieces.append(text[cursor:])
    return [piece for piece in pieces if piece.strip()]


def split_step_stream_card_blocks(
    blocks: list[dict[str, Any]],
    limit: int = STEP_STREAM_CARD_MAX_BYTES,
    max_tables: int = STEP_STREAM_CARD_MAX_TABLES,
    panels: dict[str, bool] | None = None,
) -> list[list[dict[str, Any]]]:
    """Split ``blocks`` into card-sized chunks (dsh-im ``splitStepStreamCardBlocks``).

    Each chunk is a list of blocks for one card. A chunk is sealed when adding
    the next block would exceed the byte limit or the table limit. The result
    is a list of chunks; the caller renders each with ``step_stream_card`` and
    seals all but the last (the live card).
    """
    panel_cfg = panels or normalize_feishu_step_card_panels(None)
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bytes = 0
    current_tables = 0

    def _block_bytes(block: dict[str, Any]) -> int:
        if block.get("kind") in {"tools", "notes"}:
            raw_lines = block.get("lines")
            lines = [
                line
                for line in (raw_lines if isinstance(raw_lines, list) else [])
                if isinstance(line, str) and line.strip()
            ]
            return sum(len(line.encode("utf-8")) for line in lines) + 64
        text = block.get("text")
        return len(text.encode("utf-8")) if isinstance(text, str) else 0

    def _block_tables(block: dict[str, Any]) -> int:
        if block.get("kind") in {"tools", "notes"}:
            raw_lines = block.get("lines")
            lines = [
                line
                for line in (raw_lines if isinstance(raw_lines, list) else [])
                if isinstance(line, str) and line.strip()
            ]
            return count_markdown_tables("\n".join(lines))
        text = block.get("text")
        return count_markdown_tables(text) if isinstance(text, str) else 0

    def _flush() -> None:
        nonlocal current, current_bytes, current_tables
        if current:
            chunks.append(current)
        current = []
        current_bytes = 0
        current_tables = 0

    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        block_bytes = _block_bytes(block)
        block_tables = _block_tables(block)
        if current and (
            current_bytes + block_bytes > limit or current_tables + block_tables > max_tables
        ):
            _flush()
        current.append(block)
        current_bytes += block_bytes
        current_tables += block_tables
    _flush()
    return chunks
