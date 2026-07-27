from datetime import datetime, timezone

from django.test import SimpleTestCase, TestCase

from deals.models import (
    Deal,
    DealDocument,
    VentureIntelligenceCompanyProfile,
    VentureIntelligenceCompanyRelation,
    VentureIntelligenceFinancialStatement,
)
from deals.services.contradiction_detection import (
    ClaimEvidence,
    ContradictionDetectionService,
)
from meetings.models import MeetingNote


class ContradictionClaimNormalizationTests(SimpleTestCase):
    def test_extracts_supported_document_metrics_with_normalized_units_and_periods(self):
        artifact = {
            "source_id": "document-1",
            "document_name": "Management deck.pdf",
            "source_map": {"page": 18},
            "metrics": [
                {
                    "name": "Revenue",
                    "value": "INR 125 Cr",
                    "period": "FY24",
                    "unit": "INR Cr",
                    "source_location": "Page 18",
                    "confidence": "High",
                },
                {
                    "name": "Promoter shareholding",
                    "value": "71.5%",
                    "period": "FY 2024",
                    "unit": "%",
                    "source_location": "Page 42",
                },
                {"name": "Employee count", "value": "300", "period": "FY24"},
            ],
        }

        claims = ContradictionDetectionService.extract_document_claims(
            artifact,
            subject="Acme",
        )

        self.assertEqual(
            [(claim.metric, claim.value, claim.unit, claim.period) for claim in claims],
            [
                ("revenue", 125.0, "INR_crore", "FY2024"),
                ("shareholding_percent", 71.5, "percent", "FY2024"),
            ],
        )
        self.assertTrue(all(claim.evidence.passage for claim in claims))

    def test_extracts_text_claims_but_does_not_label_differences_as_contradictions(self):
        left = ContradictionDetectionService.extract_text_claims(
            "Revenue was INR 120 Cr in FY24.",
            subject="Acme",
            evidence=ClaimEvidence(
                source_type="deal_document",
                source_id="deck",
                source_label="Deck",
                passage="Revenue was INR 120 Cr in FY24.",
            ),
        )[0]
        right = ContradictionDetectionService.extract_text_claims(
            "Revenue reached INR 100 Cr in FY 2024.",
            subject="Acme",
            evidence=ClaimEvidence(
                source_type="meeting_note",
                source_id="meeting",
                source_label="Founder call",
                passage="Revenue reached INR 100 Cr in FY 2024.",
            ),
        )[0]

        comparison = ContradictionDetectionService.build_comparison_candidates(
            [left, right]
        )[0]

        self.assertEqual(comparison.numeric_relation, "different")
        self.assertEqual(comparison.absolute_delta, 20.0)
        self.assertEqual(comparison.classification_status, "requires_classification")
        self.assertNotIn("contradiction", comparison.as_dict().values())

    def test_does_not_compare_different_periods_units_or_same_source(self):
        base_evidence = {
            "source_type": "deal_document",
            "source_label": "Deck",
            "passage": "Supported passage",
        }
        claims = []
        for source_id, period, unit_text, value in (
            ("a", "FY24", "INR Cr", "100"),
            ("b", "FY25", "INR Cr", "120"),
            ("c", "FY24", "USD million", "100"),
            ("a", "FY24", "INR Cr", "90"),
        ):
            claims.extend(
                ContradictionDetectionService.extract_document_claims(
                    {
                        "source_id": source_id,
                        "document_name": f"{source_id}.pdf",
                        "metrics": [
                            {
                                "name": "Revenue",
                                "value": value,
                                "unit": unit_text,
                                "period": period,
                            }
                        ],
                    },
                    subject="Acme",
                )
            )

        self.assertEqual(
            ContradictionDetectionService.build_comparison_candidates(claims),
            [],
        )

    def test_requires_retrievable_evidence(self):
        claim = ContradictionDetectionService._claim_from_fields(
            subject="Acme",
            metric_label="Revenue",
            raw_value="100",
            raw_unit="INR Cr",
            raw_period="FY24",
            evidence=ClaimEvidence(
                source_type="deal_document",
                source_id="deck",
                source_label="Deck",
                passage="",
            ),
        )

        self.assertIsNone(claim)


class DealClaimCollectionTests(TestCase):
    def test_collects_document_meeting_and_target_public_profile_claims(self):
        deal = Deal.objects.create(title="Acme")
        DealDocument.objects.create(
            deal=deal,
            title="Management deck.pdf",
            evidence_json={
                "document_name": "Management deck.pdf",
                "metrics": [
                    {
                        "name": "Revenue",
                        "value": "INR 120 Cr",
                        "unit": "INR Cr",
                        "period": "FY24",
                        "source_location": "Page 10",
                    }
                ],
            },
        )
        note = MeetingNote.objects.create(
            title="Founder call",
            body="EBITDA was INR 18 Cr in FY24.",
            meeting_at=datetime(2024, 8, 1, tzinfo=timezone.utc),
        )
        note.deals.add(deal)
        profile = VentureIntelligenceCompanyProfile.objects.create(
            name="Acme Private Limited",
            shp_year=2024,
            shp_promoter=72.5,
        )
        VentureIntelligenceCompanyRelation.objects.create(
            deal=deal,
            company_profile=profile,
            relation_type="target",
        )
        VentureIntelligenceFinancialStatement.objects.create(
            company_profile=profile,
            statement_type="profit_loss",
            fy="FY24",
            data={"revenue": "INR 118 Cr", "employee_count": 400},
        )

        claims = ContradictionDetectionService.collect_deal_claims(deal)

        self.assertEqual(
            {claim.evidence.source_type for claim in claims},
            {"deal_document", "meeting_note", "public_profile"},
        )
        self.assertEqual(
            {claim.metric for claim in claims},
            {"revenue", "ebitda", "shareholding_percent"},
        )
        revenue_claims = [claim for claim in claims if claim.metric == "revenue"]
        self.assertEqual(len(revenue_claims), 2)
        comparisons = ContradictionDetectionService.build_comparison_candidates(claims)
        self.assertEqual(len(comparisons), 1)
        self.assertEqual(comparisons[0].metric, "revenue")
