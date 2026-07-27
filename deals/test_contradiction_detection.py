from datetime import datetime, timezone
import json
import time
from unittest.mock import MagicMock

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Profile
from ai_orchestrator.models import AIAuditLog
from deals.models import (
    Deal,
    DealContradiction,
    DealDocument,
    VentureIntelligenceCompanyProfile,
    VentureIntelligenceCompanyRelation,
    VentureIntelligenceFinancialStatement,
)
from deals.services.contradiction_detection import (
    ClaimEvidence,
    ContradictionDetectionService,
    DiscrepancyClassifier,
    StructuredClaim,
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


class DiscrepancyClassifierTests(SimpleTestCase):
    def _claim(
        self,
        *,
        value=100,
        period="FY2024",
        unit="INR_crore",
        passage="Revenue was INR 100 Cr in FY24.",
        qualifier="",
        source_id="source-a",
    ):
        return StructuredClaim(
            subject="Acme",
            metric="revenue",
            value=value,
            value_text=str(value),
            unit=unit,
            period=period,
            evidence=ClaimEvidence(
                source_type="deal_document",
                source_id=source_id,
                source_label=f"{source_id}.pdf",
                passage=passage,
            ),
            qualifier=qualifier,
        )

    def test_contract_exposes_every_supported_outcome(self):
        schema = DiscrepancyClassifier.response_format()["json_schema"]["schema"]

        self.assertEqual(
            set(schema["properties"]["classification"]["enum"]),
            {
                "contradiction",
                "definition_difference",
                "time_period_difference",
                "estimate",
                "opinion",
                "insufficient_evidence",
                "no_discrepancy",
            },
        )
        self.assertFalse(schema["additionalProperties"])

    def test_period_and_unit_gates_do_not_call_the_model(self):
        provider = MagicMock()
        classifier = DiscrepancyClassifier(llm_service=provider, model="test-model")

        period_result = classifier.classify(
            self._claim(period="FY2024"),
            self._claim(period="FY2025", source_id="source-b"),
        )
        unit_result = classifier.classify(
            self._claim(),
            self._claim(unit="USD_million", source_id="source-b"),
        )

        self.assertEqual(period_result.classification, "time_period_difference")
        self.assertEqual(unit_result.classification, "definition_difference")
        provider.execute_standard.assert_not_called()

    def test_estimates_and_opinions_are_not_sent_as_factual_contradictions(self):
        provider = MagicMock()
        classifier = DiscrepancyClassifier(llm_service=provider, model="test-model")

        estimate = classifier.classify(
            self._claim(passage="Management forecasts revenue of INR 100 Cr."),
            self._claim(value=120, source_id="source-b"),
        )
        opinion = classifier.classify(
            self._claim(passage="Management believes revenue is strong."),
            self._claim(value=120, source_id="source-b"),
        )

        self.assertEqual(estimate.classification, "estimate")
        self.assertEqual(opinion.classification, "opinion")
        provider.execute_standard.assert_not_called()

    def test_valid_llm_contradiction_preserves_both_evidence_records(self):
        provider = MagicMock()
        provider.execute_standard.return_value = {
            "response": json.dumps(
                {
                    "classification": "contradiction",
                    "confidence": 0.91,
                    "rationale": "Both sources state different actual revenue for FY2024.",
                    "materiality": "high",
                }
            )
        }
        classifier = DiscrepancyClassifier(llm_service=provider, model="test-model")

        result = classifier.classify(
            self._claim(),
            self._claim(value=120, source_id="source-b"),
        )

        self.assertEqual(result.classification, "contradiction")
        self.assertEqual(result.confidence, 0.91)
        self.assertEqual(result.left_evidence.source_id, "source-a")
        self.assertEqual(result.right_evidence.source_id, "source-b")
        payload = provider.execute_standard.call_args.args[0]
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(
            payload["response_format"]["json_schema"]["name"],
            "claim_discrepancy_classification",
        )

    def test_unknown_period_prevents_model_claim_of_contradiction(self):
        provider = MagicMock()
        provider.execute_standard.return_value = {
            "response": json.dumps(
                {
                    "classification": "contradiction",
                    "confidence": 0.99,
                    "rationale": "Values differ.",
                    "materiality": "high",
                }
            )
        }
        classifier = DiscrepancyClassifier(llm_service=provider, model="test-model")

        result = classifier.classify(
            self._claim(period="unspecified"),
            self._claim(value=120, period="unspecified", source_id="source-b"),
        )

        self.assertEqual(result.classification, "insufficient_evidence")
        self.assertIn("periods", result.rationale)

    def test_invalid_model_output_fails_closed(self):
        provider = MagicMock()
        provider.execute_standard.return_value = {
            "response": '{"classification":"definitely_wrong","confidence":8}'
        }
        classifier = DiscrepancyClassifier(llm_service=provider, model="test-model")

        result = classifier.classify(
            self._claim(),
            self._claim(value=120, source_id="source-b"),
        )

        self.assertEqual(result.classification, "insufficient_evidence")
        self.assertEqual(result.confidence, 0.0)


class DealContradictionApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="analyst",
            password="password",
        )
        self.profile = Profile.objects.create(
            user=self.user,
            name="Deal Analyst",
            email="analyst@example.com",
        )
        self.other_user = User.objects.create_user(
            username="other",
            password="password",
        )
        Profile.objects.create(
            user=self.other_user,
            name="Other Analyst",
            email="other@example.com",
        )
        self.deal = Deal.objects.create(
            title="Acme",
            funding_ask="50",
            deal_summary="Canonical summary",
        )
        self.deal.responsibility.add(self.profile)
        self.record = DealContradiction.objects.create(
            deal=self.deal,
            fingerprint="a" * 64,
            subject="Acme",
            metric="revenue",
            period="FY2024",
            unit="INR_crore",
            classification="contradiction",
            confidence=0.91,
            materiality="high",
            rationale="Same-period actual revenue differs.",
            left_claim={
                "value": 100,
                "evidence": {
                    "source_id": "deck",
                    "source_label": "Deck",
                    "passage": "Revenue was INR 100 Cr.",
                },
            },
            right_claim={
                "value": 120,
                "evidence": {
                    "source_id": "meeting",
                    "source_label": "Founder call",
                    "passage": "Revenue was INR 120 Cr.",
                },
            },
        )
        self.url = reverse("deal-contradictions", kwargs={"pk": self.deal.id})
        self.client = APIClient()

    def test_authenticated_user_can_list_evidence_linked_records(self):
        self.client.force_authenticate(self.other_user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], str(self.record.id))
        self.assertEqual(
            response.data[0]["left_claim"]["evidence"]["source_id"],
            "deck",
        )

    def test_responsible_analyst_can_confirm_without_changing_deal(self):
        self.client.force_authenticate(self.user)
        original = {
            "funding_ask": self.deal.funding_ask,
            "deal_summary": self.deal.deal_summary,
            "deal_status": self.deal.deal_status,
        }

        response = self.client.patch(
            self.url,
            {
                "id": str(self.record.id),
                "review_status": "CONFIRMED",
                "analyst_comment": "Confirmed against audited FY24 accounts.",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.record.refresh_from_db()
        self.deal.refresh_from_db()
        self.assertEqual(self.record.review_status, "CONFIRMED")
        self.assertEqual(self.record.reviewed_by, self.profile)
        self.assertIsNotNone(self.record.reviewed_at)
        self.assertEqual(self.record.analyst_comment, "Confirmed against audited FY24 accounts.")
        self.assertEqual(
            {
                "funding_ask": self.deal.funding_ask,
                "deal_summary": self.deal.deal_summary,
                "deal_status": self.deal.deal_status,
            },
            original,
        )

    def test_unassigned_non_admin_cannot_review(self):
        self.client.force_authenticate(self.other_user)

        response = self.client.patch(
            self.url,
            {
                "id": str(self.record.id),
                "review_status": "DISMISSED",
                "analyst_comment": "Not comparable.",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 403)
        self.record.refresh_from_db()
        self.assertEqual(self.record.review_status, "UNREVIEWED")

    def test_completed_review_cannot_be_reversed(self):
        self.record.review_status = "CONFIRMED"
        self.record.reviewed_by = self.profile
        self.record.save(update_fields=["review_status", "reviewed_by"])
        self.client.force_authenticate(self.user)

        response = self.client.patch(
            self.url,
            {
                "id": str(self.record.id),
                "review_status": "DISMISSED",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.record.refresh_from_db()
        self.assertEqual(self.record.review_status, "CONFIRMED")

    def test_invalid_filter_and_cross_deal_identifier_are_rejected(self):
        self.client.force_authenticate(self.user)
        other_deal = Deal.objects.create(title="Other")
        other_record = DealContradiction.objects.create(
            deal=other_deal,
            fingerprint="b" * 64,
            subject="Other",
            metric="revenue",
            classification="contradiction",
            rationale="Evidence",
            left_claim={},
            right_claim={},
        )

        invalid_filter = self.client.get(self.url, {"status": "INVALID"})
        cross_deal = self.client.patch(
            self.url,
            {
                "id": str(other_record.id),
                "review_status": "CONFIRMED",
            },
            format="json",
        )

        self.assertEqual(invalid_filter.status_code, 400)
        self.assertEqual(cross_deal.status_code, 404)

    def test_unauthenticated_requests_are_rejected(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 401)


class DealContradictionPersistenceTests(TestCase):
    def test_pipeline_persistence_is_idempotent_and_preserves_review(self):
        deal = Deal.objects.create(title="Acme")
        left = StructuredClaim(
            subject="Acme",
            metric="revenue",
            value=100,
            value_text="100",
            unit="INR_crore",
            period="FY2024",
            evidence=ClaimEvidence(
                source_type="deal_document",
                source_id="deck",
                source_label="Deck",
                passage="Revenue: 100",
            ),
        )
        right = StructuredClaim(
            subject="Acme",
            metric="revenue",
            value=120,
            value_text="120",
            unit="INR_crore",
            period="FY2024",
            evidence=ClaimEvidence(
                source_type="meeting_note",
                source_id="call",
                source_label="Call",
                passage="Revenue: 120",
            ),
        )
        provider = MagicMock()
        provider.execute_standard.return_value = {
            "response": json.dumps(
                {
                    "classification": "contradiction",
                    "confidence": 0.9,
                    "rationale": "Same-period actuals differ.",
                    "materiality": "high",
                }
            )
        }
        classifier = DiscrepancyClassifier(llm_service=provider, model="test")
        classification = classifier.classify(left, right)

        first, created = classifier.persist_classification(
            deal=deal,
            left=left,
            right=right,
            classification=classification,
        )
        first.review_status = DealContradiction.ReviewStatus.CONFIRMED
        first.save(update_fields=["review_status"])
        second, created_again = classifier.persist_classification(
            deal=deal,
            left=left,
            right=right,
            classification=classification,
        )

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.review_status, DealContradiction.ReviewStatus.CONFIRMED)
        self.assertEqual(DealContradiction.objects.count(), 1)

    def test_end_to_end_runner_persists_result_and_ai_history(self):
        deal = Deal.objects.create(title="Acme")
        for title, value in (("Deck.pdf", 100), ("Accounts.pdf", 120)):
            DealDocument.objects.create(
                deal=deal,
                title=title,
                evidence_json={
                    "document_name": title,
                    "metrics": [
                        {
                            "name": "Revenue",
                            "value": f"INR {value} Cr",
                            "unit": "INR Cr",
                            "period": "FY24",
                            "source_location": "Page 1",
                        }
                    ],
                },
            )
        provider = MagicMock()
        provider.execute_standard.return_value = {
            "response": json.dumps(
                {
                    "classification": "contradiction",
                    "confidence": 0.93,
                    "rationale": "Two actual FY24 revenue values conflict.",
                    "materiality": "high",
                }
            )
        }
        classifier = DiscrepancyClassifier(llm_service=provider, model="test")

        result = classifier.run_for_deal(deal)

        self.assertEqual(result["claims"], 2)
        self.assertEqual(result["comparisons"], 1)
        self.assertEqual(result["persisted"], 1)
        record = DealContradiction.objects.get()
        self.assertEqual(record.classification, "contradiction")
        self.assertEqual(str(record.audit_log_id), result["audit_log_id"])
        audit = AIAuditLog.objects.get(id=result["audit_log_id"])
        self.assertEqual(audit.status, "COMPLETED")
        self.assertTrue(audit.is_success)
        self.assertIsNotNone(audit.completed_at)
        self.assertEqual(audit.source_metadata["persisted_count"], 1)

    def test_comparison_generation_is_bounded_for_large_source_sets(self):
        claims = []
        for index in range(100):
            claims.append(
                StructuredClaim(
                    subject="Acme",
                    metric="revenue",
                    value=float(index),
                    value_text=str(index),
                    unit="INR_crore",
                    period="FY2024",
                    evidence=ClaimEvidence(
                        source_type="deal_document",
                        source_id=f"source-{index}",
                        source_label=f"Source {index}",
                        passage=f"Revenue: {index}",
                    ),
                )
            )

        started_at = time.monotonic()
        comparisons = ContradictionDetectionService.build_comparison_candidates(
            claims,
            max_candidates=75,
        )
        duration = time.monotonic() - started_at

        self.assertEqual(len(comparisons), 75)
        self.assertLess(duration, 1.0)
