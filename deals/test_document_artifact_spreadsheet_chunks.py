from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from deals.services.document_artifacts import DocumentArtifactService


class SpreadsheetEmbeddingChunkTests(SimpleTestCase):
    def test_rebuilds_cell_manifest_from_coordinate_rendering(self):
        manifest = DocumentArtifactService.spreadsheet_manifest_from_text(
            file_name="Legacy Model.xlsx",
            text=(
                "[Sheet: Revenue Build]\n\n"
                "A1=Metric\tB1=FY25\n\n"
                "A2=Revenue\tB2==SUM(B3:B4) [cached value: 125]"
            ),
        )

        self.assertEqual(manifest["kind"], "spreadsheet")
        self.assertEqual(manifest["fallback_fidelity"], "reconstructed_from_stored_text")
        cells = manifest["sheets"][0]["cells"]
        self.assertEqual(cells[0], {"coordinate": "A1", "value": "Metric"})
        self.assertEqual(cells[-1]["coordinate"], "B2")
        self.assertEqual(cells[-1]["value"], "=SUM(B3:B4)")
        self.assertEqual(cells[-1]["cached_value"], "125")
        self.assertEqual(manifest["chunks"][0]["metadata"]["cell_range"], "A1:B2")

    def test_rebuilds_cell_manifest_from_numbered_legacy_rows(self):
        manifest = DocumentArtifactService.spreadsheet_manifest_from_text(
            file_name="Legacy Model.xlsb",
            text="[Sheet: P&L]\n1\tMetric\tFY25\n2\tRevenue\t100",
        )

        cells = manifest["sheets"][0]["cells"]
        self.assertIn({"coordinate": "A2", "value": "Revenue"}, cells)
        self.assertIn({"coordinate": "B2", "value": "100"}, cells)

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

    def test_compact_spreadsheet_text_omits_blank_cells_and_labels_empty_sheets(self):
        manifest = {
            "schema_version": "2",
            "kind": "spreadsheet",
            "content_sha256": "abc123",
            "sheets": [
                {"name": "Empty", "cells": []},
                {
                    "name": "P&L",
                    "cells": [
                        {"coordinate": "A1", "value": "Revenue"},
                        {"coordinate": "B1", "value": "  "},
                        {"coordinate": "F42", "value": "  100\n INR  "},
                    ],
                },
            ],
        }

        compact = DocumentArtifactService.compact_spreadsheet_text(
            file_name="Model.xlsx",
            manifest=manifest,
        )

        self.assertIn("## SHEET: Empty\nEMPTY: no populated cells", compact)
        self.assertIn("## SHEET: P&L", compact)
        self.assertIn("A1=Revenue", compact)
        self.assertIn("F42=100 ⏎ INR", compact)
        self.assertNotIn("B1=", compact)

    def test_artifact_segments_use_only_populated_cells_and_repeat_sheet_context(self):
        manifest = {
            "schema_version": "2",
            "kind": "spreadsheet",
            "sheets": [
                {"name": "Empty", "cells": []},
                {
                    "name": "P&L",
                    "cells": [
                        {
                            "coordinate": f"A{row}",
                            "value": f"Revenue evidence for period {row} with exact value {row * 100}",
                        }
                        for row in range(1, 80)
                    ],
                },
            ],
        }

        segments = DocumentArtifactService._spreadsheet_artifact_segments(
            file_name="Model.xlsx",
            manifest=manifest,
            source_tokens=500,
        )

        self.assertGreater(len(segments), 1)
        self.assertTrue(all("[WORKBOOK: Model.xlsx]" in segment for segment in segments))
        self.assertTrue(all("[SHEET: P&L]" in segment for segment in segments))
        self.assertTrue(all("[CELLS]" in segment for segment in segments))
        self.assertFalse(any("[SHEET: Empty]" in segment for segment in segments))
        combined = "\n".join(segments)
        for row in range(1, 80):
            self.assertIn(f"A{row}=Revenue evidence", combined)

    def test_recursive_spreadsheet_subdivision_retains_context_on_every_piece(self):
        source = (
            "[WORKBOOK: Model.xlsx]\n[SHEET: P&L]\n[RANGE: A1:A80]\n"
            "[POPULATED CELLS ONLY; BLANK CELLS AND ROWS OMITTED]\n[CELLS]\n"
            + "\n".join(f"A{row}=Revenue {row} " * 8 for row in range(1, 80))
        )

        pieces = DocumentArtifactService._split_for_subdivision(
            source,
            source_tokens=400,
            overlap_tokens=20,
            minimum_source_tokens=64,
        )

        self.assertGreater(len(pieces), 1)
        self.assertTrue(all("[SHEET: P&L]" in piece for piece in pieces))
        self.assertTrue(all("[CELLS]" in piece for piece in pieces))

    @patch("deals.services.document_artifacts.cache")
    def test_artifact_build_uses_manifest_instead_of_bloated_rendered_grid(self, mock_cache):
        mock_cache.get.return_value = None
        service = MagicMock()
        service.process_content.return_value = {
            "parsed_json": {
                "document_name": "Model.xlsx",
                "document_summary": "Revenue evidence was extracted.",
                "quality_flags": [],
            },
        }
        manifest = {
            "schema_version": "2",
            "kind": "spreadsheet",
            "sheets": [
                {"name": "Empty", "cells": []},
                {"name": "P&L", "cells": [{"coordinate": "A1", "value": "Revenue"}]},
            ],
        }

        artifact = DocumentArtifactService.build_document_artifact(
            file_name="Model.xlsx",
            extracted_text="## SHEET: Empty\n" + ("1000\t\t\t\n" * 1000),
            extraction_manifest=manifest,
            ai_service=service,
        )

        request = service.process_content.call_args.kwargs
        self.assertIn("A1=Revenue", request["content"])
        self.assertNotIn("1000\t\t\t", request["content"])
        self.assertEqual(request["metadata"]["max_tokens"], 16_384)
        self.assertIn("## SHEET: Empty\nEMPTY: no populated cells", artifact["normalized_text"])

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
