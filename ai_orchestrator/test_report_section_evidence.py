from types import SimpleNamespace
from unittest.mock import MagicMock

from django.test import TestCase

from ai_orchestrator.models import AIPromptRevision
from ai_orchestrator.services.pipeline_registry import PipelineRegistryService
from ai_orchestrator.services.report_section_evidence import ICReportSectionEvidenceService
from ai_orchestrator.services.token_budget import estimate_tokens, estimate_message_tokens


class ICReportSectionEvidenceServiceTests(TestCase):
    def test_academic_year_headers_are_primary_periods(self):
        document=SimpleNamespace(id='academic',title='Model.xlsx',extraction_manifest={'sheets':[{'name':'Annual','cells':[
            {'coordinate':'B1','value':'AY24'}, {'coordinate':'C1','value':'AY25'}, {'coordinate':'D1','value':'AY27'},
            {'coordinate':'A2','value':'INR Mn'}, {'coordinate':'A4','value':'Revenue'},
            {'coordinate':'B4','value':100}, {'coordinate':'C4','value':200}, {'coordinate':'D4','value':300},
        ]}]})
        service=ICReportSectionEvidenceService(deal=SimpleNamespace(id='deal',title='Example'),documents=[document],embedding_service=MagicMock())
        chunk=SimpleNamespace(source_id='academic',source_type='document',content='B4=100 | C4=200 | D4=300',metadata={'sheet_name':'Annual','chunk_kind':'spreadsheet_cells'})
        facts=service._citation(chunk,rank=1)['financial_cells']
        self.assertEqual([facts[cell]['period'] for cell in ['B4','C4','D4']],['AY24','AY25','AY27'])
    def test_annual_total_columns_are_preserved_on_a_monthly_worksheet(self):
        document=SimpleNamespace(id='mixed',title='Model.xlsx',extraction_manifest={'sheets':[{'name':'Mixed periods','cells':[
            {'coordinate':'B1','value':'2025-01-31'}, {'coordinate':'C1','value':'2025-02-28'},
            {'coordinate':'D1','value':'FY25'}, {'coordinate':'A3','value':'Revenue'},
            {'coordinate':'B3','value':10}, {'coordinate':'C3','value':20}, {'coordinate':'D3','value':300},
        ]}]})
        service=ICReportSectionEvidenceService(deal=SimpleNamespace(id='deal',title='Example'),documents=[document],embedding_service=MagicMock())
        chunk=SimpleNamespace(source_id='mixed',source_type='document',content='B3=10 | C3=20 | D3=300',metadata={'sheet_name':'Mixed periods','chunk_kind':'spreadsheet_cells'})
        facts=service._citation(chunk,rank=1)['financial_cells']
        self.assertEqual(facts['B3']['period_scope'],'monthly')
        self.assertEqual(facts['D3']['period_scope'],'annual')
    def test_metric_label_and_units_are_bound_to_the_same_primary_row(self):
        document = SimpleNamespace(id='row-source', title='Model.xlsx', extraction_manifest={'sheets': [{
            'name': 'Annual statement', 'cells': [
                {'coordinate':'E6','value':'FY24'}, {'coordinate':'F6','value':'FY25'},
                {'coordinate':'B67','value':'Total Income'}, {'coordinate':'D67','value':'INR Mn'},
                {'coordinate':'E67','value':268.99}, {'coordinate':'F67','value':443.95},
                {'coordinate':'A2','value':'USD'}, {'coordinate':'A3','value':'Lakhs'},
            ]}]})
        service=ICReportSectionEvidenceService(deal=SimpleNamespace(id='deal',title='Example'),documents=[document],embedding_service=MagicMock())
        chunk=SimpleNamespace(source_type='document',source_id='row-source',content='E67=268.99 | F67=443.95',
            metadata={'chunk_kind':'spreadsheet_cells','sheet_name':'Annual statement'})
        fact=service._citation(chunk,rank=1)['financial_cells']['F67']
        self.assertEqual(fact['row_label'],'Total Income')
        self.assertEqual(fact['period'],'FY25')
        self.assertEqual(fact['unit_labels'],['INR Mn'])

    @classmethod
    def setUpTestData(cls):
        PipelineRegistryService.ensure_report_pipeline_defaults()

    def test_retrieval_uses_only_published_query_revision(self):
        service = ICReportSectionEvidenceService(
            deal=SimpleNamespace(id="deal-1", title="Example Foods"),
            documents=[],
            embedding_service=MagicMock(),
        )
        stage = PipelineRegistryService.resolve_stage(
            "ic_report_generation", "retrieval_key_financials"
        )
        draft = PipelineRegistryService.create_prompt_draft(
            stage.stage.prompt_definition,
            user_template="Find audited cash flow for {{ deal_title }} in {{ section_title }}",
        )

        self.assertIn("historical projected P&L", service._query("Key Financials"))
        self.assertEqual(draft.status, AIPromptRevision.Status.DRAFT)

        PipelineRegistryService.publish_prompt(draft)

        self.assertEqual(
            service._query("Key Financials"),
            "Find audited cash flow for Example Foods in Key Financials",
        )

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
        self.assertLessEqual(estimate_message_tokens(result["context"]), 4_000)
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

    def test_context_exposes_precise_citation_marker_from_artifact_source_url(self):
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
        self.assertIn("Citation marker: [R001]", result["context"])
        self.assertIn("Document: Investment Memorandum 2025.pdf", result["context"])
        self.assertEqual(result["citations"]["1"]["location"], "p. 7")
        self.assertEqual(result["citations"]["1"]["url"], source_url)

    def test_spreadsheet_citation_retains_verified_bounds(self):
        deal = SimpleNamespace(id="deal-1", title="Example Foods")
        document = SimpleNamespace(
            id="doc-1",
            title="Financial Model.xlsx",
            file_url="https://contoso.sharepoint.com/model.xlsx",
            evidence_json={},
        )
        chunk = SimpleNamespace(
            source_type="document",
            source_id="doc-1",
            content="F42=100 | G42=125 | H42=150",
            metadata={
                "chunk_kind": "spreadsheet_cells",
                "sheet_name": "Revenue Build",
                "row_start": 42,
                "row_end": 49,
                "column_start": "A",
                "column_end": "H",
                "cell_range": "A42:H49",
            },
        )
        embedding_service = MagicMock()
        embedding_service.search_global_chunks.return_value = [chunk]

        result = ICReportSectionEvidenceService(
            deal=deal,
            documents=[document],
            embedding_service=embedding_service,
            max_tokens=4_000,
        ).retrieve("Key Financials")

        citation = result["citations"]["1"]
        self.assertEqual(citation["location"], "Revenue Build!A42:H49")
        self.assertEqual(citation["locator"]["row_start"], 42)
        self.assertIn("[R001@'Revenue Build'!A42:H49]", result["context"])
