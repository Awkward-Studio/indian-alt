import hashlib
import json
import uuid
from datetime import timedelta
from pathlib import Path

from django.core import signing
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ai_orchestrator.models import AIAuditLog
from ai_orchestrator.services.embedding_processor import EmbeddingService
from deals.models import AnalysisKind, Deal, DealAnalysis
from deals.services.analysis_section_rewrite import AnalysisSectionRewriteService
from meetings.models import MeetingNote, MeetingNoteSource
from meetings.services.meeting_signal_analysis import MeetingSignalAnalysisService


INITIAL_REPORT = """# Project Monsoon Investment Committee Note

## Company Details

Project Monsoon is a Bengaluru-based B2B cold-chain logistics platform serving food and pharmaceutical customers across South and West India.

## Industry Overview

The company operates in temperature-controlled logistics, where network density, asset utilization, compliance, and energy costs determine margins.

## Key Financials

The original teaser reported FY25 revenue of INR 90 Cr, EBITDA margin of 8%, and 74% fleet utilization. FY26 performance remained subject to management confirmation.

## Risk Factors

The initial review identified customer concentration, diesel-price exposure, and incomplete evidence regarding pharmaceutical compliance.

## Investment Rationale

The opportunity may benefit from formalization of cold-chain logistics and increasing demand for validated pharmaceutical distribution.

## Next Steps

Validate FY26 financials, customer retention, fleet utilization, capex requirements, and regulatory compliance.
"""


MEETINGS = (
    {
        "title": "Project Monsoon – FY26 operating review",
        "summary": "Management confirmed FY26 revenue, margins, utilization, and customer retention.",
        "attendees": "Ananya Iyer (CEO), Rohit Menon (CFO), India Alternatives deal team",
        "body": """Management confirmed audited FY26 revenue of INR 128 Cr versus INR 90 Cr in FY25, representing 42.2% year-on-year growth. EBITDA margin improved from 8% to 14% after route-density gains and fuel-surcharge clauses. Fleet utilization averaged 86% in Q4 FY26 versus 74% in FY25. The top customer contributed 18% of revenue, down from 27%, and the top-ten customer retention rate was 96%. CFO Rohit Menon stated that INR 11 Cr of FY26 EBITDA was converted to operating cash flow before growth capex.""",
        "decisions": "Use audited FY26 revenue of INR 128 Cr and 14% EBITDA margin in the IC note.",
        "action_items": "Obtain the signed FY26 audit pack and customer-wise revenue bridge.",
    },
    {
        "title": "Project Monsoon – customer and commercial diligence",
        "summary": "Customers validated service quality, renewal behavior, and contracted pricing protections.",
        "attendees": "Two food customers, one pharmaceutical customer, Ananya Iyer, deal team",
        "body": """Three reference customers described on-time-in-full delivery of 97.4% during the last twelve months. A pharmaceutical customer renewed a three-year contract through March 2029 and confirmed that validated lanes passed two quality audits with no critical observations. Management disclosed that 72% of FY26 revenue is covered by contracts containing quarterly diesel-price pass-through clauses. Net revenue retention was 118%, supported by customers adding new cities and temperature bands. One food customer noted two service disruptions during the June monsoon, each resolved within six hours.""",
        "decisions": "Reflect 97.4% OTIF, 118% net revenue retention, and 72% fuel pass-through coverage in diligence conclusions.",
        "action_items": "Review the pharmaceutical customer's renewal and the two monsoon incident reports.",
    },
    {
        "title": "Project Monsoon – capex, compliance, and downside session",
        "summary": "The team reviewed expansion capex, pharmaceutical certification, and downside liquidity.",
        "attendees": "Rohit Menon (CFO), Operations Head, external logistics adviser, deal team",
        "body": """The FY27 plan requires INR 24 Cr of growth capex for 38 reefer vehicles and two leased cross-dock facilities. Management expects 65% of vehicle capex to be debt funded at an indicative 11.2% interest rate. All four pharmaceutical hubs hold valid GDP certifications through December 2027. In the downside case of 15% lower volume, management forecasts a minimum cash balance of INR 7 Cr and EBITDA margin of 9.5%, assuming discretionary fleet purchases are deferred. The operations head acknowledged that one Pune subcontractor lacked complete temperature calibration records for eleven days in February; no product loss was reported, and the subcontractor was suspended pending remediation.""",
        "decisions": "Add the INR 24 Cr FY27 capex requirement and Pune calibration control failure to the report.",
        "action_items": "Verify capex quotations, financing term sheet, GDP certificates, and subcontractor remediation evidence.",
    },
)


