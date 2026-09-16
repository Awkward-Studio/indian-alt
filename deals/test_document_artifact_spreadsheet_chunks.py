from django.test import SimpleTestCase

from deals.services.document_artifacts import DocumentArtifactService


class SpreadsheetEmbeddingChunkTests(SimpleTestCase):
    def test_cell_manifest_becomes_coordinate_aware_chunks(self):
        manifest = {
            "schema_version": "2",
            "kind": "spreadsheet",
            "chunks": [{
                "text": "legacy broad range",
                "metadata": {"sheet_name": "Revenue Build", "row_start": 1, "row_end": 200},
            }],
            "sheets": [{
                "name": "Revenue Build",
                "cells": [
                    {"coordinate": "A42", "value": "FY25"},
                    {"coordinate": "F42", "value": 100, "number_format": "₹0.0"},
                    {"coordinate": "G42", "value": "=F42*(1+$D$8)", "cached_value": 125},
                    {"coordinate": "B43", "value": "EBITDA", "comment": "Management estimate"},
                    {"coordinate": "G43", "value": 18},
                ],
            }],
        }

        chunks = DocumentArtifactService._spreadsheet_manifest_chunks(
            manifest,
            base_metadata={"document_name": "Model.xlsx"},
        )

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["metadata"]["sheet_name"], "Revenue Build")
        self.assertEqual(chunks[0]["metadata"]["cell_range"], "A42:G43")
        self.assertEqual(chunks[0]["metadata"]["row_start"], 42)
        self.assertEqual(chunks[0]["metadata"]["row_end"], 43)
        self.assertIn("G42==F42*(1+$D$8) [calculated: 125]", chunks[0]["text"])
        self.assertIn("F42=100 [format: ₹0.0]", chunks[0]["text"])
        self.assertIn("B43=EBITDA [comment: Management estimate]", chunks[0]["text"])

    def test_build_embedding_chunks_falls_back_for_legacy_manifest(self):
        document = type("Document", (), {})()
        document.title = "Legacy.xlsx"
        document.document_type = "Financial Model"
        document.extraction_mode = "native"
        document.transcription_status = "complete"
        document.chunking_status = "chunked"
        document.is_indexed = True
        document.normalized_text = ""
        document.extracted_text = ""
        document.reasoning = ""
        document.source_map_json = {}
        document.evidence_json = {
            "document_name": "Legacy.xlsx",
            "document_type": "Financial Model",
            "normalized_text": "Legacy workbook evidence",
            "source_map": {"document_name": "Legacy.xlsx"},
        }
        document.extraction_manifest = {
            "schema_version": "2",
            "kind": "spreadsheet",
            "chunks": [{
                "text": "1\tRevenue\t100",
                "metadata": {
                    "chunk_kind": "spreadsheet_range",
                    "sheet_name": "P&L",
                    "row_start": 1,
                    "row_end": 1,
                    "column_start": "A",
                    "column_end": "C",
                },
            }],
            "sheets": [{"name": "P&L", "cells": []}],
        }

        chunks = DocumentArtifactService.build_embedding_chunks(document)

        self.assertTrue(any(chunk["text"] == "1\tRevenue\t100" for chunk in chunks))
        spreadsheet_chunk = next(chunk for chunk in chunks if chunk["text"] == "1\tRevenue\t100")
        self.assertEqual(spreadsheet_chunk["metadata"]["chunk_kind"], "spreadsheet_range")
