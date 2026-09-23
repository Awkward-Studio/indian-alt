import hashlib

from django.contrib.auth.models import User
from django.core import signing
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient
from unittest.mock import MagicMock, patch

from ai_orchestrator.models import AIAuditLog, DocumentChunk
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.pipeline_registry import PipelineRegistryService
from ai_orchestrator.services.prompts import PromptBuilderService
from deals.models import AnalysisKind, Deal, DealAnalysis, DealDocument
from deals.services.analysis_section_rewrite import AnalysisSectionRewriteService
from deals.tasks import rewrite_analysis_section_async
from meetings.models import MeetingNote


REPORT = """# Investment Committee Note

## Company Details

Legacy company description.

## Key Financials

FY25 revenue was INR 90 Cr and EBITDA margin was 8%.

## Risk Factors

Customer concentration requires diligence.
"""


class AnalysisSectionReplacementTests(SimpleTestCase):
    def test_legacy_section_titles_resolve_to_published_prompts(self):
        self.assertEqual(
            AnalysisSectionRewriteService.published_section_title("4. Industry Overview"),
            "Industry Overview",
        )
        self.assertEqual(
            AnalysisSectionRewriteService.published_section_title("Key Financial Highlights"),
            "Key Financials",
        )

    def test_replaces_only_requested_markdown_section(self):
        updated = AnalysisSectionRewriteService.replace_section(
            REPORT,
            "Key Financials",
            "## Key Financials\n\nFY26 revenue was INR 128 Cr and EBITDA margin was 14%.",
        )

        self.assertIn("FY26 revenue was INR 128 Cr", updated)
        self.assertNotIn("FY25 revenue was INR 90 Cr", updated)
        self.assertIn("Legacy company description", updated)
        self.assertIn("Customer concentration requires diligence", updated)

    def test_missing_section_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "was not found"):
            AnalysisSectionRewriteService.replace_section(REPORT, "Exit Considerations", "New text")