class Command(BaseCommand):
    help = "Create substantive meetings, analyze them, and produce a confirmation-gated section rewrite preview."

    def add_arguments(self, parser):
        parser.add_argument("--report-json", default="/tmp/meeting-rewrite-pipeline.json")
        parser.add_argument("--skip-indexing", action="store_true")

    def handle(self, *args, **options):
        run_id = uuid.uuid4().hex[:10]
        title = f"Project Monsoon Meeting E2E {run_id}"
        deal = Deal.objects.create(title=title, deal_summary=INITIAL_REPORT, sector="Logistics", industry="Cold Chain")
        analysis = DealAnalysis.objects.create(
            deal=deal,
            version=1,
            analysis_kind=AnalysisKind.INITIAL,
            analysis_json={
                "analyst_report": INITIAL_REPORT,
                "canonical_snapshot": {"analyst_report": INITIAL_REPORT},
                "metadata": {"test_run_id": run_id},
            },
        )

        notes = []
        indexing = []
        for offset, fixture in enumerate(MEETINGS):
            note = MeetingNote.objects.create(
                **fixture,
                source=MeetingNoteSource.MANUAL,
                meeting_at=timezone.now() - timedelta(days=14 - offset * 5),
                metadata={"test_run_id": run_id, "sequence": offset + 1},
            )
            note.deals.add(deal)
            indexed = True if options["skip_indexing"] else EmbeddingService().vectorize_meeting_note(note)
            note.refresh_from_db()
            indexing.append({
                "meeting_note_id": str(note.id),
                "indexed": bool(indexed and note.is_indexed) if not options["skip_indexing"] else None,
                "chunk_count": note.chunk_count,
                "embedding_error": note.embedding_error,
            })
            notes.append(note)

        signal_result = MeetingSignalAnalysisService().analyze_deal(deal, notes)
        signal_audit = AIAuditLog.objects.filter(id=signal_result.get("audit_log_id")).first()
        if not signal_audit or not signal_audit.is_success:
            raise CommandError("Meeting signal analysis did not complete successfully.")

        section_title = "Key Financials"
        section_markdown = AnalysisSectionRewriteService.locate_section(INITIAL_REPORT, section_title)[0]
        meeting_evidence = json.dumps(
            {
                "meeting_signal_analysis": signal_result,
                "meeting_notes": [
                    {"title": note.title, "summary": note.summary, "body": note.body, "decisions": note.decisions}
                    for note in notes
                ],
            },
            ensure_ascii=False,
        )
        instruction = (
            "Rewrite Key Financials using the analyzed meeting evidence below. Explicitly state that the updated figures "
            "were confirmed across management, customer, and downside diligence meetings. Include FY26 revenue INR 128 Cr, "
            "42.2% growth, 14% EBITDA margin, 86% utilization, INR 11 Cr operating cash flow, 118% net revenue retention, "
            "and INR 24 Cr FY27 growth capex. Preserve evidence caveats and do not modify other sections.\n\n"
            + meeting_evidence
        )
        rewrite_service = AnalysisSectionRewriteService()
        rewritten = rewrite_service.rewrite(
            deal=deal,
            section_title=section_title,
            section_markdown=section_markdown,
            instruction=instruction,
            full_report=INITIAL_REPORT,
            version=analysis.version,
        )
        token = signing.dumps(
            {
                "deal_id": str(deal.id),
                "version": str(analysis.version),
                "section_title": section_title,
                "report_sha256": hashlib.sha256(INITIAL_REPORT.encode("utf-8")).hexdigest(),
                "section_markdown": rewritten,
            },
            salt="analysis-section-rewrite",
            compress=True,
        )
        analysis.refresh_from_db()
        unchanged_before_confirmation = analysis.analysis_json.get("analyst_report") == INITIAL_REPORT
        required_facts = ["128", "42.2", "14%", "86%", "11", "118%", "24"]
        missing = [fact for fact in required_facts if fact not in rewritten]
        report = {
            "status": "awaiting_confirmation" if not missing and unchanged_before_confirmation else "failed",
            "run_id": run_id,
            "deal_id": str(deal.id),
            "deal_title": deal.title,
            "analysis_id": str(analysis.id),
            "analysis_version": analysis.version,
            "meeting_note_ids": [str(note.id) for note in notes],
            "indexing": indexing,
            "meeting_signal_analysis": signal_result,
            "meeting_signal_audit_success": signal_audit.is_success,
            "section_title": section_title,
            "original_section": section_markdown,
            "rewrite_preview": rewritten,
            "missing_required_facts": missing,
            "stored_report_unchanged_before_confirmation": unchanged_before_confirmation,
            "confirmation_token": token,
            "confirmation_endpoint": f"POST /api/deals/{deal.id}/rewrite_analysis_section/",
        }
        output = Path(options["report_json"])
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        self.stdout.write(json.dumps(report, indent=2, ensure_ascii=False))
        if report["status"] == "failed":
            raise CommandError(f"Meeting rewrite pipeline failed; see {output}")
        self.stdout.write(self.style.WARNING("Rewrite preview generated. Stored report was not changed; explicit confirmation is required."))
