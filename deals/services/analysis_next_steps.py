from __future__ import annotations

import html
import re
from collections import Counter
from typing import Any


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
TABLE_DIVIDER_RE = re.compile(r"^:?-{3,}:?$")
HTML_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
MARKDOWN_LINK_RE = re.compile(r"\[([^]]+)]\([^)]+\)")
TASK_INTERFACE_FIELDS = ("task", "owner", "assignee", "status", "priority", "due_date")


def _clean_cell(value: str) -> str:
    value = HTML_BREAK_RE.sub("\n", value.strip())
    value = MARKDOWN_LINK_RE.sub(r"\1", value)
    value = re.sub(r"\*\*|__|`", "", value)
    return html.unescape(value).strip()


def _split_table_row(line: str) -> list[str]:
    source = line.strip()
    if source.startswith("|"):
        source = source[1:]
    if source.endswith("|") and not source.endswith(r"\|"):
        source = source[:-1]

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in source:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            cells.append(_clean_cell("".join(current)))
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    cells.append(_clean_cell("".join(current)))
    return cells


def _normalized_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _is_divider(cells: list[str]) -> bool:
    return bool(cells) and all(TABLE_DIVIDER_RE.fullmatch(cell.replace(" ", "")) for cell in cells)


def _classify_table(headers: list[str]) -> str | None:
    normalized = [_normalized_header(header) for header in headers]
    joined = " | ".join(normalized)
    has_task = any("task" in header or "next step" in header for header in normalized)
    has_workflow_columns = any(
        token in joined for token in ("owner", "assigned", "assignee", "status", "serial number")
    )
    if has_task and has_workflow_columns and len(headers) >= 3:
        return "canonical_task_table"
    if any("next step" in header or "further diligence" in header for header in normalized):
        return "section_next_steps"
    if any(header == "action" or header.startswith("action ") for header in normalized):
        return "action_table"
    return None


def _matching_index(headers: list[str], *patterns: str) -> int | None:
    normalized = [_normalized_header(header) for header in headers]
    for pattern in patterns:
        for index, header in enumerate(normalized):
            if pattern == header or pattern in header:
                return index
    return None


def _value_at(row: list[str], index: int | None) -> str | None:
    if index is None or index >= len(row):
        return None
    return row[index].strip() or None


def _normalize_task(headers: list[str], row: list[str], table_kind: str) -> dict[str, Any]:
    serial_index = _matching_index(headers, "serial number", "serial", "number", "no")
    owner_index = _matching_index(headers, "task owner", "owner")
    assignee_index = _matching_index(headers, "task assigned to", "assigned to", "assignee")
    status_index = _matching_index(headers, "status")
    priority_index = _matching_index(headers, "priority")
    due_date_index = _matching_index(headers, "due date", "deadline", "timeline")

    if table_kind == "section_next_steps":
        category_index = 0
        task_index = 1 if len(headers) > 1 else 0
    elif table_kind == "action_table":
        category_index = _matching_index(headers, "item", "category", "area")
        task_index = _matching_index(headers, "action")
    else:
        category_index = _matching_index(headers, "category", "item", "area")
        task_index = _matching_index(headers, "tasks next step", "task", "next step", "action")

    task = _value_at(row, task_index)
    normalized = {
        "serial_number": _value_at(row, serial_index),
        "category": _value_at(row, category_index),
        "task": task,
        "owner": _value_at(row, owner_index),
        "assignee": _value_at(row, assignee_index),
        "status": _value_at(row, status_index),
        "priority": _value_at(row, priority_index),
        "due_date": _value_at(row, due_date_index),
    }
    normalized["missing_fields"] = [field for field in TASK_INTERFACE_FIELDS if not normalized.get(field)]
    return normalized


def inspect_analysis_next_steps(markdown: str) -> dict[str, Any]:
    """Extract task-like Markdown tables without changing or enriching their values."""
    lines = (markdown or "").splitlines()
    sections: list[dict[str, Any]] = []
    current_section = "Document"
    table_index = 0
    line_index = 0

    while line_index < len(lines):
        heading_match = HEADING_RE.match(lines[line_index].strip())
        if heading_match:
            current_section = _clean_cell(heading_match.group(2))
            line_index += 1
            continue

        if not lines[line_index].lstrip().startswith("|"):
            line_index += 1
            continue

        start_line = line_index
        table_lines: list[str] = []
        while line_index < len(lines) and lines[line_index].lstrip().startswith("|"):
            table_lines.append(lines[line_index])
            line_index += 1

        if len(table_lines) < 2:
            continue
        headers = _split_table_row(table_lines[0])
        divider = _split_table_row(table_lines[1])
        if len(headers) != len(divider) or not _is_divider(divider):
            continue

        table_kind = _classify_table(headers)
        if not table_kind:
            continue

        table_index += 1
        tasks: list[dict[str, Any]] = []
        for offset, raw_line in enumerate(table_lines[2:], start=2):
            row = _split_table_row(raw_line)
            if not any(row):
                continue
            if len(row) < len(headers):
                row.extend([""] * (len(headers) - len(row)))
            normalized = _normalize_task(headers, row, table_kind)
            if not normalized.get("task"):
                continue
            normalized.update(
                {
                    "source_section": current_section,
                    "source_table": table_index,
                    "source_line": start_line + offset + 1,
                    "source_row": row,
                }
            )
            tasks.append(normalized)

        sections.append(
            {
                "section": current_section,
                "table_kind": table_kind,
                "table_index": table_index,
                "source_line": start_line + 1,
                "headers": headers,
                "tasks": tasks,
            }
        )

    tasks = [task for section in sections for task in section["tasks"]]
    field_coverage = {
        field: sum(1 for task in tasks if task.get(field))
        for field in TASK_INTERFACE_FIELDS
    }
    return {
        "summary": {
            "section_tables": sum(1 for section in sections if section["table_kind"] == "section_next_steps"),
            "canonical_task_tables": sum(
                1 for section in sections if section["table_kind"] == "canonical_task_table"
            ),
            "action_tables": sum(1 for section in sections if section["table_kind"] == "action_table"),
            "task_candidates": len(tasks),
            "tasks_by_section": dict(Counter(task["source_section"] for task in tasks)),
            "field_coverage": field_coverage,
        },
        "tables": sections,
        "tasks": tasks,
    }
