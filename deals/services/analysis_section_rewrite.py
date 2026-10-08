from __future__ import annotations

import json
import re

from django.conf import settings

from ai_orchestrator.prompt_contracts import IC_SECTION_TITLES
from ai_orchestrator.services.ai_processor import AIProcessorService


class AnalysisSectionRewriteService:
    HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
    SECTION_ALIASES = {
        "company overview": "Company Details",
        "promoters and their background": "Promoter and Management Details",
        "strategic fit and market opportunity": "Industry Overview",
        "key financial highlights": "Key Financials",
        "financial deep dive": "Key Financials",
        "financial deep dive include revenue ebitda margins": "Key Financials",
        "key peers and valuation multiples": "Transaction / Trading Multiples",
        "risk matrix top 5 risks": "Risk Factors",
        "red flags and warning signs": "Risk Factors",
        "key observations risks and open points": "Risk Factors",
        "valuation and exit range": "Exit Considerations",
        "next steps data requests": "Next Steps",
    }

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
        document_ids: list[str] | None = None,
        audit_log_id: str | None = None,
        celery_task_id: str | None = None,
        use_review_feedback: bool = True,
        rewrite_scope: str = 'section',
    ) -> str:
        section_title = self.published_section_title(section_title) or section_title
        table_only = rewrite_scope == 'financial_table'
        if rewrite_scope not in {'section', 'financial_table'} or (table_only and section_title != 'Key Financials'):
            raise ValueError('Table rewrites are available only for Key Financials.')
        if table_only:
            from .financial_table_rewrite import locate_main_financial_table
            section_markdown, _, _ = self.locate_section(full_report, section_title)
            current_table, _, _ = locate_main_financial_table(section_markdown)
            instruction += (
                '\n\nScope: regenerate only the single main standardized Revenue-to-PAT table. '
                'Apply the existing Key Financials prompt requirements for source values, fiscal periods, '
                'currency, units, row layout and calculations, together with the analyst instructions. '
                'Return the table and its source references only. Do not rewrite narrative, supplemental '
                'tables or other sections. Do not invent missing amounts; use Not provided. '
                'State currency and units in the table header. Keep the existing currency/unit basis '
                'unless explicitly instructed to convert it.\n\n'
                'Current main table:\n' + current_table
            )
        published_section = section_title in IC_SECTION_TITLES
        evidence_scope = self._requested_evidence_scope(instruction)
        from deals.services.report_coverage import latest_review_feedback, format_review_feedback
        review_feedback = (latest_review_feedback(deal).get(section_title) or {}) if use_review_feedback else {}
        feedback_text = format_review_feedback(review_feedback)
        if feedback_text:
            instruction += '\n\n'+feedback_text
        from ai_orchestrator.services.report_financial_format import FINANCIAL_BASIS_RULE
        prompt_parts = [FINANCIAL_BASIS_RULE]
        citations = {}
        evidence_metadata = None
        if published_section:
            from ai_orchestrator.services.report_section_evidence import ICReportSectionEvidenceService
            from ai_orchestrator.services.token_budget import report_evidence_budget

            indexed_documents = list(deal.documents.filter(is_indexed=True).order_by("title", "id"))
            if indexed_documents:
                try:
                    retrieved = ICReportSectionEvidenceService(
                        deal=deal, documents=indexed_documents,
                        max_tokens=report_evidence_budget(
                            context_window=int(getattr(settings, 'CHAT_MODEL_CONTEXT_TOKENS', 65536)),
                            input_budget=int(getattr(settings, 'VDR_REPORT_SECTION_INPUT_TOKENS', 40960)),
                            output_budget=int(getattr(settings, 'VDR_REPORT_SECTION_MAX_TOKENS', 16384)),
                            evidence_budget=int(getattr(settings, 'VDR_REPORT_SECTION_EVIDENCE_TOKENS', 36000)),
                            extra_context=FINANCIAL_BASIS_RULE+instruction+section_markdown+self._report_context(full_report, section_title)),
                    ).retrieve(section_title)
                except ValueError as exc:
                    if "No indexed document chunks were available" not in str(exc):
                        raise
                    retrieved = {}
                if retrieved.get("context"):
                    prompt_parts.append(retrieved["context"])
                    citations = dict(retrieved.get("citations") or {})
                    evidence_metadata = retrieved.get("metadata")

        evidence_query = f"{section_title}\n{instruction}"
        document_context, document_citations = self._document_context(
            deal=deal, document_ids=document_ids, query=evidence_query,
            start_rank=len(citations) + 1,
        )
        citations.update(document_citations)
        meeting_context, meeting_citations = self._meeting_context(
            deal=deal, query=evidence_query, start_rank=len(citations) + 1,
        ) if evidence_scope in {"all", "meetings", "meetings_and_news"} else ("", {})
        citations.update(meeting_citations)
        news_context, news_citations = self._news_context(
            deal=deal, query=evidence_query, start_rank=len(citations) + 1,
        ) if evidence_scope in {"all", "news", "meetings_and_news"} else ("", {})
        citations.update(news_citations)
        if document_context:
            prompt_parts.append(f"[ATTACHED DEAL DOCUMENTS CONTEXT]\n{document_context}")
        if meeting_context:
            prompt_parts.append(f"[RELEVANT INDEXED MEETING EVIDENCE]\n{meeting_context}")
        if news_context:
            prompt_parts.append(f"[RELEVANT INDEXED COMPANY NEWS EVIDENCE]\n{news_context}")
        content = "\n\n".join(prompt_parts)

        analysis = (
            deal.analyses.filter(version=int(version)).order_by("-created_at").first()
            if version not in (None, "") else deal.latest_analysis
        )
        analysis_payload = analysis.analysis_json if analysis and isinstance(analysis.analysis_json, dict) else {}
        snapshot = analysis_payload.get("canonical_snapshot")
        model_data = analysis_payload.get("deal_model_data")
        if not isinstance(model_data, dict) and isinstance(snapshot, dict):
            model_data = snapshot.get("deal_model_data")
        if not isinstance(model_data, dict):
            model_data = {}
        stage_key = None
        minimum_words = 600
        if published_section:
            from ai_orchestrator.services.bulk_prompt_contracts import IC_REPORT_SECTION_STAGE_KEYS
            from ai_orchestrator.services.report_sections import ICReportSectionService

            stage_key = IC_REPORT_SECTION_STAGE_KEYS[section_title]
            ICReportSectionService._resolve_prompt_stage(section_title)
            minimum_words = ICReportSectionService._minimum_words(section_title, evidence_metadata)

        result = self.ai_service.process_content(
            content=content,
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
                "max_tokens": int(getattr(settings, "VDR_REPORT_SECTION_MAX_TOKENS", 16_384)),
                "max_input_tokens": int(getattr(settings, "VDR_REPORT_SECTION_INPUT_TOKENS", 40_960)),
                "request_timeout": int(getattr(settings, "EMAIL_REPORT_SECTION_TIMEOUT", 1800)),
                "enforce_context_budget": True,
                "lossless_input": True,
                "max_input_chars": max(180_000, len(content) + 1024),
                "temperature": 0.0,
                "repetition_penalty": float(getattr(settings, "REPORT_SECTION_REPETITION_PENALTY", 1.08)),
                **({'report_calculator': True, 'chat_template_kwargs': {'enable_thinking': False}} if table_only else {}),
                **({"personality_only_system": True, "response_mode": "markdown"} if published_section else {}),
                "pipeline_key": "ic_report_generation" if published_section else "analysis_support",
                "stage_key": stage_key or "section_rewrite",
                "deal_title": deal.title,
                "instruction": instruction,
                "rewrite_instruction": instruction if published_section else None,
                "rewrite_scope": rewrite_scope,
                "section_markdown": section_markdown,
                "full_report": self._report_context(full_report, section_title),
                "minimum_words": f"{minimum_words:,}",
                "target_words": f"{max(minimum_words, int(getattr(settings, 'VDR_REPORT_SECTION_TARGET_WORDS', 2500)), int(getattr(settings, 'VDR_KEY_FINANCIALS_TARGET_WORDS', 4500)) if section_title == 'Key Financials' and not table_only else 0):,}",
                "model_data_json": json.dumps(model_data, ensure_ascii=False, default=str),
                "document_context": document_context or "No specific deal documents attached for this rewrite.",
                "meeting_context": meeting_context or "No indexed meeting evidence matched this rewrite.",
                "news_context": news_context or "No indexed company-news evidence matched this rewrite.",
                "audit_log_id": audit_log_id,
                "celery_task_id": celery_task_id,
                "context_label": f"IC section rewrite: {deal.title} / {section_title}",
                "_source_metadata": {
                    "deal_id": str(deal.id),
                    "section_title": section_title,
                    "analysis_version": version,
                    "evidence_retrieval": evidence_metadata or {},
                    "rewrite": True,
                    "rewrite_scope": rewrite_scope,
                    "review_feedback": review_feedback,
                },
            },
        )
        if isinstance(result, dict):
            rewritten = result.get("response") or result.get("_raw_response") or result.get("content") or ""
        else:
            rewritten = str(result or "")
        rewritten = rewritten.strip()
        if not rewritten:
            raise ValueError("AI did not return a rewritten section.")
        if citations or table_only:
            from ai_orchestrator.services.report_sections import ICReportSectionService
            self.calculation_corrections = []
            rewritten = ICReportSectionService._normalize_section(
                section_title, rewritten, citations=citations,
                strict_financial_table=table_only,
                calculation_corrections=self.calculation_corrections,
            )
        if table_only:
            from .financial_table_rewrite import merge_financial_table
            warning_block = re.search(r'^### (?:Calculation review warnings|Source gaps and calculation issues)\n(.*?)(?=^### |\Z)', rewritten, re.M | re.S)
            self.table_calculation_warnings = warning_block[1].strip() if warning_block else ''
            rewritten, _ = merge_financial_table(section_markdown, rewritten)
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
    def _extra_citation(*, rank: int, source_id: str, title: str, url: str = "", location: str = "") -> dict:
        return {
            "rank": rank, "document_id": source_id, "title": title,
            "url": url, "location": location, "locator": {},
        }

    @staticmethod
    def _meeting_context(*, deal, query: str, limit: int = 10, start_rank: int = 1) -> tuple[str, dict]:
        from ai_orchestrator.models import DocumentChunk

        meeting_ids = list(
            deal.meeting_notes.filter(is_indexed=True).values_list("id", flat=True)
        )
        if not meeting_ids:
            return "", {}
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
        citations = {}
        for chunk in chunks[:limit]:
            metadata = chunk.metadata or {}
            rank = start_rank + len(blocks)
            title = str(metadata.get('title') or chunk.source_id)
            blocks.append(
                "\n".join(
                    [
                        f"Citation marker: [R{rank:03d}]",
                        f"### Meeting: {title}",
                        f"Meeting note ID: {chunk.source_id}",
                        f"Meeting date: {metadata.get('meeting_at') or 'Not recorded'}",
                        str(chunk.content or "").strip(),
                    ]
                )
            )
            citations[str(rank)] = AnalysisSectionRewriteService._extra_citation(
                rank=rank, source_id=str(chunk.source_id), title=f"Meeting: {title}",
                location=str(metadata.get("meeting_at") or ""),
            )
        return "\n\n".join(blocks), citations

    @staticmethod
    def _news_context(*, deal, query: str, limit: int = 10, start_rank: int = 1) -> tuple[str, dict]:
        from ai_orchestrator.models import DocumentChunk

        news_document_ids = list(
            deal.documents.filter(
                is_indexed=True,
                title__istartswith="Public Domain News Research",
            ).values_list("id", flat=True)
        )
        if not news_document_ids:
            return "", {}
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
        citations = {}
        for chunk in chunks[:limit]:
            metadata = chunk.metadata or {}
            rank = start_rank + len(blocks)
            title = str(metadata.get('title') or chunk.source_id)
            blocks.append(
                "\n".join(
                    [
                        f"Citation marker: [R{rank:03d}]",
                        f"### News memo: {title}",
                        f"News document ID: {chunk.source_id}",
                        str(chunk.content or "").strip(),
                    ]
                )
            )
            citations[str(rank)] = AnalysisSectionRewriteService._extra_citation(
                rank=rank, source_id=str(chunk.source_id), title=title,
            )
        return "\n\n".join(blocks), citations

    @staticmethod
    def _document_context(
        *, deal, document_ids: list[str] | None = None, query: str = "",
        limit: int = 10, start_rank: int = 1,
    ) -> tuple[str, dict]:
        if not document_ids:
            return "", {}
        from ai_orchestrator.models import DocumentChunk
        from ai_orchestrator.services.report_section_evidence import ICReportSectionEvidenceService

        clean_ids = [str(did).strip() for did in document_ids if str(did).strip()]
        if not clean_ids:
            return "", {}

        documents = list(deal.documents.filter(id__in=clean_ids))
        if not documents:
            return "", {}

        blocks = []
        citations = {}
        for doc in documents:
            title = doc.title or f"Document {doc.id}"
            doc_type = doc.document_type or "Document"
            chunks = []
            source_id = str(doc.id)
            if doc.is_indexed:
                try:
                    from ai_orchestrator.services.embedding_processor import EmbeddingService
                    chunks = EmbeddingService().search_global_chunks(
                        query,
                        limit=limit,
                        deal_ids=[str(deal.id)],
                        source_ids=[source_id],
                    )
                    chunks = [c for c in chunks if c.source_type == "document"]
                except Exception:
                    chunks = []
                if not chunks:
                    chunks = list(
                        DocumentChunk.objects.filter(
                            deal=deal,
                            source_type="document",
                            source_id=source_id,
                        ).order_by("chunk_index")[:limit]
                    )

            if chunks:
                chunk_texts = [str(c.content or "").strip() for c in chunks if str(c.content or "").strip()]
                body = "\n\n".join(chunk_texts)
            else:
                body = str(doc.normalized_text or doc.extracted_text or "").strip()

            if body:
                if len(body) > 12_000:
                    body = body[:12_000] + "\n...[Content truncated for context budget]..."
                rank = start_rank + len(blocks)
                blocks.append(
                    f"Citation marker: [R{rank:03d}]\n"
                    f"### Document: {title}\n"
                    f"Document ID: {doc.id}\n"
                    f"Type: {doc_type}\n"
                    f"{body}"
                )
                citations[str(rank)] = AnalysisSectionRewriteService._extra_citation(
                    rank=rank, source_id=source_id, title=title,
                    url=ICReportSectionEvidenceService._document_source_url(doc),
                    location="selected document",
                )

        return "\n\n".join(blocks), citations

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

    @classmethod
    def published_section_title(cls, value: str) -> str | None:
        normalized = cls._normalize_title(
            re.sub(r"^\s*(?:\d+|[IVX]+)[.)-]\s*", "", str(value or ""), flags=re.I)
        )
        for title in IC_SECTION_TITLES:
            if cls._normalize_title(title) == normalized:
                return title
        return cls.SECTION_ALIASES.get(normalized)
