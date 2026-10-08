"""Complete canonical IC reports section by section when one-shot output is incomplete."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist

from ai_orchestrator.prompt_contracts import IC_REPORT_HEADERS, IC_SECTION_TITLES
from ai_orchestrator.services.bulk_prompt_contracts import IC_REPORT_SECTION_STAGE_KEYS
from ai_orchestrator.services.pipeline_registry import PipelineRegistryService


class ReportSectionValidationError(ValueError):
    """A deterministic model-output validation failure that should not be retried unchanged."""


class ReportSectionDegenerateOutputError(ReportSectionValidationError):
    """A transient repetition loop that warrants a fresh model request."""


class ReportSectionTooShortError(ReportSectionValidationError):
    """A usable but under-length draft that warrants a corrected model request."""


class ReportSectionCitationError(ReportSectionValidationError):
    """A draft with missing retrieval markers needs a corrected citation request."""


class ReportSectionStructureError(ReportSectionValidationError):
    """A draft violates the financial table layout and needs a corrected request."""


class ICReportSectionService:
    CACHE_VERSION = "ic-report-sections-v13"
    # Dense tabular sections need fewer prose words than narrative sections.
    # The configured minimum remains the baseline for essay-style sections.
    SECTION_MINIMUM_WORD_FACTORS = {
        "Promoter and Management Details": 0.8,
        "Transaction Details": 0.8,
        "Key Financials": 0.75,
        "Transaction / Trading Multiples": 0.65,
        "Exit Considerations": 0.85,
        "Next Steps": 0.75,
    }
    # Rich VDR evidence needs more than the sparse-evidence floor above. This
    # catches the short, table-free summaries seen with 30k+ token evidence packs
    # without forcing unsupported prose when a deal has few usable records.
    RICH_EVIDENCE_MINIMUM_WORD_FACTORS = {
        "Executive Summary": 1.4,
        "Company Details": 1.5,
        "Promoter and Management Details": 1.3,
        "Industry Overview": 1.3,
        "Transaction Details": 1.3,
        "Key Financials": 1.6,
        "Transaction / Trading Multiples": 1.2,
        "Risk Factors": 1.4,
        "Investment Rationale": 1.4,
        "Exit Considerations": 1.2,
        "Next Steps": 1.1,
    }
    INTERNAL_CITATION_PATTERN = re.compile(
        r"\[(?:Evidence\s+(?P<evidence>\d+)|R0*(?P<rank>\d+))"
        r"(?:@(?P<locator>[^\]\n]+))?\]"
        r"|\b(?:Evidence\s+(?P<bare_evidence>\d+)|R0*(?P<bare_rank>\d+))\b",
        flags=re.IGNORECASE,
    )
    SPREADSHEET_LOCATOR_PATTERN = re.compile(
        r"^(?:(?:'(?P<quoted_sheet>(?:[^']|'')+)'|(?P<sheet>[^!]+))!)?"
        r"(?P<start>[A-Za-z]{1,4}[1-9]\d*)(?::(?P<end>[A-Za-z]{1,4}[1-9]\d*))?$"
    )
    # Keep this deliberately bounded. The former nested repetition pattern
    # could spend minutes backtracking when a model emitted thousands of
    # citation tokens without a closing bracket.
    CITATION_CLUSTER_PATTERN = re.compile(r"\[(?P<items>[^\]\n]{1,2000})\]")
    CITATION_TOKEN_PATTERN = re.compile(
        r"\b(?:Evidence\s+\d+|R0*\d+)\b",
        flags=re.IGNORECASE,
    )
    FINANCIAL_PERIOD_PATTERN = re.compile(
        r"^(?:(?:FY|CY)\s*\d{2,4}[A-Z]?|20\d{2}[A-Z]?|Q[1-4]\b|H[12]\b|Period\b|Month\b)",
        flags=re.IGNORECASE,
    )
    FINANCIAL_METRIC_PATTERN = re.compile(
        r"revenue|sales|gross profit|margin|ebitda|ebit|pat|net income|cash|capex|"
        r"debt|borrowings|receivable|payable|inventory|working capital|roce|roic|roe",
        flags=re.IGNORECASE,
    )

    @classmethod
    def headings(cls, report: str) -> list[str]:
        return re.findall(r"^##\s+(.+?)\s*$", str(report or ""), flags=re.MULTILINE)

    @classmethod
    def is_complete(cls, report: str) -> bool:
        return cls.headings(report) == list(IC_SECTION_TITLES)

    @classmethod
    def _split(cls, report: str) -> dict[str, str]:
        text = str(report or "").strip()
        matches = list(re.finditer(r"^##\s+(.+?)\s*$", text, flags=re.MULTILINE))
        sections = {}
        for index, match in enumerate(matches):
            title = match.group(1).strip()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            sections[title] = text[match.start():end].strip()
        return sections

    @classmethod
    def _stable_prefix_length(cls, report: str) -> int:
        actual = cls.headings(report)
        for index, expected in enumerate(IC_SECTION_TITLES):
            if index >= len(actual) or actual[index] != expected:
                # The section before a missing tail may itself be cut short.
                return max(0, index - 1)
        return len(IC_SECTION_TITLES)

    @classmethod
    def _cache_key(
        cls, *, evidence: str, model_data: dict, title: str, prompt_revision: str
    ) -> str:
        fingerprint = json.dumps(
            [cls.CACHE_VERSION, title, prompt_revision, evidence, model_data],
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return "ic-report-section:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()

    @classmethod
    def _minimum_words(cls, title: str, evidence_metadata: dict | None = None) -> int:
        baseline = max(0, int(getattr(settings, "VDR_REPORT_SECTION_MIN_WORDS", 900)))
        metadata = evidence_metadata or {}
        rich_evidence = (
            int(metadata.get("selected_chunk_count") or 0) >= 40
            or int(metadata.get("estimated_context_tokens") or 0) >= 12_000
        )
        factors = (
            cls.RICH_EVIDENCE_MINIMUM_WORD_FACTORS
            if rich_evidence else cls.SECTION_MINIMUM_WORD_FACTORS
        )
        return round(baseline * factors.get(title, 1.0))

    @classmethod
    def _resolve_prompt_stage(cls, title: str):
        stage_key = IC_REPORT_SECTION_STAGE_KEYS[title]
        try:
            return PipelineRegistryService.resolve_stage("ic_report_generation", stage_key)
        except ObjectDoesNotExist:
            PipelineRegistryService.ensure_report_pipeline_defaults()
            return PipelineRegistryService.resolve_stage("ic_report_generation", stage_key)

    @staticmethod
    def _column_number(label: str) -> int:
        value = 0
        for character in str(label or "").upper():
            if not "A" <= character <= "Z":
                return 0
            value = value * 26 + ord(character) - ord("A") + 1
        return value

    @staticmethod
    def _cell_parts(value: str) -> tuple[int, int] | None:
        match = re.fullmatch(r"([A-Za-z]{1,4})([1-9]\d*)", str(value or "").strip())
        if not match:
            return None
        return ICReportSectionService._column_number(match.group(1)), int(match.group(2))

    @classmethod
    def _validated_spreadsheet_location(cls, locator: str, citation: dict) -> str:
        match = cls.SPREADSHEET_LOCATOR_PATTERN.fullmatch(str(locator or "").strip())
        source = citation.get("locator") if isinstance(citation.get("locator"), dict) else {}
        if not match or not source.get("sheet_name"):
            return ""
        requested_sheet = (match.group("quoted_sheet") or match.group("sheet") or "").replace("''", "'").strip()
        source_sheet = str(source.get("sheet_name") or "").strip()
        if requested_sheet and requested_sheet.casefold() != source_sheet.casefold():
            return ""
        start = cls._cell_parts(match.group("start"))
        end = cls._cell_parts(match.group("end") or match.group("start"))
        source_start = cls._cell_parts(
            f"{source.get('column_start') or 'A'}{source.get('row_start') or ''}"
        )
        source_end = cls._cell_parts(
            f"{source.get('column_end') or source.get('column_start') or 'A'}"
            f"{source.get('row_end') or source.get('row_start') or ''}"
        )
        if not all((start, end, source_start, source_end)):
            return ""
        if start[0] > end[0] or start[1] > end[1]:
            return ""
        if (
            start[0] < source_start[0]
            or end[0] > source_end[0]
            or start[1] < source_start[1]
            or end[1] > source_end[1]
        ):
            return ""
        visible = citation.get("visible_cells")
        if visible:
            area = (end[0] - start[0] + 1) * (end[1] - start[1] + 1)
            present = sum(1 for address in visible if (parts := cls._cell_parts(address))
                and start[0] <= parts[0] <= end[0] and start[1] <= parts[1] <= end[1])
            if present != area:
                return ""  # Never attach a cell absent from this retrieved block.
        start_label = match.group("start").upper()
        end_label = (match.group("end") or match.group("start")).upper()
        cell_range = start_label if start_label == end_label else f"{start_label}:{end_label}"
        return f"{source_sheet}!{cell_range}"

    @classmethod
    def _location_url(cls, citation: dict, location: str) -> str:
        url = str(citation.get("url") or "").strip()
        title = str(citation.get("title") or "").lower()
        if not url or not title.endswith((".xlsx", ".xlsm", ".xlsb", ".xls")):
            return url
        match = re.fullmatch(r"(?P<sheet>.+)!(?P<cell>[A-Za-z]{1,4}[1-9]\d*)(?::[A-Za-z]{1,4}[1-9]\d*)?", location)
        if not match:
            return url
        try:
            parsed = urlsplit(url)
            query = [
                (key, value)
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                if key.casefold() != "activecell"
            ]
            sheet = match.group("sheet").replace("'", "''")
            query.append(("activeCell", f"'{sheet}'!{match.group('cell').upper()}"))
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
        except ValueError:
            return url

    @classmethod
    def _render_inline_citation(cls, citation: dict, *, location: str = "") -> str:
        title = str(citation.get("title") or citation.get("document_id") or "Source")
        title = title.replace("[", "\\[").replace("]", "\\]")
        rendered_location = str(location or citation.get("location") or "").strip()
        label = f"{title}, {rendered_location}" if rendered_location else title
        url = cls._location_url(citation, rendered_location)
        # Email evidence often has no public URL. Make those source labels
        # visibly distinct from prose instead of emitting a bare repeated
        # filename/message title that looks like model text.
        return f"[{label}](<{url}>)" if url else f"**[Source: {label}]**"

    @staticmethod
    def _strip_model_references(text: str) -> str:
        heading = re.search(
            r"^###\s+(?:References|Citations)\s*$",
            text,
            flags=re.MULTILINE | re.IGNORECASE,
        )
        return text[:heading.start()].rstrip() if heading else text.rstrip()

    @staticmethod
    def _unverified_links(text: str, citations: dict | None) -> list[str]:
        allowed_locations = set()
        for citation in (citations or {}).values():
            if not isinstance(citation, dict) or not citation.get("url"):
                continue
            try:
                parsed = urlsplit(str(citation["url"]))
            except ValueError:
                continue
            allowed_locations.add((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path))
        links = re.findall(r"\]\(<(https?://[^>]+)>\)", text, flags=re.IGNORECASE)
        links.extend(
            re.findall(r"\]\((https?://[^)\s]+)\)", text, flags=re.IGNORECASE)
        )
        invalid = []
        for link in links:
            try:
                parsed = urlsplit(link)
            except ValueError:
                invalid.append(link)
                continue
            if (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path) not in allowed_locations:
                invalid.append(link)
        return invalid

    @classmethod
    def _replace_internal_citations(cls, text: str, citations: dict | None) -> tuple[str, list[dict]]:
        citation_map = citations or {}
        used: list[dict] = []
        citation_numbers: dict[tuple[str, str, str], int] = {}

        # Earlier authored prompts invited [IM: filename @R001, R002]. Resolve
        # supplied ranks without leaving nested brackets or a filename in a table
        # cell. Filename-only labels remain invalid; never guess an evidence rank.
        def source_label(match: re.Match) -> str:
            value = match.group("value")
            ranks = cls.CITATION_TOKEN_PATTERN.findall(value)
            if not ranks:
                return match.group(0)
            prefix = "External evidence: " if match.group("kind").upper() == "EXT" else ""
            return prefix + " ".join(f"[{rank}]" for rank in ranks)

        text = re.sub(
            r"\[(?P<kind>IM|EXT):\s*(?P<value>[^\]\n]+)\]",
            source_label, text, flags=re.IGNORECASE,
        )

        # Expand multi-rank clusters first so each rank can be resolved or
        # removed independently without leaving malformed nested brackets.
        def expand_cluster(match: re.Match) -> str:
            items = re.split(r"\s*[,;|]\s*", match.group("items"))
            if len(items) < 2 or not all(
                cls.CITATION_TOKEN_PATTERN.fullmatch(item.strip()) for item in items
            ):
                return match.group(0)
            return ", ".join(f"[{item.strip()}]" for item in items)

        text = cls.CITATION_CLUSTER_PATTERN.sub(expand_cluster, text)

        def replace(match: re.Match) -> str:
            rank = (
                match.group("evidence")
                or match.group("rank")
                or match.group("bare_evidence")
                or match.group("bare_rank")
            )
            citation = citation_map.get(str(int(rank))) if rank else None
            if not isinstance(citation, dict):
                # When a retrieval map exists, an unknown rank is a model-only
                # marker. Omit it instead of linking it to an unrelated source
                # or rerunning the same deterministic request.
                return "" if citation_map else match.group(0)
            requested_locator = str(match.group("locator") or "").strip()
            resolved_location = (
                cls._validated_spreadsheet_location(requested_locator, citation)
                if requested_locator else ""
            )
            used_citation = dict(citation)
            used_citation["used_location"] = resolved_location or str(citation.get("location") or "")
            identity = (
                str(citation.get("document_id") or citation.get("title") or ""),
                str(citation.get("url") or ""),
                used_citation["used_location"],
            )
            number = citation_numbers.get(identity)
            if number is None:
                number = len(used) + 1
                citation_numbers[identity] = number
                used_citation["citation_number"] = number
                used.append(used_citation)
            return f"[{number}]"

        rendered = cls.INTERNAL_CITATION_PATTERN.sub(replace, text)
        # Collapse a repeated marker within one citation cluster while keeping
        # repetitions attached to separate claims intact.
        rendered = re.sub(r"(\[\d+])(\s*(?:[,;|]\s*)\1)+", r"\1", rendered)
        if citation_map:
            rendered = re.sub(r"\[\s*(?:[,;|]\s*)*]", "", rendered)
            # Citation punctuation cleanup must never cross line boundaries or
            # treat Markdown table pipes as punctuation. Collapsing ``|\n|``
            # turns an otherwise valid table into one long paragraph.
            rendered = re.sub(r"[ \t]+([,.;:])", r"\1", rendered)
            rendered = re.sub(r"([,;])(?:[ \t]*[,;])+", r"\1", rendered)
            rendered = re.sub(r"[,;][ \t]*([.?!])", r"\1", rendered)
        return rendered, used

    @staticmethod
    def _table_cells(line: str) -> list[str]:
        value = str(line or "").strip()
        if value.startswith("|"):
            value = value[1:]
        if value.endswith("|") and not value.endswith(r"\|"):
            value = value[:-1]
        return [cell.strip() for cell in re.split(r"(?<!\\)\|", value)]

    @classmethod
    def _is_table_separator(cls, line: str) -> bool:
        cells = cls._table_cells(line)
        return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)

    @staticmethod
    def _plain_table_label(value: str) -> str:
        value = re.sub(r"\[(?:R?\d+|Evidence\s+\d+)]", "", str(value or ""), flags=re.I)
        return re.sub(r"[*_`]", "", value).strip()

    @classmethod
    def _normalize_financial_table_axes(cls, text: str, title: str) -> str:
        """Keep financial metrics on rows and reporting periods on columns."""
        if title != "Key Financials":
            return text
        lines = text.splitlines()
        output = []
        index = 0
        while index < len(lines):
            if index + 2 >= len(lines) or "|" not in lines[index] or not cls._is_table_separator(lines[index + 1]):
                output.append(lines[index])
                index += 1
                continue
            block = [lines[index], lines[index + 1]]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                block.append(lines[index])
                index += 1
            header = cls._table_cells(block[0])
            rows = [cls._table_cells(line) for line in block[2:]]
            consistent = len(header) >= 3 and rows and all(len(row) == len(header) for row in rows)
            metric_headers = sum(
                bool(cls.FINANCIAL_METRIC_PATTERN.search(cls._plain_table_label(cell)))
                for cell in header[1:]
            )
            period_rows = sum(
                bool(cls.FINANCIAL_PERIOD_PATTERN.search(cls._plain_table_label(row[0])))
                for row in rows
            ) if consistent else 0
            should_transpose = (
                consistent
                and metric_headers >= max(2, len(header[1:]) // 2)
                and period_rows >= max(2, len(rows) // 2)
            )
            if not should_transpose:
                output.extend(block)
                continue
            transposed = [["Metric", *[row[0] for row in rows]]]
            transposed.append(["---"] * (len(rows) + 1))
            transposed.extend([
                [header[column], *[row[column] for row in rows]]
                for column in range(1, len(header))
            ])
            output.extend("| " + " | ".join(row) + " |" for row in transposed)
        return "\n".join(output)

    @classmethod
    def _normalize_financial_metric_labels(cls, text: str, title: str) -> str:
        """Relabel amount metrics when every reported value is a percentage."""
        if title != "Key Financials":
            return text
        replacements = {
            "revenue": "Revenue Growth",
            "gross profit": "Gross Margin",
            "ebitda": "EBITDA Margin",
            "pat": "PAT Margin",
        }
        canonical_aliases = {
            "ebitdaoperating": "EBITDA", "operatingebitda": "EBITDA",
            "cogs": "Cost of Goods Sold", "depreciationamortization": "Depreciation and Amortization",
            "depreciationandamortisation": "Depreciation and Amortization",
            "depreciationamortisation": "Depreciation and Amortization", "da": "Depreciation and Amortization",
            "profitbeforetax": "PBT", "profitbeforetaxpbt": "PBT",
            "profitaftertax": "PAT", "profitaftertaxpat": "PAT",
        }
        output = []
        classification_notes = []
        for line in text.splitlines():
            if "|" not in line or cls._is_table_separator(line):
                output.append(line)
                continue
            cells = cls._table_cells(line)
            label = cls._plain_table_label(cells[0]).casefold() if cells else ""
            from .report_financial_format import FINANCIAL_ROWS
            cleaned_label = re.sub(r"\s*\((?:calculated|derived)\)\s*[¹²³⁴⁵⁶⁷⁸⁹⁰]*", "", label).strip()
            canonical = canonical_aliases.get(re.sub(r"[^a-z0-9]", "", cleaned_label))
            metric_names = "|".join(re.escape(name.casefold()) for name in FINANCIAL_ROWS)
            classification = re.fullmatch(rf"({metric_names})\s*\(([^()]+)\)", cleaned_label)
            if classification and not re.search(r"\badjusted\b|\bnormalized\b|\bpro\s*forma\b|\bmargin\b|\bgrowth\b|%|\bexcl(?:uding)?\b", classification.group(2)):
                canonical = next(name for name in FINANCIAL_ROWS if name.casefold() == classification.group(1))
                markers = " ".join(re.findall(r"\[\d+\]", cells[0]))
                classification_notes.append(f"*{canonical} classification: {classification.group(2)}.* {markers}".rstrip())
            if cleaned_label != label:
                canonical = canonical or next((name for name in FINANCIAL_ROWS if name.casefold() == cleaned_label),None)
            if canonical:
                markers = re.findall(r"\[\d+\]",cells[0])
                cells[0] = canonical + (" " + " ".join(markers) if markers else "")
                output.append("| " + " | ".join(cells) + " |")
                continue
            replacement = replacements.get(label)
            if not replacement:
                output.append(line)
                continue
            values = [
                re.sub(r"\[(?:\d+(?:\s*[,;]\s*\d+)*)]", "", cell).strip()
                for cell in cells[1:]
            ]
            numeric_values = [value for value in values if re.search(r"\d", value)]
            if not numeric_values or not all("%" in value for value in numeric_values):
                output.append(line)
                continue
            plain_label = cls._plain_table_label(cells[0])
            cells[0] = re.sub(
                rf"\b{re.escape(plain_label)}\b",
                replacement,
                cells[0],
                count=1,
                flags=re.IGNORECASE,
            )
            output.append("| " + " | ".join(cells) + " |")
        return "\n".join(output) + ("\n\n" + "\n\n".join(dict.fromkeys(classification_notes)) if classification_notes else "")

    @classmethod
    def _append_references(cls, text: str, citations: dict | None, used: list[dict]) -> str:
        references = []
        for item in used:
            if not str(item.get("document_id") or item.get("title") or "").strip():
                continue
            location = str(item.get("used_location") or item.get("location") or "").strip()
            reference = cls._render_inline_citation(item, location=location)
            location_note = f"; cited at {location}" if location else ""
            references.append(
                f"{int(item.get('citation_number') or len(references) + 1)}. "
                f"{reference}{location_note}"
            )
        if not references:
            return text
        return text.rstrip() + "\n\n### Citations\n\n" + "\n".join(references)

    @classmethod
    def _normalize_section(
        cls,
        title: str,
        response: str,
        *,
        citations: dict | None = None,
        minimum_words: int = 0,
        strict_financial_table: bool = False,
        calculation_corrections: list | None = None,
    ) -> str:
        text = str(response or "").strip()
        if "<report_calculations>" in text or "</report_calculations>" in text:
            raise ReportSectionStructureError("Return only the final Markdown report after completing calculator requests.")
        citation_tokens = [
            token.casefold() for token in cls.CITATION_TOKEN_PATTERN.findall(text)
        ]
        if citation_tokens:
            most_common_count = Counter(citation_tokens).most_common(1)[0][1]
            if len(citation_tokens) > 500 or most_common_count > 100:
                raise ReportSectionDegenerateOutputError(
                    f"Report section '{title}' contained degenerate repeated citation output."
                )
        text = re.sub(r"^```(?:markdown)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()
        target = f"## {title}"
        if target in text:
            text = text[text.index(target):]
            next_heading = re.search(r"\n##\s+", text[len(target):])
            if next_heading:
                text = text[:len(target) + next_heading.start()]
        else:
            text = f"{target}\n\n{text}"
        text = cls._strip_model_references(text)
        text, used_citations = cls._replace_internal_citations(text, citations)
        from ai_orchestrator.services.report_calculations import normalize_financial_notation
        text = normalize_financial_notation(text)
        text = cls._normalize_financial_table_axes(text, title)
        text = cls._normalize_financial_metric_labels(text, title)
        if title == "Key Financials":
            text = cls._normalize_financial_amount_cells(text)
            text = cls._expand_financial_period_citations(text, {int(item['citation_number']) for item in used_citations})
            text = cls._cite_supported_financial_calculations(text, {int(item['citation_number']) for item in used_citations})
        # Validate the cited metric, period and normalized reporting basis before
        # testing arithmetic. Correct arithmetic alone cannot establish facts.
        reconciliation_warnings = []
        if strict_financial_table and title == 'Key Financials':
            reconciliation_warnings = cls._validate_financial_table(text,
                verified_citation_numbers={int(item['citation_number']) for item in used_citations}, source_citations=used_citations)
        from ai_orchestrator.services.report_calculations import report_calculation_errors, correct_small_percentage_calculations
        text, corrections = correct_small_percentage_calculations(text)
        if calculation_corrections is not None:
            calculation_corrections.extend(corrections)
        calculation_errors = report_calculation_errors(text)
        if calculation_errors:
            raise ReportSectionStructureError(f"Report section '{title}' has inconsistent calculations: " + "; ".join(calculation_errors[:5]))
        if strict_financial_table and title == "Key Financials":
            if reconciliation_warnings:
                text += (
                    "\n\n### Calculation review warnings\n\n"
                    "The displayed statement has unresolved reconciliation differences. "
                    "Amounts have been preserved; these figures are not verified as a reconciled "
                    "income statement. Confirm source classifications and reporting basis before relying on them.\n\n"
                    + "\n".join(f"- {warning}" for warning in reconciliation_warnings)
                )
        body = text[len(target):].strip()
        if len(body) < 40 and not used_citations:
            raise ReportSectionValidationError(f"Report section '{title}' was empty or incomplete.")
        # Length is editorial guidance, not an acceptance gate. Empty outputs,
        # citations, required statement structure and numerical checks remain.
        return cls._append_references(text, citations, used_citations).strip()

    @classmethod
    def _normalize_financial_amount_cells(cls, text: str) -> str:
        """Keep authored range endpoints and reporting qualifiers explicit."""
        from .report_financial_format import FINANCIAL_ROWS, displayed_amount
        lines, notes = text.splitlines(), []
        expected = [re.sub(r'[^a-z0-9]', '', label.casefold()) for label in FINANCIAL_ROWS]
        number = r'[-+]?\d[\d,]*(?:\.\d+)?'
        range_pattern = re.compile(rf'({number})\s*(?:–|—|\bto\b|-)\s*({number})', re.I)
        index = 0
        while index + 1 < len(lines):
            if not lines[index].strip().startswith('|') or not cls._is_table_separator(lines[index + 1]):
                index += 1
                continue
            end = index + 2
            while end < len(lines) and lines[end].strip().startswith('|'):
                end += 1
            rows = [cls._table_cells(line) for line in lines[index:end]]
            labels = [re.sub(r'[^a-z0-9]', '', cls._plain_table_label(row[0]).casefold()) for row in rows[2:]]
            width = len(rows[0])
            if labels != expected or any(len(row) != width for row in rows):
                index = end
                continue
            ranges = {}
            for row_index, row in enumerate(rows[2:], 2):
                for column in range(1, width):
                    cell = row[column]
                    markers = ' '.join(re.findall(r'\[\d+\]', cell))
                    plain = re.sub(r'\[\d+\]|[*_`]', '', cell).strip()
                    qualifier = re.fullmatch(r'(.+?)\s*\(([^()]+)\)', plain)
                    amount_text = qualifier[1].strip() if qualifier else plain
                    endpoints = range_pattern.fullmatch(amount_text)
                    if endpoints:
                        ranges[row_index, column] = [endpoints[1], endpoints[2]]
                    elif displayed_amount(amount_text) is None:
                        continue
                    row[column] = amount_text + (' ' + markers if markers else '')
                    if qualifier:
                        row_markers = ' '.join(re.findall(r'\[\d+\]', row[0]))
                        notes.append(f"*{cls._plain_table_label(row[0])}, {cls._plain_table_label(rows[0][column])}: {qualifier[2]}.* {markers or row_markers}".rstrip())
            range_columns = {column for _, column in ranges}
            normalized = []
            for row_index, row in enumerate(rows):
                result = [row[0]]
                for column in range(1, width):
                    if column not in range_columns:
                        result.append(row[column])
                        continue
                    if row_index == 0:
                        result.extend([row[column] + ' (Lower bound)', row[column] + ' (Upper bound)'])
                    elif row_index == 1:
                        result.extend(['---', '---'])
                    else:
                        markers = ' '.join(re.findall(r'\[\d+\]', row[column]))
                        endpoints = ranges.get((row_index, column))
                        result.extend([value + (' ' + markers if markers else '') for value in endpoints] if endpoints else [row[column], row[column]])
                normalized.append('| ' + ' | '.join(result) + ' |')
            lines[index:end] = normalized
            index += len(normalized)
        return '\n'.join(lines) + ('\n\n' + '\n\n'.join(dict.fromkeys(notes)) if notes else '')

    @classmethod
    def _expand_financial_period_citations(cls, text: str, verified: set[int]) -> str:
        """Repeat explicitly declared period sources in main-statement numeric cells."""
        from .report_financial_format import FINANCIAL_ROWS, displayed_amount
        lines = text.splitlines()
        expected = [re.sub(r'[^a-z0-9]', '', label.casefold()) for label in FINANCIAL_ROWS]
        index = 0
        while index + 1 < len(lines):
            if not lines[index].strip().startswith('|') or not cls._is_table_separator(lines[index + 1]):
                index += 1
                continue
            end = index + 2
            while end < len(lines) and lines[end].strip().startswith('|'):
                end += 1
            labels = [re.sub(r'[^a-z0-9]', '', cls._plain_table_label(cls._table_cells(line)[0]).casefold()) for line in lines[index + 2:end]]
            headers = cls._table_cells(lines[index])
            if labels == expected:
                for row_index in range(index + 2, end):
                    cells = cls._table_cells(lines[row_index])
                    if len(cells) != len(headers):
                        continue
                    row_markers = {int(value) for value in re.findall(r'\[(\d+)\]', cells[0])}
                    for column in range(1, len(cells)):
                        existing = row_markers | {int(value) for value in re.findall(r'\[(\d+)\]', cells[column])}
                        if existing or displayed_amount(cells[column]) is None:
                            continue
                        markers = [value for value in re.findall(r'\[(\d+)\]', headers[column]) if int(value) in verified]
                        if markers:
                            cells[column] += ' ' + ' '.join(f'[{value}]' for value in dict.fromkeys(markers))
                    lines[row_index] = '| ' + ' | '.join(cells) + ' |'
            index = end
        return '\n'.join(lines)

    @classmethod
    def _cite_supported_financial_calculations(cls, text: str, verified: set[int]) -> str:
        """Attach input-row sources to an uncited derived row only when its arithmetic matches."""
        from .report_financial_format import FINANCIAL_ROWS,displayed_amount
        lines=text.splitlines()
        rows={}
        for index,line in enumerate(lines):
            if not line.strip().startswith('|') or cls._is_table_separator(line): continue
            cells=cls._table_cells(line)
            label=cls._plain_table_label(cells[0])
            if label in FINANCIAL_ROWS: rows[label]=(index,cells)
        equations={
            'Gross Profit':[('Revenue',1),('Cost of Goods Sold',-1)],
            'Operating Expenses':[('Gross Profit',1),('EBITDA',-1)],
            'EBITDA':[('Gross Profit',1),('Operating Expenses',-1)],
            'EBIT':[('EBITDA',1),('Depreciation and Amortization',-1)],
            'Income Tax Expense':[('PBT',1),('PAT',-1)],
            'PAT':[('PBT',1),('Income Tax Expense',-1)],
        }
        for name,inputs in equations.items():
            if name not in rows or not all(key in rows for key,_ in inputs): continue
            index,cells=rows[name]
            if any(int(n) in verified for n in re.findall(r'\[(\d+)\]',lines[index])): continue
            references=set()
            for key,_ in inputs:
                input_line=lines[rows[key][0]]
                cited={int(n) for n in re.findall(r'\[(\d+)\]',input_line)} & verified
                if not cited: break
                references.update(cited)
            else:
                matched=True;has_values=False
                for column,cell in enumerate(cells[1:],1):
                    actual=displayed_amount(cell)
                    if actual is None: continue
                    has_values=True
                    operands=[displayed_amount(rows[key][1][column]) if column<len(rows[key][1]) else None for key,_ in inputs]
                    if any(value is None for value in operands): matched=False;break
                    expected=sum(value[0]*sign for value,(_,sign) in zip(operands,inputs))
                    if abs(actual[0]-expected)>actual[1]+sum(value[1] for value in operands): matched=False;break
                if matched and has_values:
                    cells[0]+=' '+' '.join(f'[{n}]' for n in sorted(references))
                    lines[index]='| '+' | '.join(cells)+' |'
        return '\n'.join(lines)

    @classmethod
    def _validate_financial_table(cls, text: str, *, verified_citation_numbers: set[int] | None = None, source_citations: list[dict] | None = None) -> list[str]:
        from ai_orchestrator.services.report_financial_format import FINANCIAL_ROWS
        tables, pending = [], []
        for line in [*text.splitlines(), ""]:
            if line.strip().startswith("|"):
                pending.append(line)
            elif pending:
                if len(pending) >= 2 and cls._is_table_separator(pending[1]):
                    tables.append(pending)
                pending = []
        normalize = lambda value: re.sub(r"[^a-z0-9]+", "", value.casefold())
        def table_labels(table):
            return [re.sub(r"\[[^]]+\]", "", cls._plain_table_label(cls._table_cells(row)[0])).strip() for row in table[2:]]
        expected = [normalize(v) for v in FINANCIAL_ROWS]
        standardized = [table for table in tables if [normalize(v) for v in table_labels(table)] == expected]
        if len(standardized) > 1:
            raise ReportSectionStructureError("Key Financials contains multiple standardized Revenue-to-PAT tables. Consolidate periods into one main statement; supplemental tables may remain.")
        if standardized:
            table = standardized[0]
        else:
            candidates = [table for table in tables if {'revenue', 'pat'}.issubset({normalize(v) for v in table_labels(table)})]
            if len(candidates) != 1:
                raise ReportSectionStructureError("Key Financials must contain a standardized Revenue-to-PAT table. Supplemental tables are allowed, but do not replace the main statement.")
            table = candidates[0]
        labels = table_labels(table)
        if [normalize(v) for v in labels] != [normalize(v) for v in FINANCIAL_ROWS]:
            raise ReportSectionStructureError("Key Financials row layout is invalid. Expected exactly: " + "; ".join(FINANCIAL_ROWS) + ". Received: " + "; ".join(labels) + ". Keep the source values and citations; repair only labels/order and mark missing inputs Not provided.")
        width = len(cls._table_cells(table[0]))
        if width < 2 or any(len(cls._table_cells(row)) != width for row in table):
            raise ReportSectionStructureError("Key Financials table must have consistent period columns and one metric per row.")
        from ai_orchestrator.services.report_financial_format import displayed_amount
        for label, row in zip(labels, table[2:]):
            for cell in cls._table_cells(row)[1:]:
                if re.search(r"\d", re.sub(r"\[[^]\n]+\]", "", cell)) and displayed_amount(cell) is None:
                    raise ReportSectionStructureError(f"Key Financials row '{label}' must use plain numeric amounts; explain qualifications in prose.")
        from ai_orchestrator.services.report_financial_format import financial_bridge_errors
        # Source statements can use different expense classifications. Report every
        # unresolved bridge visibly without regenerating the whole section. Explicit
        # authored equations and known saved-value mismatches still block acceptance.
        if source_citations:
            from ai_orchestrator.services.report_financial_format import financial_source_errors
            source_errors = financial_source_errors([cls._table_cells(row) for row in table], source_citations, check_citation_support=True)
            if source_errors:
                raise ReportSectionStructureError("Key Financials source values do not match: " + "; ".join(source_errors[:5]))
        return financial_bridge_errors([cls._table_cells(row) for row in table])

    @staticmethod
    def _mark_rejected_section_audit(audit_log_id: str | None, error: ReportSectionValidationError) -> None:
        if not audit_log_id:
            return
        from ai_orchestrator.models import AIAuditLog
        from ai_orchestrator.services.realtime import broadcast_audit_log_update

        audit = AIAuditLog.objects.filter(id=audit_log_id).first()
        if not audit:
            return
        audit.status = "FAILED"
        audit.is_success = False
        audit.error_message = str(error)
        audit.source_metadata = {
            **(audit.source_metadata or {}),
            "inference_state": "rejected",
            "report_section_outcome": "rejected",
        }
        audit.save(update_fields=["status", "is_success", "error_message", "source_metadata"])
        broadcast_audit_log_update(audit, event_type="terminal", done=True)

    @staticmethod
    def _mark_prior_rejected_attempts_retried(*, source_type: str, source_id: str, title: str) -> None:
        from ai_orchestrator.models import AIAuditLog
        from ai_orchestrator.services.realtime import broadcast_audit_log_update

        rejected = AIAuditLog.objects.filter(
            source_type=source_type,
            source_id=str(source_id),
            source_metadata__report_section=title,
            source_metadata__report_section_outcome="rejected",
        )
        for audit in rejected:
            audit.source_metadata = {
                **(audit.source_metadata or {}),
                "inference_state": "retried",
                "report_section_outcome": "retried",
            }
            audit.save(update_fields=["source_metadata"])
            broadcast_audit_log_update(audit, event_type="terminal", done=True)

    @classmethod
    def _generate_section(
        cls,
        *,
        ai_service,
        evidence: str,
        analysis: dict,
        title: str,
        source_id: str,
        evidence_metadata: dict | None = None,
        citations: dict | None = None,
        source_type: str = "email_report_section",
        context_label_prefix: str = "Email report section",
        max_tokens: int | None = None,
        max_input_tokens: int | None = None,
        vdr_dispatch_generation: int | None = None,
        force_regenerate: bool = False,
    ) -> str:
        from .report_financial_format import FINANCIAL_BASIS_RULE
        evidence = FINANCIAL_BASIS_RULE + '\n\n' + evidence
        model_data = analysis.get("deal_model_data") if isinstance(analysis.get("deal_model_data"), dict) else {}
        resolved_stage = cls._resolve_prompt_stage(title)
        revision = resolved_stage.prompt_revision
        if not revision:
            raise ValueError(f"No published live prompt is configured for report section '{title}'.")
        revision_key = f"{revision.id}:r{revision.revision}"
        cache_key = cls._cache_key(
            evidence=evidence,
            model_data=model_data,
            title=title,
            prompt_revision=revision_key,
        )
        cached = None
        if not force_regenerate:
            try:
                cached = cache.get(cache_key)
            except Exception:
                cached = None
        if isinstance(cached, str) and cached:
            return cached

        is_vdr_section = source_type == "vdr_report_section"
        use_report_calculator = is_vdr_section and getattr(settings, "AI_INFERENCE_TARGET", "") == "h100"
        output_budget = int(max_tokens or getattr(settings, "EMAIL_REPORT_SECTION_MAX_TOKENS", 8192))
        minimum_words = cls._minimum_words(title, evidence_metadata) if is_vdr_section else 0
        target_words = (
            max(minimum_words, int(getattr(settings, "VDR_REPORT_SECTION_TARGET_WORDS", 2500)))
            if is_vdr_section else 1200
        )
        cls._mark_prior_rejected_attempts_retried(
            source_type=source_type, source_id=str(source_id), title=title,
        )
        result = ai_service.process_content(
            content=evidence,
            skill_name=None,
            source_type=source_type,
            source_id=str(source_id),
            metadata={
                "pipeline_key": "ic_report_generation",
                "stage_key": resolved_stage.stage.key,
                "section_title": title,
                "minimum_words": f"{minimum_words or 600:,}",
                "target_words": f"{target_words:,}",
                "model_data_json": json.dumps(model_data, ensure_ascii=False, default=str),
                "personality_only_system": True,
                "response_mode": "markdown",
                "report_calculator": use_report_calculator,
                # Keep output tokens for the report. Arithmetic still uses the
                # bounded calculator; acceptance uses deterministic validation.
                **({"chat_template_kwargs": {"enable_thinking": False}} if use_report_calculator else {}),
                "temperature": 0.0,
                "repetition_penalty": float(
                    getattr(settings, "REPORT_SECTION_REPETITION_PENALTY", 1.08)
                ),
                "max_tokens": output_budget,
                **({"max_input_tokens": int(max_input_tokens)} if max_input_tokens else {}),
                "request_timeout": int(getattr(settings, "EMAIL_REPORT_SECTION_TIMEOUT", 1800)),
                "enforce_context_budget": True,
                "lossless_input": True,
                "max_input_chars": max(180_000, len(evidence) + 1024),
                "include_audit_log_id": True,
                "context_label": f"{context_label_prefix}: {title}",
                "_source_metadata": {
                    "report_section": title,
                    "prompt_revision": revision_key,
                    "force_regenerate": bool(force_regenerate),
                    "generation_mode": "grounded_single_pass",
                    "source_review_enabled": False,
                    "citation_validation_enabled": False,
                    "financial_source_validation_enabled": title == 'Key Financials',
                    "financial_validation_order": ['source_metric_and_period', 'currency_and_scale', 'arithmetic'] if title == 'Key Financials' else [],
                    "evidence_retrieval": evidence_metadata or {"strategy": "shared_context"},
                    **({
                        "vdr_parent_audit_id": str(source_id),
                        "vdr_dispatch_generation": int(vdr_dispatch_generation),
                    } if vdr_dispatch_generation is not None else {}),
                },
            },
        )
        calculation_corrections = []
        try:
            if isinstance(result, dict) and result.get('error'):
                raise ReportSectionStructureError(f"Generation for '{title}' could not complete: {result['error']}")
            section = cls._normalize_section(
                title,
                result.get("response") if isinstance(result, dict) else result,
                citations=citations,
                minimum_words=minimum_words,
                strict_financial_table="Key Financials table format:" in revision.user_template,
                calculation_corrections=calculation_corrections,
            )
        except ReportSectionValidationError as exc:
            cls._mark_rejected_section_audit(
                result.get("_audit_log_id") if isinstance(result, dict) else None, exc,
            )
            raise
        if calculation_corrections and isinstance(result, dict) and result.get('_audit_log_id'):
            from ai_orchestrator.models import AIAuditLog
            from django.db import transaction
            with transaction.atomic():
                audit = AIAuditLog.objects.select_for_update().filter(pk=result['_audit_log_id']).first()
                if audit:
                    audit.source_metadata = {
                        **(audit.source_metadata or {}),
                        'calculation_corrections': calculation_corrections,
                    }
                    audit.save(update_fields=['source_metadata'])
        try:
            cache.set(
                cache_key,
                section,
                timeout=int(getattr(settings, "EMAIL_REPORT_SECTION_CACHE_TTL", 7 * 24 * 60 * 60)),
            )
        except Exception:
            pass
        return section

    @classmethod
    def complete(
        cls,
        *,
        ai_service,
        report: str,
        evidence: str,
        analysis: dict,
        source_id: str,
        progress: Callable[[str], None] | None = None,
        evidence_for_section: Callable[[str], dict | str] | None = None,
        source_type: str = "email_report_section",
        context_label_prefix: str = "Email report section",
        max_tokens: int | None = None,
        max_input_tokens: int | None = None,
        force_regenerate: bool = False,
    ) -> str:
        if force_regenerate:
            report = ""
        elif cls.is_complete(report):
            return str(report).strip()
        existing = cls._split(report)
        stable_prefix = cls._stable_prefix_length(report)
        sections = []
        for index, title in enumerate(IC_SECTION_TITLES):
            if index < stable_prefix and existing.get(title):
                sections.append(existing[title])
                continue
            if progress:
                progress(f"Generating report section {index + 1} of {len(IC_SECTION_TITLES)}: {title}")
            section_evidence = evidence
            evidence_metadata = None
            citations = None
            if evidence_for_section:
                retrieved = evidence_for_section(title)
                if isinstance(retrieved, dict):
                    section_evidence = str(retrieved.get("context") or "")
                    evidence_metadata = retrieved.get("metadata")
                    citations = retrieved.get("citations")
                else:
                    section_evidence = str(retrieved or "")
                if not section_evidence.strip():
                    raise ValueError(f"No evidence was retrieved for report section '{title}'.")
                if progress:
                    selected = (evidence_metadata or {}).get("selected_chunk_count")
                    progress(
                        f"Retrieved {selected or 'ranked'} indexed chunks for report section "
                        f"{index + 1} of {len(IC_SECTION_TITLES)}: {title}"
                    )
            sections.append(cls._generate_section(
                ai_service=ai_service,
                evidence=section_evidence,
                analysis=analysis,
                title=title,
                source_id=source_id,
                evidence_metadata=evidence_metadata,
                citations=citations,
                source_type=source_type,
                context_label_prefix=context_label_prefix,
                max_tokens=max_tokens,
                max_input_tokens=max_input_tokens,
                force_regenerate=force_regenerate,
            ))
            if progress:
                progress(f"Completed report section {index + 1} of {len(IC_SECTION_TITLES)}: {title}")
        completed = "\n\n".join(sections)
        if not cls.is_complete(completed):
            raise ValueError("Email synthesis did not produce the complete 11-section IC report.")
        return completed
