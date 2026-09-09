from django.test import SimpleTestCase, TestCase

from ai_orchestrator.prompt_contracts import IC_REPORT_HEADERS

from .services.report_status import (
    analyst_report_from_payload,
    is_complete_analyst_report,
)


class AnalystReportStatusTests(SimpleTestCase):
    def test_accepts_the_exact_ordered_ic_report(self):
        report = "\n\n".join(f"{header}\n\nEvidence-backed content." for header in IC_REPORT_HEADERS)
        self.assertTrue(is_complete_analyst_report({"analyst_report": report}))

    def test_rejects_missing_or_misordered_sections(self):
        report = "\n\n".join(f"{header}\n\nEvidence-backed content." for header in IC_REPORT_HEADERS[:-1])
        self.assertFalse(is_complete_analyst_report({"analyst_report": report}))

        reordered = list(IC_REPORT_HEADERS)
        reordered[0], reordered[1] = reordered[1], reordered[0]
        report = "\n\n".join(f"{header}\n\nEvidence-backed content." for header in reordered)
        self.assertFalse(is_complete_analyst_report({"analyst_report": report}))

    def test_uses_canonical_snapshot_when_top_level_report_is_blank(self):
        report = "\n\n".join(f"{header}\n\nEvidence-backed content." for header in IC_REPORT_HEADERS)
        payload = {"analyst_report": "", "canonical_snapshot": {"analyst_report": report}}
        self.assertEqual(analyst_report_from_payload(payload), report)
        self.assertTrue(is_complete_analyst_report(payload))


class CompleteReportFilterTests(TestCase):
    def test_filter_uses_the_latest_analysis_record(self):
        from .models import Deal, DealAnalysis
        from .views import DealFilterSet, DealViewSet

        complete = "\n\n".join(f"{header}\n\nEvidence-backed content." for header in IC_REPORT_HEADERS)
        complete_deal = Deal.objects.create(title="Complete report")
        incomplete_deal = Deal.objects.create(title="Incomplete report")
        DealAnalysis.objects.create(deal=complete_deal, analysis_json={"analyst_report": complete})
        DealAnalysis.objects.create(deal=incomplete_deal, analysis_json={"analyst_report": "## Executive Summary\n\nPartial"})

        queryset = DealViewSet.queryset.all()
        filtered = DealFilterSet(
            data={"has_complete_analysis": "true"},
            queryset=queryset,
        ).qs

        self.assertEqual(list(filtered.values_list("title", flat=True)), ["Complete report"])
