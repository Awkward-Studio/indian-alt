"""Complete canonical IC reports section by section when one-shot output is incomplete."""
from __future__ import annotations

import hashlib
import json
import re
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


class ICReportSectionService:
    CACHE_VERSION = "ic-report-sections-v6"
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
    CITATION_CLUSTER_PATTERN = re.compile(
        r"\[(?P<items>(?:Evidence\s+\d+|R0*\d+)"
        r"(?:\s*[,;|]\s*(?:Evidence\s+\d+|R0*\d+))+)]",
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

        # Expand multi-rank clusters first so each rank can be resolved or
        # removed independently without leaving malformed nested brackets.
        text = cls.CITATION_CLUSTER_PATTERN.sub(
            lambda match: ", ".join(
                f"[{item.strip()}]"
                for item in re.split(r"\s*[,;|]\s*", match.group("items"))
            ),
            text,
        )

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
            rendered = re.sub(r"\s+([,.;:])", r"\1", rendered)
            rendered = re.sub(r"([,;|])(?:\s*[,;|])+", r"\1", rendered)
            rendered = re.sub(r"[,;|]\s*([.?!])", r"\1", rendered)
        return rendered, used

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
    ) -> str:
        text = str(response or "").strip()
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
        if citations and not used_citations:
            raise ReportSectionValidationError(
                f"Report section '{title}' returned no verifiable evidence citations."
            )
        text = cls._append_references(text, citations, used_citations)
        body = text[len(target):].strip()
        if len(body) < 40:
            raise ReportSectionValidationError(f"Report section '{title}' was empty or incomplete.")
        unresolved = cls.INTERNAL_CITATION_PATTERN.search(body)
        if unresolved:
            raise ReportSectionValidationError(
                f"Report section '{title}' returned unresolved internal citation "
                f"'{unresolved.group(0)}'."
            )
        unverified_links = cls._unverified_links(body, citations)
        if unverified_links:
            raise ReportSectionValidationError(
                f"Report section '{title}' returned an unverified source link."
            )
        word_count = len(re.findall(r"\b\w+\b", body))
        if minimum_words and word_count < minimum_words:
            raise ReportSectionValidationError(
                f"Report section '{title}' was too short: {word_count} words; "
                f"minimum is {minimum_words}."
            )
        return text.strip()

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
        minimum_words = (
            max(0, int(getattr(settings, "VDR_REPORT_SECTION_MIN_WORDS", 900)))
            if is_vdr_section else 0
        )
        target_words = (
            max(minimum_words, int(getattr(settings, "VDR_REPORT_SECTION_TARGET_WORDS", 2500)))
            if is_vdr_section else 1200
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
                "temperature": 0.0,
                "max_tokens": int(max_tokens or getattr(settings, "EMAIL_REPORT_SECTION_MAX_TOKENS", 8192)),
                **({"max_input_tokens": int(max_input_tokens)} if max_input_tokens else {}),
                "request_timeout": int(getattr(settings, "EMAIL_REPORT_SECTION_TIMEOUT", 1800)),
                "enforce_context_budget": True,
                "context_label": f"{context_label_prefix}: {title}",
                "_source_metadata": {
                    "report_section": title,
                    "prompt_revision": revision_key,
                    "force_regenerate": bool(force_regenerate),
                    "evidence_retrieval": evidence_metadata or {"strategy": "shared_context"},
                    **({
                        "vdr_parent_audit_id": str(source_id),
                        "vdr_dispatch_generation": int(vdr_dispatch_generation),
                    } if vdr_dispatch_generation is not None else {}),
                },
            },
        )
        section = cls._normalize_section(
            title,
            result.get("response") if isinstance(result, dict) else result,
            citations=citations,
            minimum_words=minimum_words,
        )
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
