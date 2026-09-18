from __future__ import annotations

import csv
import json
import hashlib
import logging
import math
import re
from copy import deepcopy
from datetime import date, datetime
from typing import Any, Callable, Optional, TYPE_CHECKING

from langchain_text_splitters import RecursiveCharacterTextSplitter
from django.conf import settings
from django.core.cache import cache

from ai_orchestrator.services.bulk_prompt_contracts import (
    BULK2_INTEL_SYSTEM_PROMPT,
    build_bulk2_segment_prompt,
)
from ai_orchestrator.services.token_budget import (
    ContextBudgetExceeded,
    ModelOutputTruncated,
    estimate_tokens,
)

if TYPE_CHECKING:
    from ai_orchestrator.services.ai_processor import AIProcessorService

logger = logging.getLogger(__name__)


class DocumentArtifactCancelled(RuntimeError):
    """Raised when a caller cancels a segmented artifact build."""


class DocumentArtifactYielded(RuntimeError):
    """Raised between segments when interactive AI work is waiting."""


class DocumentArtifactService:
    """
    Builds and persists normalized document artifacts so deal synthesis can
    operate on structured evidence instead of one giant combined prompt.
    """

    DEFAULT_EVIDENCE_KEYS = {
        "document_name": "",
        "document_type": "Other",
        "document_type_suggestion": {},
        "document_summary": "",
        "claims": [],
        "metrics": [],
        "numeric_evidence": [],
        "table_definitions": [],
        "tables_summary": [],
        "contacts_found": [],
        "risks": [],
        "open_questions": [],
        "diligence_gaps": [],
        "citations": [],
        "industry_overview": {
            "findings": [],
            "market_figures": [],
            "citations": [],
        },
        "reasoning": "",
        "quality_flags": [],
        "normalized_text": "",
        "source_map": {},
        "source_metadata": {},
        "spreadsheet_profile": {},
    }
    REQUIRED_ARTIFACT_KEYS = (
        "document_name",
        "document_type",
        "document_type_suggestion",
        "document_summary",
        "claims",
        "metrics",
        "numeric_evidence",
        "table_definitions",
        "tables_summary",
        "contacts_found",
        "risks",
        "open_questions",
        "diligence_gaps",
        "citations",
        "industry_overview",
        "quality_flags",
        "normalized_text",
        "source_map",
    )
    STATUS_COMPLETE = "complete"
    STATUS_PARTIAL = "partial"
    STATUS_DEGRADED = "degraded"
    STATUS_FAILED = "failed"
    STATUS_MISSING = "missing"
    ARTIFACT_PIPELINE_VERSION = "vdr-bulk2-segment-artifact-v3"
    MIN_SUBDIVISION_SOURCE_TOKENS = 256
    MAX_SUBDIVISION_DEPTH = 8
    SPREADSHEET_EMBEDDING_ROWS_PER_CHUNK = 8
    SPREADSHEET_EMBEDDING_CHARS_PER_CHUNK = 900
    # The evidence prompt and output schema consume substantial context. Compact
    # spreadsheet segments stay below this source cap while retaining every
    # populated cell across as many full-fidelity requests as necessary.
    SPREADSHEET_ARTIFACT_SOURCE_TOKENS = 6_000
    SPREADSHEET_ARTIFACT_OUTPUT_TOKENS = 16_384

    @staticmethod
    def _spreadsheet_coordinate(value: Any) -> tuple[str, int] | None:
        match = re.fullmatch(r"([A-Za-z]{1,4})([1-9]\d*)", str(value or "").strip())
        if not match:
            return None
        return match.group(1).upper(), int(match.group(2))

    @staticmethod
    def _spreadsheet_column_number(label: str) -> int:
        number = 0
        for character in str(label or "").upper():
            if not "A" <= character <= "Z":
                return 0
            number = number * 26 + ord(character) - ord("A") + 1
        return number

    @staticmethod
    def _spreadsheet_column_label(number: int) -> str:
        label = ""
        while number > 0:
            number, remainder = divmod(number - 1, 26)
            label = chr(ord("A") + remainder) + label
        return label

    @classmethod
    def spreadsheet_manifest_from_text(
        cls,
        *,
        file_name: str,
        text: str,
    ) -> dict[str, Any]:
        """Rebuild exact cell coordinates from legacy rendered spreadsheet text.

        Native XLSX text uses ``A1=value`` tokens. Calamine-backed legacy
        formats use ``row_number<TAB>value...``. CSV/TSV sources are parsed
        directly when no sheet markers are present.
        """
        source = str(text or "").strip()
        if not source:
            return {}

        sheet_marker = re.compile(r"^(?:\[Sheet:\s*(.+?)\]|##\s*SHEET:\s*(.+?))$", re.I)
        coordinate_token = re.compile(r"^([A-Za-z]{1,4}[1-9]\d*)=(.*)$", re.S)
        cached_suffix = re.compile(r"^(.*?)\s*\[cached value:\s*(.*?)\]\s*$", re.S | re.I)
        sheets: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None

        def ensure_sheet(name: str = "Sheet1") -> dict[str, Any]:
            nonlocal current
            if current is None:
                current = {"name": name, "cells": []}
                sheets.append(current)
            return current

        for raw_line in source.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            marker = sheet_marker.match(line)
            if marker:
                current = {"name": (marker.group(1) or marker.group(2)).strip(), "cells": []}
                sheets.append(current)
                continue

            fields = raw_line.split("\t")
            coordinate_cells = []
            for field in fields:
                match = coordinate_token.match(field.strip())
                if not match:
                    coordinate_cells = []
                    break
                value = match.group(2)
                cached = cached_suffix.match(value)
                cell = {"coordinate": match.group(1).upper(), "value": value}
                if cached:
                    cell["value"] = cached.group(1)
                    if cached.group(2).lower() != "unavailable":
                        cell["cached_value"] = cached.group(2)
                coordinate_cells.append(cell)
            if coordinate_cells:
                ensure_sheet()["cells"].extend(coordinate_cells)
                continue

            if len(fields) > 1 and fields[0].strip().isdigit():
                row_number = int(fields[0].strip())
                target = ensure_sheet()
                for column_number, value in enumerate(fields[1:], start=1):
                    if value == "":
                        continue
                    target["cells"].append({
                        "coordinate": f"{cls._spreadsheet_column_label(column_number)}{row_number}",
                        "value": value,
                    })

        extension = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
        if not any(sheet.get("cells") for sheet in sheets) and extension in {"csv", "tsv"}:
            delimiter = "\t" if extension == "tsv" else ","
            current = {"name": "Sheet1", "cells": []}
            sheets = [current]
            for row_number, row in enumerate(csv.reader(source.splitlines(), delimiter=delimiter), start=1):
                for column_number, value in enumerate(row, start=1):
                    if value == "":
                        continue
                    current["cells"].append({
                        "coordinate": f"{cls._spreadsheet_column_label(column_number)}{row_number}",
                        "value": value,
                    })

        populated_sheets = []
        for sheet in sheets:
            coordinates = [
                cls._spreadsheet_coordinate(cell.get("coordinate"))
                for cell in sheet.get("cells") or []
            ]
            coordinates = [coordinate for coordinate in coordinates if coordinate]
            if not coordinates:
                continue
            sheet["row_count"] = max(row for _, row in coordinates)
            sheet["column_count"] = max(cls._spreadsheet_column_number(column) for column, _ in coordinates)
            populated_sheets.append(sheet)
        if not populated_sheets:
            return {}

        manifest = {
            "schema_version": "2",
            "kind": "spreadsheet",
            "format": extension,
            "filename": file_name,
            "sheets": populated_sheets,
            "fallback_fidelity": "reconstructed_from_stored_text",
            "content_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        }
        manifest["chunks"] = cls._spreadsheet_manifest_chunks(manifest, base_metadata={})
        return manifest

    @classmethod
    def _spreadsheet_manifest_chunks(
        cls,
        manifest: dict[str, Any],
        *,
        base_metadata: dict[str, Any],
        number_format_mode: str = "full",
    ) -> list[dict[str, Any]]:
        """Build bounded retrieval units from the exact workbook cell manifest."""
        chunks: list[dict[str, Any]] = []
        for sheet in manifest.get("sheets") or []:
            if not isinstance(sheet, dict):
                continue
            sheet_name = str(sheet.get("name") or "").strip()
            cells_by_row: dict[int, list[tuple[str, dict[str, Any]]]] = {}
            for cell in sheet.get("cells") or []:
                if not isinstance(cell, dict):
                    continue
                parsed = cls._spreadsheet_coordinate(cell.get("coordinate"))
                if not parsed:
                    continue
                if not cls._spreadsheet_cell_has_content(cell):
                    continue
                column, row_number = parsed
                cells_by_row.setdefault(row_number, []).append((column, cell))
            if not cells_by_row:
                continue

            window: list[tuple[int, str, str, str]] = []
            window_chars = 0

            def flush() -> None:
                nonlocal window, window_chars
                if not window:
                    return
                row_start = window[0][0]
                row_end = window[-1][0]
                columns = [
                    column
                    for _, column_start, column_end, _ in window
                    for column in (column_start, column_end)
                ]
                column_start = min(columns, key=cls._spreadsheet_column_number)
                column_end = max(columns, key=cls._spreadsheet_column_number)
                chunks.append({
                    "text": "\n".join(item[3] for item in window),
                    "metadata": {
                        **base_metadata,
                        "chunk_kind": "spreadsheet_cells",
                        "sheet_name": sheet_name,
                        "row_start": row_start,
                        "row_end": row_end,
                        "column_start": column_start,
                        "column_end": column_end,
                        "cell_range": f"{column_start}{row_start}:{column_end}{row_end}",
                        "extraction_schema_version": manifest.get("schema_version", "1"),
                    },
                })
                window = []
                window_chars = 0

            for row_number in sorted(cells_by_row):
                row_cells = sorted(
                    cells_by_row[row_number],
                    key=lambda item: cls._spreadsheet_column_number(item[0]),
                )
                rendered_cells = []
                for column, cell in row_cells:
                    coordinate = f"{column}{row_number}"
                    raw_value = cell.get("value")
                    cached_value = cell.get("cached_value")
                    rendered = f"{coordinate}={cls._compact_spreadsheet_value(raw_value)}"
                    if (
                        cached_value not in (None, "")
                        and str(cached_value) != str(raw_value)
                    ):
                        rendered += f" [calculated: {cls._compact_spreadsheet_value(cached_value)}]"
                    number_format = cell.get("number_format")
                    if number_format_mode == "semantic":
                        number_format = cls._semantic_spreadsheet_format(number_format)
                    elif number_format_mode == "none":
                        number_format = ""
                    if number_format not in (None, "", "General"):
                        rendered += f" [format: {cls._compact_spreadsheet_value(number_format)}]"
                    if cell.get("hyperlink"):
                        rendered += f" [link: {cls._compact_spreadsheet_value(cell['hyperlink'])}]"
                    if cell.get("comment"):
                        rendered += f" [comment: {cls._compact_spreadsheet_value(cell['comment'])}]"
                    rendered_cells.append(rendered)
                row_text = " | ".join(rendered_cells)
                row_start_column = row_cells[0][0]
                row_end_column = row_cells[-1][0]
                would_exceed = (
                    window
                    and (
                        len(window) >= cls.SPREADSHEET_EMBEDDING_ROWS_PER_CHUNK
                        or window_chars + len(row_text) + 1 > cls.SPREADSHEET_EMBEDDING_CHARS_PER_CHUNK
                    )
                )
                if would_exceed:
                    flush()
                window.append((row_number, row_start_column, row_end_column, row_text))
                window_chars += len(row_text) + 1
            flush()
        return chunks

    @staticmethod
    def _compact_spreadsheet_value(value: Any) -> str:
        lines = [
            re.sub(r"[ \t]+", " ", line).strip()
            for line in str(value if value is not None else "").splitlines()
        ]
        return " ⏎ ".join(line for line in lines if line).strip()

    @classmethod
    def _spreadsheet_cell_has_content(cls, cell: dict[str, Any]) -> bool:
        return any(
            cls._compact_spreadsheet_value(cell.get(field))
            for field in ("value", "cached_value", "hyperlink", "comment")
        )

    @staticmethod
    def _semantic_spreadsheet_format(value: Any) -> str:
        """Collapse verbose Excel display codes to evidence-relevant semantics."""
        number_format = str(value or "").strip()
        if not number_format or number_format.lower() == "general":
            return ""
        lowered = number_format.lower()
        labels = []
        if "%" in number_format:
            labels.append("percent")
        currency_symbols = "".join(
            symbol for symbol in ("₹", "$", "€", "£", "¥") if symbol in number_format
        )
        if currency_symbols or "[$" in number_format:
            labels.append(f"currency {currency_symbols}".strip())
        # Excel date/time formats use these tokens; numeric/financial formats
        # without a year/day token are intentionally not labelled as dates.
        if re.search(r"(?:^|[^a-z])[dmy]{1,4}(?:[^a-z]|$)", lowered):
            labels.append("date")
        elif re.search(r"(?:^|[^a-z])[hs]{1,2}(?:[^a-z]|$)", lowered):
            labels.append("time")
        return ", ".join(dict.fromkeys(labels))

    @classmethod
    def _manifest_has_spreadsheet_cell_contract(cls, manifest: Any) -> bool:
        if not isinstance(manifest, dict) or manifest.get("kind") != "spreadsheet":
            return False
        sheets = manifest.get("sheets")
        return isinstance(sheets, list) and all(
            isinstance(sheet, dict) and isinstance(sheet.get("cells"), list)
            for sheet in sheets
        )

    @classmethod
    def compact_spreadsheet_text(
        cls,
        *,
        file_name: str,
        manifest: dict[str, Any],
    ) -> str:
        """Render a coordinate-stable workbook without blank row/cell padding."""
        chunks = cls._spreadsheet_manifest_chunks(
            manifest,
            base_metadata={},
            number_format_mode="semantic",
        )
        chunks_by_sheet: dict[str, list[dict[str, Any]]] = {}
        for chunk in chunks:
            sheet_name = str(chunk["metadata"].get("sheet_name") or "Unknown Sheet")
            chunks_by_sheet.setdefault(sheet_name, []).append(chunk)

        lines = [f"# WORKBOOK: {file_name}"]
        if manifest.get("content_sha256"):
            lines.append(f"SHA256: {manifest['content_sha256']}")
        for sheet in manifest.get("sheets") or []:
            if not isinstance(sheet, dict):
                continue
            sheet_name = str(sheet.get("name") or "Unknown Sheet")
            lines.extend(["", f"## SHEET: {sheet_name}"])
            sheet_chunks = chunks_by_sheet.get(sheet_name) or []
            if not sheet_chunks:
                lines.append("EMPTY: no populated cells")
                continue
            populated_rows = {
                row
                for cell in sheet.get("cells") or []
                if isinstance(cell, dict) and cls._spreadsheet_cell_has_content(cell)
                for parsed in [cls._spreadsheet_coordinate(cell.get("coordinate"))]
                if parsed
                for row in [parsed[1]]
            }
            lines.append(f"POPULATED ROWS: {len(populated_rows)}")
            lines.extend(chunk["text"] for chunk in sheet_chunks)
        return "\n".join(lines).strip()

    @classmethod
    def normalize_source_text(
        cls,
        *,
        file_name: str,
        extracted_text: str,
        extraction_manifest: dict[str, Any] | None = None,
    ) -> str:
        if cls._manifest_has_spreadsheet_cell_contract(extraction_manifest):
            return cls.compact_spreadsheet_text(
                file_name=file_name,
                manifest=extraction_manifest or {},
            )
        return (extracted_text or "").strip()

    @classmethod
    def _spreadsheet_artifact_segments(
        cls,
        *,
        file_name: str,
        manifest: dict[str, Any],
        source_tokens: int,
    ) -> list[str]:
        """Build sheet-aware inference segments from populated manifest cells only."""
        source_cap = max(256, min(
            int(source_tokens),
            int(getattr(
                settings,
                "VDR_ARTIFACT_SPREADSHEET_SOURCE_TOKENS",
                cls.SPREADSHEET_ARTIFACT_SOURCE_TOKENS,
            )),
        ))
        cell_chunks = cls._spreadsheet_manifest_chunks(
            manifest,
            base_metadata={},
            number_format_mode="semantic",
        )
        if not cell_chunks:
            empty_sheets = [
                str(sheet.get("name") or "Unknown Sheet")
                for sheet in manifest.get("sheets") or []
                if isinstance(sheet, dict)
            ]
            sheet_lines = "\n".join(
                f"[SHEET: {name}]\nEMPTY: no populated cells"
                for name in empty_sheets
            )
            return [f"[WORKBOOK: {file_name}]\n{sheet_lines}".strip()]

        segments: list[str] = []
        pending: list[dict[str, Any]] = []
        pending_sheet = ""

        def render(items: list[dict[str, Any]]) -> str:
            metadata = [item["metadata"] for item in items]
            row_start = min(int(item["row_start"]) for item in metadata)
            row_end = max(int(item["row_end"]) for item in metadata)
            column_start = min(
                (str(item["column_start"]) for item in metadata),
                key=cls._spreadsheet_column_number,
            )
            column_end = max(
                (str(item["column_end"]) for item in metadata),
                key=cls._spreadsheet_column_number,
            )
            header = (
                f"[WORKBOOK: {file_name}]\n"
                f"[SHEET: {pending_sheet}]\n"
                f"[RANGE: {column_start}{row_start}:{column_end}{row_end}]\n"
                "[POPULATED CELLS ONLY; BLANK CELLS AND ROWS OMITTED]\n"
                "[CELLS]\n"
            )
            return header + "\n".join(item["text"] for item in items)

        def flush() -> None:
            nonlocal pending
            if pending:
                segments.append(render(pending))
                pending = []

        for chunk in cell_chunks:
            sheet_name = str(chunk["metadata"].get("sheet_name") or "Unknown Sheet")
            if pending and sheet_name != pending_sheet:
                flush()
            pending_sheet = sheet_name
            candidate = [*pending, chunk]
            if pending and estimate_tokens(render(candidate)) > source_cap:
                flush()
                pending_sheet = sheet_name
            pending.append(chunk)
        flush()
        return segments

    @classmethod
    def _artifact_source_segments(
        cls,
        *,
        file_name: str,
        extracted_text: str,
        extraction_manifest: dict[str, Any] | None,
        source_tokens: int,
        overlap_tokens: int,
    ) -> list[str]:
        if cls._manifest_has_spreadsheet_cell_contract(extraction_manifest):
            return cls._spreadsheet_artifact_segments(
                file_name=file_name,
                manifest=extraction_manifest or {},
                source_tokens=source_tokens,
            )
        return cls._split_for_artifact(
            extracted_text,
            source_tokens=source_tokens,
            overlap_tokens=overlap_tokens,
        )

    @staticmethod
    def _segment_artifact_usable(artifact: Any) -> bool:
        if not isinstance(artifact, dict) or not str(artifact.get("document_summary") or "").strip():
            return False
        invalid_flags = {
            "fallback_artifact",
            "artifact_missing_text",
            "artifact_segment_processing_incomplete",
        }
        return not invalid_flags.intersection(artifact.get("quality_flags") or [])

    @classmethod
    def _normalize_segment_artifact(
        cls,
        artifact: dict[str, Any],
        *,
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        """Normalize segment evidence without requiring duplicated source text."""
        normalized = cls._normalize_artifact(artifact, fallback=fallback)
        normalized["normalized_text"] = ""
        normalized["quality_flags"] = [
            flag
            for flag in normalized.get("quality_flags") or []
            if flag != "artifact_missing_text"
        ]
        return normalized

    @staticmethod
    def _process_segment(service, **kwargs):
        try:
            return service.process_content(**kwargs)
        except Exception as exc:
            # Prompt preparation can fail after audit creation but before the
            # model response handler takes responsibility for terminal state.
            metadata = kwargs["metadata"]["_source_metadata"]
            if metadata.get("artifact_run_id"):
                from ai_orchestrator.models import AIAuditLog
                from django.utils import timezone
                AIAuditLog.objects.filter(
                    source_type="document_evidence_segment", status__in=["PENDING", "PROCESSING"],
                    source_metadata__artifact_segment_cache_key=metadata["artifact_segment_cache_key"],
                ).update(status="FAILED", is_success=False, error_message=str(exc), completed_at=timezone.now())
            raise

    @classmethod
    def prepare_artifact_run_metadata(
        cls,
        extracted_text: str,
        source_metadata: dict[str, Any] | None = None,
        extraction_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Freeze every input that determines segment identity for one artifact run."""
        raw_text = (extracted_text or "").strip()
        prepared = json.loads(json.dumps(source_metadata or {}, default=str))
        try:
            from ai_orchestrator.services.runtime import AIRuntimeService

            artifact_model = str(
                prepared.get("artifact_model")
                or AIRuntimeService.get_text_model(AIRuntimeService.get_default_personality())
            )
        except Exception:
            artifact_model = str(
                prepared.get("artifact_model")
                or getattr(settings, "VLLM_MODEL", "default")
            )

        source_token_budget = max(4_000, int(
            prepared.get("artifact_segment_source_token_budget")
            or getattr(settings, "VDR_ARTIFACT_SEGMENT_SOURCE_TOKENS", 10_000)
        ))
        stored_overlap_token_budget = prepared.get("artifact_segment_overlap_token_budget")
        overlap_token_budget = max(0, min(
            int(
                stored_overlap_token_budget
                if stored_overlap_token_budget is not None
                else getattr(settings, "VDR_ARTIFACT_SEGMENT_OVERLAP_TOKENS", 768)
            ),
            source_token_budget // 4,
        ))
        prepared.update({
            "artifact_pipeline_version": cls.ARTIFACT_PIPELINE_VERSION,
            "artifact_model": artifact_model,
            "artifact_source_sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
            "artifact_segment_source_token_budget": source_token_budget,
            "artifact_segment_overlap_token_budget": overlap_token_budget,
        })
        if cls._manifest_has_spreadsheet_cell_contract(extraction_manifest):
            manifest_payload = json.dumps(
                extraction_manifest,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            prepared.update({
                "artifact_source_kind": "spreadsheet_cells",
                "artifact_segment_effective_source_token_budget": min(
                    source_token_budget,
                    int(getattr(
                        settings,
                        "VDR_ARTIFACT_SPREADSHEET_SOURCE_TOKENS",
                        cls.SPREADSHEET_ARTIFACT_SOURCE_TOKENS,
                    )),
                ),
                "artifact_segment_effective_output_token_budget": min(
                    int(getattr(settings, "VDR_ARTIFACT_SEGMENT_MAX_TOKENS", 32_768)),
                    int(getattr(
                        settings,
                        "VDR_ARTIFACT_SPREADSHEET_MAX_TOKENS",
                        cls.SPREADSHEET_ARTIFACT_OUTPUT_TOKENS,
                    )),
                ),
                "artifact_manifest_sha256": hashlib.sha256(
                    manifest_payload.encode("utf-8")
                ).hexdigest(),
            })
        return prepared

    @classmethod
    def begin_document_artifact_run(
        cls,
        document: Any,
        source_metadata: dict[str, Any],
        extraction_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist the exact extraction and splitter contract before inference starts."""
        manifest = extraction_manifest or getattr(document, "extraction_manifest", None)
        text = cls.normalize_source_text(
            file_name=document.title,
            extracted_text=document.normalized_text or document.extracted_text or "",
            extraction_manifest=manifest,
        )
        prepared = cls.prepare_artifact_run_metadata(
            text,
            source_metadata,
            extraction_manifest=manifest,
        )
        segments = cls._artifact_source_segments(
            file_name=document.title,
            extracted_text=text,
            extraction_manifest=manifest,
            source_tokens=prepared["artifact_segment_source_token_budget"],
            overlap_tokens=prepared["artifact_segment_overlap_token_budget"],
        )
        checkpoint = cls._fallback_artifact(
            file_name=document.title,
            extracted_text=text,
            document_type=document.document_type,
            extraction_mode=document.extraction_mode,
        )
        checkpoint["quality_flags"] = [
            "fallback_artifact",
            "artifact_segment_processing_incomplete",
        ]
        checkpoint["source_metadata"] = {
            **prepared,
            "artifact_segment_count": len(segments),
            "artifact_segments_completed": 0,
        }
        cls.persist_artifact(document, checkpoint)
        return prepared

    @classmethod
    def build_document_artifact(
        cls,
        *,
        file_name: str,
        extracted_text: str,
        document_type: str = "Other",
        extraction_mode: str | None = None,
        ai_service: Optional["AIProcessorService"] = None,
        source_metadata: dict[str, Any] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        yield_check: Callable[[], bool] | None = None,
        segment_progress: Callable[[int, int], None] | None = None,
        force_fresh: bool = False,
        extraction_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw_text = cls.normalize_source_text(
            file_name=file_name,
            extracted_text=extracted_text,
            extraction_manifest=extraction_manifest,
        )
        source_metadata = cls.prepare_artifact_run_metadata(
            raw_text,
            source_metadata,
            extraction_manifest=extraction_manifest,
        )
        # Run-scoped keys already isolate a fresh run from earlier results.
        # Retrying that run must still recover its own completed segments.
        force_fresh = force_fresh and not bool(source_metadata.get("artifact_run_id"))
        artifact_model = source_metadata["artifact_model"]
        source_token_budget = source_metadata["artifact_segment_source_token_budget"]
        overlap_token_budget = source_metadata["artifact_segment_overlap_token_budget"]
        spreadsheet_cell_source = cls._manifest_has_spreadsheet_cell_contract(
            extraction_manifest
        )
        output_token_budget = int(
            source_metadata.get("artifact_segment_effective_output_token_budget")
            if spreadsheet_cell_source
            else getattr(settings, "VDR_ARTIFACT_SEGMENT_MAX_TOKENS", 32_768)
        )
        fallback = cls._fallback_artifact(
            file_name=file_name,
            extracted_text=raw_text,
            document_type=document_type,
            extraction_mode=extraction_mode,
        )
        fallback["source_metadata"] = deepcopy(source_metadata)

        if not raw_text:
            return fallback

        if ai_service is None:
            from ai_orchestrator.services.ai_processor import AIProcessorService
            service = AIProcessorService()
        else:
            service = ai_service

        # Match bulk_2's lossless map/merge contract: every bounded segment is
        # analyzed, cached by content, and merged. The full extracted source is
        # retained separately and is never replaced by an LLM-cleaned excerpt.
        segments = cls._artifact_source_segments(
            file_name=file_name,
            extracted_text=raw_text,
            extraction_manifest=extraction_manifest,
            source_tokens=source_token_budget,
            overlap_tokens=overlap_token_budget,
        )
        segment_artifacts: list[dict[str, Any] | None] = [None] * len(segments)
        failures: list[str] = []

        def analyze_segment(index: int, segment: str) -> tuple[int, dict[str, Any], bool]:
            if cancel_check and cancel_check():
                raise DocumentArtifactCancelled("Document artifact processing was cancelled.")
            cache_key = cls._segment_cache_key(
                file_name=file_name,
                segment=segment,
                index=index,
                total=len(segments),
                model=artifact_model,
                run_scope=str(source_metadata.get("artifact_run_id") or ""),
            )
            cached = None
            if not force_fresh:
                try:
                    cached = cache.get(cache_key)
                except Exception:
                    cached = None
                if cls._segment_artifact_usable(cached):
                    return index, cached, True
                if cached:
                    try:
                        cache.delete(cache_key)
                    except Exception:
                        pass

            # A worker can exit after saving the completed model audit but
            # before writing Redis. Recover that response using the same
            # content/model/run checksum instead of sending it again.
            if not force_fresh and source_metadata.get("artifact_run_id"):
                from ai_orchestrator.models import AIAuditLog
                completed = AIAuditLog.objects.filter(
                    source_type="document_evidence_segment", status="COMPLETED", is_success=True,
                    source_metadata__artifact_segment_cache_key=cache_key,
                ).order_by("-completed_at").values_list("parsed_json", flat=True).first()
                if isinstance(completed, dict):
                    recovered = cls._normalize_segment_artifact(completed, fallback=fallback)
                    if cls._segment_artifact_usable(recovered):
                        return index, recovered, True

            # Yield only after all durable recovery checks. This ensures a
            # resumed document walks past completed checkpoints without
            # blocking interactive work or repeating model inference.
            if yield_check and yield_check():
                raise DocumentArtifactYielded(
                    "Higher-priority AI work is waiting; yielding before the next VDR segment."
                )

            segment_context = {
                "document_name": file_name,
                "heuristic_document_type": document_type,
                "spreadsheet_profile": source_metadata.get("spreadsheet_profile") or {},
                "source_metadata": source_metadata,
                "phase1_quality_flags": source_metadata.get("quality_flags") or [],
                "segment_index": index + 1,
                "segment_count": len(segments),
                "source_location": f"{file_name} | segment {index + 1}/{len(segments)}",
                "segment_metadata": {},
                "segment_estimated_tokens": estimate_tokens(segment),
                "segment_source_token_budget": int(
                    source_metadata.get("artifact_segment_effective_source_token_budget")
                    or getattr(settings, "VDR_ARTIFACT_SEGMENT_SOURCE_TOKENS", 10_000)
                ),
            }
            metadata = {
                "document_name": file_name,
                "document_type": document_type,
                "source_metadata_json": json.dumps(source_metadata, default=str),
                "_source_metadata": {
                    **source_metadata,
                    "segment_index": index,
                    "segment_count": len(segments),
                    "segment_estimated_tokens": estimate_tokens(segment),
                    "segment_input_token_budget": int(
                        getattr(settings, "VDR_ARTIFACT_SEGMENT_INPUT_TOKENS", 14_336)
                    ),
                    "segment_output_token_budget": int(
                        output_token_budget
                    ),
                    "artifact_pipeline_version": cls.ARTIFACT_PIPELINE_VERSION,
                    "artifact_segment_cache_key": cache_key,
                },
                "context_label": f"Document Evidence: {file_name} [{index + 1}/{len(segments)}]",
                "segment_index": index,
                "segment_count": len(segments),
                "chat_template_kwargs": {"enable_thinking": False},
                "max_input_tokens": int(
                    getattr(settings, "VDR_ARTIFACT_SEGMENT_INPUT_TOKENS", 14_336)
                ),
                "max_tokens": output_token_budget,
                "request_timeout": int(getattr(settings, "VDR_ARTIFACT_SEGMENT_TIMEOUT", 1800)),
                "enforce_context_budget": True,
                "lossless_input": True,
                "serialize_inference": True,
                "celery_task_id": source_metadata.get("celery_task_id"),
                "vdr_parent_audit_id": source_metadata.get("vdr_parent_audit_id"),
            }
            def run_model(
                source_segment: str,
                *,
                piece_cache_key: str = cache_key,
                subdivision_path: tuple[int, ...] = (),
                subdivision_count: int | None = None,
            ):
                attempt_metadata = deepcopy(metadata)
                attempt_context = deepcopy(segment_context)
                attempt_metadata["_source_metadata"]["lossless_input"] = True
                if subdivision_path and subdivision_count is not None:
                    path_label = ".".join(str(item + 1) for item in subdivision_path)
                    attempt_metadata["_source_metadata"].update({
                        "artifact_parent_segment_cache_key": cache_key,
                        "artifact_segment_cache_key": piece_cache_key,
                        "segment_subdivision_index": subdivision_path[-1],
                        "segment_subdivision_count": subdivision_count,
                        "segment_subdivision_depth": len(subdivision_path),
                        "segment_subdivision_path": [item + 1 for item in subdivision_path],
                        "segment_estimated_tokens": estimate_tokens(source_segment),
                    })
                    attempt_metadata["context_label"] += (
                        f" [full subdivision {path_label}; {subdivision_path[-1] + 1}/{subdivision_count}]"
                    )
                    attempt_context.update({
                        "source_location": (
                            f"{file_name} | segment {index + 1}/{len(segments)} | "
                            f"subsegment {path_label}"
                        ),
                        "segment_metadata": {
                            "parent_segment_index": index + 1,
                            "subsegment_index": subdivision_path[-1] + 1,
                            "subsegment_count": subdivision_count,
                            "subsegment_depth": len(subdivision_path),
                            "subsegment_path": [item + 1 for item in subdivision_path],
                        },
                        "segment_estimated_tokens": estimate_tokens(source_segment),
                    })
                return cls._process_segment(
                    service,
                    content=(
                        f"{BULK2_INTEL_SYSTEM_PROMPT}\n\n"
                        f"{build_bulk2_segment_prompt(segment=source_segment, context=attempt_context)}"
                    ),
                    skill_name="document_evidence_extraction",
                    source_type="document_evidence_segment",
                    source_id=str(source_metadata.get("source_id") or file_name),
                    metadata=attempt_metadata,
                    model_override=artifact_model,
                )

            def normalize_result(result: Any) -> dict[str, Any]:
                parsed = result.get("parsed_json") if isinstance(result, dict) and "parsed_json" in result else result
                if not isinstance(parsed, dict) or parsed.get("error"):
                    raise RuntimeError(
                        str(parsed.get("error") if isinstance(parsed, dict) else "AI segment response was invalid.")
                    )
                normalized = cls._normalize_segment_artifact(parsed, fallback=fallback)
                normalized["reasoning"] = (
                    result.get("thinking") or normalized.get("reasoning") or ""
                    if isinstance(result, dict)
                    else ""
                )
                if not cls._segment_artifact_usable(normalized):
                    raise RuntimeError("AI segment response did not produce a complete evidence artifact.")
                return normalized

            def subdivision_cache_key(path: tuple[int, ...], source_segment: str) -> str:
                fingerprint = json.dumps(
                    [cls.ARTIFACT_PIPELINE_VERSION, cache_key, list(path), source_segment],
                    ensure_ascii=False,
                )
                return "vdr-document-artifact-subsegment:" + hashlib.sha256(
                    fingerprint.encode("utf-8")
                ).hexdigest()

            def recover_piece(piece_cache_key: str) -> dict[str, Any] | None:
                if force_fresh:
                    return None
                try:
                    stored = cache.get(piece_cache_key)
                except Exception:
                    stored = None
                if cls._segment_artifact_usable(stored):
                    return stored
                if stored:
                    try:
                        cache.delete(piece_cache_key)
                    except Exception:
                        pass
                if not source_metadata.get("artifact_run_id"):
                    return None
                from ai_orchestrator.models import AIAuditLog
                completed = AIAuditLog.objects.filter(
                    source_type="document_evidence_segment",
                    status="COMPLETED",
                    is_success=True,
                    source_metadata__artifact_segment_cache_key=piece_cache_key,
                ).order_by("-completed_at").values_list("parsed_json", flat=True).first()
                if not isinstance(completed, dict):
                    return None
                recovered = cls._normalize_segment_artifact(completed, fallback=fallback)
                return recovered if cls._segment_artifact_usable(recovered) else None

            def cache_piece(piece_cache_key: str, piece_artifact: dict[str, Any]) -> None:
                try:
                    cache.set(
                        piece_cache_key,
                        piece_artifact,
                        timeout=int(getattr(settings, "VDR_ARTIFACT_CACHE_TTL", 30 * 24 * 60 * 60)),
                    )
                except Exception:
                    pass

            def should_subdivide(exc: Exception) -> bool:
                return isinstance(exc, (ContextBudgetExceeded, ModelOutputTruncated)) or (
                    "finish_reason=length" in str(exc)
                )

            def analyze_piece(
                source_segment: str,
                *,
                path: tuple[int, ...] = (),
                sibling_count: int | None = None,
            ) -> dict[str, Any]:
                piece_cache_key = cache_key if not path else subdivision_cache_key(path, source_segment)
                if path:
                    recovered = recover_piece(piece_cache_key)
                    if recovered is not None:
                        return recovered
                    if cancel_check and cancel_check():
                        raise DocumentArtifactCancelled("Document artifact processing was cancelled.")
                    if yield_check and yield_check():
                        raise DocumentArtifactYielded(
                            "Higher-priority AI work is waiting; yielding before the next VDR subdivision."
                        )
                try:
                    artifact = normalize_result(run_model(
                        source_segment,
                        piece_cache_key=piece_cache_key,
                        subdivision_path=path,
                        subdivision_count=sibling_count,
                    ))
                except Exception as exc:
                    if not should_subdivide(exc):
                        raise
                    if len(path) >= cls.MAX_SUBDIVISION_DEPTH:
                        raise RuntimeError(
                            f"Lossless evidence subdivision reached depth {cls.MAX_SUBDIVISION_DEPTH} "
                            f"without fitting the model context: {exc}"
                        ) from exc
                    estimated_piece_tokens = estimate_tokens(source_segment)
                    subdivision_source_tokens = max(
                        cls.MIN_SUBDIVISION_SOURCE_TOKENS,
                        estimated_piece_tokens // 2,
                    )
                    if subdivision_source_tokens >= estimated_piece_tokens:
                        subdivision_source_tokens = max(
                            cls.MIN_SUBDIVISION_SOURCE_TOKENS,
                            estimated_piece_tokens - 1,
                        )
                    subdivisions = cls._split_for_subdivision(
                        source_segment,
                        source_tokens=subdivision_source_tokens,
                        overlap_tokens=min(
                            overlap_token_budget,
                            max(0, subdivision_source_tokens // 8),
                        ),
                        minimum_source_tokens=cls.MIN_SUBDIVISION_SOURCE_TOKENS,
                    )
                    if (
                        len(subdivisions) < 2
                        or any(len(subdivision) >= len(source_segment) for subdivision in subdivisions)
                    ):
                        raise RuntimeError(
                            "Lossless evidence subdivision could not produce smaller source pieces: "
                            f"{exc}"
                        ) from exc
                    subdivision_artifacts = [
                        analyze_piece(
                            subdivision,
                            path=(*path, subdivision_index),
                            sibling_count=len(subdivisions),
                        )
                        for subdivision_index, subdivision in enumerate(subdivisions)
                    ]
                    artifact = cls._normalize_segment_artifact(
                        cls._merge_segment_artifacts(subdivision_artifacts, fallback=fallback),
                        fallback=fallback,
                    )
                cache_piece(piece_cache_key, artifact)
                return artifact

            artifact = analyze_piece(segment)
            return index, artifact, False

        # Keep Celery context and database connections on the document thread.
        # Checkpoint each success before submitting the next segment. On a
        # failure the document retry reuses these completed checkpoints.
        for index, segment in enumerate(segments):
            try:
                result_index, segment_artifact, _cache_hit = analyze_segment(index, segment)
                segment_artifacts[result_index] = segment_artifact
                if segment_progress:
                    try:
                        segment_progress(
                            len([item for item in segment_artifacts if item]),
                            len(segments),
                        )
                    except Exception:
                        logger.exception(
                            "Could not persist artifact segment progress for %s",
                            file_name,
                        )
            except (DocumentArtifactCancelled, DocumentArtifactYielded):
                raise
            except Exception as exc:
                failures.append(f"segment {index + 1}/{len(segments)}: {exc}")
                break

        if cancel_check and cancel_check():
            raise DocumentArtifactCancelled("Document artifact processing was cancelled.")

        if failures or any(item is None for item in segment_artifacts):
            fallback["quality_flags"] = list(dict.fromkeys([
                *(fallback.get("quality_flags") or []),
                "artifact_segment_processing_incomplete",
                *failures,
            ]))
            fallback["source_metadata"] = {
                **source_metadata,
                "artifact_segment_count": len(segments),
                "artifact_segments_completed": len([item for item in segment_artifacts if item]),
            }
            return fallback

        artifact = cls._merge_segment_artifacts(
            [item for item in segment_artifacts if item],
            fallback=fallback,
        )
        artifact["normalized_text"] = raw_text
        artifact["source_map"] = artifact.get("source_map") or cls._default_source_map(
            file_name,
            extraction_mode,
            raw_text,
        )
        artifact["source_metadata"] = {
            **source_metadata,
            "artifact_pipeline_version": cls.ARTIFACT_PIPELINE_VERSION,
            "artifact_segment_count": len(segments),
            "artifact_segments_completed": len(segments),
            "artifact_model": artifact_model,
            "artifact_segment_input_token_budget": int(
                getattr(settings, "VDR_ARTIFACT_SEGMENT_INPUT_TOKENS", 14_336)
            ),
            "artifact_segment_output_token_budget": int(
                getattr(settings, "VDR_ARTIFACT_SEGMENT_MAX_TOKENS", 32_768)
            ),
            "artifact_segment_source_token_budget": int(
                getattr(settings, "VDR_ARTIFACT_SEGMENT_SOURCE_TOKENS", 10_000)
            ),
        }
        return artifact

    @classmethod
    def _split_for_artifact(
        cls,
        text: str,
        *,
        source_tokens: int | None = None,
        overlap_tokens: int | None = None,
        minimum_source_tokens: int = 4_000,
    ) -> list[str]:
        minimum_source_tokens = max(1, int(minimum_source_tokens))
        source_tokens = max(minimum_source_tokens, int(
            source_tokens
            if source_tokens is not None
            else getattr(settings, "VDR_ARTIFACT_SEGMENT_SOURCE_TOKENS", 10_000)
        ))
        overlap_tokens = max(0, min(
            int(
                overlap_tokens
                if overlap_tokens is not None
                else getattr(settings, "VDR_ARTIFACT_SEGMENT_OVERLAP_TOKENS", 768)
            ),
            source_tokens // 4,
        ))
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=source_tokens,
            chunk_overlap=overlap_tokens,
            length_function=estimate_tokens,
            separators=["\n\n", "\n", " ", ""],
        )
        return splitter.split_text(text) or [text]

    @classmethod
    def _split_for_subdivision(
        cls,
        text: str,
        *,
        source_tokens: int,
        overlap_tokens: int,
        minimum_source_tokens: int,
    ) -> list[str]:
        """Retain workbook/sheet/range context on every recursive spreadsheet piece."""
        marker = "[CELLS]\n"
        if marker not in text:
            return cls._split_for_artifact(
                text,
                source_tokens=source_tokens,
                overlap_tokens=overlap_tokens,
                minimum_source_tokens=minimum_source_tokens,
            )
        prefix, body = text.split(marker, 1)
        prefix = prefix + marker
        body_tokens = max(
            minimum_source_tokens,
            int(source_tokens) - estimate_tokens(prefix),
        )
        body_overlap = min(int(overlap_tokens), body_tokens // 4)
        pieces = cls._split_for_artifact(
            body,
            source_tokens=body_tokens,
            overlap_tokens=body_overlap,
            minimum_source_tokens=minimum_source_tokens,
        )
        return [prefix + piece for piece in pieces]

    @classmethod
    def _segment_cache_key(
        cls,
        *,
        file_name: str,
        segment: str,
        index: int,
        total: int,
        model: str,
        run_scope: str = "",
    ) -> str:
        fingerprint = json.dumps(
            [cls.ARTIFACT_PIPELINE_VERSION, model, run_scope, file_name, index, total, segment],
            ensure_ascii=False,
        )
        return "vdr-document-artifact:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()

    @classmethod
    def _merge_segment_artifacts(
        cls,
        artifacts: list[dict[str, Any]],
        *,
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        merged = deepcopy(fallback)
        list_fields = (
            "claims",
            "metrics",
            "numeric_evidence",
            "table_definitions",
            "tables_summary",
            "contacts_found",
            "risks",
            "open_questions",
            "diligence_gaps",
            "citations",
            "quality_flags",
        )
        for field in list_fields:
            merged[field] = []
        summaries = []
        reasoning = []
        for artifact in artifacts:
            summary = str(artifact.get("document_summary") or "").strip()
            if summary and summary != "No summary extracted":
                summaries.append(summary)
            thought = str(artifact.get("reasoning") or "").strip()
            if thought:
                reasoning.append(thought)
            for field in list_fields:
                merged[field] = cls._dedupe_values([
                    *(merged.get(field) or []),
                    *(artifact.get(field) or []),
                ])

        merged["industry_overview"] = {
            field: cls._dedupe_values([
                item
                for artifact in artifacts
                for item in ((artifact.get("industry_overview") or {}).get(field) or [])
            ])
            for field in ("findings", "market_figures", "citations")
        }

        if summaries:
            merged["document_summary"] = " ".join(cls._dedupe_values(summaries))
        if reasoning:
            merged["reasoning"] = "\n\n".join(cls._dedupe_values(reasoning))

        confidence = {"High": 3, "Medium": 2, "Low": 1}
        suggestions = [
            item.get("document_type_suggestion")
            for item in artifacts
            if isinstance(item.get("document_type_suggestion"), dict)
        ]
        if suggestions:
            merged["document_type_suggestion"] = max(
                suggestions,
                key=lambda item: confidence.get(item.get("confidence"), 0),
            )
            merged["document_type"] = (
                merged["document_type_suggestion"].get("display_label")
                or merged.get("document_type")
            )
        return merged

    @staticmethod
    def _dedupe_values(values: list[Any]) -> list[Any]:
        deduped = []
        seen = set()
        for value in values:
            if value in (None, "", [], {}):
                continue
            key = json.dumps(value, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(value)
        return deduped

    @classmethod
    def persist_artifact(cls, document, artifact: dict[str, Any]) -> None:
        normalized_artifact = cls._normalize_artifact(artifact, fallback=cls._fallback_artifact(
            file_name=document.title,
            extracted_text=document.extracted_text or "",
            document_type=document.document_type,
            extraction_mode=document.extraction_mode,
        ))
        document.normalized_text = normalized_artifact.get("normalized_text") or document.extracted_text
        document.evidence_json = normalized_artifact
        document.source_map_json = normalized_artifact.get("source_map") or {}
        document.table_json = normalized_artifact.get("table_definitions") or normalized_artifact.get("tables_summary") or []
        document.key_metrics_json = normalized_artifact.get("metrics") or []
        document.reasoning = normalized_artifact.get("reasoning") or ""
        document.is_ai_analyzed = cls.artifact_complete(normalized_artifact)
        document.save(
            update_fields=[
                "normalized_text",
                "evidence_json",
                "source_map_json",
                "table_json",
                "key_metrics_json",
                "reasoning",
                "is_ai_analyzed",
            ]
        )

    @classmethod
    def persist_analysis_artifact(cls, analysis_document, artifact: dict[str, Any]) -> None:
        normalized_artifact = cls._normalize_artifact(
            artifact,
            fallback=cls._fallback_artifact(
                file_name=analysis_document.file_name,
                extracted_text=analysis_document.raw_extracted_text or "",
                document_type=analysis_document.document_type,
                extraction_mode=analysis_document.extraction_mode,
            ),
        )
        analysis_document.normalized_text = normalized_artifact.get("normalized_text") or analysis_document.raw_extracted_text
        analysis_document.evidence_json = normalized_artifact
        analysis_document.source_map_json = normalized_artifact.get("source_map") or {}
        analysis_document.table_json = normalized_artifact.get("table_definitions") or normalized_artifact.get("tables_summary") or []
        analysis_document.key_metrics_json = normalized_artifact.get("metrics") or []
        analysis_document.reasoning = normalized_artifact.get("reasoning") or ""
        analysis_document.save(
            update_fields=[
                "normalized_text",
                "evidence_json",
                "source_map_json",
                "table_json",
                "key_metrics_json",
                "reasoning",
            ]
        )

    @classmethod
    def ensure_document_artifact(
        cls,
        document,
        *,
        ai_service: Optional["AIProcessorService"] = None,
        force: bool = False,
    ) -> dict[str, Any]:
        existing = cls.artifact_from_document(document)
        if not force and cls.artifact_status(existing) == cls.STATUS_COMPLETE:
            return existing

        text = (document.normalized_text or document.extracted_text or "").strip()
        artifact = cls.build_document_artifact(
            file_name=document.title,
            extracted_text=text,
            document_type=document.document_type,
            extraction_mode=document.extraction_mode,
            ai_service=ai_service,
            extraction_manifest=getattr(document, "extraction_manifest", None),
            source_metadata={
                "source_id": getattr(document, "id", None),
                "source_url": getattr(document, "file_url", None),
            },
        )
        cls.persist_artifact(document, artifact)
        return cls.artifact_from_document(document)

    @classmethod
    def artifact_from_document(cls, document) -> dict[str, Any]:
        stored = document.evidence_json if isinstance(document.evidence_json, dict) and document.evidence_json else None
        fallback = cls._fallback_artifact(
            file_name=document.title,
            extracted_text=document.normalized_text or document.extracted_text or "",
            document_type=document.document_type,
            extraction_mode=document.extraction_mode,
        )
        artifact = cls._normalize_artifact(stored, fallback=fallback)
        artifact["reasoning"] = document.reasoning or artifact.get("reasoning") or ""
        artifact["normalized_text"] = document.normalized_text or document.extracted_text or artifact.get("normalized_text") or ""
        artifact["source_map"] = artifact.get("source_map") or document.source_map_json or fallback.get("source_map") or {}
        return artifact

    @classmethod
    def artifact_from_file_record(cls, file_record: dict[str, Any]) -> dict[str, Any]:
        return cls._normalize_artifact(
            file_record.get("document_artifact"),
            fallback=cls._fallback_artifact(
                file_name=file_record.get("file_name") or "unknown_file",
                extracted_text=file_record.get("extracted_text") or "",
                document_type=file_record.get("document_type") or "Other",
                extraction_mode=file_record.get("extraction_mode"),
            ),
        )

    @classmethod
    def artifact_from_analysis_document(cls, analysis_document) -> dict[str, Any]:
        stored = analysis_document.evidence_json if isinstance(analysis_document.evidence_json, dict) and analysis_document.evidence_json else None
        fallback = cls._fallback_artifact(
            file_name=analysis_document.file_name,
            extracted_text=analysis_document.normalized_text or analysis_document.raw_extracted_text or "",
            document_type=analysis_document.document_type,
            extraction_mode=analysis_document.extraction_mode,
        )
        artifact = cls._normalize_artifact(stored, fallback=fallback)
        artifact["reasoning"] = analysis_document.reasoning or artifact.get("reasoning") or ""
        artifact["normalized_text"] = (
            analysis_document.normalized_text
            or analysis_document.raw_extracted_text
            or artifact.get("normalized_text")
            or ""
        )
        artifact["source_map"] = (
            artifact.get("source_map")
            or analysis_document.source_map_json
            or fallback.get("source_map")
            or {}
        )
        return artifact

    @classmethod
    def build_supporting_raw_chunks(
        cls,
        documents: list[dict[str, Any]],
        *,
        max_chunks: int = 18,
        excerpt_chars: int = 1600,
    ) -> list[dict[str, Any]]:
        chunks = []
        for doc in documents:
            if len(chunks) >= max_chunks:
                break
            artifact = cls._normalize_artifact(doc, fallback=doc)
            normalized_text = (artifact.get("normalized_text") or "").strip()
            if not normalized_text:
                continue
            chunks.append(
                {
                    "document_name": artifact.get("document_name") or doc.get("document_name") or "Unknown Document",
                    "excerpt": normalized_text[:excerpt_chars],
                    "citation_label": cls._citation_label(artifact),
                    "chunk_kind": "normalized_text",
                }
            )
            for metric in artifact.get("metrics") or []:
                if len(chunks) >= max_chunks:
                    break
                if isinstance(metric, dict):
                    chunks.append(
                        {
                            "document_name": artifact.get("document_name") or "Unknown Document",
                            "excerpt": json.dumps(metric, ensure_ascii=True, default=str),
                            "citation_label": cls._citation_label(artifact),
                            "chunk_kind": "metric",
                        }
                    )
        return chunks

    @classmethod
    def build_embedding_chunks(
        cls,
        artifact_or_document: Any,
        *,
        text_excerpt_chars: int = 2400,
        table_excerpt_chars: int = 1800,
        claim_excerpt_chars: int = 900,
    ) -> list[dict[str, Any]]:
        artifact = cls._coerce_artifact(artifact_or_document)
        if not artifact:
            return []

        document_name = artifact.get("document_name") or "Unknown Document"
        base_metadata = {
            "document_name": document_name,
            "document_type": artifact.get("document_type") or "Other",
            "citation_label": cls._citation_label(artifact),
            "source_map": artifact.get("source_map") or {},
            "artifact_status": cls.artifact_status(artifact),
            "metric_names": [
                metric.get("name")
                for metric in (artifact.get("metrics") or [])
                if isinstance(metric, dict) and metric.get("name")
            ],
        }

        chunks: list[dict[str, Any]] = []
        manifest = getattr(artifact_or_document, "extraction_manifest", None)
        spreadsheet_chunks = []
        if isinstance(manifest, dict) and manifest.get("kind") == "spreadsheet":
            spreadsheet_chunks = cls._spreadsheet_manifest_chunks(
                manifest,
                base_metadata=base_metadata,
            )
            chunks.extend(spreadsheet_chunks)
        if isinstance(manifest, dict) and not spreadsheet_chunks:
            for manifest_index, item in enumerate(manifest.get("chunks") or []):
                if not isinstance(item, dict) or not str(item.get("text") or "").strip():
                    continue
                chunks.append({
                    "text": str(item["text"]),
                    "metadata": {
                        **base_metadata,
                        **(item.get("metadata") or {}),
                        "chunk_kind": (item.get("metadata") or {}).get("chunk_kind", "structured_range"),
                        "manifest_chunk_index": manifest_index,
                        "extraction_schema_version": manifest.get("schema_version", "1"),
                    },
                })
        normalized_text = (artifact.get("normalized_text") or "").strip()
        manifest_kind = manifest.get("kind") if isinstance(manifest, dict) else None
        has_structured_chunks = bool(manifest.get("chunks")) if isinstance(manifest, dict) else False
        # Spreadsheet ranges are already purpose-built retrieval units. Splitting the
        # rendered workbook text again duplicates the same cells in the vector index.
        if manifest_kind == "spreadsheet" and has_structured_chunks:
            normalized_text = ""
        if normalized_text:
            splitter = RecursiveCharacterTextSplitter(
                chunk_size=text_excerpt_chars,
                chunk_overlap=max(200, text_excerpt_chars // 10),
                length_function=len,
                separators=["\n\n", "\n", " ", ""],
            )
            normalized_parts = splitter.split_text(normalized_text)
            total_normalized_parts = len(normalized_parts)
            for part_index, normalized_part in enumerate(normalized_parts):
                if not normalized_part.strip():
                    continue
                chunks.append(
                    {
                        "text": normalized_part,
                        "metadata": {
                            **base_metadata,
                            "chunk_kind": "normalized_text",
                            "normalized_text_part_index": part_index,
                            "normalized_text_part_count": total_normalized_parts,
                        },
                    }
                )

        for metric in artifact.get("metrics") or []:
            serialized = cls._serialize_component(metric)
            if not serialized:
                continue
            chunks.append(
                {
                    "text": serialized,
                    "metadata": {
                        **base_metadata,
                        "chunk_kind": "metric",
                    },
                }
            )

        for table in artifact.get("table_definitions") or artifact.get("tables_summary") or []:
            serialized = cls._serialize_component(table, max_chars=table_excerpt_chars)
            if not serialized:
                continue
            chunks.append(
                {
                    "text": serialized,
                    "metadata": {
                        **base_metadata,
                        "chunk_kind": "table_summary",
                    },
                }
            )

        for claim in artifact.get("claims") or []:
            serialized = cls._serialize_component(claim, max_chars=claim_excerpt_chars)
            if not serialized:
                continue
            chunks.append(
                {
                    "text": serialized,
                    "metadata": {
                        **base_metadata,
                        "chunk_kind": "claim",
                    },
                }
            )

        for risk in artifact.get("risks") or []:
            serialized = cls._serialize_component(risk, max_chars=claim_excerpt_chars)
            if not serialized:
                continue
            chunks.append(
                {
                    "text": serialized,
                    "metadata": {
                        **base_metadata,
                        "chunk_kind": "risk",
                    },
                }
            )

        industry_overview = artifact.get("industry_overview") or {}
        for finding in industry_overview.get("findings") or []:
            serialized = cls._serialize_component(finding, max_chars=claim_excerpt_chars)
            if not serialized:
                continue
            chunks.append(
                {
                    "text": serialized,
                    "metadata": {
                        **base_metadata,
                        "chunk_kind": "industry_finding",
                        "source_ids": finding.get("source_ids") if isinstance(finding, dict) else [],
                    },
                }
            )

        for figure in industry_overview.get("market_figures") or []:
            serialized = cls._serialize_component(figure)
            if not serialized:
                continue
            chunks.append(
                {
                    "text": serialized,
                    "metadata": {
                        **base_metadata,
                        "chunk_kind": "industry_market_figure",
                        "source_ids": figure.get("source_ids") if isinstance(figure, dict) else [],
                    },
                }
            )

        return chunks

    @classmethod
    def artifact_status(cls, artifact_or_document: Any) -> str:
        transcription_status = getattr(artifact_or_document, "transcription_status", None)
        chunking_status = getattr(artifact_or_document, "chunking_status", None)
        if transcription_status == "failed":
            return cls.STATUS_FAILED

        artifact = cls._coerce_artifact(artifact_or_document)
        if transcription_status == "partial" and not cls._legacy_native_extraction_complete(
            artifact_or_document,
            artifact,
        ):
            return cls.STATUS_PARTIAL
        normalized_text = (artifact.get("normalized_text") or "").strip()
        if not normalized_text:
            return cls.STATUS_MISSING

        incomplete_flags = {
            "fallback_artifact",
            "artifact_missing_text",
            "artifact_segment_processing_incomplete",
        }
        if any(flag in incomplete_flags for flag in artifact.get("quality_flags") or []):
            return cls.STATUS_DEGRADED

        source_metadata = artifact.get("source_metadata") or {}
        segment_count = source_metadata.get("artifact_segment_count")
        segments_completed = source_metadata.get("artifact_segments_completed")
        if segment_count is not None and segments_completed != segment_count:
            return cls.STATUS_DEGRADED

        missing_required = [key for key in cls.REQUIRED_ARTIFACT_KEYS if key not in artifact]
        if missing_required:
            return cls.STATUS_DEGRADED

        source_map = artifact.get("source_map") or {}
        if not isinstance(source_map, dict) or not source_map.get("document_name"):
            return cls.STATUS_DEGRADED
        if chunking_status == "failed":
            return cls.STATUS_DEGRADED

        return cls.STATUS_COMPLETE

    @classmethod
    def _legacy_native_extraction_complete(cls, document: Any, artifact: dict[str, Any]) -> bool:
        """Accept old native extractions whose partial state came from provenance flags only."""
        if not getattr(document, "is_indexed", False):
            return False
        if getattr(document, "extraction_mode", None) != "chat_native_text":
            return False
        if not (artifact.get("normalized_text") or "").strip():
            return False

        source_metadata = artifact.get("source_metadata") or {}
        render_metadata = source_metadata.get("render_metadata") or {}
        if render_metadata.get("failed_pages"):
            return False

        quality_flags = set(cls._normalize_string_list(
            source_metadata.get("quality_flags") or artifact.get("quality_flags") or []
        ))
        informational_flags = {
            "chat_direct_extraction",
            "backend_fallback_extraction",
            "calamine",
            "partial_extraction",
        }
        return "backend_fallback_extraction" in quality_flags and quality_flags <= informational_flags

    @classmethod
    def artifact_complete(cls, artifact_or_document: Any) -> bool:
        return cls.artifact_status(artifact_or_document) == cls.STATUS_COMPLETE

    @classmethod
    def _normalize_artifact(cls, artifact: Any, *, fallback: dict[str, Any]) -> dict[str, Any]:
        normalized = deepcopy(fallback)
        if isinstance(artifact, dict):
            for key, default_value in cls.DEFAULT_EVIDENCE_KEYS.items():
                value = artifact.get(key, normalized.get(key, default_value))
                if isinstance(default_value, list):
                    normalized[key] = value if isinstance(value, list) else deepcopy(default_value)
                elif isinstance(default_value, dict):
                    normalized[key] = value if isinstance(value, dict) else deepcopy(default_value)
                elif isinstance(default_value, str):
                    normalized[key] = value.strip() if isinstance(value, str) else normalized.get(key, default_value)
                else:
                    normalized[key] = value if value is not None else normalized.get(key, default_value)
        if not normalized.get("table_definitions") and normalized.get("tables_summary"):
            normalized["table_definitions"] = deepcopy(normalized["tables_summary"])
        if not normalized.get("tables_summary") and normalized.get("table_definitions"):
            normalized["tables_summary"] = deepcopy(normalized["table_definitions"])
        normalized["quality_flags"] = cls._normalize_string_list(normalized.get("quality_flags"))
        normalized["citations"] = cls._normalize_string_list(normalized.get("citations"))
        normalized["industry_overview"] = cls._normalize_industry_overview(
            normalized.get("industry_overview"),
        )
        normalized["document_name"] = normalized.get("document_name") or fallback.get("document_name") or ""
        normalized["document_type"] = normalized.get("document_type") or fallback.get("document_type") or "Other"
        normalized["source_map"] = cls._normalize_source_map(
            normalized.get("source_map"),
            fallback.get("source_map") or {},
        )
        if not normalized["normalized_text"]:
            if "artifact_missing_text" not in normalized["quality_flags"]:
                normalized["quality_flags"].append("artifact_missing_text")
        return normalized

    @classmethod
    def _fallback_artifact(
        cls,
        *,
        file_name: str,
        extracted_text: str,
        document_type: str,
        extraction_mode: str | None,
    ) -> dict[str, Any]:
        excerpt = (extracted_text or "").strip()
        return {
            "document_name": file_name,
            "document_type": document_type,
            "document_type_suggestion": {
                "label": "Other",
                "display_label": document_type or "Other",
                "confidence": "Low",
                "rationale": "Fallback artifact generated without model evidence extraction.",
            },
            "document_summary": excerpt[:500],
            "claims": [],
            "metrics": [],
            "numeric_evidence": [],
            "table_definitions": [],
            "tables_summary": [],
            "contacts_found": [],
            "risks": [],
            "open_questions": [],
            "diligence_gaps": [],
            "citations": [file_name] if file_name else [],
            "industry_overview": {
                "findings": [],
                "market_figures": [],
                "citations": [],
            },
            "reasoning": "",
            "quality_flags": ["fallback_artifact"],
            "normalized_text": excerpt,
            "source_map": cls._default_source_map(file_name, extraction_mode, excerpt),
            "source_metadata": {},
            "spreadsheet_profile": {},
        }

    @staticmethod
    def _coerce_artifact(artifact_or_document: Any) -> dict[str, Any]:
        if isinstance(artifact_or_document, dict):
            return artifact_or_document
        if hasattr(artifact_or_document, "file_name") and hasattr(artifact_or_document, "raw_extracted_text"):
            return DocumentArtifactService.artifact_from_analysis_document(artifact_or_document)
        if hasattr(artifact_or_document, "evidence_json"):
            return DocumentArtifactService.artifact_from_document(artifact_or_document)
        return {}

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        seen: set[str] = set()
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            cleaned = item.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                normalized.append(cleaned)
        return normalized

    @staticmethod
    def _normalize_source_map(value: Any, fallback: dict[str, Any]) -> dict[str, Any]:
        normalized = deepcopy(fallback) if isinstance(fallback, dict) else {}
        if isinstance(value, dict):
            normalized.update(value)
        if not normalized.get("document_name"):
            normalized["document_name"] = fallback.get("document_name") if isinstance(fallback, dict) else ""
        return normalized

    @classmethod
    def _normalize_industry_overview(cls, value: Any) -> dict[str, list[dict[str, Any]]]:
        overview = value if isinstance(value, dict) else {}
        citations: list[dict[str, Any]] = []
        citation_ids: set[str] = set()
        for index, item in enumerate(overview.get("citations") or []):
            if not isinstance(item, dict):
                continue
            citation_id = cls._clean_string(item.get("id")) or f"industry-source-{index + 1}"
            label = cls._clean_string(item.get("label"))
            page = cls._clean_string(item.get("page"))
            section = cls._clean_string(item.get("section"))
            passage = cls._clean_string(item.get("passage"))
            if not label or (not page and not section) or not passage:
                continue
            citation_ids.add(citation_id)
            citations.append(
                {
                    "id": citation_id,
                    "label": label,
                    "page": page,
                    "section": section,
                    "passage": passage,
                }
            )

        findings: list[dict[str, Any]] = []
        for item in overview.get("findings") or []:
            if not isinstance(item, dict):
                continue
            title = cls._clean_string(item.get("title"))
            summary = cls._clean_string(item.get("summary"))
            source_ids = cls._valid_source_ids(item.get("source_ids"), citation_ids)
            if not title or not summary or not source_ids:
                continue
            findings.append(
                {
                    "title": title,
                    "summary": summary,
                    "period": cls._clean_string(item.get("period")),
                    "as_of_date": cls._clean_date(item.get("as_of_date")),
                    "source_ids": source_ids,
                }
            )

        market_figures: list[dict[str, Any]] = []
        for item in overview.get("market_figures") or []:
            if not isinstance(item, dict):
                continue
            label = cls._clean_string(item.get("label"))
            unit = cls._clean_string(item.get("unit"))
            period = cls._clean_string(item.get("period"))
            calculation_method = cls._clean_string(item.get("calculation_method"))
            as_of_date = cls._clean_date(item.get("as_of_date"))
            source_ids = cls._valid_source_ids(item.get("source_ids"), citation_ids)
            try:
                numeric_value = float(item.get("value"))
            except (TypeError, ValueError):
                numeric_value = math.nan
            if (
                not label
                or not unit
                or not period
                or not calculation_method
                or not as_of_date
                or not source_ids
                or not math.isfinite(numeric_value)
            ):
                continue
            market_figures.append(
                {
                    "label": label,
                    "value": numeric_value,
                    "unit": unit,
                    "period": period,
                    "as_of_date": as_of_date,
                    "calculation_method": calculation_method,
                    "source_ids": source_ids,
                }
            )

        return {
            "findings": findings,
            "market_figures": market_figures,
            "citations": citations,
        }

    @staticmethod
    def _clean_string(value: Any) -> str | None:
        if not isinstance(value, (str, int, float)):
            return None
        cleaned = str(value).strip()
        return cleaned or None

    @staticmethod
    def _clean_date(value: Any) -> str | None:
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if not isinstance(value, str):
            return None
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            return None

    @staticmethod
    def _valid_source_ids(value: Any, citation_ids: set[str]) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(
            source_id.strip()
            for source_id in value
            if isinstance(source_id, str)
            and source_id.strip() in citation_ids
        ))

    @staticmethod
    def _serialize_component(value: Any, *, max_chars: int = 1200) -> str:
        if isinstance(value, str):
            return value.strip()[:max_chars]
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=True, default=str)[:max_chars]
        if value is None:
            return ""
        return str(value).strip()[:max_chars]

    @staticmethod
    def _default_source_map(file_name: str, extraction_mode: str | None, text: str) -> dict[str, Any]:
        return {
            "document_name": file_name,
            "extraction_mode": extraction_mode,
            "text_length": len(text or ""),
        }

    @staticmethod
    def _citation_label(artifact: dict[str, Any]) -> str:
        source_map = artifact.get("source_map") if isinstance(artifact.get("source_map"), dict) else {}
        doc_name = artifact.get("document_name") or source_map.get("document_name") or "Unknown Document"
        section = source_map.get("section")
        page = source_map.get("page")
        parts = [doc_name]
        if section:
            parts.append(f"section={section}")
        if page is not None:
            parts.append(f"page={page}")
        return " | ".join(parts)
