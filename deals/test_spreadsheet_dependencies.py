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
    def test_extracted_metric_resolves_its_exact_primary_workbook_cell(self):
        import json
        from ai_orchestrator.services.report_financial_format import FINANCIAL_ROWS, financial_source_errors
        manifest = {'sheets': [{'name':'IS','cells':[
            {'coordinate':'A1','value':'INR'}, {'coordinate':'G3','value':'2027-28'},
            {'coordinate':'B8','value':'EBITDA'}, {'coordinate':'G8','value':'=G6-G7','cached_value':'2154373474.2875'}]}]}
        doc = SimpleNamespace(id='model',title='Model.xlsx',extraction_manifest=manifest)
        service = ICReportSectionEvidenceService(deal=SimpleNamespace(id='deal',title='Example'),documents=[doc],embedding_service=Mock())
        metric = {'name':'EBITDA (FY 2027-28)','value':'2,154,373,474.2875','unit':'INR','period':'2027-28','source_location':'IS!G8'}
        chunk = SimpleNamespace(source_id='model',metadata={'chunk_kind':'metric'},content=json.dumps(metric))
        citation = service._citation(chunk,rank=1)
        self.assertEqual(citation['visible_cells'], ['G8'])
        self.assertEqual(citation['financial_cells']['G8']['row_label'], 'EBITDA')
        chunk.content=json.dumps({**metric,'source_location':'IS, G8'})
        self.assertEqual(service._citation(chunk,rank=1)['visible_cells'], ['G8'])
        citation['citation_number']=1
        rows=[['Metric (INR Cr)','FY 2027-28 Forecast'],['---','---'],*[[name,'215.44 [1]' if name=='EBITDA' else 'Not provided'] for name in FINANCIAL_ROWS]]
        self.assertEqual(financial_source_errors(rows,[citation]), [])
        rows[6][1]='225.44 [1]'
        self.assertTrue(financial_source_errors(rows,[citation]))
        chunk.content=json.dumps({**metric,'value':'999'})
        self.assertEqual(service._citation(chunk,rank=1)['financial_cells'], {})
        chunk.content=json.dumps({**metric,'source_location':'IS!G99'})
        self.assertEqual(service._citation(chunk,rank=1)['financial_cells'], {})

    def test_numeric_saved_strings_and_explicit_workbook_rupees_remain_verifiable(self):
        from ai_orchestrator.services.report_financial_format import FINANCIAL_ROWS, financial_source_errors
        manifest = {'sheets': [
            {'name': 'Inputs', 'cells': [{'coordinate': 'B7', 'value': 'Revenue (₹)'}]},
            {'name': 'IS', 'cells': [{'coordinate': 'C3', 'value': '2023-24'},
                {'coordinate': 'D3', 'value': '2024-25'},
                {'coordinate': 'B4', 'value': 'Total Revenue'},
                {'coordinate': 'C4', 'value': '=Inputs!C7', 'cached_value': '122746186'},
                {'coordinate': 'D4', 'value': '=Inputs!D7', 'cached_value': '465805011'},
                {'coordinate': 'C5', 'value': 'Not provided'}, {'coordinate': 'C6', 'value': 'NaN'}]}]}
        doc = SimpleNamespace(id='model', title='Model.xlsx', extraction_manifest=manifest)
        service = ICReportSectionEvidenceService(deal=SimpleNamespace(id='deal', title='Example'), documents=[doc], embedding_service=Mock())
        chunk = SimpleNamespace(source_id='model', content='C4=122746186\nD4=465805011\nC5=Not provided\nC6=NaN', metadata={'sheet_name':'IS'})
        citation = service._citation(chunk, rank=1)
        self.assertEqual(citation['financial_cells']['C4']['value'], '122746186')
        self.assertEqual(citation['financial_cells']['D4']['row_label'], 'Total Revenue')
        self.assertNotIn('C5', citation['financial_cells'])
        self.assertNotIn('C6', citation['financial_cells'])
        citation['citation_number'] = 1
        rows = [['Metric (INR Cr)', 'FY 2023-24 Actual'], ['---', '---'], *[[name, '12.27 [1]' if name == 'Revenue' else 'Not provided'] for name in FINANCIAL_ROWS]]
        self.assertEqual(financial_source_errors(rows, [citation]), [])
        rows[0][1] = 'FY 2024-25 Actual'
        rows[2][1] = '46.58 [1]'
        self.assertEqual(financial_source_errors(rows, [citation]), [])
        rows[2][1] = '13.27 [1]'
        self.assertTrue(financial_source_errors(rows, [citation]))
        manifest['sheets'][0]['cells'].append({'coordinate': 'B8', 'value': 'USD millions'})
        service = ICReportSectionEvidenceService(deal=SimpleNamespace(id='deal', title='Example'), documents=[doc], embedding_service=Mock())
        self.assertEqual(service._citation(chunk, rank=1)['financial_cells']['C4']['unit_labels'], [])

    def test_distant_period_header_is_preserved_for_income_statement_cells(self):
        manifest = {"sheets": [{"name": "IS", "cells": [
            {"coordinate": "G4", "value": "FY27E"},
            {"coordinate": "B28", "value": "Depreciation & Amortization"},
            {"coordinate": "G28", "value": "=Inputs!B1", "cached_value": .47549},
        ]}]}
        text = WorkbookFormulaGraph(manifest).render_cell(("IS", "G28"))
        self.assertIn("B28=Depreciation & Amortization", text)
        self.assertIn("G4=FY27E", text)
        self.assertIn("0.47549", text)

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

    def test_retrieval_retains_independent_year_headers_and_units_for_each_sheet(self):
        manifest = {"sheets": [
            {"name": "IS", "cells": [{"coordinate": "B3", "value": "INR Crores"},
                {"coordinate": "F4", "value": "FY26E"}, {"coordinate": "G4", "value": "FY27E"},
                {"coordinate": "B28", "value": "Depreciation"}, {"coordinate": "G28", "value": "=F28", "cached_value": .5}]},
            {"name": "CF", "cells": [{"coordinate": "F4", "value": "FY27E"},
                {"coordinate": "B25", "value": "Capex"}, {"coordinate": "F25", "value": -6.75}]},
        ]}
        document = SimpleNamespace(id="model", title="Model.xlsx", extraction_manifest=manifest)
        chunks = [SimpleNamespace(source_type="document", source_id="model", content=f"{cell}=1",
            metadata={"sheet_name": sheet, "row_start": row, "row_end": row, "column_start": column, "column_end": column})
            for sheet,cell,row,column in [("IS","G28",28,"G"),("CF","F25",25,"F")]]
        service = ICReportSectionEvidenceService(deal=SimpleNamespace(id="deal", title="Example"),
            documents=[document], embedding_service=Mock(), max_tokens=20_000)
        result, _ = service._formula_dependencies(chunks, token_budget=5000)
        headers = {chunk.metadata["sheet_name"]:chunk.content for chunk in result if chunk.metadata["chunk_kind"] == "spreadsheet_schedule_headers"}
        self.assertIn("G4=", headers["IS"])
        self.assertIn("INR Crores", headers["IS"])
        self.assertIn("F4=", headers["CF"])