class PersistedAnalysisSectionRewriteTests(TestCase):
    def setUp(self):
        PipelineRegistryService.ensure_report_pipeline_defaults()
        self.user = User.objects.create_user(username="rewrite-admin", password="test", is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.deal = Deal.objects.create(title="Project Monsoon", deal_summary=REPORT)
        self.analysis = DealAnalysis.objects.create(
            deal=self.deal,
            version=1,
            analysis_kind=AnalysisKind.INITIAL,
            analysis_json={
                "analyst_report": REPORT,
                "canonical_snapshot": {"analyst_report": REPORT},
            },
        )

    def test_rewrite_adds_analyst_request_after_published_section_prompt(self):
        published = PipelineRegistryService.resolve_stage(
            "ic_report_generation", "key_financials",
        ).prompt_revision
        metadata = {
            "section_title": "Key Financials", "minimum_words": "900",
            "target_words": "2,500", "model_data_json": "{}",
            "rewrite_instruction": "Explain the EBITDA bridge.",
            "section_markdown": "## Key Financials\n\nCurrent draft.",
            "full_report": REPORT,
        }
        base, _ = PromptBuilderService.build_user_prompt(
            published.user_template, "Citation marker: [R001] Revenue source.", metadata,
        )
        prompt = AIProcessorService._append_section_rewrite_request(base, metadata)

        self.assertTrue(prompt.startswith(base))
        self.assertIn("Client evaluation checklist (L1-L3) diligence lens", prompt)
        self.assertIn("Explain the EBITDA bridge.", prompt)
        self.assertIn("Current draft.", prompt)
        self.assertIn("Citation marker: [R001]", prompt)

    @patch("ai_orchestrator.services.embedding_processor.EmbeddingService.search_global_chunks")
    def test_rewrite_prompt_retrieves_indexed_meeting_evidence(self, search_chunks):
        note = MeetingNote.objects.create(
            title="FY26 management review",
            body="FY26 revenue reached INR 128 Cr and EBITDA margin reached 14%.",
            summary="Audited FY26 financial review.",
            is_indexed=True,
            chunk_count=1,
        )
        note.deals.add(self.deal)
        chunk = DocumentChunk.objects.create(
            deal=self.deal,
            source_type="meeting_note",
            source_id=str(note.id),
            content=note.body,
            search_text=note.body,
            embedding=[0.0] * 1024,
            embedding_model="test-embedding",
            embedding_dimensions=1024,
            metadata={"title": note.title, "meeting_note_id": str(note.id)},
        )
        search_chunks.return_value = [chunk]
        ai_service = MagicMock()
        ai_service.process_content.return_value = {
            "response": "## Key Financials\n\nFY26 revenue reached INR 128 Cr, with EBITDA margin at 14%. [R001]"
        }

        AnalysisSectionRewriteService(ai_service).rewrite(
            deal=self.deal,
            section_title="Key Financials",
            section_markdown=AnalysisSectionRewriteService.locate_section(REPORT, "Key Financials")[0],
            instruction="Use the FY26 management meeting.",
            full_report=REPORT,
            version=1,
        )

        prompt = ai_service.process_content.call_args.kwargs["content"]
        self.assertIn("RELEVANT INDEXED MEETING EVIDENCE", prompt)
        self.assertIn("FY26 management review", prompt)
        self.assertIn("FY26 revenue reached INR 128 Cr", prompt)
        search_chunks.assert_called_once()

    @patch("ai_orchestrator.services.embedding_processor.EmbeddingService.search_global_chunks")
    def test_rewrite_prompt_retrieves_indexed_company_news_evidence(self, search_chunks):
        document = DealDocument.objects.create(
            deal=self.deal,
            title="Public Domain News Research - 2026-08-06",
            extracted_text="Perfora reported rapid revenue growth in FY24.",
            normalized_text="Perfora reported rapid revenue growth in FY24.",
            is_indexed=True,
        )
        chunk = DocumentChunk.objects.create(
            deal=self.deal,
            source_type="document",
            source_id=str(document.id),
            content="Perfora revenue increased from INR 1.4 Cr in FY22 to INR 42 Cr in FY24.",
            search_text="Perfora revenue growth FY22 FY24",
            embedding=[0.0] * 1024,
            embedding_model="test-embedding",
            embedding_dimensions=1024,
            metadata={"title": document.title},
        )
        search_chunks.return_value = [chunk]
        ai_service = MagicMock()
        ai_service.process_content.return_value = {
            "response": "## Key Financials\n\nPerfora revenue grew to INR 42 Cr in FY24 from INR 1.4 Cr in FY22. [R001]"
        }

        AnalysisSectionRewriteService(ai_service).rewrite(
            deal=self.deal,
            section_title="Key Financials",
            section_markdown=AnalysisSectionRewriteService.locate_section(REPORT, "Key Financials")[0],
            instruction="Use the latest public news evidence.",
            full_report=REPORT,
            version=1,
        )

        prompt = ai_service.process_content.call_args.kwargs["content"]
        self.assertIn("RELEVANT INDEXED COMPANY NEWS EVIDENCE", prompt)
        self.assertIn(document.title, prompt)
        self.assertIn("INR 42 Cr in FY24", prompt)
        self.assertEqual(search_chunks.call_count, 2)

    @patch("deals.tasks.rewrite_analysis_section_async.apply_async")
    def test_api_queues_audited_rewrite_then_persists_confirmed_preview(self, queue_rewrite):

        payload = {
            "section_title": "Key Financials",
            "section_markdown": AnalysisSectionRewriteService.locate_section(
                REPORT, "Key Financials"
            )[0],
            "instruction": (
                "Use the analyzed meetings: replace the old FY25 figures with FY26 revenue "
                "of INR 128 Cr, 42% growth, and 14% EBITDA margin."
            ),
            "full_report": REPORT,
            "version": 1,
        }
        preview = self.client.post(
            f"/api/deals/{self.deal.id}/rewrite_analysis_section/",
            payload,
            format="json",
        )

        self.assertEqual(preview.status_code, 202)
        self.assertFalse(preview.data["persisted"])
        audit = AIAuditLog.objects.get(id=preview.data["audit_log_id"])
        self.assertEqual(audit.status, "PENDING")
        self.assertEqual(audit.pipeline.key, "ic_report_generation")
        self.assertEqual(audit.pipeline_stage.key, "key_financials")
        self.assertEqual(audit.celery_task_id, preview.data["task_id"])
        self.assertEqual(queue_rewrite.call_args.kwargs["queue"], "high_priority")
        self.assertEqual(queue_rewrite.call_args.kwargs["kwargs"]["instruction"], payload["instruction"])
        self.analysis.refresh_from_db()
        self.assertEqual(self.analysis.analysis_json["analyst_report"], REPORT)

        rewritten = (
            "## Key Financials\n\nManagement confirmed FY26 revenue of INR 128 Cr, "
            "a 42% increase, and EBITDA margin improved to 14% after pricing actions."
        )
        token = signing.dumps({
            "deal_id": str(self.deal.id), "version": "1",
            "section_title": "Key Financials",
            "report_sha256": hashlib.sha256(REPORT.strip().encode("utf-8")).hexdigest(),
            "section_markdown": rewritten,
        }, salt="analysis-section-rewrite", compress=True)

        confirmed = self.client.post(
            f"/api/deals/{self.deal.id}/rewrite_analysis_section/",
            {**payload, "confirmation_token": token},
            format="json",
        )

        self.assertEqual(confirmed.status_code, 200)
        self.assertTrue(confirmed.data["persisted"])
        self.analysis.refresh_from_db()
        self.deal.refresh_from_db()
        saved = self.analysis.analysis_json["analyst_report"]
        self.assertIn("FY26 revenue of INR 128 Cr", saved)
        self.assertNotIn("FY25 revenue was INR 90 Cr", saved)
        self.assertEqual(self.deal.deal_summary, saved)
        self.assertEqual(self.analysis.analysis_json["canonical_snapshot"]["analyst_report"], saved)
        queue_rewrite.assert_called_once()

    @patch("deals.tasks.rewrite_analysis_section_async.apply_async")
    def test_confirmation_rejects_changed_report(self, queue_rewrite):
        payload = {
            "section_title": "Key Financials",
            "section_markdown": AnalysisSectionRewriteService.locate_section(REPORT, "Key Financials")[0],
            "instruction": "Update from meetings.",
            "full_report": REPORT,
            "version": 1,
        }
        preview = self.client.post(
            f"/api/deals/{self.deal.id}/rewrite_analysis_section/", payload, format="json"
        )
        token = signing.dumps({
            "deal_id": str(self.deal.id), "version": "1",
            "section_title": "Key Financials",
            "report_sha256": hashlib.sha256(REPORT.strip().encode("utf-8")).hexdigest(),
            "section_markdown": "## Key Financials\n\nUpdated facts.",
        }, salt="analysis-section-rewrite", compress=True)
        response = self.client.post(
            f"/api/deals/{self.deal.id}/rewrite_analysis_section/",
            {
                **payload,
                "full_report": REPORT + "\nChanged elsewhere.",
                "confirmation_token": token,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.analysis.refresh_from_db()
        self.assertEqual(self.analysis.analysis_json["analyst_report"], REPORT)

    @patch("deals.services.analysis_section_rewrite.AnalysisSectionRewriteService.rewrite")
    @patch("deals.tasks.rewrite_analysis_section_async.apply_async")
    def test_background_rewrite_completes_audit_and_exposes_only_deal_status(self, queue_rewrite, rewrite):
        rewrite.return_value = "## Key Financials\n\nThe revised financial analysis is ready for review."
        payload = {
            "section_title": "Key Financials",
            "section_markdown": AnalysisSectionRewriteService.locate_section(REPORT, "Key Financials")[0],
            "instruction": "Explain the margin change.",
            "full_report": REPORT,
            "version": 1,
        }
        queued = self.client.post(
            f"/api/deals/{self.deal.id}/rewrite_analysis_section/", payload, format="json",
        )
        self.assertEqual(queued.status_code, 202)
        kwargs = queue_rewrite.call_args.kwargs["kwargs"]
        completed = rewrite_analysis_section_async.apply(
            kwargs=kwargs, task_id=queued.data["task_id"],
        )
        self.assertTrue(completed.successful())
        self.assertEqual(completed.result, {"audit_log_id": queued.data["audit_log_id"]})
        self.assertEqual(rewrite.call_args.kwargs["audit_log_id"], queued.data["audit_log_id"])
        self.assertEqual(rewrite.call_args.kwargs["celery_task_id"], queued.data["task_id"])
        audit = AIAuditLog.objects.get(id=queued.data["audit_log_id"])
        self.assertEqual(audit.status, "COMPLETED")
        self.assertTrue(audit.is_success)
        self.assertEqual(audit.prompt_revision.definition.key, "ic_report_section_key_financials")
        self.assertIn("confirmation_token", audit.parsed_json)
        self.assertEqual(audit.parsed_json["report_sha256"], hashlib.sha256(REPORT.strip().encode()).hexdigest())

        with patch("celery.result.AsyncResult") as task_result:
            task_result.return_value.status = "SUCCESS"
            task_result.return_value.result = completed.result
            result = self.client.get(
                f"/api/deals/{self.deal.id}/rewrite_analysis_section_status/{queued.data['task_id']}/",
            )
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.data["section_markdown"], rewrite.return_value)
            other_deal = Deal.objects.create(title="Other deal")
            forbidden = self.client.get(
                f"/api/deals/{other_deal.id}/rewrite_analysis_section_status/{queued.data['task_id']}/",
            )
            self.assertEqual(forbidden.status_code, 404)

    @patch("deals.services.analysis_section_rewrite.AnalysisSectionRewriteService.rewrite")
    @patch("deals.tasks.rewrite_analysis_section_async.apply_async")
    def test_background_rewrite_failure_marks_audit_failed(self, queue_rewrite, rewrite):
        rewrite.side_effect = ValueError("No usable source evidence was retrieved.")
        queued = self.client.post(
            f"/api/deals/{self.deal.id}/rewrite_analysis_section/",
            {
                "section_title": "Key Financials",
                "section_markdown": AnalysisSectionRewriteService.locate_section(REPORT, "Key Financials")[0],
                "instruction": "Refresh the financial analysis.",
                "full_report": REPORT,
                "version": 1,
            },
            format="json",
        )
        self.assertEqual(queued.status_code, 202)
        completed = rewrite_analysis_section_async.apply(
            kwargs=queue_rewrite.call_args.kwargs["kwargs"],
            task_id=queued.data["task_id"],
        )
        self.assertTrue(completed.failed())
        audit = AIAuditLog.objects.get(id=queued.data["audit_log_id"])
        self.assertEqual(audit.status, "FAILED")
        self.assertFalse(audit.is_success)
        self.assertIn("No usable source evidence", audit.error_message)

        with patch("celery.result.AsyncResult") as task_result:
            task_result.return_value.status = "FAILURE"
            response = self.client.get(
                f"/api/deals/{self.deal.id}/rewrite_analysis_section_status/{queued.data['task_id']}/",
            )
        self.assertEqual(response.data["status"], "FAILURE")
        self.assertIn("No usable source evidence", response.data["error"])

    def test_save_rejects_report_changed_after_background_preview(self):
        original_hash = hashlib.sha256(REPORT.strip().encode()).hexdigest()
        changed = REPORT.replace("FY25 revenue was INR 90 Cr", "FY25 revenue was INR 95 Cr")
        self.analysis.analysis_json["analyst_report"] = changed
        self.analysis.analysis_json["canonical_snapshot"]["analyst_report"] = changed
        self.analysis.save(update_fields=["analysis_json"])

        response = self.client.patch(
            f"/api/deals/{self.deal.id}/update_analysis_report/",
            {
                "report": REPORT.replace("8%", "14%"),
                "version": 1,
                "expected_report_sha256": original_hash,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.analysis.refresh_from_db()
        self.assertEqual(self.analysis.analysis_json["analyst_report"], changed)

    @patch("ai_orchestrator.services.report_section_evidence.ICReportSectionEvidenceService")
    def test_known_section_uses_published_writing_stage_and_section_evidence(self, evidence_class):
        DealDocument.objects.create(deal=self.deal, title="Financial statements", is_indexed=True)
        evidence_class.return_value.retrieve.return_value = {
            "context": "Citation marker: [R001]\nDocument: Financial statements\nFY26 revenue INR 128 Cr.",
            "citations": {},
            "metadata": {"selected_chunk_count": 1},
        }
        self.analysis.analysis_json["deal_model_data"] = {"company_name": "Project Monsoon"}
        self.analysis.save(update_fields=["analysis_json"])
        ai_service = MagicMock()
        ai_service.process_content.return_value = {
            "response": "## Key Financials\n\nFY26 revenue was INR 128 Cr, subject to statement review."
        }

        AnalysisSectionRewriteService(ai_service).rewrite(
            deal=self.deal,
            section_title="Key Financials",
            section_markdown=AnalysisSectionRewriteService.locate_section(REPORT, "Key Financials")[0],
            instruction="Explain the FY26 change.",
            full_report=REPORT,
            version=1,
        )

        call = ai_service.process_content.call_args.kwargs
        self.assertEqual(call["metadata"]["pipeline_key"], "ic_report_generation")
        self.assertEqual(call["metadata"]["stage_key"], "key_financials")
        self.assertEqual(call["metadata"]["response_mode"], "markdown")
        self.assertTrue(call["metadata"]["personality_only_system"])
        self.assertEqual(call["metadata"]["rewrite_instruction"], "Explain the FY26 change.")
        self.assertIn("Project Monsoon", call["metadata"]["model_data_json"])
        self.assertIn("FY26 revenue INR 128 Cr", call["content"])
        evidence_class.return_value.retrieve.assert_called_once_with("Key Financials")

    def test_rewrite_prompt_retrieves_attached_deal_document_evidence(self):
        document = DealDocument.objects.create(
            deal=self.deal,
            title="Q3_Investor_Deck.pdf",
            document_type="Pitch Deck",
            extracted_text="Unit economics improved to 28% gross margin in Q3.",
            normalized_text="Unit economics improved to 28% gross margin in Q3.",
            is_indexed=False,
        )
        ai_service = MagicMock()
        ai_service.process_content.return_value = {
            "response": "## Key Financials\n\nThe attached deck reports Q3 gross margin of 28%. [R001]"
        }

        AnalysisSectionRewriteService(ai_service).rewrite(
            deal=self.deal,
            section_title="Key Financials",
            section_markdown=AnalysisSectionRewriteService.locate_section(REPORT, "Key Financials")[0],
            instruction="Include the unit economics from the attached deck.",
            full_report=REPORT,
            version=1,
            document_ids=[str(document.id)],
        )

        prompt = ai_service.process_content.call_args.kwargs["content"]
        self.assertIn("ATTACHED DEAL DOCUMENTS CONTEXT", prompt)
        self.assertIn("Q3_Investor_Deck.pdf", prompt)
        self.assertIn("28% gross margin in Q3", prompt)
