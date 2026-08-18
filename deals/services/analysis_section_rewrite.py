from __future__ import annotations

import re

from ai_orchestrator.services.ai_processor import AIProcessorService


class AnalysisSectionRewriteService:
    HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

    def __init__(self, ai_service=None):
        self.ai_service = ai_service or AIProcessorService()

    @classmethod
    def locate_section(cls, report: str, section_title: str) -> tuple[str, int, int]:
        title_key = cls._normalize_title(section_title)
        headings = list(cls.HEADING_RE.finditer(report or ""))
        for index, heading in enumerate(headings):
            if cls._normalize_title(heading.group(2)) != title_key:
                continue
            level = len(heading.group(1))
            end = len(report)
            for following in headings[index + 1:]:
                if len(following.group(1)) <= level:
                    end = following.start()
                    break
            return report[heading.start():end].rstrip(), heading.start(), end
        raise ValueError(f"Section '{section_title}' was not found in the analysis report.")

    @classmethod
    def replace_section(cls, report: str, section_title: str, rewritten: str) -> str:
        _current, start, end = cls.locate_section(report, section_title)
        rewritten = (rewritten or "").strip()
        if not rewritten:
            raise ValueError("The rewritten section cannot be empty.")
        if not cls.HEADING_RE.match(rewritten):
            current, _start, _end = cls.locate_section(report, section_title)
            heading = current.splitlines()[0]
            rewritten = f"{heading}\n\n{rewritten}"
        prefix = report[:start].rstrip()
        suffix = report[end:].lstrip()
        return "\n\n".join(part for part in (prefix, rewritten, suffix) if part).strip() + "\n"

    def rewrite(
        self,
        *,
        deal,
        section_title: str,
        section_markdown: str,
        instruction: str,
        full_report: str,
        version=None,
    ) -> str:
        evidence_scope = self._requested_evidence_scope(instruction)
        meeting_context = self._meeting_context(
            deal=deal,
            query=f"{section_title}\n{instruction}",
        ) if evidence_scope in {"all", "meetings", "meetings_and_news"} else ""
        news_context = self._news_context(
            deal=deal,
            query=f"{section_title}\n{instruction}",
        ) if evidence_scope in {"all", "news", "meetings_and_news"} else ""
        result = self.ai_service.process_content(
            content="",
            skill_name=None,
            source_type="analysis_section_rewrite",
            source_id=str(deal.id),
            metadata={
                "model_provider": "vllm",
                "response_mode": "markdown",
                "personality_only_system": True,
                "deal_id": str(deal.id),
                "section_title": section_title,
                "analysis_version": version,
                "max_tokens": 4096,
                "max_input_tokens": 11000,
                "pipeline_key": "analysis_support",
                "stage_key": "section_rewrite",
                "deal_title": deal.title,
                "instruction": instruction,
                "section_markdown": section_markdown,
                "full_report": self._report_context(full_report, section_title),
                "meeting_context": meeting_context or "No indexed meeting evidence matched this rewrite.",
                "news_context": news_context or "No indexed company-news evidence matched this rewrite.",
            },
        )
        if isinstance(result, dict):
            rewritten = result.get("response") or result.get("_raw_response") or result.get("content") or ""
        else:
            rewritten = str(result or "")
        rewritten = rewritten.strip()
        if not rewritten:
            raise ValueError("AI did not return a rewritten section.")
        return rewritten

    @staticmethod
    def _requested_evidence_scope(instruction: str) -> str:
        text = str(instruction or "").casefold()
        meetings = bool(re.search(r"\b(meeting|meetings|meeting notes?|management call|management calls)\b", text))
        news = bool(re.search(r"\b(news|web research|public domain|public-domain|press coverage|media coverage)\b", text))
        if meetings and news:
            return "meetings_and_news"
        if meetings:
            return "meetings"
        if news:
            return "news"
        return "all"

    @classmethod
    def _report_context(cls, report: str, section_title: str, max_chars: int = 12_000) -> str:
        if len(report or "") <= max_chars:
            return report
        _section, start, end = cls.locate_section(report, section_title)
        surrounding_budget = max_chars // 2
        before = report[max(0, start - surrounding_budget):start].lstrip()
        after = report[end:end + surrounding_budget].rstrip()
        return (
            "[PRECEDING REPORT CONTEXT]\n"
            f"{before}\n\n"
            "[TARGET SECTION IS PROVIDED SEPARATELY ABOVE]\n\n"
            "[FOLLOWING REPORT CONTEXT]\n"
            f"{after}"
        )

    @staticmethod
    def _meeting_context(*, deal, query: str, limit: int = 10) -> str:
        from ai_orchestrator.models import DocumentChunk

        meeting_ids = list(
            deal.meeting_notes.filter(is_indexed=True).values_list("id", flat=True)
        )
        if not meeting_ids:
            return ""
        source_ids = [str(value) for value in meeting_ids]
        chunks = []
        try:
            from ai_orchestrator.services.embedding_processor import EmbeddingService

            chunks = EmbeddingService().search_global_chunks(
                query,
                limit=limit,
                deal_ids=[str(deal.id)],
                source_ids=source_ids,
            )
            chunks = [chunk for chunk in chunks if chunk.source_type == "meeting_note"]
        except Exception:
            chunks = []
        if not chunks:
            chunks = list(
                DocumentChunk.objects.filter(
                    deal=deal,
                    source_type="meeting_note",
                    source_id__in=source_ids,
                    embedding__isnull=False,
                ).order_by("-created_at")[:limit]
            )
        blocks = []
        for chunk in chunks[:limit]:
            metadata = chunk.metadata or {}
            blocks.append(
                "\n".join(
                    [
                        f"### Meeting: {metadata.get('title') or chunk.source_id}",
                        f"Meeting note ID: {chunk.source_id}",
                        f"Meeting date: {metadata.get('meeting_at') or 'Not recorded'}",
                        str(chunk.content or "").strip(),
                    ]
                )
            )
        return "\n\n".join(blocks)

    @staticmethod
    def _news_context(*, deal, query: str, limit: int = 10) -> str:
        from ai_orchestrator.models import DocumentChunk

        news_document_ids = list(
            deal.documents.filter(
                is_indexed=True,
                title__istartswith="Public Domain News Research",
            ).values_list("id", flat=True)
        )
        if not news_document_ids:
            return ""
        source_ids = [str(value) for value in news_document_ids]
        try:
            from ai_orchestrator.services.embedding_processor import EmbeddingService

            chunks = EmbeddingService().search_global_chunks(
                query,
                limit=limit,
                deal_ids=[str(deal.id)],
                source_ids=source_ids,
            )
            chunks = [chunk for chunk in chunks if chunk.source_type == "document"]
        except Exception:
            chunks = []
        if not chunks:
            chunks = list(
                DocumentChunk.objects.filter(
                    deal=deal,
                    source_type="document",
                    source_id__in=source_ids,
                    embedding__isnull=False,
                ).order_by("-created_at")[:limit]
            )
        blocks = []
        for chunk in chunks[:limit]:
            metadata = chunk.metadata or {}
            blocks.append(
                "\n".join(
                    [
                        f"### News memo: {metadata.get('title') or chunk.source_id}",
                        f"News document ID: {chunk.source_id}",
                        str(chunk.content or "").strip(),
                    ]
                )
            )
        return "\n\n".join(blocks)

    @staticmethod
    def persist(*, deal, full_report: str, version=None):
        analysis = None
        if version not in (None, ""):
            analysis = deal.analyses.filter(version=int(version)).order_by("-created_at").first()
        if analysis is None:
            analysis = deal.latest_analysis
        if analysis is not None:
            payload = analysis.analysis_json if isinstance(analysis.analysis_json, dict) else {}
            payload["analyst_report"] = full_report
            snapshot = payload.get("canonical_snapshot")
            if isinstance(snapshot, dict):
                snapshot["analyst_report"] = full_report
                payload["canonical_snapshot"] = snapshot
            analysis.analysis_json = payload
            analysis.save(update_fields=["analysis_json"])
        deal.deal_summary = full_report
        deal.save(update_fields=["deal_summary"])
        return analysis

    @staticmethod
    def _normalize_title(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
