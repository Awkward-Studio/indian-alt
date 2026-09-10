from types import SimpleNamespace
from unittest.mock import MagicMock

from django.test import SimpleTestCase

from ai_orchestrator.services.report_section_evidence import ICReportSectionEvidenceService
from ai_orchestrator.services.token_budget import estimate_tokens


class ICReportSectionEvidenceServiceTests(SimpleTestCase):
    def test_retrieval_uses_section_prompt_and_fills_token_budget(self):
        deal = SimpleNamespace(id="deal-1", title="Example Foods")
        documents = [
            SimpleNamespace(id="doc-1", title="Financial Model.xlsx"),
            SimpleNamespace(id="doc-2", title="Information Memorandum.pdf"),
        ]
        chunks = []
        for index in range(120):
            source_id = "doc-1" if index % 3 else "doc-2"
            chunks.append(SimpleNamespace(
                source_type="document",
                source_id=source_id,
                content=f"Unique evidence {index}: " + ("revenue EBITDA margin working capital " * 12),
                metadata={"chunk_kind": "metric", "chunk_index": index},
            ))
        embedding_service = MagicMock()
        embedding_service.search_global_chunks.return_value = chunks
        service = ICReportSectionEvidenceService(
            deal=deal,
            documents=documents,
            embedding_service=embedding_service,
            candidate_limit=100,
            max_chunks=80,
            max_tokens=4_000,
        )

        result = service.retrieve("Key Financials")

        search = embedding_service.search_global_chunks.call_args
        self.assertIn("historical projected P&L", search.args[0])
        self.assertFalse(search.kwargs["rerank"])
        self.assertEqual(search.kwargs["source_ids"], ["doc-1", "doc-2"])
        # Full clickable source citations add context overhead, but retrieval
        # should still use most of this deliberately small test budget.
        self.assertGreaterEqual(result["metadata"]["selected_chunk_count"], 15)
        self.assertLessEqual(estimate_tokens(result["context"]), 4_000)
        self.assertEqual(result["metadata"]["selected_document_count"], 2)

    def test_duplicate_chunk_content_is_included_once(self):
        deal = SimpleNamespace(id="deal-1", title="Example Foods")
        document = SimpleNamespace(id="doc-1", title="Deck.pdf")
        duplicate_text = "The company reported revenue of INR 100 crore."
        chunks = [
            SimpleNamespace(
                source_type="document",
                source_id="doc-1",
                content=duplicate_text,
                metadata={"chunk_kind": kind, "chunk_index": index},
            )
            for index, kind in enumerate(["claim", "normalized_text", "metric"])
        ]
        embedding_service = MagicMock()
        embedding_service.search_global_chunks.return_value = chunks

        result = ICReportSectionEvidenceService(
            deal=deal,
            documents=[document],
            embedding_service=embedding_service,
            max_tokens=4_000,
        ).retrieve("Executive Summary")

        self.assertEqual(result["metadata"]["selected_chunk_count"], 1)
        self.assertEqual(result["context"].count(duplicate_text), 1)

    def test_missing_document_gets_its_own_semantic_coverage_query(self):
        deal = SimpleNamespace(id="deal-1", title="Example Foods")
        documents = [
            SimpleNamespace(id="doc-1", title="Model.xlsx"),
            SimpleNamespace(id="doc-2", title="IM.pdf"),
        ]
        dominant = [
            SimpleNamespace(
                source_type="document",
                source_id="doc-1",
                content=f"Model evidence {index}",
                metadata={"chunk_kind": "metric", "chunk_index": index},
            )
            for index in range(30)
        ]
        supplement = [
            SimpleNamespace(
                source_type="document",
                source_id="doc-2",
                content=f"IM evidence {index}",
                metadata={"chunk_kind": "claim", "chunk_index": index},
            )
            for index in range(8)
        ]
        embedding_service = MagicMock()
        embedding_service.search_global_chunks.side_effect = [dominant, supplement]

        result = ICReportSectionEvidenceService(
            deal=deal,
            documents=documents,
            embedding_service=embedding_service,
            min_chunks_per_document=4,
            max_tokens=8_000,
        ).retrieve("Company Details")

        self.assertEqual(embedding_service.search_global_chunks.call_count, 2)
        self.assertEqual(set(result["metadata"]["selected_document_ids"]), {"doc-1", "doc-2"})
        self.assertEqual(result["metadata"]["supplemented_document_ids"], ["doc-2"])
        self.assertGreaterEqual(result["context"].count("IM evidence"), 4)

    def test_context_exposes_clickable_apa_citation_from_artifact_source_url(self):
        source_url = "https://contoso.sharepoint.com/sites/deals/Investment%20Memorandum.pdf"
        deal = SimpleNamespace(id="deal-1", title="Example Foods")
        document = SimpleNamespace(
            id="doc-1",
            title="Investment Memorandum 2025.pdf",
            file_url=None,
            evidence_json={"source_metadata": {"source_url": source_url}},
        )
        chunk = SimpleNamespace(
            source_type="document",
            source_id="doc-1",
            content="FY25 revenue was INR 100 crore.",
            metadata={"chunk_kind": "metric", "page": 7},
        )
        embedding_service = MagicMock()
        embedding_service.search_global_chunks.return_value = [chunk]

        result = ICReportSectionEvidenceService(
            deal=deal,
            documents=[document],
            embedding_service=embedding_service,
            max_tokens=4_000,
        ).retrieve("Key Financials")

        self.assertIn("Retrieval block R001", result["context"])
        self.assertNotIn("[Evidence 1]", result["context"])
        self.assertIn("Required citation:", result["context"])
        self.assertIn("Investment Memorandum 2025.pdf. (2025).", result["context"])
        self.assertIn(f"](<{source_url}>)", result["context"])
        self.assertEqual(result["citations"]["1"]["location"], "p. 7")
