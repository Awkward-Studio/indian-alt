from types import SimpleNamespace
from unittest.mock import Mock

from django.test import SimpleTestCase

from ai_orchestrator.services.report_section_evidence import ICReportSectionEvidenceService
from ai_orchestrator.services.token_budget import estimate_tokens
from deals.services.document_artifacts import DocumentArtifactService
from deals.services.spreadsheet_dependencies import WorkbookFormulaGraph


def financial_model():
    return {"kind": "spreadsheet", "schema_version": "2", "workbook": {"defined_names": [
        {"name": "GrowthRate", "attr_text": "'Assumptions'!$B$2"},
    ]}, "sheets": [
        {"name": "Assumptions", "cells": [
            {"coordinate": "A2", "value": "Growth"}, {"coordinate": "B2", "value": .1},
            {"coordinate": "A3", "value": "Base revenue"}, {"coordinate": "B3", "value": 100},
        ]},
        {"name": "Revenue", "cells": [
            {"coordinate": "B2", "value": "=Assumptions!B3*(1+GrowthRate)", "cached_value": 110},
            {"coordinate": "B3", "value": "=B2*0.2", "cached_value": 22},
        ]},
    ]}


class WorkbookFormulaTests(SimpleTestCase):
    def test_cross_sheet_named_range_and_recursive_precedents(self):
        graph = WorkbookFormulaGraph(financial_model())
        traced = graph.precedents([("Revenue", "B3")])
        self.assertEqual(set(traced["cells"]), {("Revenue", "B2"), ("Assumptions", "B2"), ("Assumptions", "B3")})
        self.assertEqual(traced["warnings"], [])
        self.assertIn("Base revenue", graph.render_cell(("Assumptions", "B3")))

    def test_quoted_sheet_ranges_and_string_literals(self):
        manifest = financial_model()
        manifest["sheets"][0]["name"] = "O'Brien Plan"
        manifest["sheets"][1]["cells"][0]["value"] = '=SUM(\'O\'\'Brien Plan\'!$B$2:$B$3)+IF("Fake!A1"="",1,0)'
        traced = WorkbookFormulaGraph(manifest).precedents([("Revenue", "B2")])
        self.assertEqual(set(traced["cells"]), {("O'Brien Plan", "B2"), ("O'Brien Plan", "B3")})
        self.assertEqual(traced["warnings"], [])

    def test_whole_column_and_3d_references_use_extracted_cells(self):
        graph = WorkbookFormulaGraph(financial_model())
        refs, warnings = graph._expression("=SUM(Assumptions!B:B)", "Revenue")
        self.assertEqual(set(refs), {("Assumptions", "B2"), ("Assumptions", "B3")})
        refs, warnings = graph._expression("=SUM(Assumptions:Revenue!B2)", "Revenue")
        self.assertEqual(set(refs), {("Assumptions", "B2"), ("Revenue", "B2")})

    def test_dynamic_external_errors_missing_cache_and_cycles_are_explicit(self):
        manifest = financial_model()
        manifest["sheets"][1]["cells"] = [
            {"coordinate": "B2", "value": '=B3+INDIRECT("Assumptions!B2")+\'[Other.xlsx]Inputs\'!A1+#REF!'},
            {"coordinate": "B3", "value": "=B2", "cached_value": 0},
        ]
        traced = WorkbookFormulaGraph(manifest).precedents([("Revenue", "B2"), ("Revenue", "B3")])
        warnings = "\n".join(traced["warnings"])
        for text in ("Dynamic reference", "External workbook", "Excel error", "No saved Excel result", "Circular reference"):
            self.assertIn(text, warnings)

    def test_depth_and_size_caps_are_disclosed(self):
        graph = WorkbookFormulaGraph(financial_model())
        self.assertTrue(any("capped" in note for note in graph.precedents([("Revenue", "B3")], max_cells=1)["warnings"]))
        self.assertTrue(any("depth capped" in note for note in graph.precedents([("Revenue", "B3")], max_depth=0)["warnings"]))

    def test_artifact_segments_include_actual_cross_sheet_inputs_and_names(self):
        manifest = financial_model()
        segments = DocumentArtifactService._spreadsheet_artifact_segments(
            file_name="Model.xlsx", manifest=manifest, source_tokens=20_000,
        )
        revenue = next(s for s in segments if "[SHEET: Revenue]" in s)
        self.assertIn("Assumptions!B2=0.1", revenue)
        self.assertIn("Assumptions!B3=100", revenue)
        self.assertIn("growthrate='Assumptions'!$B$2", revenue)

    def test_report_retrieval_adds_citable_precedents_within_context_budget(self):
        manifest = financial_model()
        document = SimpleNamespace(id="model", title="Model.xlsx", extraction_manifest=manifest)
        chunks = DocumentArtifactService._spreadsheet_manifest_chunks(manifest, base_metadata={})
        revenue = next(c for c in chunks if c["metadata"]["sheet_name"] == "Revenue")
        chunk = SimpleNamespace(source_type="document", source_id="model", content=revenue["text"], metadata=revenue["metadata"])
        embeddings = Mock()
        embeddings.search_global_chunks.return_value = [chunk]
        service = ICReportSectionEvidenceService(
            deal=SimpleNamespace(id="deal", title="Example"), documents=[document],
            embedding_service=embeddings, max_tokens=4_000,
        )
        service._query = Mock(return_value="financials")
        result = service.retrieve("Key Financials")
        self.assertIn("B2=0.1", result["context"])
        self.assertIn("B3=100", result["context"])
        self.assertNotIn("never cite this label", result["context"])
        self.assertEqual(result["metadata"]["formula_dependency_chunk_count"], 3)
        self.assertEqual(result["citations"]["3"]["locator"]["sheet_name"], "Assumptions")
        self.assertLessEqual(estimate_tokens(result["context"]), 4_000)
